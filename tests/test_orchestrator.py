import io
import json
import sys
import types

import pytest

from transform_agents.core import STAGES, AgentRequest
from transform_agents.orchestrator import (
    APPROVED,
    AWAITING,
    FAILED,
    PENDING,
    AgentCoreClient,
    FakeClient,
    LocalClient,
    Options,
    Pipeline,
    ReplayClient,
    load_legacy_estate,
)
from transform_agents.orchestrator.__main__ import main as cli_main
from transform_agents.orchestrator.clients import parse_overrides, parse_sse_line, synthesize_events
from transform_agents.orchestrator.fakes import fake_result

REQS = "# Requirements\n- FR1 keep the API\n"


async def drain(agen) -> list[dict]:
    return [event async for event in agen]


@pytest.fixture
def pipeline(tmp_path) -> Pipeline:
    p = Pipeline.create(FakeClient(), root=tmp_path, options=Options(live_aws=True, live_deploy=True))
    p.set_requirements("reqs.md", REQS)
    return p


async def run_and_approve(p: Pipeline, until: str) -> None:
    for stage in STAGES[: STAGES.index(until) + 1]:
        await drain(p.run_stage(stage))
        p.approve(stage)


# ----------------------------------------------------------------------------- legacy estate


def test_load_legacy_estate_prefixes_and_skips(tmp_path):
    def write(rel: str, content: str | bytes) -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content) if isinstance(content, bytes) else path.write_text(content)

    write("app/app.py", "print('hi')")
    write("app/templates/index.html", "<html>")
    write("app/__pycache__/app.cpython-312.pyc", b"\x00\x01")
    write("app/logo.png", b"\x89PNG\x00\xff\xfe")
    write("app/big.txt", "x" * 200_001)
    write("terraform/main.tf", "resource {}")
    write("terraform/user_data.sh.tftpl", "#!/bin/bash")
    write("terraform/terraform.tfvars.example", "region = \"us-west-2\"")
    write("terraform/.terraform/providers/registry.terraform.io/hashicorp/aws/README.md", "provider")
    write("terraform/.terraform.lock.hcl", "provider \"aws\" {}")
    write("terraform/terraform.tfstate", "{}")
    write("terraform/terraform.tfstate.backup", "{}")
    write("terraform/tests/bootstrap_in_docker.sh", "#!/bin/bash")
    write("README.md", "outside app/ and terraform/")
    files = load_legacy_estate(tmp_path)
    assert list(files) == ["legacy/app/app.py", "legacy/app/templates/index.html", "legacy/terraform/main.tf",
                           "legacy/terraform/terraform.tfvars.example", "legacy/terraform/user_data.sh.tftpl"]


def test_load_legacy_estate_reads_the_real_repo():
    files = load_legacy_estate()
    assert "legacy/app/app.py" in files and all(path.startswith("legacy/") for path in files)
    terraform = {p for p in files if p.startswith("legacy/terraform/")}
    if terraform:  # legacy-infra/terraform is written by another workstream
        assert terraform == {f"legacy/terraform/{name}" for name in (
            "versions.tf", "variables.tf", "main.tf", "outputs.tf", "user_data.sh.tftpl", "terraform.tfvars.example")}


# ----------------------------------------------------------------------------- stage requests (CONTRACTS.md)


async def test_stage_requests_follow_the_contracts(pipeline):
    req = pipeline.stage_request("discovery")
    assert req.inputs == {} and req.options == {"live_aws": True}
    assert req.files and all(p.startswith("legacy/") for p in req.files)
    await run_and_approve(pipeline, "discovery")

    req = pipeline.stage_request("requirements")
    assert req.files == {"requirements/reqs.md": REQS}
    assert req.inputs == {"assessment": pipeline.data("discovery")} and req.inputs["assessment"]["readiness_score"] == 62
    await run_and_approve(pipeline, "requirements")

    req = pipeline.stage_request("modernization")
    assert set(req.inputs) == {"assessment", "spec"} and all(p.startswith("legacy/app/") for p in req.files)
    await run_and_approve(pipeline, "modernization")

    req = pipeline.stage_request("terraform")
    assert set(req.inputs) == {"spec", "assessment", "modernization"} and req.files == {}
    await run_and_approve(pipeline, "terraform")

    req = pipeline.stage_request("k8s_validation")
    assert set(req.inputs) == {"spec", "modernization"}
    assert req.options == {"live_deploy": True, "kind_cluster": "transform-workshop", "simulate_drift": False}
    assert set(req.files) == {p for p in pipeline.artifacts("modernization") if p.startswith(("microservice/", "k8s/"))}
    await run_and_approve(pipeline, "k8s_validation")

    req = pipeline.stage_request("finops")
    assert set(req.inputs) == {"assessment", "spec", "modernization", "terraform", "k8s_validation"}
    assert req.options == {"live_pricing": False}
    assert all(p.startswith(("k8s/", "terraform/")) for p in req.files) and "terraform/main.tf" in req.files
    # k8s_validation's fixed manifest replaces the modernization one
    assert "readOnlyRootFilesystem" in req.files["k8s/base/deployment.yaml"]


def test_requirements_stage_needs_an_upload(tmp_path):
    p = Pipeline.create(FakeClient(), root=tmp_path)
    with pytest.raises(ValueError, match="upload the requirements"):
        p.stage_request("requirements")


# ----------------------------------------------------------------------------- gates and persistence


async def test_full_run_persists_state_events_results_and_workspace(pipeline):
    events = await drain(pipeline.run_stage("discovery"))
    assert events[0]["type"] == "status" and events[-1]["type"] == "result"
    assert pipeline.status("discovery") == AWAITING
    assert pipeline.next_stage() == "discovery"
    assert pipeline.approve("discovery") == "requirements"
    for stage in STAGES[1:]:
        await drain(pipeline.run_stage(stage))
        pipeline.approve(stage)
    assert pipeline.next_stage() is None

    d = pipeline.dir
    state = json.loads((d / "state.json").read_text())
    assert all(s["status"] == APPROVED for s in state["stages"].values())
    assert state["requirements_file"] == "requirements/reqs.md"
    assert (d / "inputs" / "requirements" / "reqs.md").read_text() == REQS
    for stage in STAGES:
        lines = (d / stage / "events.jsonl").read_text().splitlines()
        assert json.loads(lines[-1])["type"] == "result"
        assert json.loads((d / stage / "result.json").read_text())["stage"] == stage
    ws = d / "workspace"
    assert (ws / "reports" / "finops-report.md").exists()
    assert "readOnlyRootFilesystem" in (ws / "k8s" / "base" / "deployment.yaml").read_text()


async def test_cannot_skip_gates(pipeline):
    with pytest.raises(ValueError, match="approve discovery"):
        await drain(pipeline.run_stage("requirements"))
    with pytest.raises(ValueError, match="not awaiting approval"):
        pipeline.approve("discovery")


async def test_revise_sends_feedback_previous_artifacts_and_data(pipeline):
    await drain(pipeline.run_stage("discovery"))
    events = await drain(pipeline.revise("discovery", "  add a finding about backups  "))
    assert "Revised per feedback" in events[-1]["summary"]

    stage, request = pipeline.client.calls[-1]
    assert stage == "discovery" and request.feedback == "add a finding about backups"
    assert request.inputs["previous"]["readiness_score"] == 62
    assert "reports/assessment.md" in request.files and "legacy/app/app.py" in request.files

    st = pipeline.state.stages["discovery"]
    assert st.attempts == 2 and st.status == AWAITING
    assert st.feedback[0]["attempt"] == 2 and st.feedback[0]["text"] == "add a finding about backups"
    assert (pipeline.dir / "discovery" / "history" / "attempt-1.events.jsonl").exists()
    attempts = pipeline.attempts("discovery")
    assert [(n, fb) for n, fb, _ in attempts] == [(1, ""), (2, "add a finding about backups")]


async def test_revising_an_earlier_stage_resets_later_ones(pipeline):
    await run_and_approve(pipeline, "requirements")
    await drain(pipeline.revise("discovery", "more detail"))
    assert pipeline.status("discovery") == AWAITING
    assert pipeline.status("requirements") == PENDING
    assert "reports/migration-spec.md" not in pipeline.merged_artifacts()


async def test_failure_marks_stage_failed_and_retry_succeeds(tmp_path):
    client = FakeClient(fail={"discovery"})
    p = Pipeline.create(client, root=tmp_path)
    events = await drain(p.run_stage("discovery"))
    assert events[-1]["type"] == "error"
    assert p.status("discovery") == FAILED and "ThrottlingException" in p.state.stages["discovery"].error
    client.fail.clear()
    await drain(p.run_stage("discovery", p.retry_feedback("discovery")))
    assert p.status("discovery") == AWAITING


async def test_client_exception_becomes_error_event(tmp_path):
    class Boom:
        name = "boom"

        async def stream(self, stage, request):
            yield {"type": "status", "stage": stage, "message": "start"}
            raise RuntimeError("socket closed")

    p = Pipeline.create(Boom(), root=tmp_path)
    events = await drain(p.run_stage("discovery"))
    assert events[-1] == {"type": "error", "stage": "discovery", "message": "RuntimeError: socket closed"}
    assert p.status("discovery") == FAILED


async def test_resume_from_disk(pipeline, tmp_path):
    await run_and_approve(pipeline, "discovery")
    await drain(pipeline.run_stage("requirements"))
    pipeline.state.stages["modernization"].status = "running"  # simulate a crash mid-stage
    pipeline.save()

    resumed = Pipeline.load(pipeline.run_id, FakeClient(), root=tmp_path)
    assert resumed.status("discovery") == APPROVED and resumed.status("requirements") == AWAITING
    assert resumed.status("modernization") == FAILED
    assert resumed.data("requirements")["service"]["name"] == "inventory-service"
    assert resumed.requirements() == ("requirements/reqs.md", REQS)
    assert Pipeline.list_runs(tmp_path) == [pipeline.run_id]


async def test_stand_in_agent_result_is_stored_under_the_pipeline_stage(tmp_path):
    class Hello:
        name = "hello"

        async def stream(self, stage, request):
            yield {"type": "result", "stage": "hello", "summary": "ok", "data": {"file_count": 1},
                   "artifacts": {"hello/report.md": "hi"}, "metrics": {}}

    p = Pipeline.create(Hello(), root=tmp_path)
    await drain(p.run_stage("discovery"))
    assert p.result("discovery")["stage"] == "discovery"
    assert (p.dir / "workspace" / "hello" / "report.md").read_text() == "hi"


async def test_unsafe_artifact_paths_never_leave_the_workspace(tmp_path):
    class Evil:
        name = "evil"

        async def stream(self, stage, request):
            yield {"type": "result", "stage": stage, "summary": "", "data": {},
                   "artifacts": {"../../escape.txt": "x", "ok.txt": "y"}, "metrics": {}}

    p = Pipeline.create(Evil(), root=tmp_path)
    await drain(p.run_stage("discovery"))
    assert not (tmp_path / "escape.txt").exists() and (p.dir / "workspace" / "ok.txt").exists()


# ----------------------------------------------------------------------------- clients


def test_parse_overrides():
    assert parse_overrides("") == {}
    assert parse_overrides("hello") == {s: "hello" for s in STAGES}
    assert parse_overrides("discovery=hello, finops=hello") == {"discovery": "hello", "finops": "hello"}


async def test_local_client_imports_the_stage_agent(monkeypatch):
    async def handle(request):
        yield {"type": "result", "stage": "x", "summary": request.instruction, "data": {}, "artifacts": {}}

    monkeypatch.setitem(sys.modules, "transform_agents.fakeagent.agent", types.SimpleNamespace(handle=handle))
    client = LocalClient(overrides={"discovery": "fakeagent"})
    events = await drain(client.stream("discovery", AgentRequest(instruction="hi")))
    assert events[0]["summary"] == "hi"

    missing = await drain(LocalClient(overrides={"discovery": "nope"}).stream("discovery", AgentRequest()))
    assert missing[0]["type"] == "error" and "STAGE_OVERRIDE" in missing[0]["message"]


def test_local_client_reads_stage_override_env(monkeypatch):
    monkeypatch.setenv("STAGE_OVERRIDE", "discovery=hello")
    client = LocalClient()
    assert client.module_for("discovery") == "hello" and client.module_for("finops") == "finops"


class StubBody:
    def __init__(self, raw: bytes):
        self.raw = raw

    def iter_lines(self):
        yield from self.raw.splitlines()

    def read(self):
        return self.raw


class StubAgentCore:
    def __init__(self, content_type: str, raw: bytes):
        self.content_type, self.raw, self.kwargs = content_type, raw, {}

    def invoke_agent_runtime(self, **kwargs):
        self.kwargs = kwargs
        return {"contentType": self.content_type, "response": StubBody(self.raw)}


async def test_agentcore_client_parses_sse():
    sse = (b'data: {"type": "status", "stage": "discovery", "message": "agent started"}\n\n'
           b': keep-alive\n\n'
           b'data: {"type": "text", "delta": "hi"}\n\n'
           b'data: "{\\"type\\": \\"text\\", \\"delta\\": \\"double\\"}"\n\n'
           b'data: {"type": "result", "stage": "discovery", "summary": "s", "data": {}, "artifacts": {}}\n\n')
    stub = StubAgentCore("text/event-stream; charset=utf-8", sse)
    client = AgentCoreClient(arns={"discovery": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/discovery-x"},
                             boto_client=stub)
    events = await drain(client.stream("discovery", AgentRequest(run_id="r1", feedback="f")))
    assert [e["type"] for e in events] == ["status", "status", "text", "text", "result"]
    assert events[3]["delta"] == "double"
    assert len(stub.kwargs["runtimeSessionId"]) >= 33 and stub.kwargs["qualifier"] == "DEFAULT"
    assert json.loads(stub.kwargs["payload"])["feedback"] == "f"


async def test_agentcore_client_json_response_and_errors(monkeypatch):
    stub = StubAgentCore("application/json", json.dumps({"error": "boom", "error_type": "ValueError"}).encode())
    client = AgentCoreClient(arns={"discovery": "arn:x/runtime/d"}, boto_client=stub)
    events = await drain(client.stream("discovery", AgentRequest()))
    assert events[-1] == {"type": "error", "stage": "discovery", "message": "ValueError: boom"}

    monkeypatch.delenv("AGENTCORE_ARN_FINOPS", raising=False)
    missing = await drain(AgentCoreClient(arns={}, boto_client=stub).stream("finops", AgentRequest()))
    assert missing[0]["type"] == "error" and "AGENTCORE_ARN_FINOPS" in missing[0]["message"]

    class Failing:
        def invoke_agent_runtime(self, **kwargs):
            raise RuntimeError("ExpiredToken")

    failed = await drain(AgentCoreClient(arns={"discovery": "arn:x"}, boto_client=Failing()).stream("discovery",
                                                                                                    AgentRequest()))
    assert failed[-1]["type"] == "error" and "ExpiredToken" in failed[-1]["message"]


def test_parse_sse_line_ignores_non_data():
    assert parse_sse_line(b"event: ping", "s") is None and parse_sse_line("data:", "s") is None
    assert parse_sse_line("data: not json", "s") == {"type": "text", "delta": "not json"}


async def test_replay_client_replays_recorded_attempts(pipeline):
    await drain(pipeline.run_stage("discovery"))
    await drain(pipeline.revise("discovery", "tweak"))
    replay = ReplayClient(pipeline.dir, speed=1000)
    first = await drain(replay.stream("discovery", AgentRequest()))
    second = await drain(replay.stream("discovery", AgentRequest(feedback="tweak")))
    assert first[0]["replay"] is True
    assert "Revised" not in first[-1]["summary"] and "Revised" in second[-1]["summary"]


async def test_replay_client_falls_back_to_fixtures(tmp_path):
    result = fake_result("finops", AgentRequest())
    (tmp_path / "finops_result.json").write_text(json.dumps(result))
    replay = ReplayClient(None, fixtures_dir=tmp_path, speed=1000)
    events = await drain(replay.stream("finops", AgentRequest()))
    assert events[-1]["data"] == result["data"]
    assert {"tool_start", "tool_end", "artifact", "text"} <= {e["type"] for e in events}
    nothing = await drain(replay.stream("discovery", AgentRequest()))
    assert nothing[-1]["type"] == "error"


def test_synthesized_events_end_with_the_result():
    result = fake_result("modernization", AgentRequest())
    events = synthesize_events("modernization", result)
    assert events[-1]["type"] == "result" and events[0]["model"] == result["metrics"]["model"]


# ----------------------------------------------------------------------------- CLI


def test_cli_runs_headless_with_auto_approve(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_DELAY", "0")
    reqs = tmp_path / "reqs.md"
    reqs.write_text(REQS)
    assert cli_main(["--requirements", str(reqs), "--mode", "fake", "--auto-approve"]) == 0
    out = capsys.readouterr().out
    assert "✅ approved finops" in out and "finops=approved" in out
    run_id = Pipeline.list_runs(tmp_path)[0]

    # replay the recorded run headless
    assert cli_main(["--requirements", str(reqs), "--mode", "replay", "--replay-from", str(tmp_path / run_id),
                     "--auto-approve", "--until", "requirements"]) == 0
    assert "replaying" in capsys.readouterr().out


def test_cli_gate_prompts_without_auto_approve(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_DELAY", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO("r please add backups\na\nq\n"))
    assert cli_main(["--mode", "fake", "--until", "discovery"]) == 0
    out = capsys.readouterr().out
    assert "revising discovery: please add backups" in out and "discovery=approved" in out


def test_cli_reports_a_missing_requirements_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_DELAY", "0")
    assert cli_main(["--mode", "fake", "--auto-approve"]) == 2
    assert "upload the requirements file" in capsys.readouterr().out
