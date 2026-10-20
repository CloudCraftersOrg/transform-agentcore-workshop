"""Live end-to-end test for k8s_validation: Bedrock + a real kind deploy.

Prerequisites: AWS credentials for Bedrock (e.g. AWS_PROFILE=personal-dev AWS_REGION=us-west-2),
`scripts/install-tools.sh` and `scripts/kind-up.sh`. Run with:
    uv run pytest -q -m live tests/test_k8s_validation*.py

Input: the modernization agent's saved result (tests/fixtures/modernization_result.json) when it exists, sent clean
with `options.simulate_drift = true` so the agent itself injects four SIMULATED hand-edits (k8s_validation/drift.py)
and then has to fix them; otherwise the sample in tests/fixtures/k8s_sample. The full `result` event is saved to
tests/fixtures/k8s_validation_result.json.
"""

import json
from pathlib import Path

import pytest

from transform_agents.core import AgentRequest, Workspace
from transform_agents.k8s_validation.agent import handle
from transform_agents.k8s_validation.deploy import unavailable_reason
from transform_agents.k8s_validation.drift import inject_drift
from transform_agents.k8s_validation.tools import ValidationSession

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "k8s_sample"
MODERNIZATION = FIXTURES / "modernization_result.json"
REQUIREMENTS = FIXTURES / "requirements_result.json"
OUTPUT = FIXTURES / "k8s_validation_result.json"
CLUSTER = "transform-workshop"


def load_input() -> tuple[str, dict[str, str], dict]:
    """Build the request the orchestrator would send: modernization artifacts + data, and the spec if available."""
    spec = json.loads(REQUIREMENTS.read_text()).get("data") or {} if REQUIREMENTS.exists() else {}
    if MODERNIZATION.exists():
        event = json.loads(MODERNIZATION.read_text())
        files = {p: c for p, c in (event.get("artifacts") or {}).items() if p.startswith(("microservice/", "k8s/"))}
        if any(p.startswith("k8s/base/") for p in files) and any(p.endswith("Dockerfile") for p in files):
            return "modernization", files, {"spec": spec, "modernization": event.get("data") or {}}
    files = {p.relative_to(SAMPLE).as_posix(): p.read_text() for p in SAMPLE.rglob("*") if p.is_file()}
    return "sample", files, {"modernization": {"service_name": "inventory-service"}}


async def test_live_validation_fixes_policies_and_deploys_to_kind():
    reason = unavailable_reason(CLUSTER)
    if reason:
        pytest.skip(reason)
    source, files, inputs = load_input()
    drifted, injected = inject_drift(files)
    before = ValidationSession(Workspace(drifted), inputs).validate().failing_rules()

    request = AgentRequest(run_id=f"live-k8s-{source}", files=files, inputs=inputs,
                           options={"live_deploy": True, "kind_cluster": CLUSTER, "simulate_drift": True})
    events = [event async for event in handle(request)]
    assert any(e["type"] == "status" and e["message"] == f"simulated drift: injected {len(injected)} violations"
               for e in events)
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0], terminal
    result = terminal[0]
    assert result["type"] == "result", result

    data, metrics = result["data"], result["metrics"]
    print(f"\nsource={source} metrics={json.dumps(metrics)}")
    print(f"policy_before={data['policy_before']} policy_after={data['policy_after']} "
          f"fixes={json.dumps(data['fixes_applied'])} live={data['live_deploy']['status']} score={data['score']}")
    assert {"passed", "score", "checks", "fixes_applied", "live_deploy"} <= set(data)
    assert isinstance(data["passed"], bool) and 0 <= data["score"] <= 100
    for check in data["checks"]:
        assert {"id", "tool", "status", "resource", "details"} <= set(check)
        assert check["status"] in ("pass", "fail", "warn", "skipped")
    assert any(c["id"] == "schema" and c["tool"] == "kubeconform" for c in data["checks"])

    # SIMULATED drift: injected before the first check, then fixed by the agent and verified by the checks.
    if source == "modernization":
        assert len(injected) == 4 and before == {"POL-001", "POL-005", "POL-009", "POL-011"}
    assert [{k: d[k] for k in ("file", "change")} for d in data["drift_injected"]] == \
        [{k: d[k] for k in ("file", "change")} for d in injected]
    assert all(d["fixed"] for d in data["drift_injected"]), data["drift_injected"]
    assert before, "the input should contain deliberate policy violations"
    assert data["policy_before"] == sorted(before)
    assert data["policy_after"] == [], data["policy_after"]
    assert data["fixes_applied"] and all(f["file"].startswith("k8s/") for f in data["fixes_applied"])
    resolved = {rule for f in data["fixes_applied"] for rule in f["resolves"]}
    assert resolved == before
    assert all(f["file"] in result["artifacts"] for f in data["fixes_applied"])

    # Live deploy to kind + smoke tests (incl. the oversell -> 409 business rule).
    live = data["live_deploy"]
    assert live["status"] == "pass", live
    assert live["cluster"] == f"kind-{CLUSTER}" and live["image"] and "rolled out" in live["rollout"]
    smoke = {t["request"]: t for t in live["smoke_tests"]}
    assert all(t["passed"] for t in smoke.values()), smoke
    assert smoke["POST /api/orders (oversell)"]["status"] == 409
    assert data["passed"] is True

    report = result["artifacts"]["reports/validation-report.md"]
    assert "## Checks" in report and "## Fixes applied" in report and "## Live deploy (kind)" in report
    assert "SIMULATED drift" in report
    assert set(result["artifacts"]) <= {p for p in files if p.startswith("k8s/")} | {"reports/validation-report.md"}
    assert metrics["submitted"] is True
    assert metrics["latency_s"] < 120, f"too slow for the stage: {metrics['latency_s']}s"
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")  # only a passing run replaces the replay fixture
