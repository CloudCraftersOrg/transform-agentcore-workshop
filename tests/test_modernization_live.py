"""Live end-to-end test of the modernization agent against Bedrock (needs AWS credentials).

    AWS_PROFILE=personal-dev AWS_REGION=us-west-2 uv run pytest -q -m live tests/test_modernization_live.py -s

Inputs: legacy-infra/app as legacy/app/**, the assessment and spec from tests/fixtures/{discovery,requirements}_result.json
when they exist (else tests/fixtures/modernization_inputs/). The full result event is saved to
tests/fixtures/modernization_result.json for the K8s validation, FinOps and orchestrator stages.
"""

import json
from pathlib import Path

import pytest

from transform_agents.core import AgentRequest, Workspace
from transform_agents.modernization import agent, checks
from transform_agents.modernization.agent import handle, load_bundled_legacy
from transform_agents.modernization.pattern import Params
from transform_agents.modernization.tools import smoke_paths

FIXTURES = Path(__file__).parent / "fixtures"
RESULT = FIXTURES / "modernization_result.json"
REQUIRED = [
    "microservice/Dockerfile",
    "microservice/.dockerignore",
    "microservice/requirements.txt",
    "microservice/app/api.py",
    "microservice/app/models.py",
    "microservice/app/report.py",
    "microservice/tests/test_api.py",
    "k8s/overlays/local/kustomization.yaml",
] + [
    f"k8s/base/{n}"
    for n in (
        "kustomization.yaml",
        "namespace.yaml",
        "serviceaccount.yaml",
        "configmap.yaml",
        "deployment.yaml",
        "service.yaml",
        "hpa.yaml",
        "pdb.yaml",
        "cronjob-report.yaml",
    )
]


def load_inputs() -> dict:
    inputs = {}
    for key, recorded in (("assessment", "discovery_result.json"), ("spec", "requirements_result.json")):
        path = FIXTURES / recorded
        source = json.loads(path.read_text()).get("data") if path.exists() else None
        inputs[key] = source or json.loads((FIXTURES / "modernization_inputs" / f"{key}.json").read_text())
    return inputs


@pytest.mark.live
async def test_modernization_agent_live():
    request = AgentRequest(run_id="live-test", files=load_bundled_legacy(), inputs=load_inputs())
    events = [e async for e in handle(request)]
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0], [e for e in events if e["type"] == "error"]
    result = terminal[0]
    assert result["type"] == "result", result
    data, artifacts, metrics = result["data"], result["artifacts"], result["metrics"]
    print("\nmetrics:", json.dumps(metrics), "\ntests:", data["tests"]["summary"])

    # contract (CONTRACTS.md section 3)
    for key in ("service_name", "image", "endpoints", "env", "resources", "manifests", "changes", "tests"):
        assert data[key], key
    assert data["image"]["dockerfile"] == "microservice/Dockerfile" and data["image"]["port"] == 8080
    assert {"/api/items", "/api/items/{item_id}", "/api/orders"} <= set(data["endpoints"]["api"])
    assert all(path in artifacts for path in REQUIRED), [p for p in REQUIRED if p not in artifacts]
    assert not any(p.startswith("legacy/") for p in artifacts)

    # validators: tests, API parity with the legacy app, manifests + policy
    assert data["tests"]["passed"], data["tests"]["output"]
    assert data["api_parity"]["missing"] == [] and len(data["api_parity"]["legacy"]) == 6
    assert data["validation"]["manifests"] == "pass", data["validation"]
    assert any("LF-01" in c["addresses"] or "SEC5" in c["addresses"] for c in data["changes"])
    assert metrics["submitted"] and metrics["latency_s"] <= agent.TIME_BUDGET_S + 5

    # re-run the generated microservice's own tests independently of the agent
    rerun = checks.run_service_tests(Workspace({**load_bundled_legacy(), **artifacts}))
    assert rerun["passed"], rerun["output"]
    # Record only a good run: later stages and replay read this fixture, and a fallback result must not replace it.
    RESULT.write_text(json.dumps(result, indent=1) + "\n")


@pytest.mark.live
def test_generated_image_builds_and_serves():
    """docker build + run (read-only, UID 10001, SQLite) and GET /healthz, /readyz, /api/items, /api/items/1."""
    if not RESULT.exists():
        pytest.skip("run test_modernization_agent_live first")
    if reason := checks.docker_available():
        pytest.skip(reason)
    ws = Workspace({**load_bundled_legacy(), **json.loads(RESULT.read_text())["artifacts"]})
    report = checks.docker_build_check(ws, Params(), smoke_paths(ws))
    print("\ndocker:", json.dumps(report))
    assert report["status"] == "pass", report
