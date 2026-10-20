"""How the orchestrator reaches an agent. Every client streams the same contract events for a stage request.

    LocalClient      imports transform_agents.<stage>.agent and calls handle() in-process
    AgentCoreClient  InvokeAgentRuntime on Amazon Bedrock AgentCore Runtime, parsing the SSE stream
    ReplayClient     replays a recorded run (samples/recorded-run, runs/<id>) or tests/fixtures: the offline fallback
    FakeClient       deterministic demo events, for tests and UI development
"""

import asyncio
import importlib
import json
import os
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
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

# Seconds to wait before each event type when replaying (divided by REPLAY_SPEED).
REPLAY_DELAYS = {"status": 0.3, "text": 0.01, "tool_start": 0.25, "tool_end": 0.15, "artifact": 0.04, "result": 0.3}


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


def successful_attempts(stage_dir: Path) -> list[Path]:
    """Recorded attempts of one stage that ended with a `result` (failed attempts are skipped), oldest first."""
    history = stage_dir / "history"
    files = sorted(history.glob("attempt-*.events.jsonl"),
                   key=lambda p: int(p.name.split("-", 1)[1].split(".", 1)[0])) if history.is_dir() else []
    if (stage_dir / "events.jsonl").exists():
        files.append(stage_dir / "events.jsonl")
    out = []
    for path in files:
        try:
            if any(event.get("type") == "result" for event in read_jsonl(path)):
                out.append(path)
        except (OSError, ValueError):
            continue
    return out


class ReplayClient:
    """Replays a previous run's recorded events, falling back to tests/fixtures/<stage>_result.json.

    The pipeline passes its attempt number (attempt_aware): attempt n replays the n-th recorded attempt that
    produced a result, so recorded revisions replay too and failed attempts are skipped. Past the recorded
    attempts it replays the last good one. Called without an attempt, it counts the calls per stage.
    """

    name = "replay"
    attempt_aware = True

    def __init__(self, source: Path | str | None = None, fixtures_dir: Path = FIXTURES_DIR, speed: float | None = None):
        self.source = Path(source) if source else None
        self.fixtures_dir = fixtures_dir
        self.speed = speed or float(os.getenv("REPLAY_SPEED", "1"))
        self.calls: Counter[str] = Counter()

    def recording(self, stage: str, attempt: int) -> tuple[str, list[dict]] | None:
        if self.source:
            if good := successful_attempts(self.source / stage):
                path = good[min(max(attempt, 1), len(good)) - 1]
                return f"{self.source.name}/{stage} attempt {good.index(path) + 1}/{len(good)}", read_jsonl(path)
        fixture = self.fixtures_dir / f"{stage}_result.json"
        if fixture.exists():
            return f"fixture {fixture.name}", synthesize_events(stage, json.loads(fixture.read_text()))
        return None

    async def stream(self, stage: str, request: AgentRequest, attempt: int | None = None) -> AsyncIterator[dict]:
        self.calls[stage] += 1
        found = self.recording(stage, attempt or self.calls[stage])
        if found is None:
            yield error(stage, f"nothing to replay for {stage}: no recorded run and no tests/fixtures/{stage}_result.json")
            return
        label, events = found
        yield status(stage, f"replaying {label}", replay=True)
        for event in events:
            await asyncio.sleep(REPLAY_DELAYS.get(event.get("type", ""), 0.05) / self.speed)
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


def make_client(mode: str, *, replay_from: Path | str | None = None) -> AgentClient:
    """Client factory used by the CLI and the UI: local | agentcore | replay | fake.

    replay_from: "recorded", "latest", "fixtures"/None or a run directory (see resolve_replay_source).
    """
    if mode == "local":
        return LocalClient()
    if mode == "agentcore":
        return AgentCoreClient()
    if mode == "replay":
        return ReplayClient(resolve_replay_source(replay_from))
    if mode == "fake":
        return FakeClient(delay=float(os.getenv("FAKE_DELAY", "0.02")))
    raise ValueError(f"unknown mode {mode!r}; expected local, agentcore, replay or fake")
