"""Bridges the orchestrator's async event streams into Streamlit's rerun-the-script model.

Streamlit re-executes app.py on every click. If the agent ran inside that script run, any click
would cancel it. Instead each stage attempt is a StageJob: a daemon thread drives the async
generator on its own event loop and appends events to a list. Every rerun re-attaches to it and
renders whatever has arrived.

Jobs and pipelines live in a process-wide RunRegistry (st.cache_resource), not in st.session_state:
a page reload starts a new session, and that session must find the live job (and the same Pipeline
object) instead of reloading the run from disk while an agent is still writing to it.
"""

import asyncio
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import streamlit as st

CLEANUP_TIMEOUT_S = 2.0  # how long a finished job waits for leftover tasks before closing its loop


class StageJob:
    """One stage attempt; `events` fills up in the background while the UI polls it."""

    def __init__(self, stage: str, attempt: int, feedback: str = "",
                 stream: Callable[[], AsyncIterator[dict]] | None = None, events: list[dict] | None = None,
                 run_id: str = ""):
        self.id = uuid.uuid4().hex[:8]
        self.stage, self.attempt, self.feedback, self.run_id = stage, attempt, feedback, run_id
        self.events: list[dict] = list(events or [])
        self.started = time.time()
        self.finished: float | None = None
        self._done = threading.Event()
        if stream is None:  # a finished attempt loaded from disk
            self._done.set()
        else:
            threading.Thread(target=self._run, args=(stream,), daemon=True, name=f"stage-{stage}-{self.id}").start()

    def _run(self, stream: Callable[[], AsyncIterator[dict]]) -> None:
        async def consume() -> None:
            try:
                async for event in stream():
                    self.events.append(event)  # list.append is atomic: safe to read from the script thread
            except Exception as exc:  # noqa: BLE001 - shown in the chat instead of crashing the thread
                self.events.append({"type": "error", "stage": self.stage, "message": f"{type(exc).__name__}: {exc}"})
            finally:
                # The pipeline has saved the result (its finally ran when the stream ended): show the gate now,
                # not after the event loop has waited for leftover executor threads (asyncio.run waits up to 300 s).
                self.finished = time.time()
                self._done.set()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(consume())
            leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in leftover:
                task.cancel()
            if leftover:
                loop.run_until_complete(asyncio.wait(leftover, timeout=CLEANUP_TIMEOUT_S))
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), CLEANUP_TIMEOUT_S))
        except Exception:  # noqa: BLE001 - cleanup only; the job is already done
            pass
        finally:
            self._done.set()
            loop.close()  # shuts the default executor down without joining its threads

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    @property
    def result(self) -> dict | None:
        return next((e for e in reversed(self.events) if e.get("type") == "result"), None)


class RunRegistry:
    """Process-wide: one Pipeline per run id and its latest StageJob, shared by every browser session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pipelines: dict[str, Any] = {}
        self._jobs: dict[str, StageJob] = {}

    def pipeline(self, run_id: str, load: Callable[[], Any]) -> Any:
        """The live Pipeline for run_id, loading it from disk (once) when this process has not seen it yet."""
        with self._lock:
            if run_id not in self._pipelines:
                self._pipelines[run_id] = load()
            return self._pipelines[run_id]

    def add(self, pipeline: Any) -> Any:
        with self._lock:
            self._pipelines[pipeline.run_id] = pipeline
            return pipeline

    def job(self, run_id: str | None) -> StageJob | None:
        return self._jobs.get(run_id) if run_id else None

    def start(self, run_id: str, make_job: Callable[[], StageJob]) -> StageJob | None:
        """Create and register the run's next job; None (nothing started) if the run already has one running."""
        with self._lock:
            current = self._jobs.get(run_id)
            if current is not None and not current.done:
                return None
            job = self._jobs[run_id] = make_job()
            return job

    def live(self) -> list[StageJob]:
        """Jobs still running, newest first."""
        return sorted((j for j in self._jobs.values() if not j.done), key=lambda j: j.started, reverse=True)


@st.cache_resource(show_spinner=False)
def run_registry() -> RunRegistry:
    return RunRegistry()
