"""Agent 1: Discovery & Assessment (the AWS Transform discovery/assessment analogue).

Python tools inventory the legacy estate (code, ops files, Terraform, optional live EC2/CloudWatch).
The model assigns severities, picks the 6R strategy and a readiness score, and writes the report narrative.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from transform_agents.core import (
    AgentRequest,
    ResultBox,
    Workspace,
    error,
    model_id_for,
    region,
    run_agent,
    workspace_tools,
)

from .tools import (
    APPENDIX_MARKER,
    DEFAULT_TARGET,
    REPORT,
    DiscoveryContext,
    fallback_assessment,
    finalize_report,
    load_legacy_estate,
    make_tools,
)

STAGE = "discovery"
TIMEOUT_S = 60  # typical run: ~10 s; override with options.timeout_s

SYSTEM_PROMPT = """You are the Discovery & Assessment agent of a migration factory (like the AWS Transform assessment).
You assess a legacy application before it is migrated. Python tools collect every fact; your job is to judge them.

Work in this order:
1. Call scan_codebase, parse_legacy_terraform and get_utilization (all three in one turn). If describe_live_instance
   is available, call it in the same turn.
2. Give EVERY finding id (LF-xx) the tools returned a severity:
   - critical: exploitable from the network today, or leaked credentials (SQL injection, hard-coded secrets).
   - high: can cause an outage or data loss, or a serious security weakness (single instance or AZ, local database,
     no backups, running as root, unauthenticated writes, end-of-life runtime).
   - medium: must change for containers/Kubernetes but is no immediate risk (local files, file logs, dev server,
     host cron, missing probes, hard-coded config, plain HTTP).
   - low: hygiene and cost (encryption defaults, over-provisioning).
3. Choose the 6R strategy:
   - rehost: move the VM as it is. It does not reach a container target.
   - replatform: keep the codebase, framework and API; make targeted code changes (fix queries, read config and
     secrets from the environment, log to stdout, add probes, use a production server), containerize it and move
     the data to a managed database.
   - refactor: rewrite or re-architect most of the code (new framework or language, split into several services,
     new data model).
   Targeted fixes inside existing functions are replatform, even when they fix critical findings. Pick the least
   invasive option that reaches the target and fixes the critical and high findings; use the LOC and the evidence.
4. Choose readiness_score (0-100): how ready the app is to run on the target today.
   80-100 containerize as is; 50-79 targeted fixes needed; below 50 significant rework.
5. Call write_file for reports/assessment.md (markdown, at most 50 lines) with exactly these sections:
   # Assessment: <application name>
   ## Executive summary   (3-5 bullets)
   ## Migration strategy  (a table | Option | Fit | Why | with rows rehost, replatform, refactor and Fit = good,
                          partial or poor; then the decision)
   ## Readiness           (the score and the blockers that keep it from being higher, citing finding ids)
   ## Next steps          (3-5 numbered steps)
   Do NOT add inventory or findings tables: they are appended automatically from the tool results.
6. Call submit_result with summary, severities (every finding id), strategy, strategy_rationale, readiness_score.
Never invent facts: cite finding ids and the tools' evidence."""


def build_prompt(request: AgentRequest, ctx: DiscoveryContext) -> str:
    n_app = len(ctx.ws.listing("legacy/app/"))
    n_tf = len([p for p in ctx.ws.listing("legacy/terraform/") if p.endswith(".tf")])
    live = ("enabled (call describe_live_instance)" if ctx.live_aws
            else "disabled, so utilization is an estimate; say so in the report")
    lines = [
        (f"Assess the legacy estate in the workspace: {n_app} files under legacy/app/, {n_tf} Terraform files "
         "under legacy/terraform/."),
        f"Migration target: {request.inputs.get('target') or DEFAULT_TARGET}.",
        f"Live AWS lookup: {live}.",
    ]
    if request.instruction:
        lines.append(f"Instruction from the user: {request.instruction}")
    if request.feedback:
        previous = request.inputs.get("previous") or {}
        brief = {"severities": {f["id"]: f["severity"] for f in previous.get("findings", [])},
                 "strategy": previous.get("strategy"), "readiness_score": previous.get("readiness_score")}
        lines += [
            f"REVISION. Reviewer feedback to apply: {request.feedback}",
            f"Your previous result: {json.dumps(brief)}",
            (f"Read {REPORT} (your previous narrative), rewrite it with write_file so it reflects the feedback, then "
             "call submit_result again with the changes the reviewer asked for. Keep everything else as it was."),
        ]
    return "\n".join(lines)


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    files = dict(request.files)
    if not any(p.startswith("legacy/app/") for p in files):
        files = {**load_legacy_estate(), **files}  # local default: the repo's legacy-infra/
    if not any(p.startswith("legacy/app/") for p in files):
        yield error(STAGE, "no legacy/app/** files in the request and no local legacy-infra/ to fall back to")
        return
    ws = Workspace(files)
    if REPORT in ws.files:  # revision: the model only sees its narrative; the generated tables are re-appended
        ws.files[REPORT] = ws.files[REPORT].split(APPENDIX_MARKER)[0].rstrip() + "\n"
    ctx = DiscoveryContext(ws, live_aws=bool(request.options.get("live_aws")),
                           region=request.options.get("region") or region())
    box = ResultBox()
    # Writing tools only: the domain tools already read the inputs. Revisions may also read the previous report.
    allowed = {"write_file", "edit_file"} | ({"read_file", "list_files"} if request.feedback else set())
    tools = [*(t for t in workspace_tools(ws) if t.tool_name in allowed), *make_tools(ctx, box)]
    timeout = float(request.options.get("timeout_s") or TIMEOUT_S)
    run = run_agent(stage=STAGE, system_prompt=SYSTEM_PROMPT, prompt=build_prompt(request, ctx), tools=tools,
                    workspace=ws, box=box)
    result = None
    async for event in with_deadline(run, timeout):
        if event["type"] in ("result", "timeout"):
            result = event
        elif event["type"] != "artifact":  # artifacts are re-emitted once the report has its generated tables
            yield event
            if event["type"] == "error":
                return
    if result is None or result["type"] == "timeout":
        result = timeout_result(STAGE, timeout, box)
    data = box.data
    if data is None:  # degraded mode: the model never submitted; keep the pipeline moving
        data = fallback_assessment(ctx)
        result["summary"] = f"(fallback: model did not submit) {result['summary']}"[:2000]
    finalize_report(ws, data, result["summary"])
    result["data"], result["artifacts"] = data, ws.artifacts()
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
