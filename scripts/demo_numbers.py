"""Print the numbers the talk quotes, straight from a run: `uv run python scripts/demo_numbers.py [run_dir]`.

Defaults to the tracked recording (samples/recorded-run). Use it after re-recording to update the cheat sheet in
docs/KEYNOTE-SCRIPT.md and the reference timings in WORKSHOP.md (see docs/specs/05-demo-content.md).
"""

import json
import re
import sys
from pathlib import Path

STAGES = ["discovery", "requirements", "modernization", "terraform", "k8s_validation", "finops"]


def load(run: Path, stage: str) -> dict:
    path = run / stage / "result.json"
    return json.loads(path.read_text()) if path.exists() else {}


def first_attempt_latency(run: Path, stage: str) -> float | None:
    attempts = sorted((run / stage / "history").glob("attempt-1*.json")) if (run / stage / "history").exists() else []
    for path in attempts:
        try:
            return json.loads(path.read_text())["metrics"]["latency_s"]
        except (KeyError, ValueError):
            continue
    return None


def main(run: Path) -> None:
    results = {s: load(run, s) for s in STAGES}
    missing = [s for s, r in results.items() if not r]
    if missing:
        sys.exit(f"{run}: no result.json for {', '.join(missing)} (is it a complete run?)")
    d = {s: r.get("data", {}) for s, r in results.items()}
    m = {s: r.get("metrics", {}) for s, r in results.items()}

    findings = d["discovery"].get("findings", [])
    reqs = d["requirements"].get("requirements", [])
    questions = d["requirements"].get("open_questions", [])
    tests_out = (d["modernization"].get("tests") or {}).get("output", "")
    passed = re.findall(r"(\d+) passed", tests_out)
    checks = d["terraform"].get("spec_checks", [])
    drift = d["k8s_validation"].get("drift_injected", [])
    smoke = (d["k8s_validation"].get("live_deploy") or {}).get("smoke_tests", [])
    fin = d["finops"]
    rev_latency = m["requirements"].get("latency_s") if first_attempt_latency(run, "requirements") else None

    rows = [
        ("Discovery", m["discovery"].get("latency_s"),
         f"{len(findings)} findings ({sum(f.get('severity') == 'critical' for f in findings)} critical), "
         f"readiness {d['discovery'].get('readiness_score')}/100, {d['discovery'].get('strategy', {}).get('recommended')}"),
        ("Requirements", first_attempt_latency(run, "requirements") or m["requirements"].get("latency_s"),
         f"{len(reqs)} requirements ({sum(bool(r.get('traces_to')) for r in reqs)} traced), "
         f"budget ${(d['requirements'].get('finops') or {}).get('monthly_budget_usd')}, {len(questions)} open questions, "
         f"{sum(q.get('status') == 'resolved' for q in questions)} resolved"
         + (f" (revision {rev_latency} s)" if rev_latency else "")),
        ("Modernization", m["modernization"].get("latency_s"),
         f"tests {passed[-1] if passed else '?'} passed, {len(results['modernization'].get('artifacts', {}))} files"),
        ("Terraform", m["terraform"].get("latency_s"),
         f"valid={d['terraform'].get('valid')} ({d['terraform'].get('validation_level')}), "
         f"spec checks {sum(bool(c.get('ok', c.get('status') == 'pass')) for c in checks)}/{len(checks)}, "
         f"Kubernetes {d['terraform'].get('kubernetes_version')}"),
        ("K8s Validation", m["k8s_validation"].get("latency_s"),
         f"drift {sum(bool(x.get('fixed')) for x in drift)}/{len(drift)} fixed, "
         f"deploy {(d['k8s_validation'].get('live_deploy') or {}).get('status')}, "
         f"smoke {'/'.join(str(t.get('status')) for t in smoke)}"),
        ("FinOps", m["finops"].get("latency_s"),
         f"${(fin.get('current') or {}).get('monthly_usd')} today -> ${(fin.get('target_as_designed') or {}).get('monthly_usd')}"
         f" as designed -> ${(fin.get('target_optimized') or {}).get('monthly_usd')} optimized, "
         f"within budget={fin.get('within_budget')}"),
    ]
    total = sum(x.get("latency_s") or 0 for x in m.values())
    tokens = sum((x.get("input_tokens") or 0) + (x.get("output_tokens") or 0) for x in m.values())
    print(f"Run: {run}\n")
    print(f"{'Stage':16} {'Time':>8}  Numbers")
    for name, latency, text in rows:
        print(f"{name:16} {(f'{latency:.1f} s' if latency else '-'):>8}  {text}")
    print(f"\nWhole pipeline: {total:.0f} s of machine time, {tokens / 1000:.0f}k tokens")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "samples" / "recorded-run")
