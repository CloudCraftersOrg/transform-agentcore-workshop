"""Well-known repository paths used by the orchestrator, the CLI and the UI."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_DIR = ROOT / "legacy-infra"
FIXTURES_DIR = ROOT / "tests" / "fixtures"
SAMPLE_REQUIREMENTS = ROOT / "samples" / "requirements" / "migration-requirements.md"
AGENT_ARNS_FILE = ROOT / "deploy" / "agent-arns.json"
RECORDED_RUN = ROOT / "samples" / "recorded-run"  # tracked copy of a complete run: `make record RUN=runs/<id>`


def runs_dir() -> Path:
    """Where runs are persisted; override with RUNS_DIR (tests, rehearsals)."""
    return Path(os.getenv("RUNS_DIR") or ROOT / "runs")
