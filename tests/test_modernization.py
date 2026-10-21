"""Modernization agent: deterministic pattern, schema conversion, validators and handler (no network, no LLM)."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from transform_agents.core import AgentRequest, ResultBox, Workspace
from transform_agents.modernization import agent, checks, pattern
from transform_agents.modernization.agent import build_data, handle, load_bundled_legacy, trace_ids
from transform_agents.modernization.pattern import EXAMPLE_DIR, Params, scaffold
from transform_agents.modernization.tools import MIN_CHECK_S, RunState, build_tools, parse_changes, smoke_paths

INPUTS = Path(__file__).parent / "fixtures" / "modernization_inputs"
CONTRACT_K8S = [
    "kustomization.yaml",
    "namespace.yaml",
    "serviceaccount.yaml",
    "configmap.yaml",
    "deployment.yaml",
    "service.yaml",
    "hpa.yaml",
    "pdb.yaml",
    "cronjob-report.yaml",
]


def spec() -> dict:
    return json.loads((INPUTS / "spec.json").read_text())


def legacy_ws() -> Workspace:
    return Workspace(load_bundled_legacy())


def example_ws() -> Workspace:
    """The curated worked example as if it were the legacy app, fully ported."""
    ws = Workspace(
        {f"legacy/app/{n}": (EXAMPLE_DIR / n).read_text() for n in ("schema.sql", "seed.sql")}
        | {"legacy/app/app.py": (EXAMPLE_DIR / "legacy_app.py").read_text()}
    )
    scaffold(ws, Params(service_name="tickets-service", image="tickets-service", namespace="tickets"))
    ws.write("microservice/app/api.py", (EXAMPLE_DIR / "api.py").read_text())
    ws.write("microservice/tests/test_api.py", (EXAMPLE_DIR / "test_api.py").read_text())
    return ws


def tool_map(ws: Workspace, state: RunState, box: ResultBox | None = None, overrides: bool = False) -> dict:
    return {t.tool_name: t for t in build_tools(ws, state, box or ResultBox(), overrides=overrides)}


# ---------------------------------------------------------------- params


def test_params_follow_the_spec_and_legacy_cron():
    s = spec()
    s["workload"] |= {"container_port": 9090, "replicas_min": 3, "replicas_max": 5, "health_path": "/livez"}
    p = Params.from_inputs(s, {}, legacy_ws())
    assert (p.service_name, p.namespace, p.image, p.port) == (
        "inventory-service",
        "inventory",
        "inventory-service",
        9090,
    )
    assert (p.replicas_min, p.replicas_max, p.health_path, p.readiness_path) == (3, 5, "/livez", "/readyz")
    assert p.report_schedule == "0 2 * * *"  # from legacy/app/ops/legacy-inventory.cron


def test_params_defaults_and_validation():
    p = Params.from_inputs({}, {"application": {"name": "legacy-inventory"}}, Workspace())
    assert p.service_name == "inventory-service" and p.validate() == []
    assert "image tags must be pinned (not 'latest')" in Params(image_tag="latest").validate()
    assert any("DNS-1123" in e for e in Params(service_name="Bad_Name").validate())
    assert p.override(replicas_min=4, replicas_max=2) == ["need 1 <= replicas_min <= replicas_max"]


# ---------------------------------------------------------------- schema conversion


def test_legacy_schema_converts_to_portable_sqlalchemy():
    tables, notes = pattern.convert_legacy_schema(legacy_ws())
    assert [t.name for t in tables] == ["items", "orders"] and notes == []
    code = pattern.render_models(tables)
    compile(code, "models.py", "exec")
    assert 'Column("sku", Text, nullable=False, unique=True)' in code
    assert 'Column("price", Float, nullable=False, server_default=text("0"))' in code
    assert 'Column("item_id", Integer, ForeignKey("items.id"), nullable=False)' in code
    assert code.count("sqlite_autoincrement=True") == 2


def test_create_table_found_inside_python_strings_and_table_constraints():
    src = (
        'db.execute("""CREATE TABLE IF NOT EXISTS `t` (a INTEGER, b VARCHAR(20), c DATETIME DEFAULT CURRENT_TIMESTAMP,\n'
        'PRIMARY KEY (a), UNIQUE (b, c))""")'
    )
    table, notes = pattern.parse_table(pattern.extract_create_tables(src)[0])
    assert [c.sa_type for c in table.columns] == ["Integer", "String(20)", "Text"]
    assert table.columns[0].primary_key and table.extra == ['UniqueConstraint("b", "c")']
    assert table.columns[2].default == "CURRENT_TIMESTAMP" and "ISO text" in notes[0]


# ---------------------------------------------------------------- scaffold


def test_scaffold_renders_contract_layout_with_spec_values():
    ws = legacy_ws()
    result = scaffold(ws, Params.from_inputs(spec(), {}, ws))
    for name in CONTRACT_K8S:
        assert f"k8s/base/{name}" in ws.files
    for name in (
        "Dockerfile",
        ".dockerignore",
        "requirements.txt",
        "app/__init__.py",
        "app/report.py",
        "tests/conftest.py",
    ):
        assert f"microservice/{name}" in ws.files
    assert "k8s/overlays/local/kustomization.yaml" in ws.files and len(result["written"]) > 25
    assert not any("{{" in content for path, content in ws.files.items() if not path.startswith("legacy/"))
    assert 'COLUMNS = ["sku", "name", "quantity", "price"]' in ws.read("microservice/app/report.py")
    assert ws.read("microservice/app/seed.sql") == ws.read("legacy/app/seed.sql")
    deployment = yaml.safe_load(ws.read("k8s/base/deployment.yaml"))
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "inventory-service:1.0.0" and container["ports"][0]["containerPort"] == 8080
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert "USER 10001" in ws.read("microservice/Dockerfile")


def test_rescaffold_keeps_domain_files():
    ws = legacy_ws()
    scaffold(ws, Params())
    ws.write("microservice/app/api.py", "# ported")
    result = scaffold(ws, Params(replicas_min=3, replicas_max=9))
    assert ws.read("microservice/app/api.py") == "# ported" and "microservice/app/api.py" in result["kept"]
    assert yaml.safe_load(ws.read("k8s/base/hpa.yaml"))["spec"]["maxReplicas"] == 9


# ---------------------------------------------------------------- tools


def test_writes_are_guarded():
    ws, state = legacy_ws(), RunState(params=Params())
    tools = tool_map(ws, state)
    tools["scaffold_from_pattern"]()
    assert "locked platform file" in tools["write_file"](path="microservice/app/db.py", content="x")
    assert "only microservice/ and k8s/" in tools["write_file"](path="legacy/app/app.py", content="x")
    assert tools["write_file"](path="microservice/app/api.py", content="x").startswith("wrote")
    assert "found 0 times" in tools["edit_file"](path="microservice/app/api.py", old="zzz", new="y")
    assert state.code_dirty


def test_scaffold_tool_takes_overrides_only_with_reviewer_feedback():
    ws, state = legacy_ws(), RunState(params=Params())
    plain = tool_map(ws, state)["scaffold_from_pattern"]
    assert plain.tool_spec["inputSchema"]["json"].get("properties", {}) == {}
    out = plain()
    assert "items(id, sku, name, quantity, price)" in out and "AFTER: example/api.py" in out
    revise = tool_map(ws, state, overrides=True)["scaffold_from_pattern"]
    assert revise(image_tag="latest").startswith("error:") and state.params.image_tag == "1.0.0"
    assert "Kept existing" not in revise(replicas_min=3) and state.params.replicas_min == 3  # nothing ported yet


def test_submit_requires_passing_tests_once():
    ws, state, box = legacy_ws(), RunState(params=Params()), ResultBox()
    submit = tool_map(ws, state, box)["submit_result"]
    assert submit(summary="s", changes_json="[]").startswith("error:")
    assert submit(summary="s", changes_json='[{"file": "a", "change": "b", "addresses": ["LF-01"]}]').startswith(
        "result"
    )
    assert box.data == {"changes": [{"file": "a", "change": "b", "addresses": ["LF-01"]}]}


def test_tool_validators_never_run_past_the_model_loop_deadline(monkeypatch):
    seen = []

    def fake_tests(ws, timeout):
        seen.append(timeout)
        return {"passed": True, "status": "pass", "summary": "1 passed", "output": ""}

    monkeypatch.setattr(checks, "run_service_tests", fake_tests)
    ws, state = legacy_ws(), RunState(params=Params(), deadline=time.monotonic() + 40)
    tools = tool_map(ws, state)
    tools["scaffold_from_pattern"]()
    assert tools["run_service_tests"]().startswith("PASS") and 35 < seen[0] <= 40 and checks.TEST_TIMEOUT_S <= 90
    state.deadline = time.monotonic() + 3  # too little left to start the tests: ask the model to submit
    assert tools["run_service_tests"]().startswith("time budget used") and len(seen) == 1
    assert tools["validate_manifests"]().startswith("time budget used")


def test_late_tool_threads_cannot_touch_the_state_after_the_deadline(monkeypatch):
    ws, state = legacy_ws(), RunState(params=Params())
    tools = tool_map(ws, state)
    tools["scaffold_from_pattern"]()

    def tests_outliving_the_loop(ws, timeout):
        state.closed = True  # the stage deadline passes while the tests run
        return {"passed": True, "status": "pass", "summary": "1 passed", "output": ""}

    monkeypatch.setattr(checks, "run_service_tests", tests_outliving_the_loop)
    assert tools["run_service_tests"]().startswith("error: the stage time budget") and state.tests is None
    assert tools["write_file"](path="microservice/app/api.py", content="x").startswith("error: the stage time budget")
    assert tools["scaffold_from_pattern"]().startswith("error: the stage time budget")


def test_parse_changes_is_lenient():
    assert parse_changes('{"changes": [{"file": "f", "change": "c"}]}') == [
        {"file": "f", "change": "c", "addresses": []}
    ]
    assert parse_changes("not json") == []


# ---------------------------------------------------------------- validators


def test_legacy_routes_and_api_parity():
    legacy = checks.legacy_routes(legacy_ws())
    api = [r for r in legacy if r[0].startswith("/api")]
    assert len(api) == 6 and ("/api/items/<int:item_id>", "DELETE") in api
    ported = [["/api/items", ["GET", "POST"]], ["/api/items/<int:item_id>", ["GET", "PUT"]], ["/healthz", ["GET"]]]
    parity = checks.api_parity(legacy, ported)
    assert parity["missing"] == ["DELETE /api/items/{}", "POST /api/orders"]
    assert any(d.startswith("/reports/daily (batch job") for d in parity["dropped"])
    assert parity["endpoints"] == ["/api/items", "/api/items/{item_id}"]
    assert smoke_paths(legacy_ws()) == ["/api/items", "/api/items/1"]


def test_contract_smoke_plan_is_built_from_the_legacy_code():
    plan = {step["label"]: step for step in checks.smoke_plan(legacy_ws())}
    assert set(plan) == {
        "GET /api/items",
        "GET /api/items?search=a",
        "GET /api/items/1",
        "DELETE /api/items/1",
        'POST /api/items {"name": "smoke-name", "price": 2.5, "quantity": 1, "sku": "smoke-sku"}',
        'PUT /api/items/1 {"price": 2.5, "quantity": 1}',
        'POST /api/orders {"item_id": 1, "quantity": 1}',
    }
    assert plan["DELETE /api/items/1"]["headers"] == {"X-Admin-Token": "smoke-admin-token"}


def test_manifests_pass_policy_and_catch_violations():
    ws = legacy_ws()
    params = Params.from_inputs(spec(), {}, ws)
    scaffold(ws, params)
    report = checks.validate_manifests(ws, params)
    assert report["status"] == "pass", report["failed"] or report["errors"]
    assert report["kustomize"] in ("pass", "skipped (kubectl not found)")
    bad = ws.read("k8s/base/deployment.yaml").replace("inventory-service:1.0.0", "inventory-service:latest")
    ws.write("k8s/base/deployment.yaml", bad.replace("readOnlyRootFilesystem: true", "readOnlyRootFilesystem: false"))
    failed = {c["id"] for c in checks.validate_manifests(ws, params)["failed"]}
    assert {"image-tag", "read-only-fs"} <= failed


@pytest.mark.skipif(not shutil.which("kubectl"), reason="kubectl not installed")
def test_local_overlay_runs_one_sqlite_replica_with_dev_image():
    ws = legacy_ws()
    scaffold(ws, Params())
    root = ws.materialize("k8s")
    import subprocess

    out = subprocess.run(
        ["kubectl", "kustomize", str(root / "overlays/local")], capture_output=True, text=True, check=True
    )
    docs = {f"{d['kind']}/{d['metadata']['name'].split('-secrets')[0]}": d for d in yaml.safe_load_all(out.stdout)}
    deploy = docs["Deployment/inventory-service"]
    assert deploy["spec"]["replicas"] == 1 and deploy["metadata"]["namespace"] == "inventory"
    assert deploy["spec"]["template"]["spec"]["containers"][0]["image"] == "inventory-service:dev"
    assert docs["HorizontalPodAutoscaler/inventory-service"]["spec"]["maxReplicas"] == 1
    assert docs["ConfigMap/inventory-service-config"]["data"]["SEED_DEMO_DATA"] == "true"
    assert any(k.startswith("Secret/inventory-service") for k in docs)


def test_pattern_with_worked_example_passes_its_tests(monkeypatch):
    """The curated pattern + example port is known-good (offline uv; skipped when the cache is cold)."""
    if not shutil.which("uv"):
        pytest.skip("uv not installed")
    monkeypatch.setenv("UV_OFFLINE", "1")
    result = checks.run_service_tests(example_ws(), timeout=120)
    if not result["passed"] and ("offline" in result["output"].lower() or "network" in result["output"].lower()):
        pytest.skip("uv cache is cold (run once online)")
    assert result["passed"], result["output"]
    assert result["parity"]["missing"] == [] and "20 passed" in result["summary"]


SECRETS = {
    "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "do-not-leak",
    "AWS_SESSION_TOKEN": "do-not-leak",
    "AWS_PROFILE": "personal-dev",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
    "AZURE_CLIENT_SECRET": "do-not-leak",
    "GOOGLE_APPLICATION_CREDENTIALS": "/keys/sa.json",
    "GITHUB_TOKEN": "do-not-leak",
    "GH_TOKEN": "do-not-leak",
    "OPENAI_API_KEY": "do-not-leak",
    "UV_INDEX_PRIVATE_PASSWORD": "do-not-leak",
    "DATABASE_URL": "postgresql://prod",
    "ADMIN_TOKEN": "do-not-leak",
}
ENV_PROBE = """import os
from pathlib import Path


def test_no_credentials_and_a_throwaway_home():
    leaked = [k for k, v in os.environ.items() if k.startswith(("AWS_", "AZURE", "GOOGLE", "GITHUB")) or v == "do-not-leak"]
    assert leaked == []
    assert Path.home().name == ".home" and not (Path.home() / ".aws").exists()
"""


def test_service_test_env_has_no_credentials_and_a_throwaway_home(monkeypatch, tmp_path):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UV_OFFLINE", "1")
    env = checks.clean_env(str(tmp_path))
    assert not set(SECRETS) & set(env) and "do-not-leak" not in env.values()
    assert env["HOME"] == str(tmp_path) and env["UV_OFFLINE"] == "1" and env["PATH"] == os.environ["PATH"]
    if shutil.which("uv"):  # the real uv cache and Python installs, so a throwaway HOME stays fast and offline
        real = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, check=True).stdout.strip()
        assert env["UV_CACHE_DIR"] == real and env["UV_PYTHON_INSTALL_DIR"]


def test_model_written_tests_see_no_credentials_and_reuse_the_warm_uv_cache(monkeypatch):
    if not shutil.which("uv"):
        pytest.skip("uv not installed")
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("UV_OFFLINE", "1")
    assert checks.prewarm_test_env(timeout=60)  # the path scripts/preflight.sh uses
    ws = example_ws()
    ws.write("microservice/tests/test_env.py", ENV_PROBE)
    result = checks.run_service_tests(ws, timeout=60)
    if not result["passed"] and ("offline" in result["output"].lower() or "network" in result["output"].lower()):
        pytest.skip("uv cache is cold (run once online)")
    assert result["passed"], result["output"]
    assert "21 passed" in result["summary"]


def test_hanging_tests_are_killed_with_their_whole_process_group(tmp_path):
    hang = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    out, code = checks._run_bounded([sys.executable, "-c", hang], tmp_path, checks.clean_env(str(tmp_path)), 1)
    assert code == -1 and "timed out after 1s" in out and time.monotonic() - started < 5
    grandchild = int(out.split()[0])
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild process survived the timeout")


# ---------------------------------------------------------------- result data and handler


def test_build_data_follows_contract_and_traces_findings():
    ws = legacy_ws()
    state = RunState(params=Params())
    state.scaffold_result = scaffold(ws, state.params)
    state.tests = {
        "passed": True,
        "status": "pass",
        "summary": "23 passed",
        "output": "",
        "parity": checks.api_parity(checks.legacy_routes(ws), [["/api/items", ["GET", "POST"]]]),
    }
    state.manifests = {"status": "pass", "kustomize": "pass", "failed": []}
    assessment = {
        "findings": [
            {"id": "LF-01", "title": "SQL built by string concatenation (SQL injection)"},
            {"id": "LF-04", "title": "Service and scheduled job run as root"},
        ]
    }
    data = build_data(ws, state, assessment, spec())
    for key in ("service_name", "image", "endpoints", "env", "resources", "manifests", "changes", "tests"):
        assert data[key], key
    assert data["image"] == {
        "repository": "inventory-service",
        "tag": "1.0.0",
        "local_tag": "dev",
        "context": "microservice",
        "dockerfile": "microservice/Dockerfile",
        "port": 8080,
    }
    assert "k8s/base/deployment.yaml" in data["manifests"] and data["tests"]["passed"] is True
    by_change = {c["change"]: c["addresses"] for c in data["changes"]}
    assert by_change["Search and all SQL use bound parameters (SQL injection fixed)"] == ["LF-01", "SEC5"]
    assert trace_ids(("as root", "non-root"), assessment, spec()) == ["LF-04", "SEC4", "TC5"]


async def test_handle_continues_once_then_always_ends_with_one_result(monkeypatch):
    prompts = []

    async def lazy_model(**kwargs):
        prompts.append(kwargs["prompt"])
        yield {"type": "status", "stage": "modernization", "message": "agent started"}
        yield {
            "type": "result",
            "stage": "modernization",
            "summary": "",
            "data": {},
            "artifacts": {},
            "metrics": {"model": "fake", "input_tokens": 10, "output_tokens": 1, "cycles": 1},
        }

    monkeypatch.setattr(agent, "run_agent", lazy_model)
    monkeypatch.setattr(
        checks,
        "run_service_tests",
        lambda ws, timeout=0: {"passed": False, "status": "fail", "summary": "no domain tests", "output": ""},
    )
    monkeypatch.setattr(checks, "prewarm_test_env", lambda: True)
    events = [e async for e in handle(AgentRequest())]
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0] and terminal[0]["type"] == "result"
    assert len(prompts) == agent.MAX_PASSES and "NEXT STEP: call scaffold_from_pattern" in prompts[1]
    result = terminal[0]
    assert "k8s/base/deployment.yaml" in result["artifacts"] and not any(
        p.startswith("legacy/") for p in result["artifacts"]
    )
    assert result["data"]["tests"]["passed"] is False and result["metrics"]["input_tokens"] == 20
    assert result["metrics"]["submitted"] is False and result["metrics"]["passes"] == 2
    assert any("bundled legacy-infra/app" in e.get("message", "") for e in events)


async def test_handle_rejects_invalid_spec_with_one_error():
    bad = spec()
    bad["service"]["name"] = "x" * 80 + "!"
    bad["workload"]["container_port"] = 70000
    events = [e async for e in handle(AgentRequest(inputs={"spec": bad}))]
    assert events[-1]["type"] == "error" and "port must be" in events[-1]["message"]



def stalled_model(stall_s: float = 60):
    """A model that goes silent, e.g. while streaming a large tool input: no events reach the handler."""

    async def run(**kwargs):
        yield {"type": "status", "stage": "modernization", "message": "agent started"}
        await asyncio.sleep(stall_s)
        yield {"type": "result", "stage": "modernization", "summary": "", "data": {}, "artifacts": {}, "metrics": {}}

    return run


async def test_handle_enforces_the_stage_deadline_without_events(monkeypatch):
    monkeypatch.setattr(agent, "run_agent", stalled_model())
    monkeypatch.setattr(checks, "prewarm_test_env", lambda: True)
    started = time.monotonic()
    events = [e async for e in handle(AgentRequest(options={"time_budget_s": 2}))]
    assert time.monotonic() - started < 3
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and events[-1] is terminal[0] and terminal[0]["type"] == "result"
    result = terminal[0]
    assert result["metrics"]["fallback"] is True and "time budget" in result["metrics"]["fallback_reason"]
    assert result["data"]["tests"]["status"] == "not run" and "WARNING" in result["summary"]
    assert "k8s/base/deployment.yaml" in result["artifacts"]  # deterministic scaffold still delivered


async def test_finalize_gives_the_tests_only_the_remaining_budget(monkeypatch):
    seen = []

    def fake_tests(ws, timeout):
        seen.append(timeout)
        return {"passed": True, "status": "pass", "summary": "1 passed", "output": ""}

    monkeypatch.setattr(checks, "run_service_tests", fake_tests)
    usage = {"model": "fake", "input_tokens": 0, "output_tokens": 0, "cycles": 0, "passes": 0}
    started = time.monotonic()
    state = RunState(params=Params())
    final = agent.finalize(legacy_ws(), state, ResultBox(), usage, {}, {}, started, AgentRequest(), started + 20)
    events = [e async for e in final]
    assert 10 < seen[0] <= 20 - MIN_CHECK_S and events[-1]["metrics"]["time_budget_s"] == 20
    assert await agent.in_thread(time.monotonic() + 0.2, time.sleep, 1) is None  # never waits past the deadline


async def test_handle_falls_back_to_a_result_when_the_model_errors_before_scaffolding(monkeypatch):
    async def broken_model(**kwargs):
        yield {"type": "error", "stage": "modernization", "message": "AccessDeniedException: model not enabled"}

    monkeypatch.setattr(agent, "run_agent", broken_model)
    monkeypatch.setattr(checks, "prewarm_test_env", lambda: True)
    monkeypatch.setattr(
        checks, "run_service_tests", lambda ws, timeout=0: {"passed": False, "status": "fail", "summary": "x"}
    )
    events = [e async for e in handle(AgentRequest())]
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and terminal[0]["type"] == "result" and events[-1] is terminal[0]
    assert "AccessDeniedException" in terminal[0]["metrics"]["fallback_reason"]
    assert "microservice/app/__init__.py" in terminal[0]["artifacts"]

