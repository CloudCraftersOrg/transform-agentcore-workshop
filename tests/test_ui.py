import asyncio
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from transform_agents.core import STAGES
from transform_agents.orchestrator import APPROVED, AWAITING, FAILED, FakeClient, Pipeline
from ui.bridge import StageJob
from ui.views import file_tree, language_for, zip_bytes

ROOT = Path(__file__).resolve().parents[1]

APP = str(ROOT / "ui" / "app.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("UI_ENABLE_FAKE", "1")
    monkeypatch.setenv("UI_MODE", "fake")
    monkeypatch.setenv("FAKE_DELAY", "0.005")
    monkeypatch.setenv("REPLAY_SPEED", "200")
    monkeypatch.delenv("STAGE_OVERRIDE", raising=False)
    return tmp_path


def app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def click(at: AppTest, key: str) -> AppTest:
    at.button(key=key).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def start(at: AppTest) -> Pipeline:
    click(at, "sample-welcome")
    click(at, "start")
    return at.session_state["pipeline"]


def keys(at: AppTest) -> set[str]:
    return {b.key for b in at.button if b.key}


def stepper(at: AppTest) -> str:
    return next(m.value for m in at.sidebar.markdown if "tx-step" in m.value)


def test_welcome_page(env):
    at = app()
    assert any("AWS Transform" in m.value and "AgentCore" in m.value for m in at.markdown)
    assert {"start", "sample-welcome"} <= keys(at)
    assert at.sidebar.radio[0].options == ["🖥️ Local (in-process)", "☁️ AgentCore Runtime", "⏯️ Replay (offline)",
                                           "🧪 Fake (demo data)"]
    assert stepper(at).count("tx-badge-pending") == 6


def test_start_and_approve_every_gate(env):
    at = app()
    p = start(at)
    assert p.status("discovery") == AWAITING
    assert p.requirements()[0] == "requirements/migration-requirements.md"
    assert {"approve", "request_changes"} <= keys(at)
    assert any(m.label == "🎯 Readiness" and m.value == "62/100" for m in at.metric)
    assert "tx-badge-awaiting_approval" in stepper(at)

    for stage in STAGES:
        assert p.status(stage) == AWAITING
        click(at, "approve")
        assert p.status(stage) == APPROVED
    assert any("Migration complete" in s.value for s in at.success)
    assert stepper(at).count("tx-badge-approved") == 6
    stage_entries = [e for e in at.session_state["transcript"] if e["kind"] == "stage"]
    assert [e["job"].stage for e in stage_entries] == STAGES
    assert any(m.label == "✨ Optimized" for m in at.metric)  # finops card rendered
    assert (p.dir / "workspace" / "reports" / "finops-report.md").exists()


def test_request_changes_form_revises_the_stage(env):
    at = app()
    p = start(at)
    click(at, "request_changes")
    at.text_area(key="feedback_text").input("Add a finding about missing backups")
    click(at, "send_feedback")
    st = p.state.stages["discovery"]
    assert st.attempts == 2 and st.status == AWAITING
    assert st.feedback[-1]["text"] == "Add a finding about missing backups"
    assert any(m.value == "✏️ Add a finding about missing backups" for m in at.markdown)
    assert any("revision 1" in m.value for m in at.markdown)


def test_chat_input_sends_feedback_to_the_stage_under_review(env):
    at = app()
    p = start(at)
    at.chat_input(key="chat").set_value("Use Graviton everywhere").run()
    assert not at.exception
    assert p.state.stages["discovery"].feedback[-1]["text"] == "Use Graviton everywhere"
    assert p.status("discovery") == AWAITING


def test_chat_before_start_explains_what_to_do(env):
    at = app()
    at.chat_input(key="chat").set_value("hello?").run()
    assert any("Start migration" in m.value for m in at.markdown)


def test_replay_mode_replays_a_recorded_run(env):
    recorded = Pipeline.create(FakeClient(), root=env)
    asyncio.run(_drain(recorded.run_stage("discovery")))

    at = app()
    at.sidebar.radio[0].set_value("replay").run()
    at.selectbox(key="replay_from").set_value(recorded.run_id).run()
    p = start(at)
    assert p.run_id != recorded.run_id and p.status("discovery") == AWAITING
    first_event = (p.dir / "discovery" / "events.jsonl").read_text().splitlines()[0]
    assert "replaying" in first_event
    assert p.data("discovery") == recorded.data("discovery")


def test_resume_rebuilds_the_transcript(env):
    old = Pipeline.create(FakeClient(), root=env)
    old.set_requirements("reqs.md", "# reqs")
    asyncio.run(_drain(old.run_stage("discovery")))
    old.approve("discovery")
    asyncio.run(_drain(old.run_stage("requirements")))
    asyncio.run(_drain(old.revise("requirements", "add a cost requirement")))

    at = app()
    at.selectbox(key="resume_id").set_value(old.run_id).run()
    click(at, "resume")
    p = at.session_state["pipeline"]
    assert p.run_id == old.run_id and p.status("requirements") == AWAITING
    jobs = [(e["job"].stage, e["job"].attempt) for e in at.session_state["transcript"] if e["kind"] == "stage"]
    assert jobs == [("discovery", 1), ("requirements", 1), ("requirements", 2)]
    assert "approve" in keys(at)
    click(at, "approve")
    assert p.status("modernization") == AWAITING


def test_failed_stage_offers_retry(env):
    at = app()
    at.session_state["client"] = FakeClient(fail={"discovery"})
    at.session_state["client_key"] = ("fake", None)
    p = start(at)
    assert p.status("discovery") == FAILED
    assert "retry" in keys(at) and any("ThrottlingException" in e.value for e in at.error)
    at.session_state["client"].fail.clear()
    click(at, "retry")
    assert p.status("discovery") == AWAITING


def test_requirements_can_be_provided_at_the_gate(env):
    at = app()
    click(at, "start")  # no requirements yet: discovery does not need them
    p = at.session_state["pipeline"]
    click(at, "approve")
    assert p.status("requirements") == "pending"
    assert any("Upload the migration requirements" in m.value for m in at.markdown)
    assert at.button(key="run").disabled
    click(at, "sample-gate")
    assert not at.button(key="run").disabled
    click(at, "run")
    assert p.status("requirements") == AWAITING
    assert "requirements/migration-requirements.md" in p.stage_request("requirements").files


# ----------------------------------------------------------------------------- bridge and helpers


async def _drain(agen):
    return [e async for e in agen]


def test_stage_job_runs_an_async_stream_in_the_background():
    async def stream():
        for i in range(3):
            await asyncio.sleep(0.01)
            yield {"type": "text", "delta": str(i)}
        yield {"type": "result", "summary": "ok"}

    job = StageJob("discovery", 1, stream=stream)
    assert job.wait(5) and job.done
    assert [e.get("delta") for e in job.events[:3]] == ["0", "1", "2"] and job.result["summary"] == "ok"


def test_stage_job_turns_exceptions_into_error_events():
    async def stream():
        yield {"type": "status", "message": "start"}
        raise RuntimeError("boom")

    job = StageJob("terraform", 1, stream=stream)
    job.wait(5)
    assert job.events[-1] == {"type": "error", "stage": "terraform", "message": "RuntimeError: boom"}
    assert StageJob("finops", 1, events=[{"type": "result"}]).done


def test_view_helpers():
    assert language_for("microservice/Dockerfile") == "docker"
    assert language_for("terraform/main.tf") == "hcl" and language_for("k8s/base/a.yaml") == "yaml"
    assert language_for("terraform/terraform.tfvars.example") == "hcl" and language_for("x.unknown") == "text"
    tree = file_tree(["microservice/app/main.py", "microservice/Dockerfile", "k8s/base/deployment.yaml"])
    assert tree.splitlines()[0] == "├── k8s/" and "│   └── base/" in tree and "└── Dockerfile" in tree
    names = zipfile.ZipFile(BytesIO(zip_bytes({"a/b.txt": "x", "c.md": "y"}))).namelist()
    assert names == ["a/b.txt", "c.md"]


def test_live_follow_path_with_a_slow_agent(env, monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "0.08")  # ~1.5s stage: the script is still rendering while events arrive
    at = app()
    p = start(at)
    job = at.session_state["job"]
    assert job.done and p.status("discovery") == AWAITING
    assert any(s.label == "🛠️ 1 tool call" for s in at.status)
