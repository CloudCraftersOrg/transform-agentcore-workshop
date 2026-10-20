"""Headless pipeline runner for rehearsals and pre-recording.

    uv run python -m transform_agents.orchestrator \\
        --requirements samples/requirements/migration-requirements.md --mode local --auto-approve

Without --auto-approve it stops at every gate: [a]pprove, [r]evise with feedback, or [q]uit.

A rehearsal (and so the recording `make record` copies) can include scripted revisions, the presenter's
"Ask for changes": after the stage's first attempt it is revised with that feedback, then the gate follows.

    uv run python -m transform_agents.orchestrator ... --auto-approve \\
        --revise requirements="Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."

In replay mode the recording's own revisions are replayed unless --revise is given.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import fields
from pathlib import Path

from transform_agents.core import STAGES

from .clients import make_client, parse_speed
from .pipeline import AWAITING, FAILED, Options, Pipeline


class EventPrinter:
    """Prints a compact, one-line-per-event log; streamed text is collapsed into short lines."""

    def __init__(self, width: int = 160):
        self.width, self.text = width, ""

    def line(self, icon: str, message: str) -> None:
        message = " ".join(str(message).split())
        print(f"  {icon} {message[: self.width]}{'…' if len(message) > self.width else ''}", flush=True)

    def flush_text(self) -> None:
        for chunk in self.text.strip().splitlines():
            if chunk.strip():
                self.line("💬", chunk)
        self.text = ""

    def __call__(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "text":
            self.text += event.get("delta", "")
            return
        self.flush_text()
        if kind == "status":
            model = f" ({event['model']})" if event.get("model") else ""
            self.line("▶", f"{event.get('message', '')}{model}")
        elif kind == "tool_start":
            self.line("🔧", f"{event.get('name')} {event.get('input', '')}")
        elif kind == "tool_end":
            icon = "✓" if event.get("status", "success") == "success" else "✗"
            self.line(icon, f"{event.get('name')} → {event.get('output', '')}")
        elif kind == "artifact":
            self.line("📄", f"{event.get('path')} ({event.get('bytes', 0)} bytes)")
        elif kind == "result":
            m = event.get("metrics") or {}
            self.line("✅", f"{event.get('summary', '')}")
            self.line("📊", f"{m.get('model', '?')} · {m.get('latency_s', '?')}s · {m.get('input_tokens', 0)} in / "
                            f"{m.get('output_tokens', 0)} out tokens · {len(event.get('artifacts') or {})} artifacts")
        elif kind == "error":
            self.line("❌", event.get("message", ""))
        else:
            self.line("·", json.dumps(event))


def speed_arg(text: str) -> float:
    try:
        return parse_speed(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parse_revision(spec: str) -> tuple[str, str]:
    """`--revise STAGE=FEEDBACK` -> (stage, feedback)."""
    stage, _, text = spec.partition("=")
    stage, text = stage.strip(), text.strip()
    if stage not in STAGES or not text:
        raise argparse.ArgumentTypeError(f"expected STAGE=FEEDBACK with STAGE one of {', '.join(STAGES)}, got {spec!r}")
    return stage, text


async def run_revision(pipeline: Pipeline, printer: EventPrinter, stage: str, text: str) -> bool:
    """Revise a reviewed stage with feedback, printing its events; False when the revision failed."""
    print(f"\n━━ revising {stage}: {text}", flush=True)
    async for event in pipeline.revise(stage, text):
        printer(event)
    printer.flush_text()
    return not pipeline.revision_failed(stage)


async def drive(pipeline: Pipeline, auto_approve: bool, until: str | None,
                revisions: dict[str, list[str]] | None = None) -> int:
    """Run the remaining stages. revisions: scripted feedback per stage, applied after its attempt, before the gate."""
    printer = EventPrinter()
    scripted = {stage: list(texts) for stage, texts in (revisions or {}).items()}
    while (stage := pipeline.next_stage()) is not None:
        if until and STAGES.index(stage) > STAGES.index(until):
            break
        feedback = pipeline.retry_feedback(stage) if pipeline.status(stage) == FAILED else None
        if pipeline.status(stage) != AWAITING:
            print(f"\n━━ [{STAGES.index(stage) + 1}/{len(STAGES)}] {stage}", flush=True)
            async for event in pipeline.run_stage(stage, feedback):
                printer(event)
            printer.flush_text()
        if pipeline.status(stage) == FAILED:
            print(f"✗ {stage} failed: {pipeline.state.stages[stage].error}")
            return 1
        if pipeline.revision_failed(stage):
            print(f"  ⚠️ {pipeline.state.stages[stage].error}; the previous result is kept for review")
        for text in scripted.pop(stage, []):
            if not await run_revision(pipeline, printer, stage, text):
                print(f"  ⚠️ {pipeline.state.stages[stage].error}; the previous result is kept for review")
        if auto_approve:
            pipeline.approve(stage)
            print(f"  ✅ approved {stage}")
            continue
        answer = (await asyncio.to_thread(input, f"  gate {stage}: [a]pprove, [r]evise <feedback>, [q]uit > ")).strip()
        if answer.startswith("q"):
            return 0
        if answer.startswith("r"):
            text = answer[1:].strip() or (await asyncio.to_thread(input, "  feedback > ")).strip()
            if not await run_revision(pipeline, printer, stage, text):
                print(f"  ⚠️ {pipeline.state.stages[stage].error}: [a]pprove keeps the previous result")
            continue
        pipeline.approve(stage)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m transform_agents.orchestrator", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requirements", type=Path, help="requirements file uploaded to the requirements stage")
    parser.add_argument("--mode", default="local", choices=["local", "agentcore", "replay", "fake"])
    parser.add_argument("--auto-approve", action="store_true", help="approve every gate automatically")
    parser.add_argument("--resume", metavar="RUN_ID", help="continue an existing run from runs/<RUN_ID>")
    parser.add_argument("--replay-from", default="recorded",
                        help="replay mode: 'recorded' (samples/recorded-run, the default), 'latest' (newest complete "
                             "run in runs/), 'fixtures' or a run directory")
    parser.add_argument("--replay-speed", type=speed_arg, metavar="X",
                        help="replay mode: 1 = the recorded timings (default: $REPLAY_SPEED or 1), 4 = four times faster")
    parser.add_argument("--revise", action="append", type=parse_revision, default=[], metavar="STAGE=FEEDBACK",
                        help="after STAGE's first attempt, revise it with FEEDBACK before its gate (repeatable). "
                             "Replay mode replays the recording's revisions when no --revise is given")
    parser.add_argument("--until", choices=STAGES, help="stop after this stage")
    # Flags default to None so --resume only overrides the options given explicitly (--no-<flag> turns one off).
    flag = argparse.BooleanOptionalAction
    parser.add_argument("--live-aws", action=flag, default=None, help="discovery: describe the EC2 instance + CloudWatch")
    parser.add_argument("--live-deploy", action=flag, default=None, help="k8s_validation: build and deploy to kind")
    parser.add_argument("--live-pricing", action=flag, default=None, help="finops: AWS Pricing API")
    parser.add_argument("--simulate-drift", action=flag, default=None,
                        help="k8s_validation: inject typical hand-edit violations so the agent shows detect & fix")
    args = parser.parse_args(argv)

    client = make_client(args.mode, replay_from=args.replay_from, replay_speed=args.replay_speed)
    revisions: dict[str, list[str]] = {}
    for stage, text in args.revise:
        revisions.setdefault(stage, []).append(text)
    if args.mode == "replay" and not args.revise:
        revisions = client.revisions()
    given = {f.name: getattr(args, f.name) for f in fields(Options) if getattr(args, f.name, None) is not None}
    if args.resume:
        pipeline = Pipeline.load(args.resume, client)
        if given:
            for name, value in given.items():
                setattr(pipeline.state.options, name, value)
            pipeline.save()
    else:
        pipeline = Pipeline.create(client, options=Options(**given))
    if args.requirements:
        pipeline.set_requirements(args.requirements.name, args.requirements.read_text())
    source = f" · replaying {getattr(client, 'source', None) or 'tests/fixtures'}" if args.mode == "replay" else ""
    print(f"run {pipeline.run_id} · mode {args.mode}{source} · {pipeline.dir}", flush=True)
    try:
        code = asyncio.run(drive(pipeline, args.auto_approve, args.until, revisions))
    except ValueError as exc:  # e.g. the requirements stage without --requirements
        print(f"✗ {exc}")
        code = 2
    print(f"\nrun {pipeline.run_id}: " + ", ".join(f"{s}={pipeline.status(s)}" for s in STAGES))
    print(f"workspace: {pipeline.dir / 'workspace'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
