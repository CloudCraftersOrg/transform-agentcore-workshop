"""How the orchestrator reaches an agent. Every client streams the same contract events for a stage request.

    LocalClient      imports transform_agents.<stage>.agent and calls handle() in-process
    AgentCoreClient  InvokeAgentRuntime on Amazon Bedrock AgentCore Runtime, parsing the SSE stream
    ReplayClient     replays a recorded run (samples/recorded-run, runs/<id>) or tests/fixtures: the offline fallback
    FakeClient       deterministic demo events, for tests and UI development
"""

import asyncio
import importlib
import itertools
import json
import math
import os
import re
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from transform_agents.core import STAGES, AgentRequest, error, preview, status
from transform_agents.core.config import region

from . import paths
from .fakes import fake_result
from .paths import FIXTURES_DIR, runs_dir


class AgentClient(Protocol):
    """Anything that can stream contract events for one stage request."""

    name: str

    def stream(self, stage: str, request: AgentRequest) -> AsyncIterator[dict]: ...


# --------------------------------------------------------------------------- local (in-process)


def parse_overrides(spec: str) -> dict[str, str]:
    """STAGE_OVERRIDE="hello" maps every stage to the hello agent; "discovery=hello,finops=hello" maps some."""
    spec = spec.strip()
    if not spec:
        return {}
    if "=" not in spec:
        return {stage: spec for stage in STAGES}
    pairs = (item.split("=", 1) for item in spec.split(",") if "=" in item)
    return {stage.strip(): module.strip() for stage, module in pairs}


class LocalClient:
    """Runs each agent in this process: `transform_agents.<stage>.agent.handle(request)`."""

    name = "local"

    def __init__(self, overrides: dict[str, str] | None = None):
        self.modules = {**parse_overrides(os.getenv("STAGE_OVERRIDE", "")), **(overrides or {})}

    def module_for(self, stage: str) -> str:
        return self.modules.get(stage, stage)

    async def stream(self, stage: str, request: AgentRequest) -> AsyncIterator[dict]:
        module_name = f"transform_agents.{self.module_for(stage)}.agent"
        try:
            handle = importlib.import_module(module_name).handle
        except (ImportError, AttributeError) as exc:
            yield error(stage, f"cannot load {module_name}: {exc}. Tip: STAGE_OVERRIDE={stage}=hello runs the reference agent.")
            return
        try:
            async for event in handle(request):
                yield event
        except Exception as exc:  # noqa: BLE001 - an agent crash becomes an error event, never a dead UI
            yield error(stage, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- AgentCore Runtime


def load_arns(path: Path | None = None) -> dict[str, str]:
    """Runtime ARNs per stage: deploy/agent-arns.json ({stage: arn}), overridden by AGENTCORE_ARN_<STAGE>."""
    path = path or paths.AGENT_ARNS_FILE
    try:
        arns = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):  # half-written by `make deploy-agents`: env overrides still apply
        arns = {}
    arns = arns if isinstance(arns, dict) else {}
    for stage in STAGES:
        if env := os.getenv(f"AGENTCORE_ARN_{stage.upper()}"):
            arns[stage] = env
    return arns


def normalize_event(value: Any, stage: str) -> dict:
    """Coerce whatever the runtime sent into a contract event."""
    for _ in range(2):  # raw JSON text, possibly double-encoded
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"type": "text", "delta": value}
    if isinstance(value, dict) and "type" in value:
        return value
    if isinstance(value, dict) and "error" in value:  # BedrockAgentCoreApp's own error envelope
        return error(stage, f"{value.get('error_type', 'Error')}: {value['error']}")
    return {"type": "text", "delta": json.dumps(value)}


def parse_sse_line(line: bytes | str, stage: str) -> dict | None:
    """`data: {...}` -> event; comments, blank lines and other SSE fields are ignored."""
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    if not text.startswith("data:"):
        return None
    body = text[5:].strip()
    return normalize_event(body, stage) if body else None


async def iterate_in_thread[T](make_iter: Callable[[], Iterator[T]]) -> AsyncIterator[T]:
    """Run a blocking iterator (boto3 streaming body) in a worker thread and yield its items asynchronously."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def put(kind: str, item: Any = None) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, item))
        except RuntimeError:  # the consumer's loop is gone; nothing left to deliver to
            pass

    def worker() -> None:
        try:
            for item in make_iter():
                put("item", item)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the consumer
            put("error", exc)
        finally:
            put("done")

    threading.Thread(target=worker, daemon=True, name="blocking-stream").start()
    while True:
        kind, item = await queue.get()
        if kind == "item":
            yield item
        elif kind == "error":
            raise item
        else:
            return


class AgentCoreClient:
    """Invokes each agent on Amazon Bedrock AgentCore Runtime and streams its SSE events."""

    name = "agentcore"

    def __init__(self, arns: dict[str, str] | None = None, boto_client: Any = None, region_name: str | None = None):
        self._arns = arns  # None: read deploy/agent-arns.json on every invoke, so a redeploy is picked up live
        self.region = region_name or region()
        self._client = boto_client

    @property
    def arns(self) -> dict[str, str]:
        return self._arns if self._arns is not None else load_arns()

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            config = Config(read_timeout=900, connect_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
            self._client = boto3.client("bedrock-agentcore", region_name=self.region, config=config)
        return self._client

    async def stream(self, stage: str, request: AgentRequest) -> AsyncIterator[dict]:
        arn = self.arns.get(stage)
        if not arn:
            yield error(stage, f"no AgentCore Runtime ARN for {stage}: run `make deploy-agents` or set "
                               f"AGENTCORE_ARN_{stage.upper()}")
            return
        session_id = f"{stage}-{uuid.uuid4()}"  # InvokeAgentRuntime needs >= 33 chars
        yield status(stage, f"invoking AgentCore Runtime {arn.rsplit('/', 1)[-1]}", session_id=session_id)
        try:
            async for event in iterate_in_thread(lambda: self._invoke(stage, arn, session_id, request)):
                yield event
        except Exception as exc:  # noqa: BLE001 - throttling, expired credentials, runtime errors
            yield error(stage, f"AgentCore invoke failed: {type(exc).__name__}: {exc}")

    def _invoke(self, stage: str, arn: str, session_id: str, request: AgentRequest) -> Iterator[dict]:
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            payload=json.dumps(request.to_payload()).encode(),
            qualifier="DEFAULT",
            contentType="application/json",
            accept="text/event-stream, application/json",
        )
        body = response["response"]
        if "text/event-stream" in response.get("contentType", ""):
            for line in body.iter_lines():
                if (event := parse_sse_line(line, stage)) is not None:
                    yield event
            return
        payload = json.loads(body.read() or b"{}")
        for value in payload if isinstance(payload, list) else [payload]:
            yield normalize_event(value, stage)


# --------------------------------------------------------------------------- replay and fake

# Replay pacing, in seconds at REPLAY_SPEED=1. A stage replays over its recorded duration (metrics.latency_s): tool
# calls, artifacts and the result come in quick bursts as they do live, narration streams word by word, and the rest
# of the time goes to the model turns (thinking, and writing the tool inputs), as in a real run.
PACE_STATUS = 0.3  # a status after the first event (an agent's own steps, e.g. k8s_validation's preflight)
PACE_SAME_TURN = 0.08  # tool calls requested (or finishing) together in one model turn
PACE_TOOL = 0.4  # a tool running: tool_start -> tool_end
PACE_ARTIFACT = 0.03
PACE_RESULT = 0.25
STREAM_CHARS_PER_S = 250  # narration streaming rate
FIRST_TURN_WEIGHT = 0.5  # the first model turn gets half a share, so tool activity starts early
FINAL_TURN_WEIGHT = 0.5  # the closing turn after the last tool call when no narration was recorded
INPUT_CHARS_PER_TURN = 1000  # writing a tool input this long takes the model about one turn's share...
MAX_INPUT_WEIGHT = 3.0  # ...up to three shares, so one big file never eats the whole stage
MIN_THINK_S = 0.3  # per share; below that a short stage is compressed as a whole
FALLBACK_THINK_S = 1.0  # per share, for a recording without metrics.latency_s


def parse_speed(value: Any) -> float:
    """A replay speed: a finite number > 0 (1 = the recorded timings, 4 = four times faster)."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        speed = 0.0
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError(f"replay speed must be a number > 0, got {value!r}")
    return speed


def env_replay_speed() -> float:
    """REPLAY_SPEED, or 1 (real time) when unset or invalid: a typo must never break the stage fallback."""
    try:
        return parse_speed(os.getenv("REPLAY_SPEED") or 1)
    except ValueError:
        return 1.0


def stream_pieces(events: list[dict]) -> list[dict]:
    """Drop empty text deltas and split the others into words, so recorded narration streams smoothly."""
    out = []
    for event in events:
        if event.get("type") != "text":
            out.append(event)
        elif delta := event.get("delta") or "":
            out += [{**event, "delta": word} for word in re.findall(r"\s*\S+\s*", delta) or [delta]]
    return out


def input_chars(event: dict) -> int:
    """Size of a tool call's input before the timeline preview truncated it ("... (+3716 chars)")."""
    text = str(event.get("input") or "")
    return len(text) + sum(int(n) for n in re.findall(r"\(\+(\d+) chars\)", text))


def pace(events: list[dict], speed: float = 1.0) -> list[tuple[float, dict]]:
    """(seconds to wait, event) pairs that replay a recorded attempt over its recorded duration, divided by speed.

    Events stamped with `t` (seconds since the attempt started) keep those timings. The pipeline does not stamp them
    today, so the attempt is spread over the result's metrics.latency_s: fixed gaps for the mechanical events (tool
    runs, parallel calls, artifacts, streamed words) and the remaining time shared by the model turns, the gaps
    before a turn's narration or tool calls, weighted by the size of the tool inputs the model had to write.
    """
    if events and all(isinstance(e.get("t"), int | float) for e in events):
        offsets = [0.0] + [float(e["t"]) for e in events]
        return [(max(offsets[i + 1] - offsets[i], 0.0) / speed, e) for i, e in enumerate(events)]

    events = stream_pieces(events)
    # Model turns start after the agent's own "agent started" status (it names the model); before it, tool events
    # are deterministic steps (k8s_validation's preflight). A recording without one is all model.
    agent = not any(e.get("type") == "status" and e.get("model") for e in events)
    fixed: list[float] = []
    shares: list[float] = []  # how much of the model time comes right before each event
    prev: dict = {}
    first_turn = True
    for i, event in enumerate(events):
        kind, before = event.get("type"), prev.get("type")
        gap = len(prev.get("delta") or "") / STREAM_CHARS_PER_S if before == "text" else 0.0
        starts_turn, writes = False, 0
        if kind == "status":
            gap += PACE_STATUS
            agent = agent or bool(event.get("model"))
        elif kind == "text":
            starts_turn = before != "text"
        elif kind == "tool_start" and before == "tool_start":
            gap += PACE_SAME_TURN  # the same message asked for several tools at once
        elif kind == "tool_start" and not agent:
            gap += PACE_TOOL
        elif kind == "tool_start":
            starts_turn = before != "text"  # after narration, the same turn goes on to write the tool inputs
            group = list(itertools.takewhile(lambda e: e.get("type") == "tool_start", events[i:]))
            writes = sum(input_chars(e) for e in group)
        elif kind == "tool_end":
            gap += PACE_SAME_TURN if before == "tool_end" else PACE_TOOL
        else:  # artifact, result, error
            gap += PACE_ARTIFACT if kind == "artifact" else PACE_RESULT
        share = min(writes / INPUT_CHARS_PER_TURN, MAX_INPUT_WEIGHT)
        if starts_turn:
            share += FIRST_TURN_WEIGHT if first_turn else 1.0
            first_turn = False
        elif kind in ("artifact", "result") and before == "tool_end" and agent:
            share += FINAL_TURN_WEIGHT
        fixed.append(gap if prev else 0.0)
        shares.append(share)
        prev = event

    latency = next((e.get("metrics") or {} for e in reversed(events) if e.get("type") == "result"), {}).get("latency_s")
    total_fixed, weight = sum(fixed), sum(shares)
    target = float(latency) if isinstance(latency, int | float) and latency > 0 else total_fixed + FALLBACK_THINK_S * weight
    think = (target - total_fixed) / weight if weight else 0.0
    scale = 1.0
    if weight and think < MIN_THINK_S:  # more narration and tool calls than time: compress it all evenly
        scale = target / (total_fixed + MIN_THINK_S * weight)
        think = MIN_THINK_S * scale
    elif not weight and total_fixed:
        scale = target / total_fixed
    return [((f * scale + w * think) / speed, e) for f, w, e in zip(fixed, shares, events, strict=True)]


def synthesize_events(stage: str, result: dict) -> list[dict]:
    """Plausible event stream for a bare `result` (fixtures and fakes): status, narration, tool calls, artifacts."""
    metrics = result.get("metrics") or {}
    events: list[dict] = [status(stage, "agent started", model=metrics.get("model", "replay"))]
    words = (result.get("summary") or "").split(" ")
    events += [{"type": "text", "delta": word + " "} for word in words if word]
    for i, (path, content) in enumerate((result.get("artifacts") or {}).items()):
        tool_id = f"replay-{i}"
        events.append({"type": "tool_start", "id": tool_id, "name": "write_file",
                       "input": json.dumps({"path": path, "content": preview(content, 60)})})
        events.append({"type": "tool_end", "id": tool_id, "name": "write_file", "status": "success",
                       "output": f"wrote {path} ({len(content)} bytes)"})
    events += [{"type": "artifact", "path": p, "bytes": len(c)} for p, c in (result.get("artifacts") or {}).items()]
    return [*events, {**result, "type": "result", "stage": stage}]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def recorded_runs(root: Path | None = None) -> list[Path]:
    """Run directories that hold at least one recorded stage, newest first."""
    root = root or runs_dir()
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir() if d.is_dir() and any(d.glob("*/events.jsonl"))), reverse=True)


REPLAYABLE_CLIENTS = {"local", "agentcore"}  # real agents; replay and fake runs are not worth replaying


def is_complete_run(run_dir: Path) -> bool:
    """All six stages approved, recorded by real agents (local or AgentCore), with every stage's events on disk."""
    try:
        state = json.loads((run_dir / "state.json").read_text())
    except (OSError, ValueError):
        return False
    stages = {s: (state.get("stages") or {}).get(s) or {} for s in STAGES}
    return (all(st.get("status") == "approved" for st in stages.values())
            and all((st.get("client") or state.get("client")) in REPLAYABLE_CLIENTS for st in stages.values())
            and all((run_dir / s / "events.jsonl").exists() for s in STAGES))


def complete_runs(root: Path | None = None) -> list[Path]:
    """Complete runs (see is_complete_run), newest first."""
    return [d for d in recorded_runs(root) if is_complete_run(d)]


def resolve_replay_source(spec: Path | str | None) -> Path | None:
    """What a replay plays back. spec: "recorded" (samples/recorded-run), "latest" (the newest complete run in
    runs/), "fixtures"/None (tests/fixtures only) or a run directory. "recorded" falls back to "latest", and
    "latest" to the fixtures, so a replay always has something to play."""
    if spec in (None, "", "fixtures"):
        return None
    if spec == "recorded":
        if (paths.RECORDED_RUN / "state.json").exists():
            return paths.RECORDED_RUN
        spec = "latest"
    if spec == "latest":
        return next(iter(complete_runs()), None)
    return Path(spec)


def recorded_attempts(stage_dir: Path) -> list[tuple[int, Path]]:
    """(attempt, events file) of every recorded attempt of a stage, oldest first; the latest is <stage>/events.jsonl."""
    history = stage_dir / "history"
    files = {int(m[1]): path for path in (history.glob("attempt-*.events.jsonl") if history.is_dir() else [])
             if (m := re.fullmatch(r"attempt-(\d+)\.events\.jsonl", path.name))}
    attempts = sorted(files.items())
    if (stage_dir / "events.jsonl").exists():
        attempts.append((max(files, default=0) + 1, stage_dir / "events.jsonl"))
    return attempts


def successful_recorded_attempts(stage_dir: Path) -> list[tuple[int, Path]]:
    """Recorded attempts that ended with a `result` (failed attempts are skipped), oldest first."""
    out = []
    for n, path in recorded_attempts(stage_dir):
        try:
            if any(event.get("type") == "result" for event in read_jsonl(path)):
                out.append((n, path))
        except (OSError, ValueError):
            continue
    return out


def successful_attempts(stage_dir: Path) -> list[Path]:
    """Events files of the recorded attempts that ended with a `result`, oldest first."""
    return [path for _, path in successful_recorded_attempts(stage_dir)]


class ReplayClient:
    """Replays a previous run's recorded events, falling back to tests/fixtures/<stage>_result.json.

    The pipeline passes its attempt number (attempt_aware): attempt n replays the n-th recorded attempt that
    produced a result, so the first run of a stage replays the recorded first attempt and "Ask for changes" the
    recorded revision; failed attempts are skipped. Past the recorded attempts it replays the last good one.
    Called without an attempt, it counts the calls per stage.

    The stream looks like a live run: no replay status, and each attempt takes its recorded duration divided by
    `speed` (REPLAY_SPEED, default 1 = real time). `played` keeps what was replayed per stage, for logs and tests.
    """

    name = "replay"
    attempt_aware = True

    def __init__(self, source: Path | str | None = None, fixtures_dir: Path = FIXTURES_DIR, speed: float | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.source = Path(source) if source else None
        self.fixtures_dir = fixtures_dir
        self.speed = parse_speed(speed) if speed is not None else env_replay_speed()
        self.sleep = sleep
        self.calls: Counter[str] = Counter()
        self.played: dict[str, str] = {}

    def recording(self, stage: str, attempt: int) -> tuple[str, list[dict]] | None:
        if self.source:
            if good := successful_attempts(self.source / stage):
                index = min(max(attempt, 1), len(good)) - 1
                return f"{self.source.name}/{stage} attempt {index + 1}/{len(good)}", read_jsonl(good[index])
        fixture = self.fixtures_dir / f"{stage}_result.json"
        if fixture.exists():
            return f"fixture {fixture.name}", synthesize_events(stage, json.loads(fixture.read_text()))
        return None

    def revisions(self) -> dict[str, list[str]]:
        """Feedback of every recorded revision that produced a result, per stage: what `make replay` re-enacts."""
        try:
            state = json.loads((self.source / "state.json").read_text()) if self.source else {}
        except (OSError, ValueError):
            return {}
        out: dict[str, list[str]] = {}
        for stage in STAGES:
            good = {n for n, _ in successful_recorded_attempts(self.source / stage)} if self.source else set()
            feedback = sorted(((state.get("stages") or {}).get(stage) or {}).get("feedback") or [],
                              key=lambda f: f.get("attempt", 0))
            if texts := [f["text"] for f in feedback if f.get("attempt") in good and f.get("text")]:
                out[stage] = texts
        return out

    async def stream(self, stage: str, request: AgentRequest, attempt: int | None = None) -> AsyncIterator[dict]:
        self.calls[stage] += 1
        found = self.recording(stage, attempt or self.calls[stage])
        if found is None:
            yield error(stage, f"nothing to replay for {stage}: no recorded run and no tests/fixtures/{stage}_result.json")
            return
        self.played[stage], events = found
        for delay, event in pace(events, self.speed):
            await self.sleep(delay)
            yield event


class FakeClient:
    """Deterministic offline agent: realistic events and results for every stage (tests, UI development)."""

    name = "fake"

    def __init__(self, delay: float = 0.0, fail: set[str] | None = None):
        self.delay = delay
        self.fail = fail or set()
        self.calls: list[tuple[str, AgentRequest]] = []

    async def stream(self, stage: str, request: AgentRequest) -> AsyncIterator[dict]:
        self.calls.append((stage, request))
        if stage in self.fail:
            yield status(stage, "agent started", model="fake")
            yield error(stage, "ThrottlingException: simulated failure")
            return
        for event in synthesize_events(stage, fake_result(stage, request)):
            await asyncio.sleep(self.delay)
            yield event


def make_client(mode: str, *, replay_from: Path | str | None = None, replay_speed: float | None = None) -> AgentClient:
    """Client factory used by the CLI and the UI: local | agentcore | replay | fake.

    replay_from: "recorded", "latest", "fixtures"/None or a run directory (see resolve_replay_source).
    replay_speed: replay pacing, 1 = the recorded timings; None reads REPLAY_SPEED.
    """
    if mode == "local":
        return LocalClient()
    if mode == "agentcore":
        return AgentCoreClient()
    if mode == "replay":
        return ReplayClient(resolve_replay_source(replay_from), speed=replay_speed)
    if mode == "fake":
        return FakeClient(delay=float(os.getenv("FAKE_DELAY", "0.02")))
    raise ValueError(f"unknown mode {mode!r}; expected local, agentcore, replay or fake")
