"""Agent 6, FinOps: right-sizing and cost, the AWS Transform assessment TCO / business case analogue.

Deterministic tools do every calculation (prices, parsing, right-sizing, bin-packing, scenarios).
The LLM chooses which optimizations to apply, explains the trade-offs against the SLO, and submits.
If the LLM fails, times out or never submits, the same tools finish the job deterministically.
"""

import asyncio
import time
from collections.abc import AsyncIterator

from transform_agents.core import AgentRequest, ResultBox, model_id_for, run_agent, status
from transform_agents.core.contract import error

from . import cost_model as cm
from .tools import REPORT_PATH, FinOpsSession, build_tools

STAGE = "finops"
DEFAULT_INSTRUCTION = ("Compare the monthly cost of the legacy EC2 estate with the EKS + RDS target as designed and "
                       "optimized, right-size the pods and nodes, and check the budget.")
TIME_BUDGET_S = 55  # keep a live demo under a minute; the deterministic fallback finishes the job

SYSTEM_PROMPT = """You are the FinOps agent of a migration pipeline (the AWS Transform assessment and business case
step).
You compare the legacy EC2 monolith with the new EKS + RDS design and recommend optimizations.

Rules:
- Never calculate, estimate or round numbers yourself. Every number comes from a tool; copy numbers exactly.
- Respect the SLO and the spec in production: at least 2 nodes across AZs, Multi-AZ RDS, on-demand or
  Savings Plan capacity. Spot and off-hours scale-down are only for dev/test.
- Be honest: one EC2 instance is cheaper than a highly available platform. Explain what the extra cost buys
  (no single point of failure, 99.9% SLO, managed PostgreSQL with backups, security fixes, autoscaling) and how
  the optimizations reduce it. The increase over today is never a "saving"; extended support is a cost.

Steps (call independent tools in the same turn):
1. get_prices, parse_k8s_resources and parse_terraform_sizing.
2. rightsize_pods and node_fit.
3. Pick the optimizations that change something for this design (check the EKS support status, node types,
   volume types, NAT count, logs, commitments) and call compare_scenarios with their ids. Apply every prod-safe
   optimization that saves money unless reviewer feedback or the SLO rules it out (say why in risks). If
   compare_scenarios lists optimizations that would still save and nothing rules them out, call it once more with
   them added. At most twice.
4. Call submit_result once with: summary (2-4 sentences: verdict, the three monthly totals, budget),
   value_case (markdown, 1-2 short paragraphs: how much more the optimized target costs than today and the biggest
   fixed costs, e.g. EKS control plane, NAT gateway, load balancer, Multi-AZ database; then what that buys: no single
   point of failure for the 99.9% SLO, RPO/RTO through managed backups, the security fixes, autoscaling for growth;
   call out an extended-support Kubernetes version if the tools report one),
   risks (markdown bullets) and recommendations_json, a JSON list of
   {"id": "<optimization id>", "rationale": "<why, trade-off, SLO impact>"} for every applied optimization, plus at
   most 2 {"id": "custom", "title": "...", "rationale": "...", "effort": "low|medium|high", "risk": "low|medium|high",
   "applies_to": "k8s|terraform|process"} for follow-ups the tools cannot price.
Then reply with one short sentence."""


def build_prompt(request: AgentRequest, s: FinOpsSession) -> str:
    files = request.files
    k8s = sum(p.startswith("k8s/") for p in files)
    tf = sum(p.startswith("terraform/") for p in files)
    spec = s.spec
    lines = [
        request.instruction or DEFAULT_INSTRUCTION,
        "",
        (f"Context: service {s.service}; environment {s.environment}; availability SLO "
        f"{(spec.get('nfr') or {}).get('availability_slo_pct', 99.9)}%; budget USD {s.budget:g}/month; "
        "prefer Graviton: "
        f"{(spec.get('finops') or {}).get('prefer_graviton', True)}; traffic growth next year: "
        f"{(spec.get('finops') or {}).get('growth_factor_next_year', 1)}x; files: {k8s} k8s, {tf} terraform."),
        "",
        "Optimization catalog (ids for compare_scenarios):",
        *[f"- {o.id}: {o.summary}" for o in cm.CATALOG],
        f"Rejected in prod by the tool: {', '.join(cm.REJECTED_IN_PROD)}.",
    ]
    if request.feedback:
        lines += ["", (f"Reviewer feedback (it overrides the default choices; never re-add what it excludes): "
                      f"{request.feedback}")]
    previous = request.inputs.get("previous") or {}
    if previous.get("applied_optimizations") is not None:
        lines += [f"Previous run applied: {', '.join(previous['applied_optimizations']) or 'nothing'}."]
    return "\n".join(lines)


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    try:
        session = FinOpsSession.from_request(request)
    except (ValueError, TypeError) as exc:
        yield error(STAGE, f"bad request: {exc}")
        return
    box = ResultBox()
    stream = run_agent(stage=STAGE, system_prompt=SYSTEM_PROMPT, prompt=build_prompt(request, session),
                       tools=build_tools(session, box), workspace=session.ws, box=box)
    started = time.monotonic()
    deadline = started + float(request.options.get("time_budget_s", TIME_BUDGET_S))
    problem = None
    try:
        while True:
            event = await asyncio.wait_for(anext(stream), timeout=max(deadline - time.monotonic(), 0.1))
            if event["type"] == "result":
                if event["metrics"]["submitted"]:
                    yield event
                    return
                problem, metrics = "the model did not call submit_result", event["metrics"]
                break
            if event["type"] == "error":
                problem, metrics = event["message"], {}
                break
            yield event
    except StopAsyncIteration:
        problem, metrics = "the agent stream ended without a result", {}
    except TimeoutError:
        problem, metrics = f"time budget of {deadline - started:.0f}s exceeded", {}
        await stream.aclose()

    # Degrade gracefully: the deterministic tools produce the result without the LLM narrative.
    yield status(STAGE, f"{problem}; completing with the deterministic cost model")
    try:
        session.finalize(box, fallback=True)
    except Exception as exc:  # noqa: BLE001 - the stream must always end with exactly one result or error
        yield error(STAGE, f"{problem}; fallback failed: {type(exc).__name__}: {exc}")
        return
    report = session.ws.read(REPORT_PATH)
    yield {"type": "artifact", "path": REPORT_PATH, "bytes": len(report)}
    yield {
        "type": "result", "stage": STAGE, "summary": box.summary, "data": box.data,
        "artifacts": session.ws.artifacts(),
        "metrics": {"model": model_id_for(STAGE), "latency_s": round(time.monotonic() - started, 1),
                    "input_tokens": metrics.get("input_tokens", 0), "output_tokens": metrics.get("output_tokens", 0),
                    "cycles": metrics.get("cycles", 0), "submitted": False, "fallback": True,
                    "fallback_reason": problem},
    }
