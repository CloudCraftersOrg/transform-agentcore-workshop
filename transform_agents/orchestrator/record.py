"""Copy a finished run into samples/recorded-run, the tracked recording that `make replay` plays back.

    uv run python -m transform_agents.orchestrator.record runs/<id>      (= make record RUN=runs/<id>)

Keeps what a replay needs (state.json, inputs/, <stage>/events.jsonl, result.json, history/) and drops what is
large or volatile: workspace/ (rebuilt from the results), lock and temp files, and any file over 2 MB. Local
paths (the repo root, then the home directory) are replaced by `<repo>` and `~`. Only complete runs (six
approved stages from local or AgentCore agents) are accepted unless --force is given.
"""

import argparse
import shutil
import sys
from pathlib import Path

from .clients import is_complete_run
from .paths import RECORDED_RUN, ROOT

MAX_FILE_BYTES = 2_000_000
SKIP_DIRS = {"workspace", "__pycache__"}
SKIP_NAMES = {".running", ".DS_Store"}


def keep(rel: Path, size: int) -> bool:
    if rel.parts[0] in SKIP_DIRS or rel.name in SKIP_NAMES or rel.name.endswith(".tmp"):
        return False
    return size <= MAX_FILE_BYTES


def scrub(text: str) -> str:
    text = text.replace(str(ROOT), "<repo>")
    return text.replace(str(Path.home()), "~")


def record(source: Path, dest: Path = RECORDED_RUN, force: bool = False) -> list[str]:
    """Replace `dest` with a trimmed copy of the run in `source`; returns the copied relative paths."""
    source = source.resolve()
    if not (source / "state.json").exists():
        raise ValueError(f"{source} is not a run directory (no state.json)")
    if not force and not is_complete_run(source):
        raise ValueError(f"{source.name} is not a complete run (six approved stages from local/agentcore agents); "
                         "use --force to record it anyway")
    staging = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    copied = []
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        rel = path.relative_to(source)
        if not keep(rel, path.stat().st_size):
            continue
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(scrub(path.read_text(encoding="utf-8")), encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(path, target)
        copied.append(rel.as_posix())
    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m transform_agents.orchestrator.record", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path, help="the run directory, e.g. runs/20261020-005135-1852")
    parser.add_argument("--dest", type=Path, default=RECORDED_RUN, help="default: samples/recorded-run")
    parser.add_argument("--force", action="store_true", help="record an incomplete run too")
    args = parser.parse_args(argv)
    try:
        copied = record(args.run, args.dest, args.force)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    size = sum((args.dest / rel).stat().st_size for rel in copied)
    print(f"✅ recorded {args.run} → {args.dest} ({len(copied)} files, {size / 1024:.0f} KB). "
          "Check it with `make replay`, then commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
