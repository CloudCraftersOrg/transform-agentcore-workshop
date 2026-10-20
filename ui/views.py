"""Rendering: each stage attempt as a chat message (a live progress card, then a result card), the finale and the
artifacts browser.

Shared with ui/app.py and ui/style.py:
- containers: live-<stage>, card-<stage>, kpis-<stage>, bridge-<stage> (a later attempt adds "-<attempt>") and finale;
- st.session_state["presenter"] hides raw JSON and telemetry; st.session_state["show_bridges"] (default True) shows
  the "In AWS Transform" strip under each result;
- CSS (below) is extra CSS that app.py injects once per page.

Every number on screen comes from the agents' result data (deterministic tools did the counting). The helpers
tolerate missing or odd fields, and nothing raised while rendering escapes to the page: a section that cannot be
drawn says so and the raw data stays available under Details.
"""

import html
import io
import json
import re
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any, NamedTuple

import altair as alt
import pandas as pd
import streamlit as st

from transform_agents.core import STAGES
from transform_agents.core.runner import LEAKED_MARKER_RE, TextScrubber

from .bridge import StageJob
from .style import STAGE_META, TOKENS

_TOKEN_DEFAULTS = {
    "canvas": "#F6F4FB", "card": "#FFFFFF", "border": "#E0E0E0", "text": "#242424", "text2": "#616161",
    "disabled": "#707070", "indigo": "#5B5FC7", "indigo_pressed": "#4F52B2", "indigo_tint": "#EBEBF8",
    "success": "#0E700E", "success_bg": "#F1FAF1", "warning": "#8A3707", "warning_bg": "#FFF9F5",
    "danger": "#C50F1F", "danger_bg": "#FDF3F4", "info": "#0F6CBD", "info_bg": "#EBF3FC",
    "chart_today": "#8A8886", "chart_over": "#C50F1F", "chart_optimized": "#5B5FC7", "chart_budget": "#616161",
}
T = {**_TOKEN_DEFAULTS, **(TOKENS if isinstance(TOKENS, dict) else {})}
DESIGNED_OVER = "#E58A93"  # the as-designed bar when it is over budget; its extended-support segment is chart_over

AGENT_AVATAR = ":material/hub:"
LIVE_TOOL_LINES = 12
LANGUAGES = {".py": "python", ".md": "markdown", ".tf": "hcl", ".hcl": "hcl", ".tfvars": "hcl", ".yaml": "yaml",
             ".yml": "yaml", ".json": "json", ".sql": "sql", ".sh": "bash", ".toml": "toml", ".html": "html",
             ".ini": "ini", ".cfg": "ini", ".conf": "nginx", ".service": "ini", ".txt": "text"}

# Extra CSS for app.py to inject. The markup below is styled inline (TOKENS), so this only adds motion and the
# finale tiles' wrapping. Never write a less-than sign followed by a letter in here (tests/test_ui.py): the HTML
# sanitizer would drop the whole style block.
CSS = """
<style>
@keyframes tx2-spin { to { transform: rotate(360deg); } }
.tx2-spin { animation: tx2-spin 1.1s linear infinite; }
.tx2-row:last-child { border-bottom: none !important; }
.tx2-timeline { max-height: none; }
.st-key-finale [data-testid="stMetricLabel"], .st-key-finale [data-testid="stMetricLabel"] *,
.st-key-finale [data-testid="stMetricDelta"], .st-key-finale [data-testid="stMetricDelta"] * {
  white-space: normal !important; overflow: visible !important; text-overflow: clip !important; }
</style>
"""


# --------------------------------------------------------------------------- small, pure helpers

# Emoji and pictographs, but not the plain check and cross marks (U+2713/2714/2717/2718) the tables use.
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u2712\u2715\u2716\u2719-\u27BF"
                      "\u2B00-\u2BFF\uFE0F\u200D\u20E3]")
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def rows_of(value: Any) -> list[dict]:
    return [row for row in as_list(value) if isinstance(row, dict)]


def num(value: Any) -> float | None:
    """A number from an int, float or numeric string ("$365.00", "1,234"); None for anything else."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value == value else None  # NaN
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def whole(value: Any) -> int | None:
    n = num(value)
    return int(round(n)) if n is not None else None


def plain(text: Any) -> str:
    """Model or data text without emoji or leaked tool-call markers, whitespace collapsed."""
    return " ".join(EMOJI_RE.sub("", LEAKED_MARKER_RE.sub("", str(text or ""))).split())


def short(text: Any, limit: int) -> str:
    flat = plain(text).replace("`", "'")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def esc(text: Any) -> str:
    """Escaped for inline HTML; "$" as an entity so Streamlit's markdown never starts math mode."""
    return html.escape(str(text if text is not None else ""), quote=True).replace("$", "&#36;")


def md(text: Any) -> str:
    """Safe inside Streamlit markdown: a literal "$" instead of math mode."""
    return str(text if text is not None else "").replace("$", "\\$")


def plural(n: Any, word: str, many: str | None = None) -> str:
    count = whole(n) or 0
    return f"{count} {word if count == 1 else (many or word + 's')}"


def money(value: Any, cents: bool = False) -> str:
    n = num(value)
    if n is None:
        return "–"
    return f"${n:,.2f}" if cents else f"${n:,.0f}"


def fmt_secs(value: Any) -> str:
    n = num(value)
    if n is None:
        return "–"
    if n < 60:
        return f"{n:.1f} s" if n < 10 or n != int(n) else f"{n:.0f} s"
    minutes, seconds = divmod(int(round(n)), 60)
    return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"


def fmt_tokens(value: Any) -> str:
    n = num(value) or 0
    if n < 1000:
        return f"{n:.0f}"
    if n < 100_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_pct(value: Any) -> str:
    n = num(value)
    return "–" if n is None else f"{n:g}%"


def cap(text: Any) -> str:
    s = plain(text)
    return s[:1].upper() + s[1:] if s else ""


ACRONYMS = {"db": "DB", "az": "AZ", "azs": "AZs", "nat": "NAT", "ecr": "ECR", "eks": "EKS", "rds": "RDS", "vpc": "VPC",
            "cpu": "CPU", "id": "ID", "slo": "SLO", "rpo": "RPO", "rto": "RTO", "api": "API", "url": "URL",
            "iac": "IaC", "gib": "GiB", "usd": "USD", "pct": "%", "ha": "HA", "s3": "S3", "fs": "FS", "hpa": "HPA",
            "pdb": "PDB", "tls": "TLS", "ms": "ms", "rps": "RPS"}


def humanize(key: Any) -> str:
    """snake_case or dotted keys as words: "db_multi_az" -> "DB multi AZ", "node_instance_types" -> "Node instance types"."""
    words = [w for w in re.split(r"[_.\s]+", str(key or "")) if w]
    out = [ACRONYMS.get(w.lower(), w) for w in words]
    if out and out[0] == words[0]:
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


def fmt_value(value: Any) -> str:
    if value is None or value == "":
        return "–"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(fmt_value(v) for v in value) or "–"
    if isinstance(value, dict):
        return " · ".join(f"{humanize(k)} {fmt_value(v)}" for k, v in value.items()) or "–"
    return plain(value)


def cell(value: Any) -> str:
    """One table cell as plain text (no JSON, no "365.0")."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return ", ".join(f"{k}: {fmt_value(v)}" for k, v in value.items())
    return fmt_value(value) if not isinstance(value, str) else plain(value)


def nb(text: Any) -> str:
    """IDs such as LF-01 or POL-009 with non-breaking hyphens, so table cells never split them."""
    return cell(text).replace("-", "\u2011")


def first_sentence(text: Any, limit: int = 140) -> str:
    flat = plain(text)
    match = re.match(r"(.+?[.!?])(\s|$)", flat)
    return short(match.group(1) if match else flat, limit)


SENTENCE_END_RE = re.compile(r"[.!?:…][\"'”’)\]*_]*$")


def latest_sentence(text: Any, limit: int = 200) -> str:
    """The latest COMPLETE sentence of a stream of narration: the live view shows one muted line, not a wall of
    text. A sentence still streaming, or a turn that stopped mid-sentence, is skipped (so turns never run together);
    a closing colon ("Now I'll write the tests:") reads as an ellipsis; an over-long sentence keeps its end."""
    pieces = [p for chunk in str(text or "").splitlines() for p in re.split(r"(?<=[.!?:])\s+", chunk)]
    pieces = [plain(p).replace("`", "") for p in pieces if plain(p)]
    line = next((p for p in reversed(pieces) if SENTENCE_END_RE.search(p)), "")
    line = re.sub(r"\s*:$", "…", line)
    if len(line) <= limit:
        return line
    cut = line[-(limit - 1):]
    return "…" + (cut.split(" ", 1)[1] if " " in cut else cut)


def scrub_text(text: Any) -> str:
    """One-shot scrub of leaked tool-call markers (the streaming path uses TextScrubber, which handles splits)."""
    return LEAKED_MARKER_RE.sub("", str(text or ""))


def scrub_deltas(deltas: Iterable[str]) -> str:
    scrubber, out = TextScrubber(), []
    for delta in deltas:
        out.append(scrubber.feed(str(delta or "")))
    out.append(scrubber.flush())
    return "".join(out)


MODEL_NAMES = [("nova-2-lite", "Nova 2 Lite"), ("nova-2-pro", "Nova 2 Pro"), ("nova-premier", "Nova Premier"),
               ("nova-pro", "Nova Pro"), ("nova-lite", "Nova Lite"), ("nova-micro", "Nova Micro"),
               ("deepseek.v3.2", "DeepSeek V3.2"), ("deepseek.v3", "DeepSeek V3"), ("deepseek.r1", "DeepSeek R1"),
               ("mistral-large-3", "Mistral Large 3"), ("ministral-3-14b", "Ministral 3 14B"), ("claude", "Claude")]


def friendly_model(model_id: Any) -> str:
    raw = str(model_id or "").strip()
    low = raw.lower()
    if not raw:
        return ""
    if low in ("fake", "replay"):
        return "Demo model" if low == "fake" else "Recorded run"
    for needle, name in MODEL_NAMES:
        if needle in low:
            return name
    core = re.sub(r"^(us|eu|apac|global)\.", "", raw.split(":")[0])
    core = core.split(".", 1)[-1]
    return re.sub(r"-v\d+$", "", core).replace("-", " ").strip().title() or raw


def telemetry_line(metrics: Any) -> str:
    """"Nova 2 Lite · 7.9 s · 24.6k tokens": one muted caption (hidden in presenter mode)."""
    m = as_dict(metrics)
    parts = [friendly_model(m.get("model"))]
    if num(m.get("latency_s")) is not None:
        parts.append(fmt_secs(m.get("latency_s")))
    tokens = (num(m.get("input_tokens")) or 0) + (num(m.get("output_tokens")) or 0)
    if tokens:
        parts.append(f"{fmt_tokens(tokens)} tokens")
    if m.get("submitted") is False:
        parts.append("result parsed from text")
    return " · ".join(p for p in parts if p)


def step_number(stage: str) -> int:
    return STAGES.index(stage) + 1 if stage in STAGES else 0


def container_key(prefix: str, stage: str, attempt: int = 1) -> str:
    """live-<stage>, card-<stage>, …; a later attempt of the same stage adds "-<attempt>" so keys stay unique."""
    return f"{prefix}-{stage}" if (whole(attempt) or 1) <= 1 else f"{prefix}-{stage}-{whole(attempt)}"


def keyed_container(key: str, fallback: str = "", **kwargs: Any) -> Any:
    try:
        return st.container(key=key, **kwargs)
    except Exception:  # noqa: BLE001 - a duplicate key (the same attempt drawn twice) must not break the page
        return st.container(key=f"{key}-{fallback or int(time.time() * 1000)}", **kwargs)


def presenter() -> bool:
    try:
        return bool(st.session_state.get("presenter"))
    except Exception:  # noqa: BLE001 - outside a script run
        return False


def show_bridges() -> bool:
    try:
        return bool(st.session_state.get("show_bridges", True))
    except Exception:  # noqa: BLE001
        return True


def meta_of(stage: str) -> Any:
    return STAGE_META.get(stage)


def meta_attr(stage: str, name: str, default: str = "") -> str:
    return str(getattr(meta_of(stage), name, "") or default)


# --------------------------------------------------------------------------- inline HTML (styled with TOKENS)

_ICON_STYLE = ("font-family:'Material Symbols Rounded';font-weight:normal;font-style:normal;line-height:1;"
               "letter-spacing:normal;text-transform:none;display:inline-block;white-space:nowrap;direction:ltr;"
               "font-feature-settings:'liga';-webkit-font-smoothing:antialiased;vertical-align:middle;flex:none;")


def rem(px: float) -> str:
    """Sizes in px at the 18 px root the shell uses, emitted in rem so presenter mode scales them."""
    return f"{px / 18:.4g}rem"


def ic(name: str, color: str | None = None, size: float = 20, spin: bool = False) -> str:
    style = f"{_ICON_STYLE}font-size:{rem(size)};" + (f"color:{color};" if color else "")
    cls = "tx-ms tx2-spin" if spin else "tx-ms"
    return f'<span class="{cls}" aria-hidden="true" style="{style}">{esc(name)}</span>'


def tile(symbol: str, size: int = 40) -> str:
    return (f'<div style="flex:none;width:{rem(size)};height:{rem(size)};border-radius:{rem(12)};'
            f'background:{T["indigo_tint"]};display:flex;align-items:center;justify-content:center">'
            f'{ic(symbol or "hub", T["indigo"], size * 0.6)}</div>')


def chip(kind: str) -> str:
    if kind == "model":
        bg, fg, icon, text = T["indigo_tint"], T["indigo_pressed"], "lightbulb", "Model"
    else:
        bg, fg, icon, text = "#F0F0F0", T["text2"], "build", "Tool"
    return (f'<span class="tx2-chip" style="flex:none;display:inline-flex;align-items:center;gap:3px;'
            f'padding:1px 9px 1px 7px;border-radius:999px;background:{bg};color:{fg};font-size:{rem(14)};'
            f'font-weight:600;line-height:1.5">{ic(icon, fg, 15)}{text}</span>')


def muted_html(text: str, icon: str | None = None) -> str:
    lead = ic(icon, T["text2"], 18) if icon else ""
    return (f'<div style="display:flex;gap:8px;align-items:center;color:{T["text2"]};font-size:{rem(15)};'
            f'line-height:1.45">{lead}<span>{esc(text)}</span></div>')


def header_html(stage: str, attempt: int, right: str, title: str | None = None, overline: str | None = None) -> str:
    """Icon tile, "Step n of 6 · <title>" (or an overline plus a big title), and something on the right."""
    meta_title = meta_attr(stage, "title", humanize(stage))
    step = f"Step {step_number(stage)} of {len(STAGES)} · {meta_title}"
    revision = f" · revision {attempt - 1}" if (whole(attempt) or 1) > 1 else ""
    if title is None:
        text = (f'<div style="font-size:{rem(18)};font-weight:600;line-height:1.35;color:inherit">{esc(step)}'
                f'<span style="color:{T["text2"]};font-weight:500">{esc(revision)}</span></div>')
    else:
        text = (f'<div style="font-size:{rem(14)};font-weight:600;color:{T["text2"]};line-height:1.4">'
                f'{esc(overline or step)}{esc(revision)}</div>'
                f'<div style="font-size:{rem(21)};font-weight:600;line-height:1.3;color:inherit">{title}</div>')
    return (f'<div style="display:flex;align-items:center;gap:{rem(12)}">{tile(meta_attr(stage, "symbol"))}'
            f'<div style="flex:1;min-width:0">{text}</div>'
            f'<div style="flex:none;display:flex;align-items:center;gap:{rem(8)};flex-wrap:wrap;'
            f'justify-content:flex-end">{right}</div></div>')


def status_pill(state: str, text: str) -> str:
    icon, fg, bg = {"done": ("check_circle", T["success"], T["success_bg"]),
                    "warning": ("warning", T["warning"], T["warning_bg"]),
                    "failed": ("error", T["danger"], T["danger_bg"]),
                    "running": ("progress_activity", T["indigo_pressed"], T["indigo_tint"])}.get(
        state, ("info", T["text2"], "#F5F5F5"))
    return (f'<span style="display:inline-flex;align-items:center;gap:6px;padding:3px 12px 3px 8px;'
            f'border-radius:999px;background:{bg};color:{fg};font-size:{rem(14)};font-weight:600;white-space:nowrap">'
            f'{ic(icon, fg, 18, spin=state == "running")}{esc(text)}</span>')


def plain_pill(text: str, icon: str | None = None) -> str:
    lead = ic(icon, T["text2"], 16) if icon else ""
    return (f'<span style="display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:999px;'
            f'border:1px solid {T["border"]};background:#FFFFFF;color:{T["text2"]};font-size:{rem(14)};'
            f'font-weight:600;white-space:nowrap">{lead}{esc(text)}</span>')


def headline_html(headline: str, summary: str = "", revised: bool = False) -> str:
    """The deterministic headline, the model's summary clamped to ONE line (the full text is the tooltip and opens
    the Overview tab; presenter mode passes no summary) and, on a revision, "Revised after your feedback"."""
    out = (f'<div class="tx2-headline" style="font-size:{rem(20)};font-weight:600;line-height:1.4;color:inherit">'
           f'{esc(headline)}</div>') if headline else ""
    if summary:
        out += (f'<div class="tx2-summary" title="{esc(summary)}" style="margin-top:2px;color:{T["text2"]};'
                f'font-size:{rem(15.5)};line-height:1.5;display:-webkit-box;-webkit-line-clamp:1;line-clamp:1;'
                f'-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere">{esc(summary)}</div>')
    if revised:
        out += f'<div style="margin-top:2px">{muted_html("Revised after your feedback", "edit_note")}</div>'
    return f"<div>{out}</div>" if out else ""


def summary_html(summary: str) -> str:
    """The model's full summary, at the top of the Overview tab (the card clamps it to one line, or hides it)."""
    return (f'<div style="display:flex;gap:8px;align-items:flex-start;color:{T["text2"]};font-size:{rem(15.5)};'
            f'line-height:1.5">{ic("lightbulb", T["indigo_pressed"], 18)}<div style="min-width:0">'
            f'<span style="font-weight:600;color:{T["text"]}">The model\'s summary.</span> {esc(summary)}</div></div>')


def evidence_html(found: str, decided: str) -> str:
    def box(icon: str, label: str, text: str, bg: str, fg: str, border: str) -> str:
        return (f'<div style="border:1px solid {border};border-radius:{rem(14)};padding:{rem(10)} {rem(14)};'
                f'background:{bg};min-width:0">'
                f'<div class="tx2-ev-label" style="display:flex;align-items:center;gap:6px;font-size:{rem(14)};'
                f'font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:{fg}">{ic(icon, fg, 17)}'
                f'{esc(label)}</div>'
                f'<div style="margin-top:4px;font-size:{rem(16.5)};font-weight:600;line-height:1.4;color:{T["text"]}">'
                f'{esc(text)}</div></div>')

    return (f'<div class="tx2-evidence" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));'
            f'gap:{rem(12)}">'
            + box("build", "Tools found", found or "–", "#FFFFFF", T["text2"], T["border"])
            + box("lightbulb", "Model decided", decided or "–", T["indigo_tint"], T["indigo_pressed"], "#D6D7F3")
            + "</div>")


def alert_html(title: str, text: str) -> str:
    """A Fluent MessageBar: danger colour bar, icon, title and text."""
    return (f'<div role="alert" style="display:flex;gap:{rem(10)};align-items:flex-start;padding:{rem(10)} {rem(14)};'
            f'border-radius:{rem(10)};background:{T["danger_bg"]};border:1px solid #F3C4C8;'
            f'border-left:4px solid {T["danger"]}">{ic("warning", T["danger"], 22)}'
            f'<div style="line-height:1.45;min-width:0"><div style="font-weight:700;font-size:{rem(16.5)};'
            f'color:{T["danger"]}">{esc(title)}</div>'
            f'<div style="font-size:{rem(15.5)};color:{T["text"]}">{esc(text)}</div></div></div>')


def kv_html(pairs: Iterable[tuple[str, Any]], min_width: int = 190) -> str:
    cells = []
    for key, value in pairs:
        if value is None or value == "" or value == [] or value == {}:
            continue
        cells.append(f'<div style="padding:{rem(8)} {rem(12)};border:1px solid {T["border"]};border-radius:{rem(10)};'
                     f'background:#FFFFFF;min-width:0"><div class="tx2-kv-label" style="font-size:{rem(14.5)};'
                     f'color:{T["text2"]}">'
                     f'{esc(key)}</div><div style="font-weight:600;font-size:{rem(15.5)};overflow-wrap:anywhere;'
                     f'color:{T["text"]}">{esc(fmt_value(value))}</div></div>')
    if not cells:
        return ""
    return (f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax({min_width}px,1fr));'
            f'gap:{rem(8)}">{"".join(cells)}</div>')


def flow_html(found: int, fixed: int, left: int) -> str:
    """4 found → 4 fixed → 0 left, as three pills."""
    def pill(icon: str, value: int, word: str, fg: str, bg: str) -> str:
        return (f'<span style="display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;'
                f'background:{bg};color:{fg};font-size:{rem(16)};font-weight:600">{ic(icon, fg, 20)}'
                f'<span style="font-size:{rem(20)}">{value}</span> {esc(word)}</span>')

    arrow = ic("arrow_forward", T["text2"], 20)
    left_bg, left_fg = (T["success_bg"], T["success"]) if left == 0 else (T["danger_bg"], T["danger"])
    return (f'<div aria-label="{found} found, {fixed} fixed, {left} left" style="display:flex;align-items:center;'
            f'gap:10px;flex-wrap:wrap">{pill("report", found, "found", T["warning"], T["warning_bg"])}{arrow}'
            f'{pill("build", fixed, "fixed", T["indigo_pressed"], T["indigo_tint"])}{arrow}'
            f'{pill("check_circle" if left == 0 else "error", left, "left", left_fg, left_bg)}</div>')


def questions_html(questions: list[dict]) -> str:
    """Open questions: "Resolved" (success pill + the reviewer's decision) or "Open" (+ the agent's assumption)."""
    rows = []
    for q in rows_of(questions):
        settled = str(q.get("status") or "").strip().lower() == "resolved"
        if settled:
            pill = status_pill("done", "Resolved")
            body = f"Your decision: {plain(q.get('resolution')) or 'resolved in review'}"
        else:
            pill = plain_pill("Open", "help")
            body = f"Assumption: {plain(q.get('assumption')) or 'none recorded'}"
            if q.get("impact"):
                body += f" · Impact: {plain(q.get('impact'))}"
        qid = f'<span style="color:{T["text2"]};font-weight:600">{esc(plain(q.get("id")))}</span> ' if q.get("id") else ""
        rows.append(f'<div class="tx2-row" style="display:flex;gap:12px;align-items:flex-start;padding:8px 0;'
                    f'border-bottom:1px solid #EFEFEF"><div style="flex:none;width:{rem(118)}">{pill}</div>'
                    f'<div style="min-width:0;line-height:1.45"><div style="font-weight:600;font-size:{rem(16)}">'
                    f'{qid}{esc(plain(q.get("question")) or "?")}</div><div style="color:{T["text2"]};'
                    f'font-size:{rem(15)}">{esc(body)}</div></div></div>')
    return f'<div style="display:flex;flex-direction:column">{"".join(rows)}</div>' if rows else ""


def html_block(markup: str, slot: Any = None) -> None:
    """Inline HTML through st.markdown (one line, so markdown never re-parses it). Streamlit pulls every markdown
    block up by 1rem to swallow a paragraph's bottom margin, which raw HTML does not have: without the padding the
    next element overlaps the block's last line (a clipped "Revised after your feedback"). The padding gives that
    space back minus 10 px, so a block sits 6 px above whatever follows it, at any presenter scale."""
    if markup:
        (slot or st).markdown(f'<div style="padding-bottom:calc(1rem - 10px)">{markup}</div>'.replace("\n", " "),
                              unsafe_allow_html=True)


# --------------------------------------------------------------------------- tools, phases and the timeline


class ToolInfo(NamedTuple):
    label: str
    kind: str  # "tool": deterministic code; "model": content the model wrote or a call it made
    outcome: Callable[[str, str], str] | None = None


def _m(pattern: str, text: Any, flags: int = 0) -> re.Match | None:
    return re.search(pattern, str(text or ""), flags)


def tool_path(raw_input: Any) -> str:
    match = _m(r'"path"\s*:\s*"([^"]+)"', raw_input)
    return match.group(1) if match else ""


def _name(path: str) -> str:
    return PurePosixPath(path).name or path


def _first_line(text: Any) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def _out_scan(i: str, o: str) -> str:
    parts = []
    if m := _m(r'"framework"\s*:\s*"([^"]+)"', o):
        parts.append(cap(m.group(1)))
    if m := _m(r'"files"\s*:\s*(\d+)', o):
        parts.append(f"{m.group(1)} files")
    if m := _m(r'"loc"\s*:\s*(\d+)', o):
        parts.append(f"{m.group(1)} lines of code")
    return " · ".join(parts)


def _out_legacy_tf(i: str, o: str) -> str:
    m = _m(r'"instance_type"\s*:\s*"([^"]+)"', o)
    return f"Found one {m.group(1)}" if m else ""


def _out_utilization(i: str, o: str) -> str:
    avg, p95 = _m(r'"cpu_avg_pct"\s*:\s*([\d.]+)', o), _m(r'"cpu_p95_pct"\s*:\s*([\d.]+)', o)
    if not avg:
        return ""
    source = _m(r'"source"\s*:\s*"(\w+)"', o)
    text = f"CPU {float(avg.group(1)):g}% average" + (f", {float(p95.group(1)):g}% p95" if p95 else "")
    return text + (f" ({source.group(1)})" if source else "")


def _out_live_instance(i: str, o: str) -> str:
    m = _m(r'"instance_type"\s*:\s*"([^"]+)"', o)
    return f"Live instance {m.group(1)}" if m else ""


def _out_specs(i: str, o: str) -> str:
    cpu, mem = _m(r'"vcpu"\s*:\s*(\d+)', o), _m(r'"memory_gib"\s*:\s*([\d.]+)', o)
    return f"{cpu.group(1)} vCPU · {float(mem.group(1)):g} GiB" if cpu and mem else ""


def _out_write(i: str, o: str) -> str:
    m = _m(r"wrote (\S+) \((\d+) bytes\)", o)
    return f"{_name(m.group(1))} · {int(m.group(2)) / 1024:.1f} KB" if m else ""


def _out_edit(i: str, o: str) -> str:
    m = _m(r"edited (\S+)", o)
    return f"Edited {_name(m.group(1))}" if m else ""


def _out_read(i: str, o: str) -> str:
    return f"Read {_name(tool_path(i))}" if tool_path(i) else ""


def _out_submit(i: str, o: str) -> str:
    return "Result handed in"


def _out_read_requirements(i: str, o: str) -> str:
    m = _m(r'"documents"\s*:\s*\["([^"]+)"', o)
    return f"Read {_name(m.group(1))}" if m else ""


def _out_set_fields(i: str, o: str) -> str:
    m = _m(r"set (\d+) fields?", o)
    return f"{m.group(1)} fields set" if m else ""


def _out_set_lists(i: str, o: str) -> str:
    m = _m(r"draft:\s*(.+)", o)
    return cap(m.group(1).strip().rstrip(".")) if m else ""


def _out_check_spec(i: str, o: str) -> str:
    line = _first_line(o)
    return short(line.rstrip("."), 90) if line else ""


def _out_scaffold(i: str, o: str) -> str:
    m = _m(r"Scaffolded (\d+) files", o)
    return f"{m.group(1)} files from the tested pattern" if m else ""


def _out_tests(i: str, o: str) -> str:
    passed, failed = _m(r"(\d+) passed", o), _m(r"(\d+) failed", o)
    parity = _m(r"API parity:\s*(\d+/\d+)", o)
    parts = []
    if failed:
        parts.append(f"{failed.group(1)} failed")
    if passed:
        parts.append(f"{passed.group(1)} passed")
    if parity:
        parts.append(f"API parity {parity.group(1)}")
    return " · ".join(parts)


def _out_manifests(i: str, o: str) -> str:
    m = _m(r"(\d+) policy checks passed", o)
    if m:
        return f"{m.group(1)} policy checks passed"
    return "Manifests need fixes" if _m(r"^FAIL", o) else ""


def _out_blueprint(i: str, o: str) -> str:
    m = _m(r"Files:\s*([^\n]+)", o)
    return f"Validated blueprint copied · {len(m.group(1).split(','))} files" if m else "Validated blueprint copied"


def _out_module_inputs(i: str, o: str) -> str:
    m = _m(r'"source"\s*:\s*"([^"]+)"', i)
    return f"Looked up {m.group(1)}" if m else ""


def _out_set_variables(i: str, o: str) -> str:
    m = _m(r"(\d+) change", o)
    return f"{plural(int(m.group(1)), 'value')} set" if m else ""


def _out_validate(i: str, o: str) -> str:
    m = _m(r"attempt (\d+)/\d+:\s*(VALID|INVALID|NOT VALID)", o)
    if not m:
        return ""
    text = f"{'Valid' if m.group(2) == 'VALID' else 'Not valid yet'} (attempt {m.group(1)})"
    if "Spec mismatches" in o:
        mismatches = re.findall(r"^\s+- ", o.split("Spec mismatches", 1)[1], re.M)
        if mismatches:
            text += f" · {plural(len(mismatches), 'spec mismatch', 'spec mismatches')} to fix"
    return text


def _out_kubeconform(i: str, o: str) -> str:
    m = _m(r"kubeconform_check:\s*(PASS|FAIL)", o)
    return ("Schemas valid" if m.group(1) == "PASS" else "Schema errors") if m else ""


def _out_policy(i: str, o: str) -> str:
    m = _m(r"(PASS|FAIL) - (\d+) failing findings?, (\d+)/(\d+) rules pass", o)
    if not m:
        return ""
    failing = int(m.group(2))
    lead = "No violations" if failing == 0 else plural(failing, "violation")
    return f"{lead} · {m.group(3)}/{m.group(4)} rules pass"


def _out_deploy(i: str, o: str) -> str:
    m = _m(r"deploy_to_kind:\s*(PASS|FAIL|SKIPPED|SKIP)", o)
    if not m:
        return ""
    if m.group(1).startswith("SKIP"):
        return "Skipped"
    ok = len(re.findall(r"^\s*ok\s", o, re.M))
    if m.group(1) == "PASS":
        return "Rolled out" + (f" · {plural(ok, 'smoke test')} passed" if ok else "")
    return "Deploy failed"


def _out_prices(i: str, o: str) -> str:
    m = _m(r"pricing_source=(\S+)", o)
    return f"Prices from {m.group(1)}" if m else ""


def _out_k8s_sizing(i: str, o: str) -> str:
    m = _m(r'"requests"\s*:\s*\{\s*"cpu"\s*:\s*"([^"]+)"\s*,\s*"memory"\s*:\s*"([^"]+)"', o)
    return f"Pods request {m.group(1)} CPU · {m.group(2)}" if m else ""


def _out_tf_sizing(i: str, o: str) -> str:
    m = _m(r"Kubernetes ([\d.]+):\s*(EXTENDED_SUPPORT|STANDARD_SUPPORT)", o)
    if not m:
        return ""
    return f"Kubernetes {m.group(1)} is in {'extended' if m.group(2).startswith('EXT') else 'standard'} support"


def _out_rightsize(i: str, o: str) -> str:
    m = _m(r'"requests"\s*:\s*\{\s*"cpu"\s*:\s*"([^"]+)"\s*,\s*"memory"\s*:\s*"([^"]+)"', o)
    return f"Pods right-sized to {m.group(1)} CPU · {m.group(2)}" if m else ""


def _out_node_fit(i: str, o: str) -> str:
    m = _m(r'"instance_type"\s*:\s*"([^"]+)"\s*,\s*"nodes"\s*:\s*(\d+)\s*,\s*"monthly_usd"\s*:\s*([\d.]+)\s*,'
           r'\s*"fits"\s*:\s*true', o)
    return f"Cheapest fit: {m.group(2)} × {m.group(1)}, {money(m.group(3), cents=True)}/month" if m else ""


def _out_compare(i: str, o: str) -> str:
    m = _m(r"current ([\d.]+) \| as designed ([\d.]+).*?\| optimized ([\d.]+)", o)
    return f"{money(m.group(1))} today · {money(m.group(2))} as designed · {money(m.group(3))} optimized" if m else ""


TOOLS: dict[str, ToolInfo] = {
    # discovery
    "scan_codebase": ToolInfo("Scan the code", "tool", _out_scan),
    "parse_legacy_terraform": ToolInfo("Parse the old Terraform", "tool", _out_legacy_tf),
    "get_utilization": ToolInfo("Read CPU and memory use", "tool", _out_utilization),
    "describe_live_instance": ToolInfo("Look up the live EC2 instance", "tool", _out_live_instance),
    "instance_specs": ToolInfo("Look up the instance specs", "tool", _out_specs),
    # requirements
    "read_requirements": ToolInfo("Read the request", "tool", _out_read_requirements),
    "get_spec_template": ToolInfo("Load the spec template", "tool", lambda i, o: "Template loaded"),
    "set_fields": ToolInfo("Fill in the spec", "model", _out_set_fields),
    "set_lists": ToolInfo("Write requirements and open questions", "model", _out_set_lists),
    "check_spec": ToolInfo("Check the spec", "tool", _out_check_spec),
    # modernization
    "scaffold_from_pattern": ToolInfo("Render the tested pattern", "tool", _out_scaffold),
    "run_service_tests": ToolInfo("Run the tests", "tool", _out_tests),
    "validate_manifests": ToolInfo("Validate the manifests", "tool", _out_manifests),
    "docker_build_check": ToolInfo("Build the container image", "tool", None),
    # terraform
    "get_blueprint": ToolInfo("Fetch the validated blueprint", "tool", _out_blueprint),
    "get_module_inputs": ToolInfo("Look up module inputs", "tool", _out_module_inputs),
    "set_variables": ToolInfo("Map the spec onto the blueprint", "model", _out_set_variables),
    "terraform_validate": ToolInfo("Run terraform validate", "tool", _out_validate),
    # k8s validation
    "render_manifests": ToolInfo("Render the manifests", "tool", lambda i, o: "Rendered"),
    "kubeconform_check": ToolInfo("Check the schemas", "tool", _out_kubeconform),
    "policy_check": ToolInfo("Check the policies", "tool", _out_policy),
    "deploy_to_kind": ToolInfo("Deploy to kind and smoke-test", "tool", _out_deploy),
    # finops
    "get_prices": ToolInfo("Load the price book", "tool", _out_prices),
    "parse_k8s_resources": ToolInfo("Read the Kubernetes sizing", "tool", _out_k8s_sizing),
    "parse_terraform_sizing": ToolInfo("Read the Terraform sizing", "tool", _out_tf_sizing),
    "rightsize_pods": ToolInfo("Right-size the pods", "tool", _out_rightsize),
    "node_fit": ToolInfo("Fit the pods onto nodes", "tool", _out_node_fit),
    "estimate_cost": ToolInfo("Price a scenario", "tool", None),
    "compare_scenarios": ToolInfo("Compare the scenarios", "tool", _out_compare),
    # shared
    "write_file": ToolInfo("Write a file", "model", _out_write),
    "edit_file": ToolInfo("Edit a file", "model", _out_edit),
    "read_file": ToolInfo("Read a file", "tool", _out_read),
    "list_files": ToolInfo("List the files", "tool", lambda i, o: ""),
    "submit_result": ToolInfo("Hand in the result", "model", _out_submit),
}
FILE_VERBS = {"write_file": "Write", "edit_file": "Edit", "read_file": "Read"}


def tool_info(name: Any) -> ToolInfo:
    key = str(name or "")
    if key in TOOLS:
        return TOOLS[key]
    words = key.replace("_", " ").replace("-", " ").strip()
    return ToolInfo(f"Run {words}" if words else "Tool call", "tool", None)


def tool_label(name: Any, raw_input: Any = "") -> str:
    path = tool_path(raw_input)
    if str(name) in FILE_VERBS and path:
        return f"{FILE_VERBS[str(name)]} {_name(path)}"
    return tool_info(name).label


def tool_outcome(tool: dict) -> str:
    """One short, friendly line about what a tool call produced; never raw JSON."""
    status, output, raw_input = tool.get("status"), str(tool.get("output") or ""), str(tool.get("input") or "")
    if status == "running":
        return ""
    if status == "error":
        return "Failed: " + short(_first_line(output) or "no details", 80)
    info = tool_info(tool.get("name"))
    try:
        text = info.outcome(raw_input, output) if info.outcome else ""
    except Exception:  # noqa: BLE001 - an unexpected output shape only loses the outcome text
        text = ""
    if text:
        return text
    if output.lstrip().startswith("error"):
        return short(_first_line(output), 80)
    line = _first_line(output)
    return "" if not line or line[:1] in "{[\"" else short(line, 80)


class Phase(NamedTuple):
    label: str
    kind: str
    tools: frozenset[str]
    path: str = ""  # only calls whose input path contains this (e.g. model-written tests vs. routes)


def _phase(label: str, kind: str, *tools: str, path: str = "") -> Phase:
    return Phase(label, kind, frozenset(tools), path)


PHASES: dict[str, list[Phase]] = {
    "discovery": [
        _phase("Scan the code", "tool", "scan_codebase"),
        _phase("Parse the old Terraform", "tool", "parse_legacy_terraform"),
        _phase("Read CPU and memory use", "tool", "get_utilization", "describe_live_instance", "instance_specs"),
        _phase("Weigh the evidence and pick a strategy", "model", "write_file", "submit_result"),
    ],
    "requirements": [
        _phase("Read the request", "tool", "read_requirements"),
        _phase("Load the spec template", "tool", "get_spec_template"),
        _phase("Write the requirements", "model", "set_fields", "set_lists"),
        _phase("Check the spec", "tool", "check_spec"),
        _phase("Write the spec document", "model", "write_file", "submit_result"),
    ],
    "modernization": [
        _phase("Render the tested pattern", "tool", "scaffold_from_pattern"),
        _phase("Port the routes", "model", "write_file", "edit_file", path="/app/"),
        _phase("Write the tests", "model", "write_file", "edit_file", path="tests/"),
        _phase("Run the tests", "tool", "run_service_tests", "docker_build_check"),
        _phase("Validate the manifests", "tool", "validate_manifests"),
    ],
    "terraform": [
        _phase("Fetch the validated blueprint", "tool", "get_blueprint", "get_module_inputs"),
        _phase("Map the spec onto it", "model", "set_variables"),
        _phase("Prove it with terraform validate", "tool", "terraform_validate"),
    ],
    "k8s_validation": [
        _phase("Check the schemas", "tool", "kubeconform_check", "render_manifests"),
        _phase("Check the policies", "tool", "policy_check"),
        _phase("Fix the manifests", "model", "edit_file", "write_file"),
        _phase("Check again", "tool", "kubeconform_check", "policy_check"),
        _phase("Deploy to kind and smoke-test", "tool", "deploy_to_kind"),
    ],
    "finops": [
        _phase("Load the price book", "tool", "get_prices"),
        _phase("Read the Kubernetes and Terraform sizing", "tool", "parse_k8s_resources", "parse_terraform_sizing"),
        _phase("Right-size the pods and nodes", "tool", "rightsize_pods", "node_fit"),
        _phase("Price the three scenarios", "tool", "compare_scenarios", "estimate_cost"),
        _phase("Pick and explain the savings", "model", "submit_result"),
    ],
}

IDEAS = {
    "discovery": "Tools find the evidence; the model weighs it and explains it.",
    "requirements": "The model turns the request into a spec; a checker makes sure nothing is missing.",
    "modernization": "Tools render the pattern; the model ports what needs judgement.",
    "terraform": "Grounded in a validated blueprint, then proved with a real terraform validate.",
    "k8s_validation": "Policy tools find the problems, the model fixes them, a real cluster proves it.",
    "finops": "Tools do every sum; the model picks the savings and explains them.",
}


def _matches(phase: Phase, tool: dict) -> bool:
    return str(tool.get("name")) in phase.tools and (not phase.path or phase.path in tool_path(tool.get("input")))


def assign_phases(stage: str, tools: list[dict]) -> list[int | None]:
    """Which phase each tool call belongs to: search forward from the current phase first (work moves on), then
    backward (a retry, e.g. set_variables again after terraform_validate). Unknown calls belong to no phase."""
    phases, out, cur = PHASES.get(stage) or [], [], -1
    for tool in tools:
        hit = next((i for i in range(max(cur, 0), len(phases)) if _matches(phases[i], tool)), None)
        if hit is None and cur > 0:
            hit = next((i for i in range(cur - 1, -1, -1) if _matches(phases[i], tool)), None)
        out.append(hit)
        if hit is not None:
            cur = hit
    return out


class PhaseView(NamedTuple):
    label: str
    kind: str
    state: str  # done | current | next | skipped | failed
    outcome: str


def _phase_outcome(calls: list[dict]) -> str:
    if not calls:
        return ""
    if len(calls) > 1 and all(c.get("name") in ("edit_file", "write_file") for c in calls):
        files = {tool_path(c.get("input")) for c in calls}
        return f"{plural(len(calls), 'edit')} in {plural(len(files), 'file')}"
    informative = [c for c in calls if c.get("name") != "submit_result"] or calls
    return next((o for o in (tool_outcome(c) for c in reversed(informative)) if o), "")


def phase_views(stage: str, tools: list[dict], running: bool = True) -> list[PhaseView]:
    phases = PHASES.get(stage) or []
    if not phases:
        return []
    owners = assign_phases(stage, tools)
    calls: list[list[dict]] = [[] for _ in phases]
    for tool, owner in zip(tools, owners, strict=False):
        if owner is not None:
            calls[owner].append(tool)
    reached = max((i for i, c in enumerate(calls) if c), default=-1)
    views = []
    for i, (phase, cs) in enumerate(zip(phases, calls, strict=False)):
        if any(c.get("status") == "running" for c in cs):
            state = "current"
        elif cs and cs[-1].get("status") == "error":
            state = "failed"
        elif cs:
            state = "done"
        elif i < reached:
            state = "skipped"
        else:
            state = "next"
        views.append(PhaseView(phase.label, phase.kind, state, _phase_outcome(cs)))
    if running and not any(t.get("status") == "running" for t in tools):
        recent = next((o for o in reversed(owners) if o is not None), -1)
        if recent + 1 < len(views):  # between calls: the model is working towards the next phase
            views[recent + 1] = views[recent + 1]._replace(state="current")
    return views


PHASE_LOOK = {"done": ("check_circle", "success", "Done"), "current": ("progress_activity", "indigo", "In progress"),
              "next": ("radio_button_unchecked", "disabled", "Next"),
              "skipped": ("do_not_disturb_on", "disabled", "Not needed"), "failed": ("error", "danger", "Failed")}


def phases_html(views: list[PhaseView]) -> str:
    rows = []
    for view in views:
        icon, color, word = PHASE_LOOK.get(view.state, PHASE_LOOK["next"])
        label_color = T["text"] if view.state in ("done", "current", "failed") else T["text2"]
        note = view.outcome if view.state in ("done", "failed") and view.outcome else word
        if view.state == "current" and view.outcome:
            note = f"In progress · {view.outcome}"
        rows.append(f'<div class="tx2-row" style="display:flex;align-items:center;gap:10px;padding:5px 0;'
                    f'border-bottom:1px solid #EFEFEF">{ic(icon, T[color], 20, spin=view.state == "current")}'
                    f'<span style="font-weight:600;font-size:{rem(16)};color:{label_color};white-space:nowrap">'
                    f'{esc(view.label)}</span>{chip(view.kind)}<span style="flex:1;min-width:0;color:{T["text2"]};'
                    f'font-size:{rem(15)};white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{esc(note)}</span>'
                    f'</div>')
    return f'<div style="display:flex;flex-direction:column">{"".join(rows)}</div>' if rows else ""


def tool_duration(tool: dict, now: float | None = None) -> float | None:
    t0, t1 = num(tool.get("t0")), num(tool.get("t1"))
    if t0 is None:
        return None
    if t1 is None:
        return (now - t0) if (now is not None and tool.get("status") == "running") else None
    return max(t1 - t0, 0.0)


def timeline_html(tools: list[dict], limit: int | None = None, now: float | None = None) -> str:
    if not tools:
        return muted_html("No steps yet: the agent is getting started.", "hourglass_top")
    hidden = len(tools) - limit if limit and len(tools) > limit else 0
    rows = [muted_html(f"{plural(hidden, 'earlier step')} above", "more_horiz")] if hidden else []
    for tool in tools[hidden:]:
        status = tool.get("status")
        icon, color = {"running": ("progress_activity", T["indigo"]), "error": ("error", T["danger"])}.get(
            status, ("check_circle", T["success"]))
        info = tool_info(tool.get("name"))
        seconds = tool_duration(tool, now)
        duration = f"{seconds:.1f} s" if seconds is not None and seconds >= 0.1 else ""
        outcome = tool_outcome(tool) or ("Working…" if status == "running" else "")
        rows.append(f'<div class="tx2-row" style="display:flex;align-items:center;gap:10px;padding:5px 0;'
                    f'border-bottom:1px solid #EFEFEF">{ic(icon, color, 18, spin=status == "running")}'
                    f'<span style="font-weight:600;font-size:{rem(15)};white-space:nowrap">'
                    f'{esc(tool_label(tool.get("name"), tool.get("input")))}</span>{chip(info.kind)}'
                    f'<span style="flex:1;min-width:0;color:{T["text2"]};font-size:{rem(14.5)};white-space:nowrap;'
                    f'overflow:hidden;text-overflow:ellipsis">{esc(outcome)}</span>'
                    f'<span style="flex:none;color:{T["text2"]};font-size:{rem(14)};'
                    f'font-variant-numeric:tabular-nums">{esc(duration)}</span></div>')
    return f'<div class="tx2-timeline" style="display:flex;flex-direction:column">{"".join(rows)}</div>'


def status_note(message: Any) -> str:
    """The agent's status line, minus noise (and minus "replaying …", so a replay looks like the live run)."""
    text = plain(message)
    if not text or text.lower() in ("agent started", "start", "done") or text.lower().startswith("replaying"):
        return ""
    return cap(text)


def live_header_html(stage: str, attempt: int, elapsed: float, status: str, model: str, hide_model: bool) -> str:
    doing = meta_attr(stage, "doing") or f"Working on {meta_attr(stage, 'title', humanize(stage))}"
    title = (f'{esc(doing)} <span style="color:{T["text2"]};font-weight:500;font-variant-numeric:tabular-nums">'
             f'· {max(int(elapsed), 0)} s</span>')
    right = status_pill("running", "Working")
    if model and not hide_model:
        right += plain_pill(friendly_model(model), "memory")
    out = header_html(stage, attempt, right, title=title)
    if note := status_note(status):
        out += f'<div style="margin-top:8px">{muted_html(note, "info")}</div>'
    if idea := IDEAS.get(stage):
        out += f'<div style="margin-top:6px">{muted_html(idea, "lightbulb")}</div>'
    return out


def narration_html(line: str) -> str:
    return (f'<div class="tx2-narration" style="display:flex;gap:8px;align-items:flex-start;color:{T["text2"]};'
            f'font-style:italic;'
            f'font-size:{rem(15.5)};line-height:1.5">{ic("chat_bubble", T["text2"], 18)}<span>{esc(line)}</span></div>')


# --------------------------------------------------------------------------- facts: what each result says, in numbers


class Kpi(NamedTuple):
    label: str
    value: str
    delta: str | None = None
    color: str = "gray"  # st.metric delta_color
    help: str | None = None


def _int_in(pattern: str, *texts: Any) -> int | None:
    for text in texts:
        if m := _m(pattern, text):
            return int(m.group(1))
    return None


def discovery_facts(d: dict) -> dict:
    d = as_dict(d)
    findings = rows_of(d.get("findings"))
    sev = Counter(str(f.get("severity") or "").strip().lower() for f in findings)
    app, infra = as_dict(d.get("application")), as_dict(d.get("infrastructure"))
    util, strategy = as_dict(d.get("utilization")), as_dict(d.get("strategy"))
    return {
        "findings": len(findings) if isinstance(d.get("findings"), list) else None,
        "critical": sev.get("critical", 0), "high": sev.get("high", 0),
        "medium": sev.get("medium", 0), "low": sev.get("low", 0),
        "readiness": num(d.get("readiness_score")),
        "strategy": plain(strategy.get("recommended")) or None,
        "target": plain(strategy.get("target")) or None,
        "app": plain(app.get("name")) or None, "framework": plain(app.get("framework")) or None,
        "loc": whole(app.get("loc")), "instance": plain(infra.get("instance_type")) or None,
        "cpu_avg": num(util.get("cpu_avg_pct")), "cpu_p95": num(util.get("cpu_p95_pct")),
        "util_source": plain(util.get("source")) or None,
    }


def question_resolved(question: dict) -> bool:
    """An open question the reviewer settled ("status": "resolved"); absent or "open" means still open."""
    return str(as_dict(question).get("status") or "").strip().lower() == "resolved"


def requirements_facts(d: dict) -> dict:
    d = as_dict(d)
    reqs = rows_of(d.get("requirements"))
    priority = Counter(str(r.get("priority") or "").lower() for r in reqs)
    target, finops, nfr = as_dict(d.get("target")), as_dict(d.get("finops")), as_dict(d.get("nfr"))
    workload, data = as_dict(d.get("workload")), as_dict(d.get("data"))
    questions = rows_of(d.get("open_questions"))
    resolved = sum(1 for q in questions if question_resolved(q))
    has_questions = isinstance(d.get("open_questions"), list)
    return {
        "requirements": len(reqs) if isinstance(d.get("requirements"), list) else None,
        "must": priority.get("must", 0), "should": priority.get("should", 0),
        "traced": sum(1 for r in reqs if as_list(r.get("traces_to"))),
        "questions": len(questions) - resolved if has_questions else None,  # still open
        "questions_resolved": resolved,
        "conflicts": len(rows_of(d.get("conflicts"))),
        "budget": num(finops.get("monthly_budget_usd")), "slo": num(nfr.get("availability_slo_pct")),
        "platform": plain(target.get("platform")) or None, "k8s": plain(target.get("kubernetes_version")) or None,
        "arch": plain(target.get("cpu_architecture")) or None,
        "replicas_min": whole(workload.get("replicas_min")), "replicas_max": whole(workload.get("replicas_max")),
        "db": plain(data.get("engine")) or None, "service": plain(as_dict(d.get("service")).get("name")) or None,
    }


def test_counts(tests: Any) -> tuple[int | None, int | None]:
    """(passed, total) from a pytest summary such as "25 passed" or "2 failed, 23 passed in 0.4s"."""
    t = as_dict(tests)
    texts = [t.get("summary"), t.get("output")] if t else [tests]
    for text in texts:
        passed = _int_in(r"(\d+)\s+passed", text)
        failed = _int_in(r"(\d+)\s+failed", text) or 0
        errors = _int_in(r"(\d+)\s+errors?\b", text) or 0
        if passed is not None or failed or errors:
            return passed or 0, (passed or 0) + failed + errors
    return None, None


def modernization_facts(d: dict, artifacts: dict | None = None) -> dict:
    d, artifacts = as_dict(d), as_dict(artifacts)
    tests = as_dict(d.get("tests"))
    passed, total = test_counts(tests)
    ok = tests.get("passed") if isinstance(tests.get("passed"), bool) else (
        passed == total if total else None)
    parity = as_dict(d.get("api_parity"))
    legacy, missing = as_list(parity.get("legacy")), as_list(parity.get("missing"))
    return {
        "tests_passed": passed, "tests_total": total, "tests_ok": ok,
        "routes_total": len(legacy) if legacy else None, "routes_ported": len(legacy) - len(missing) if legacy else None,
        "routes_missing": len(missing),
        "endpoints": len(as_list(as_dict(d.get("endpoints")).get("api"))),
        "files": len(artifacts) if artifacts else None,
        "changes": len(rows_of(d.get("changes"))),
        "manifests": plain(as_dict(d.get("validation")).get("manifests")) or None,
        "service": plain(d.get("service_name")) or None,
    }


def terraform_facts(d: dict) -> dict:
    d = as_dict(d)
    checks = rows_of(d.get("spec_checks"))
    compute, db, net = as_dict(d.get("compute")), as_dict(d.get("database")), as_dict(d.get("network"))
    k8s = d.get("kubernetes_version") or next(
        (c.get("actual") for c in checks if c.get("check") == "kubernetes_version"), None)
    return {
        "valid": d.get("valid") if isinstance(d.get("valid"), bool) else None,
        "level": plain(d.get("validation_level")) or None, "attempts": whole(d.get("validate_attempts")),
        "checks_total": len(checks) if checks else None,
        "checks_passed": sum(1 for c in checks if str(c.get("status") or "").lower() == "pass"),
        "k8s": plain(k8s) or None,
        "nodes": ", ".join(plain(t) for t in as_list(compute.get("node_instance_types"))) or None,
        "node_min": whole(compute.get("node_min")), "node_max": whole(compute.get("node_max")),
        "db_class": plain(db.get("instance_class")) or None,
        "multi_az": db.get("multi_az") if isinstance(db.get("multi_az"), bool) else None,
        "azs": whole(net.get("azs")), "nat": whole(net.get("nat_gateways")),
        "conflicts": coverage_conflicts(d),
    }


def violations_flow(d: dict, metrics: dict | None = None) -> tuple[int | None, int | None, int | None]:
    """(found, fixed, left) policy violations: policy_before/policy_after, else the metrics, else the drift list."""
    d, m = as_dict(d), as_dict(metrics)
    before, after = d.get("policy_before"), d.get("policy_after")
    drift = rows_of(d.get("drift_injected"))
    if isinstance(before, list):
        found = len(before)
    elif whole(m.get("failing_before")) is not None:
        found = whole(m.get("failing_before"))
    else:
        found = len(drift) if drift else None
    if isinstance(after, list):
        left = len(after)
    elif whole(m.get("failing_after")) is not None:
        left = whole(m.get("failing_after"))
    elif isinstance(d.get("checks"), list):
        left = sum(1 for c in rows_of(d.get("checks")) if str(c.get("status") or "").lower() == "fail")
    else:
        left = None
    if found is None:
        return None, None, left
    left = left if left is not None else 0
    return found, max(found - left, 0), left


def expected_status(row: dict) -> int | None:
    if whole(row.get("expected")) is not None:
        return whole(row.get("expected"))
    request = str(row.get("request") or "").strip().lower()
    if "oversell" in request or "conflict" in request:
        return 409
    if request.startswith("post"):
        return 201
    if request.startswith(("get", "head")):
        return 200
    return None


def smoke_rows(tests: Any) -> list[dict]:
    """Smoke tests with what was expected, what came back and whether that is a pass: a 409 on the oversell
    attempt is an expected pass, not a failure."""
    out = []
    for row in rows_of(tests):
        actual, expected = whole(row.get("status")), expected_status(row)
        if isinstance(row.get("passed"), bool):
            ok = row["passed"]
        elif expected is not None and actual is not None:
            ok = actual == expected
        elif actual is not None:
            ok = 200 <= actual < 400
        else:
            ok = None
        out.append({"request": plain(row.get("request")) or "?", "expected": expected, "actual": actual, "ok": ok,
                    "ms": num(row.get("ms"))})
    return out


def k8s_facts(d: dict, metrics: dict | None = None) -> dict:
    d = as_dict(d)
    found, fixed, left = violations_flow(d, metrics)
    live = as_dict(d.get("live_deploy"))
    smoke = smoke_rows(live.get("smoke_tests"))
    checks = rows_of(d.get("checks"))
    status_counts = Counter(str(c.get("status") or "").lower() for c in checks)
    fixes = rows_of(d.get("fixes_applied"))
    resolved = sum(len(as_list(f.get("resolves"))) for f in fixes)
    return {
        "found": found, "fixed": fixed, "left": left,
        "drift": len(rows_of(d.get("drift_injected"))),
        "smoke_passed": sum(1 for r in smoke if r["ok"] is True), "smoke_total": len(smoke),
        "oversell_blocked": any(r["expected"] == 409 and r["ok"] for r in smoke),
        "deploy": plain(live.get("status")).lower() or None, "cluster": plain(live.get("cluster")) or None,
        "checks_total": len(checks), "checks_pass": status_counts.get("pass", 0),
        "checks_warn": status_counts.get("warn", 0), "checks_fail": status_counts.get("fail", 0),
        "score": num(d.get("score")), "passed": d.get("passed") if isinstance(d.get("passed"), bool) else None,
        "fix_files": len({str(f.get("file")) for f in fixes}), "fixes": resolved or len(fixes),
    }


def finops_facts(d: dict) -> dict:
    d = as_dict(d)

    def scenario(key: str) -> float | None:
        return num(as_dict(d.get(key)).get("monthly_usd"))

    today, designed, optimized = scenario("current"), scenario("target_as_designed"), scenario("target_optimized")
    budget = num(d.get("budget_usd"))
    within = d.get("within_budget") if isinstance(d.get("within_budget"), bool) else (
        optimized <= budget if optimized is not None and budget else None)
    within_designed = d.get("within_budget_as_designed") if isinstance(d.get("within_budget_as_designed"), bool) else (
        designed <= budget if designed is not None and budget else None)
    recs = rows_of(d.get("recommendations"))
    eks_rec = next((r for r in recs if r.get("optimization") == "eks_standard_support"
                    or "standard support" in str(r.get("title") or "").lower()), None)
    items = rows_of(as_dict(d.get("target_as_designed")).get("items"))
    surcharge = sum(num(i.get("monthly_usd")) or 0 for i in items if "extended support" in str(i.get("item")).lower())
    if not surcharge and eks_rec:
        surcharge = num(eks_rec.get("monthly_savings_usd")) or 0
    support = as_dict(d.get("eks_support"))
    as_designed, optimized_support = as_dict(support.get("as_designed")), as_dict(support.get("optimized"))
    version = plain(as_designed.get("version") or as_dict(as_dict(d.get("target_as_designed")).get("design"))
                    .get("kubernetes_version")) or None
    target_version = plain(optimized_support.get("version") or as_dict(d.get("rightsizing")).get("kubernetes_version")) \
        or None
    extended = (as_designed.get("extended") is True or str(as_designed.get("status") or "").upper() == "EXTENDED_SUPPORT"
                or surcharge > 0)
    fo1 = next((r for r in recs if str(r.get("id")) == "FO-01"), recs[0] if recs else None)
    return {
        "today": today, "designed": designed, "optimized": optimized, "budget": budget,
        "within": within, "within_designed": within_designed,
        "over_designed": within_designed is False,
        "surcharge": surcharge or 0.0, "extended": bool(extended and surcharge), "version": version,
        "target_version": target_version, "eks_rec_id": plain(eks_rec.get("id")) if eks_rec else None,
        "standard_end": plain(as_designed.get("end_of_standard_support")) or None,
        "alert_text": plain(d.get("eks_support_alert")) or None,
        "fo1": fo1, "recommendations": len(recs),
        "applied": len(as_list(d.get("applied_optimizations"))),
    }


def stage_facts(stage: str, data: Any, artifacts: Any = None, metrics: Any = None) -> dict:
    try:
        if stage == "discovery":
            return discovery_facts(data)
        if stage == "requirements":
            return requirements_facts(data)
        if stage == "modernization":
            return modernization_facts(data, artifacts)
        if stage == "terraform":
            return terraform_facts(data)
        if stage == "k8s_validation":
            return k8s_facts(data, metrics)
        if stage == "finops":
            return finops_facts(data)
    except Exception:  # noqa: BLE001 - odd data: the card falls back to the summary and the raw JSON
        return {}
    return {}


def coverage_conflicts(d: dict) -> dict[str, str]:
    """Model-written spec_coverage lines that contradict the tool-verified values, by requirement id."""
    d = as_dict(d)
    net, db = as_dict(d.get("network")), as_dict(d.get("database"))
    azs, nat = whole(net.get("azs")), whole(net.get("nat_gateways"))
    out: dict[str, str] = {}
    for row in rows_of(d.get("spec_coverage")):
        how, req, notes = str(row.get("how") or ""), str(row.get("requirement") or "?"), []
        for m in re.finditer(r"\b(\d+)\s*-?\s*AZs?\b", how):
            if azs is not None and int(m.group(1)) != azs:
                notes.append(f"tools: {plural(azs, 'AZ')}")
        if re.search(r"NAT (gateway )?per AZ", how, re.I) and nat is not None and azs is not None and nat < azs:
            notes.append(f"tools: {plural(nat, 'NAT gateway')}")
        if re.search(r"multi-?AZ", how, re.I) and db.get("multi_az") is False:
            notes.append("tools: single-AZ database")
        if notes:
            out[req] = "; ".join(dict.fromkeys(notes))
    return out


# --------------------------------------------------------------------------- KPIs, headlines, evidence


def show_metric(kpi: Kpi, slot: Any = None) -> None:
    """st.metric renders label, value and delta as markdown: "$" is escaped so "$72 → $623" never turns into math."""
    (slot or st).metric(md(kpi.label), md(kpi.value), delta=md(kpi.delta) if kpi.delta else None,
                        delta_color=kpi.color, delta_arrow="off", help=kpi.help, width="stretch")


def stage_kpis(stage: str, f: dict) -> list[Kpi]:
    f, k = as_dict(f), []
    if stage == "discovery":
        if f.get("findings") is not None:
            crit = f.get("critical") or 0
            k.append(Kpi("Findings", str(f["findings"]), f"{crit} critical" if crit else "none critical",
                         "red" if crit else "green"))
        if f.get("readiness") is not None:
            r = f["readiness"]
            word, color = ("blockers first", "orange") if r < 50 else (
                ("some prep needed", "gray") if r < 75 else ("ready to move", "green"))
            k.append(Kpi("Readiness", f"{r:.0f}/100", word, color))
        if f.get("strategy"):
            k.append(Kpi("Strategy", cap(f["strategy"]), "the model's call", "violet"))
    elif stage == "requirements":
        if f.get("requirements") is not None:
            traced = f.get("traced") or 0
            k.append(Kpi("Requirements", str(f["requirements"]),
                         f"{traced} traced to findings" if traced else f"{f.get('must', 0)} must-haves", "gray"))
        if f.get("questions") is not None:
            resolved = f.get("questions_resolved") or 0
            if resolved:
                k.append(Kpi("Open questions", str(f["questions"]), f"{resolved} resolved by you", "green"))
            else:
                k.append(Kpi("Open questions", str(f["questions"]),
                             "assumptions recorded" if f["questions"] else "none", "gray"))
        if f.get("budget") is not None:
            k.append(Kpi("Budget", f"{money(f['budget'])}/mo",
                         f"SLO {fmt_pct(f['slo'])}" if f.get("slo") is not None else None, "gray"))
    elif stage == "modernization":
        p, t, ok = f.get("tests_passed"), f.get("tests_total"), f.get("tests_ok")
        if t:
            k.append(Kpi("Tests", f"{p}/{t}", "passing" if p == t else f"{t - p} failing", "green" if p == t else "red"))
        elif ok is not None:
            k.append(Kpi("Tests", "Passed" if ok else "Failed", None, "green" if ok else "red"))
        if f.get("routes_total"):
            missing = f.get("routes_missing") or 0
            k.append(Kpi("API routes", f"{f['routes_ported']} ported",
                         "contract kept" if not missing else f"{missing} missing", "green" if not missing else "red"))
        elif f.get("endpoints"):
            k.append(Kpi("API endpoints", str(f["endpoints"]), None))
        if f.get("files") is not None:
            k.append(Kpi("Files", str(f["files"]), plural(f.get("changes"), "change") if f.get("changes") else None))
    elif stage == "terraform":
        if f.get("valid") is not None:
            level = f.get("level")
            note = "terraform validate" if level in (None, "terraform") else f"{level} checks only"
            if f.get("attempts"):
                note += f" · {plural(f['attempts'], 'run')}"
            k.append(Kpi("Validate", "Passed" if f["valid"] else "Not passed", note, "green" if f["valid"] else "red"))
        if f.get("checks_total"):
            p, t = f.get("checks_passed") or 0, f["checks_total"]
            k.append(Kpi("Spec checks", f"{p}/{t}", "tool-verified" if p == t else f"{t - p} mismatched",
                         "green" if p == t else "red"))
        if f.get("k8s"):
            k.append(Kpi("Kubernetes", str(f["k8s"]), "from the spec", "gray"))
    elif stage == "k8s_validation":
        found, fixed, left = f.get("found"), f.get("fixed"), f.get("left")
        if found:
            k.append(Kpi("Violations", f"{found} → {left}", f"{fixed} fixed", "green" if not left else "orange"))
        elif found == 0:
            k.append(Kpi("Violations", "0", "none found", "green"))
        elif f.get("checks_total"):
            k.append(Kpi("Checks", f"{f.get('checks_pass', 0)}/{f['checks_total']}", "passing", "gray"))
        if f.get("smoke_total"):
            sp, stt = f.get("smoke_passed") or 0, f["smoke_total"]
            note = "oversell blocked (409)" if f.get("oversell_blocked") and sp == stt else (
                "all passing" if sp == stt else f"{stt - sp} failing")
            k.append(Kpi("Smoke tests", f"{sp}/{stt}", note, "green" if sp == stt else "red"))
        elif f.get("score") is not None:
            k.append(Kpi("Score", f"{f['score']:.0f}/100", None))
        deploy = f.get("deploy")
        if deploy:
            if deploy == "pass":
                k.append(Kpi("Deploy", "Live on kind", "rolled out", "green"))
            elif deploy == "fail":
                k.append(Kpi("Deploy", "Failed", "see Details", "red"))
            else:
                k.append(Kpi("Deploy", "Skipped", "live deploy off", "gray"))
    elif stage == "finops":
        today, designed, optimized, budget = f.get("today"), f.get("designed"), f.get("optimized"), f.get("budget")
        if today is not None:
            k.append(Kpi("Today", money(today), "per month", "gray"))
        if designed is not None:
            if budget:
                over = designed > budget
                k.append(Kpi("As designed", money(designed),
                             f"{money(abs(designed - budget))} {'over' if over else 'under'} budget",
                             "red" if over else "green"))
            else:
                k.append(Kpi("As designed", money(designed), "per month", "gray"))
        if optimized is not None:
            if budget:
                over = optimized > budget
                k.append(Kpi("Optimized", money(optimized),
                             f"{money(abs(budget - optimized))} {'over' if over else 'under'} budget",
                             "red" if over else "green"))
            elif designed is not None:
                k.append(Kpi("Optimized", money(optimized), f"{money(designed - optimized)} saved", "green"))
    return k[:3]


def headline(stage: str, f: dict) -> str:
    """One deterministic sentence about what the agent did, from the data (never from the model's summary)."""
    f = as_dict(f)
    if stage == "discovery" and f.get("findings") is not None:
        text = plural(f["findings"], "finding") + (f", {f['critical']} of them critical." if f.get("critical") else ".")
        return text + (f" Recommended path: {f['strategy'].lower()}." if f.get("strategy") else "")
    if stage == "requirements" and f.get("requirements") is not None:
        text = plural(f["requirements"], "requirement")
        if f.get("traced"):
            text += f", {f['traced']} traced to findings"
        q, resolved = f.get("questions"), f.get("questions_resolved") or 0
        if resolved:
            return text + (f"; {resolved} of your answers applied, {q} still open." if q
                           else f"; all {plural(resolved, 'open question')} resolved by you.")
        return text + (f", with {plural(q, 'open question')} for you." if q else ".")
    if stage == "modernization":
        service = f.get("service") or "the new service"
        parts = []
        if f.get("routes_total"):
            parts.append(f"Ported {plural(f['routes_ported'], 'API route')} into {service}")
        if f.get("tests_total"):
            parts.append(f"{f['tests_passed']} of {f['tests_total']} tests pass")
        elif f.get("tests_ok") is not None:
            parts.append("the tests pass" if f["tests_ok"] else "the tests do not pass yet")
        return ("; ".join(parts) + ".") if parts else ""
    if stage == "terraform" and f.get("valid") is not None:
        text = "Blueprint adapted: terraform validate " + ("passes" if f["valid"] else "did not pass")
        if f.get("checks_total"):
            text += f", {f.get('checks_passed', 0)} of {f['checks_total']} spec checks hold"
        return text + "."
    if stage == "k8s_validation":
        parts = []
        if f.get("found"):
            fixed, found = f.get("fixed") or 0, f["found"]
            parts.append(f"Fixed all {plural(found, 'policy violation')}" if fixed == found
                         else f"Fixed {fixed} of {plural(found, 'policy violation')}")
        elif f.get("found") == 0:
            parts.append("No policy violations")
        if f.get("deploy") == "pass" and f.get("smoke_total"):
            parts.append(f"deployed to kind, {f['smoke_passed']} of {f['smoke_total']} smoke tests pass")
        elif f.get("deploy") == "fail":
            parts.append("the live deploy failed")
        return ("; ".join(parts) + ".") if parts else ""
    if stage == "finops" and f.get("today") is not None and f.get("designed") is not None:
        text = f"{money(f['today'])} today → {money(f['designed'])} as designed"
        if f.get("budget"):
            text += f", {'over' if f.get('over_designed') else 'within'} the {money(f['budget'])} budget"
        if f.get("optimized") is not None:
            text += f" → {money(f['optimized'])} optimized"
        return text + "."
    return ""


def evidence(stage: str, f: dict) -> tuple[str, str] | None:
    """("Tools found", "Model decided"): tools do the counting, models make the call."""
    f = as_dict(f)

    def join(*parts: Any) -> str:
        return " · ".join(str(p) for p in parts if p)

    if stage == "discovery":
        found = join(plural(f["findings"], "finding") if f.get("findings") is not None else "",
                     f"{f['critical']} critical" if f.get("critical") else "",
                     f"CPU {fmt_pct(f['cpu_avg'])} average" if f.get("cpu_avg") is not None else "")
        decided = join(cap(f.get("strategy")), f"readiness {f['readiness']:.0f}/100" if f.get("readiness") is not None
                       else "")
    elif stage == "requirements":
        found = join(f"budget {money(f['budget'])}/mo" if f.get("budget") is not None else "",
                     f"SLO {fmt_pct(f['slo'])}" if f.get("slo") is not None else "",
                     plural((f["questions"] or 0) + (f.get("questions_resolved") or 0), "open point")
                     if f.get("questions") is not None else "")
        resolved = whole(f.get("questions_resolved")) or 0
        # one line at presenter scale: a revision says what it applied (the headline and KPIs carry the rest)
        decided = join(plural(f["requirements"], "requirement") + ("" if resolved else f" ({f.get('must', 0)} must)")
                       if f.get("requirements") is not None else "",
                       f"your {plural(resolved, 'answer')} applied" if resolved else
                       (f"assumptions for {plural(f['questions'], 'question')}" if f.get("questions") else ""))
    elif stage == "modernization":
        found = join(f"{f['tests_passed']}/{f['tests_total']} tests pass" if f.get("tests_total") else "",
                     f"{f['routes_ported']}/{f['routes_total']} routes kept" if f.get("routes_total") else "",
                     "manifests pass" if f.get("manifests") == "pass" else "")
        decided = join("Ported the business logic and wrote its tests",
                       plural(f["changes"], "change") if f.get("changes") else "")
    elif stage == "terraform":
        found = join(("terraform validate passed" if f.get("valid") else "terraform validate failed")
                     if f.get("valid") is not None else "",
                     f"{f.get('checks_passed', 0)}/{f['checks_total']} spec checks" if f.get("checks_total") else "")
        nodes = f"{f['nodes']} × {f.get('node_min')}–{f.get('node_max')}" if f.get("nodes") else ""
        db = f"{f['db_class']}{' Multi-AZ' if f.get('multi_az') else ''}" if f.get("db_class") else ""
        decided = join(nodes, db, plural(f["azs"], "AZ") if f.get("azs") else "")
        decided = f"Mapped the spec: {decided}" if decided else ""
    elif stage == "k8s_validation":
        found = join(plural(f["found"], "violation") + " found" if f.get("found") is not None else "",
                     f"{f['smoke_passed']}/{f['smoke_total']} smoke tests" if f.get("smoke_total") else "")
        decided = (f"Fixed {plural(f.get('fixes') or f.get('fixed'), 'violation')} in {plural(f['fix_files'], 'file')}"
                   if f.get("fix_files") else ("Nothing to fix" if f.get("found") == 0 else ""))
    elif stage == "finops":
        path = " → ".join(money(f[k]) for k in ("today", "designed", "optimized") if f.get(k) is not None)
        found = join(f"{path} per month" if path else "",
                     f"+{money(f['surcharge'])} extended support" if f.get("extended") else "")
        first = f"Upgrade to {f['target_version']} first ({f['eks_rec_id']})" if f.get("extended") and \
            f.get("target_version") and f.get("eks_rec_id") else ""
        decided = join(first, plural(f["applied"], "saving") + " applied" if f.get("applied") else "")
    else:
        return None
    return (found, decided) if found or decided else None


def finops_alert(f: dict) -> tuple[str, str] | None:
    """(title, text) for the MessageBar: extended support first, then over budget."""
    f = as_dict(f)
    if f.get("extended") and f.get("version"):
        title = f"Kubernetes {f['version']} is in EKS extended support: +{money(f['surcharge'])}/month"
        text = ""
        if f.get("target_version"):
            text = f"Upgrade to {f['target_version']}" + (f" ({f['eks_rec_id']})" if f.get("eks_rec_id") else "") \
                + " for standard support."
        if f.get("standard_end"):
            text += f" Standard support ended {f['standard_end']}."
        return title, text.strip() or (f.get("alert_text") or "")
    if f.get("alert_text"):
        return "Cost alert", f["alert_text"]
    if f.get("over_designed") and f.get("designed") is not None and f.get("budget"):
        return (f"As designed, the target is over budget: {money(f['designed'])} vs {money(f['budget'])}/month",
                "The recommendations below bring it back inside the budget.")
    return None


# --------------------------------------------------------------------------- the FinOps chart

CHART_HEIGHT = 230  # px: with the alert above it, it fits above the sticky gate when the card lands at 1280x720


def cost_chart(today: float | None, designed: float | None, optimized: float | None, budget: float | None,
               surcharge: float = 0.0, over_budget: bool | None = None) -> alt.LayerChart:
    """Today / as designed / optimized, coloured by meaning, with the extended-support surcharge as its own red
    segment and a dashed budget line."""
    today, designed, optimized = num(today) or 0.0, num(designed) or 0.0, num(optimized) or 0.0
    budget, surcharge = num(budget), max(num(surcharge) or 0.0, 0.0)
    surcharge = min(surcharge, designed)
    over = over_budget if over_budget is not None else bool(budget and designed > budget)
    order = ["Today", "As designed", "Optimized"]
    bars = pd.DataFrame([
        {"scenario": "Today", "usd": today, "color": T["chart_today"], "order": 0},
        {"scenario": "As designed", "usd": designed - surcharge, "color": DESIGNED_OVER if over else T["chart_today"],
         "order": 0},
        {"scenario": "As designed", "usd": surcharge, "color": T["chart_over"], "order": 1},
        {"scenario": "Optimized", "usd": optimized, "color": T["chart_optimized"], "order": 0},
    ])
    bars = bars[bars["usd"] > 0]
    totals = pd.DataFrame({"scenario": order, "usd": [today, designed, optimized],
                           "label": [money(today), money(designed), money(optimized)]})
    top = max([today, designed, optimized, budget or 0.0]) or 1.0
    y_scale = alt.Scale(domain=[0, top * 1.18], nice=False)
    x = alt.X("scenario:N", sort=order, title=None,
              axis=alt.Axis(labelAngle=0, labelFontSize=17, labelColor=T["text"], labelFontWeight=600, ticks=False,
                            domainColor=T["border"], labelPadding=8, labelLimit=0))
    layers: list[Any] = [
        alt.Chart(bars).mark_bar(size=96, cornerRadiusTopLeft=0, cornerRadiusTopRight=0).encode(
            x=x,
            y=alt.Y("usd:Q", stack="zero", scale=y_scale, title=None,
                    axis=alt.Axis(format="$,.0f", labelFontSize=16, labelColor=T["text2"], grid=True,
                                  gridColor="#EDEDED", domain=False, ticks=False, tickCount=4)),
            color=alt.Color("color:N", scale=None, legend=None),
            order=alt.Order("order:Q", sort="ascending"),
            tooltip=[alt.Tooltip("scenario:N", title="Scenario"), alt.Tooltip("usd:Q", title="USD / month",
                                                                                format="$,.2f")]),
        alt.Chart(totals).mark_text(dy=-12, fontSize=18, fontWeight="bold", color=T["text"]).encode(
            x=alt.X("scenario:N", sort=order), y=alt.Y("usd:Q", scale=y_scale), text="label:N"),
    ]
    if surcharge > 0:
        # A two-line callout right of the red segment, kept clear of the dashed budget line: centred on the segment
        # when the line is far, else just above it (or just below when the bar ends too close above the line).
        px = top * 1.18 / CHART_HEIGHT  # USD per pixel
        centre = designed - surcharge / 2  # of the two-line block (36 px tall)
        if budget and abs(centre - budget) < 26 * px:
            centre = budget + 28 * px if designed - budget > 50 * px else budget - 28 * px
        note = pd.DataFrame({"scenario": ["As designed"], "usd": [centre + 9 * px],  # the first line's middle
                             "label": [f"+{money(surcharge)}/mo\nextended support"]})
        layers.append(alt.Chart(note).mark_text(align="left", baseline="middle", dx=56, fontSize=15,
                                                fontWeight="bold", color=T["chart_over"], lineBreak="\n",
                                                lineHeight=18).encode(
            x=alt.X("scenario:N", sort=order), y=alt.Y("usd:Q", scale=y_scale), text="label:N"))
    if budget:
        rule = alt.Chart(pd.DataFrame({"usd": [budget]}))
        layers.append(rule.mark_rule(color=T["chart_budget"], strokeDash=[6, 4], strokeWidth=2).encode(
            y=alt.Y("usd:Q", scale=y_scale)))
        layers.append(rule.mark_text(align="right", baseline="bottom", dy=-5, x="width", fontSize=16,
                                     fontWeight="bold", color=T["chart_budget"]).encode(
            y=alt.Y("usd:Q", scale=y_scale), text=alt.value(f"Budget {money(budget)}")))
    return alt.layer(*layers).properties(height=CHART_HEIGHT, background="transparent").configure_view(strokeWidth=0)


# --------------------------------------------------------------------------- tables and sections


def _severity_css(value: Any) -> str:
    v = str(value).strip().lower()
    if v.startswith("critical"):
        return f"color:{T['danger']};font-weight:700"
    if v.startswith("high"):
        return f"color:{T['warning']};font-weight:700"
    return ""


def _result_css(value: Any) -> str:
    v = str(value).lower()
    if "pass" in v or v.startswith("yes"):
        return f"color:{T['success']};font-weight:600"
    if "fail" in v or v.startswith("no"):
        return f"color:{T['danger']};font-weight:600"
    if "warn" in v:
        return f"color:{T['warning']};font-weight:600"
    return ""


RESULT_WORDS = {"pass": "✓\u00a0Pass", "fail": "✗\u00a0Fail", "warn": "!\u00a0Warning", "skipped": "–\u00a0Skipped",
                "skip": "–\u00a0Skipped"}
YES, NO = "✓\u00a0Yes", "✗\u00a0No"


def result_word(status: Any) -> str:
    return RESULT_WORDS.get(str(status or "").strip().lower(), cap(status) or "–")


Column = tuple[str, str | Callable[[dict], Any]]


def table(rows: Any, columns: list[Column], *, styles: dict[str, Callable[[Any], str]] | None = None,
          nowrap: Iterable[str] = (), scroll_after: int = 7, empty: str = "Nothing to show.") -> None:
    """A static, wrapping table with human headers (st.table), scrolling inside the card when it is long."""
    rows = rows_of(rows)
    if not rows:
        st.caption(empty)
        return
    data = {}
    for label, getter in columns:
        values = []
        for row in rows:
            try:
                values.append(cell(getter(row) if callable(getter) else row.get(getter)))
            except Exception:  # noqa: BLE001
                values.append("")
        data[label] = values
    frame = pd.DataFrame(data)
    styler = frame.style.hide(axis="index")
    for column, fn in (styles or {}).items():
        if column in frame.columns:
            styler = styler.map(fn, subset=[column])
    keep = [c for c in nowrap if c in frame.columns]
    if keep:
        styler = styler.set_properties(subset=keep, **{"white-space": "nowrap"})
    box = st.container(height=44 * scroll_after + 46, border=False) if len(rows) > scroll_after else st.container()
    with box:
        st.table(styler, hide_index=True)


def subhead(text: str, icon: str | None = None) -> None:
    st.markdown(f"{f':material/{icon}: ' if icon else ''}**{md(text)}**")


def kv(pairs: Iterable[tuple[str, Any]], title: str | None = None, icon: str | None = None) -> None:
    markup = kv_html(pairs)
    if not markup:
        return
    if title:
        subhead(title, icon)
    html_block(markup)


def raw_json(data: Any, label: str = "Raw result data") -> None:
    if presenter():
        return
    with st.expander(label, icon=":material/data_object:"):
        st.json(data, expanded=1)


def guarded(fn: Callable[..., Any], *args: Any, label: str = "this section") -> bool:
    try:
        fn(*args)
        return True
    except Exception as exc:  # noqa: BLE001 - agents may return partial data; never break the transcript
        st.caption(f":material/info: Could not show {md(label)} ({type(exc).__name__}). The raw data is in Details.")
        return False


# ---- discovery


def discovery_overview(d: dict, artifacts: dict, f: dict) -> None:
    rows = sorted(rows_of(d.get("findings")), key=lambda r: (
        SEVERITY_RANK.get(str(r.get("severity") or "").lower(), 9), str(r.get("id") or "")))
    subhead("Findings", "search")
    table(rows, [("Severity", lambda r: cap(r.get("severity"))), ("ID", lambda r: nb(r.get("id"))),
                 ("Finding", "title"), ("Category", lambda r: cap(r.get("category"))),
                 ("Evidence", lambda r: short(r.get("evidence"), 80))],
          styles={"Severity": _severity_css}, nowrap=["Severity", "ID"], empty="No findings.")


def discovery_details(d: dict, artifacts: dict, f: dict) -> None:
    app, infra, util = as_dict(d.get("application")), as_dict(d.get("infrastructure")), as_dict(d.get("utilization"))
    strategy, deps = as_dict(d.get("strategy")), as_dict(d.get("dependencies"))
    volume = as_dict(infra.get("volume"))
    kv([("Name", app.get("name")), ("Framework", app.get("framework")), ("Language", " ".join(
        str(x) for x in (app.get("language"), app.get("language_version")) if x)), ("Lines of code", app.get("loc")),
        ("Files", app.get("files")), ("Entry point", app.get("entrypoint")), ("Port", app.get("listen_port"))],
       "Application", "code")
    kv([("Instance", infra.get("instance_type")), ("vCPU", infra.get("vcpu")), ("Memory", f"{infra['memory_gib']} GiB"
        if infra.get("memory_gib") else None), ("OS", infra.get("os")), ("Region", infra.get("region")),
        ("Root volume", " · ".join(str(x) for x in (volume.get("type"), f"{volume['size_gib']} GiB"
                                                    if volume.get("size_gib") else None) if x) or None),
        ("Process manager", infra.get("process_manager")), ("Reverse proxy", infra.get("reverse_proxy")),
        ("Source", infra.get("source"))], "Server", "dns")
    source = util.get("source")
    kv([("CPU average", fmt_pct(util.get("cpu_avg_pct")) if util.get("cpu_avg_pct") is not None else None),
        ("CPU p95", fmt_pct(util.get("cpu_p95_pct")) if util.get("cpu_p95_pct") is not None else None),
        ("Memory average", fmt_pct(util.get("mem_avg_pct")) if util.get("mem_avg_pct") is not None else None),
        ("Period", plural(util["period_days"], "day") if util.get("period_days") else None),
        ("Source", "estimate (not measured)" if source == "estimate" else source)], "Utilization", "monitoring")
    db = as_dict(deps.get("database"))
    kv([("Database", db.get("engine")), ("Tables", db.get("tables")), ("Local state", deps.get("local_state")),
        ("Packages", deps.get("python_packages"))], "Dependencies", "deployed_code")
    if strategy.get("rationale"):
        subhead("Why this strategy (the model's reasoning)", "lightbulb")
        st.markdown(md(plain(strategy["rationale"])))
    if rows_of(d.get("components")):
        subhead("Components", "widgets")
        table(d.get("components"), [("Component", "name"), ("Kind", "kind"), ("Routes", "routes"), ("Notes", "notes")])


# ---- requirements


def requirements_overview(d: dict, artifacts: dict, f: dict) -> None:
    questions = rows_of(d.get("open_questions"))
    resolved = sum(1 for q in questions if question_resolved(q))
    subhead("Open questions" + (f" ({resolved} resolved by you)" if resolved else " (assumptions the agent made)"),
            "help")
    if questions:
        html_block(questions_html(questions))
    else:
        st.caption("No open questions.")
    subhead("Requirements", "checklist")
    table(d.get("requirements"), [("ID", lambda r: nb(r.get("id"))), ("Priority", lambda r: cap(r.get("priority"))),
                                  ("Category", lambda r: cap(r.get("category"))), ("Requirement", "text"),
                                  ("Traces to", lambda r: nb(r.get("traces_to")))], scroll_after=5)


def requirements_details(d: dict, artifacts: dict, f: dict) -> None:
    for key, title, icon in (("target", "Target", "cloud"), ("workload", "Workload", "speed"),
                             ("data", "Data", "database"), ("nfr", "Reliability", "health_and_safety"),
                             ("security", "Security", "shield"), ("finops", "Budget", "savings")):
        kv([(humanize(k), v) for k, v in as_dict(d.get(key)).items()], title, icon)
    if rows_of(d.get("conflicts")):
        subhead("Conflicts and how they were resolved", "balance")
        table(d.get("conflicts"), [("Conflict", "description"), ("Resolution", "resolution")])
    if rows_of(d.get("gaps")):
        subhead("How each discovery finding is handled", "link")
        table(d.get("gaps"), [("Finding", lambda r: nb(r.get("finding_id"))), ("Handling", "handling")])
    if any(r.get("acceptance") for r in rows_of(d.get("requirements"))):
        subhead("Acceptance criteria", "task_alt")
        table(d.get("requirements"), [("ID", lambda r: nb(r.get("id"))), ("Acceptance", "acceptance")])


# ---- modernization


def modernization_overview(d: dict, artifacts: dict, f: dict) -> None:
    subhead("What changed", "edit_document")
    table(d.get("changes"), [("File", lambda r: str(r.get("file") or "").removeprefix("microservice/")),
                             ("Change", "change"), ("Addresses", "addresses")], empty="No changes listed.")
    parity = as_dict(d.get("api_parity"))
    if dropped := [plain(x) for x in as_list(parity.get("dropped")) if x]:
        st.caption(":material/info: Not ported by design: " + md("; ".join(dropped)))


def modernization_details(d: dict, artifacts: dict, f: dict) -> None:
    image, endpoints = as_dict(d.get("image")), as_dict(d.get("endpoints"))
    resources, scaling = as_dict(d.get("resources")), as_dict(d.get("scaling"))
    kv([("Repository", image.get("repository")), ("Tag", image.get("tag")), ("Local tag", image.get("local_tag")),
        ("Dockerfile", image.get("dockerfile")), ("Port", image.get("port"))], "Container image", "inventory_2")
    kv([("Health", endpoints.get("health")), ("Readiness", endpoints.get("readiness")),
        ("API routes", endpoints.get("api"))], "Endpoints", "api")
    kv([("Requests", as_dict(resources.get("requests"))), ("Limits", as_dict(resources.get("limits"))),
        ("Replicas", f"{scaling['replicas_min']}–{scaling['replicas_max']}" if scaling.get("replicas_min") else None),
        ("CPU target", fmt_pct(scaling["cpu_target_pct"]) if scaling.get("cpu_target_pct") else None)],
       "Resources and scaling", "tune")
    if rows_of(d.get("env")):
        subhead("Configuration", "settings")
        table(d.get("env"), [("Variable", "name"), ("Source", "source"), ("Required", "required"),
                             ("Description", "description")], nowrap=["Variable"])
    kv([(humanize(k), v) for k, v in as_dict(d.get("validation")).items()], "Validation", "rule")
    tests = as_dict(d.get("tests"))
    if tests.get("output"):
        subhead(f"Tests · {tests.get('command') or 'pytest'}", "science")
        st.code(str(tests["output"]), language="text", height=160 if str(tests["output"]).count("\n") > 6 else "content")


# ---- terraform


def terraform_overview(d: dict, artifacts: dict, f: dict) -> None:
    checks = rows_of(d.get("spec_checks"))
    if checks:
        subhead("Spec checks, verified by the tools", "fact_check")
        table(checks, [("Check", lambda r: humanize(r.get("check"))), ("Expected", "expected"), ("Actual", "actual"),
                       ("Result", lambda r: result_word(r.get("status")))],
              styles={"Result": _result_css}, nowrap=["Result"], scroll_after=8)
    else:
        compute, db, net = as_dict(d.get("compute")), as_dict(d.get("database")), as_dict(d.get("network"))
        kv([("Nodes", compute.get("node_instance_types")), ("Node count", f"{compute.get('node_min')}–"
            f"{compute.get('node_max')}" if compute.get("node_min") else None), ("Database", db.get("instance_class")),
            ("Multi-AZ", db.get("multi_az")), ("AZs", net.get("azs")), ("NAT gateways", net.get("nat_gateways"))],
           "Target infrastructure", "stacks")
    if conflicts := as_dict(f.get("conflicts")):
        st.caption(f":material/info: {plural(len(conflicts), 'model-written coverage note')} "
                   f"({', '.join(conflicts)}) {'disagrees' if len(conflicts) == 1 else 'disagree'} with the "
                   "tool-verified values; the checks above win. See Details.")


def terraform_details(d: dict, artifacts: dict, f: dict) -> None:
    compute, db, net = as_dict(d.get("compute")), as_dict(d.get("database")), as_dict(d.get("network"))
    kv([("Cluster", d.get("cluster_name")), ("Region", d.get("region")), ("Kubernetes", d.get("kubernetes_version")),
        ("Node types", compute.get("node_instance_types")),
        ("Nodes", f"{compute['node_min']}–{compute['node_max']}" if compute.get("node_min") else None),
        ("Capacity", compute.get("capacity_type")), ("Architecture", compute.get("architecture"))],
       "Cluster", "hub")
    kv([(humanize(k), v) for k, v in db.items()], "Database", "database")
    kv([(humanize(k), v) for k, v in net.items()], "Network", "lan")
    if rows_of(d.get("modules")):
        subhead("Pinned modules", "extension")
        table(d.get("modules"), [("Module", "source"), ("Version", "version")])
    if d.get("validate_output"):
        subhead("terraform validate", "verified")
        st.code(str(d["validate_output"]), language="text")
    coverage = rows_of(d.get("spec_coverage"))
    if coverage:
        conflicts = as_dict(f.get("conflicts"))
        subhead("Spec coverage, as the model explains it", "lightbulb")
        st.caption("Written by the model. Where it disagrees with the tool-verified spec checks, the checks win.")
        table(coverage, [("Requirement", "requirement"), ("How (model-written)", "how"),
                         ("Tool data", lambda r: f"Disagrees · {conflicts[str(r.get('requirement'))]}"
                          if str(r.get("requirement")) in conflicts else "")],
              styles={"Tool data": lambda v: f"color:{T['warning']};font-weight:600" if v else ""},
              nowrap=["Requirement"])


# ---- k8s validation


def k8s_overview(d: dict, artifacts: dict, f: dict) -> None:
    if f.get("found") is not None:
        html_block(flow_html(f["found"], f.get("fixed") or 0, f.get("left") or 0))
    drift = rows_of(d.get("drift_injected"))
    if drift:
        st.markdown(f"**Simulated drift** · {plural(len(drift), 'hand-edit violation')} injected before validation "
                    "(a demo aid, not in the generated manifests)")
        table(drift, [("File", lambda r: str(r.get("file") or "").removeprefix("k8s/")), ("Hand edit", "change"),
                      ("Rule", lambda r: nb(r.get("rule"))), ("Fixed", lambda r: (YES if r["fixed"] else NO)
                                         if isinstance(r.get("fixed"), bool) else "")],
              styles={"Fixed": _result_css}, nowrap=["Rule", "Fixed"])
    live = as_dict(d.get("live_deploy"))
    smoke = smoke_rows(live.get("smoke_tests"))
    if smoke:
        subhead(f"Smoke tests on {live.get('cluster') or 'the cluster'}", "rocket_launch")
        table(smoke, [("Request", "request"), ("Expected", lambda r: r["expected"] if r["expected"] is not None else "–"),
                      ("Actual", lambda r: r["actual"] if r["actual"] is not None else "–"),
                      ("Result", lambda r: {True: RESULT_WORDS["pass"], False: RESULT_WORDS["fail"]}.get(r["ok"], "–")),
                      ("Time", lambda r: f"{r['ms']:.0f} ms" if r.get("ms") is not None else "")],
              styles={"Result": _result_css}, nowrap=["Expected", "Actual", "Result", "Time"])
    elif str(live.get("status") or "").lower() in ("skipped", ""):
        st.caption(":material/info: Live deploy to kind was off for this run" +
                   (f": {md(plain(live.get('reason') or live.get('rollout')))}" if live.get("reason") or
                    live.get("rollout") else "."))


def k8s_details(d: dict, artifacts: dict, f: dict) -> None:
    subhead("Checks", "policy")
    table(d.get("checks"), [("Result", lambda r: result_word(r.get("status"))),
                            ("Check", lambda r: " · ".join(x for x in (nb(r.get("id")), cell(r.get("title"))) if x)),
                            ("Tool", "tool"), ("Target", lambda r: r.get("target") or r.get("resource")),
                            ("Details", "details")],
          styles={"Result": _result_css}, nowrap=["Result"], scroll_after=8)
    if rows_of(d.get("fixes_applied")):
        subhead("Fixes the model applied", "build")
        table(d.get("fixes_applied"), [("File", "file"), ("Change", "change"), ("Resolves", lambda r: nb(r.get("resolves")))])
    live = as_dict(d.get("live_deploy"))
    timings = as_dict(live.get("timings"))
    kv([("Status", cap(live.get("status"))), ("Cluster", live.get("cluster")), ("Image", live.get("image")),
        ("Namespace", live.get("namespace")), ("Overlay", live.get("overlay")),
        ("Total time", fmt_secs(timings["total_s"]) if timings.get("total_s") is not None else None),
        ("Rollout", short(live.get("rollout"), 120) if live.get("rollout") else None)], "Live deploy", "rocket_launch")
    if live.get("access"):
        st.code(str(live["access"]), language="bash")
    if d.get("notes"):
        st.caption(":material/info: " + md(plain(d["notes"])))


# ---- finops


def finops_header(d: dict, f: dict) -> None:
    """The alert bar and the chart: both visible when the card lands."""
    if alert := finops_alert(f):
        html_block(alert_html(*alert))
    if f.get("designed") is not None:
        st.altair_chart(cost_chart(f.get("today"), f.get("designed"), f.get("optimized"), f.get("budget"),
                                   f.get("surcharge") or 0.0, f.get("over_designed")), width="stretch")


def finops_overview(d: dict, artifacts: dict, f: dict) -> None:
    recs = rows_of(d.get("recommendations"))
    fo1 = f.get("fo1") if isinstance(f.get("fo1"), dict) else None
    if fo1:
        saving = money(fo1.get("monthly_savings_usd"))
        label = plain(fo1.get("id")) or "Top recommendation"
        if num(fo1.get("monthly_savings_usd")):
            label += f" · saves {saving}/month"
        label += f" · {plain(fo1.get('title'))}" if fo1.get("title") else ""
        with st.expander(md(label), icon=":material/lightbulb:"):
            html_block(kv_html([("Saves per month", money(fo1.get("monthly_savings_usd"), cents=True)),
                                ("Effort", cap(fo1.get("effort"))), ("Risk", cap(fo1.get("risk"))),
                                ("Applies to", cap(fo1.get("applies_to"))), ("Scope", fo1.get("scope"))]))
            if fo1.get("change"):
                st.markdown(f"**Change.** {md(plain(fo1['change']))}")
            if fo1.get("rationale") and plain(fo1["rationale"]) != plain(fo1.get("change")):
                st.markdown(f"**Why.** {md(plain(fo1['rationale']))}")
    others = [r for r in recs if r is not fo1]
    if others:
        subhead("Other recommendations", "savings")
        table(others, [("ID", lambda r: nb(r.get("id"))), ("Recommendation", "title"),
                       ("Saves / month", lambda r: money(r.get("monthly_savings_usd"), cents=True)),
                       ("Effort", lambda r: cap(r.get("effort"))), ("Risk", lambda r: cap(r.get("risk"))),
                       ("Applies to", lambda r: cap(r.get("applies_to"))), ("Scope", "scope")],
              nowrap=["ID", "Saves / month", "Effort", "Risk"])


def rightsizing_pairs(rs: dict) -> list[tuple[str, Any]]:
    """Right-sizing as key-value pairs a person can read: "Pod requests: 200m CPU · 128Mi memory"."""
    rs, out, used = as_dict(rs), [], set()

    def res(value: Any) -> str | None:
        v = as_dict(value)
        parts = [f"{v['cpu']} CPU" if v.get("cpu") else "", f"{v['memory']} memory" if v.get("memory") else ""]
        return " · ".join(p for p in parts if p) or None

    for key, label in (("pod_requests", "Pod requests"), ("pod_limits", "Pod limits")):
        if key in rs:
            out.append((label, res(rs[key]) or fmt_value(rs[key])))
            used.add(key)
    if rs.get("replicas_min") is not None:
        out.append(("Replicas", f"{rs['replicas_min']}–{rs.get('replicas_max', '?')}"))
        used |= {"replicas_min", "replicas_max"}
    if rs.get("node_instance_types"):
        count = f" × {rs['node_min']}–{rs.get('node_max', '?')}" if rs.get("node_min") is not None else ""
        out.append(("Nodes", f"{fmt_value(rs['node_instance_types'])}{count}"))
        used |= {"node_instance_types", "node_min", "node_max"}
    named = {"db_instance_class": "Database", "kubernetes_version": "Kubernetes"}
    for key, value in rs.items():
        if key not in used:
            out.append((named.get(key, humanize(key)), value))
    return out


def finops_details(d: dict, artifacts: dict, f: dict) -> None:
    kv(rightsizing_pairs(d.get("rightsizing")), "Right-sizing", "straighten")
    items = []
    for key, label in (("current", "Today"), ("target_as_designed", "As designed"), ("target_optimized", "Optimized")):
        items += [{**i, "scenario": label} for i in rows_of(as_dict(d.get(key)).get("items"))]
    if items:
        subhead("Cost line items, per month", "receipt_long")
        table(items, [("Scenario", "scenario"), ("Item", "item"),
                      ("Per month", lambda r: money(r.get("monthly_usd"), cents=True)), ("How", "calc")],
              nowrap=["Scenario", "Per month"], scroll_after=8)
    if rows_of(d.get("node_fit")):
        subhead("Node fit (cheapest first)", "grid_view")
        table(d.get("node_fit"), [("Node type", "instance_type"), ("Nodes", "nodes"),
                                  ("Per month", lambda r: money(r.get("monthly_usd"), cents=True)),
                                  ("Fits", lambda r: (YES if r["fits"] else NO)
                                   if isinstance(r.get("fits"), bool) else "")],
              styles={"Fits": _result_css})
    notes = [plain(n) for n in as_list(as_dict(d.get("assumptions")).get("notes")) if n]
    if notes:
        with st.expander("Assumptions", icon=":material/rule:"):
            st.markdown("\n".join(f"- {md(n)}" for n in notes))
    if d.get("pricing_source"):
        st.caption(f":material/sell: Prices: {md(plain(d['pricing_source']))}"
                   + (f" (fetched {md(plain(d['pricing_date']))})" if d.get("pricing_date") else ""))


# A field every contract-conforming result has (docs/CONTRACTS.md); without it the highlights would be empty.
CONTRACT_KEYS = {"discovery": "findings", "requirements": "requirements", "modernization": "changes",
                 "terraform": "valid", "k8s_validation": "checks", "finops": "recommendations"}
OVERVIEW: dict[str, Callable[[dict, dict, dict], None]] = {
    "discovery": discovery_overview, "requirements": requirements_overview, "modernization": modernization_overview,
    "terraform": terraform_overview, "k8s_validation": k8s_overview, "finops": finops_overview}
DETAILS: dict[str, Callable[[dict, dict, dict], None]] = {
    "discovery": discovery_details, "requirements": requirements_details, "modernization": modernization_details,
    "terraform": terraform_details, "k8s_validation": k8s_details, "finops": finops_details}


# --------------------------------------------------------------------------- the stage message (live, then final)


def _event_times(job: StageJob) -> dict[int, float]:
    """When each event arrived (index -> time), kept on the job so a rerun still knows the tool durations."""
    times = getattr(job, "_ui_event_times", None)
    if not isinstance(times, dict):
        times = {}
        try:
            job._ui_event_times = times  # UI-only bookkeeping: the job's events and behaviour are untouched
        except Exception:  # noqa: BLE001
            pass
    return times


class StageView:
    """One stage attempt as an assistant message: a live card while the agent works, then the result card."""

    def __init__(self, job: StageJob):
        self.job, self.seen = job, 0
        self.stage, self.attempt = job.stage, whole(getattr(job, "attempt", 1)) or 1
        self.status, self.model, self.text, self.error = "", "", "", ""
        self.scrubber, self.new_turn = TextScrubber(), False
        self.tools: dict[str, dict[str, Any]] = {}
        self.result: dict | None = None
        self.times = _event_times(job)
        self.header_slot = self.phases_slot = self.text_slot = self.tools_box = self.tools_slot = None
        with st.chat_message("assistant", avatar=AGENT_AVATAR):
            self.live_holder = st.empty()
            if not job.done:
                self._build_live()
            self.result_slot = st.container()

    def _build_live(self) -> None:
        try:
            with self.live_holder.container():
                with keyed_container(container_key("live", self.stage, self.attempt), self.job.id):
                    self.header_slot = st.empty()
                    self.phases_slot = st.empty()
                    self.text_slot = st.empty()
                    # The phase checklist tells the story while the agent works; the step-by-step timeline repeats
                    # it, so it stays collapsed (expanded only for a stage without a checklist).
                    self.tools_box = st.status("Timeline", expanded=not PHASES.get(self.stage), state="running")
                    with self.tools_box:
                        self.tools_slot = st.empty()
        except Exception:  # noqa: BLE001 - the final card still renders
            self.header_slot = None

    def _timing(self) -> bool:
        """Record arrival times only while the job is live (a job loaded from disk has no real timing)."""
        if not self.job.done:
            return True
        finished = getattr(self.job, "finished", None)
        return finished is not None and time.time() - finished < 1.5

    def consume(self) -> bool:
        """Apply events that arrived since the last call; True when something changed."""
        start = self.seen
        new = self.job.events[start:]
        self.seen += len(new)
        timing, now = self._timing(), time.time()
        for offset, event in enumerate(new):
            index = start + offset
            if timing:
                self.times.setdefault(index, now)
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in ("tool_start", "tool_end"):
                self.new_turn = True  # the model's next words start a new turn: never glue them to the last ones
            if kind == "status":
                self.status, self.model = str(event.get("message") or ""), event.get("model") or self.model
            elif kind == "text":
                out = self.scrubber.feed(str(event.get("delta") or ""))
                if out and self.new_turn:
                    self.text = self.text.rstrip() + "\n\n" if self.text.strip() else self.text
                    out, self.new_turn = out.lstrip(), False
                self.text += out
            elif kind == "tool_start":
                tool_id = str(event.get("id") or f"tool-{len(self.tools)}")
                self.tools[tool_id] = {"name": str(event.get("name") or "?"), "input": event.get("input") or "",
                                       "status": "running", "output": "", "t0": self.times.get(index), "t1": None}
            elif kind == "tool_end":
                tool_id = str(event.get("id") or "")
                tool = self.tools.get(tool_id)
                if tool is None:
                    tool = self.tools.setdefault(tool_id or f"tool-{len(self.tools)}", {
                        "name": str(event.get("name") or "?"), "input": "", "t0": None})
                failed = str(event.get("status") or "success").lower() in ("error", "failed", "fail")
                tool.update(status="error" if failed else "success", output=event.get("output") or "",
                            t1=self.times.get(index))
            elif kind == "result":
                self.result = event
                self.text += self.scrubber.flush()
            elif kind == "error":
                self.error = str(event.get("message") or "agent error")
                self.text += self.scrubber.flush()
        return bool(new)

    def narration(self) -> str:
        """What the model said so far, scrubbed (a tail that may still grow into a marker is held back)."""
        return EMOJI_RE.sub("", scrub_text(self.text)).strip()

    def render_live(self) -> None:
        if self.header_slot is None:
            return
        try:
            now, tools = time.time(), list(self.tools.values())
            html_block(live_header_html(self.stage, self.attempt, now - self.job.started, self.status, self.model,
                                        presenter()), self.header_slot)
            html_block(phases_html(phase_views(self.stage, tools, running=True)), self.phases_slot)
            if line := latest_sentence(self.narration()):
                html_block(narration_html(line), self.text_slot)
            # no `expanded` here: the timeline stays as it was built (collapsed), or as the viewer toggled it
            self.tools_box.update(label=f"Timeline · {plural(len(tools), 'step')} so far", state="running")
            html_block(timeline_html(tools, LIVE_TOOL_LINES, now), self.tools_slot)
        except Exception:  # noqa: BLE001 - a paint glitch must never stop the live loop
            pass

    def render_final(self) -> None:
        self.consume()
        self.text += self.scrubber.flush()
        try:
            self.live_holder.empty()
        except Exception:  # noqa: BLE001
            pass
        failed = self.result is None
        with self.result_slot:
            try:
                if failed:
                    render_failure(self.stage, self.attempt, self.error, self.job.id)
                else:
                    render_result(self.stage, self.result, key=f"{self.stage}-{self.attempt}-{self.job.id}",
                                  attempt=self.attempt, warning=self.error)
            except Exception as exc:  # noqa: BLE001 - last line of defence: the page keeps working
                st.error(f"Could not show this result ({type(exc).__name__}: {exc}).")
            try:
                self._process_row(failed)
            except Exception:  # noqa: BLE001
                pass

    def _process_row(self, failed: bool) -> None:
        """The collapsed timeline and the full narration, side by side under the card."""
        tools, story = list(self.tools.values()), self.narration()
        if not tools and not story:
            return
        left, right = st.columns(2) if tools and story else (st.container(), None)
        if tools:
            with left:
                box = st.status(f"Timeline · {plural(len(tools), 'step')}", expanded=False,
                                state="error" if failed else "complete")
                with box:
                    html_block(timeline_html(tools))
        if plain(story):
            with right or left, st.expander("What the agent said", icon=":material/chat:"):
                st.markdown(md(story))


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


def card_header(stage: str, attempt: int, metrics: dict, warning: str = "") -> str:
    seconds = num(as_dict(metrics).get("latency_s"))
    state, word = ("warning", "Done, with a warning") if warning else ("done", "Done")
    right = status_pill(state, f"{word} · {fmt_secs(seconds)}" if seconds is not None else word)
    return header_html(stage, attempt, right)


def render_failure(stage: str, attempt: int, error: str, fallback: str = "") -> None:
    with keyed_container(container_key("card", stage, attempt), fallback):
        html_block(header_html(stage, attempt, status_pill("failed", "Didn't finish")))
        st.error(error or "The agent stream ended without a result.", icon=":material/error:")


def render_result(stage: str, result: dict, key: str, *, attempt: int = 1, warning: str = "") -> None:
    """The result card: header, headline, KPIs, evidence and decision, telemetry, tabs and the bridge strip."""
    result = as_dict(result)
    data, metrics = as_dict(result.get("data")), as_dict(result.get("metrics"))
    artifacts = {str(k): v if isinstance(v, str) else json.dumps(v, indent=2)
                 for k, v in as_dict(result.get("artifacts")).items()}
    follows = CONTRACT_KEYS.get(stage, "") in data
    facts = stage_facts(stage, data, artifacts, metrics) if follows else {}
    summary = plain(result.get("summary"))
    revised = (whole(attempt) or 1) > 1 or summary.lower().startswith("revised")
    summary = re.sub(r"^Revised:\s*", "", summary, flags=re.I)
    with keyed_container(container_key("card", stage, attempt), key):
        html_block(card_header(stage, attempt, metrics, warning))
        if warning:
            st.warning(f"The agent reported an error before its result: {warning}", icon=":material/warning:")
        charted = False  # the FinOps chart is on the card
        if stage == "finops" and follows:
            charted = guarded(finops_header, data, facts, label="the cost chart") and facts.get("designed") is not None
        lead, rest = headline(stage, facts), summary
        if not lead and summary:
            lead = first_sentence(summary)
            rest = summary[len(first_sentence(summary, 10_000)):].strip()
        # one line of summary at most, none in presenter mode: the hero must clear the sticky gate
        html_block(headline_html(lead, "" if presenter() else rest, revised))
        # FinOps in presenter mode: the chart above already carries today / as designed / optimized
        if kpis := [] if charted and presenter() else stage_kpis(stage, facts):
            with keyed_container(container_key("kpis", stage, attempt), key, horizontal=True, gap="medium"):
                for k in kpis:
                    show_metric(k)
        if pair := evidence(stage, facts):
            html_block(evidence_html(*pair))
        if not presenter() and (line := telemetry_line(metrics)):
            st.caption(f":material/memory: {md(line)}")
        if not follows:
            st.info(f"This result doesn't follow the {stage} contract (a stand-in agent?), so the details show its "
                    "raw data.", icon=":material/info:")
        overview, details, files = st.tabs(["Overview", "Details", "Files"])
        with overview:
            if follows:
                if summary:  # the card shows one line of it at most
                    html_block(summary_html(summary))
                guarded(OVERVIEW[stage], data, artifacts, facts, label="the overview")
            elif summary:
                st.markdown(md(summary))
        with details:
            if follows:
                guarded(DETAILS[stage], data, artifacts, facts, label="the details")
            if presenter() and not follows:
                html_block(kv_html([(humanize(k), v) for k, v in data.items() if not isinstance(v, dict | list)]))
            raw_json(data)
        with files:
            guarded(files_tab, artifacts, key, stage, label="the files")
        if show_bridges():
            bridge_strip(stage, attempt, key)


def bridge_strip(stage: str, attempt: int = 1, fallback: str = "") -> None:
    bridge, benefit = meta_attr(stage, "bridge"), meta_attr(stage, "benefit")
    if not (bridge or benefit):
        return
    lines = ([f"**In AWS Transform** · {md(bridge)}"] if bridge else []) + (
        [f"**Benefit** · {md(benefit)}"] if benefit else [])
    with keyed_container(container_key("bridge", stage, attempt), fallback):
        st.markdown("\n\n".join(lines))


# --------------------------------------------------------------------------- finale


def minutes_phrase(seconds: Any) -> str:
    s = num(seconds)
    if not s:
        return ""
    if s < 45:
        return "Under a minute"
    minutes = max(1, round(s / 60))
    return f"About {minutes} minute{'s' if minutes != 1 else ''}"


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def finale_facts(pipeline: Any) -> dict:
    """Every number of the wrap-up, from the pipeline's state and results (never hard-coded)."""
    per, seconds, tokens, timed = {}, 0.0, 0.0, False
    for stage in STAGES:
        result = as_dict(_call(lambda s=stage: pipeline.result(s)))
        data = as_dict(result.get("data")) or as_dict(_call(lambda s=stage: pipeline.data(s)))
        metrics = as_dict(_call(lambda s=stage: pipeline.state.stages[s].metrics)) or as_dict(result.get("metrics"))
        if num(metrics.get("latency_s")) is not None:
            seconds += num(metrics["latency_s"])
            timed = True
        tokens += (num(metrics.get("input_tokens")) or 0) + (num(metrics.get("output_tokens")) or 0)
        per[stage] = stage_facts(stage, data, result.get("artifacts"), metrics) if data else {}
    statuses = [_call(lambda s=stage: pipeline.status(s)) for stage in STAGES]
    return {"stages": per, "seconds": seconds if timed else None, "tokens": tokens or None,
            "approved_all": all(s == "approved" for s in statuses), "run_id": _call(lambda: pipeline.run_id) or ""}


def finale_title(f: dict) -> str:
    agents = NUMBER_WORDS.get(len(STAGES), str(len(STAGES))).capitalize()
    parts = ["One server.", f"{agents} agents."]
    if phrase := minutes_phrase(as_dict(f).get("seconds")):
        parts.append(f"{phrase}.")
    if as_dict(f).get("approved_all"):
        parts.append("You approved every step.")
    return " ".join(parts)


def finale_tiles(f: dict) -> list[Kpi]:
    s = as_dict(as_dict(f).get("stages"))
    disc, req, mod = as_dict(s.get("discovery")), as_dict(s.get("requirements")), as_dict(s.get("modernization"))
    tf, k8s, fin = as_dict(s.get("terraform")), as_dict(s.get("k8s_validation")), as_dict(s.get("finops"))
    tiles = []
    if disc.get("findings") is not None:
        tiles.append(Kpi("Findings", str(disc["findings"]),
                         f"{disc.get('critical', 0)} critical" if disc.get("critical") else None, "red"))
    if req.get("requirements") is not None:
        tiles.append(Kpi("Requirements", str(req["requirements"]),
                         f"{req['traced']} traced" if req.get("traced") else None, "gray"))
    if mod.get("tests_total"):
        ok = mod.get("tests_passed") == mod["tests_total"]
        tiles.append(Kpi("Tests", f"{mod['tests_passed']}/{mod['tests_total']}", "passing" if ok else "failing",
                         "green" if ok else "red"))
    if tf.get("valid") is not None:
        tiles.append(Kpi("Infrastructure", "Valid" if tf["valid"] else "Not valid",
                         f"{tf.get('checks_passed', 0)}/{tf['checks_total']} spec checks" if tf.get("checks_total")
                         else "terraform validate", "green" if tf["valid"] else "red"))
    if k8s.get("smoke_total"):
        ok = k8s.get("smoke_passed") == k8s["smoke_total"]
        tiles.append(Kpi("Smoke tests", f"{k8s['smoke_passed']}/{k8s['smoke_total']}",
                         "live on kind" if k8s.get("deploy") == "pass" else None, "green" if ok else "red"))
    path = [money(fin[k]) for k in ("today", "designed", "optimized") if fin.get(k) is not None]
    if len(path) >= 2:
        tiles.append(Kpi("Monthly cost", " → ".join(path), "today → as designed → optimized"
                         if len(path) == 3 else None, "gray"))
    if fin.get("extended") and fin.get("surcharge"):
        tiles.append(Kpi("Caught before go-live", f"{money(fin['surcharge'])}/mo", "EKS extended support", "green"))
    return tiles


def render_finale(pipeline: Any) -> None:
    """The wrap-up when all six stages are approved (app.py calls it inside its own container)."""
    try:
        f = finale_facts(pipeline)
        tiles = finale_tiles(f)
        with keyed_container("finale", "finale"):
            title = finale_title(f)
            lead, _, tail = title.partition(" You approved")
            second = f'<div style="color:{T["indigo_pressed"]}">You approved{esc(tail)}</div>' if tail else ""
            html_block(f'<div style="display:flex;align-items:center;gap:{rem(16)}">{tile("verified", 52)}'
                       f'<div style="font-size:{rem(26)};line-height:1.3;font-weight:700;color:inherit">'
                       f'<div>{esc(lead)}</div>{second}</div></div>')
            wide = next((t for t in tiles if t.label == "Monthly cost"), None)
            first = [t for t in tiles if t is not wide][:4]
            rest = [t for t in tiles if t is not wide and t not in first]
            for row, weights in ((first, None), ([*rest[:1], *([wide] if wide else []), *rest[1:]], "wide")):
                if not row:
                    continue
                # the cost path needs its width; "EKS extended support" fits beside it (CSS wraps it if it must)
                spec = [2.4 if t is wide else 1.5 if t.label.startswith("Caught") else 0.9 for t in row] if weights \
                    else len(row)
                for col, t in zip(st.columns(spec), row, strict=False):
                    show_metric(t, col)
            caption = []
            if f.get("seconds"):
                caption.append(f"Machine time {fmt_secs(f['seconds'])}")
            if f.get("tokens") and not presenter():
                caption.append(f"{fmt_tokens(f['tokens'])} tokens")
            if f.get("run_id"):
                caption.append(f"run {f['run_id']}")
            if caption:
                st.caption(":material/schedule: " + md(" · ".join(caption)))
            merged = _call(lambda: pipeline.merged_artifacts()) or {}
            if merged:
                st.download_button("Download all artifacts", zip_bytes(merged), type="primary",
                                   icon=":material/download:", file_name=f"{f.get('run_id') or 'migration'}-artifacts.zip",
                                   mime="application/zip", key="zip-finale", on_click="ignore")
    except Exception:  # noqa: BLE001 - the wrap-up must never break the page
        st.success("Migration complete. All six steps are approved.", icon=":material/verified:")


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


def files_tab(files: dict[str, str], key: str, stage: str) -> None:
    """The file tree next to the browser (a long tree scrolls in place instead of leaving a dead zone)."""
    if not files:
        st.caption("This step produced no files.")
        return
    tree = file_tree(files)
    if tree.count("\n") >= 8:
        left, right = st.columns([2, 5])
        with left:
            st.caption(f":material/folder: {plural(len(files), 'file')}")
            with st.container(height=420, border=False):
                st.code(tree, language="text")
        with right:
            artifacts_browser(files, key=key, zip_name=f"{stage}-artifacts.zip")
    else:
        artifacts_browser(files, key=key, zip_name=f"{stage}-artifacts.zip")


def artifacts_browser(files: dict[str, str], key: str, zip_name: str) -> None:
    if not files:
        st.caption("No artifacts.")
        return
    left, right = st.columns([4, 2], vertical_alignment="bottom")
    path = left.selectbox("File", sorted(files), key=f"file-{key}")
    right.download_button("Download .zip", zip_bytes(files), file_name=zip_name, mime="application/zip",
                          key=f"zip-{key}", on_click="ignore", icon=":material/download:", width="stretch")
    content = files.get(path, "")
    language = language_for(path)
    if language == "markdown" and not st.toggle("Show source", key=f"src-{key}"):
        with st.container(height=420, border=True):
            st.markdown(content)
    else:
        st.code(content, language=language, line_numbers=True, height=420 if content.count("\n") > 18 else "content")
