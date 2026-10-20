import asyncio
import re
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from transform_agents.core import STAGES
from transform_agents.orchestrator import APPROVED, AWAITING, FAILED, FakeClient, Pipeline
from ui import views
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
    no_aws_credentials(monkeypatch)
    return tmp_path


AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
           "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE")


def no_aws_credentials(monkeypatch) -> None:
    """The welcome screen depends on whether AWS credentials exist: make it the same on every machine."""
    for name in AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/aws/credentials")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws/config")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


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
    click(at, "suggest-sample")  # the first suggestion card: the sample requirements, and the agents start
    return at.session_state["pipeline"]


def keys(at: AppTest) -> set[str]:
    return {b.key for b in at.button if b.key}


def nav(at: AppTest) -> str:
    return next(m.value for m in at.sidebar.markdown if "tx-nav" in m.value)


def text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_welcome_page(env):
    at = app()
    assert "let's migrate your legacy app." in text(at) and "You approve every step." in text(at)
    assert {"start", "suggest-sample", "suggest-replay", "suggest-agents", "new_run", "resume"} <= keys(at)
    switch = at.segmented_control(key="mode_seg")
    assert switch.options == ["Local", "AgentCore"]
    assert switch.value is None and at.toggle(key="fake_on").value  # UI_MODE=fake lives in the "…" menu
    assert not at.toggle(key="replay_on").value and at.toggle(key="show_bridges_w").value
    assert nav(at).count("Not started") == 6 and "Nova 2 Lite" in nav(at) and "DeepSeek V3.2" in nav(at)
    assert not at.chat_input  # the chat bar is hidden until a run exists
    copy = text(at)
    assert "transparent re-creation of the loop AWS Transform's agents run" in copy
    assert "AWS Transform ×" not in copy and "rebuilt from AWS Transform" not in copy and "Mistral" not in copy


def test_the_shell_has_no_emoji():
    emoji = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
    for name in ("app.py", "style.py"):
        source = (ROOT / "ui" / name).read_text()
        assert not emoji.findall(source), (name, emoji.findall(source))


def test_css_survives_the_html_sanitizer():
    from ui.style import CSS, PRESENTER_CSS

    # st.html runs DOMPurify, which drops a whole <style> block whose text contains "<" + a letter
    for css in (CSS, PRESENTER_CSS, getattr(views, "CSS", "<style></style>")):
        assert css.count("<style>") == css.count("</style>") >= 1
        for body in css.split("<style>")[1:]:
            assert not re.search(r"<[a-zA-Z/!]", body.split("</style>")[0]), body[:80]


def test_six_agents_dialog_and_replay_suggestion(env):
    at = app()
    click(at, "suggest-agents")
    assert "Inventory the EC2 box" in text(at) and "In AWS Transform: Test &amp; validation" in text(at)
    click(at, "suggest-replay")
    assert at.session_state["mode"] == "replay" and "Recorded run" in text(at)
    assert at.session_state["pipeline"] is None  # switching to Replay does not start anything


def test_start_and_approve_every_gate(env):
    at = app()
    p = start(at)
    assert p.status("discovery") == AWAITING
    assert p.requirements()[0] == "requirements/migration-requirements.md"
    assert {"approve", "request_changes"} <= keys(at)
    assert at.button(key="approve").label == "Approve and continue"
    assert at.button(key="request_changes").label == "Ask for changes"
    assert "**Step 1 of 6 is ready for review.** Next: requirements." in text(at)
    assert any(m.label.endswith("Readiness") and m.value == "62/100" for m in at.metric)
    assert "tx-awaiting_approval tx-active" in nav(at) and "Needs review" in nav(at)
    assert any(".st-key-stage-" in h.value and "tx-scroll" in h.value for h in at.get("html"))  # result in view
    assert any("var sel = " in h.value and "land = true" in h.value for h in at.get("html"))  # above the gate

    for stage in STAGES:
        assert p.status(stage) == AWAITING
        click(at, "approve")
        assert p.status(stage) == APPROVED
    assert "approve" not in keys(at) and nav(at).count("tx-nav tx-approved") == 6
    if not hasattr(views, "render_finale"):
        assert "Migration complete" in text(at)
    assert at.chat_input(key="chat").proto.disabled
    stage_entries = [e for e in at.session_state["transcript"] if e["kind"] == "stage"]
    assert [e["job"].stage for e in stage_entries] == STAGES
    assert any("Optimized" in m.label for m in at.metric)  # finops card rendered
    assert (p.dir / "workspace" / "reports" / "finops-report.md").exists()


def test_ask_for_changes_dialog_revises_the_stage(env):
    at = app()
    p = start(at)
    click(at, "request_changes")  # opens the dialog
    assert at.session_state["show_feedback"]
    click(at, "send_feedback")  # empty: nothing is sent, the dialog stays with a hint
    assert p.state.stages["discovery"].attempts == 1 and at.session_state["show_feedback"]
    at.text_area(key="feedback_text").input("Add a finding about missing backups")
    click(at, "send_feedback")
    st = p.state.stages["discovery"]
    assert st.attempts == 2 and st.status == AWAITING and not at.session_state["show_feedback"]
    assert st.feedback[-1]["text"] == "Add a finding about missing backups"
    assert any(m.value == "Add a finding about missing backups" for m in at.markdown)
    assert any("revision 1" in m.value for m in at.markdown)


def test_chat_input_sends_feedback_to_the_stage_under_review(env):
    at = app()
    p = start(at)
    assert at.chat_input(key="chat").placeholder == "Ask for changes to Discovery…"
    at.chat_input(key="chat").set_value("Use Graviton everywhere").run()
    assert not at.exception
    assert p.state.stages["discovery"].feedback[-1]["text"] == "Use Graviton everywhere"
    assert p.status("discovery") == AWAITING


def test_mode_switch_and_plan_b_toggles(env):
    at = app()
    assert at.session_state["mode"] == "fake"
    at.segmented_control(key="mode_seg").set_value("agentcore").run()
    assert at.session_state["mode"] == "agentcore" and not at.toggle(key="fake_on").value
    at.toggle(key="replay_on").set_value(True).run()
    assert at.session_state["mode"] == "replay" and at.segmented_control(key="mode_seg").value is None
    assert "Recorded run" in text(at)
    at.toggle(key="replay_on").set_value(False).run()  # back to the live mode it came from
    assert at.session_state["mode"] == "agentcore" and "Recorded run" not in text(at)
    at.segmented_control(key="mode_seg").set_value("local").run()
    assert at.session_state["mode"] == "local"


def test_presenter_mode(env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.query_params["presenter"] = "1"
    at.run()
    assert not at.exception and at.session_state["presenter"] is True
    assert any("tx-strip" in m.value for m in at.markdown)
    p = start(at)
    strip = next(m.value for m in at.markdown if "tx-strip" in m.value)
    assert strip.count("tx-current") == 1 and strip.count("tx-pending") == 5
    click(at, "approve")
    strip = next(m.value for m in at.markdown if "tx-strip" in m.value)
    assert strip.count("tx-approved") == 1 and p.status("requirements") == AWAITING
    at.run()  # AppTest keeps the pre-st.rerun copy of the New run button until the next run
    click(at, "new_run")
    assert at.query_params["presenter"] in ("1", ["1"]) and "run" not in at.query_params


def test_show_aws_transform_notes_toggle(env):
    at = app()
    assert at.session_state["show_bridges"] is True
    toggle = at.toggle(key="show_bridges_w")  # the widget mirrors the state key views.py reads
    assert toggle.label == "Show AWS Transform notes"
    toggle.set_value(False).run()
    assert at.session_state["show_bridges"] is False and not at.toggle(key="show_bridges_w").value
    start(at)
    assert not any("In AWS Transform" in m.value for m in at.markdown)  # no bridge strip under the result
    at.toggle(key="show_bridges_w").set_value(True).run()
    assert any("In AWS Transform" in m.value for m in at.markdown)


def test_replay_mode_replays_a_recorded_run(env):
    recorded = Pipeline.create(FakeClient(), root=env)
    asyncio.run(_drain(recorded.run_stage("discovery")))

    at = app()
    at.toggle(key="replay_on").set_value(True).run()
    at.selectbox(key="replay_from").set_value(recorded.run_id).run()
    p = start(at)
    assert "Recorded run" in text(at)
    assert p.run_id != recorded.run_id and p.status("discovery") == AWAITING
    assert at.session_state["client"].source == recorded.dir  # replayed from the picked run, not a live agent
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
    assert "Step 1 of 6 failed" in "\n".join(e.value for e in at.error)
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
    assert any("1 tool call" in s.label or "1 step" in s.label for s in at.status)
