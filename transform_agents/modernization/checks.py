"""Deterministic validators the agent loops on: service tests + API parity, manifests + policy, docker build."""

import functools
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

import yaml

from transform_agents.core import Workspace

from .pattern import PATTERN_DIR, Params, convert_legacy_schema

API_PREFIX = "/api"
TEST_TIMEOUT_S = int(os.getenv("MODERNIZATION_TEST_TIMEOUT", "90"))  # one run; the agent passes less near its deadline
PREWARM_TIMEOUT_S = int(os.getenv("MODERNIZATION_PREWARM_TIMEOUT", "300"))  # cold uv download, off the stage clock
KUBECTL_TIMEOUT_S = 30

# Model-written tests run on the host, so their env is an allowlist: what uv, Python and pytest need. Cloud and SCM
# credentials (AWS_*, AZURE_*, GOOGLE_*, GITHUB_TOKEN, ...) are never passed and HOME is a throwaway dir, so
# ~/.aws, ~/.ssh and ~/.config are not found through the usual paths. This is hygiene, not a sandbox.
_ENV_KEEP = frozenset(
    "PATH LANG LANGUAGE TZ TMPDIR TEMP TMP TERM USER LOGNAME SHELL SYSTEMROOT COMSPEC PATHEXT WINDIR "
    "HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY http_proxy https_proxy no_proxy all_proxy "
    "SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE".split()
)
_ENV_KEEP_PREFIXES = ("LC_", "UV_")
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|PRIVATE_?KEY", re.IGNORECASE)

# Runs inside the isolated service env: pytest, the Flask url_map (API parity) and the contract smoke requests.
_CHECK_SCRIPT = """
import json, logging, sys, tempfile
import pytest
code = pytest.main(["-o", "addopts=", "-q", "-p", "no:cacheprovider", "--no-header", "-rfE", "--tb=short", "tests"])
try:
    from app import create_app
    from app.config import Settings
    from app.db import migrate, seed
    app = create_app(Settings(database_url="sqlite://", log_level="CRITICAL"))
    print("ROUTES_JSON=" + json.dumps([[r.rule, sorted(r.methods - {"HEAD", "OPTIONS"})] for r in app.url_map.iter_rules()]))
    errors = []
    class Grab(logging.Handler):
        def emit(self, record):
            if record.exc_info:
                errors.append(f"{record.exc_info[0].__name__}: {record.exc_info[1]}")
    logging.getLogger().addHandler(Grab())
    results = []
    for step in json.load(open("_smoke_plan.json")):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(database_url=f"sqlite:///{tmp}/smoke.db", admin_token="smoke-admin-token",
                                      log_level="ERROR"))
            engine = app.extensions["engine"]
            migrate(engine)
            seed(engine)
            errors.clear()
            resp = app.test_client().open(step["path"], method=step["method"], json=step["json"],
                                          query_string=step["query"], headers=step["headers"])
            results.append({"request": step["label"], "status": resp.status_code, "error": errors[-1] if errors else ""})
            engine.dispose()
    print("SMOKE_JSON=" + json.dumps(results))
except Exception as exc:
    print(f"ROUTES_ERROR={type(exc).__name__}: {exc}")
sys.exit(int(code))
"""


# ---------------------------------------------------------------- API parity (legacy routes vs ported routes)

_ROUTE = re.compile(r"@\w+\.route\(\s*['\"]([^'\"]+)['\"]([^)]*)\)")
_SHORT = re.compile(r"@\w+\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]")


def _norm(path: str) -> str:
    return re.sub(r"<(?:\w+:)?\w+>", "{}", path)


def _legacy_handlers(ws: Workspace) -> list[tuple[str, list[str], str]]:
    """(rule, methods, handler source) for every route decorator in the legacy Python files."""
    found = []
    for path in ws.listing("legacy/"):
        if not path.endswith(".py"):
            continue
        source = ws.read(path)
        marks = sorted(
            [(m.start(), m.end(), m.group(1), m.group(2), None) for m in _ROUTE.finditer(source)]
            + [(m.start(), m.end(), m.group(2), "", m.group(1)) for m in _SHORT.finditer(source)]
        )
        for i, (_, end, rule, rest, verb) in enumerate(marks):
            body = source[end : marks[i + 1][0] if i + 1 < len(marks) else len(source)]
            if verb:
                methods = [verb.upper()]
            else:
                methods = re.findall(r"['\"](\w+)['\"]", rest.split("methods", 1)[1]) if "methods" in rest else ["GET"]
            found.append((rule, [m.upper() for m in methods], body))
    return found


def legacy_routes(ws: Workspace) -> list[tuple[str, str]]:
    """(path, METHOD) pairs declared with @app.route / @app.get in the legacy Python files."""
    return sorted({(rule, m) for rule, methods, _ in _legacy_handlers(ws) for m in methods})


def _dropped_reason(rule: str) -> str:
    if "report" in rule:
        return "batch job: now the CronJob `python -m app.report`"
    if any(k in rule for k in ("status", "health", "ping")):
        return "replaced by the liveness/readiness probes"
    return "HTML/UI route: out of scope for the API microservice (FR2, low priority)"


def api_parity(legacy: list[tuple[str, str]], ported: list[list[Any]]) -> dict[str, Any]:
    want = {(_norm(r), m) for r, m in legacy if r.startswith(API_PREFIX)}
    have = {(_norm(r), m) for r, methods in ported for m in methods if r.startswith(API_PREFIX)}
    fmt = lambda pairs: [f"{m} {r}" for r, m in sorted(pairs)]
    return {
        "legacy": fmt(want),
        "missing": fmt(want - have),
        "extra": fmt(have - want),
        "dropped": sorted({f"{r} ({_dropped_reason(r)})" for r, _ in legacy if not r.startswith(API_PREFIX)}),
        "endpoints": sorted({re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", r) for r, _ in ported if r.startswith(API_PREFIX)}),
    }


def _sample(field: str, types: dict[str, str]) -> Any:
    """A valid-looking value for a JSON field, typed from the converted schema (unknown fields get 1)."""
    sa_type = types.get(field, "Integer")
    if "Integer" in sa_type:
        return 1
    return 2.5 if sa_type.startswith(("Float", "Numeric")) else f"smoke-{field}"


def smoke_plan(ws: Workspace) -> list[dict[str, Any]]:
    """One well-formed request per legacy /api route, built from the legacy handler code and the schema types:
    JSON fields read from request.get_json(), query args, request headers (sent with the admin token)."""
    tables, _ = convert_legacy_schema(ws)
    types = {c.name: c.sa_type for t in tables for c in t.columns}
    order = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
    plan = []
    for rule, methods, body in _legacy_handlers(ws):
        if not rule.startswith(API_PREFIX):
            continue
        path = re.sub(r"<(?:\w+:)?\w+>", "1", rule)
        var = re.search(r"(\w+)\s*=\s*request\.(?:get_json|json)\b", body)
        fields = sorted(set(re.findall(rf"\b{var.group(1)}(?:\[|\.get\()\s*['\"](\w+)['\"]", body))) if var else []
        payload = {f: _sample(f, types) for f in fields} or None
        query = {q: "a" for q in re.findall(r"request\.args\.get\(\s*['\"](\w+)['\"]", body)}
        headers = {h: "smoke-admin-token" for h in re.findall(r"request\.headers\.get\(\s*['\"]([\w-]+)['\"]", body)}
        for method in methods:
            for q in [{}, query] if query and method == "GET" else [query]:
                label = f"{method} {path}" + (f"?{'&'.join(f'{k}=a' for k in q)}" if q else "")
                send = payload if method in ("POST", "PUT", "PATCH") else None
                plan.append(
                    {
                        "label": label + (f" {json.dumps(send)}" if send else ""),
                        "method": method,
                        "path": path,
                        "json": send,
                        "query": q,
                        "headers": headers,
                    }
                )
    return sorted(plan, key=lambda s: order.get(s["method"], 9))


# ---------------------------------------------------------------- service tests


def _uv_run(*args: str) -> list[str] | None:
    uv = shutil.which("uv")
    return (
        [
            uv,
            "run",
            "--no-project",
            "--quiet",
            "--python",
            "3.12",
            "--with-requirements",
            "requirements-dev.txt",
            "--",
            *args,
        ]
        if uv
        else None
    )


def _test_command() -> tuple[list[str] | None, str]:
    if cmd := _uv_run("python", "_checks.py"):
        return cmd, "uv"
    if all(importlib.util.find_spec(m) for m in ("flask", "sqlalchemy", "pytest")):
        return [sys.executable, "_checks.py"], "current-python"
    return None, "uv not found and flask/sqlalchemy/pytest are not importable"


@functools.cache
def uv_dirs() -> dict[str, str]:
    """The real uv cache and managed-Python dirs, resolved once with the real HOME. Passing them explicitly keeps
    runs under a throwaway HOME fast and offline-capable after the first (prewarm) resolution."""
    uv, dirs = shutil.which("uv"), {}
    for var, args in (("UV_CACHE_DIR", ("cache", "dir")), ("UV_PYTHON_INSTALL_DIR", ("python", "dir"))):
        try:
            proc = subprocess.run([uv, *args], check=False, capture_output=True, text=True, timeout=15) if uv else None
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc and proc.returncode == 0 and proc.stdout.strip():
            dirs[var] = proc.stdout.strip().splitlines()[-1]
    return dirs


def clean_env(home: str) -> dict[str, str]:
    """Environment for model-written code: allowlisted variables only, no credentials, HOME = `home`."""
    env = {
        k: v
        for k, v in os.environ.items()
        if (k in _ENV_KEEP or k.startswith(_ENV_KEEP_PREFIXES)) and not _SECRET_NAME.search(k)
    }
    return env | uv_dirs() | {"HOME": home, "PYTHONDONTWRITEBYTECODE": "1", "UV_NO_PROGRESS": "1"}


def _run_bounded(cmd: list[str], cwd: Any, env: dict[str, str], timeout: float) -> tuple[str, int]:
    """Run a command with a hard timeout that kills its whole process group (uv -> python -> pytest), so a hanging
    model-written test can never outlive its budget. Returns (stdout + stderr, returncode; -1 on timeout)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        out, err = proc.communicate(timeout=max(timeout, 0.1))
        return out + err, proc.returncode
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)  # the session leader's pid is the group id
            else:
                proc.kill()
        except OSError:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return f"{out or ''}{err or ''}\ntimed out after {timeout:.0f}s", -1


def _marker(output: str, name: str) -> Any:
    line = next((ln for ln in output.splitlines() if ln.startswith(f"{name}=")), None)
    return json.loads(line.split("=", 1)[1]) if line else None


def run_service_tests(ws: Workspace, timeout: float = TEST_TIMEOUT_S) -> dict[str, Any]:
    """Run the microservice's pytest suite in an isolated env, check API parity with the legacy app and send one
    well-formed request per legacy /api route (contract smoke: no 5xx allowed)."""
    started = time.time()
    cmd, runner = _test_command()
    result: dict[str, Any] = {"command": "pytest -q", "runner": runner, "passed": False, "status": "fail"}
    if cmd is None:
        return result | {"status": "skipped", "summary": f"skipped: {runner}", "output": ""}
    if "microservice/tests/test_api.py" not in ws.files:
        return result | {"summary": "no domain tests: write microservice/tests/test_api.py", "output": ""}
    root = ws.materialize("microservice")
    (root / "_checks.py").write_text(_CHECK_SCRIPT)
    (root / "_smoke_plan.json").write_text(json.dumps(smoke_plan(ws)))
    (home := root / ".home").mkdir()
    try:
        raw, code = _run_bounded(cmd, root, clean_env(str(home)), timeout)
    except OSError as exc:
        raw, code = f"could not start the tests: {exc}", -1
    finally:
        shutil.rmtree(root, ignore_errors=True)
    routes, smoke = _marker(raw, "ROUTES_JSON"), _marker(raw, "SMOKE_JSON") or []
    route_error = next((ln for ln in raw.splitlines() if ln.startswith("ROUTES_ERROR=")), "")
    output = "\n".join(ln for ln in raw.splitlines() if not ln.startswith(("ROUTES_", "SMOKE_", '{"ts"'))).strip()
    counts = re.findall(r"(\d+) (passed|failed|errors?)", output)
    summary = ", ".join(f"{n} {w}" for n, w in counts) or (output.splitlines()[-1] if output else "no output")
    parity = api_parity(legacy_routes(ws), routes) if routes else {"error": route_error or "app did not import"}
    smoke_failed = [s for s in smoke if s["status"] >= 500]
    passed = code == 0 and not parity.get("missing") and "error" not in parity and not smoke_failed
    return result | {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "summary": summary,
        "output": output[-4000:],
        "parity": parity,
        "smoke": smoke,
        "duration_s": round(time.time() - started, 1),
    }


def prewarm_test_env(timeout: float = PREWARM_TIMEOUT_S) -> bool:
    """Resolve the pattern's test environment (Python 3.12 + requirements-dev.txt) into the real uv cache, with the
    same sandboxed env as run_service_tests, so later runs are fast and work offline. True when it is ready.

    Called in the background by the agent and up front by scripts/preflight.sh."""
    if not (cmd := _uv_run("python", "-c", "import flask, sqlalchemy, pytest")):
        return False
    home = tempfile.mkdtemp(prefix="svc-home-")
    try:
        _, code = _run_bounded(cmd, PATTERN_DIR / "microservice", clean_env(home), timeout)
    except OSError:
        return False
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return code == 0


# ---------------------------------------------------------------- manifests


def _containers(doc: dict) -> tuple[dict, list[dict]]:
    spec = doc.get("spec", {})
    pod = (
        (spec.get("jobTemplate", {}).get("spec", {}) if doc["kind"] == "CronJob" else spec)
        .get("template", {})
        .get("spec", {})
    )
    return pod, pod.get("containers", [])


def policy_checks(docs: list[dict], params: Params, overlay: bool = False) -> list[dict[str, str]]:
    """Baseline policy (TC5): requests/limits, probes, non-root, read-only FS, pinned tags, PDB, HPA, spread."""
    checks: list[dict[str, str]] = []

    def check(cid: str, ok: bool, resource: str, details: str = "") -> None:
        checks.append(
            {"id": cid, "status": "pass" if ok else "fail", "resource": resource, "details": "" if ok else details}
        )

    kinds = {d.get("kind") for d in docs}
    for kind in (
        "Namespace",
        "ServiceAccount",
        "ConfigMap",
        "Deployment",
        "Service",
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "CronJob",
    ):
        check("kind-present", kind in kinds, kind, f"no {kind} rendered")
    for doc in docs:
        kind, name = doc.get("kind"), doc.get("metadata", {}).get("name", "?")
        res = f"{kind}/{name}"
        if kind == "HorizontalPodAutoscaler":
            check("hpa-v2", doc.get("apiVersion") == "autoscaling/v2", res, "use autoscaling/v2")
        if kind not in ("Deployment", "CronJob"):
            continue
        pod, containers = _containers(doc)
        psc = pod.get("securityContext", {})
        check("pod-non-root", psc.get("runAsNonRoot") is True, res, "securityContext.runAsNonRoot must be true")
        check(
            "seccomp",
            psc.get("seccompProfile", {}).get("type") == "RuntimeDefault",
            res,
            "seccompProfile RuntimeDefault",
        )
        if kind == "Deployment":
            check("topology-spread", bool(pod.get("topologySpreadConstraints")), res, "add topologySpreadConstraints")
            want = 1 if overlay else params.replicas_min
            check("replicas", doc.get("spec", {}).get("replicas", 1) >= want, res, f"replicas < {want}")
        for c in containers:
            cres, image = f"{res}/{c.get('name')}", c.get("image", "")
            tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
            check("image-tag", tag not in ("", "latest"), cres, f"image {image!r} must have a pinned tag")
            r = c.get("resources", {})
            check(
                "resources",
                all(r.get(k, {}).get(x) for k in ("requests", "limits") for x in ("cpu", "memory")),
                cres,
                "set cpu/memory requests and limits",
            )
            sc = c.get("securityContext", {})
            check("read-only-fs", sc.get("readOnlyRootFilesystem") is True, cres, "readOnlyRootFilesystem: true")
            check("no-priv-esc", sc.get("allowPrivilegeEscalation") is False, cres, "allowPrivilegeEscalation: false")
            check("drop-all", "ALL" in sc.get("capabilities", {}).get("drop", []), cres, "capabilities.drop: [ALL]")
            if kind == "Deployment":
                paths = {p: c.get(p, {}).get("httpGet", {}).get("path") for p in ("livenessProbe", "readinessProbe")}
                check(
                    "probes",
                    paths == {"livenessProbe": params.health_path, "readinessProbe": params.readiness_path},
                    cres,
                    f"probes must use {params.health_path} / {params.readiness_path}, got {paths}",
                )
                ports = [p.get("containerPort") for p in c.get("ports", [])]
                check("port", params.port in ports, cres, f"containerPort {params.port} missing (got {ports})")
                if overlay:
                    check(
                        "local-image", tag == params.local_tag, cres, f"local overlay must use tag {params.local_tag}"
                    )
    return checks


def validate_manifests(ws: Workspace, params: Params, timeout: float = KUBECTL_TIMEOUT_S) -> dict[str, Any]:
    """YAML parse + `kubectl kustomize` (base and local overlay, `timeout` s each) + built-in policy checks."""
    errors, raw_docs = [], []
    for path in ws.listing("k8s/"):
        if path.endswith((".yaml", ".yml")):
            try:
                raw_docs += [d for d in yaml.safe_load_all(ws.read(path)) if isinstance(d, dict)]
            except yaml.YAMLError as exc:
                errors.append(f"{path}: {exc}")
    if errors:
        return {"status": "fail", "errors": errors, "checks": [], "kustomize": "not run"}
    kubectl, rendered, kustomize = shutil.which("kubectl"), {}, "skipped (kubectl not found)"
    if kubectl and timeout < 1:
        kubectl, kustomize = None, "skipped (stage time budget used)"
    if kubectl:
        root = ws.materialize("k8s")
        try:
            for target in ("base", "overlays/local"):
                proc = subprocess.run(
                    [kubectl, "kustomize", str(root / target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if proc.returncode != 0:
                    errors.append(f"kubectl kustomize k8s/{target}: {proc.stderr.strip()[-800:]}")
                else:
                    rendered[target] = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict)]
            kustomize = "pass" if not errors else "fail"
        except subprocess.TimeoutExpired:  # a slow tool is not a manifest error: fall back to the raw documents
            rendered, kustomize = {}, f"skipped (kubectl kustomize timed out after {timeout:.0f}s)"
        finally:
            shutil.rmtree(root, ignore_errors=True)
    base_docs = rendered.get("base") or [d for d in raw_docs if d.get("kind") != "Kustomization"]
    checks = policy_checks(base_docs, params)
    if "overlays/local" in rendered:
        checks += [
            c | {"id": f"local:{c['id']}"} for c in policy_checks(rendered["overlays/local"], params, overlay=True)
        ]
    failed = [c for c in checks if c["status"] == "fail"]
    return {
        "status": "fail" if errors or failed else "pass",
        "errors": errors,
        "kustomize": kustomize,
        "checks": checks,
        "failed": failed,
        "resources": sorted({f"{d['kind']}/{d['metadata']['name']}" for d in base_docs}),
    }


# ---------------------------------------------------------------- docker


def docker_available() -> str | None:
    """None when docker is usable, otherwise the reason it is not."""
    if not (docker := shutil.which("docker")):
        return "docker CLI not found"
    try:
        ok = (
            subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"], check=False, capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return None if ok else "docker daemon not reachable"


def docker_build_check(
    ws: Workspace, params: Params, smoke_paths: list[str] | None = None, timeout: int = 600
) -> dict[str, Any]:
    """`docker build` the microservice; optionally run it read-only as UID 10001 and GET a few paths."""
    if reason := docker_available():
        return {"status": "skipped", "details": reason}
    tag = f"{params.image}:{params.local_tag}"
    root, started = ws.materialize("microservice"), time.time()
    try:
        proc = subprocess.run(
            ["docker", "build", "-q", "-t", tag, str(root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "fail", "image": tag, "details": f"build timed out after {timeout}s"}
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if proc.returncode != 0:
        return {"status": "fail", "image": tag, "details": proc.stderr.strip()[-2000:]}
    result = {"status": "pass", "image": tag, "build_s": round(time.time() - started, 1), "smoke_tests": []}
    if smoke_paths:
        result["smoke_tests"] = _smoke(tag, params, smoke_paths)
        if any(s["status"] != 200 for s in result["smoke_tests"]):
            result["status"] = "fail"
    return result


def _http_status(url: str, wait_s: float) -> int:
    status, deadline = 0, time.time() + wait_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            pass
        if time.time() >= deadline:
            return status
        time.sleep(0.5)


def _smoke(tag: str, params: Params, paths: list[str]) -> list[dict[str, Any]]:
    """Run the image like Kubernetes does (read-only root FS, /tmp tmpfs) and GET health + API paths."""
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,mode=1777",
            "-e",
            "SEED_DEMO_DATA=true",
            "-p",
            f"127.0.0.1::{params.port}",
            tag,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if run.returncode != 0:
        return [{"request": "docker run", "status": 0, "error": run.stderr.strip()[-500:]}]
    cid = run.stdout.strip()
    try:
        time.sleep(1)
        port = (
            subprocess.run(
                ["docker", "port", cid, f"{params.port}/tcp"], check=False, capture_output=True, text=True, timeout=15
            )
            .stdout.split(":")[-1]
            .strip()
        )
        results = []
        for i, path in enumerate([params.health_path, params.readiness_path, *paths]):
            status = _http_status(f"http://127.0.0.1:{port}{path}", 30 if i == 0 else 5) if port else 0
            results.append({"request": f"GET {path}", "status": status})
            if i == 0 and status != 200:
                logs = subprocess.run(
                    ["docker", "logs", "--tail", "20", cid], check=False, capture_output=True, text=True, timeout=15
                )
                results[-1]["error"] = (logs.stdout + logs.stderr).strip()[-800:]
                break
        return results
    finally:
        subprocess.run(["docker", "rm", "-f", cid], check=False, capture_output=True, timeout=30)
