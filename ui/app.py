"""Transform × AgentCore chatbot: upload the requirements, watch each agent work, approve or request changes.

Run from the repo root: `make ui` (= uv run streamlit run ui/app.py). Theme: ui/.streamlit/config.toml.
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
from ui.bridge import StageJob, run_registry
from ui.style import CSS, STAGE_META, banner_html, stepper_html
from ui.views import StageView, follow, zip_bytes

MODES = {"local": "🖥️ Local (in-process)", "agentcore": "☁️ AgentCore Runtime", "replay": "⏯️ Replay (offline)"}
if os.getenv("UI_ENABLE_FAKE") == "1":
    MODES["fake"] = "🧪 Fake (demo data)"

st.set_page_config(page_title="Transform × AgentCore", page_icon="🚀", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
ss = st.session_state
REG = run_registry()  # process-wide pipelines and live jobs: a page reload (new session) re-attaches to them
ss.setdefault("transcript", [])  # [{"kind": "user" | "note", "text"} | {"kind": "stage", "job": StageJob}]
ss.setdefault("pipeline", None)  # the same object as REG's pipeline for that run id
ss.setdefault("job", None)  # this run's latest StageJob (also in REG)
ss.setdefault("requirements", None)  # (filename, text)
ss.setdefault("show_feedback", False)
ss.setdefault("mode", os.getenv("UI_MODE", "local") if os.getenv("UI_MODE", "local") in MODES else "local")


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


OPTION_KEYS = ("live_aws", "live_deploy", "live_pricing", "simulate_drift")  # sidebar toggles = Options fields


def current_options() -> Options:
    return Options(**{key: bool(ss.get(key, False)) for key in OPTION_KEYS})


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
        note(f"⚠️ {exc}")
        return
    attempt = p.state.stages[stage].attempts + 1
    stream = (lambda: p.revise(stage, feedback)) if feedback else (lambda: p.run_stage(stage))
    job = REG.start(p.run_id, lambda: StageJob(stage, attempt, feedback or "", stream, run_id=p.run_id))
    if job is None:  # another browser session started one a moment ago
        note("⏳ An agent is already working on this run.")
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
    ss.transcript = [{"kind": "user", "text": f"🚀 Start migration{with_file}"}]
    start_stage("discovery")


def approve(stage: str) -> None:
    p = pipeline()
    try:
        nxt = p.approve(stage)
    except ValueError as exc:
        note(f"⚠️ {exc}")
        return
    ss.transcript.append({"kind": "user", "text": f"✅ Approve **{STAGE_META[stage].title}** & continue"})
    if nxt == "requirements" and not p.requirements():
        note("📎 Upload the migration requirements (or use the sample) to run the **Requirements** agent.")
    elif nxt:
        start_stage(nxt)


def request_changes(stage: str, text: str) -> None:
    text = text.strip()
    if text:
        ss.transcript.append({"kind": "user", "text": f"✏️ {text}"})
        start_stage(stage, text)


def send_feedback_form(stage: str) -> None:
    request_changes(stage, ss.get("feedback_text", ""))


def on_chat() -> None:
    """The chat box sends free-text feedback to the stage under review (= revise)."""
    text, p = (ss.get("chat") or "").strip(), pipeline()
    if not text:
        return
    stage = p.next_stage() if p else None
    if p is None:
        ss.transcript.append({"kind": "user", "text": text})
        note("Upload the requirements and press **🚀 Start migration** first.")
    elif busy():
        ss.transcript.append({"kind": "user", "text": text})
        note("⏳ An agent is still working. Send your feedback when it reaches the approval gate.")
    elif stage and p.status(stage) in (AWAITING, FAILED):
        request_changes(stage, text)
    else:
        ss.transcript.append({"kind": "user", "text": text})
        note("Nothing is waiting for review right now.")


def retry(stage: str) -> None:
    ss.transcript.append({"kind": "user", "text": f"🔁 Retry **{STAGE_META[stage].title}**"})
    start_stage(stage, pipeline().retry_feedback(stage))


def run_pending(stage: str) -> None:
    ss.transcript.append({"kind": "user", "text": f"▶️ Run **{STAGE_META[stage].title}**"})
    start_stage(stage)


def use_sample() -> None:
    if not busy():
        store_requirements(SAMPLE_REQUIREMENTS.name, SAMPLE_REQUIREMENTS.read_text())


def new_run() -> None:
    ss.pipeline, ss.job, ss.transcript, ss.show_feedback = None, None, [], False
    st.query_params.clear()


def rebuild_transcript(p: Pipeline, job: StageJob | None, header: str) -> list[dict]:
    """The chat of a run from its recorded attempts; the attempt still running is the live job itself."""
    transcript: list[dict] = [{"kind": "user", "text": header}]
    live = job if job is not None and not job.done else None
    for stage in STAGES:
        for attempt, feedback, events in p.attempts(stage):
            if feedback:
                transcript.append({"kind": "user", "text": f"✏️ {feedback}"})
            if live and (live.stage, live.attempt) == (stage, attempt):
                transcript.append({"kind": "stage", "job": live})
            elif p.status(stage) == RUNNING and attempt == p.state.stages[stage].attempts:
                transcript.append({"kind": "note", "text": f"⏳ **{STAGE_META[stage].title}** is still running in "
                                                           "another process (e.g. the CLI)."})
            else:
                transcript.append({"kind": "stage", "job": StageJob(stage, attempt, feedback, events=events)})
        if p.status(stage) == APPROVED:
            transcript.append({"kind": "user", "text": f"✅ Approve **{STAGE_META[stage].title}** & continue"})
    if live and not any(e.get("job") is live for e in transcript):  # its thread has not started the stage yet
        transcript.append({"kind": "stage", "job": live})
    return transcript


def open_run(run_id: str, header: str) -> None:
    """Show a run in this session: the live in-process Pipeline (and its running job) if there is one, else
    runs/<id> loaded from disk. Never reloads a run whose agent is still working, which would corrupt it."""
    try:
        p = REG.pipeline(run_id, lambda: Pipeline.load(run_id, current_client()))
    except (OSError, ValueError, KeyError, TypeError) as exc:  # a missing or hand-edited state.json
        note(f"⚠️ Cannot open run `{run_id}`: {type(exc).__name__}: {exc}")
        return
    job = REG.job(run_id)
    ss.pipeline, ss.job, ss.show_feedback = p, job, False
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
    open_run(run_id, f"↩️ Resume run `{run_id}`")


def attach_from_url() -> None:
    """After a page reload (a new session) the URL still names the run: re-attach to it, live job included."""
    run_id = st.query_params.get("run")
    if ss.pipeline is not None or not run_id or ss.get("attached") == run_id:
        return
    ss.attached = run_id  # once per session, even if the run is gone
    if (runs_dir() / run_id / "state.json").exists():
        open_run(run_id, f"🔄 Reconnected to run `{run_id}`")


def refresh_from_disk() -> None:
    """Re-read a run another process is driving (nothing runs here)."""
    p = pipeline()
    if p and not busy():
        REG.add(Pipeline.load(p.run_id, current_client()))
        open_run(p.run_id, f"🔄 Refreshed run `{p.run_id}`")


# --------------------------------------------------------------------------- page sections


def paint_stepper(slot: DeltaGenerator) -> None:
    """Stage statuses; the stage of the active job shows as running before its thread has even started."""
    p = pipeline()

    job = live_job()

    def status(stage: str) -> str:
        if job is not None and not job.done and job.stage == stage:
            return "running"
        return p.status(stage) if p else PENDING

    slot.markdown(stepper_html([(s, status(s), model_label(s)) for s in STAGES]), unsafe_allow_html=True)


def sidebar() -> DeltaGenerator:
    """Renders the sidebar and returns the stepper slot, repainted once the page is done."""
    p = pipeline()
    with st.sidebar:
        st.markdown("### 🧭 Migration pipeline")
        stepper = st.empty()
        paint_stepper(stepper)

        st.markdown("### ⚙️ Agent runtime")
        st.radio("Mode", list(MODES), format_func=MODES.get, key="mode", disabled=busy(), label_visibility="collapsed")
        if ss.mode == "local" and (overrides := current_client().modules):
            st.caption("🔁 Stand-ins: " + ", ".join(f"{k}→{v}" for k, v in overrides.items()))
        elif ss.mode == "agentcore":
            arns = load_arns()
            found = sum(s in arns for s in STAGES)
            (st.caption if found == len(STAGES) else st.warning)(f"{found}/{len(STAGES)} runtime ARNs found "
                                                                 "(deploy/agent-arns.json or AGENTCORE_ARN_<STAGE>)")
        elif ss.mode == "replay":
            runs = [d for d in recorded_runs() if not p or d.name != p.run_id]
            complete = {d.name for d in runs if is_complete_run(d)}
            sources = (["recorded"] if recording_available() else []) + ["fixtures"] + [d.name for d in runs]
            labels = {"recorded": "samples/recorded-run (tracked)", "fixtures": "tests/fixtures"}
            st.selectbox("Replay from", sources, key="replay_from", disabled=busy(),
                         format_func=lambda s: labels.get(s) or f"runs/{s}{' ✓ complete' if s in complete else ''}")
        st.toggle("Live AWS discovery (EC2 + CloudWatch)", key="live_aws")
        st.toggle("Live deploy to kind", key="live_deploy")
        st.toggle("Live AWS pricing", key="live_pricing")
        st.toggle("🧪 Simulate manual drift (demo)", key="simulate_drift",
                  help="Injects typical hand-edit violations before validation so the agent shows detect & fix "
                       "(K8s Validation stage only).")

        st.markdown("### 🗂️ Runs")
        if p:
            st.caption(f"Run `{p.run_id}` · open `{p.dir.relative_to(ROOT) if p.dir.is_relative_to(ROOT) else p.dir}"
                       "/workspace` in your IDE")
            if merged := p.merged_artifacts():
                st.download_button("⬇️ Download all artifacts (.zip)", zip_bytes(merged), on_click="ignore",
                                   file_name=f"{p.run_id}-artifacts.zip", mime="application/zip", key="zip-all")
        st.button("➕ New run", key="new_run", on_click=new_run, disabled=busy(), width="stretch")
        runs = Pipeline.list_runs()
        st.selectbox("Resume a run", runs, index=None, key="resume_id", placeholder="Select a run…")
        st.button("↩️ Resume", key="resume", on_click=resume, disabled=busy() or not ss.get("resume_id"), width="stretch")
    return stepper


def requirements_picker(key: str) -> None:
    """Upload or pick the sample; disabled while an agent works (the requirements stage may be reading them)."""
    working = busy()
    left, right = st.columns([3, 1], vertical_alignment="bottom")
    upload = left.file_uploader("📎 Migration requirements (.md / .txt)", type=["md", "txt", "markdown"],
                                key=f"upload-{key}", disabled=working)
    right.button("📄 Use sample requirements", key=f"sample-{key}", on_click=use_sample, width="stretch",
                 disabled=working)
    if upload is not None and not working and ss.get(f"upload-id-{key}") != upload.file_id:
        ss[f"upload-id-{key}"] = upload.file_id
        store_requirements(upload.name, upload.getvalue().decode("utf-8", errors="replace"))
    if ss.requirements:
        name, text = ss.requirements
        with st.expander(f"✅ Requirements ready: `{name}` ({len(text.splitlines())} lines)"):
            st.markdown(text)


def welcome() -> None:
    with st.chat_message("assistant", avatar="🧭"):
        st.markdown(
            "**Hi! I orchestrate six migration agents**, rebuilt from AWS Transform with Strands Agents and "
            "running on Amazon Bedrock AgentCore. Together they move **legacy-inventory**, a Flask monolith "
            "with SQLite on one EC2 instance, to a containerized microservice on Kubernetes. "
            "After every stage you **approve** the result or **request changes**."
        )
        rows = "\n".join(f"| {m.icon} | **{m.title}** | {m.does} | {m.analogue} | `{model_label(s)}` |"
                         for s, m in STAGE_META.items())
        st.markdown("| | Agent | What it does | AWS Transform analogue | Model |\n|---|---|---|---|---|\n" + rows)
        requirements_picker("welcome")
        if pipeline() is None:
            st.button("🚀 Start migration", type="primary", key="start", on_click=start_migration)


def render_transcript() -> None:
    for entry in ss.transcript:
        if entry["kind"] == "user":
            with st.chat_message("user"):
                st.markdown(entry["text"])
        elif entry["kind"] == "note":
            with st.chat_message("assistant", avatar="🧭"):
                st.markdown(entry["text"])
        else:
            job: StageJob = entry["job"]
            view = StageView(job)
            if job.done:
                view.render_final()
            else:
                follow(job, view)  # blocks this script run until the stage finishes, painting as events arrive
                st.rerun()  # re-render everything with the final result and the approval gate


def feedback_form(stage: str) -> None:
    with st.form("feedback_form", clear_on_submit=True):
        st.text_area("What should the agent change?", key="feedback_text",
                     placeholder="e.g. Size the database for 3x traffic next year")
        st.form_submit_button("📨 Send to the agent", key="send_feedback", on_click=send_feedback_form, args=(stage,))


def gate() -> None:
    """The approval gate (or the next action) for the first stage that is not yet approved."""
    p = pipeline()
    if p is None or busy():
        return
    stage = p.next_stage()
    with st.chat_message("assistant", avatar="🧭"):
        if stage is None:
            st.success("🎉 **Migration complete.** All six stages are approved.")
            st.download_button("⬇️ Download all artifacts (.zip)", zip_bytes(p.merged_artifacts()), type="primary",
                               file_name=f"{p.run_id}-artifacts.zip", mime="application/zip", key="zip-final",
                               on_click="ignore")
            return
        meta, status = STAGE_META[stage], p.status(stage)
        n = STAGES.index(stage) + 1
        if p.revision_failed(stage):
            st.warning(f"**{meta.title}**: {p.state.stages[stage].error}. The previous result above is unchanged: "
                       "approve it, retry the revision, or ask for something else.")
            left, mid, right, _ = st.columns([1.6, 1.3, 1.3, 1.8])
            left.button("✅ Approve previous result", type="primary", key="approve_previous", on_click=approve,
                        args=(stage,), width="stretch")
            mid.button("🔁 Retry revision", key="retry", on_click=retry, args=(stage,), width="stretch")
            right.button("✏️ Request changes", key="request_changes", width="stretch",
                         on_click=lambda: ss.update(show_feedback=not ss.show_feedback))
            if ss.show_feedback:
                feedback_form(stage)
        elif status == AWAITING:
            following = STAGE_META[STAGES[n]].title if n < len(STAGES) else "the wrap-up"
            st.markdown(f"**Gate {n}/{len(STAGES)} · {meta.icon} {meta.title}.** Review the result above. "
                        f"Approve to continue with **{following}**, or ask for changes.")
            left, right, _ = st.columns([1.4, 1.4, 3])
            left.button("✅ Approve & continue", type="primary", key="approve", on_click=approve, args=(stage,),
                        width="stretch")
            right.button("✏️ Request changes", key="request_changes", width="stretch",
                         on_click=lambda: ss.update(show_feedback=not ss.show_feedback))
            if ss.show_feedback:
                feedback_form(stage)
        elif status == RUNNING:  # driven by another process: nothing to follow here
            st.info(f"⏳ **{meta.title}** is running in another process. Refresh when it finishes.")
            st.button("🔄 Refresh", key="refresh", on_click=refresh_from_disk)
        elif status == FAILED:
            st.error(f"**{meta.title}** failed: {p.state.stages[stage].error}")
            st.button("🔁 Retry", key="retry", on_click=retry, args=(stage,), type="primary")
        elif status == PENDING:
            missing = stage == "requirements" and not p.requirements()
            if missing:
                st.markdown("The **Requirements** agent needs the migration request.")
                requirements_picker("gate")
            st.button(f"▶️ Run {meta.title}", key="run", on_click=run_pending, args=(stage,), type="primary",
                      disabled=missing and not ss.requirements)


# --------------------------------------------------------------------------- page

attach_from_url()
st.markdown(banner_html(), unsafe_allow_html=True)
stepper = sidebar()
st.chat_input("Request changes to the stage under review…", key="chat", on_submit=on_chat)
welcome()
render_transcript()
gate()
paint_stepper(stepper)  # a fast stage (replay, fake) may have finished while the page was rendering
