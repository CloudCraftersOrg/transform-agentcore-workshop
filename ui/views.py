"""Rendering: a stage attempt as a chat message (live or finished), result cards and the artifacts browser."""

import io
import json
import time
import zipfile
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from transform_agents.core import STAGES

from .bridge import StageJob
from .style import STAGE_META

TOOL_ICON = {"running": "⏳", "success": "✅", "error": "❌"}
SEVERITY_ICON = {"critical": "🟥", "high": "🟧", "medium": "🟨", "low": "🟩"}
CHECK_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "skipped": "⏭️"}
PRIORITY_ICON = {"must": "🔴", "should": "🟠", "could": "🔵"}
LANGUAGES = {".py": "python", ".md": "markdown", ".tf": "hcl", ".hcl": "hcl", ".tfvars": "hcl", ".yaml": "yaml",
             ".yml": "yaml", ".json": "json", ".sql": "sql", ".sh": "bash", ".toml": "toml", ".html": "html",
             ".ini": "ini", ".cfg": "ini", ".conf": "nginx", ".service": "ini", ".txt": "text"}
LIVE_TOOL_LINES = 12


def calls(n: int) -> str:
    return f"{n} tool call{'' if n == 1 else 's'}"


def short(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split()).replace("`", "'")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class StageView:
    """One stage attempt as an assistant message: status line, tool timeline, narration and result card."""

    def __init__(self, job: StageJob):
        self.job, self.seen = job, 0
        self.status, self.model, self.text, self.error = "waiting for the agent…", "", "", ""
        self.tools: dict[str, dict[str, str]] = {}
        self.result: dict | None = None
        meta = STAGE_META[job.stage]
        with st.chat_message("assistant", avatar=meta.icon):
            revision = f" · revision {job.attempt - 1}" if job.attempt > 1 else ""
            st.markdown(f"**Stage {STAGES.index(job.stage) + 1}/{len(STAGES)} · {meta.title}**{revision}")
            self.status_slot = st.empty()
            self.tools_box = st.status("🛠️ Tool calls", expanded=not job.done, state="running")
            with self.tools_box:
                self.tools_slot = st.empty()
            self.text_slot = st.empty()
            self.result_slot = st.container()

    def consume(self) -> bool:
        """Apply events that arrived since the last call; True when something changed."""
        new = self.job.events[self.seen:]
        self.seen += len(new)
        for event in new:
            kind = event.get("type")
            if kind == "status":
                self.status, self.model = event.get("message", ""), event.get("model") or self.model
            elif kind == "text":
                self.text += event.get("delta", "")
            elif kind == "tool_start":
                self.tools[event.get("id", str(len(self.tools)))] = {
                    "name": event.get("name", "?"), "input": event.get("input", ""), "status": "running", "output": ""}
            elif kind == "tool_end":
                tool = self.tools.setdefault(event.get("id", ""), {"name": event.get("name", "?"), "input": ""})
                tool.update(status=event.get("status", "success"), output=event.get("output", ""))
            elif kind == "result":
                self.result = event
            elif kind == "error":
                self.error = event.get("message", "agent error")
        return bool(new)

    def tool_lines(self, limit: int | None = None) -> str:
        tools = list(self.tools.values())
        hidden = len(tools) - limit if limit and len(tools) > limit else 0
        lines = [f"- … {hidden} earlier tool calls"] if hidden else []
        for t in tools[hidden:]:
            line = f"- {TOOL_ICON.get(t['status'], '✅')} **{t['name']}** `{short(t['input'], 90)}`"
            if t["output"]:
                line += f"  \n  ↳ `{short(t['output'], 140)}`"
            lines.append(line)
        return "\n".join(lines) or "_no tool calls yet_"

    def render_live(self) -> None:
        elapsed = time.time() - self.job.started
        model = f" · `{self.model}`" if self.model else ""
        self.status_slot.markdown(f'<span class="tx-live">⏳ {short(self.status, 120)} · {elapsed:.0f}s</span>{model}',
                                  unsafe_allow_html=True)
        self.tools_box.update(label=f"🛠️ Working · {calls(len(self.tools))}", state="running")
        self.tools_slot.markdown(self.tool_lines(LIVE_TOOL_LINES))
        if self.text:
            self.text_slot.markdown(self.text + " ▌")

    def render_final(self) -> None:
        self.consume()
        failed = self.result is None
        if failed:
            self.status_slot.markdown(f'<span class="tx-fail">❌ Failed: {short(self.error or "no result", 300)}</span>',
                                      unsafe_allow_html=True)
        elif self.error:  # a result arrived, but the agent (or a retry inside it) also reported an error
            m = self.result.get("metrics") or {}
            self.status_slot.markdown(f'<span class="tx-fail">⚠️ Done in {m.get("latency_s", "?")}s, with an error: '
                                      f'{short(self.error, 200)}</span>', unsafe_allow_html=True)
        else:
            m = self.result.get("metrics") or {}
            self.status_slot.markdown(f'<span class="tx-done">✅ Done in {m.get("latency_s", "?")}s</span>',
                                      unsafe_allow_html=True)
        self.tools_box.update(label=f"🛠️ {calls(len(self.tools))}", state="error" if failed else "complete",
                              expanded=False)
        self.tools_slot.markdown(self.tool_lines())
        if self.text.strip():
            with self.text_slot.container(), st.expander("💬 Agent narration"):
                st.markdown(self.text)
        with self.result_slot:
            if failed:
                st.error(self.error or "The agent stream ended without a result.")
            else:
                if self.error:
                    st.warning(f"The agent reported an error before its result: {self.error}")
                render_result(self.job.stage, self.result, key=f"{self.job.stage}-{self.job.attempt}-{self.job.id}")


def follow(job: StageJob, view: StageView, poll: float = 0.15) -> None:
    """Keep re-rendering the live view as events arrive (one placeholder update per poll = natural throttling)."""
    last_paint = 0.0
    while True:
        finished = job.done  # read before consuming so no trailing event is missed
        if view.consume() or time.time() - last_paint > 1:
            view.render_live()
            last_paint = time.time()
        if finished:
            return
        time.sleep(poll)


# --------------------------------------------------------------------------- result card


def render_result(stage: str, result: dict, key: str) -> None:
    data, metrics = result.get("data") or {}, result.get("metrics") or {}
    artifacts = result.get("artifacts") or {}
    with st.container(border=True):
        st.markdown(f"**Summary.** {result.get('summary') or '(no summary)'}")
        cols = st.columns(4)
        cols[0].metric("⏱️ Latency", f"{metrics.get('latency_s', '–')} s")
        cols[1].metric("📥 Input tokens", f"{int(metrics.get('input_tokens') or 0):,}")
        cols[2].metric("📤 Output tokens", f"{int(metrics.get('output_tokens') or 0):,}")
        cols[3].metric("📄 Artifacts", len(artifacts))
        fallback = " · ⚠️ result parsed from text (no submit_result)" if metrics.get("submitted") is False else ""
        st.caption(f"🤖 `{metrics.get('model', '?')}` · {metrics.get('cycles', '?')} agent cycles{fallback}")
        follows_contract = CONTRACT_KEYS.get(stage, "") in data
        if follows_contract:
            try:
                HIGHLIGHTS[stage](data, artifacts)
            except Exception as exc:  # noqa: BLE001 - agents may return partial data; never break the transcript
                st.warning(f"Could not render the highlights ({type(exc).__name__}: {exc}).")
        else:
            st.info(f"This result does not follow the {stage} contract (a stand-in agent?), so here is its raw data.")
        with st.expander("🧾 Result data (JSON)", expanded=not follows_contract):
            st.json(data, expanded=1)
        with st.expander(f"📁 Artifacts ({len(artifacts)})"):
            artifacts_browser(artifacts, key=key, zip_name=f"{stage}-artifacts.zip")


def table(rows: Any, columns: list[str], icons: dict[str, dict[str, str]] | None = None) -> None:
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not rows:
        st.caption("(none)")
        return
    out = []
    for row in rows:
        cells = {}
        for col in columns:
            value = row.get(col, "")
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            elif isinstance(value, dict):
                value = json.dumps(value)
            if icons and col in icons:
                value = f"{icons[col].get(str(value).lower(), '')} {value}".strip()
            cells[col] = str(value)
        out.append(cells)
    st.dataframe(pd.DataFrame(out, columns=columns), hide_index=True, width="stretch")


def discovery_highlights(d: dict, artifacts: dict) -> None:
    app, infra, util = d.get("application") or {}, d.get("infrastructure") or {}, d.get("utilization") or {}
    strategy, score = d.get("strategy") or {}, d.get("readiness_score")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🎯 Readiness", f"{score}/100" if score is not None else "–")
    c2.metric("🧭 Strategy", str(strategy.get("recommended", "–")).title())
    c3.metric("🖥️ Instance", infra.get("instance_type", "–"))
    c4.metric("📈 CPU avg / p95", f"{util.get('cpu_avg_pct', '–')}% / {util.get('cpu_p95_pct', '–')}%")
    if isinstance(score, int | float):
        st.progress(max(0, min(100, int(score))) / 100, text=f"Migration readiness {score}/100")
    st.caption(f"{app.get('name', '?')} · {app.get('framework', '?')} · {app.get('loc', '?')} LOC · "
               f"target: {strategy.get('target', '?')}")
    st.markdown("**Findings**")
    table(d.get("findings"), ["id", "severity", "category", "title", "evidence", "recommendation"],
          icons={"severity": SEVERITY_ICON})


def requirements_highlights(d: dict, artifacts: dict) -> None:
    target, workload, finops = d.get("target") or {}, d.get("workload") or {}, d.get("finops") or {}
    reqs = d.get("requirements") or []
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📌 Requirements", f"{len(reqs)} ({sum(r.get('priority') == 'must' for r in reqs)} must)")
    c2.metric("☁️ Platform", f"{target.get('platform', '–')} · {target.get('cpu_architecture', '–')}")
    c3.metric("📦 Replicas", f"{workload.get('replicas_min', '–')}–{workload.get('replicas_max', '–')}")
    c4.metric("💵 Budget", f"${finops.get('monthly_budget_usd', '–')}/mo")
    st.markdown("**Requirements**")
    table(reqs, ["id", "priority", "category", "text", "traces_to", "acceptance"], icons={"priority": PRIORITY_ICON})
    if d.get("conflicts"):
        st.markdown("**⚖️ Conflicts**")
        table(d["conflicts"], ["description", "resolution"])
    st.markdown("**❓ Open questions** (assumptions the agent made)")
    table(d.get("open_questions"), ["id", "question", "assumption", "impact"])


def modernization_highlights(d: dict, artifacts: dict) -> None:
    image, tests = d.get("image") or {}, d.get("tests") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🧩 Service", d.get("service_name", "–"))
    c2.metric("🐳 Image", f"{image.get('repository', '–')}:{image.get('tag', '–')}")
    c3.metric("🧪 Tests", {True: "✅ passed", False: "❌ failed"}.get(tests.get("passed"), "–"))
    c4.metric("🔌 Endpoints", len((d.get("endpoints") or {}).get("api") or []))
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Changes**")
        table(d.get("changes"), ["file", "change", "addresses"])
    with right:
        st.markdown("**Files**")
        st.code(file_tree(artifacts) or "(no files)", language="text")


def terraform_highlights(d: dict, artifacts: dict) -> None:
    if d.get("valid"):
        st.badge("terraform validate: passed", icon="✅", color="green")
    else:
        st.badge("terraform validate: not passed", icon="❌", color="red")
    compute, db, net = d.get("compute") or {}, d.get("database") or {}, d.get("network") or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("🖥️ Nodes", f"{', '.join(compute.get('node_instance_types') or ['–'])} × "
                         f"{compute.get('node_min', '–')}–{compute.get('node_max', '–')}")
    c2.metric("🗄️ Database", f"{db.get('instance_class', '–')}{' Multi-AZ' if db.get('multi_az') else ''}")
    c3.metric("🌐 VPC", f"{net.get('vpc_cidr', '–')} · {net.get('azs', '–')} AZ")
    st.markdown("**Spec coverage**")
    table(d.get("spec_coverage"), ["requirement", "how"])
    if d.get("validate_output"):
        st.caption(f"`terraform validate`: {short(d['validate_output'], 300)}")


def k8s_validation_highlights(d: dict, artifacts: dict) -> None:
    passed = d.get("passed")
    st.badge("policy baseline: passed" if passed else "policy baseline: failed", icon="✅" if passed else "❌",
             color="green" if passed else "red")
    live = d.get("live_deploy") or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("🏅 Score", f"{d.get('score', '–')}/100")
    c2.metric("🔧 Fixes applied", len(d.get("fixes_applied") or []))
    c3.metric("🚀 Live deploy", f"{CHECK_ICON.get(str(live.get('status')), '')} {live.get('status', '–')}")
    if drift := [x for x in d.get("drift_injected") or [] if isinstance(x, dict)]:
        st.markdown(f"**🧪 Simulated drift** · {len(drift)} hand-edit violation{'' if len(drift) == 1 else 's'} "
                    "injected before validation (demo, not in the generated manifests)")
        table(drift, ["file", "change"])
    st.markdown("**Checks**")
    table(d.get("checks"), ["status", "id", "tool", "resource", "details"], icons={"status": CHECK_ICON})
    if d.get("fixes_applied"):
        st.markdown("**Fixes applied**")
        table(d["fixes_applied"], ["file", "change"])
    if live.get("status") not in (None, "skipped"):
        st.markdown(f"**Live deploy** · `{live.get('cluster', '?')}` · image `{live.get('image', '?')}`")
        if live.get("rollout"):
            st.caption(short(live["rollout"], 300))
        table(live.get("smoke_tests"), ["request", "status"])


def finops_highlights(d: dict, artifacts: dict) -> None:
    def usd(section: str) -> float:
        return float((d.get(section) or {}).get("monthly_usd") or 0)

    current, designed, optimized = usd("current"), usd("target_as_designed"), usd("target_optimized")
    budget = d.get("budget_usd")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🖥️ Current", f"${current:,.0f}/mo")
    c2.metric("🏗️ As designed", f"${designed:,.0f}/mo", delta=f"{designed - current:+,.0f} vs current",
              delta_color="inverse")
    c3.metric("✨ Optimized", f"${optimized:,.0f}/mo", delta=f"{optimized - designed:+,.0f} vs designed",
              delta_color="inverse")
    c4.metric("💵 Budget", f"${budget}/mo" if budget else "–",
              delta="within budget" if d.get("within_budget") else "over budget",
              delta_color="normal" if d.get("within_budget") else "inverse")
    st.altair_chart(cost_chart(current, designed, optimized, budget), width="stretch")
    st.markdown("**Recommendations**")
    table(d.get("recommendations"), ["id", "title", "monthly_savings_usd", "effort", "risk", "applies_to"])
    if rs := d.get("rightsizing"):
        st.caption("Right-sizing: " + " · ".join(f"{k} = {json.dumps(v)}" for k, v in rs.items()))


def cost_chart(current: float, designed: float, optimized: float, budget: float | None) -> alt.LayerChart:
    """One series (USD/month) across three scenarios: one hue, value labels, a muted budget reference line."""
    df = pd.DataFrame({"scenario": ["Current (EC2)", "Target as designed", "Target optimized"],
                       "usd": [current, designed, optimized]})
    base = alt.Chart(df).encode(
        x=alt.X("scenario:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("usd:Q", title="USD / month"),
        tooltip=[alt.Tooltip("scenario:N", title="Scenario"), alt.Tooltip("usd:Q", title="USD / month", format="$,.2f")],
    )
    bars = base.mark_bar(color="#FF9900", size=56, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
    labels = base.mark_text(dy=-9, color="#E6EDF3", fontWeight="bold").encode(text=alt.Text("usd:Q", format="$,.0f"))
    layers = [bars, labels]
    if budget:
        rule = alt.Chart(pd.DataFrame({"usd": [float(budget)]}))
        layers.append(rule.mark_rule(color="#8B9BB0", strokeDash=[6, 4], strokeWidth=2).encode(y="usd:Q"))
        layers.append(rule.mark_text(align="right", dy=-7, color="#AAB7C4", x="width")
                      .encode(y="usd:Q", text=alt.value(f"Budget ${budget:,.0f}")))
    return alt.layer(*layers).properties(height=280)


# A field every contract-conforming result has (docs/CONTRACTS.md); without it the highlights would be empty.
CONTRACT_KEYS = {"discovery": "findings", "requirements": "requirements", "modernization": "changes",
                 "terraform": "valid", "k8s_validation": "checks", "finops": "recommendations"}

HIGHLIGHTS: dict[str, Callable[[dict, dict], None]] = {
    "discovery": discovery_highlights, "requirements": requirements_highlights,
    "modernization": modernization_highlights, "terraform": terraform_highlights,
    "k8s_validation": k8s_validation_highlights, "finops": finops_highlights,
}


# --------------------------------------------------------------------------- artifacts


def language_for(path: str) -> str:
    name = PurePosixPath(path).name
    if name.startswith("Dockerfile"):
        return "docker"
    if name.endswith(".tfvars.example"):
        return "hcl"
    return LANGUAGES.get(PurePosixPath(path).suffix.lower(), "text")


def zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            zf.writestr(path, content)
    return buffer.getvalue()


def file_tree(paths: Any) -> str:
    tree: dict = {}
    for path in sorted(paths):
        node = tree
        for part in path.split("/"):
            node = node.setdefault(part, {})
    lines: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        items = sorted(node.items(), key=lambda kv: (not kv[1], kv[0]))  # folders first
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{name}{'/' if child else ''}")
            if child:
                walk(child, prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines)


def artifacts_browser(files: dict[str, str], key: str, zip_name: str) -> None:
    if not files:
        st.caption("No artifacts.")
        return
    left, right = st.columns([4, 1], vertical_alignment="bottom")
    path = left.selectbox("File", sorted(files), key=f"file-{key}")
    right.download_button("⬇️ Download .zip", zip_bytes(files), file_name=zip_name, mime="application/zip",
                          key=f"zip-{key}", on_click="ignore")
    language = language_for(path)
    if language == "markdown" and not st.toggle("Show source", key=f"src-{key}"):
        with st.container(border=True):
            st.markdown(files[path])
    else:
        st.code(files[path], language=language, line_numbers=True)
