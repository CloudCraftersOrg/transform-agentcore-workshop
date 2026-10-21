"""Agent 3, Code Modernization (the AWS Transform code-transformation and containerization step).

Turns the legacy Flask monolith into a 12-factor microservice + Kustomize manifests. Deterministic tools render
a curated, tested pattern (platform layer, schema conversion, manifests); the model ports only the domain routes
and their tests, looping on the validators. `data` is assembled from validator results, not from model claims.
"""

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from transform_agents.core import AgentRequest, ResultBox, Workspace, error, model_id_for, run_agent, status

from . import checks
from .pattern import Params, scaffold
from .tools import MIN_CHECK_S, RunState, build_tools, scaffold_report, smoke_paths, tests_report

STAGE = "modernization"
# Whole stage, worst case (override with options.time_budget_s). A normal run takes ~50 s. The model loop gets the
# budget minus FINALIZE_RESERVE_S; the deterministic finish (tests + manifests on the final files) gets the rest.
TIME_BUDGET_S = float(os.getenv("MODERNIZATION_TIME_BUDGET_S", "150"))
FINALIZE_RESERVE_S = 30.0
CONTINUE_MIN_S = 40.0  # start the continuation pass only with at least this much model-loop time left
MAX_PASSES = 2  # one continuation pass when the model stops before submitting
OUTPUT_PREFIXES = ("microservice/", "k8s/")
BUNDLED_LEGACY = Path(__file__).resolve().parents[2] / "legacy-infra" / "app"

SYSTEM_PROMPT = """You are the Code Modernization agent of a migration factory (AWS Transform code transformation).
You turn a legacy Flask monolith into a containerized 12-factor microservice by ADAPTING a curated, tested pattern.
You never design the platform yourself: config, database, health probes, logging, gunicorn, Dockerfile and Kubernetes
manifests come from the pattern.

Workflow (about 5 turns; call several tools in one turn when they are independent):
1. Call scaffold_from_pattern with no arguments (defaults come from the migration spec; pass an argument only when
   the reviewer feedback asks for a different value). It returns helpers and a worked example.
2. In ONE turn write both domain files with write_file, adapting the worked example to the legacy code:
   - microservice/app/api.py: every legacy /api route with the same path, methods, status codes and JSON keys.
   - microservice/tests/test_api.py: contract tests, a SQL injection test for the search and oversell tests.
   If the report defaults differ from the legacy report columns, fix microservice/app/report.py with edit_file.
3. Call run_service_tests and validate_manifests in the same turn. Fix failures with edit_file and re-run.
4. Call submit_result with a short summary and the domain changes mapped to finding/requirement IDs.

Porting rules:
- Bound parameters only (":name"); never f-strings, + or % to build SQL. Add ORDER BY id to list queries.
- Search: LOWER(col) LIKE LOWER(:pattern) keeps SQLite's case-insensitive LIKE on PostgreSQL.
- Inserts: "INSERT ... RETURNING id" with .scalar_one() inside transaction(); never lastrowid.
- Stock decrements: one atomic conditional UPDATE ... WHERE id = :id AND quantity >= :n inside transaction(),
  then check rowcount (prevents overselling across replicas).
- Validate input with json_body/int_field/float_field (400 instead of a crash); admin routes call
  require_admin_token("<legacy header name>"). Keep every legacy success response unchanged.
- Do not port HTML pages, form posts, the report route or the status route (CronJob and probes replace them).
- Only microservice/ and k8s/ are writable and platform files are locked. Be terse between tool calls."""


# ---------------------------------------------------------------- prompt


def build_prompt(request: AgentRequest, ws: Workspace, params: Params, revision: bool) -> str:
    spec, assessment = request.inputs.get("spec") or {}, request.inputs.get("assessment") or {}
    parts = [request.instruction or "Modernize the legacy application into the target microservice."]
    if request.feedback:
        parts.append(
            f"## Reviewer feedback (apply it{' by editing the existing files' if revision else ''})\n{request.feedback}"
        )
    parts.append(
        f"## Target (from the migration spec)\nservice {params.service_name}, port {params.port}, probes "
        f"{params.health_path} + {params.readiness_path}, replicas {params.replicas_min}-{params.replicas_max}, "
        f"image {params.image}:{params.image_tag}, database {(spec.get('data') or {}).get('engine', 'postgres')} "
        f"(SQLite locally)."
    )
    reqs = [f"- {r.get('id')}: {r.get('text')}" for r in spec.get("requirements", []) if isinstance(r, dict)]
    if reqs:
        parts.append("## Requirements\n" + "\n".join(reqs[:30]))
    findings = [
        f"- {f.get('id')} [{f.get('severity')}] {f.get('title')} ({f.get('evidence', '')})"
        for f in assessment.get("findings", [])
        if isinstance(f, dict)
    ]
    if findings:
        parts.append("## Assessment findings to fix\n" + "\n".join(findings[:30]))
    parts.append("## Legacy source")
    budget = 40_000
    for path in ws.listing("legacy/"):
        content = ws.read(path)
        if path.endswith((".py", ".sql", ".cron", ".service")) and len(content) <= budget:
            budget -= len(content)
            parts.append(f"### {path}\n```\n{content.rstrip()}\n```")
        else:
            parts.append(f"### {path} ({len(content)} bytes, use read_file if needed)")
    if revision:
        parts.append(
            "## Existing output (previous run)\n" + "\n".join(ws.listing("microservice/") + ws.listing("k8s/"))
        )
    return "\n\n".join(parts)


def load_bundled_legacy() -> dict[str, str]:
    """Fallback for minimal requests: the repo's legacy-infra/app as legacy/app/**."""
    if not BUNDLED_LEGACY.is_dir():
        return {}
    return {
        f"legacy/app/{p.relative_to(BUNDLED_LEGACY).as_posix()}": p.read_text()
        for p in sorted(BUNDLED_LEGACY.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


# ---------------------------------------------------------------- result data

# Changes the pattern always makes; keywords trace them to finding titles and requirement texts.
PLATFORM_CHANGES = [
    (
        "microservice/app/config.py",
        "Settings from environment variables and a Kubernetes Secret; no credentials or paths in code or image",
        ("hard-coded", "hardcoded", "credential"),
    ),
    (
        "microservice/app/api.py",
        (
            "Every legacy /api route ported with identical paths, methods, status codes and "
            "JSON keys (verified by the API parity check)"
        ),
        ("api contract", "json api"),
    ),
    ("microservice/app/api.py", "Search and all SQL use bound parameters (SQL injection fixed)", ("injection",)),
    (
        "microservice/app/api.py",
        "Order creation is one transaction with an atomic conditional stock decrement",
        ("oversell", "race condition"),
    ),
    (
        "microservice/Dockerfile",
        "Multi-stage python:3.12-slim image running as UID 10001, read-only root FS ready",
        ("as root", "non-root"),
    ),
    (
        "microservice/gunicorn.conf.py",
        "gunicorn (gthread) replaces the Flask development server under systemd",
        ("development server", "dev server"),
    ),
    ("microservice/app/logjson.py", "Structured JSON logs to stdout; no log files on disk", ("logs ", "logging")),
    (
        "microservice/app/health.py",
        "Liveness and readiness (database check) endpoints for Kubernetes probes",
        ("health", "readiness"),
    ),
    (
        "microservice/app/report.py",
        "Nightly report runs as a CronJob (`python -m app.report`) writing to stdout or S3, not to local disk",
        ("report", "cron"),
    ),
    (
        "k8s/base/hpa.yaml",
        "2+ replicas spread across zones with an HPA (CPU) and a PodDisruptionBudget",
        ("single point", "availability zone", "scale out", "replicas"),
    ),
    (
        "k8s/base/deployment.yaml",
        (
            "Policy baseline: probes, requests/limits, non-root, read-only root FS, dropped "
            "capabilities, seccomp, pinned image tag"
        ),
        ("policy baseline",),
    ),
    (
        "microservice/app/db.py",
        "SQLAlchemy Core: SQLite locally, Amazon RDS PostgreSQL in production; schema migration on start or as a job",
        ("sqlite", "target database"),
    ),
    (
        "microservice/requirements.txt",
        "Python 3.9/Flask 2.0 upgraded to Python 3.12/Flask 3.1 with pinned versions",
        ("end-of-life", "outdated", "end of life"),
    ),
]


def trace_ids(keywords: tuple[str, ...], assessment: dict, spec: dict) -> list[str]:
    """IDs of findings (by title) and requirements (by text) that mention any keyword."""
    items = [(f.get("id"), f.get("title", "")) for f in assessment.get("findings", []) if isinstance(f, dict)]
    items += [(r.get("id"), r.get("text", "")) for r in spec.get("requirements", []) if isinstance(r, dict)]
    return [i for i, text in items if i and any(k in str(text).lower() for k in keywords)]


def build_data(ws: Workspace, state: RunState, assessment: dict, spec: dict) -> dict[str, Any]:
    p, tests, manifests = state.params, state.tests or {}, state.manifests or {}
    parity = tests.get("parity") or {}
    changes = [{"file": f, "change": c, "addresses": trace_ids(k, assessment, spec)} for f, c, k in PLATFORM_CHANGES]
    model_files = {c["file"] for c in state.changes}
    changes = [c for c in changes if c["file"] not in model_files] + state.changes
    return {
        "service_name": p.service_name,
        "namespace": p.namespace,
        "image": {
            "repository": p.image,
            "tag": p.image_tag,
            "local_tag": p.local_tag,
            "context": "microservice",
            "dockerfile": "microservice/Dockerfile",
            "port": p.port,
        },
        "service": {"name": p.service_name, "port": p.service_port, "target_port": p.port},
        "endpoints": {"health": p.health_path, "readiness": p.readiness_path, "api": parity.get("endpoints", [])},
        "env": [
            {
                "name": "DATABASE_URL",
                "source": "secret",
                "required": True,
                "description": "sqlite:////tmp/app.db locally; postgresql://... (RDS, via AWS Secrets Manager) in prod",
            },
            {"name": "ADMIN_TOKEN", "source": "secret", "required": True, "description": "token for admin-only routes"},
            {"name": "LOG_LEVEL", "source": "configmap", "required": False, "description": "JSON log level (INFO)"},
            {
                "name": "SEED_DEMO_DATA",
                "source": "configmap",
                "required": False,
                "description": "load seed data on start",
            },
            {
                "name": "DB_AUTO_MIGRATE",
                "source": "configmap",
                "required": False,
                "description": "create schema on start",
            },
            {"name": "WEB_CONCURRENCY", "source": "configmap", "required": False, "description": "gunicorn workers"},
            {
                "name": "REPORT_S3_URI",
                "source": "configmap",
                "required": False,
                "description": "s3://bucket/prefix for the report",
            },
        ],
        "resources": {
            "requests": {"cpu": p.cpu_request, "memory": p.memory_request},
            "limits": {"cpu": p.cpu_limit, "memory": p.memory_limit},
        },
        "scaling": {"replicas_min": p.replicas_min, "replicas_max": p.replicas_max, "cpu_target_pct": p.cpu_target_pct},
        "report_job": {"schedule": p.report_schedule, "command": ["python", "-m", "app.report"]},
        "local_overlay": {
            "path": "k8s/overlays/local",
            "database": "sqlite (emptyDir)",
            "replicas": 1,
            "image": f"{p.image}:{p.local_tag}",
        },
        "manifests": [f for f in ws.listing("k8s/") if f.endswith(".yaml")],
        "changes": changes,
        "tests": {
            "command": tests.get("command", "pytest -q"),
            "passed": bool(tests.get("passed")),
            "status": tests.get("status", "not run"),
            "summary": tests.get("summary", ""),
            "output": tests.get("output", "")[-1500:],
            "runs": state.test_runs,
        },
        "api_parity": {k: parity.get(k, []) for k in ("legacy", "missing", "dropped")},
        "validation": {
            "manifests": manifests.get("status", "not run"),
            "kustomize": manifests.get("kustomize", "not run"),
            "policy_failed": [f"{c['id']} {c['resource']}" for c in manifests.get("failed", [])],
            "docker_build": (state.docker or {}).get("status", "not run"),
        },
    }


# ---------------------------------------------------------------- handler


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    """Stream contract events; always ends with exactly one `result` (or `error`) within the time budget."""
    finished = False
    try:
        async for event in _handle(request):
            finished |= event["type"] in ("result", "error")
            yield event
    except Exception as exc:  # noqa: BLE001 - the stream must end cleanly for the orchestrator/UI
        if not finished:
            yield error(STAGE, f"{type(exc).__name__}: {exc}")


async def _handle(request: AgentRequest) -> AsyncIterator[dict]:
    started = time.monotonic()
    budget = float(request.options.get("time_budget_s") or TIME_BUDGET_S)
    deadline = started + budget
    loop_deadline = deadline - min(FINALIZE_RESERVE_S, budget / 4)
    try:
        files = dict(request.files)
        if not any(p.startswith("legacy/") for p in files):
            files.update(load_bundled_legacy())
            yield status(STAGE, "no legacy/** files in the request: using the bundled legacy-infra/app")
        if not any(p.startswith("legacy/") for p in files):
            yield error(STAGE, "no legacy application files (expected legacy/app/**)")
            return
        ws = Workspace(files)
        spec, assessment = request.inputs.get("spec") or {}, request.inputs.get("assessment") or {}
        params = Params.from_inputs(spec, assessment, ws)
        if errors := params.validate():
            yield error(STAGE, "invalid spec values: " + "; ".join(errors))
            return
        revision = bool(request.feedback) and "microservice/app/api.py" in ws.files
        state, box = RunState(params=params, started=started, deadline=loop_deadline), ResultBox()
        tools = build_tools(
            ws, state, box, docker=bool(request.options.get("docker_build")), overrides=bool(request.feedback)
        )
        prompt = build_prompt(request, ws, params, revision)
    except Exception as exc:  # noqa: BLE001 - never crash the event stream
        yield error(STAGE, f"setup failed: {type(exc).__name__}: {exc}")
        return

    threading.Thread(target=checks.prewarm_test_env, daemon=True).start()  # warm the test env while the model thinks
    usage = {"model": model_id_for(STAGE), "input_tokens": 0, "output_tokens": 0, "cycles": 0, "passes": 0}
    todo, problem = "", ""
    for attempt in range(MAX_PASSES):
        run_prompt = prompt if attempt == 0 else continuation_prompt(prompt, ws, state, todo)
        stream = run_agent(
            stage=STAGE,
            system_prompt=SYSTEM_PROMPT,
            prompt=run_prompt,
            tools=tools,
            workspace=ws,
            box=box,
            max_tokens=12000,
        )
        timed_out = False
        try:
            while True:  # a per-event deadline: streaming tool input and long tool calls emit no events
                event = await asyncio.wait_for(anext(stream), timeout=max(loop_deadline - time.monotonic(), 0.1))
                if event["type"] == "error":
                    problem = f"model error: {event['message'][:200]}"
                    yield status(STAGE, f"{problem}: finishing with the current files")
                    break
                if event["type"] == "result":
                    usage = merge_usage(usage, event.get("metrics") or {})
                elif event["type"] != "artifact":
                    yield event
        except StopAsyncIteration:
            pass
        except TimeoutError:
            timed_out, problem = True, f"time budget reached after {time.monotonic() - started:.0f}s"
            yield status(STAGE, f"{problem}: finishing deterministically with the current files")
        finally:
            with contextlib.suppress(RuntimeError):
                await stream.aclose()
        todo = unfinished(ws, state, box)
        if todo is None or timed_out or loop_deadline - time.monotonic() < CONTINUE_MIN_S or attempt == MAX_PASSES - 1:
            break
        yield status(STAGE, f"model stopped before finishing ({todo}): continuing")

    state.closed = True  # from here on only the deterministic finish touches the files and validator results
    async for event in finalize(ws, state, box, usage, spec, assessment, started, request, deadline, problem):
        yield event


def merge_usage(usage: dict, metrics: dict) -> dict:
    """Sum token usage over model passes."""
    merged = {k: usage[k] + (metrics.get(k) or 0) for k in ("input_tokens", "output_tokens", "cycles")}
    return usage | merged | {"model": metrics.get("model", usage["model"]), "passes": usage["passes"] + 1}


def unfinished(ws: Workspace, state: RunState, box: ResultBox) -> str | None:
    """The next step the model still owes, or None once it has submitted."""
    if box.data is not None:
        return None
    if state.scaffold_result is None:
        return "call scaffold_from_pattern"
    if "@bp." not in ws.files.get("microservice/app/api.py", "") or "microservice/tests/test_api.py" not in ws.files:
        return "write microservice/app/api.py and microservice/tests/test_api.py"
    if not (state.tests or {}).get("passed") or state.code_dirty:
        return "call run_service_tests and validate_manifests, fix failures"
    return "call submit_result"


def continuation_prompt(prompt: str, ws: Workspace, state: RunState, todo: str) -> str:
    """A fresh pass for a model that stopped early: the original task plus what is already done."""
    parts = [prompt, "## Progress so far: you stopped before finishing. Continue from here, do not start over."]
    if state.scaffold_result is not None:
        parts.append("scaffold_from_pattern already ran; its output was:\n" + scaffold_report(ws, state))
    written = [p for p in ("microservice/app/api.py", "microservice/tests/test_api.py") if p in ws.files]
    if written:
        parts.append("Already written (read_file to see them): " + ", ".join(written))
    if state.tests is not None:
        parts.append("Last run_service_tests result:\n" + tests_report(state))
    parts.append(f"NEXT STEP: {todo}.")
    return "\n\n".join(parts)


async def in_thread(deadline: float, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking validator in a worker thread, never waiting past the stage deadline (None if it would)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs), timeout=max(deadline - time.monotonic(), 0.1)
        )
    except TimeoutError:
        return None


def not_run(reason: str) -> dict[str, Any]:
    return {"command": "pytest -q", "passed": False, "status": "not run", "summary": f"not run: {reason}", "output": ""}


async def finalize(
    ws: Workspace,
    state: RunState,
    box: ResultBox,
    usage: dict,
    spec: dict,
    assessment: dict,
    started: float,
    request: AgentRequest,
    deadline: float,
    problem: str = "",
) -> AsyncIterator[dict]:
    """Make the result trustworthy whatever the model did: scaffold if skipped, re-validate stale files, all within
    what is left of the stage budget."""
    left = lambda: deadline - time.monotonic()
    if "microservice/app/__init__.py" not in ws.files:
        yield status(STAGE, "model did not scaffold: rendering the pattern deterministically")
        state.scaffold_result = scaffold(ws, state.params)
    if state.tests is None or state.code_dirty:
        timeout = min(checks.TEST_TIMEOUT_S, left() - MIN_CHECK_S)  # keep a few seconds for the manifests
        if timeout < MIN_CHECK_S:
            state.tests = not_run("stage time budget used")
        else:
            yield status(STAGE, f"running the service tests on the final files (limit {timeout:.0f}s)")
            tests = await in_thread(deadline, checks.run_service_tests, ws, timeout=timeout)
            state.tests = tests or not_run(f"timed out after {timeout:.0f}s")
        state.code_dirty = False
    if state.manifests is None or state.k8s_dirty:
        yield status(STAGE, "validating the final manifests")
        timeout = max(0.0, min(checks.KUBECTL_TIMEOUT_S, (left() - 1) / 2))  # two kustomize renders
        manifests = await in_thread(deadline, checks.validate_manifests, ws, state.params, timeout=timeout)
        state.manifests = manifests or {"status": "not run", "kustomize": "not run", "failed": []}
        state.k8s_dirty = False
    if request.options.get("docker_build") and state.docker is None:
        if left() < 30:
            state.docker = {"status": "skipped", "details": "stage time budget used"}
        else:
            yield status(STAGE, "building and smoke-testing the image")
            state.docker = await in_thread(
                deadline, checks.docker_build_check, ws, state.params, smoke_paths(ws), timeout=int(left() - 15)
            ) or {"status": "skipped", "details": "stage time budget used"}

    data = build_data(ws, state, assessment, spec)
    artifacts = {p: c for p, c in ws.files.items() if p.startswith(OUTPUT_PREFIXES)}
    for path, content in artifacts.items():
        yield {"type": "artifact", "path": path, "bytes": len(content)}
    tests, checks_ok = data["tests"], data["tests"]["passed"] and data["validation"]["manifests"] == "pass"
    summary = box.summary or (
        f"Ported {len(data['endpoints']['api'])} API endpoints of the legacy app to {data['service_name']} "
        f"with a Kustomize base and kind overlay. Tests: {tests['summary'] or tests['status']}."
        + (f" Finished deterministically ({problem})." if problem else "")
    )
    if not checks_ok:  # never let a model summary hide a failed validator
        summary += (
            f" WARNING: validation incomplete (tests: {tests['summary'] or tests['status']}; "
            f"manifests: {data['validation']['manifests']})."
        )
    metrics = usage | {"submitted": box.data is not None}
    metrics |= {
        "latency_s": round(time.monotonic() - started, 1),
        "time_budget_s": round(deadline - started),
        "test_runs": state.test_runs,
        "tests_passed": tests["passed"],
        "manifests": data["validation"]["manifests"],
    }
    if box.data is None:
        metrics |= {"fallback": True, "fallback_reason": problem or "the model did not call submit_result"}
    yield {
        "type": "result",
        "stage": STAGE,
        "summary": summary,
        "data": data,
        "artifacts": artifacts,
        "metrics": metrics,
    }
