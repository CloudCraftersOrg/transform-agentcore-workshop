"""Makefile behaviour: clean keeps runs/ unless CLEAN_RUNS=1, AWS_PROFILE is passed through only when set."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def make(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base = {k: v for k, v in os.environ.items() if k not in ("AWS_PROFILE", "MAKEFLAGS", "MFLAGS")}
    proc = subprocess.run(["make", "--no-print-directory", "-C", str(cwd), *args], capture_output=True, text=True,
                          env={**base, **(env or {})}, check=False, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


@pytest.fixture
def sandbox(tmp_path) -> Path:
    """A copy of the Makefile plus a probe target, in a scratch directory."""
    text = (ROOT / "Makefile").read_text()
    probe = '\nprobe:\n\t@echo "profile=[$${AWS_PROFILE-unset}] region=[$$AWS_REGION]"\n'
    (tmp_path / "Makefile").write_text(text + probe)
    return tmp_path


def test_clean_keeps_runs_unless_asked(sandbox):
    (sandbox / "runs" / "r1").mkdir(parents=True)
    (sandbox / ".pytest_cache").mkdir()
    out = make(sandbox, "clean")
    assert (sandbox / "runs" / "r1").is_dir() and not (sandbox / ".pytest_cache").exists()
    assert "CLEAN_RUNS=1" in out
    make(sandbox, "clean", "CLEAN_RUNS=1")
    assert not (sandbox / "runs").exists()


def test_aws_profile_is_exported_only_when_set(sandbox):
    assert "profile=[unset] region=[us-west-2]" in make(sandbox, "probe")
    assert "profile=[mine]" in make(sandbox, "probe", env={"AWS_PROFILE": "mine"})
    assert "profile=[cli]" in make(sandbox, "probe", "AWS_PROFILE=cli")
    assert "region=[eu-west-1]" in make(sandbox, "probe", env={"AWS_REGION": "eu-west-1"})


def test_help_describes_tools_and_record(sandbox):
    out = make(sandbox, "help")
    tools = next(line for line in out.splitlines() if "tools" in line and "Install" in line)
    assert "kind and kubeconform" in tools and "(kind, kubeconform, terraform)" not in tools
    assert "record" in out and "REPLAY_FROM=recorded" in out


def test_record_requires_a_run(sandbox):
    proc = subprocess.run(["make", "-C", str(sandbox), "record"], capture_output=True, text=True, check=False)
    assert proc.returncode != 0 and "usage: make record RUN=runs/<id>" in proc.stdout


def test_replay_speed_is_passed_through(sandbox):
    assert "--replay-speed 1 " in make(sandbox, "-n", "replay")  # real time by default
    assert "--replay-speed 4 " in make(sandbox, "-n", "replay", "REPLAY_SPEED=4")
    assert "--replay-speed 2 " in make(sandbox, "-n", "replay", env={"REPLAY_SPEED": "2"})
    probe = '\nspeed:\n\t@echo "speed=[$$REPLAY_SPEED]"\n'
    (sandbox / "Makefile").write_text((sandbox / "Makefile").read_text() + probe)
    assert "speed=[4]" in make(sandbox, "speed", "REPLAY_SPEED=4")  # `make ui REPLAY_SPEED=4` reaches the UI
