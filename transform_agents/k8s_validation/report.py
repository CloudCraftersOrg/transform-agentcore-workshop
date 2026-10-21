"""Builds the contract `data` (CONTRACTS.md §5) and reports/validation-report.md from deterministic check results.

The LLM contributes the summary, the fix descriptions and notes; every status, score and table row comes from the
tools, so the report can't claim a check passed when it didn't.
"""

import difflib
from datetime import UTC, datetime
from typing import Any

import yaml

from .policy import RULES, SEVERITY_ORDER
from .tools import CREATES_RESOLVE, Validation, ValidationSession

REPORT_PATH = "reports/validation-report.md"
PENALTY = {"critical": 25, "high": 10, "medium": 5, "low": 2}


def build_checks(v: Validation) -> list[dict]:
    checks: list[dict] = []
    for name, tc in v.targets.items():
        r, s = tc.rendered, tc.schema
        checks.append({"id": "render", "tool": r.tool, "status": r.status, "target": name, "resource": name,
                       "details": r.details})
        checks.append({"id": "schema", "tool": s.tool, "status": s.status, "target": name, "resource": name,
                       "details": s.details})
        checks += [{"id": "schema", "tool": s.tool, "status": "fail", "target": name, "resource": ref,
                    "details": msg[:300]} for ref, msg in s.invalid]
    groups = v.grouped()
    for f, targets in groups:
        checks.append({"id": f.id, "tool": "policy", "title": f.rule.title, "severity": f.severity, "status": f.status,
                       "target": ", ".join(targets), "resource": f.resource + (f" ({f.container})" if f.container else ""),
                       "details": f.message})
    flagged = {f.id for f, _ in groups}
    checked_in = ", ".join(v.targets)
    for rule in RULES.values():
        if rule.id not in flagged:
            checks.append({"id": rule.id, "tool": "policy", "title": rule.title, "severity": rule.severity,
                           "status": "pass", "target": checked_in, "resource": "all", "details": "ok"})
    for ref in sorted({w for tc in v.targets.values() for w in tc.exempt}):
        checks.append({"id": "exempt", "tool": "policy", "status": "skipped", "target": checked_in, "resource": ref,
                       "details": "dev-only datastore: baseline not enforced"})
    return checks


def build_fixes(session: ValidationSession, first: Validation, final: Validation) -> list[dict]:
    """One entry per file that really changed, with the LLM's description and the rules the change resolved."""
    resolved = [(f, t) for f, t in first.grouped() if f.status == "fail" and f.key() not in final.failing_keys()]
    index = session.source_index()
    fixes = []
    for path in sorted(session.ws.files):
        before, after = session.original.get(path), session.ws.files[path]
        if before == after or path.startswith("reports/"):
            continue
        described = list(dict.fromkeys(n["change"] for n in session.fix_notes if n["file"] == path and n["change"]))
        resolves = {f.id for f, t in resolved if path in session.edit_files(f, t, index)}
        if before is None:
            kinds = {d.get("kind") for d in _safe_docs(after)}
            resolves |= {r for k in kinds for r in CREATES_RESOLVE.get(k, []) if any(f.id == r for f, _ in resolved)}
        added, removed = _line_delta(before or "", after)
        auto = f"{'created' if before is None else 'modified'} (+{added}/-{removed} lines)"
        fixes.append({"file": path, "change": "; ".join(described) or auto, "resolves": sorted(resolves)})
    earlier = [f for f in session.previous_fixes if isinstance(f, dict) and f not in fixes]  # revision: keep history
    return earlier + fixes


def _safe_docs(text: str) -> list[dict]:
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def _line_delta(before: str, after: str) -> tuple[int, int]:
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    return sum(d.startswith("+ ") for d in diff), sum(d.startswith("- ") for d in diff)


def score(checks: list[dict], live: dict) -> int:
    points = 100
    for c in checks:
        if c["status"] == "fail":
            points -= {"render": 30, "schema": 20}.get(c["id"], PENALTY.get(c.get("severity", "high"), 10))
        elif c["status"] == "warn":
            points -= 2
    if live.get("status") == "fail":
        points -= 20
    return max(points, 0)


def skipped_deploy(session: ValidationSession, reason: str) -> dict:
    return {"status": "skipped", "cluster": f"kind-{session.cluster}", "image": "", "rollout": "", "smoke_tests": [],
            "reason": reason}


def build_data(session: ValidationSession, final: Validation) -> dict[str, Any]:
    first = session.first or final
    checks = build_checks(final)
    live = session.deploy_result or skipped_deploy(
        session, "options.live_deploy is false" if not session.live_deploy else "deploy was not run")
    if session.deploy_stale:
        live = {**live, "note": "manifests changed after this deploy"}
    return {
        "passed": not any(c["status"] == "fail" for c in checks) and live.get("status") != "fail",
        "score": score(checks, live),
        "checks": checks,
        "fixes_applied": build_fixes(session, first, final),
        "live_deploy": live,
        "targets": list(final.targets),
        "policy_before": sorted(first.failing_rules()),
        "policy_after": sorted(final.failing_rules()),
        "notes": session.notes,
        "simulated_drift": bool(session.drift),
        "drift_injected": [{**d, "fixed": d["rule"] not in final.failing_rules()} for d in session.drift],
    }


def _cell(value: Any) -> str:
    return str(value if value not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return "\n".join(lines + ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows])


STATUS_ICON = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skipped": "SKIP"}


def render_report(session: ValidationSession, data: dict, summary: str, model: str = "", run_id: str = "") -> str:
    service = next(iter(sorted(session.app_names)), "the microservice")
    verdict = ("PASSED" if data["passed"] else "FAILED") + (" (SIMULATED drift run)" if data.get("drift_injected")
                                                            else "")
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Kubernetes validation report: {service}", "",
           (f"**Result: {verdict}** · score {data['score']}/100 · targets {', '.join(data['targets']) or '-'} · "
           f"run `{run_id or '-'}` · model `{model or '-'}` · {stamp}"), "", "## Summary", "", summary.strip(), ""]
    drift = data.get("drift_injected") or []
    if drift:
        fixed = sum(d["fixed"] for d in drift)
        out += ["## SIMULATED drift (demo)", "",
                (f"> **SIMULATED drift:** `options.simulate_drift` injected {len(drift)} typical hand-edits into "
                 "k8s/base on purpose, before the first check and without the model, to show the detect-and-fix "
                 "loop. They are not in the modernization output. "
                 f"The agent fixed {fixed}/{len(drift)}."), "",
                _table(["File", "Injected change (SIMULATED)", "Rule", "Fixed"],
                       [[d["file"], d["change"], d["rule"], "yes" if d["fixed"] else "NO"] for d in drift]), ""]

    before, after = data["policy_before"], data["policy_after"]
    out += ["## Checks", "",
            (f"Policy baseline (TC5/SEC4): {len(before)} failing rule(s) before fixes "
            f"({', '.join(before) or 'none'}), {len(after)} after ({', '.join(after) or 'none'})."), ""]
    order = {"fail": 0, "warn": 1, "skipped": 2, "pass": 3}
    rows = sorted(data["checks"], key=lambda c: (order.get(c["status"], 9), SEVERITY_ORDER.get(c.get("severity"), 9),
                                                 c["id"]))
    out += [_table(["Status", "Check", "Tool", "Severity", "Target", "Resource", "Details"],
                   [[STATUS_ICON.get(c["status"], c["status"]), f"{c['id']} {c.get('title', '')}".strip(), c["tool"],
                     c.get("severity"), c.get("target"), c["resource"], c["details"]] for c in rows]), ""]

    out += ["## Fixes applied", ""]
    fixes = data["fixes_applied"]
    drift_rules = {d["rule"] for d in drift}
    out += [_table(["File", "Change", "Resolves"],
                   [[f["file"], f["change"], ", ".join(f["resolves"]) + (" (SIMULATED drift)" if drift_rules & set(
                       f["resolves"]) else "")] for f in fixes])
            if fixes else "No changes were needed.", ""]

    live = data["live_deploy"]
    out += ["## Live deploy (kind)", "",
            _table(["Field", "Value"], [["Status", STATUS_ICON.get(live["status"], live["status"])],
                                        ["Cluster", live.get("cluster")], ["Namespace", live.get("namespace")],
                                        ["Image", live.get("image")], ["Rollout", live.get("rollout")],
                                        ["Reason", live.get("reason")], ["Note", live.get("note")],
                                        ["Timings (s)", ", ".join(f"{k.removesuffix('_s')} {v}" for k, v in
                                                                  (live.get("timings") or {}).items() if v is not None)]]),
            ""]
    if live.get("smoke_tests"):
        out += [_table(["Smoke test", "Expected", "Got", "Result", "ms"],
                       [[t["request"], t["expected"], t["status"], "pass" if t["passed"] else "FAIL", t.get("ms")]
                        for t in live["smoke_tests"]]), ""]
    if live.get("access"):
        out += [f"The deployment stays up for exploration: `{live['access']}` then `curl localhost:8080/api/items`.", ""]
    if live.get("diagnostics"):
        out += ["```text", live["diagnostics"], "```", ""]
    if data.get("notes"):
        out += ["## Notes", "", data["notes"].strip(), ""]
    return "\n".join(out)
