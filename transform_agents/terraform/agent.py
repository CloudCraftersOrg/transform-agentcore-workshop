"""Agent 4 - Terraform: turns the migration spec into validated infrastructure as code.

AWS Transform analogue: network/infrastructure conversion to Terraform. The agent does not write EKS Terraform from
memory (models trained before terraform-aws-modules/eks v21 use removed arguments); it adapts a curated, validated
blueprint and loops on a layered validator until the code is valid and matches the spec.
"""

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from transform_agents.core import (
    AgentRequest,
    ResultBox,
    Workspace,
    error,
    model_id_for,
    preview,
    run_agent,
    status,
    workspace_tools,
)

from .tools import CONTEXT_TOOLS, TerraformRun, terraform_tools

STAGE = "terraform"
MAX_RUNS = 3  # follow-up runs when the model stops early (no tool call, no submit_result, model error)
CONTINUE_WITHIN_S = 50  # only start a follow-up run this early in the stage
DEADLINE_S = 100  # hard cap for the whole stage
SILENCE_S = 45  # no event for this long (e.g. a runaway tool-call generation) stops the run
MAX_PROSE_CHARS = 2500  # model text between tool calls before the run counts as rambling

SYSTEM_PROMPT = """You are the Terraform agent of an AWS Transform-style migration pipeline. You turn the migration
spec into validated Terraform for the target platform on AWS: VPC, EKS, ECR and RDS for PostgreSQL.
Never write EKS Terraform from memory: terraform-aws-modules/eks v21 renamed many arguments. Adapt the blueprint.

Steps (keep them short; the whole run should take a handful of tool calls):
1. Call get_blueprint. It copies the validated blueprint into terraform/ and shows terraform/variables.tf.
2. Derive every value with the mapping rules below and call set_variables ONCE with all of them.
3. Only if a requirement cannot be expressed with the variables, change terraform/main.tf with edit_file
   (small, unique snippets). Before adding a module argument, check it with get_module_inputs.
4. Call terraform_validate. If it reports errors or spec mismatches, fix them (set_variables or edit_file) and
   validate again. At most 4 validation rounds.
5. Call submit_result with a 2-4 sentence summary and spec_coverage: one entry per spec requirement the
   infrastructure addresses, using the requirement ids from the spec (e.g. SEC3, NFR4, TC2); keep each "how"
   under 15 words.

Mapping rules (spec -> variables.tf):
- name = spec.service.name; environment = spec.finops.environment (default "prod"); region = spec.target.region
- tags = {app = <name>, environment = <environment>, managed-by = "terraform", migrated-by = "transform-agentcore"}
- kubernetes_version = spec.target.kubernetes_version (a string such as "1.33")
- node_architecture = "arm64" if spec.finops.prefer_graviton or spec.target.cpu_architecture == "arm64", else "x86_64"
- node_instance_types: arm64 -> ["t4g.medium"]; x86_64 -> ["t3.medium"]. Exception: when
  modernization.resources.limits.cpu is 1 vCPU or more ("1", "1000m"), use ["m7g.large"] (arm64). A 500m limit is
  NOT the exception. Sizing for growth forecasts is the FinOps agent's job, not yours.
- node_capacity_type = "ON_DEMAND" for prod, "SPOT" otherwise
- node_min = max(2, ceil(workload.replicas_min / 2)); node_max = max(2 * node_min, ceil(workload.replicas_max / 2))
  (several pods share a node, so node_max is not replicas_max; e.g. replicas 2-6 -> node_min 2, node_max 4)
- az_count = 3 if nfr.availability_slo_pct >= 99.99 else 2
- single_nat_gateway = false (one NAT per AZ) if nfr.availability_slo_pct >= 99.95, else true
  ("survive the loss of an AZ" goals are already met by nodes in 2 AZs plus the Multi-AZ database; do not add AZs
  or NAT gateways for them)
- ecr_repository_name = modernization.image.repository (fallback: name)
- db_engine_version = spec.data.version (string); db_multi_az = spec.data.multi_az
- db_instance_class: arm64 -> "db.t4g.small" for prod, "db.t4g.micro" otherwise; x86_64 -> "db.t3.small" / "db.t3.micro"
- db_backup_retention_days = 7 if prod or nfr.rpo_minutes <= 60 (point-in-time recovery gives ~5 min RPO), else 1
- db_deletion_protection = true for prod, false otherwise
If a spec value is missing, keep the blueprint default and say so in the summary.

Guardrails: keep module versions (vpc ~> 6.0, eks ~> 21.0); never remove storage encryption, the private database,
IMMUTABLE ECR tags or scan on push; do not invent requirement ids. When reviewer feedback is given, apply it to the
existing terraform/ files; feedback overrides the mapping rules."""

SPEC_KEYS = ["service", "target", "workload", "data", "nfr", "security", "finops"]


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def build_prompt(request: AgentRequest) -> str:
    """A compact digest of the inputs: only the fields that drive infrastructure."""
    inputs = request.inputs or {}
    spec = inputs.get("spec") or {}
    modernization = inputs.get("modernization") or {}
    assessment = inputs.get("assessment") or {}
    parts = [request.instruction or "Generate validated Terraform for the target platform from the migration spec."]
    if spec:
        parts.append("Migration spec (infrastructure fields):\n" + "\n".join(
            f"- {k}: {_compact(spec[k])}" for k in SPEC_KEYS if spec.get(k)))
        reqs = [r for r in spec.get("requirements") or [] if isinstance(r, dict)]
        if reqs:
            parts.append("Spec requirements (ids for spec_coverage):\n" + "\n".join(
                f"- {r.get('id')}: {str(r.get('text', ''))[:140]}" for r in reqs[:40]))
    else:
        parts.append("No migration spec was provided: keep the blueprint defaults and say so in the summary.")
    if modernization:
        keep = {k: modernization.get(k) for k in ("service_name", "image", "resources") if modernization.get(k)}
        parts.append(f"Modernization result: {_compact(keep)}")
    if assessment:
        keep = {k: assessment.get(k) for k in ("infrastructure", "strategy") if assessment.get(k)}
        parts.append(f"Assessment (current estate): {_compact(keep)}")
    if request.feedback:
        parts.append(f"Reviewer feedback to apply to the existing terraform/ files: {request.feedback}\n"
                     "Change only what the feedback asks for (set_variables with just those values); keep the rest.")
    return "\n\n".join(parts)


def leaked_call(text: str, names: list[str]) -> tuple[str, dict] | None:
    """Mistral on Bedrock sometimes writes a tool call as text (`<junk>get_blueprint{}`). Parse the last one."""
    at, name = max(((text.rfind(n), n) for n in names), default=(-1, ""))
    rest = text[at + len(name):].lstrip() if at >= 0 else ""
    if not rest.startswith("{"):
        return None
    try:
        args, _ = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError:
        return None
    return (name, args) if isinstance(args, dict) else None


def run_tool(tool: Any, args: dict) -> str:
    """Execute a tool call ourselves (used for calls the model wrote as text)."""
    if tool.tool_name in CONTEXT_TOOLS:
        args = {**args, "tool_context": SimpleNamespace(invocation_state={})}
    try:
        return str(tool(**args))
    except Exception as exc:  # noqa: BLE001 - bad arguments from the model
        return f"error: {type(exc).__name__}: {exc}"


def _why_continue(final: dict, run: TerraformRun, box: ResultBox) -> str | None:
    """Why the run is not finished yet (None = done)."""
    if final["type"] == "error":
        return f"the model call failed ({final['message'][:120]})"
    if not run.tf_files():
        return "it answered without calling any tool"
    if box.data is None:
        return "it stopped before calling submit_result"
    return None


def _continuation(reason: str, run: TerraformRun, executed: str) -> str:
    """Extra prompt for a follow-up run: the workspace keeps the earlier work, the new model call does not."""
    note = f"\nYour last tool call was written as text; it was executed for you: {executed}" if executed else ""
    if not run.tf_files():
        return f"Your previous attempt failed: {reason}.{note}\nStart now by calling get_blueprint. Use tools."
    todo = "\n".join(f"- {p}" for p in run.open_problems()[:10]) or "- none: Terraform is valid and matches the spec"
    return (f"Your previous attempt stopped early: {reason}.{note}\nThe terraform/ files already contain that work; "
            f"do not start over. Open problems:\n{todo}\nUse tools, not prose: fix any problem (set_variables / "
            "edit_file), call terraform_validate, then call submit_result with summary and spec_coverage.")


async def _guarded(events: AsyncIterator[dict], deadline: float) -> AsyncIterator[dict]:
    """Pass run_agent events through; stop a run that rambles or passes the deadline (it then ends in an error)."""
    prose = 0
    try:
        while True:
            try:
                timeout = max(min(deadline - time.time(), SILENCE_S), 0.1)
                event = await asyncio.wait_for(anext(events), timeout=timeout)
            except StopAsyncIteration:
                return
            except TimeoutError:
                why = "the stage deadline passed" if time.time() >= deadline - 0.2 else f"no progress for {SILENCE_S}s"
                yield error(STAGE, f"stopped: {why}")
                return
            if event["type"] == "text":
                prose += len(event["delta"])
                if prose > MAX_PROSE_CHARS:
                    yield error(STAGE, "stopped: the model wrote prose instead of calling tools")
                    return
            elif event["type"] == "tool_start":
                prose = 0
            yield event
    finally:
        await events.aclose()


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    ws = Workspace(request.files)
    box = ResultBox()
    inputs = request.inputs or {}
    run = TerraformRun(ws, spec=inputs.get("spec") or {}, modernization=inputs.get("modernization") or {},
                       feedback=request.feedback, max_validations=int(request.options.get("max_validations", 4)))
    tools = [*workspace_tools(ws), *terraform_tools(run, box)]
    by_name = {t.tool_name: t for t in tools}
    prompt, started, totals = build_prompt(request), time.time(), Counter()
    for attempt in range(1, MAX_RUNS + 1):
        final: dict = error(STAGE, "the agent produced no events")
        text = ""  # model text since its last tool call
        events = run_agent(stage=STAGE, system_prompt=SYSTEM_PROMPT, prompt=prompt, tools=tools, workspace=ws, box=box)
        async for event in _guarded(events, started + DEADLINE_S):
            if event["type"] in ("result", "error"):
                final = event
                continue
            if event["type"] == "text":
                text += event["delta"]
            elif event["type"] == "tool_start":
                text = ""
            if event["type"] != "artifact":  # artifacts are re-emitted below, after formatting
                yield event
        executed = ""
        if final["type"] == "result":
            totals.update({k: final["metrics"].get(k, 0) for k in ("input_tokens", "output_tokens", "cycles")})
            call = leaked_call(text, list(by_name)) if box.data is None else None
            if call:  # replay it, visibly, so the work is not lost
                name, args = call
                call_id = f"text-call-{attempt}"
                yield {"type": "tool_start", "id": call_id, "name": name, "input": preview(json.dumps(args))}
                output = run_tool(by_name[name], args)
                yield {"type": "tool_end", "id": call_id, "name": name, "status": "success", "output": preview(output)}
                executed = f"{name} -> {output[:800]}"
        reason = _why_continue(final, run, box)
        if reason is None or attempt == MAX_RUNS or time.time() - started > CONTINUE_WITHIN_S:
            break
        yield status(STAGE, f"continuing: {reason}")
        prompt = build_prompt(request) + "\n\n" + _continuation(reason, run, executed)

    if final["type"] == "error":
        if not run.tf_files() or run.open_problems():
            yield final
            return
        # The model failed late, but the Terraform is valid and matches the spec: report it.
        final = {"type": "result", "stage": STAGE, "summary": f"The model did not finish ({final['message']}).",
                 "data": {}, "artifacts": {}, "metrics": {"model": model_id_for(STAGE)}}
    try:
        data = run.finalize(box.data or final.get("data"))
    except Exception as exc:  # noqa: BLE001 - never lose the run over a summary bug
        yield error(STAGE, f"finalize failed: {type(exc).__name__}: {exc}")
        return
    if data is None:
        yield error(STAGE, "the agent produced no Terraform files under terraform/")
        return
    for path, content in ws.artifacts().items():
        yield {"type": "artifact", "path": path, "bytes": len(content)}
    if box.summary:
        final["summary"] = box.summary
    else:  # the model never called submit_result: state the facts first
        verdict = f"Terraform {'is valid' if data['valid'] else 'is NOT valid'} ({data['validation_level']})."
        final["summary"] = verdict + ("" if final["summary"] == "(no summary)" else f" {final['summary']}")
    final["data"] = data
    final["artifacts"] = ws.artifacts()
    final["metrics"].update(totals, latency_s=round(time.time() - started, 1), runs=attempt,
                            submitted=box.data is not None, validate_attempts=run.attempts,
                            validation_level=data["validation_level"], valid=data["valid"])
    yield final
