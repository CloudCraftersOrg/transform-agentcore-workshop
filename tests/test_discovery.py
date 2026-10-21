"""Discovery agent: deterministic scanners and submit validation (offline), plus one live Bedrock run.

Live: AWS_PROFILE=personal-dev uv run pytest -q -m live tests/test_discovery.py
      (SAVE_FIXTURES=1 also writes tests/fixtures/discovery_result.json)
"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from transform_agents.core import AgentRequest, ResultBox, Workspace
from transform_agents.discovery import agent as discovery_agent
from transform_agents.discovery import infra
from transform_agents.discovery.codescan import scan_app
from transform_agents.discovery.tools import (
    APPENDIX_MARKER,
    REPORT,
    RULES,
    DiscoveryContext,
    fallback_assessment,
    finalize_report,
    load_legacy_estate,
    make_tools,
    validate_submission,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_TF = (FIXTURES / "legacy_terraform" / "main.tf").read_text()


def legacy_files(real_terraform: bool = False) -> dict[str, str]:
    """legacy/app from the repo; legacy/terraform from the fixture unless asked for the real one."""
    files = load_legacy_estate()
    if not real_terraform or not any(p.startswith("legacy/terraform/") for p in files):
        files = {p: c for p, c in files.items() if not p.startswith("legacy/terraform/")}
        files["legacy/terraform/main.tf"] = FIXTURE_TF
    return files


def assert_assessment_contract(data: dict) -> None:
    """CONTRACTS.md §1: required keys and types (shared by the offline and live tests)."""
    app = data["application"]
    assert app["name"] == "legacy-inventory" and app["language"] == "python"
    assert app["framework"].startswith("flask 2.") and app["listen_port"] == 5000 and app["entrypoint"] == "app.py"
    assert isinstance(app["loc"], int) and app["loc"] > 50
    assert {c["kind"] for c in data["components"]} <= {"http-api", "web-ui", "batch"}
    assert all(isinstance(c["routes"], list) and c["name"] for c in data["components"])
    deps = data["dependencies"]
    assert deps["database"]["engine"] == "sqlite" and set(deps["database"]["tables"]) == {"items", "orders"}
    assert any("reports" in s for s in deps["local_state"]) and isinstance(deps["external_services"], list)
    inf = data["infrastructure"]
    assert inf["instance_type"] == "t3.large" and (inf["vcpu"], inf["memory_gib"]) == (2, 8)
    assert inf["os"] == "Amazon Linux 2023" and inf["volume"] == {"type": "gp2", "size_gib": 30, "defaulted": False}
    assert inf["process_manager"] == "systemd" and inf["reverse_proxy"] == "nginx" and inf["scheduled_jobs"]
    assert inf["source"] in ("terraform", "live") and inf["region"] == "us-west-2"
    util = data["utilization"]
    assert util["source"] in ("cloudwatch", "estimate") and util["period_days"] > 0
    assert all(isinstance(util[k], (int, float)) for k in ("cpu_avg_pct", "cpu_p95_pct", "mem_avg_pct"))
    ids = [f["id"] for f in data["findings"]]
    assert len(ids) == len(set(ids)) >= 12
    for f in data["findings"]:
        assert f["severity"] in ("critical", "high", "medium", "low")
        assert f["category"] in ("security", "reliability", "operability", "cost")
        assert f["title"] and f["evidence"] and f["recommendation"]
    assert data["strategy"]["recommended"] in ("rehost", "replatform", "refactor", "repurchase", "retain", "retire")
    assert data["strategy"]["rationale"] and data["strategy"]["target"]
    assert isinstance(data["readiness_score"], int) and 0 <= data["readiness_score"] <= 100


# ---- code scan ----

def test_scan_app_inventory():
    code = scan_app(legacy_files())
    app = code["application"]
    assert (app["framework"], app["listen_port"], app["entrypoint"]) == ("flask 2.0.3", 5000, "app.py")
    endpoints = {e for c in code["components"] for e in c["endpoints"]}
    assert "GET /api/items -> list_items()" in endpoints and "POST /api/orders -> create_order()" in endpoints
    assert {c["kind"] for c in code["components"]} == {"http-api", "web-ui", "batch"}
    deps = code["dependencies"]
    assert deps["database"] == {"engine": "sqlite", "location": "/opt/legacy-inventory/data/inventory.db",
                                "tables": ["items", "orders"]}
    assert deps["local_state"] == ["/opt/legacy-inventory/reports", "/var/log/legacy-inventory/app.log"]
    rt = code["runtime"]
    assert (rt["process_manager"], rt["run_as"], rt["reverse_proxy"]) == ("systemd", "root", "nginx")
    assert rt["scheduled_jobs"] == ["nightly job via cron (0 2 * * *): http://127.0.0.1:5000/reports/daily"]


def test_scan_app_signals_have_evidence_and_mask_secrets():
    signals = {s["key"]: s["evidence"] for s in scan_app(legacy_files())["signals"]}
    assert {"sql_injection", "hardcoded_secrets", "unauthenticated_writes", "runs_as_root", "outdated_stack",
            "local_database", "local_files", "dev_server", "hardcoded_config", "file_logging", "host_cron",
            "no_probes", "public_http"} <= set(signals)
    assert "list_items()" in signals["sql_injection"] and "+ search +" in signals["sql_injection"]
    assert "ADMIN_TOKEN" in signals["hardcoded_secrets"] and "admin123" not in signals["hardcoded_secrets"]
    assert "POST /api/orders (create_order)" in signals["unauthenticated_writes"]
    assert "User=root" in signals["runs_as_root"] and "app.run(" in signals["dev_server"]


def test_scan_clean_app_has_no_false_positives():
    app = '''import os
from flask import Flask, request
app = Flask(__name__)
DB = os.environ["DATABASE_URL"]

@app.get("/healthz")
def healthz():
    return "ok"

@app.get("/readyz")
def readyz():
    return db().execute("SELECT 1").fetchone()

@app.get("/api/items")
def items():
    return db().execute("SELECT * FROM items WHERE name LIKE ?", (request.args["q"],)).fetchall()
'''
    signals = {s["key"] for s in scan_app({"legacy/app/main.py": app, "legacy/app/requirements.txt": "flask==3.1.0\n"})["signals"]}
    assert not signals & {"sql_injection", "dev_server", "no_probes", "hardcoded_secrets", "outdated_stack",
                          "file_logging", "runs_as_root"}


# ---- infrastructure ----

def test_parse_fixture_terraform():
    tf = infra.parse_terraform({"legacy/terraform/main.tf": FIXTURE_TF})
    inst = tf["instance"]
    assert (inst["instance_type"], inst["os"], inst["vcpu"], inst["memory_gib"]) == ("t3.large", "Amazon Linux 2023", 2, 8)
    assert inst["ami_source"].startswith("ssm:/aws/service/ami-amazon-linux-latest/al2023")
    assert inst["root_volume"] == {"type": "gp2", "size_gib": 30, "encrypted": None, "defaulted": False}
    assert tf["region"] == "us-west-2" and tf["tags"]["app"] == "legacy-inventory"
    assert tf["security_groups"][0]["ingress"] == [{"protocol": "tcp", "ports": "80", "cidr": "0.0.0.0/0"}]
    assert {s["key"] for s in tf["signals"]} == {"single_instance", "no_backups", "public_http", "volume_encryption"}


def test_parse_terraform_resolves_vars_and_new_style_rules():
    tf = infra.parse_terraform({"legacy/terraform/main.tf": '''
variable "cidr" { default = "0.0.0.0/0" }
resource "aws_instance" "x" {
  ami           = "ami-123"
  instance_type = "m6g.large"
  metadata_options { http_tokens = "required" }
  root_block_device { encrypted = true }
}
resource "aws_vpc_security_group_ingress_rule" "http" {
  ip_protocol = "tcp"
  from_port   = 80
  to_port     = 80
  cidr_ipv4   = var.cidr
}
resource "aws_backup_plan" "b" { name = "daily" }
'''})
    assert tf["instance"]["arch"] == "arm64" and tf["instance"]["imds_http_tokens"] == "required"
    # unset volume_type/volume_size: a plain price key (FinOps looks it up) with the default flagged separately
    assert tf["instance"]["root_volume"] == {"type": "gp2", "size_gib": 8, "encrypted": True, "defaulted": True}
    assert tf["security_groups"][0]["ingress"][0]["cidr"] == "0.0.0.0/0"
    assert {s["key"] for s in tf["signals"]} == {"single_instance", "public_http"}


def test_defaulted_root_volume_is_a_plain_price_key_in_the_assessment():
    """FinOps prices infrastructure.volume.type directly, so it must be "gp2", never "gp2 (default)"."""
    from transform_agents.finops.pricing import load_snapshot

    tf = FIXTURE_TF.replace('volume_type = "gp2"', "").replace("volume_size = 30", "")
    files = {**legacy_files(), "legacy/terraform/main.tf": tf}
    data = fallback_assessment(DiscoveryContext(Workspace(files)))
    volume = data["infrastructure"]["volume"]
    assert volume == {"type": "gp2", "size_gib": 8, "defaulted": True}
    assert volume["type"] in load_snapshot()["ebs_gb_month"]
    ws = Workspace(files)
    finalize_report(ws, data, "summary")
    assert "gp2 8 GiB (AWS default)" in ws.read(REPORT)


def test_parse_terraform_missing_and_broken_files():
    assert infra.parse_terraform({})["source"] == "none"
    broken = infra.parse_terraform({"legacy/terraform/main.tf": "resource {", "legacy/terraform/ok.tf": FIXTURE_TF})
    assert broken["parse_errors"] and broken["instance"]["instance_type"] == "t3.large"


def test_instance_specs_and_percentile():
    assert infra.instance_specs("t3.large") == {"instance_type": "t3.large", "vcpu": 2, "memory_gib": 8,
                                                "arch": "x86_64", "burstable": True}
    assert "error" in infra.instance_specs("x9.huge")
    assert infra.percentile([1, 2, 3, 4, 100], 95) == 100 and infra.percentile([5.0], 95) == 5.0


class FakeCloudWatch:
    def __init__(self, hourly):
        self.hourly, self.periods = hourly, []

    def get_metric_statistics(self, **kw):
        self.periods.append(kw["Period"])
        if kw["Namespace"] == "CWAgent":
            return {"Datapoints": [{"Average": 20.0}, {"Average": 30.0}]}
        if kw["Period"] == 3600 and not self.hourly:
            return {"Datapoints": []}
        return {"Datapoints": [{"Average": a, "Maximum": m} for a, m in [(2, 5), (4, 10), (6, 40)]]}

    def list_metrics(self, **kw):
        return {"Metrics": [{"Dimensions": kw["Dimensions"]}]}


@pytest.mark.parametrize("hourly", [True, False])
def test_cpu_utilization_hourly_and_24h_fallback(monkeypatch, hourly):
    cw = FakeCloudWatch(hourly)
    monkeypatch.setattr(infra, "_client", lambda service, region: cw)
    util = infra.cpu_utilization("us-west-2", "i-123")
    assert (util["cpu_avg_pct"], util["cpu_p95_pct"], util["mem_avg_pct"]) == (4.0, 40, 25.0)
    assert util["source"] == "cloudwatch" and util["period_days"] == (14 if hourly else 1)
    assert cw.periods[0] == 3600 and (hourly or 300 in cw.periods)


def test_live_lookups_degrade_without_credentials(monkeypatch):
    from botocore.exceptions import NoCredentialsError

    def no_creds(service, region):
        raise NoCredentialsError()

    monkeypatch.setattr(infra, "_client", no_creds)
    assert infra.describe_live_instance("us-west-2")["status"] == "skipped"
    assert "NoCredentialsError" in infra.cpu_utilization("us-west-2", "i-1")["reason"]
    ctx = DiscoveryContext(Workspace(legacy_files()), live_aws=True)
    assert ctx.utilization["source"] == "estimate" and "NoCredentialsError" in ctx.utilization["notes"]
    assert ctx.facts()["infrastructure"]["source"] == "terraform"


# ---- findings, submit and report ----

def test_candidate_findings_use_stable_rule_ids():
    findings = DiscoveryContext(Workspace(legacy_files())).candidate_findings()
    by_id = {f["id"]: f for f in findings}
    assert [r["id"] for r in RULES] == sorted(by_id)  # every curated rule fires on the legacy estate
    assert by_id["LF-01"]["title"].startswith("SQL") and by_id["LF-17"]["category"] == "cost"
    assert "nginx.conf" in by_id["LF-06"]["evidence"] and "0.0.0.0/0" in by_id["LF-06"]["evidence"]
    assert "python3 3.9" in by_id["LF-05"]["evidence"] and "source: estimate" in by_id["LF-17"]["evidence"]


def test_validate_submission_reports_precise_errors():
    ctx = DiscoveryContext(Workspace(legacy_files()))
    errors = validate_submission(ctx, {"LF-01": "urgent", "LF-99": "low"}, "lift-and-shift", "short", 120)
    text = "\n".join(errors)
    for expected in ("missing severity for LF-02", "unknown finding id LF-99", "LF-01: severity 'urgent'",
                     "strategy 'lift-and-shift'", "strategy_rationale", "readiness_score", f"write {REPORT}"):
        assert expected in text


def test_submit_tool_merges_facts_and_stops_loop():
    ws = Workspace(legacy_files())
    ctx, box = DiscoveryContext(ws), ResultBox()
    submit = {t.tool_name: t for t in make_tools(ctx, box)}["submit_result"]
    tool_context = SimpleNamespace(invocation_state={})
    severities = {f["id"]: "medium" for f in ctx.candidate_findings()} | {"LF-01": "critical"}
    args = {"summary": "s", "severities": severities, "strategy": "Replatform",
                "strategy_rationale": "Targeted fixes (LF-01, LF-09) and a managed database are enough.",
                "readiness_score": 40, "tool_context": tool_context}
    assert submit(**args).startswith("error:") and box.data is None  # report not written yet
    ws.write(REPORT, "# Assessment: legacy-inventory\n")
    assert submit(**args).startswith("result recorded")
    assert_assessment_contract(box.data)
    assert box.data["strategy"]["recommended"] == "replatform" and box.data["findings"][0]["severity"] == "critical"
    assert tool_context.invocation_state["request_state"]["stop_event_loop"] is True


def test_fallback_and_report_tables_are_regenerated_once():
    ws = Workspace(legacy_files())
    data = fallback_assessment(DiscoveryContext(ws))
    assert_assessment_contract(data)
    ws.write(REPORT, f"# Assessment\n\nnarrative\n\n{APPENDIX_MARKER}\nstale tables")
    finalize_report(ws, data, "summary")
    report = ws.read(REPORT)
    assert report.count(APPENDIX_MARKER) == 1 and "stale tables" not in report and "narrative" in report
    assert "| LF-01 | critical | security |" in report and "## Inventory" in report


def test_revision_prompt_carries_feedback_and_previous_result():
    ctx = DiscoveryContext(Workspace(legacy_files()))
    req = AgentRequest(feedback="LF-07 should be medium", inputs={"previous": {
        "findings": [{"id": "LF-07", "severity": "low"}], "strategy": {"recommended": "replatform"}}})
    prompt = discovery_agent.build_prompt(req, ctx)
    assert "LF-07 should be medium" in prompt and '"LF-07": "low"' in prompt and "write_file" in prompt


async def collect(gen) -> list[dict]:
    return [event async for event in gen]


async def test_handle_falls_back_when_model_does_not_submit(monkeypatch):
    async def fake_run_agent(**kw):
        kw["workspace"].write(REPORT, "# Assessment: legacy-inventory\n\nnarrative")
        yield {"type": "artifact", "path": REPORT, "bytes": 1}
        yield {"type": "result", "stage": "discovery", "summary": "done", "data": {}, "artifacts": {},
               "metrics": {"submitted": False}}

    monkeypatch.setattr(discovery_agent, "run_agent", fake_run_agent)
    events = await collect(discovery_agent.handle(AgentRequest(files=legacy_files())))
    assert [e["type"] for e in events] == ["artifact", "result"]
    result = events[-1]
    assert_assessment_contract(result["data"])
    assert result["summary"].startswith("(fallback") and APPENDIX_MARKER in result["artifacts"][REPORT]


async def test_handle_errors_without_legacy_files(monkeypatch):
    monkeypatch.setattr(discovery_agent, "load_legacy_estate", dict)
    events = await collect(discovery_agent.handle(AgentRequest()))
    assert len(events) == 1 and events[0]["type"] == "error"


# ---- live ----

@pytest.mark.live
async def test_discovery_live_end_to_end():
    request = AgentRequest(run_id="test-discovery", files=legacy_files(real_terraform=True))
    events = await collect(discovery_agent.handle(request))
    assert events[-1]["type"] == "result", events[-1]
    assert sum(e["type"] in ("result", "error") for e in events) == 1
    result = events[-1]
    assert result["metrics"]["submitted"] is True, result["summary"]
    data = result["data"]
    assert_assessment_contract(data)
    sev = {f["id"]: f["severity"] for f in data["findings"]}
    assert sev["LF-01"] in ("critical", "high") and sev["LF-02"] in ("critical", "high")
    assert data["strategy"]["recommended"] in ("replatform", "refactor")
    report = result["artifacts"][REPORT]
    assert report.startswith("# Assessment") and "## Findings" in report and "LF-01" in report
    assert result["metrics"]["latency_s"] < 90
    if os.getenv("SAVE_FIXTURES"):
        (FIXTURES / "discovery_result.json").write_text(json.dumps(result, indent=2) + "\n")


async def test_handle_times_out_into_the_fallback(monkeypatch):
    async def stalled_run_agent(**kw):
        yield {"type": "status", "stage": "discovery", "message": "agent started"}
        await asyncio.sleep(30)  # a stalled model stream
        yield {"type": "result"}

    monkeypatch.setattr(discovery_agent, "run_agent", stalled_run_agent)
    events = await collect(discovery_agent.handle(AgentRequest(files=legacy_files(), options={"timeout_s": 0.2})))
    assert [e["type"] for e in events] == ["status", "artifact", "result"]
    assert events[-1]["metrics"]["timed_out"] is True and events[-1]["metrics"]["submitted"] is False
    assert_assessment_contract(events[-1]["data"])


def test_runtime_serves_the_contract_over_sse(monkeypatch):
    from starlette.testclient import TestClient

    from transform_agents.discovery.runtime import app

    async def no_submit_run_agent(**kw):  # no Bedrock: exercise the AgentCore wrapper and the fallback path
        yield {"type": "result", "stage": "discovery", "summary": "", "data": {}, "artifacts": {}, "metrics": {}}

    monkeypatch.setattr(discovery_agent, "run_agent", no_submit_run_agent)
    response = TestClient(app).post("/invocations", json={"prompt": "assess", "files": legacy_files()})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert response.status_code == 200 and [e["type"] for e in events] == ["artifact", "result"]
    assert_assessment_contract(events[-1]["data"])
