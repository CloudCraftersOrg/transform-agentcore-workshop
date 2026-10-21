"""Live end-to-end test of the Terraform agent against Bedrock (needs AWS credentials).

    AWS_PROFILE=personal-dev AWS_REGION=us-west-2 uv run pytest -q -m live tests/test_terraform_live.py -s

Inputs come from tests/fixtures/terraform_inputs/. Set TF_LIVE_PIPELINE_INPUTS=1 to use the other stages'
recorded results (tests/fixtures/{requirements,discovery,modernization}_result.json) when they exist.
The full result event is saved to tests/fixtures/terraform_result.json.
"""

import json
import os
from pathlib import Path

import pytest

from transform_agents.core import AgentRequest
from transform_agents.terraform.agent import handle
from transform_agents.terraform.hcl import TfConfig, parse
from transform_agents.terraform.summary import facts, spec_checks

FIXTURES = Path(__file__).parent / "fixtures"
PIPELINE = {"spec": "requirements_result.json", "assessment": "discovery_result.json",
            "modernization": "modernization_result.json"}


def load_inputs() -> dict:
    inputs = {}
    for key, recorded in PIPELINE.items():
        path = FIXTURES / recorded
        if os.getenv("TF_LIVE_PIPELINE_INPUTS") == "1" and path.exists():
            event = json.loads(path.read_text())
            inputs[key] = event.get("data", event)
        else:
            inputs[key] = json.loads((FIXTURES / "terraform_inputs" / f"{key}.json").read_text())
    return inputs


@pytest.mark.live
async def test_terraform_agent_live():
    inputs = load_inputs()
    events = [e async for e in handle(AgentRequest(run_id="live-test", inputs=inputs))]
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0], [e for e in events if e["type"] == "error"]
    result = terminal[0]
    assert result["type"] == "result", result
    (FIXTURES / "terraform_result.json").write_text(json.dumps(result, indent=1) + "\n")
    data, artifacts, metrics = result["data"], result["artifacts"], result["metrics"]
    print("\nmetrics:", json.dumps(metrics))

    # contract: valid Terraform and the required artifacts and data fields
    assert data["valid"] is True, data["validate_output"]
    for name in ("versions.tf", "variables.tf", "main.tf", "outputs.tf", "terraform.tfvars.example"):
        assert f"terraform/{name}" in artifacts
    for key in ("validate_output", "modules", "compute", "database", "network", "registry", "outputs", "spec_coverage"):
        assert data[key], key
    assert {"source": "terraform-aws-modules/eks/aws", "version": "~> 21.0"} in data["modules"]

    # the requested spec items are in the code itself (parsed from the artifacts, not from result.data)
    cfg = TfConfig({p.split("/")[-1]: parse(t) for p, t in artifacts.items() if p.endswith(".tf")})
    code = facts(cfg)
    misses = [c for c in spec_checks(code, inputs["spec"], inputs["modernization"]) if c["status"] != "pass"]
    assert not misses, misses
    spec, image = inputs["spec"], inputs["modernization"]["image"]
    variables_tf = artifacts["terraform/variables.tf"]
    for value in (spec["service"]["name"], spec["target"]["region"], spec["target"]["kubernetes_version"],
                  image["repository"], spec["data"]["version"]):
        assert f'"{value}"' in variables_tf, value
    assert code["database"]["multi_az"] is spec["data"]["multi_az"]
    assert code["database"]["storage_encrypted"] is True and code["database"]["publicly_accessible"] is False
    assert code["network"]["azs"] >= 2 and code["registry"]["immutable_tags"]

    # the agent explained how the spec is covered, with the spec's own requirement ids
    ids = {r["id"] for r in spec.get("requirements", [])}
    covered = {c["requirement"] for c in data["spec_coverage"]}
    assert len(covered & ids) >= 5, covered

    assert metrics["submitted"] and metrics["latency_s"] < 90 and metrics["validate_attempts"] <= 4
