"""Strands tools for the modernization agent, bound to one workspace and one run state.

The model calls: scaffold_from_pattern -> write_file/edit_file (domain only) -> run_service_tests +
validate_manifests (-> docker_build_check) -> submit_result. Every tool returns short, actionable text.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from strands import tool

from transform_agents.core import ResultBox, Workspace, extract_json, workspace_tools

from . import checks
from .pattern import LOCKED_FILES, Params, example_text, scaffold

MAX_TEST_RUNS = 4
SOFT_LIMIT_S = int(os.getenv("MODERNIZATION_SOFT_LIMIT_S", "90"))  # after this the tools ask the model to submit
SUBMIT_MARGIN_S = 20  # ... and also when the model loop has less than this left (one more turn to submit)
MIN_CHECK_S = 5  # do not start a validator with less time than this before the deadline
WRITABLE = ("microservice/", "k8s/")


@dataclass
class RunState:
    params: Params
    started: float = field(default_factory=time.monotonic)
    deadline: float = 0.0  # time.monotonic() by which every tool call must be done (0: no limit)
    closed: bool = False  # set when the model loop ends: late tool threads must not touch the state any more
    scaffold_result: dict[str, Any] | None = None
    tests: dict[str, Any] | None = None
    test_runs: int = 0
    manifests: dict[str, Any] | None = None
    docker: dict[str, Any] | None = None
    code_dirty: bool = True
    k8s_dirty: bool = True
    submit_warned: bool = False
    changes: list[dict[str, Any]] = field(default_factory=list)

    def over_budget(self) -> bool:
        late = bool(self.deadline) and self.deadline - time.monotonic() < SUBMIT_MARGIN_S
        return late or time.monotonic() - self.started > SOFT_LIMIT_S

    def remaining(self, cap: float) -> float:
        """Seconds a validator may run: at most `cap` and never past the model-loop deadline."""
        return cap if not self.deadline else max(0.0, min(cap, self.deadline - time.monotonic()))

    def touch(self, path: str) -> None:
        self.code_dirty |= path.startswith("microservice/")
        self.k8s_dirty |= path.startswith("k8s/")


BUDGET_MSG = "time budget used: stop editing and call submit_result now"
CLOSED_MSG = "error: the stage time budget is used up; the result is being finished from the current files"


def _guard(path: str, state: RunState) -> str | None:
    if state.closed:
        return CLOSED_MSG
    norm = Workspace.normalize(path)
    if not norm.startswith(WRITABLE):
        return f"error: only microservice/ and k8s/ are writable (got {norm})"
    if norm in LOCKED_FILES:
        return (
            f"error: {norm} is a locked platform file from the pattern. Put domain code in "
            f"microservice/app/api.py, or call scaffold_from_pattern with different parameters."
        )
    return None


def build_tools(ws: Workspace, state: RunState, box: ResultBox, docker: bool = False, overrides: bool = False) -> list:
    """All tools for one run: read-only workspace tools, guarded writers, pattern + validators, submit."""
    read_tools = workspace_tools(ws, writable=False)

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a domain file (microservice/app/api.py, microservice/tests/test_api.py, ...).

        Args:
            path: Relative path under microservice/ or k8s/.
            content: Full file content.
        """
        if err := _guard(path, state):
            return err
        norm = ws.write(path, content)
        state.touch(norm)
        return f"wrote {norm} ({len(content)} bytes)"

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace one exact, unique snippet in an existing file.

        Args:
            path: Relative path of the file.
            old: Exact text to replace; must appear exactly once.
            new: Replacement text.
        """
        if err := _guard(path, state):
            return err
        try:
            current = ws.read(path)
        except KeyError:
            return f"error: {path} not found"
        if (count := current.count(old)) != 1:
            return f"error: snippet found {count} times in {path}; include more surrounding context so it is unique"
        norm = ws.write(path, current.replace(old, new))
        state.touch(norm)
        return f"edited {norm}"

    def render(**overrides: Any) -> str:
        if state.closed:
            return CLOSED_MSG
        if errors := state.params.override(**overrides):
            return "error: " + "; ".join(errors)
        state.scaffold_result = scaffold(ws, state.params)
        state.code_dirty = state.k8s_dirty = True
        return scaffold_report(ws, state)

    @tool(name="scaffold_from_pattern")
    def scaffold_from_spec() -> str:
        """Render the curated, tested microservice pattern into microservice/ and k8s/ with the values from the
        migration spec, convert the legacy schema and return the helpers plus the worked example to adapt."""
        return render()

    @tool(name="scaffold_from_pattern")
    def scaffold_with_overrides(
        service_name: str = "",
        port: int = 0,
        replicas_min: int = 0,
        replicas_max: int = 0,
        image_tag: str = "",
        cpu_request: str = "",
        memory_request: str = "",
        cpu_limit: str = "",
        memory_limit: str = "",
    ) -> str:
        """Re-render the pattern (platform files and k8s/; existing domain files are kept). Every argument
        defaults to the migration spec: pass one only when the reviewer feedback asks for a different value.

        Args:
            service_name: Kubernetes/image name override, e.g. "inventory-service".
            port: Container port override.
            replicas_min: Minimum replicas (Deployment + HPA).
            replicas_max: Maximum replicas (HPA).
            image_tag: Image tag override (never "latest").
            cpu_request: Container CPU request, e.g. "100m".
            memory_request: Container memory request, e.g. "128Mi".
            cpu_limit: Container CPU limit, e.g. "500m".
            memory_limit: Container memory limit, e.g. "256Mi".
        """
        return render(
            service_name=service_name,
            image=service_name,
            port=port,
            replicas_min=replicas_min,
            replicas_max=replicas_max,
            image_tag=image_tag,
            cpu_request=cpu_request,
            memory_request=memory_request,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
        )

    # Without reviewer feedback the model cannot override the spec (weaker models "helpfully" change values).
    scaffold_from_pattern = scaffold_with_overrides if overrides else scaffold_from_spec

    @tool
    def run_service_tests() -> str:
        """Run the microservice pytest suite in an isolated Python 3.12 env, check that every legacy /api route
        is ported (API parity) and send one legacy-style request per route (no 5xx allowed). Returns PASS or
        the failures to fix."""
        timeout = state.remaining(checks.TEST_TIMEOUT_S)
        if state.test_runs >= MAX_TEST_RUNS or state.over_budget() or timeout < MIN_CHECK_S:
            return f"{BUDGET_MSG} (last result: {(state.tests or {}).get('summary', 'none')})"
        state.test_runs += 1
        result = checks.run_service_tests(ws, timeout=timeout)
        if state.closed:  # the deadline passed while the tests ran: the deterministic finish owns the state now
            return CLOSED_MSG
        state.tests, state.code_dirty = result, False
        return tests_report(state)

    @tool
    def validate_manifests() -> str:
        """Parse k8s/**, render `kubectl kustomize` for k8s/base and k8s/overlays/local and run the policy
        baseline (requests/limits, probes, non-root, read-only FS, pinned tags, HPA, PDB, spread)."""
        timeout = state.remaining(checks.KUBECTL_TIMEOUT_S)
        if timeout < MIN_CHECK_S:
            return BUDGET_MSG
        result = checks.validate_manifests(ws, state.params, timeout=timeout)
        if state.closed:
            return CLOSED_MSG
        state.manifests, state.k8s_dirty = result, False
        return manifests_report(state.manifests)

    @tool
    def submit_result(summary: str, changes_json: str = "[]") -> str:
        """Submit the result. Call exactly once, at the very end, after run_service_tests passed.

        Args:
            summary: 2-4 sentences for the reviewer: what was ported, what was fixed, test results.
            changes_json: JSON array of the domain changes you made, e.g.
                [{"file": "microservice/app/api.py", "change": "bound search parameter", "addresses": ["LF-01", "SEC5"]}]
                (platform changes are recorded automatically).
        """
        failing = not (state.tests or {}).get("passed") or state.code_dirty
        if failing and not state.submit_warned and state.test_runs < MAX_TEST_RUNS and not state.over_budget():
            state.submit_warned = True
            return (
                "error: the service tests have not passed on the current files. Run run_service_tests, fix the "
                "failures, then submit (call submit_result again to submit anyway)."
            )
        state.changes = parse_changes(changes_json)
        return box.set(summary, {"changes": state.changes})

    tools = [
        *read_tools,
        write_file,
        edit_file,
        scaffold_from_pattern,
        run_service_tests,
        validate_manifests,
        submit_result,
    ]
    if docker:

        @tool
        def docker_build_check() -> str:
            """Build the microservice image with docker and smoke-test it (read-only, non-root): GET the health,
            readiness and legacy GET /api paths. Returns pass, fail (with build output) or skipped."""
            timeout = state.remaining(600)
            if state.over_budget() or timeout < 30:
                return BUDGET_MSG
            result = checks.docker_build_check(ws, state.params, smoke_paths(ws), timeout=int(timeout))
            if state.closed:
                return CLOSED_MSG
            state.docker = result
            return json.dumps(state.docker)

        tools.append(docker_build_check)
    return tools


def smoke_paths(ws: Workspace) -> list[str]:
    """Legacy GET /api routes with path parameters filled with 1 (seed data exists for id 1)."""
    return sorted(
        {
            re.sub(r"<(?:\w+:)?\w+>", "1", r)
            for r, m in checks.legacy_routes(ws)
            if m == "GET" and r.startswith(checks.API_PREFIX)
        }
    )


def parse_changes(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        value = extract_json(f'{{"changes": {raw}}}') or {}
        value = value.get("changes", [])
    if isinstance(value, dict):
        value = value.get("changes", [value])
    items = value if isinstance(value, list) else []
    return [
        {
            "file": str(c.get("file", "")),
            "change": str(c.get("change", "")),
            "addresses": [str(a) for a in c.get("addresses", []) if a],
        }
        for c in items
        if isinstance(c, dict)
    ]


# ---------------------------------------------------------------- tool output formatting

HELPERS = """Helpers for app/api.py (platform, already written):
  from .db import fetch_all, fetch_one, execute, transaction
    fetch_all(sql, id=1) -> list[dict]; fetch_one(sql, id=1) -> dict | None; execute(sql, id=1) -> rowcount
    (own transaction; a params dict also works); with transaction() as conn: conn.execute(text(sql), {...})
  from .web import json_body, int_field, float_field, require_admin_token, utc_now_iso
    json_body(*required) -> dict (400 if missing); int_field(data, name, minimum=0, default=None) -> int (400 if bad)
    float_field(data, name, minimum=0.0, default=None) -> float; require_admin_token(header) -> 403 unless it matches
    the ADMIN_TOKEN secret; utc_now_iso() -> same format as legacy datetime.utcnow().isoformat()
  abort(status, "message") returns {"error": "message"} as JSON.
Test fixtures (tests/conftest.py): client, app, engine, seeded (loads app/seed.sql, the legacy seed), admin_token."""


def scaffold_report(ws: Workspace, state: RunState) -> str:
    p, res = state.params, state.scaffold_result or {}
    tables = (
        "; ".join(f"{t.name}({', '.join(c.name for c in t.columns)})" for t in res.get("tables", [])) or "none found"
    )
    report = ws.read("microservice/app/report.py").split("# ---")[1].split("\n", 1)[1].strip()
    lines = [
        (
            f"Scaffolded {len(res.get('written', []))} files for {p.service_name}: port {p.port}, "
            f"probes {p.health_path} + "
            f"{p.readiness_path}, replicas {p.replicas_min}-{p.replicas_max} (HPA cpu {p.cpu_target_pct}%), image "
            f"{p.image}:{p.image_tag} (kind: {p.image}:{p.local_tag}), namespace {p.namespace}, "
            f"report cron '{p.report_schedule}'."
        ),
        f"Legacy schema converted to microservice/app/models.py: {tables}.",
        *(f"Schema note: {n}" for n in res.get("notes", [])),
        (
            f"Report defaults in microservice/app/report.py (compare with the legacy report; edit_file if different):"
            f"\n{report}"
        ),
        "Locked platform files (do not edit): "
        + ", ".join(f.removeprefix("microservice/") for f in LOCKED_FILES if f.startswith("microservice/")),
        HELPERS,
    ]
    if kept := res.get("kept"):
        lines.append(f"Kept existing domain files: {', '.join(kept)}. Apply the reviewer feedback by editing them.")
    if "microservice/app/api.py" not in kept:
        lines.append(
            "NEXT: in ONE turn write microservice/app/api.py and microservice/tests/test_api.py by adapting this "
            "worked example (another legacy app ported to the same pattern) to the legacy code in your prompt. "
            "Everything you need is above: do not re-read files.\n\n" + example_text()
        )
    return "\n".join(lines)


def tests_report(state: RunState) -> str:
    t = state.tests or {}
    parity = t.get("parity") or {}
    if t.get("status") == "skipped":
        return f"SKIPPED: {t.get('summary')}. Continue with validate_manifests and submit_result."
    lines = [f"{'PASS' if t.get('passed') else 'FAIL'}: {t.get('summary')} ({t.get('duration_s', '?')}s)"]
    if not t.get("passed") and t.get("output"):
        lines.append(t["output"][-3000:])
    if "error" in parity:
        lines.append(f"API parity not checked: {parity['error']}")
    elif parity:
        lines.append(
            f"API parity: {len(parity['legacy']) - len(parity['missing'])}/{len(parity['legacy'])} legacy /api "
            f"routes ported." + (f" MISSING: {', '.join(parity['missing'])}" if parity["missing"] else "")
        )
        if parity.get("dropped"):
            lines.append("Not ported by design: " + "; ".join(parity["dropped"]))
    smoke = t.get("smoke") or []
    if broken := [s for s in smoke if s["status"] >= 500]:
        lines.append("Contract smoke (one request per legacy route, built from the legacy code) hit server errors:")
        lines += [f"- {s['request']} -> {s['status']} {s.get('error', '')}" for s in broken]
        lines.append("Fix those routes and add a test for each success case.")
    elif smoke:
        lines.append(f"Contract smoke: {len(smoke)} legacy-style requests, no server errors.")
    if not t.get("passed"):
        lines.append(f"Fix with edit_file, then call run_service_tests again (run {state.test_runs}/{MAX_TEST_RUNS}).")
    return "\n".join(lines)


def manifests_report(m: dict[str, Any]) -> str:
    passed = sum(c["status"] == "pass" for c in m.get("checks", []))
    if m.get("status") == "pass":
        return (
            f"PASS: kustomize {m.get('kustomize')}; {passed} policy checks passed; resources: "
            f"{', '.join(m.get('resources', []))}"
        )
    issues = m.get("errors", []) + [f"{c['id']} {c['resource']}: {c['details']}" for c in m.get("failed", [])]
    return "FAIL:\n- " + "\n- ".join(issues[:15]) + "\nFix the k8s/ files with edit_file and validate again."
