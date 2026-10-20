"""Review fixes: revision merges, failed-revision fallback, run locks, replay attempts/sources, recording, CLI options."""

import json
import threading

import pytest

from transform_agents.core import STAGES, AgentRequest
from transform_agents.orchestrator import (
    APPROVED,
    AWAITING,
    FAILED,
    PENDING,
    RUNNING,
    AgentCoreClient,
    FakeClient,
    Options,
    Pipeline,
    ReplayClient,
    make_client,
    resolve_replay_source,
)
from transform_agents.orchestrator import paths as orch_paths
from transform_agents.orchestrator.__main__ import main as cli_main
from transform_agents.orchestrator.clients import is_complete_run
from transform_agents.orchestrator.pipeline import LOCK_FILE
from transform_agents.orchestrator.record import record

REQS = "# Requirements\n- FR1 keep the API\n"


async def drain(agen) -> list[dict]:
    return [event async for event in agen]


async def run_and_approve(p: Pipeline, until: str) -> None:
    for stage in STAGES[: STAGES.index(until) + 1]:
        await drain(p.run_stage(stage))
        p.approve(stage)


def result_event(stage: str, artifacts: dict[str, str], summary: str = "ok") -> dict:
    return {"type": "result", "stage": stage, "summary": summary, "data": {"checks": []}, "artifacts": artifacts,
            "metrics": {}}


class Scripted(FakeClient):
    """FakeClient, except the stages in `script` yield the next scripted event list on each call."""

    def __init__(self, script: dict[str, list[list[dict]]], name: str = "fake"):
        super().__init__()
        self.script, self.name = script, name

    async def stream(self, stage, request):
        if self.script.get(stage):
            self.calls.append((stage, request))
            for event in self.script[stage].pop(0):
                yield event
            return
        async for event in super().stream(stage, request):
            yield event


@pytest.fixture
def pipeline(tmp_path) -> Pipeline:
    p = Pipeline.create(FakeClient(), root=tmp_path)
    p.set_requirements("reqs.md", REQS)
    return p


# ----------------------------------------------------------------------------- 1. revisions keep earlier artifacts


async def test_revision_merges_new_artifacts_over_the_previous_ones(pipeline):
    await run_and_approve(pipeline, "terraform")
    fixed_deploy = "kind: Deployment\n# attempt 1: readOnlyRootFilesystem\n"
    fixed_service = "kind: Service\n# attempt 2: named port\n"
    pipeline.client = Scripted({"k8s_validation": [
        [result_event("k8s_validation", {"reports/validation-report.md": "v1", "k8s/base/deployment.yaml": fixed_deploy})],
        [result_event("k8s_validation", {"k8s/base/service.yaml": fixed_service, "reports/validation-report.md": "v2"})],
    ]})
    await drain(pipeline.run_stage("k8s_validation"))
    events = await drain(pipeline.revise("k8s_validation", "also name the service port"))

    _, request = pipeline.client.calls[-1]
    assert request.files["k8s/base/deployment.yaml"] == fixed_deploy  # the agent saw its previous output
    artifacts = pipeline.artifacts("k8s_validation")
    assert artifacts == {"reports/validation-report.md": "v2", "k8s/base/deployment.yaml": fixed_deploy,
                         "k8s/base/service.yaml": fixed_service}
    assert events[-1]["artifacts"] == artifacts  # the UI card and the recording show the full set
    on_disk = json.loads((pipeline.dir / "k8s_validation" / "result.json").read_text())
    assert on_disk["artifacts"] == artifacts
    ws = pipeline.dir / "workspace" / "k8s" / "base"
    assert (ws / "deployment.yaml").read_text() == fixed_deploy and (ws / "service.yaml").read_text() == fixed_service

    pipeline.approve("k8s_validation")
    finops = pipeline.stage_request("finops").files
    assert finops["k8s/base/deployment.yaml"] == fixed_deploy and finops["k8s/base/service.yaml"] == fixed_service


async def test_fresh_rerun_does_not_merge_or_keep_a_stale_result(pipeline):
    await run_and_approve(pipeline, "requirements")
    await drain(pipeline.revise("discovery", "more detail"))  # requirements is stale now
    assert pipeline.status("requirements") == PENDING
    pipeline.approve("discovery")
    pipeline.client = Scripted({"requirements": [[{"type": "error", "stage": "requirements", "message": "boom"}]]})
    await drain(pipeline.run_stage("requirements"))
    assert pipeline.status("requirements") == FAILED and pipeline.result("requirements") is None
    assert not (pipeline.dir / "requirements" / "result.json").exists()
    assert (pipeline.dir / "requirements" / "history" / "attempt-1.result.json").exists()


# ----------------------------------------------------------------------------- 4. failed revision falls back


async def test_failed_revision_keeps_the_previous_result_for_approval(pipeline):
    await drain(pipeline.run_stage("discovery"))
    baseline = pipeline.result("discovery")
    pipeline.client = Scripted({"discovery": [[{"type": "status", "stage": "discovery", "message": "start"},
                                               {"type": "error", "stage": "discovery", "message": "Throttling"}]]})
    events = await drain(pipeline.revise("discovery", "add backups"))
    assert events[-1]["type"] == "error"
    st = pipeline.state.stages["discovery"]
    assert st.status == AWAITING and "revision failed" in st.error and "Throttling" in st.error
    assert pipeline.revision_failed("discovery") and pipeline.result("discovery") == baseline
    assert "reports/assessment.md" in pipeline.merged_artifacts()
    assert pipeline.retry_feedback("discovery") == "add backups"
    reloaded = Pipeline.load(pipeline.run_id, FakeClient(), root=pipeline.dir.parent)
    assert reloaded.revision_failed("discovery")

    assert pipeline.approve("discovery") == "requirements"  # "Approve previous result"
    assert pipeline.status("discovery") == APPROVED and not pipeline.state.stages["discovery"].error


async def test_failed_first_attempt_is_still_failed(tmp_path):
    p = Pipeline.create(FakeClient(fail={"discovery"}), root=tmp_path)
    await drain(p.run_stage("discovery"))
    assert p.status("discovery") == FAILED and not p.revision_failed("discovery")


# ----------------------------------------------------------------------------- 2/10. run locks and saves


async def test_load_keeps_a_stage_running_while_its_lock_is_alive(pipeline, tmp_path):
    started = threading.Event()
    release = threading.Event()

    class Slow:
        name = "slow"

        async def stream(self, stage, request):
            import asyncio

            yield {"type": "status", "stage": stage, "message": "start"}
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield result_event(stage, {"reports/assessment.md": "x"})

    pipeline.client = Slow()
    agen = pipeline.run_stage("discovery")
    await agen.__anext__()
    assert pipeline.status("discovery") == RUNNING and (pipeline.dir / "discovery" / LOCK_FILE).exists()

    other = Pipeline.load(pipeline.run_id, FakeClient(), root=tmp_path)  # e.g. a new UI session or the CLI
    assert other.status("discovery") == RUNNING  # not "interrupted": our process still owns the lock

    release.set()
    await drain(agen)
    assert pipeline.status("discovery") == AWAITING and not (pipeline.dir / "discovery" / LOCK_FILE).exists()


def test_load_marks_running_failed_when_the_lock_is_dead_or_missing(pipeline, tmp_path):
    for stage, lock in (("discovery", {"pid": 2**22 + 12345, "attempt": 1, "started_at": "2026-10-20T00:00:00+00:00"}),
                        ("requirements", None)):
        pipeline.state.stages[stage].status = RUNNING
        (pipeline.dir / stage).mkdir(exist_ok=True)
        if lock:
            (pipeline.dir / stage / LOCK_FILE).write_text(json.dumps(lock))
    pipeline.save()
    resumed = Pipeline.load(pipeline.run_id, FakeClient(), root=tmp_path)
    assert resumed.status("discovery") == FAILED and resumed.status("requirements") == FAILED


def test_concurrent_saves_use_unique_temp_files(pipeline):
    errors = []

    def hammer():
        try:
            for _ in range(30):
                pipeline.save()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert json.loads((pipeline.dir / "state.json").read_text())["run_id"] == pipeline.run_id
    assert not list(pipeline.dir.glob("*.tmp"))


def test_old_state_files_without_new_fields_still_load(pipeline, tmp_path):
    raw = json.loads((pipeline.dir / "state.json").read_text())
    raw["options"].pop("simulate_drift")
    raw["options"]["removed_option"] = True
    raw["stages"]["discovery"].pop("client")
    (pipeline.dir / "state.json").write_text(json.dumps(raw))
    loaded = Pipeline.load(pipeline.run_id, FakeClient(), root=tmp_path)
    assert loaded.state.options.simulate_drift is False


# ----------------------------------------------------------------------------- 5. AgentCore ARNs are read lazily


class StubBody:
    def iter_lines(self):
        yield b'data: {"type": "result", "stage": "discovery", "summary": "s", "data": {}, "artifacts": {}}'


class StubAgentCore:
    def __init__(self):
        self.arns = []

    def invoke_agent_runtime(self, **kwargs):
        self.arns.append(kwargs["agentRuntimeArn"])
        return {"contentType": "text/event-stream", "response": StubBody()}


async def test_agentcore_client_reads_arns_at_invoke_time(tmp_path, monkeypatch):
    arns_file = tmp_path / "agent-arns.json"
    monkeypatch.setattr(orch_paths, "AGENT_ARNS_FILE", arns_file)
    monkeypatch.delenv("AGENTCORE_ARN_DISCOVERY", raising=False)
    stub = StubAgentCore()
    client = AgentCoreClient(boto_client=stub)  # created before `make deploy-agents` finished
    missing = await drain(client.stream("discovery", AgentRequest()))
    assert missing[-1]["type"] == "error"

    arns_file.write_text(json.dumps({"discovery": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/discovery-v1"}))
    assert (await drain(client.stream("discovery", AgentRequest())))[-1]["type"] == "result"
    arns_file.write_text("{ half-written")  # never crashes; env overrides still apply
    monkeypatch.setenv("AGENTCORE_ARN_DISCOVERY", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/discovery-v2")
    assert (await drain(client.stream("discovery", AgentRequest())))[-1]["type"] == "result"
    assert stub.arns == ["arn:aws:bedrock-agentcore:us-west-2:1:runtime/discovery-v1",
                         "arn:aws:bedrock-agentcore:us-west-2:1:runtime/discovery-v2"]
    assert AgentCoreClient(arns={"x": "y"}).arns == {"x": "y"}  # injected ARNs are kept as given


# ----------------------------------------------------------------------------- 6. replay follows pipeline attempts


async def recorded_run_with_a_failed_attempt(root):
    """discovery: attempt 1 fails, attempt 2 succeeds, attempt 3 is a revision."""
    rec = Pipeline.create(FakeClient(fail={"discovery"}), root=root)
    await drain(rec.run_stage("discovery"))
    rec.client.fail.clear()
    await drain(rec.run_stage("discovery"))
    await drain(rec.revise("discovery", "add backups"))
    assert rec.state.stages["discovery"].attempts == 3
    return rec


async def test_replay_keys_on_the_pipeline_attempt_and_skips_failed_ones(tmp_path):
    rec = await recorded_run_with_a_failed_attempt(tmp_path / "rec")
    replay = ReplayClient(rec.dir, speed=1000)

    for _ in range(2):  # "New run" twice with the same client: each starts at the first good attempt
        p = Pipeline.create(replay, root=tmp_path / "runs")
        await drain(p.run_stage("discovery"))
        assert p.status("discovery") == AWAITING and "Revised" not in p.result("discovery")["summary"]
    await drain(p.revise("discovery", "anything"))
    assert "Revised" in p.result("discovery")["summary"]  # attempt 2 = the recorded revision

    resumed = Pipeline.load(p.run_id, ReplayClient(rec.dir, speed=1000), root=tmp_path / "runs")
    await drain(resumed.revise("discovery", "again"))  # attempt 3: past the recording, the last good one
    assert resumed.status("discovery") == AWAITING and "Revised" in resumed.result("discovery")["summary"]
    assert "attempt 2/2" in json.loads((resumed.dir / "discovery" / "events.jsonl").read_text().splitlines()[0])[
        "message"]


# ----------------------------------------------------------------------------- 7. replay sources and recording


async def make_run(root, client_name: str, until: str | None = "finops", run_id: str | None = None) -> Pipeline:
    p = Pipeline.create(Scripted({}, name=client_name), root=root, run_id=run_id)
    p.set_requirements("reqs.md", REQS)
    if until:
        await run_and_approve(p, until)
    return p


async def test_latest_means_the_newest_complete_real_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(orch_paths, "RECORDED_RUN", tmp_path / "no-recording")
    good = await make_run(tmp_path, "local", run_id="20261020-090000-aaaa")  # the oldest of the four
    partial = await make_run(tmp_path, "agentcore", until="terraform", run_id="20261020-090100-bbbb")
    fake = await make_run(tmp_path, "fake", run_id="20261020-090200-cccc")
    replayed = await make_run(tmp_path, "replay", run_id="20261020-090300-dddd")
    assert [is_complete_run(p.dir) for p in (good, partial, fake, replayed)] == [True, False, False, False]
    assert resolve_replay_source("latest") == good.dir
    assert resolve_replay_source("recorded") == good.dir  # no recording yet: falls back to latest
    assert resolve_replay_source("fixtures") is None and resolve_replay_source(None) is None
    assert make_client("replay", replay_from="latest").source == good.dir


async def test_record_copies_a_complete_run_and_replay_recorded_uses_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    src = await make_run(tmp_path / "runs", "local")
    (src.dir / "discovery" / LOCK_FILE).write_text("{}")
    (src.dir / "state.json.123.tmp").write_text("{}")
    (src.dir / "finops" / "result.json").write_text(json.dumps({"note": f"{orch_paths.ROOT}/runs/x"}))
    dest = tmp_path / "samples" / "recorded-run"
    copied = record(src.dir, dest)
    assert "state.json" in copied and "discovery/events.jsonl" in copied and "inputs/requirements/reqs.md" in copied
    assert not any(c.startswith("workspace/") or c.endswith((".tmp", LOCK_FILE)) for c in copied)
    assert "<repo>/runs/x" in (dest / "finops" / "result.json").read_text()

    monkeypatch.setattr(orch_paths, "RECORDED_RUN", dest)
    assert resolve_replay_source("recorded") == dest
    p = Pipeline.create(make_client("replay", replay_from="recorded"), root=tmp_path / "runs")
    p.client.speed = 1000
    await drain(p.run_stage("discovery"))
    assert p.status("discovery") == AWAITING and p.data("discovery") == src.data("discovery")

    partial = await make_run(tmp_path / "runs", "local", until="discovery")
    with pytest.raises(ValueError, match="not a complete run"):
        record(partial.dir, tmp_path / "other")
    assert record(partial.dir, tmp_path / "other", force=True)


def test_record_cli(tmp_path, capsys):
    from transform_agents.orchestrator.record import main as record_main

    assert record_main([str(tmp_path / "nope")]) == 1
    assert "not a run directory" in capsys.readouterr().out


# ----------------------------------------------------------------------------- 13/14. CLI options and drift


def test_cli_resume_only_overrides_explicit_options(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_DELAY", "0")
    assert cli_main(["--mode", "fake", "--auto-approve", "--until", "discovery", "--live-deploy",
                     "--simulate-drift"]) == 0
    run_id = Pipeline.list_runs(tmp_path)[0]
    opts = Pipeline.load(run_id, FakeClient(), root=tmp_path).state.options
    assert opts.live_deploy and opts.simulate_drift and not opts.live_aws

    assert cli_main(["--mode", "fake", "--auto-approve", "--until", "discovery", "--resume", run_id,
                     "--live-aws"]) == 0
    opts = Pipeline.load(run_id, FakeClient(), root=tmp_path).state.options
    assert opts.live_aws and opts.live_deploy and opts.simulate_drift

    assert cli_main(["--mode", "fake", "--auto-approve", "--until", "discovery", "--resume", run_id,
                     "--no-simulate-drift"]) == 0
    opts = Pipeline.load(run_id, FakeClient(), root=tmp_path).state.options
    assert opts.live_aws and opts.live_deploy and not opts.simulate_drift


async def test_simulate_drift_reaches_only_the_k8s_validation_stage(tmp_path):
    client = FakeClient()
    p = Pipeline.create(client, root=tmp_path, options=Options(simulate_drift=True))
    p.set_requirements("reqs.md", REQS)
    await run_and_approve(p, "finops")
    sent = {stage: request.options for stage, request in client.calls}
    assert sent["k8s_validation"]["simulate_drift"] is True
    assert all("simulate_drift" not in options for stage, options in sent.items() if stage != "k8s_validation")
    assert p.data("k8s_validation")["drift_injected"][0]["file"] == "k8s/base/deployment.yaml"
    assert "drift_injected" not in Pipeline.create(FakeClient(), root=tmp_path).stage_request("discovery").options


def test_replay_cli_defaults_to_the_recording(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("REPLAY_SPEED", "1000")
    monkeypatch.setattr(orch_paths, "RECORDED_RUN", tmp_path / "no-recording")
    reqs = tmp_path / "reqs.md"
    reqs.write_text(REQS)
    assert cli_main(["--requirements", str(reqs), "--mode", "replay", "--auto-approve", "--until", "discovery"]) == 0
    out = capsys.readouterr().out
    assert "replaying tests/fixtures" in out  # no recording and no complete run yet: the fixtures
