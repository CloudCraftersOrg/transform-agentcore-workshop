"""Unit tests for the k8s_validation agent's deterministic tools (no network, no LLM).

tests/fixtures/k8s_sample is a small stand-in for the modernization output (microservice/**, k8s/base/**,
k8s/overlays/local/**) with three deliberate policy violations the agent is expected to fix:
POL-005 (no readOnlyRootFilesystem), POL-009 (image `:latest` in base; the local overlay retags it to `:dev`)
and POL-011 (no PodDisruptionBudget). The app already mounts an emptyDir at /tmp, so it still runs once fixed.
"""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from transform_agents.core import AgentRequest, ResultBox, Workspace
from transform_agents.k8s_validation import agent as agent_mod
from transform_agents.k8s_validation import kube
from transform_agents.k8s_validation.deploy import pick_target, smoke_tests
from transform_agents.k8s_validation.drift import inject_drift
from transform_agents.k8s_validation.policy import RULES, builtin_schema_check, check_policies, parse_image
from transform_agents.k8s_validation.report import build_data, render_report
from transform_agents.k8s_validation.tools import ValidationSession, format_policy, validation_tools, write_tools

SAMPLE = Path(__file__).parent / "fixtures" / "k8s_sample"
KNOWLEDGE = Path(__file__).parents[1] / "transform_agents" / "k8s_validation" / "knowledge" / "policy-baseline.md"
needs_kubectl = pytest.mark.skipif(kube.find_binary("kubectl") is None, reason="kubectl not installed")


def load_sample() -> dict[str, str]:
    return {p.relative_to(SAMPLE).as_posix(): p.read_text() for p in SAMPLE.rglob("*") if p.is_file()}


@pytest.fixture(autouse=True)
def offline_kubeconform(monkeypatch):
    """Unit tests never download schemas: kubeconform is hidden, so the builtin fallback runs."""
    real = kube.find_binary
    monkeypatch.setattr(kube, "find_binary", lambda name: None if name == "kubeconform" else real(name))


def deployment(**overrides) -> dict:
    """A fully compliant Deployment; tests break one thing at a time."""
    doc = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api", "namespace": "ns"},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "api"}}, "template": {
            "metadata": {"labels": {"app": "api"}},
            "spec": {"securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{
                         "name": "app", "image": "api:1.0.0", "ports": [{"name": "http", "containerPort": 8080}],
                         "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                       "limits": {"cpu": "500m", "memory": "256Mi"}},
                         "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}},
                         "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
                         "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}}}]}}}}
    container = doc["spec"]["template"]["spec"]["containers"][0]
    for key, value in overrides.items():
        container[key] = value
    return doc


def companions(selector: dict | None = None, target_port="http") -> list[dict]:
    meta = {"name": "api", "namespace": "ns"}
    return [
        {"apiVersion": "v1", "kind": "Service", "metadata": meta,
         "spec": {"selector": selector or {"app": "api"}, "ports": [{"port": 80, "targetPort": target_port}]}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta,
         "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "api"}}}},
        {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": meta,
         "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api"},
                  "minReplicas": 2, "maxReplicas": 6}},
    ]


def ids(findings, status="fail") -> set[str]:
    return {f.id for f in findings if f.status == status}


# --- policy ------------------------------------------------------------------------------------------------------

def test_compliant_manifests_have_no_findings():
    assert check_policies([deployment(), *companions()]) == []


@pytest.mark.parametrize("image,expected", [
    ("api:1.0.0", ("api", "1.0.0", None)),
    ("registry:5000/team/api", ("registry:5000/team/api", None, None)),
    ("api@sha256:abc", ("api", None, "sha256:abc")),
    ("123.dkr.ecr.us-west-2.amazonaws.com/api:latest", ("123.dkr.ecr.us-west-2.amazonaws.com/api", "latest", None)),
])
def test_parse_image(image, expected):
    assert parse_image(image) == expected


@pytest.mark.parametrize("override,rule", [
    ({"resources": {"requests": {"cpu": "100m"}}}, "POL-001"),
    ({"livenessProbe": None}, "POL-002"),
    ({"readinessProbe": None}, "POL-003"),
    ({"securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}, "POL-005"),
    ({"securityContext": {"readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}}, "POL-006"),
    ({"securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False}}, "POL-007"),
    ({"image": "api:latest"}, "POL-009"),
    ({"image": "api"}, "POL-009"),
    ({"readinessProbe": {"httpGet": {"path": "/readyz", "port": 9090}}}, "POL-015"),
])
def test_each_container_rule_detects_its_violation(override, rule):
    assert ids(check_policies([deployment(**override), *companions()])) == {rule}


def test_pod_level_rules():
    dep = deployment()
    dep["spec"]["template"]["spec"]["securityContext"] = {"runAsUser": 0}
    assert ids(check_policies([dep, *companions()])) == {"POL-004", "POL-008"}


def test_availability_rules():
    dep = deployment()
    dep["spec"]["replicas"] = 1
    _svc, pdb, _hpa = companions()
    assert ids(check_policies([dep, _svc])) == {"POL-010", "POL-011", "POL-012"}
    pdb["spec"]["minAvailable"] = 2
    assert ids(check_policies([deployment(), _svc, pdb, _hpa])) == {"POL-016"}


def test_service_wiring_rules():
    assert ids(check_policies([deployment(), *companions(selector={"app": "other"})])) == {"POL-013"}
    assert ids(check_policies([deployment(), *companions(target_port=9999)])) == {"POL-014"}
    assert ids(check_policies([deployment(), *companions(target_port=8080)])) == set()


def test_non_production_overlay_only_warns_on_replicas():
    dep = deployment()
    dep["spec"]["replicas"] = 1
    svc, pdb, hpa = companions()
    hpa["spec"]["minReplicas"] = 1
    assert ids(check_policies([dep, svc, pdb, hpa], target="base")) == {"POL-010", "POL-016"}
    local = check_policies([dep, svc, pdb, hpa], target="overlays/local")
    assert ids(local) == set() and ids(local, "warn") == {"POL-010", "POL-016"}


def test_dev_datastore_is_exempt():
    db = deployment(image="postgres:16-alpine", securityContext=None, resources=None)
    db["metadata"]["name"] = "postgres"
    assert [f for f in check_policies([db]) if f.resource == "Deployment/postgres"] == []


def test_cronjob_containers_are_checked_without_probes():
    cron = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "report"},
            "spec": {"jobTemplate": {"spec": {"template": {"spec": copy.deepcopy(
                deployment()["spec"]["template"]["spec"])}}}}}
    cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = "api:latest"
    assert ids(check_policies([cron])) == {"POL-009"}


@pytest.mark.parametrize("garbage", [
    {"kind": "Deployment", "spec": "oops"},
    {"kind": "Deployment", "metadata": ["x"], "spec": {"template": {"spec": {"containers": ["app", {"ports": [8080],
     "securityContext": "yes", "resources": [], "livenessProbe": "on", "image": 5}]}}}},
    {"kind": "Service", "spec": {"selector": ["a"], "ports": "80"}},
    {"kind": "HorizontalPodAutoscaler", "spec": {"scaleTargetRef": "api", "minReplicas": "two"}},
    {"kind": "PodDisruptionBudget", "spec": {"selector": {"matchLabels": "app"}}},
    {"kind": "CronJob", "spec": {"jobTemplate": None}},
])
def test_malformed_manifests_never_crash_the_checks(garbage):
    check_policies([deployment(), *companions(), garbage])
    builtin_schema_check([garbage])
    pick_target([deployment(), garbage])


def test_builtin_schema_check():
    bad = deployment()
    bad["spec"]["selector"]["matchLabels"] = {"app": "nope"}
    problems = builtin_schema_check([bad, {"kind": "Service"}, {"apiVersion": "v1beta1", "kind": "Deployment",
                                                                  "metadata": {"name": "x"}}])
    text = " ".join(p for _, p in problems)
    assert "selector" in text and "missing apiVersion" in text and "expected apps/v1" in text


def test_knowledge_doc_lists_every_rule():
    doc = KNOWLEDGE.read_text()
    assert all(rule_id in doc for rule_id in RULES)


# --- kubeconform parsing -----------------------------------------------------------------------------------------

def test_parse_kubeconform_classifies_results():
    output = json.dumps({"resources": [
        {"kind": "Deployment", "name": "x", "status": "statusInvalid",
         "validationErrors": [{"path": "/spec/x", "msg": "additional properties 'y' not allowed"}]},
        {"kind": "Service", "name": "s", "status": "statusError", "msg": "failed downloading schema at https://..."},
        {"kind": "Widget", "name": "w", "status": "statusSkipped"}],
        "summary": {"valid": 0, "invalid": 1, "errors": 1, "skipped": 1}})
    summary, invalid, errors = kube.parse_kubeconform(output)
    assert summary["skipped"] == 1
    assert invalid == [("Deployment/x", "/spec/x: additional properties 'y' not allowed")]
    assert errors[0][0] == "Service/s"
    assert kube.parse_kubeconform("not json") is None


# --- session: render + checks on the sample -----------------------------------------------------------------------

@needs_kubectl
def test_sample_has_the_three_deliberate_violations():
    session = ValidationSession(Workspace(load_sample()))
    v = session.validate()
    assert list(v.targets) == ["base", "overlays/local"]
    assert v.failing_rules() == {"POL-005", "POL-009", "POL-011"}
    assert all(tc.schema.tool == "builtin" and tc.schema.status == "warn" for tc in v.targets.values())
    by_rule = {f.id: t for f, t in v.grouped()}
    assert by_rule["POL-009"] == ["base"]  # the local overlay retags the image to :dev
    text = format_policy(session, v)
    assert "edit: k8s/base/deployment.yaml" in text
    assert "write_file k8s/base/pdb.yaml" in text and "edit_file k8s/base/kustomization.yaml" in text
    assert session.validate() is v  # cached by content fingerprint


def test_raw_render_fallback_without_kubectl(monkeypatch):
    monkeypatch.setattr(kube, "find_binary", lambda name: None)
    session = ValidationSession(Workspace(load_sample()))
    v = session.validate()
    local = v.targets["overlays/local"]
    assert local.rendered.tool == "raw" and local.rendered.status == "warn"
    assert v.failing_rules() == {"POL-005", "POL-009", "POL-011"}
    assert "POL-009" not in {f.id for f in local.findings}  # images: newTag applied by the raw renderer


@needs_kubectl
def test_broken_yaml_is_a_render_failure():
    files = load_sample()
    files["k8s/base/service.yaml"] = "apiVersion: v1\nkind: Service\n  metadata: [oops\n"
    v = ValidationSession(Workspace(files)).validate()
    assert not v.render_ok and v.targets["base"].rendered.status == "fail"


def fix_sample(ws: Workspace) -> None:
    """The three fixes the agent is expected to make."""
    dep = ws.read("k8s/base/deployment.yaml").replace("inventory-service:latest", "inventory-service:1.0.0")
    ws.write("k8s/base/deployment.yaml", dep.replace("allowPrivilegeEscalation: false\n",
                                                     "allowPrivilegeEscalation: false\n            readOnlyRootFilesystem: true\n"))
    ws.write("k8s/base/pdb.yaml", yaml.safe_dump({
        "apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "inventory-service"},
        "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "inventory-service"}}}}))
    ws.write("k8s/base/kustomization.yaml", ws.read("k8s/base/kustomization.yaml") + "  - pdb.yaml\n")


@needs_kubectl
def test_data_and_report_after_fixes():
    session = ValidationSession(Workspace(load_sample()), {"modernization": {"service_name": "inventory-service"}})
    session.validate()
    fix_sample(session.ws)
    session.fix_notes.append({"file": "k8s/base/pdb.yaml", "change": "added PDB minAvailable 1"})
    data = build_data(session, session.validate())
    assert data["passed"] and data["policy_before"] == ["POL-005", "POL-009", "POL-011"] and data["policy_after"] == []
    assert data["score"] == 100 - 2 * 2  # two builtin schema warnings (offline)
    fixes = {f["file"]: f for f in data["fixes_applied"]}
    assert set(fixes) == {"k8s/base/deployment.yaml", "k8s/base/pdb.yaml", "k8s/base/kustomization.yaml"}
    assert fixes["k8s/base/deployment.yaml"]["resolves"] == ["POL-005", "POL-009"]
    assert fixes["k8s/base/pdb.yaml"] == {"file": "k8s/base/pdb.yaml", "change": "added PDB minAvailable 1",
                                          "resolves": ["POL-011"]}
    assert data["live_deploy"]["status"] == "skipped"
    for check in data["checks"]:
        assert {"id", "tool", "status", "resource", "details"} <= set(check)
    report = render_report(session, data, "All good.")
    assert "## Checks" in report and "## Fixes applied" in report and "## Live deploy (kind)" in report
    assert "| k8s/base/pdb.yaml | added PDB minAvailable 1 | POL-011 |" in report


# --- tools -------------------------------------------------------------------------------------------------------

@needs_kubectl
def test_submit_refuses_once_while_checks_fail_or_deploy_missing():
    session = ValidationSession(Workspace(load_sample()), options={"live_deploy": True})
    box = ResultBox()
    tools = {t.tool_name: t for t in validation_tools(session, box)}
    assert "deploy_to_kind" in tools
    first = tools["submit_result"](summary="done", fixes=["k8s/base/deployment.yaml: added ro fs"])
    assert first.startswith("not submitted yet") and "POL-005" in first and "deploy_to_kind" in first
    assert box.data is None
    second = tools["submit_result"](summary="done", fixes=["k8s/base/deployment.yaml: added ro fs"], notes="n")
    assert "recorded" in second and box.summary == "done"
    assert session.fix_notes == [{"file": "k8s/base/deployment.yaml", "change": "added ro fs"}]


@needs_kubectl
def test_submit_refuses_once_when_reviewer_feedback_was_not_applied():
    session = ValidationSession(Workspace(load_sample()))
    session.feedback = "raise maxReplicas to 8"
    fix_sample(session.ws)
    session.original = dict(session.ws.files)  # revision input already contains the earlier fixes
    tools = {t.tool_name: t for t in validation_tools(session, ResultBox())}
    assert "feedback was not applied" in tools["submit_result"](summary="ok")
    session.ws.write("k8s/base/hpa.yaml", session.ws.read("k8s/base/hpa.yaml").replace("maxReplicas: 6", "maxReplicas: 8"))
    session.submit_refusals = 0
    assert "recorded" in tools["submit_result"](summary="ok", fixes=["k8s/base/hpa.yaml: maxReplicas 8"])


def test_deploy_is_cached_until_manifests_change(monkeypatch):
    session = ValidationSession(Workspace(load_sample()), options={"live_deploy": True})
    calls = []
    monkeypatch.setattr(session, "_deploy", lambda budget_s: calls.append(1) or session._record({"status": "pass"}))
    assert session.deploy() == session.deploy() == {"status": "pass"} and len(calls) == 1
    session.ws.write("k8s/base/hpa.yaml", "changed")
    session.deploy()
    assert len(calls) == 2


def test_deploy_is_skipped_without_binaries(monkeypatch):
    monkeypatch.setattr(kube, "find_binary", lambda name: None)
    monkeypatch.setattr("transform_agents.k8s_validation.deploy.find_binary", lambda name: None)
    session = ValidationSession(Workspace(load_sample()), options={"live_deploy": True})
    result = session.deploy()
    assert result["status"] == "skipped" and "missing binaries" in result["reason"]


def test_pick_target_prefers_the_app_over_a_dev_database():
    db = deployment(image="postgres:16")
    db["metadata"]["name"] = "postgres"
    target = pick_target([db, deployment(), *companions()], app_names={"api"})
    assert (target.deployment, target.image, target.service, target.service_port) == ("api", "api:1.0.0", "api", 80)
    assert target.rollouts == ["deployment/postgres", "deployment/api"]


class FakeInventory(BaseHTTPRequestHandler):
    stock: ClassVar[dict[int, int]] = {1: 5}

    def _send(self, code: int, body) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/healthz", "/readyz"):
            return self._send(200, {"status": "ok"})
        return self._send(200, [{"id": i, "quantity": q} for i, q in self.stock.items()])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.stock.get(body["item_id"], 0) < body["quantity"]:
            return self._send(409, {"error": "insufficient stock"})
        self.stock[body["item_id"]] -= body["quantity"]
        return self._send(201, {"order_id": 1})

    def log_message(self, *args):
        pass


def test_smoke_tests_against_a_local_fake_service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeInventory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        results = smoke_tests(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
    assert [r["request"] for r in results] == ["GET /healthz", "GET /readyz", "GET /api/items",
                                               "POST /api/orders (qty 1)", "POST /api/orders (oversell)"]
    assert all(r["passed"] for r in results)


# --- handle() ----------------------------------------------------------------------------------------------------

async def collect(request: AgentRequest) -> list[dict]:
    return [event async for event in agent_mod.handle(request)]


def assert_one_terminal(events: list[dict]) -> dict:
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0]
    return terminal[0]


async def test_handle_minimal_request_needs_no_llm(monkeypatch):
    monkeypatch.setattr(agent_mod, "run_agent", None)  # would crash if called
    result = assert_one_terminal(await collect(AgentRequest()))
    assert result["type"] == "result" and result["data"]["passed"] is False
    assert result["data"]["checks"][0]["status"] == "skipped"


def fake_agent(edits):
    """Stands in for core.run_agent: applies `edits(tools, ws)` the way the LLM would, then yields a result."""
    async def run_agent(*, stage, tools, workspace, box, **_):
        by_name = {t.tool_name: t for t in tools}
        yield {"type": "tool_start", "id": "1", "name": "edit_file", "input": "{}"}
        edits(by_name, workspace)
        yield {"type": "result", "stage": stage, "summary": box.summary or "", "data": box.data or {},
               "artifacts": workspace.artifacts(), "metrics": {"model": "fake", "latency_s": 0.1, "input_tokens": 10,
                                                               "output_tokens": 5, "cycles": 1}}
    return run_agent


@needs_kubectl
async def test_handle_with_fake_llm_fixes_and_reports(monkeypatch):
    def edits(tools, ws):
        fix_sample(ws)
        tools["policy_check"]()
        tools["submit_result"](summary="Fixed 3 findings.", fixes=["k8s/base/pdb.yaml: added PDB"])

    monkeypatch.setattr(agent_mod, "run_agent", fake_agent(edits))
    events = await collect(AgentRequest(files=load_sample(), inputs={"modernization": {"service_name": "inventory-service"}}))
    result = assert_one_terminal(events)
    assert [e["name"] for e in events if e["type"] == "tool_start"][:2] == ["kubeconform_check", "policy_check"]
    assert result["summary"] == "Fixed 3 findings." and result["metrics"]["submitted"] is True
    data = result["data"]
    assert data["passed"] and data["policy_after"] == [] and len(data["fixes_applied"]) == 3
    assert set(result["artifacts"]) == {"reports/validation-report.md", "k8s/base/deployment.yaml",
                                        "k8s/base/pdb.yaml", "k8s/base/kustomization.yaml"}
    assert {e["path"] for e in events if e["type"] == "artifact"} == set(result["artifacts"])


@needs_kubectl
async def test_handle_reverts_edits_that_break_the_manifests(monkeypatch):
    def edits(tools, ws):
        tools["write_file"](path="k8s/base/service.yaml", content="kind: Service\n  bad: [yaml\n")

    monkeypatch.setattr(agent_mod, "run_agent", fake_agent(edits))
    result = assert_one_terminal(await collect(AgentRequest(files=load_sample())))
    data = result["data"]
    assert data["fixes_applied"] == [] and "k8s/base/service.yaml" not in result["artifacts"]
    assert data["policy_after"] == ["POL-005", "POL-009", "POL-011"] and not data["passed"]
    assert "reverted" in data["notes"] and result["metrics"]["submitted"] is False


@needs_kubectl
async def test_handle_survives_an_llm_error(monkeypatch):
    async def broken(**_):
        yield {"type": "error", "stage": "k8s_validation", "message": "ThrottlingException: slow down"}

    monkeypatch.setattr(agent_mod, "run_agent", broken)
    result = assert_one_terminal(await collect(AgentRequest(files=load_sample())))
    assert result["type"] == "result" and "ThrottlingException" in result["summary"]
    assert result["metrics"]["llm_error"].startswith("ThrottlingException")
    assert "reports/validation-report.md" in result["artifacts"]


def test_runtime_serves_the_contract_over_sse():
    from starlette.testclient import TestClient

    from transform_agents.k8s_validation.runtime import app

    response = TestClient(app).post("/invocations", json={"prompt": "validate"})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert response.status_code == 200 and [e["type"] for e in events] == ["status", "result"]


# --- simulated drift, write guard, artifact selection, path containment -------------------------------------------

def clean_sample() -> dict[str, str]:
    ws = Workspace(load_sample())
    fix_sample(ws)
    return dict(ws.files)


DRIFT_RULES = {"POL-001", "POL-005", "POL-009", "POL-011"}


def test_inject_drift_adds_four_hand_edits_deterministically():
    files = clean_sample()
    drifted, injected = inject_drift(files)
    assert [(d["file"], d["rule"]) for d in injected] == [
        ("k8s/base/deployment.yaml", "POL-009"), ("k8s/base/deployment.yaml", "POL-001"),
        ("k8s/base/deployment.yaml", "POL-005"), ("k8s/base/kustomization.yaml", "POL-011")]
    assert all(d["change"] for d in injected) and inject_drift(files) == (drifted, injected)
    dep = yaml.safe_load(drifted["k8s/base/deployment.yaml"])["spec"]["template"]["spec"]["containers"][0]
    assert dep["image"] == "inventory-service:latest" and "limits" not in dep["resources"]
    assert dep["resources"]["requests"] and dep["securityContext"]["readOnlyRootFilesystem"] is False
    assert "pdb.yaml" not in yaml.safe_load(drifted["k8s/base/kustomization.yaml"])["resources"]
    assert "k8s/base/pdb.yaml" in drifted and files == clean_sample()  # input untouched
    assert ValidationSession(Workspace(files)).validate().failing_rules() == set()
    assert ValidationSession(Workspace(drifted)).validate().failing_rules() == DRIFT_RULES


def test_inject_drift_skips_what_is_already_broken():
    drifted, injected = inject_drift(load_sample())  # sample: :latest, no readOnlyRootFilesystem, no pdb
    assert [d["rule"] for d in injected] == ["POL-001"]
    assert inject_drift({"k8s/base/service.yaml": "kind: Service\n"}) == ({"k8s/base/service.yaml": "kind: Service\n"},
                                                                           [])


def test_pol001_fix_snippet_uses_modernization_resources():
    drifted, _ = inject_drift(clean_sample())
    session = ValidationSession(Workspace(drifted), {"modernization": {"resources": {
        "requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "750m", "memory": "300Mi"}}}})
    text = format_policy(session, session.validate())
    assert "limits:\n  cpu: 750m\n  memory: 300Mi" in text


def test_write_tools_only_change_k8s_files():
    ws = Workspace(load_sample())
    tools = {t.tool_name: t for t in write_tools(ws)}
    dockerfile = ws.read("microservice/Dockerfile")
    assert "outside k8s/" in tools["write_file"](path="microservice/Dockerfile", content="FROM evil")
    assert "outside k8s/" in tools["edit_file"](path="microservice/Dockerfile", old="FROM", new="FROM evil")
    assert "outside k8s/" in tools["write_file"](path="reports/validation-report.md", content="fake")
    assert "invalid workspace path" in tools["write_file"](path="../k8s/x.yaml", content="x")
    assert ws.read("microservice/Dockerfile") == dockerfile and ws.changed == []
    assert tools["write_file"](path="k8s/base/pdb.yaml", content="kind: PodDisruptionBudget\n").startswith("wrote")
    assert tools["edit_file"](path="k8s/base/hpa.yaml", old="maxReplicas: 6", new="maxReplicas: 8") == \
        "edited k8s/base/hpa.yaml"
    assert ws.changed == ["k8s/base/pdb.yaml", "k8s/base/hpa.yaml"]


def test_select_artifacts_keeps_only_k8s_and_the_report():
    session = ValidationSession(Workspace(load_sample()))
    session.ws.write("microservice/Dockerfile", "FROM evil")  # e.g. a write that bypassed the tools
    session.ws.write("k8s/base/hpa.yaml", session.ws.read("k8s/base/hpa.yaml") + "# changed\n")
    session.ws.write("notes.txt", "x")
    session.ws.write(agent_mod.REPORT_PATH, "# report")
    assert set(agent_mod.select_artifacts(session)) == {"k8s/base/hpa.yaml", agent_mod.REPORT_PATH}
    every_k8s = {p for p in session.ws.files if p.startswith("k8s/")}
    assert set(agent_mod.select_artifacts(session, revision=True)) == every_k8s | {agent_mod.REPORT_PATH}


def test_raw_render_never_reads_outside_the_k8s_root(tmp_path):
    (tmp_path / "secret.yaml").write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: stolen}\n")
    root = tmp_path / "k8s"
    (root / "base").mkdir(parents=True)
    (root / "base" / "ok.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ok}\n")
    (root / "base" / "kustomization.yaml").write_text(
        f"resources:\n  - ok.yaml\n  - ../../secret.yaml\n  - {tmp_path / 'secret.yaml'}\n  - ../..\n")
    text, docs, note = kube.raw_render(root, "base")
    assert [d["metadata"]["name"] for d in docs] == ["ok"] and "stolen" not in text
    assert note.count("outside k8s/") == 3
    assert kube.raw_render(root, "../..")[1] == []


async def test_handle_simulated_drift_is_detected_fixed_and_labelled(monkeypatch):
    clean = clean_sample()

    def edits(tools, ws):  # the model restores the hand-edited files
        for path in ("k8s/base/deployment.yaml", "k8s/base/kustomization.yaml"):
            assert tools["write_file"](path=path, content=clean[path]).startswith("wrote")
        tools["submit_result"](summary="Fixed the drift.", fixes=["k8s/base/deployment.yaml: pinned image, limits"])

    monkeypatch.setattr(agent_mod, "run_agent", fake_agent(edits))
    events = await collect(AgentRequest(files=clean, inputs={"modernization": {"service_name": "inventory-service"}},
                                        options={"simulate_drift": True}))
    result = assert_one_terminal(events)
    drift_status = [e for e in events if e["type"] == "status" and "simulated drift" in e["message"]]
    assert drift_status and drift_status[0]["message"] == "simulated drift: injected 4 violations"
    data = result["data"]
    assert data["simulated_drift"] is True
    assert [{k: d[k] for k in ("file", "change")} for d in data["drift_injected"]] and \
        all(set(d) == {"file", "change", "rule", "fixed"} and d["fixed"] for d in data["drift_injected"])
    assert set(data["policy_before"]) == DRIFT_RULES and data["policy_after"] == []
    assert {r for f in data["fixes_applied"] for r in f["resolves"]} == DRIFT_RULES
    assert result["artifacts"]["k8s/base/deployment.yaml"] == clean["k8s/base/deployment.yaml"]
    assert set(result["artifacts"]) == {"k8s/base/deployment.yaml", "k8s/base/kustomization.yaml",
                                        "reports/validation-report.md"}
    report = result["artifacts"]["reports/validation-report.md"]
    assert "## SIMULATED drift (demo)" in report and "(SIMULATED drift run)" in report
    assert "The agent fixed 4/4." in report


async def test_handle_unfixed_drift_never_reaches_the_artifacts(monkeypatch):
    clean = clean_sample()
    monkeypatch.setattr(agent_mod, "run_agent", fake_agent(lambda tools, ws: None))
    result = assert_one_terminal(await collect(AgentRequest(files=clean, options={"simulate_drift": True})))
    data = result["data"]
    assert set(data["policy_after"]) == DRIFT_RULES and not data["passed"]
    assert not any(d["fixed"] for d in data["drift_injected"]) and "SIMULATED drift was not fully fixed" in data["notes"]
    for path in ("k8s/base/deployment.yaml", "k8s/base/kustomization.yaml"):
        assert result["artifacts"][path] == clean[path]


async def test_handle_skips_drift_on_revision_and_returns_every_k8s_file(monkeypatch):
    clean = clean_sample()

    def edits(tools, ws):
        tools["edit_file"](path="k8s/base/hpa.yaml", old="maxReplicas: 6", new="maxReplicas: 8")
        tools["write_file"](path="microservice/Dockerfile", content="FROM evil")  # refused by the guard
        tools["submit_result"](summary="Raised maxReplicas.", fixes=["k8s/base/hpa.yaml: maxReplicas 8"])

    monkeypatch.setattr(agent_mod, "run_agent", fake_agent(edits))
    events = await collect(AgentRequest(files=clean, feedback="raise maxReplicas to 8",
                                        options={"simulate_drift": True}))
    result = assert_one_terminal(events)
    assert any("simulated drift skipped" in e.get("message", "") for e in events)
    assert result["data"]["drift_injected"] == [] and result["data"]["policy_after"] == []
    assert set(result["artifacts"]) == {p for p in clean if p.startswith("k8s/")} | {"reports/validation-report.md"}
    assert "maxReplicas: 8" in result["artifacts"]["k8s/base/hpa.yaml"]
    assert not any(p.startswith("microservice/") for p in result["artifacts"])
