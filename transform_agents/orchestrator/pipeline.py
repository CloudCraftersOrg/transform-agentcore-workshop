"""The migration pipeline: six stages with a human approval gate after each, every run persisted on disk.

runs/<run_id>/
  state.json                         stage statuses, options and feedback history
  inputs/requirements/<file>         the uploaded requirements (sent to the agent as requirements/<file>)
  <stage>/events.jsonl               every event of the latest attempt (replayable)
  <stage>/result.json                the reviewed result (a revision's artifacts merged over the previous ones)
  <stage>/.running                   lock file while an attempt runs: {"pid", "attempt", "started_at"}
  <stage>/history/attempt-<n>.*      earlier attempts, kept when a stage is revised or retried
  workspace/<path>                   all artifacts merged in stage order: open it in your IDE
"""

import json
import os
import shutil
import threading
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transform_agents.core import STAGES, AgentRequest, Workspace, error

from .clients import AgentClient
from .paths import LEGACY_DIR, runs_dir

PENDING, RUNNING, AWAITING, APPROVED, FAILED = "pending", "running", "awaiting_approval", "approved", "failed"

# Which earlier results each stage receives as `inputs` (input name -> producing stage), per docs/CONTRACTS.md.
STAGE_INPUTS: dict[str, dict[str, str]] = {
    "discovery": {},
    "requirements": {"assessment": "discovery"},
    "modernization": {"assessment": "discovery", "spec": "requirements"},
    "terraform": {"spec": "requirements", "assessment": "discovery", "modernization": "modernization"},
    "k8s_validation": {"spec": "requirements", "modernization": "modernization"},
    "finops": {"assessment": "discovery", "spec": "requirements", "modernization": "modernization",
               "terraform": "terraform", "k8s_validation": "k8s_validation"},
}

MAX_LEGACY_FILE_BYTES = 200_000
SKIP_DIRS = {".terraform", "__pycache__", ".git", ".venv", "node_modules"}  # never walked (providers are ~800 MB)
SKIP_SUBTREES = {"terraform/tests"}  # the legacy Terraform's own test harness, not part of the estate
SKIP_FILES = {".terraform.lock.hcl", ".DS_Store"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def with_prefix(files: dict[str, str], *prefixes: str) -> dict[str, str]:
    return {path: text for path, text in files.items() if path.startswith(prefixes)}


def load_legacy_estate(root: Path = LEGACY_DIR) -> dict[str, str]:
    """legacy-infra/{app,terraform}/** as text, keyed `legacy/...`.

    Skips provider caches, lock and state files, the Terraform tests, binaries and files over 200 KB.
    """
    files: dict[str, str] = {}
    for sub in ("app", "terraform"):
        for dirpath, dirnames, filenames in os.walk(root / sub):
            here = Path(dirpath).relative_to(root)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (here / d).as_posix() not in SKIP_SUBTREES]
            for name in filenames:
                path = here / name
                if name in SKIP_FILES or ".tfstate" in name or (root / path).stat().st_size > MAX_LEGACY_FILE_BYTES:
                    continue
                try:
                    text = (root / path).read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue  # binary
                if "\x00" not in text:
                    files[f"legacy/{path.as_posix()}"] = text
    return dict(sorted(files.items()))


@dataclass
class Options:
    live_aws: bool = False  # discovery: describe the running EC2 instance + CloudWatch CPU
    live_deploy: bool = False  # k8s_validation: build the image and deploy it to kind
    live_pricing: bool = False  # finops: AWS Pricing API instead of the static price sheet
    kind_cluster: str = "transform-workshop"
    simulate_drift: bool = False  # k8s_validation only: inject typical hand-edit violations before validating

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Options":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})


@dataclass
class StageState:
    status: str = PENDING
    attempts: int = 0
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    feedback: list[dict[str, Any]] = field(default_factory=list)  # [{"attempt", "text", "at"}]
    updated_at: str = ""
    client: str = ""  # local | agentcore | replay | fake: who produced the latest attempt


@dataclass
class RunState:
    run_id: str
    created_at: str
    client: str = ""
    requirements_file: str = ""
    options: Options = field(default_factory=Options)
    stages: dict[str, StageState] = field(default_factory=lambda: {s: StageState() for s in STAGES})

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunState":
        known = StageState.__dataclass_fields__
        stages = {s: StageState(**{k: v for k, v in (raw.get("stages", {}).get(s) or {}).items() if k in known})
                  for s in STAGES}
        return cls(run_id=raw["run_id"], created_at=raw.get("created_at", ""), client=raw.get("client", ""),
                   requirements_file=raw.get("requirements_file", ""), options=Options.from_dict(raw.get("options", {})),
                   stages=stages)


LOCK_FILE = ".running"
LOCK_MAX_AGE_S = 2 * 3600  # older lock files are stale even if their pid was reused
_SAVE_LOCK = threading.RLock()  # serializes state.json writes (UI threads, background jobs)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def lock_alive(path: Path) -> bool:
    """A stage's `.running` lock belongs to a live process (this one or another) and is not ancient."""
    try:
        lock = json.loads(path.read_text())
        started = datetime.fromisoformat(lock["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    age = (datetime.now(UTC) - started).total_seconds()
    return age < LOCK_MAX_AGE_S and pid_alive(int(lock.get("pid", 0)))


class Pipeline:
    """Runs the six stages in order; each one stops at an approval gate (approve, or revise with feedback)."""

    def __init__(self, client: AgentClient, state: RunState, root: Path):
        self.client = client
        self.state = state
        self.dir = root / state.run_id
        self.results: dict[str, dict] = {}
        for stage in STAGES:
            path = self.dir / stage / "result.json"
            if path.exists():
                self.results[stage] = json.loads(path.read_text())

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, client: AgentClient, *, options: Options | None = None, root: Path | None = None,
               run_id: str | None = None) -> "Pipeline":
        run_id = run_id or f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        pipeline = cls(client, RunState(run_id=run_id, created_at=now(), options=options or Options()), root or runs_dir())
        pipeline.save()
        return pipeline

    @classmethod
    def load(cls, run_id: str, client: AgentClient, *, root: Path | None = None) -> "Pipeline":
        """Resume a run from disk. A stage left `running` by a crash is marked failed so it can be retried;
        one whose `.running` lock still belongs to a live process (e.g. the UI server) stays running."""
        root = root or runs_dir()
        state = RunState.from_dict(json.loads((root / run_id / "state.json").read_text()))
        for stage, st in state.stages.items():
            if st.status == RUNNING and not lock_alive(root / run_id / stage / LOCK_FILE):
                st.status, st.error = FAILED, "interrupted (resumed from disk)"
        pipeline = cls(client, state, root)
        pipeline.save()
        return pipeline

    @staticmethod
    def list_runs(root: Path | None = None) -> list[str]:
        """Run ids on disk, newest first."""
        root = root or runs_dir()
        return sorted((d.name for d in root.glob("*") if (d / "state.json").exists()), reverse=True) if root.is_dir() else []

    @property
    def run_id(self) -> str:
        return self.state.run_id

    def save(self) -> None:
        """Atomic write of state.json: unique temp file + os.replace, serialized across threads."""
        with _SAVE_LOCK:
            self.state.client = self.state.client or self.client_name()
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f"state.json.{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:6]}.tmp"
            try:
                tmp.write_text(json.dumps(asdict(self.state), indent=2))
                os.replace(tmp, self.dir / "state.json")
            finally:
                tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ queries

    def client_name(self) -> str:
        return getattr(self.client, "name", type(self.client).__name__)

    def status(self, stage: str) -> str:
        return self.state.stages[stage].status

    def result(self, stage: str) -> dict | None:
        return self.results.get(stage)

    def data(self, stage: str) -> dict[str, Any]:
        return (self.results.get(stage) or {}).get("data") or {}

    def artifacts(self, stage: str) -> dict[str, str]:
        return (self.results.get(stage) or {}).get("artifacts") or {}

    def is_complete(self) -> bool:
        return all(self.status(s) == APPROVED for s in STAGES)

    def revision_failed(self, stage: str) -> bool:
        """A revision failed but the previous result is kept for review (awaiting approval, with an error)."""
        st = self.state.stages[stage]
        return st.status == AWAITING and bool(st.error) and stage in self.results

    def next_stage(self) -> str | None:
        """The first stage not yet approved (None when the migration is complete)."""
        return next((s for s in STAGES if self.status(s) != APPROVED), None)

    def merged_artifacts(self, before: str | None = None) -> dict[str, str]:
        """Artifacts of every reviewed stage in order; a later stage's file replaces an earlier one with the same path."""
        merged: dict[str, str] = {}
        for stage in STAGES[: STAGES.index(before)] if before else STAGES:
            if self.status(stage) in (AWAITING, APPROVED):
                merged.update(self.artifacts(stage))
        return merged

    def requirements(self) -> tuple[str, str] | None:
        """(path, text) of the uploaded requirements, e.g. ("requirements/migration-requirements.md", "...")."""
        rel = self.state.requirements_file
        path = self.dir / "inputs" / rel if rel else None
        return (rel, path.read_text()) if path and path.exists() else None

    def attempts(self, stage: str) -> list[tuple[int, str, list[dict]]]:
        """Every recorded attempt of a stage as (attempt, feedback, events), oldest first."""
        st, stage_dir = self.state.stages[stage], self.dir / stage
        feedback = {f["attempt"]: f["text"] for f in st.feedback}
        out = []
        for n in range(1, st.attempts + 1):
            path = stage_dir / ("events.jsonl" if n == st.attempts else f"history/attempt-{n}.events.jsonl")
            if path.exists():
                events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                out.append((n, feedback.get(n, ""), events))
        return out

    # ------------------------------------------------------------------ inputs

    def set_requirements(self, filename: str, text: str) -> str:
        """Store the uploaded requirements as requirements/<filename> (the requirements stage's only file)."""
        name = Path(filename).name or "requirements.md"
        self.state.requirements_file = f"requirements/{name}"
        path = self.dir / "inputs" / self.state.requirements_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.save()
        return self.state.requirements_file

    def stage_request(self, stage: str, feedback: str = "") -> AgentRequest:
        """Build a stage's AgentRequest exactly as docs/CONTRACTS.md defines its inputs, files and options."""
        opts = self.state.options
        files: dict[str, str] = {}
        options: dict[str, Any] = {}
        match stage:
            case "discovery":
                files = load_legacy_estate()
                options = {"live_aws": opts.live_aws}
            case "requirements":
                if not (req := self.requirements()):
                    raise ValueError("upload the requirements file before running the requirements stage")
                files = {req[0]: req[1]}
            case "modernization":
                files = with_prefix(load_legacy_estate(), "legacy/app/")
            case "terraform":
                pass
            case "k8s_validation":
                files = with_prefix(self.artifacts("modernization"), "microservice/", "k8s/")
                options = {"live_deploy": opts.live_deploy, "kind_cluster": opts.kind_cluster,
                           "simulate_drift": opts.simulate_drift}
            case "finops":
                files = with_prefix(self.merged_artifacts(before="finops"), "k8s/", "terraform/")
                options = {"live_pricing": opts.live_pricing}
            case _:
                raise ValueError(f"unknown stage {stage!r}")
        inputs: dict[str, Any] = {name: self.data(source) for name, source in STAGE_INPUTS[stage].items()}
        if feedback:  # revision: the agent edits its previous output instead of starting over
            files = {**files, **self.artifacts(stage)}
            inputs["previous"] = self.data(stage)
        return AgentRequest(run_id=self.run_id, feedback=feedback, inputs=inputs, files=files, options=options)

    # ------------------------------------------------------------------ gates

    def check_runnable(self, stage: str) -> None:
        """Raise ValueError unless every earlier stage is approved and this one is not already running."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        if self.status(stage) == RUNNING:
            raise ValueError(f"{stage} is already running")
        if blocked := [s for s in STAGES[: STAGES.index(stage)] if self.status(s) != APPROVED]:
            raise ValueError(f"approve {', '.join(blocked)} before running {stage}")
        if stage == "requirements" and not self.requirements():
            raise ValueError("upload the requirements file before running the requirements stage")

    async def run_stage(self, stage: str, feedback: str | None = None) -> AsyncIterator[dict]:
        """Run (or re-run) one stage: stream and record its events, then stop at the approval gate."""
        self.check_runnable(stage)
        request = self.stage_request(stage, feedback or "")
        st = self.state.stages[stage]
        # A revision edits the reviewed result: agents may return only the files they changed, so the previous
        # artifacts are the base the new ones are merged over, and the fallback if the revision fails.
        baseline = self.results.get(stage) if feedback else None
        self._archive_attempt(stage, keep_result=baseline is not None)
        if baseline is None:  # a fresh attempt: an older result (e.g. made stale by an upstream revision) is gone
            self.results.pop(stage, None)
        st.attempts += 1
        attempt = st.attempts
        if feedback:
            st.feedback.append({"attempt": attempt, "text": feedback, "at": now()})
        st.status, st.error, st.updated_at = RUNNING, "", now()
        st.client = self.state.client = self.client_name()  # a resumed run may continue with another client
        for later in STAGES[STAGES.index(stage) + 1:]:  # downstream results are stale now
            self.state.stages[later].status = PENDING
        self._write_lock(stage, attempt)
        self.save()

        result: dict | None = None
        failure = "interrupted before a result"
        try:
            # A replay client follows the pipeline's attempt numbers (resume, retries, revisions).
            events = (self.client.stream(stage, request, attempt=attempt) if getattr(self.client, "attempt_aware", False)
                      else self.client.stream(stage, request))
            with (self.dir / stage / "events.jsonl").open("w") as log:
                async for event in events:
                    if event.get("type") == "result" and baseline is not None:
                        event = {**event, "artifacts": {**(baseline.get("artifacts") or {}),
                                                        **(event.get("artifacts") or {})}}
                    log.write(json.dumps(event) + "\n")
                    log.flush()
                    if event.get("type") == "result":
                        result = event
                    elif event.get("type") == "error":
                        failure = event.get("message") or "agent error"
                    yield event
        except Exception as exc:  # noqa: BLE001 - a broken client stream fails the stage, not the app
            failure = f"{type(exc).__name__}: {exc}"
            yield error(stage, failure)
        finally:
            self._finish(stage, result, failure, baseline)

    async def revise(self, stage: str, feedback: str) -> AsyncIterator[dict]:
        """Re-run a stage with reviewer feedback plus its previous artifacts (`files`) and data (`inputs.previous`)."""
        if not feedback.strip():
            raise ValueError("feedback is required to revise a stage")
        async for event in self.run_stage(stage, feedback.strip()):
            yield event

    def retry_feedback(self, stage: str) -> str | None:
        """Feedback of the last attempt, so retrying a failed revision repeats the same request."""
        st = self.state.stages[stage]
        return next((f["text"] for f in st.feedback if f["attempt"] == st.attempts), None)

    def approve(self, stage: str) -> str | None:
        """Approve a reviewed stage and return the next stage to run (None when the pipeline is complete)."""
        if self.status(stage) != AWAITING:
            raise ValueError(f"{stage} is {self.status(stage)}, not awaiting approval")
        st = self.state.stages[stage]
        st.status, st.error, st.updated_at = APPROVED, "", now()
        self.save()
        return self.next_stage()

    # ------------------------------------------------------------------ persistence

    def _archive_attempt(self, stage: str, keep_result: bool) -> None:
        """Move the latest attempt to history/. result.json stays in place for a revision (its baseline)."""
        stage_dir = self.dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        n = self.state.stages[stage].attempts
        if n and (stage_dir / "events.jsonl").exists():
            (stage_dir / "history").mkdir(exist_ok=True)
            os.replace(stage_dir / "events.jsonl", stage_dir / "history" / f"attempt-{n}.events.jsonl")
            if (stage_dir / "result.json").exists():
                shutil.copy(stage_dir / "result.json", stage_dir / "history" / f"attempt-{n}.result.json")
        if not keep_result:
            (stage_dir / "result.json").unlink(missing_ok=True)

    def _write_lock(self, stage: str, attempt: int) -> None:
        lock = {"pid": os.getpid(), "attempt": attempt, "started_at": now()}
        (self.dir / stage / LOCK_FILE).write_text(json.dumps(lock))

    def _finish(self, stage: str, result: dict | None, failure: str, baseline: dict | None = None) -> None:
        st = self.state.stages[stage]
        if result is not None:
            result = {**result, "stage": stage}  # a stand-in agent (hello) may report another stage name
            (self.dir / stage / "result.json").write_text(json.dumps(result, indent=2))
            self.results[stage] = result
            st.status, st.summary, st.metrics, st.error = AWAITING, result.get("summary", ""), result.get("metrics", {}), ""
        elif baseline is not None:  # a failed revision: keep the previous result reviewable (approve or retry)
            self.results[stage] = baseline
            st.status, st.error = AWAITING, f"revision failed: {failure}"
        else:
            st.status, st.error = FAILED, failure
        st.updated_at = now()
        (self.dir / stage / LOCK_FILE).unlink(missing_ok=True)
        self.write_workspace()
        self.save()

    def write_workspace(self) -> Path:
        """Rewrite runs/<id>/workspace/ from the merged artifacts so it always matches the reviewed stages."""
        root = self.dir / "workspace"
        shutil.rmtree(root, ignore_errors=True)
        for path, content in self.merged_artifacts().items():
            try:
                target = root / Workspace.normalize(path)  # rejects absolute and ../ paths
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return root
