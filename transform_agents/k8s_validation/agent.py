"""Agent 5, k8s_validation: the AWS Transform "test and validation" step.

Flow: deterministic preflight (kustomize render, kubeconform, policy baseline) -> the LLM patches failing manifests
with small edit_file changes and re-runs the checks -> optional live deploy to kind with smoke tests -> a
deterministic finalization that rebuilds `data` and reports/validation-report.md from the real check results.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

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

from .drift import inject_drift
from .report import REPORT_PATH, build_data, render_report
from .tools import (
    MAX_DEPLOYS,
    NEW_FILE_RULES,
    WRITABLE_PREFIX,
    Validation,
    ValidationSession,
    format_deploy,
    format_policy,
    format_schema,
    validation_tools,
    write_tools,
)

STAGE = "k8s_validation"
TIME_BUDGET_S = 150  # hard stop for the LLM loop; a normal run takes 30-90 s including the live deploy
HARD_TOOL_CALLS = 45  # runaway-loop guard (the tools ask the model to submit after 30)
PROMPT_FILE_CHARS = 16000

SYSTEM_PROMPT = """You are the Kubernetes validation agent of a migration factory (the AWS Transform "test and \
validation" step). The input is a Kustomize base (k8s/base) and overlays (k8s/overlays/local) for a containerized \
microservice (microservice/). Deterministic tools do all the checking; your job is to fix what they report with \
small, precise patches, re-check, and report.

Workflow:
1. Read the preflight results in the user message (kubeconform_check and policy_check already ran).
2. For every FAIL, patch the file named after "edit:" with edit_file: copy the exact `old` snippet from the file \
content in the message, keep YAML indentation, change only what is needed. To add a manifest, write_file it and \
then add it to the `resources:` list of that kustomization.yaml. You may call several edit tools in one turn, but \
never two edits of the same file in one turn.
3. Call kubeconform_check and policy_check again. Repeat steps 2-3 until both PASS (at most 3 rounds).
4. If live deploy is enabled, call deploy_to_kind once the checks pass. If it fails, read the diagnostics, fix the \
manifests and deploy again (at most twice).
5. Call submit_result with a 2-4 sentence summary, one `fixes` entry per change ("<file>: <what changed>"), and \
notes for anything left open. Then stop.

Rules:
- Edit only files under k8s/. Never change microservice/ code or delete resources, probes or security settings.
- Fix the source the finding points to: base files for problems in base, overlay files only when the overlay \
causes the problem.
- Prefer the fix snippets given in the findings. Do not write reports/validation-report.md: submit_result builds \
it from the check results.
- Keep text short; let the tools do the work."""


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    """Stream contract events; always ends with exactly one `result` (or `error` on an unexpected bug)."""
    try:
        async for event in _handle(request):
            yield event
    except Exception as exc:  # noqa: BLE001 - the stream must end cleanly for the orchestrator/UI
        yield error(STAGE, f"{type(exc).__name__}: {exc}")


async def _handle(request: AgentRequest) -> AsyncIterator[dict]:
    started = time.monotonic()
    ws = Workspace(request.files)
    pristine = {p: c for p, c in ws.files.items() if p.startswith(WRITABLE_PREFIX)}
    drift: list[dict] = []
    if request.options.get("simulate_drift") is True or str(request.options.get("simulate_drift")).lower() == "true":
        if request.feedback:  # a revision edits the previous (already fixed) files: no new drift on top
            yield status(STAGE, "simulated drift skipped on a revision run (the feedback is applied to the fixed files)")
        else:
            ws.files, drift = inject_drift(ws.files)
            yield status(STAGE, f"simulated drift: injected {len(drift)} violations", drift=drift)
    session = ValidationSession(ws, request.inputs, request.options)
    session.feedback = request.feedback
    session.drift, session.pristine = drift, pristine
    if not session.has_manifests():
        yield status(STAGE, "no k8s/** manifests in request.files: nothing to validate")
        async for event in _finish(request, session, None, "", started, nothing=True):
            yield event
        return

    # 1. Deterministic preflight, shown as tool calls in the UI timeline.
    yield status(STAGE, "preflight: rendering Kustomize targets and running kubeconform + policy checks")
    first = await asyncio.to_thread(session.validate)
    for name, text in (("kubeconform_check", format_schema(first)), ("policy_check", format_policy(session, first))):
        yield {"type": "tool_start", "id": f"preflight-{name}", "name": name, "input": "{}"}
        yield {"type": "tool_end", "id": f"preflight-{name}", "name": name, "status": "success", "output": text}
    if session.live_deploy:
        session.start_image_build()
        yield status(STAGE, "building the image and loading it into kind in the background")

    # 2. LLM fix loop, bounded by time and tool calls.
    box = ResultBox()
    tools = [*workspace_tools(ws, writable=False), *write_tools(ws), *validation_tools(session, box)]
    agent_events = run_agent(stage=STAGE, system_prompt=SYSTEM_PROMPT, prompt=build_prompt(request, session, first),
                             tools=tools, workspace=ws, box=box)
    deadline = started + float(request.options.get("time_budget_s") or TIME_BUDGET_S)
    llm_result: dict | None = None
    llm_error = ""
    try:
        while True:
            event = await asyncio.wait_for(anext(agent_events), timeout=max(deadline - time.monotonic(), 0.1))
            if event["type"] == "result":
                llm_result = event
            elif event["type"] == "error":
                llm_error = event["message"]
            elif event["type"] != "artifact":  # artifacts are announced once, after finalization
                if event["type"] == "tool_start":
                    session.tool_calls += 1
                yield event
                if session.tool_calls >= HARD_TOOL_CALLS:
                    llm_error = f"stopped after {HARD_TOOL_CALLS} tool calls"
                    break
    except StopAsyncIteration:
        pass
    except TimeoutError:
        llm_error = f"time budget of {deadline - started:.0f}s exceeded; finishing deterministically"
    finally:
        try:
            await agent_events.aclose()
        except (RuntimeError, asyncio.CancelledError):
            pass
    if llm_error:
        yield status(STAGE, f"agent loop ended early: {llm_error}")

    # 3. Deterministic finalization: re-check, deploy if still needed, build data + report.
    async for event in _finish(request, session, llm_result, llm_error, started, box=box):
        yield event


def build_prompt(request: AgentRequest, session: ValidationSession, v: Validation) -> str:
    deploy = ("ENABLED: after the checks pass, call deploy_to_kind" if session.live_deploy
              else "disabled: do not deploy")
    spec = request.inputs.get("spec") or {}
    lines = [f"Validate and fix the Kubernetes manifests for {', '.join(sorted(session.app_names)) or 'the service'}.",
             f"Targets: {', '.join(v.targets)}. Live deploy to kind cluster '{session.cluster}': {deploy}.",
             (f"Policy: replicas_min={session.replicas_min}, non_root="
             f"{(spec.get('security') or {}).get('non_root', True)}, read_only_root_fs="
             f"{(spec.get('security') or {}).get('read_only_root_fs', True)}.")]
    if request.instruction:
        lines.append(f"Instruction: {request.instruction}")
    if session.drift:
        lines.append(f"Demo mode: {len(session.drift)} typical hand-edits (SIMULATED drift) were injected into "
                     "k8s/base before the checks. Fix them like any other finding.")
    if request.feedback:
        lines.append(f"Reviewer feedback to apply by editing the existing files: {request.feedback}")
    lines += ["", "Preflight results:", format_schema(v), "", format_policy(session, v), "",
              "Files under k8s/: " + ", ".join(session.ws.listing("k8s/"))]

    index = session.source_index()
    to_edit: list[str] = []
    for f, targets in v.grouped():
        if f.status == "fail":
            to_edit += session.edit_files(f, targets, index)
    to_edit += [f"k8s/{t}/kustomization.yaml" for t, tc in v.targets.items() if tc.rendered.status == "fail"]
    if request.feedback:  # the feedback may touch any manifest: show them all (they are small)
        to_edit += [p for p in session.ws.listing("k8s/") if p.endswith((".yaml", ".yml"))]
    budget = PROMPT_FILE_CHARS
    for path in dict.fromkeys(p for p in to_edit if p in session.ws.files):
        content = session.ws.files[path]
        if len(content) > budget:
            break
        budget -= len(content)
        lines += ["", f"Current content of {path}:", "```yaml", content.rstrip(), "```"]

    deploy_step = "call deploy_to_kind, " if session.live_deploy else ""
    if request.feedback:
        lines += ["", "First apply the reviewer feedback with edit_file (it takes priority)" +
                  (" and fix every FAIL" if not v.ok else "") +
                  f", then re-run kubeconform_check and policy_check, {deploy_step}then call submit_result."]
    elif v.ok:
        lines += ["", f"All checks already pass. {deploy_step.capitalize()}then call submit_result."]
    else:
        new_files = [r for r in NEW_FILE_RULES if r in v.failing_rules()]
        lines += ["", "Fix every FAIL now (edit_file / write_file" + (" + kustomization resources" if new_files else "")
                  + f"), re-run kubeconform_check and policy_check, {deploy_step}then call submit_result."]
    return "\n".join(lines)


async def _finish(request: AgentRequest, session: ValidationSession, llm_result: dict | None, llm_error: str,
                  started: float, box: ResultBox | None = None, nothing: bool = False) -> AsyncIterator[dict]:
    """Always ends the stream with exactly one `result` built from deterministic state."""
    model = model_id_for(STAGE)
    if nothing:
        data = {"passed": False, "score": 0, "fixes_applied": [], "targets": [], "policy_before": [],
                "policy_after": [], "notes": "",
                "checks": [{"id": "inputs", "tool": "builtin", "status": "skipped", "resource": "k8s/**",
                            "details": "no k8s/** manifests in request.files"}],
                "live_deploy": {"status": "skipped", "cluster": f"kind-{session.cluster}", "image": "", "rollout": "",
                                "smoke_tests": [], "reason": "nothing to deploy"}}
        yield _result("Nothing to validate: the request has no k8s/** manifests.", data, {},
                      {"model": model, "latency_s": round(time.monotonic() - started, 1), "input_tokens": 0,
                       "output_tokens": 0, "cycles": 0, "submitted": False})
        return

    final = await asyncio.to_thread(session.validate)
    first = session.first or final
    if _regressed(first, final):
        reverted = _revert_k8s(session)
        final = await asyncio.to_thread(session.validate)
        yield status(STAGE, f"agent edits made the manifests worse; reverted {len(reverted)} file(s)")
        session.notes = (session.notes + "\n" if session.notes else "") + \
            f"Agent edits that broke rendering/schema or added failures were reverted: {', '.join(reverted)}."

    deploy_s = 0.0
    needs_deploy = session.deploy_result is None or session.deploy_stale
    if session.live_deploy and needs_deploy and session.deploys < MAX_DEPLOYS:
        yield {"type": "tool_start", "id": "finalize-deploy_to_kind", "name": "deploy_to_kind", "input": "{}"}
        t0 = time.monotonic()
        live = await asyncio.to_thread(session.deploy)
        deploy_s = round(time.monotonic() - t0, 1)
        yield {"type": "tool_end", "id": "finalize-deploy_to_kind", "name": "deploy_to_kind", "status": "success",
               "output": format_deploy(live)}

    data = build_data(session, final)
    submitted = bool(box and box.data is not None)
    summary = (box.summary if submitted and box and box.summary else _fallback_summary(data, llm_error))
    restored = _undrift(session, final)
    if restored:
        data["drift_restored"] = restored
        data["notes"] = (data["notes"] + "\n" if data["notes"] else "") + (
            f"SIMULATED drift was not fully fixed; the artifacts carry the original manifests for "
            f"{', '.join(restored)}, so later stages never inherit simulated drift.")
    session.ws.write(REPORT_PATH, render_report(session, data, summary, model, request.run_id))
    artifacts = select_artifacts(session, revision=bool(request.feedback), restored=restored)
    for path, content in artifacts.items():
        yield {"type": "artifact", "path": path, "bytes": len(content)}

    metrics: dict[str, Any] = dict((llm_result or {}).get("metrics") or {
        "model": model, "input_tokens": 0, "output_tokens": 0, "cycles": 0})
    timings = (session.deploy_result or {}).get("timings") or {}
    metrics.update(latency_s=round(time.monotonic() - started, 1), submitted=submitted,
                   llm_latency_s=(llm_result or {}).get("metrics", {}).get("latency_s"),
                   tool_calls=session.tool_calls, check_runs=session.check_runs, deploys=session.deploys,
                   deploy_s=timings.get("total_s", deploy_s), failing_before=len(first.failing_keys()),
                   failing_after=len(final.failing_keys()))
    if llm_error:
        metrics["llm_error"] = llm_error[:300]
    yield _result(summary, data, artifacts, metrics)


def select_artifacts(session: ValidationSession, revision: bool = False, restored: list[str] | None = None) -> dict:
    """Only k8s/** and the report ever leave this stage.

    First run: the report plus the k8s/** files that changed. Revision run: the report plus ALL k8s/** files, so
    fixes from earlier attempts are never lost when the orchestrator replaces the stage's artifacts.
    """
    files = session.ws.files
    out = {}
    for path in sorted(files):
        if not path.startswith(WRITABLE_PREFIX):
            continue
        content = session.pristine.get(path, files[path]) if path in (restored or []) else files[path]
        if revision or content != session.original.get(path) or path in (restored or []):
            out[path] = content
    out[REPORT_PATH] = files[REPORT_PATH]
    return out


def _undrift(session: ValidationSession, final: Validation) -> list[str]:
    """Files whose SIMULATED drift is still (partly) unfixed: their artifacts fall back to the pristine version."""
    if not session.drift or final.ok:
        return []
    left = {d["rule"] for d in session.drift} & final.failing_rules()
    files = {d["file"] for d in session.drift if d["rule"] in left} or ({d["file"] for d in session.drift}
                                                                         if not final.render_ok else set())
    return sorted(f for f in files if f in session.pristine)


def _result(summary: str, data: dict, artifacts: dict, metrics: dict) -> dict:
    return {"type": "result", "stage": STAGE, "summary": summary, "data": data, "artifacts": artifacts,
            "metrics": metrics}


def _regressed(first: Validation, final: Validation) -> bool:
    return ((first.render_ok and not final.render_ok) or (first.schema_ok and not final.schema_ok)
            or bool(final.failing_keys() - first.failing_keys()) and len(final.failing_keys()) >= len(
                first.failing_keys()))


def _revert_k8s(session: ValidationSession) -> list[str]:
    reverted = []
    for path in list(session.ws.files):
        if path.startswith("k8s/") and session.ws.files[path] != session.original.get(path):
            reverted.append(path)
            if path in session.original:
                session.ws.files[path] = session.original[path]
            else:
                del session.ws.files[path]
    return reverted


def _fallback_summary(data: dict, llm_error: str) -> str:
    before, after = data["policy_before"], data["policy_after"]
    text = (f"Validated {', '.join(data['targets'])}: {len(before)} failing policy rule(s) before fixes, "
            f"{len(after)} after ({', '.join(after) or 'none'}); {len(data['fixes_applied'])} file(s) changed. "
            f"Live deploy: {data['live_deploy']['status']}. Overall: {'PASSED' if data['passed'] else 'FAILED'} "
            f"(score {data['score']}).")
    return text + (f" Note: {llm_error}" if llm_error else " (The model did not call submit_result.)")
