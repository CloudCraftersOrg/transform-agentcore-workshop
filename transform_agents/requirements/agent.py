"""Agent 2: Requirements analysis (the AWS Transform job-objective intake and HITL clarifications analogue).

Python parses the uploaded document, keeps the spec draft and validates it. The model fills the draft through
small tool calls: it maps every requirement (priority, acceptance, traces to discovery findings), records
conflicts and turns open points into explicit assumptions.
"""

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator

from transform_agents.core import (
    AgentRequest,
    ResultBox,
    Workspace,
    error,
    model_id_for,
    run_agent,
    status,
    workspace_tools,
)

from .tools import (
    APPENDIX_MARKER,
    REPORT,
    RequirementsContext,
    assessment_digest,
    finalize_report,
    load_sample_requirements,
    make_tools,
    narrative_report,
)

STAGE = "requirements"
TIMEOUT_S = 90  # typical run: ~18 s; override with options.timeout_s
RESUME_PROMPT = """Continue the task. Your previous attempt stopped before submit_result (for example a malformed tool
call), but the draft keeps everything set so far. Call check_spec() to see what is still missing, fix it with
set_fields / set_lists (at most 12 requirements per set_lists call), write the report if it is missing, then call
submit_result(summary)."""

SYSTEM_PROMPT = """You are the Requirements agent of a migration factory (like AWS Transform's job objective intake
with human-in-the-loop clarifications). You turn an uploaded migration request into a validated migration spec that
every later stage (code, Terraform, Kubernetes, FinOps) follows.

Work in this order:
1. Call read_requirements and get_spec_template (both in one turn). The template is your spec draft.
2. Fill the draft with set_fields and set_lists (you may call both in one turn):
   - set_fields: every null scalar field as a flat map {"section.field": value}, for example
     {"target.region": "us-west-2", "workload.peak_rps": 40, "data.multi_az": true}.
     Copy numbers from the document exactly (budget, peak rps, p95 latency, SLO, RPO and RTO in minutes, minimum
     replicas); never invent them. Keep the platform defaults already filled in (service name, container port,
     probe paths, versions) unless the document asks for something else. data.multi_az is true when the document asks to
     survive the loss of an Availability Zone.
   - set_lists(requirements=...): one entry per requirement id in the document (G*, FR*, NFR*, SEC*, TC*) with
     priority: must (mandatory), should (preferred or important), could (low priority or optional);
     acceptance: one measurable sentence a tester can verify;
     traces_to: the assessment finding ids this requirement addresses ([] if none).
     Do not copy the requirement text: it is filled in automatically.
   - set_lists(gaps=...): every critical or high finding that no requirement traces, with the concrete action
     the migration takes.
   - set_lists(conflicts=...): requirements that pull against each other (for example the cost cap vs. high
     availability, growth vs. budget, a read-only filesystem vs. writing reports), each with your resolution.
   - set_lists(open_questions=...): one per open point in the document, with the question, the assumption you
     make until it is answered, and the impact if the assumption is wrong. For traffic growth: size replicas_min
     for today and leave workload.replicas_max null; set_fields then reports the headroom value (replicas_min x
     growth factor). Write the assumption with that number, e.g. "2 replicas today, headroom up to 6".
3. Call check_spec(). Fix only the problems it lists (set_fields or set_lists; entries merge by id, so send just
   what changes) and call check_spec() again until it returns OK.
4. Call write_file for reports/migration-spec.md (markdown, at most 40 lines) with exactly these sections:
   # Migration spec: <service name>
   ## Summary        (3-5 bullets: what is built, where, and the key numbers)
   ## Key decisions  (a table | Decision | Value | Source | with 4-8 rows for the choices that needed judgment,
                     not every field; Source is a requirement or open question id)
   ## Risks          (2-4 bullets)
   Do NOT add traceability, conflict or open question tables: they are appended automatically from the spec.
5. Call submit_result(summary). It submits the validated draft."""


def build_prompt(request: AgentRequest, ctx: RequirementsContext) -> str:
    lines = [
        "Turn the uploaded requirements into the migration spec.",
        f"Uploaded documents: {', '.join(ctx.docs)}",
        "Assessment from the discovery stage (use these finding ids in traces_to and gaps):",
        assessment_digest(ctx.assessment),
    ]
    if request.instruction:
        lines.append(f"Instruction from the user: {request.instruction}")
    if request.feedback:
        lines += [
            f"REVISION. Reviewer feedback to apply: {request.feedback}",
            "The reviewer's feedback overrides the document where they differ. The draft already holds your previous "
            "spec (see get_spec_template). Apply the feedback in this order:",
            "a) Open questions: compare the feedback with every open question in the draft. For each question the "
            "feedback answers or decides, call resolve_question(question_id, decision) with the decision in the "
            "reviewer's words. Reviewers often answer several questions in one sentence.",
            "b) Values: if the feedback changes scope or sizing, update those fields or entries with set_fields / "
            "set_lists. Change nothing the feedback does not mention.",
            f"c) Call check_spec until OK, read {REPORT} and rewrite it with write_file so the Summary and Key "
            "decisions reflect the feedback.",
            "d) Call submit_result with a summary that starts with 'Revised:' and says what changed "
            "(for example which questions were resolved).",
        ]
    return "\n".join(lines)


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    files = dict(request.files)
    if not any(p.startswith("requirements/") for p in files):
        files |= load_sample_requirements()  # local default: samples/requirements/
    if not any(p.startswith("requirements/") for p in files):
        yield error(STAGE, "no requirements/<file> in the request; upload a requirements document")
        return
    ws = Workspace(files)
    if REPORT in ws.files:  # revision: the model only sees its narrative; the generated tables are re-appended
        ws.files[REPORT] = ws.files[REPORT].split(APPENDIX_MARKER)[0].rstrip() + "\n"
    assessment = request.inputs.get("assessment") or {}
    ctx = RequirementsContext(ws, assessment, previous=request.inputs.get("previous") if request.feedback else None)
    box = ResultBox()
    # Writing tools only: the domain tools already read the inputs. Revisions may also read the previous report.
    allowed = {"write_file", "edit_file"} | ({"read_file", "list_files"} if request.feedback else set())
    tools = [*(t for t in workspace_tools(ws) if t.tool_name in allowed), *make_tools(ctx, box)]
    timeout = float(request.options.get("timeout_s") or TIMEOUT_S)
    started, prompt, result, usage = time.monotonic(), build_prompt(request, ctx), None, Counter()
    for attempt in range(2):  # one resume: the draft lives in Python, so a fresh agent continues where this one stopped
        if attempt:
            yield status(STAGE, "the model stopped before submitting; resuming from the saved draft")
        run = run_agent(stage=STAGE, system_prompt=SYSTEM_PROMPT, prompt=prompt, tools=tools, workspace=ws, box=box)
        async for event in with_deadline(run, timeout - (time.monotonic() - started)):
            if event["type"] in ("result", "timeout"):
                result = event
            elif event["type"] != "artifact":  # artifacts are re-emitted once the report has its generated tables
                yield event
                if event["type"] == "error":
                    return
        done = box.data is not None or not ctx.validate()[0]  # submitted, or at least a valid draft
        if done or result is None or result["type"] == "timeout" or time.monotonic() - started > timeout / 2:
            break
        usage.update({k: result["metrics"].get(k, 0) for k in ("input_tokens", "output_tokens", "cycles")})
        prompt = RESUME_PROMPT
    if result is None or result["type"] == "timeout":
        result = timeout_result(STAGE, timeout, box)
    result["metrics"] |= {k: result["metrics"].get(k, 0) + v for k, v in usage.items()} | {
        "latency_s": round(time.monotonic() - started, 1), "resumed": bool(usage)}
    spec = box.data
    if spec is None and not ctx.validate()[0]:
        spec = ctx.draft  # the model filled a valid draft but never called submit_result
    if spec is None:
        yield error(STAGE, f"the model did not complete a valid spec ({ctx.checks} check_spec calls). "
                           f"Last message: {result['summary'][:300]}")
        return
    try:
        finalize_report(ws, spec, result["summary"], assessment)
    except Exception as exc:  # noqa: BLE001 - a malformed last-chance draft must not lose the whole stage
        ws.write(REPORT, narrative_report(ws, spec, result["summary"]) + "\n")
        yield status(STAGE, f"report tables could not be rendered ({type(exc).__name__}: {exc}); kept the narrative")
    result["data"], result["artifacts"] = spec, ws.artifacts()
    for path, content in result["artifacts"].items():
        yield {"type": "artifact", "path": path, "bytes": len(content)}
    yield result


async def with_deadline(events: AsyncIterator[dict], seconds: float) -> AsyncIterator[dict]:
    """Pass events through; past the deadline, stop the run and yield {"type": "timeout"} instead.

    Guards the live demo against a stalled model stream or a tool loop that never converges.
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            yield await asyncio.wait_for(anext(events), max(0.1, deadline - time.monotonic()))
        except StopAsyncIteration:
            return
        except TimeoutError:
            await events.aclose()
            yield {"type": "timeout"}
            return


def timeout_result(stage: str, seconds: float, box: ResultBox) -> dict:
    """The result event run_agent never produced because the deadline cut the run short."""
    summary = box.summary or f"(timed out after {seconds:.0f} s)"
    return {"type": "result", "stage": stage, "summary": summary, "data": {}, "artifacts": {},
            "metrics": {"model": model_id_for(stage), "latency_s": seconds, "input_tokens": 0, "output_tokens": 0,
                        "cycles": 0, "submitted": box.data is not None, "timed_out": True}}
