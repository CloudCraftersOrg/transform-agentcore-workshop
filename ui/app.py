"""Transform × AgentCore workshop: upload the requirements, watch each agent work, approve or ask for changes.

Run from the repo root: `make ui` (= uv run streamlit run ui/app.py). Theme: ui/.streamlit/config.toml; CSS and
design tokens: ui/style.py. Add `?presenter=1` to the URL for presenter mode (bigger type, no sidebar, no raw JSON).
"""

import os
import sys
from pathlib import Path

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

if (repo := str(Path(__file__).resolve().parents[1])) not in sys.path:
    sys.path.insert(0, repo)  # make the `ui` package importable when Streamlit runs this file as a script

from transform_agents.core import STAGES
from transform_agents.core.config import DEFAULT_MODELS, model_id_for
from transform_agents.orchestrator import (
    APPROVED,
    AWAITING,
    FAILED,
    PENDING,
    RUNNING,
    AgentClient,
    Options,
    Pipeline,
    make_client,
    recorded_runs,
    resolve_replay_source,
)
from transform_agents.orchestrator.clients import is_complete_run, load_arns
from transform_agents.orchestrator.paths import RECORDED_RUN, ROOT, SAMPLE_REQUIREMENTS, runs_dir
from ui import views
from ui.bridge import StageJob, run_registry
from ui.style import (
    CSS,
    FOCUS_JS,
    NAV_JS,
    PRESENTER_CSS,
    STAGE_META,
    agents_html,
    brand_html,
    css_block,
    legacy_html,
    nav_html,
    presenter_html,
    progress_html,
    scroll_html,
    suggestion_html,
)
from ui.views import StageView, follow, zip_bytes

LIVE_MODES = ("local", "agentcore")  # the top-bar switch; Replay (and Fake) live in the "…" menu
MODE_LABELS = {"local": ":material/laptop_mac: Local", "agentcore": ":material/cloud: AgentCore",
               "replay": "Replay (offline)", "fake": "Fake (demo data)"}
FAKE_ENABLED = os.getenv("UI_ENABLE_FAKE") == "1"
MODES = [*LIVE_MODES, "replay"] + (["fake"] if FAKE_ENABLED else [])
NEXT_STEP = {"discovery": "requirements", "requirements": "code modernization",
             "modernization": "infrastructure as code", "terraform": "Kubernetes validation",
             "k8s_validation": "FinOps right-sizing", "finops": "the wrap-up"}
USER_AVATAR, GUIDE_AVATAR = ":material/person:", ":material/explore:"

PRESENTER = st.query_params.get("presenter") == "1"
st.set_page_config(page_title="Transform × AgentCore workshop", page_icon=":material/conversion_path:",
                   layout="wide", initial_sidebar_state="collapsed" if PRESENTER else "expanded")
st.html(CSS + css_block(getattr(views, "CSS", "")) + (PRESENTER_CSS if PRESENTER else ""))  # style only: no layout slot
ss = st.session_state
REG = run_registry()  # process-wide pipelines and live jobs: a page reload (new session) re-attaches to them
ss.setdefault("transcript", [])  # [{"kind": "user" | "note", "text"} | {"kind": "stage", "job": StageJob}]
ss.setdefault("pipeline", None)  # the same object as REG's pipeline for that run id
ss.setdefault("job", None)  # this run's latest StageJob (also in REG)
ss.setdefault("requirements", None)  # (filename, text)
ss.setdefault("show_feedback", False)  # the "Ask for changes" dialog is open
ss.setdefault("mode", os.getenv("UI_MODE", "local") if os.getenv("UI_MODE", "local") in MODES else "local")
ss.setdefault("live_mode", ss.mode if ss.mode in LIVE_MODES else "local")  # where the Replay switch returns to
ss.setdefault("show_bridges", True)  # views.py: the "In AWS Transform" strip under each result
for _key in ("live_aws", "live_deploy", "live_pricing", "simulate_drift"):  # the live options (OPTION_KEYS)
    ss.setdefault(_key, False)
ss.setdefault("scrolled", [])  # "<job id>:<phase>" already scrolled into view (each happens once)
ss.presenter = PRESENTER  # views.py hides raw JSON and telemetry in presenter mode


# --------------------------------------------------------------------------- state helpers


def pipeline() -> Pipeline | None:
    return ss.pipeline


def live_job() -> StageJob | None:
    """The current run's job (from the registry, so it survives reloads)."""
    p = pipeline()
    return REG.job(p.run_id) if p else None


def busy() -> bool:
    job = live_job()
    return job is not None and not job.done


def note(text: str) -> None:
    ss.transcript.append({"kind": "note", "text": text})


def recording_available() -> bool:
    return (RECORDED_RUN / "state.json").exists()


def replay_source() -> Path | None:
    """recorded = samples/recorded-run (the default when present), fixtures = tests/fixtures, else runs/<id>."""
    choice = ss.get("replay_from") or ("recorded" if recording_available() else "fixtures")
    if choice in ("recorded", "fixtures"):
        return resolve_replay_source(choice)
    return runs_dir() / choice


def current_client() -> AgentClient:
    """One client per (mode, replay source), reused so a replay can follow recorded revisions."""
    key = (ss.mode, ss.get("replay_from"))
    if ss.get("client_key") != key:
        ss.client, ss.client_key = make_client(ss.mode, replay_from=replay_source()), key
    return ss.client


OPTION_KEYS = ("live_aws", "live_deploy", "live_pricing", "simulate_drift")  # "…" menu toggles = Options fields
MENU_TOGGLES = (*OPTION_KEYS, "show_bridges")  # state keys; the "…" menu toggle of each one is the widget "<key>_w"


def current_options() -> Options:
    return Options(**{key: bool(ss.get(key, False)) for key in OPTION_KEYS})


def sync_menu_toggles() -> None:
    """The "…" menu toggles mirror their state keys on every run. The popover mounts its body only when it opens, and
    a toggle whose value was never pushed to the browser came up at its widget default (off): opening the menu
    turned "Show AWS Transform notes" off. Pushing the value every run (like sync_mode_widgets) prevents that."""
    for key in MENU_TOGGLES:
        ss[f"{key}_w"] = bool(ss.get(key, False))


def on_menu_toggle(key: str) -> None:
    ss[key] = bool(ss.get(f"{key}_w"))


AWS_CREDENTIAL_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                      "AWS_DEFAULT_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
                      "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                      "AWS_WEB_IDENTITY_TOKEN_FILE")


@st.cache_data(ttl=300, show_spinner=False)
def _aws_credentials(env: tuple[str, ...]) -> bool:
    """botocore's credential chain without the slow or side-effecting providers (instance metadata, processes)."""
    try:
        import botocore.session

        session = botocore.session.get_session()
        resolver = session.get_component("credential_provider")
        for name in ("iam-role", "custom-process"):
            resolver.remove(name)
        return session.get_credentials() is not None
    except Exception:  # noqa: BLE001 - unknown means "no": the Replay card stays on the welcome screen
        return False


def aws_credentials_available() -> bool:
    """Whether a live run could sign its Bedrock calls (environment, profile, SSO or container credentials)."""
    return _aws_credentials(tuple(os.getenv(k, "") for k in AWS_CREDENTIAL_ENV))


def store_requirements(name: str, text: str) -> None:
    ss.requirements = (name, text)
    if pipeline():
        pipeline().set_requirements(name, text)


def model_label(stage: str) -> str:
    p = pipeline()
    if p and (model := p.state.stages[stage].metrics.get("model")):
        return model
    module = current_client().module_for(stage) if ss.mode == "local" else stage
    return model_id_for(module if module in DEFAULT_MODELS else stage)


def stage_status(stage: str) -> str:
    """The stage of the active job shows as running before its thread has even started."""
    p, job = pipeline(), live_job()
    if job is not None and not job.done and job.stage == stage:
        return RUNNING
    return p.status(stage) if p else PENDING


def active_stage() -> str | None:
    p, job = pipeline(), live_job()
    if job is not None and not job.done:
        return job.stage
    return p.next_stage() if p else None


def last_stage_job() -> StageJob | None:
    return next((e["job"] for e in reversed(ss.transcript) if e["kind"] == "stage"), None)


# --------------------------------------------------------------------------- actions (button callbacks)


def start_stage(stage: str, feedback: str | None = None) -> None:
    """Start one stage attempt on a background thread; the transcript renders it live."""
    p = pipeline()
    if p is None or busy():
        return
    p.client, p.state.options = current_client(), current_options()
    try:
        p.check_runnable(stage)
    except ValueError as exc:
        note(f"I can't start that step yet: {exc}")
        return
    attempt = p.state.stages[stage].attempts + 1
    stream = (lambda: p.revise(stage, feedback)) if feedback else (lambda: p.run_stage(stage))
    job = REG.start(p.run_id, lambda: StageJob(stage, attempt, feedback or "", stream, run_id=p.run_id))
    if job is None:  # another browser session started one a moment ago
        note("An agent is already working on this run.")
        return
    ss.job = job
    ss.transcript.append({"kind": "stage", "job": job})
    ss.show_feedback = False


def start_migration() -> None:
    ss.pipeline = REG.add(Pipeline.create(current_client(), options=current_options()))
    st.query_params["run"] = ss.pipeline.run_id  # a page reload re-attaches to this run
    if ss.requirements:
        ss.pipeline.set_requirements(*ss.requirements)
    with_file = f" with `{ss.requirements[0]}`" if ss.requirements else ""
    ss.transcript = [{"kind": "user", "text": f"Start the migration{with_file}"}]
    ss.scrolled = []
    start_stage("discovery")


def start_with_sample() -> None:
    """The first suggestion card: the sample requirements, and the agents start straight away."""
    if busy():
        return
    use_sample()
    if pipeline() is None:
        start_migration()


def switch_to_replay() -> None:
    if busy():
        return
    if ss.mode in LIVE_MODES:
        ss.live_mode = ss.mode
    ss.mode = "replay"
    ss.toast = "Replay is on. The agents will replay the recorded run, offline."


def approve(stage: str) -> None:
    p = pipeline()
    try:
        nxt = p.approve(stage)
    except ValueError as exc:
        note(f"I couldn't approve that: {exc}")
        return
    ss.transcript.append({"kind": "user", "text": f"Approve **{STAGE_META[stage].title}** and continue"})
    if nxt == "requirements" and not p.requirements():
        note("Upload the migration requirements (or use the sample) to run the **Requirements** agent.")
    elif nxt:
        start_stage(nxt)


def request_changes(stage: str, text: str) -> None:
    text = text.strip()
    if text:
        ss.transcript.append({"kind": "user", "text": text})
        start_stage(stage, text)


def send_feedback_form(stage: str) -> None:
    text = ss.get("feedback_text", "")
    if not text.strip():
        ss.feedback_hint = True
    request_changes(stage, text)


def open_feedback() -> None:
    ss.show_feedback = True


def close_feedback() -> None:
    ss.show_feedback = False


def on_chat() -> None:
    """The chat bar sends free-text feedback to the stage under review (= revise), like the dialog."""
    text, p = (ss.get("chat") or "").strip(), pipeline()
    if not text:
        return
    stage = p.next_stage() if p else None
    if p is None:
        ss.transcript.append({"kind": "user", "text": text})
        note("Start a run first: pick the sample requirements, or drop your own and press **Start migration**.")
    elif busy():
        ss.transcript.append({"kind": "user", "text": text})
        note("An agent is still working. Send your feedback when it reaches the review step.")
    elif stage and p.status(stage) in (AWAITING, FAILED):
        request_changes(stage, text)
    else:
        ss.transcript.append({"kind": "user", "text": text})
        note("Nothing is waiting for review right now.")


def retry(stage: str) -> None:
    ss.transcript.append({"kind": "user", "text": f"Retry **{STAGE_META[stage].title}**"})
    start_stage(stage, pipeline().retry_feedback(stage))


def run_pending(stage: str) -> None:
    ss.transcript.append({"kind": "user", "text": f"Run **{STAGE_META[stage].title}**"})
    start_stage(stage)


def use_sample() -> None:
    if not busy():
        store_requirements(SAMPLE_REQUIREMENTS.name, SAMPLE_REQUIREMENTS.read_text())


def new_run() -> None:
    ss.pipeline, ss.job, ss.transcript, ss.show_feedback, ss.scrolled = None, None, [], False, []
    if "run" in st.query_params:
        del st.query_params["run"]  # keeps ?presenter=1
    ss.new_run_menu = False  # close the confirmation popover


def keep_run() -> None:
    ss.new_run_menu = False


def on_mode_switch() -> None:
    choice = ss.get("mode_seg")
    if choice in LIVE_MODES:  # None = the selected segment was clicked again: keep the mode
        ss.mode = ss.live_mode = choice


def on_replay_toggle() -> None:
    ss.mode = "replay" if ss.get("replay_on") else ss.live_mode


def on_fake_toggle() -> None:
    ss.mode = "fake" if ss.get("fake_on") else ss.live_mode


def rebuild_transcript(p: Pipeline, job: StageJob | None, header: str) -> list[dict]:
    """The chat of a run from its recorded attempts; the attempt still running is the live job itself."""
    transcript: list[dict] = [{"kind": "user", "text": header}]
    live = job if job is not None and not job.done else None
    for stage in STAGES:
        for attempt, feedback, events in p.attempts(stage):
            if feedback:
                transcript.append({"kind": "user", "text": feedback})
            if live and (live.stage, live.attempt) == (stage, attempt):
                transcript.append({"kind": "stage", "job": live})
            elif p.status(stage) == RUNNING and attempt == p.state.stages[stage].attempts:
                transcript.append({"kind": "note", "text": f"**{STAGE_META[stage].title}** is still running in "
                                                           "another process (e.g. the CLI)."})
            else:
                transcript.append({"kind": "stage", "job": StageJob(stage, attempt, feedback, events=events)})
        if p.status(stage) == APPROVED:
            transcript.append({"kind": "user", "text": f"Approve **{STAGE_META[stage].title}** and continue"})
    if live and not any(e.get("job") is live for e in transcript):  # its thread has not started the stage yet
        transcript.append({"kind": "stage", "job": live})
    return transcript


def open_run(run_id: str, header: str) -> None:
    """Show a run in this session: the live in-process Pipeline (and its running job) if there is one, else
    runs/<id> loaded from disk. Never reloads a run whose agent is still working, which would corrupt it."""
    try:
        p = REG.pipeline(run_id, lambda: Pipeline.load(run_id, current_client()))
    except (OSError, ValueError, KeyError, TypeError) as exc:  # a missing or hand-edited state.json
        note(f"I can't open run `{run_id}`: {type(exc).__name__}: {exc}")
        return
    job = REG.job(run_id)
    ss.pipeline, ss.job, ss.show_feedback, ss.scrolled = p, job, False, []
    for key in OPTION_KEYS:
        ss[key] = bool(getattr(p.state.options, key, False))
    if req := p.requirements():
        ss.requirements = (Path(req[0]).name, req[1])
    ss.transcript = rebuild_transcript(p, job, header)
    st.query_params["run"] = run_id


def resume() -> None:
    """Load runs/<id> (or re-attach to it if it is live in this server) and rebuild the chat."""
    run_id = ss.get("resume_id")
    if not run_id or busy():
        return
    open_run(run_id, f"Resume run `{run_id}`")
    ss.more_menu = False


def attach_from_url() -> None:
    """After a page reload (a new session) the URL still names the run: re-attach to it, live job included."""
    run_id = st.query_params.get("run")
    if ss.pipeline is not None or not run_id or ss.get("attached") == run_id:
        return
    ss.attached = run_id  # once per session, even if the run is gone
    if (runs_dir() / run_id / "state.json").exists():
        open_run(run_id, f"Reconnected to run `{run_id}`")


def refresh_from_disk() -> None:
    """Re-read a run another process is driving (nothing runs here)."""
    p = pipeline()
    if p and not busy():
        REG.add(Pipeline.load(p.run_id, current_client()))
        open_run(p.run_id, f"Refreshed run `{p.run_id}`")


# --------------------------------------------------------------------------- scrolling


def scroll_once(target: str, phase: str, slot: DeltaGenerator | None = None, land: bool = False) -> None:
    """Scroll `target` (a CSS selector, or "top") into view once per phase. Streamlit only follows new content when
    the view is exactly at the bottom, so new stages would otherwise land off-screen. land=True (a finished result)
    keeps its header, headline and KPIs above the sticky gate. Fails safe."""
    if phase in ss.scrolled:
        return
    ss.scrolled.append(phase)
    try:
        (slot or st).html(scroll_html(target, phase, land=land), unsafe_allow_javascript=True)
    except Exception:  # noqa: BLE001 - a missing scroll must never break the page
        pass


# --------------------------------------------------------------------------- page sections


def paint_nav(slot: DeltaGenerator) -> None:
    p, active = pipeline(), active_stage()
    anchors = {}
    for entry in ss.transcript:  # the latest attempt of each stage, for the nav links
        if entry["kind"] == "stage":
            anchors[entry["job"].stage] = f"stage-{entry['job'].id}"
    rows = [(s, stage_status(s), model_label(s), s == active, anchors.get(s, "") if p else "") for s in STAGES]
    slot.markdown(nav_html(rows), unsafe_allow_html=True)


def paint_strip(slot: DeltaGenerator | None) -> None:
    """Presenter mode: ● Discovery ─ ● Requirements ─ ◐ Modernization ─ ○ …, drawn in CSS."""
    if slot is None:
        return
    active, rows = active_stage(), []
    for s in STAGES:
        status = stage_status(s)
        state = ("approved" if status == APPROVED else "failed" if status == FAILED and s == active
                 else "current" if s == active else "pending")
        rows.append((s, state))
    slot.markdown(progress_html(rows), unsafe_allow_html=True)


def sidebar() -> DeltaGenerator:
    """The named-agent nav (repainted once the page is done) and, optionally, the presenter's name."""
    with st.sidebar:
        nav = st.empty()
        paint_nav(nav)
        if name := os.getenv("PRESENTER_NAME", "").strip():
            st.markdown(presenter_html(name), unsafe_allow_html=True)
        try:
            st.html(NAV_JS, unsafe_allow_javascript=True)  # same body every run: it executes once per page
        except Exception:  # noqa: BLE001 - without it the nav still shows every status
            pass
    return nav


def sync_mode_widgets() -> None:
    """ss.mode is the single source of truth; the top-bar switch and the menu toggles mirror it."""
    ss.mode_seg = ss.mode if ss.mode in LIVE_MODES else None
    ss.replay_on = ss.mode == "replay"
    ss.fake_on = ss.mode == "fake"
    sync_menu_toggles()


def new_run_menu() -> None:
    p, working = pipeline(), busy()
    with st.popover("New run", icon=":material/add:", type="primary", key="new_run_menu", on_change="rerun",
                    disabled=p is None, help="Start over with a fresh run"):
        st.markdown("**Start a new run?**")
        st.caption("This clears the conversation on screen. The current run stays saved, and you can resume it "
                   "from the menu next to this button.")
        with st.container(horizontal=True, gap="small"):
            st.button("Start a new run", key="new_run", type="primary", on_click=new_run, disabled=working)
            st.button("Keep this run", key="keep_run", on_click=keep_run)


def menu_toggle(label: str, key: str, tip: str | None = None) -> None:
    """A "…" menu toggle: the widget "<key>_w" mirrors ss[key] (sync_menu_toggles) and writes it back on change."""
    st.toggle(label, key=f"{key}_w", on_change=on_menu_toggle, args=(key,), help=tip)


def more_menu() -> None:
    p, working = pipeline(), busy()
    with st.popover("", icon=":material/more_horiz:", key="more_menu", on_change="rerun",
                    help="Runs, live options and Plan B"):
        st.markdown('<div class="tx-menu-h">This run</div>', unsafe_allow_html=True)
        if p:
            where = p.dir.relative_to(ROOT) if p.dir.is_relative_to(ROOT) else p.dir
            st.caption(f"Run `{p.run_id}` · open `{where}/workspace` in your IDE")
            if merged := p.merged_artifacts():
                st.download_button("Download all artifacts (.zip)", zip_bytes(merged), on_click="ignore",
                                   icon=":material/download:", file_name=f"{p.run_id}-artifacts.zip",
                                   mime="application/zip", key="zip-all", width="stretch")
        st.selectbox("Resume a run", Pipeline.list_runs(), index=None, key="resume_id", placeholder="Select a run…")
        st.button("Resume", key="resume", icon=":material/history:", on_click=resume,
                  disabled=working or not ss.get("resume_id"), width="stretch")

        st.markdown('<div class="tx-menu-h">Live options</div>', unsafe_allow_html=True)
        menu_toggle("Live AWS discovery (EC2 + CloudWatch)", "live_aws")
        menu_toggle("Live deploy to kind", "live_deploy")
        menu_toggle("Simulate manual drift (demo)", "simulate_drift",
                    "Injects typical hand-edit violations before validation so the agent shows detect & fix "
                    "(K8s Validation stage only).")
        menu_toggle("Live AWS pricing", "live_pricing")
        menu_toggle("Show AWS Transform notes", "show_bridges",
                    "Under each result, how that step maps to AWS Transform and what it buys you.")
        if ss.mode == "local" and (overrides := current_client().modules):
            st.caption("Stand-ins: " + ", ".join(f"{k} → {v}" for k, v in overrides.items()))
        elif ss.mode == "agentcore":
            arns = load_arns()
            found = sum(s in arns for s in STAGES)
            st.caption(f"{found}/{len(STAGES)} AgentCore runtime ARNs found "
                       "(deploy/agent-arns.json or AGENTCORE_ARN_<STAGE>)")

        st.markdown('<div class="tx-menu-h">Plan B</div>', unsafe_allow_html=True)
        # switching mid-stage would orphan the running agent, so it waits; WORKSHOP.md has the fresh-tab fallback
        st.toggle("Replay (available when no agent is running)" if working else "Replay a recorded run (offline)",
                  key="replay_on", on_change=on_replay_toggle, disabled=working)
        if ss.mode == "replay":
            runs = [d for d in recorded_runs() if not p or d.name != p.run_id]
            complete = {d.name for d in runs if is_complete_run(d)}
            sources = (["recorded"] if recording_available() else []) + ["fixtures"] + [d.name for d in runs]
            labels = {"recorded": "samples/recorded-run (tracked)", "fixtures": "tests/fixtures"}
            st.selectbox("Replay from", sources, key="replay_from", disabled=working,
                         format_func=lambda s: labels.get(s) or f"runs/{s}{' · complete' if s in complete else ''}")
        if FAKE_ENABLED:
            st.toggle("Fake agents (demo data)", key="fake_on", on_change=on_fake_toggle, disabled=working)


def topbar() -> DeltaGenerator | None:
    """Title (+ the Recorded run chip), the Local · AgentCore switch, New run and the "…" menu. Returns the
    presenter progress strip slot (repainted once the page is done), or None outside presenter mode."""
    sync_mode_widgets()
    with st.container(key="topbar"):
        with st.container(key="tb-row", horizontal=True, vertical_alignment="center", gap="small"):
            with st.container(key="tb-left", gap=None):  # the title, and under it the Recorded run chip
                st.markdown(brand_html(), unsafe_allow_html=True, width="content")
                if ss.mode == "replay":  # honest Plan B: say it is a recording
                    with st.container(key="recorded-chip", width="content"):
                        st.markdown(":material/history: Recorded run")
                elif ss.mode == "fake":
                    with st.container(key="fake-chip", width="content"):
                        st.markdown(":material/science: Demo data")
            with st.container(key="tb-mid", horizontal=True, horizontal_alignment="center"):
                st.segmented_control("Agent runtime", list(LIVE_MODES), key="mode_seg", format_func=MODE_LABELS.get,
                                     on_change=on_mode_switch, disabled=busy(), label_visibility="collapsed")
            with st.container(key="tb-right", horizontal=True, horizontal_alignment="right",
                              vertical_alignment="center", gap="small"):
                new_run_menu()
                more_menu()
        if not PRESENTER:
            return None
        strip = st.empty()
        paint_strip(strip)  # now, so it shows while an agent works (the script then blocks in follow())
        return strip


def take_upload(upload, key: str) -> None:
    if upload is not None and not busy() and ss.get(f"upload-id-{key}") != upload.file_id:
        ss[f"upload-id-{key}"] = upload.file_id
        store_requirements(upload.name, upload.getvalue().decode("utf-8", errors="replace"))


def requirements_ready() -> None:
    if ss.requirements:
        name, text = ss.requirements
        with st.expander(f"Requirements ready: `{name}` · {len(text.splitlines())} lines",
                         icon=":material/description:"):
            st.markdown(text)


def requirements_picker(key: str) -> None:
    """Upload or pick the sample; disabled while an agent works (the requirements stage may be reading them)."""
    working = busy()
    left, right = st.columns([3, 1.3], vertical_alignment="bottom")
    upload = left.file_uploader("Migration requirements (.md / .txt)", type=["md", "txt", "markdown"],
                                key=f"upload-{key}", disabled=working)
    right.button("Use sample requirements", key=f"sample-{key}", on_click=use_sample, width="stretch",
                 icon=":material/description:", disabled=working)
    take_upload(upload, key)
    requirements_ready()


@st.dialog("What the six agents do", width="medium", icon=":material/groups:")
def agents_dialog() -> None:
    st.markdown("Each agent does one job with deterministic tools and a Bedrock model, then stops so you can "
                "review the result. Nothing moves on until you approve it.")
    st.markdown(agents_html(), unsafe_allow_html=True)


@st.dialog("The legacy estate", width="medium", icon=":material/dns:")
def legacy_dialog() -> None:
    st.markdown('**Legacy Inventory & Orders**, the "before" picture: a Flask monolith "temporarily" deployed by '
                "hand on a single EC2 instance. The six agents discover it, modernize it and right-size it.")
    st.markdown(legacy_html(), unsafe_allow_html=True)


def replay_card_wanted() -> bool:
    """The Replay card is one click from Plan B and advertises it to the audience: show it only when Replay is
    already on, or when no live run is possible (no AWS credentials). Otherwise the legacy-estate card takes its place."""
    return ss.mode == "replay" or not aws_credentials_available()


def welcome() -> None:
    st.markdown('<h1 class="tx-hello">Hi, let\'s migrate your legacy app.</h1>'
                '<p class="tx-hello-sub">Six agents, one legacy server. You approve every step.</p>',
                unsafe_allow_html=True)
    with st.container(key="welcome-input", horizontal=True, vertical_alignment="center", gap="small", wrap=False):
        upload = st.file_uploader("Drop the business requirements here, or pick a suggestion below",
                                  type=["md", "txt", "markdown"], key="upload-welcome", label_visibility="collapsed")
        st.button("Start migration", key="start", type="primary", icon=":material/arrow_forward:",
                  icon_position="right", on_click=start_migration)
        take_upload(upload, "welcome")
    requirements_ready()
    with st.container(key="suggestions", horizontal=True, gap="medium"):
        with st.container(key="sg-sample"):
            st.markdown(suggestion_html("description", "Start with the <b>sample requirements</b>",
                                        "Inventory & Orders · $450/month budget"), unsafe_allow_html=True)
            st.button("Start with the sample requirements", key="suggest-sample", on_click=start_with_sample)
        show_legacy = False
        if replay_card_wanted():
            with st.container(key="sg-replay"):
                st.markdown(suggestion_html("history", "Replay the <b>recorded run</b>", "Offline, same screens"),
                            unsafe_allow_html=True)
                st.button("Replay the recorded run", key="suggest-replay", on_click=switch_to_replay)
        else:
            with st.container(key="sg-legacy"):
                st.markdown(suggestion_html("dns", "See the <b>legacy estate</b>", "What the agents will migrate"),
                            unsafe_allow_html=True)
                show_legacy = st.button("See the legacy estate", key="suggest-legacy")
        with st.container(key="sg-agents"):
            st.markdown(suggestion_html("groups", "What will <b>the six agents</b> do?", "One minute overview"),
                        unsafe_allow_html=True)
            show_agents = st.button("What will the six agents do?", key="suggest-agents")
    st.markdown('<div class="tx-foot">A transparent re-creation of the loop AWS Transform\'s agents run, '
                'built with Strands Agents on Amazon Bedrock AgentCore.</div>', unsafe_allow_html=True)
    if show_agents:
        agents_dialog()
    elif show_legacy:
        legacy_dialog()


def render_transcript() -> None:
    for entry in ss.transcript:
        if entry["kind"] == "user":
            with st.chat_message("user", avatar=USER_AVATAR):
                st.markdown(entry["text"])
        elif entry["kind"] == "note":
            with st.chat_message("assistant", avatar=GUIDE_AVATAR):
                st.markdown(entry["text"])
        else:
            job: StageJob = entry["job"]
            with st.container(key=f"stage-{job.id}"):  # the per-stage anchor that auto-scroll targets
                view = StageView(job)
                if job.done:
                    view.render_final()
                else:
                    scroll_once(f".st-key-stage-{job.id}", f"{job.id}:start")
                    follow(job, view)  # blocks this script run until the stage finishes, painting as events arrive
                    st.rerun()  # re-render everything with the final result and the approval gate


@st.dialog("Ask for changes", width="medium", icon=":material/edit:", on_dismiss=close_feedback)
def feedback_dialog(stage: str) -> None:
    if not ss.show_feedback:  # sent: close the dialog with a full rerun that shows the revision live
        st.rerun()
    try:
        st.html(FOCUS_JS, unsafe_allow_javascript=True)  # the textarea takes the focus: the presenter just types
    except Exception:  # noqa: BLE001 - without it the presenter clicks into the box
        pass
    st.markdown(f"Tell the **{STAGE_META[stage].title}** agent what to change. It revises only what you ask for.")
    with st.form("feedback_form", clear_on_submit=True, border=False):  # the text and the click arrive together
        st.text_area("What should the agent change?", key="feedback_text", height=140,
                     placeholder="e.g. Size the database for 3x traffic next year")
        if ss.pop("feedback_hint", False):
            st.caption("Write what the agent should change first.")
        st.form_submit_button("Send to the agent", key="send_feedback", type="primary", icon=":material/send:",
                              on_click=send_feedback_form, args=(stage,))


def review_buttons(stage: str, approve_label: str, approve_key: str, retry_revision: bool = False) -> None:
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.button(approve_label, type="primary", key=approve_key, on_click=approve, args=(stage,),
                  icon=":material/check:")
        if retry_revision:
            st.button("Retry revision", key="retry", on_click=retry, args=(stage,), icon=":material/refresh:")
        st.button("Ask for changes", key="request_changes", on_click=open_feedback, icon=":material/edit:")


def recap(p: Pipeline) -> None:
    """Fallback finale when views.render_finale is not there."""
    with st.container(key="recap"):
        st.markdown("### Migration complete")
        st.markdown("One legacy server, six agents, and you approved every step.")
        pills = "".join(f'<span class="tx-pill">{STAGE_META[s].short}</span>' for s in STAGES)
        st.markdown(f'<div class="tx-recap-row">{pills}</div>', unsafe_allow_html=True)
        if merged := p.merged_artifacts():
            st.download_button("Download all artifacts (.zip)", zip_bytes(merged), type="primary",
                               icon=":material/download:", file_name=f"{p.run_id}-artifacts.zip",
                               mime="application/zip", key="zip-final", on_click="ignore")


def gate(slot: DeltaGenerator) -> None:
    """The review step (or the next action) for the first stage that is not yet approved. Never shown while an
    agent works: the slot was emptied before the transcript started following the live job."""
    p = pipeline()
    if p is None or busy():
        return
    stage = p.next_stage()
    if stage is None:
        with slot.container(key="wrapup"):
            if hasattr(views, "render_finale"):
                views.render_finale(p)
            else:
                recap(p)
        return
    meta, status = STAGE_META[stage], p.status(stage)
    n, total = STAGES.index(stage) + 1, len(STAGES)
    if p.revision_failed(stage):
        with slot.container(key="gate"):
            st.warning(f"**The revision of step {n} of {total} didn't finish:** {p.state.stages[stage].error}. "
                       "The previous result above is unchanged: approve it, retry the revision, or ask for "
                       "something else.", icon=":material/warning:")
            review_buttons(stage, "Approve previous result", "approve_previous", retry_revision=True)
    elif status == AWAITING:  # one row: the step on the left, the buttons on the right (they wrap when narrow)
        with slot.container(key="gate"), st.container(key="gate-row", horizontal=True, vertical_alignment="center"):
            st.markdown(f"**Step {n} of {total} is ready for review.** Next: {NEXT_STEP[stage]}.")
            review_buttons(stage, "Approve and continue", "approve")
    elif status == RUNNING:  # driven by another process: nothing to follow here
        with slot.container(key="gate-step"):
            st.markdown(f"**{meta.title}** is running in another process. Refresh when it finishes.")
            st.button("Refresh", key="refresh", on_click=refresh_from_disk, icon=":material/refresh:")
        return
    elif status == FAILED:
        with slot.container(key="gate"):
            st.error(f"**Step {n} of {total} failed:** {p.state.stages[stage].error}", icon=":material/error:")
            with st.container(horizontal=True, gap="small"):
                st.button("Retry", key="retry", on_click=retry, args=(stage,), type="primary",
                          icon=":material/refresh:")
        return
    elif status == PENDING:
        missing = stage == "requirements" and not p.requirements()
        with slot.container(key="gate-step"):
            if missing:
                st.markdown("The **Requirements** agent needs the migration request.")
                requirements_picker("gate")
            st.button(f"Run {meta.title}", key="run", on_click=run_pending, args=(stage,), type="primary",
                      icon=":material/play_arrow:", disabled=missing and not ss.requirements)
        return
    if ss.show_feedback:
        feedback_dialog(stage)


def chat_bar() -> None:
    """One change path: the bar at the bottom, worded for the current state (hidden on the welcome screen)."""
    p = pipeline()
    if p is None:
        return
    if busy():
        placeholder, disabled = "The agent is working…", True
    else:
        stage = p.next_stage()
        reviewable = stage is not None and p.status(stage) in (AWAITING, FAILED)
        placeholder = (f"Ask for changes to {STAGE_META[stage].short}…" if reviewable
                       else "All six steps are approved" if stage is None else "Nothing is waiting for review")
        disabled = not reviewable
    st.chat_input(placeholder, key="chat", on_submit=on_chat, disabled=disabled)


def landing_scroll(slot: DeltaGenerator) -> None:
    """After a rerun: bring the newest result (or the finale) into view once; open the welcome at the top."""
    p = pipeline()
    if p is None:
        scroll_once("top", "top", slot)  # once per session and per New run
        return
    if busy():
        return
    if p.next_stage() is None:
        scroll_once(".st-key-wrapup", f"{p.run_id}:finale", slot)
    elif (job := last_stage_job()) is not None and job.done:
        scroll_once(f".st-key-stage-{job.id}", f"{job.id}:landed", slot, land=True)


# --------------------------------------------------------------------------- page

attach_from_url()
nav_slot = sidebar()
strip_slot = topbar()
hero_slot = st.empty()
transcript_box = st.container(key="transcript")
gate_slot = st.empty()  # reserved before the transcript blocks, so no stale gate lingers while an agent works
scroll_slot = st.empty()
chat_bar()
if toast := ss.pop("toast", None):
    st.toast(toast, icon=":material/history:")
if pipeline() is None:
    with hero_slot.container(key="welcome"):
        welcome()
with transcript_box:
    render_transcript()
gate(gate_slot)
paint_nav(nav_slot)  # a fast stage (replay, fake) may have finished while the page was rendering
paint_strip(strip_slot)
landing_scroll(scroll_slot)
