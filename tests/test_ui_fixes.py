"""UI review fixes: re-attach after a reload, prompt gate, failed-revision fallback, busy state, drift, replay default."""

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from transform_agents.core import STAGES
from transform_agents.orchestrator import APPROVED, AWAITING, RUNNING, FakeClient, Pipeline
from transform_agents.orchestrator import paths as orch_paths
from transform_agents.orchestrator.fakes import fake_result
from ui.bridge import RunRegistry, StageJob, run_registry

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "ui" / "app.py")


AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
           "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE")


def no_aws_credentials(monkeypatch) -> None:
    """The welcome screen depends on whether AWS credentials exist: make it the same on every machine."""
    for name in AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/aws/credentials")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws/config")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("UI_ENABLE_FAKE", "1")
    monkeypatch.setenv("UI_MODE", "fake")
    monkeypatch.setenv("FAKE_DELAY", "0.005")
    monkeypatch.setenv("REPLAY_SPEED", "200")
    monkeypatch.delenv("STAGE_OVERRIDE", raising=False)
    monkeypatch.setattr(orch_paths, "RECORDED_RUN", tmp_path / "no-recording")
    no_aws_credentials(monkeypatch)
    run_registry.clear()
    yield tmp_path
    run_registry.clear()


def app(query: dict[str, str] | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def click(at: AppTest, key: str) -> AppTest:
    at.button(key=key).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def keys(at: AppTest) -> set[str]:
    return {b.key for b in at.button if b.key}


def start(at: AppTest) -> Pipeline:
    click(at, "suggest-sample")  # the sample requirements, and the agents start straight away
    return at.session_state["pipeline"]


async def _drain(agen):
    return [e async for e in agen]


# ----------------------------------------------------------------------------- 2. reload re-attaches


def test_a_new_session_reattaches_to_the_live_job(env):
    p = run_registry().add(Pipeline.create(FakeClient(delay=0.05), root=env))  # what start_migration does
    statuses: list[str] = []
    job = run_registry().start(p.run_id, lambda: StageJob("discovery", 1, "", lambda: p.run_stage("discovery"),
                                                           run_id=p.run_id))
    while p.status("discovery") != RUNNING:
        time.sleep(0.01)
    statuses.append(json.loads((p.dir / "state.json").read_text())["stages"]["discovery"]["status"])
    assert not job.done

    at = app({"run": p.run_id})  # the browser reloaded: a new session with ?run=<id> in the URL
    assert at.session_state["pipeline"] is p and at.session_state["job"] is job
    assert job.done and p.status("discovery") == AWAITING  # followed live, never marked "interrupted"
    assert statuses == [RUNNING] and not p.state.stages["discovery"].error
    stages = [e["job"] for e in at.session_state["transcript"] if e["kind"] == "stage"]
    assert stages == [job] and "approve" in keys(at)
    assert any("Reconnected" in m.value for m in at.markdown)
    click(at, "approve")
    assert p.status("discovery") == APPROVED


def test_resume_of_a_live_run_reattaches_instead_of_failing_it(env):
    p = run_registry().add(Pipeline.create(FakeClient(delay=0.05), root=env))
    job = run_registry().start(p.run_id, lambda: StageJob("discovery", 1, "", lambda: p.run_stage("discovery"),
                                                           run_id=p.run_id))
    at = app()
    at.selectbox(key="resume_id").set_value(p.run_id).run()
    click(at, "resume")
    assert at.session_state["pipeline"] is p and job.done
    assert p.status("discovery") == AWAITING and not p.state.stages["discovery"].error


def test_start_url_and_new_run(env):
    at = app()
    p = start(at)
    assert at.query_params["run"] == [p.run_id] or at.query_params["run"] == p.run_id
    click(at, "keep_run")  # New run asks first: "Keep this run" changes nothing
    assert at.session_state["pipeline"] is p and at.session_state["transcript"]
    click(at, "new_run")
    assert at.session_state["pipeline"] is None and "run" not in at.query_params
    assert at.session_state["transcript"] == [] and "suggest-sample" in keys(at)  # back to the welcome screen


def test_registry_starts_one_job_per_run():
    reg = RunRegistry()
    release = threading.Event()

    async def blocking():
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "result"}

    first = reg.start("r1", lambda: StageJob("discovery", 1, stream=blocking))
    assert first is not None and reg.start("r1", lambda: StageJob("discovery", 2, stream=blocking)) is None
    assert reg.live() == [first]
    release.set()
    assert first.wait(5) and reg.live() == []
    assert reg.start("r1", lambda: StageJob("discovery", 2, events=[])) is not None


# ----------------------------------------------------------------------------- 3. the gate shows up promptly


def test_job_is_done_without_waiting_for_leftover_executor_threads():
    async def stream():
        asyncio.get_running_loop().run_in_executor(None, time.sleep, 4)  # a tool thread that lingers
        yield {"type": "result", "summary": "ok"}

    started = time.time()
    job = StageJob("discovery", 1, stream=stream)
    assert job.wait(3) and time.time() - started < 2 and job.result["summary"] == "ok"


# ----------------------------------------------------------------------------- 4. failed revision fallback


def test_failed_revision_offers_approve_previous_and_retry(env):
    at = app()
    p = start(at)
    at.session_state["client"].fail = {"discovery"}
    at.chat_input(key="chat").set_value("Add a finding about backups").run()
    assert p.status("discovery") == AWAITING and p.revision_failed("discovery")
    assert {"approve_previous", "retry", "request_changes"} <= keys(at) and "approve" not in keys(at)
    assert any("revision failed" in w.value and "ThrottlingException" in w.value for w in at.warning)

    at.session_state["client"].fail.clear()
    click(at, "retry")  # retries the same revision
    assert not p.revision_failed("discovery") and p.state.stages["discovery"].attempts == 3
    assert p.state.stages["discovery"].feedback[-1]["text"] == "Add a finding about backups"

    at.session_state["client"].fail = {"discovery"}
    at.chat_input(key="chat").set_value("One more change").run()
    click(at, "approve_previous")
    assert p.status("discovery") == APPROVED and p.status("requirements") == AWAITING


# ----------------------------------------------------------------------------- 10. nothing stale while busy


def test_no_gate_picker_or_change_path_while_an_agent_works(env):
    at = app()
    p = start(at)
    release = threading.Event()

    async def blocking():
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "status", "message": "done"}

    run_registry().start(p.run_id, lambda: StageJob("discovery", 9, stream=blocking, run_id=p.run_id))
    try:
        at.run()
        assert at.button(key="new_run").disabled and at.segmented_control(key="mode_seg").proto.disabled
        replay = at.toggle(key="replay_on")  # P1-11: the menu says why Replay can't be switched on now
        assert replay.disabled and replay.label == "Replay (available when no agent is running)"
        assert not at.get("file_uploader")  # no requirements picker (the agent may be reading them)
        assert not {"approve", "request_changes", "approve_previous"} & keys(at)  # no gate while busy
        chat = at.chat_input(key="chat")
        assert chat.proto.disabled and chat.placeholder == "The agent is working…"
        assert "tx-running tx-active" in next(m.value for m in at.sidebar.markdown if "tx-nav" in m.value)
    finally:
        release.set()
    time.sleep(0.2)
    at.run()
    assert not at.button(key="new_run").disabled and "approve" in keys(at)
    assert at.toggle(key="replay_on").label == "Replay a recorded run (offline)"


# ----------------------------------------------------------------------------- 11/14. result cards


def _render_job_with_error_and_result():
    from transform_agents.core import AgentRequest
    from transform_agents.orchestrator.fakes import fake_result
    from ui.bridge import StageJob
    from ui.views import StageView

    result = fake_result("k8s_validation", AgentRequest(options={"simulate_drift": True}))
    events = [{"type": "status", "message": "start"},
              {"type": "error", "message": "ThrottlingException: retrying"}, result]
    StageView(StageJob("k8s_validation", 1, events=events)).render_final()


def test_render_final_warns_when_a_result_comes_with_an_error():
    at = AppTest.from_function(_render_job_with_error_and_result)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("reported an error" in w.value and "ThrottlingException" in w.value for w in at.warning)
    assert not any("✅ Done" in m.value for m in at.markdown)
    assert any("Simulated drift" in m.value and "3 hand-edit violations" in m.value for m in at.markdown)


def test_drift_toggle_reaches_k8s_validation_only(env):
    at = app()
    toggle = at.toggle(key="simulate_drift_w")
    assert toggle.label == "Simulate manual drift (demo)" and "detect & fix" in toggle.help
    toggle.set_value(True).run()
    p = start(at)
    for _ in STAGES[:4]:
        click(at, "approve")
    assert p.status("k8s_validation") == AWAITING
    sent = {stage: req.options for stage, req in at.session_state["client"].calls}
    assert sent["k8s_validation"]["simulate_drift"] is True
    assert all("simulate_drift" not in o for s, o in sent.items() if s != "k8s_validation")
    assert any("Simulated drift" in m.value for m in at.markdown)


# ----------------------------------------------------------------------------- 7. replay default


def test_replay_mode_defaults_to_the_tracked_recording(env, monkeypatch):
    rec = Pipeline.create(FakeClient(), root=env / "samples", run_id="recorded-run")
    rec.set_requirements("reqs.md", "# r")
    asyncio.run(_drain(rec.run_stage("discovery")))
    monkeypatch.setattr(orch_paths, "RECORDED_RUN", rec.dir)

    at = app()
    at.toggle(key="replay_on").set_value(True).run()
    box = at.selectbox(key="replay_from")
    assert box.options[0] == "samples/recorded-run (tracked)" and box.value == "recorded"
    p = start(at)
    assert at.session_state["client"].source == rec.dir and "discovery" in at.session_state["client"].played
    assert p.data("discovery") == fake_result("discovery", rec.stage_request("discovery"))["data"]


# ----------------------------------------------------------------------------- final polish (UX review v2)

MENU_TOGGLES = ("live_aws", "live_deploy", "live_pricing", "simulate_drift", "show_bridges")


def mounted(toggle) -> bool:
    """What the browser shows when the "…" popover mounts a toggle afresh (it renders its body only when open):
    the value the server pushed in this run, else the widget default."""
    return toggle.proto.value if toggle.proto.set_value else toggle.proto.default


def bridges(at: AppTest) -> bool:
    return any("In AWS Transform" in m.value for m in at.markdown)


def test_opening_the_menu_keeps_aws_transform_notes_on(env):
    """N3: opening the "…" menu turned "Show AWS Transform notes" off (the toggle mounted at its default, False)."""
    at = app()
    start(at)
    assert at.session_state["show_bridges"] is True and bridges(at)
    for _ in range(3):  # open the menu (a rerun); the freshly mounted toggles report what they show
        at.run()
        for key in MENU_TOGGLES:
            assert mounted(at.toggle(key=f"{key}_w")) == bool(at.session_state[key]), key
        notes = at.toggle(key="show_bridges_w")
        notes.set_value(mounted(notes)).run()
        assert at.session_state["show_bridges"] is True and bridges(at)
    at.toggle(key="show_bridges_w").set_value(False).run()  # a deliberate change still sticks
    at.run()
    assert at.session_state["show_bridges"] is False and not bridges(at)
    assert mounted(at.toggle(key="show_bridges_w")) is False


def test_menu_options_follow_a_resumed_run(env):
    from transform_agents.orchestrator import Options

    old = Pipeline.create(FakeClient(), root=env, options=Options(live_deploy=True))
    at = app()
    at.selectbox(key="resume_id").set_value(old.run_id).run()
    click(at, "resume")
    at.run()
    assert at.session_state["live_deploy"] is True and mounted(at.toggle(key="live_deploy_w")) is True
    at.toggle(key="live_deploy_w").set_value(False).run()
    assert at.session_state["live_deploy"] is False and mounted(at.toggle(key="live_deploy_w")) is False


def test_replay_card_only_in_replay_or_without_credentials(env, monkeypatch):
    """N5: on a live welcome screen the Replay card was one click from Plan B, in front of the audience."""
    monkeypatch.setenv("UI_MODE", "local")
    at = app()  # live mode, no AWS credentials: a live run can't work, so Replay is offered
    assert "suggest-replay" in keys(at) and "suggest-legacy" not in keys(at)

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLEEXAMPLE00")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    at = app()  # live mode with credentials: a harmless third card instead
    assert "suggest-legacy" in keys(at) and "suggest-replay" not in keys(at)
    click(at, "suggest-legacy")
    copy = "\n".join(m.value for m in at.markdown)
    assert "Flask monolith" in copy and "single EC2 instance" in copy and "Hard-coded secrets" in copy
    assert "SQL injection in search" in copy and "11 deliberate smells" in copy
    assert at.session_state["mode"] == "local" and at.session_state["pipeline"] is None  # nothing switched

    at.toggle(key="replay_on").set_value(True).run()  # Replay already on: its card is back
    assert "suggest-replay" in keys(at) and "suggest-legacy" not in keys(at)


def test_ask_for_changes_dialog_focuses_its_textarea(env):
    """N7: the dialog carries a small fail-safe script that puts the cursor in the textarea."""
    at = app()
    start(at)
    click(at, "request_changes")
    scripts = [h.value for h in at.get("html")]
    assert any("[role=\"dialog\"] textarea" in v and ".focus(" in v for v in scripts)


def test_dialog_and_presenter_css():
    from ui.style import CSS, PRESENTER_CSS

    assert 'div[role="dialog"]' not in CSS  # N9: role="dialog" sits on a section element: never matched
    assert '[role="dialog"] [data-testid="stForm"]' in CSS
    offset = int(re.search(r"--tx-top-offset:\s*(\d+)px", PRESENTER_CSS).group(1))
    assert offset >= 96  # N2: nothing lands under the progress strip
    header = re.search(r'\[data-testid="stHeader"\]\s*\{([^}]*)\}', PRESENTER_CSS).group(1)
    assert "var(--tx-canvas)" in header and "height: var(--tx-band-h)" in header  # an opaque band, not glass
    gate = re.search(r'\[data-testid="stLayoutWrapper"\]:has\(> \.st-key-gate\)\s*\{([^}]*)\}', CSS).group(1)
    assert "bottom: var(--tx-chat-h)" in gate  # N1: flush on the bottom band, no gap to see through
