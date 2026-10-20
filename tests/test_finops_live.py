"""Live end-to-end test: the FinOps agent against Amazon Bedrock with realistic pipeline inputs.

    AWS_PROFILE=<profile> AWS_REGION=us-west-2 uv run pytest -q -m live tests/test_finops_live.py

Saves the full `result` event to tests/fixtures/finops_result.json (replay fixture for the orchestrator/UI).
"""

import json
from pathlib import Path

import pytest

from transform_agents.core import AgentRequest
from transform_agents.finops.agent import handle

FIXTURES = Path(__file__).parent / "fixtures"
INPUTS = FIXTURES / "finops_inputs"
LEVELS = {"low", "medium", "high"}


def pipeline_request() -> AgentRequest:
    files = {str(p.relative_to(INPUTS)): p.read_text() for p in INPUTS.rglob("*")
             if p.is_file() and str(p.relative_to(INPUTS)).startswith(("k8s/", "terraform/"))}
    inputs = {k: json.loads((INPUTS / f"{k}.json").read_text())
              for k in ("assessment", "spec", "modernization", "terraform", "k8s_validation")}
    return AgentRequest(run_id="live-finops", instruction="", inputs=inputs, files=files)


def total(scenario: dict) -> float:
    return round(sum(i["monthly_usd"] for i in scenario["items"]), 2)


@pytest.mark.live
async def test_finops_agent_live():
    events = [ev async for ev in handle(pipeline_request())]
    final = [e for e in events if e["type"] in ("result", "error")]
    assert len(final) == 1 and final[0]["type"] == "result", final
    result = final[0]
    (FIXTURES / "finops_result.json").write_text(json.dumps(result, indent=2) + "\n")

    metrics, d = result["metrics"], result["data"]
    assert metrics["submitted"] is True, metrics  # the LLM finished with submit_result (no fallback)
    assert metrics["latency_s"] < 60, metrics
    assert any(e["type"] == "tool_start" and e["name"] == "compare_scenarios" for e in events)

    # Contract §6 schema
    assert d["currency"] == "USD" and d["pricing_source"].startswith("static-")
    for key in ("current", "target_as_designed", "target_optimized"):
        assert d[key]["items"] and all({"item", "monthly_usd"} <= set(i) for i in d[key]["items"])
        assert d[key]["monthly_usd"] == total(d[key]), key  # totals equal the sum of the line items
    rs = d["rightsizing"]
    assert {"pod_requests", "pod_limits", "replicas_min", "replicas_max", "node_instance_types", "node_min",
            "node_max", "db_instance_class"} <= set(rs)
    assert rs["node_min"] >= 2 and rs["replicas_min"] >= 2  # HA kept
    for r in d["recommendations"]:
        assert r["id"].startswith("FO-") and r["effort"] in LEVELS and r["risk"] in LEVELS
        assert r["applies_to"] in {"k8s", "terraform", "process"} and r["monthly_savings_usd"] >= 0

    # Internal consistency: as designed - required fixes - prod savings == optimized, to the cent
    cur, des, opt = (d[k]["monthly_usd"] for k in ("current", "target_as_designed", "target_optimized"))
    fixes = sum(f["monthly_delta_usd"] for f in d["required_fixes"])
    savings = sum(r["monthly_savings_usd"] for r in d["recommendations"] if r["scope"] == "prod")
    assert round(des + fixes - savings, 2) == opt
    assert opt <= des
    assert cur < opt  # honest: a highly available platform costs more than one EC2 instance

    # Budget verdict: Kubernetes 1.33 is in EKS extended support, so only the optimized design fits USD 450
    assert d["budget_usd"] == 450
    assert d["within_budget"] is (opt <= 450) is True
    assert d["within_budget_as_designed"] is False
    assert "eks_standard_support" in d["applied_optimizations"]
    assert "spot_nodes" not in d["applied_optimizations"]  # prod keeps on-demand / Savings Plan capacity

    report = result["artifacts"]["reports/finops-report.md"]
    for heading in ("| Scenario |", "## What you pay for", "## Recommendations", "## Right-sizing"):
        assert heading in report
    assert d["unverified_numbers"] == [], d["unverified_numbers"]  # the narrative only quotes tool numbers
