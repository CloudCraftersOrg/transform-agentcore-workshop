"""Look and feel: stage metadata, design tokens, the CSS and small HTML fragments (nav, top bar, strip, scroll).

The approved design is docs/design/mockup-*.html (rules in docs/design/GUIDELINES.md): one frosted app window on a
pastel mesh, a transparent nav and a lighter content pane, Inter 300/400/500/600, hairlines instead of boxes.
"""

import json
import re
from dataclasses import dataclass
from html import escape
from string import Template


@dataclass(frozen=True)
class StageMeta:
    icon: str  # ":material/<symbol>:" (st.chat_message avatar, markdown); kept for callers that still read it
    title: str
    does: str
    analogue: str
    symbol: str = ""  # Material Symbols name: ":material/<symbol>:" in markdown, icon=":material/<symbol>:" on widgets
    short: str = ""  # nav label
    doing: str = ""  # live status phrase while the agent runs
    bridge: str = ""  # "In AWS Transform" line (docs/KEYNOTE-SCRIPT.md)
    benefit: str = ""  # benefit line (docs/KEYNOTE-SCRIPT.md)


STAGE_META = {
    "discovery": StageMeta(
        ":material/travel_explore:", "Discovery & Assessment",
        "Inventory the EC2 box and the Flask code, find risks, pick a 6R strategy",
        "Discovery / Assessment", symbol="travel_explore", short="Discovery",
        doing="Reading the code and the infrastructure",
        bridge="Discovery and assessment at portfolio scale: hundreds of servers, dependency mapping, application "
               "grouping and the business case.",
        benefit="Weeks of archaeology become an evidence-backed starting point."),
    "requirements": StageMeta(
        ":material/checklist:", "Requirements", "Turn the uploaded request into a traceable migration spec",
        "Job Objective Intake + HITL Clarifications", symbol="checklist", short="Requirements",
        doing="Turning the request into a migration spec",
        bridge="Every job follows the same pattern: agents propose, people review and approve each step.",
        benefit="Speed without giving up control."),
    "modernization": StageMeta(
        ":material/deployed_code:", "Code Modernization",
        "Refactor the monolith into a microservice, Dockerfile and manifests",
        "Code Transformation + Containerization", symbol="deployed_code", short="Modernization",
        doing="Porting the business logic",
        bridge="Code transformation and containerization: .NET porting, Java and Python upgrades, mainframe "
               "reimagining, and AWS Transform custom for your own patterns.",
        benefit="The repetitive 80% is done consistently; experts spend their time on the 20% that matters."),
    "terraform": StageMeta(
        ":material/stacks:", "Terraform IaC", "Adapt a validated EKS blueprint: VPC, EKS, ECR, RDS",
        "Network / Infrastructure Conversion", symbol="stacks", short="Infrastructure",
        doing="Adapting the validated blueprint",
        bridge="Network and infrastructure conversion to infrastructure as code: CloudFormation, CDK or Terraform.",
        benefit="A landing zone in days instead of a quarter of hand-written templates."),
    "k8s_validation": StageMeta(
        ":material/verified:", "K8s Validation", "Schema and policy checks, auto-fixes, optional live deploy to kind",
        "Test & Validation", symbol="verified", short="Validation",
        doing="Checking policies and deploying",
        bridge="Validation is part of every job: tests, test environments and cutover checks.",
        benefit="You don't find out in production."),
    "finops": StageMeta(
        ":material/savings:", "FinOps Right-Sizing", "Current vs target monthly cost, right-sizing and savings",
        "Assessment TCO / Business Case", symbol="savings", short="FinOps",
        doing="Pricing the current and the target estate",
        bridge="Assessment and business case: know the cost before you migrate.",
        benefit="No surprises on the first invoice."),
}

# Design tokens (docs/design/mockup-*.html). ink3 is #737380, not the mockup's #8b8b97, so small grey text keeps
# WCAG AA (4.5:1) on white. Charts and inline HTML use these, so CSS, charts and markup stay consistent.
TOKENS = {
    "ink": "#18181d", "ink2": "#5d5d69", "ink3": "#737380",
    "hair": "rgba(20,20,40,.08)", "hair2": "rgba(20,20,40,.05)",
    "indigo": "#5b5fc7", "indigo_ink": "#4b4fb4", "indigo_wash": "rgba(91,95,199,.07)",
    "ok": "#1f7a3a", "warn": "#a15c07", "bad": "#c4314b",
    "dot_ok": "#34a853", "dot_warn": "#e0a106", "dot_idle": "#c7c7d1",
    "glass": "rgba(255,255,255,.58)", "pane": "rgba(255,255,255,.86)", "canvas": "#f4f2fa",
    # charts: muted, with colour only where it means something
    "chart_today": "#c9c9d3", "chart_designed": "#efc6cd", "chart_over": "#c4314b", "chart_optimized": "#5b5fc7",
    "chart_budget": "#737380", "chart_grid": "rgba(20,20,40,.06)",
    # older names, kept for callers that still read them
    "text": "#18181d", "text2": "#5d5d69", "disabled": "#737380", "border": "rgba(20,20,40,.08)",
    "indigo_pressed": "#4b4fb4", "indigo_tint": "rgba(91,95,199,.07)",
    "success": "#1f7a3a", "warning": "#a15c07", "danger": "#c4314b",
}

# Container keys shared by app.py (shell) and views.py (cards); CSS in this module targets `.st-key-<key>`.
#   views.py creates: live-<stage>, card-<stage>, process-<stage>…, bridge-<stage> (unused now), finale
#   app.py creates:   topbar (+ tb-row, tb-left, tb-mid, tb-right), gate (+ gate-row), welcome, welcome-input,
#                     suggestions, sg-<name>, gate-step, wrapup, recap, transcript, stage-<job id>

STATUS_WORDS = {"pending": "Not Started", "running": "In Progress", "awaiting_approval": "Needs Review",
                "approved": "Approved", "failed": "Failed"}

# Friendly model names for the nav; the raw Bedrock model id stays in the tooltip.
MODEL_NAMES = [("nova-2-lite", "Nova 2 Lite"), ("nova-2-pro", "Nova 2 Pro"), ("nova-premier", "Nova Premier"),
               ("nova-pro", "Nova Pro"), ("nova-lite", "Nova Lite"), ("nova-micro", "Nova Micro"),
               ("deepseek.v3.2", "DeepSeek V3.2"), ("deepseek.v3", "DeepSeek V3"), ("deepseek.r1", "DeepSeek R1"),
               ("mistral-large-3", "Mistral Large 3"), ("ministral-3-14b", "Ministral 3 14B")]


def friendly_model(model_id: str) -> str:
    low = (model_id or "").lower()
    return next((name for needle, name in MODEL_NAMES if needle in low), model_id or "")


# --------------------------------------------------------------------------- capitalization (GUIDELINES.md)

# Lowercase inside a Title Case label unless first or last: articles, coordinating conjunctions and prepositions of
# four letters or fewer.
SMALL_WORDS = frozenset("a an the and but or nor for so yet as at by in of on to up via with from into onto per vs".split())
# Product names and acronyms keep their own casing (matched case-insensitively on lowercase input).
PROTECTED = {w.lower(): w for w in (
    "AgentCore", "FinOps", "AWS", "EKS", "ECR", "RDS", "VPC", "IaC", "K8s", "kind", "API", "APIs", "CPU", "DB", "AZ",
    "AZs", "NAT", "ID", "IDs", "SLO", "RPO", "RTO", "URL", "TLS", "HPA", "PDB", "HA", "S3", "EC2", "GiB", "MiB", "USD",
    "TCO", "HITL", "JSON", "YAML", "HCL", "SQL", "SQLite", "PostgreSQL", "CloudWatch", "DeepSeek", "Nova", "Bedrock",
    "Strands", "Terraform", "Kubernetes", "Graviton", "Flask", "pytest", "kubeconform", "6R")}
_WORD_RE = re.compile(r"(\s+)")


def _title_word(word: str, first: bool, last: bool) -> str:
    if not word or any(ch in word for ch in "./_`@\\") or any(ch.isdigit() for ch in word):
        return word  # code, paths, versions and numbers keep their casing
    if "-" in word.strip("-"):
        return "-".join(_title_word(part, True, True) for part in word.split("-"))
    lead = len(word) - len(word.lstrip("([\"'“‘"))
    core = word[lead:]
    bare = core.rstrip(")]\"'”’:;,!?…")
    tail = core[len(bare):]
    if not bare:
        return word
    if bare.lower() in PROTECTED and bare == bare.lower():
        return word[:lead] + PROTECTED[bare.lower()] + tail
    if any(ch.isupper() for ch in bare):
        return word  # already cased on purpose ("AgentCore", "Discovery")
    if bare in SMALL_WORDS and not first and not last:
        return word
    return word[:lead] + bare[:1].upper() + bare[1:] + tail


def title_case(label: object) -> str:
    """Title Case for labels that name things (buttons, columns, KPI labels, statuses): first, last and major words
    capitalized; short articles, conjunctions and prepositions lowercase; hyphenated parts both capitalized; product
    names, acronyms and code untouched. "finops right-sizing" -> "FinOps Right-Sizing"."""
    parts = _WORD_RE.split(str(label or "").strip())
    words = [i for i, p in enumerate(parts) if p and not p.isspace()]
    if not words:
        return ""
    for n, i in enumerate(words):
        # a word right after a separator such as "·", ":" or "(" starts a new label part
        prev = parts[words[n - 1]] if n else ""
        starts = n == 0 or prev in ("·", "/", "–", "—", "→") or prev.endswith(":")
        ends = n == len(words) - 1 or parts[words[n + 1]] in ("·", "/", "–", "—", "→")
        parts[i] = _title_word(parts[i], starts, ends)
    return "".join(parts)


CODE_LEADS = ("terraform validate", "kind", "pytest", "kubeconform", "kubectl")  # never capitalized: commands


def sentence_case(text: object) -> str:
    """Sentence case for captions and messages: only the first letter is raised ("passing" -> "Passing"), except
    when the text starts with a command or a lowercase product name ("terraform validate · 2 runs")."""
    s = str(text or "").strip()
    if not s or s.lower().startswith(CODE_LEADS) and s.split(" ", 1)[0].islower():
        return s
    return s[:1].upper() + s[1:]


# --------------------------------------------------------------------------- the CSS

BASE_PX = 15  # config.toml baseFontSize: the root size every rem below hangs off (presenter mode scales it)
_PX_RE = re.compile(r"(?<![\w.#])(\d*\.?\d+)px\b")


def rem(px: float) -> str:
    """A size written in px at the 15 px root, emitted in rem so presenter mode scales it with one variable."""
    return f"{px / BASE_PX:.4g}rem"


def px_to_rem(css: str) -> str:
    """Every px size above 2 px (hairlines stay hairlines) becomes rem. Lines marked "/* px */", media queries and
    --tx-top-offset (read by the scroll script, which needs px) are left alone."""
    out = []
    for line in css.splitlines():
        if "@media" in line or "/* px */" in line or "--tx-top-offset" in line:
            out.append(line)
        else:
            out.append(_PX_RE.sub(lambda m: m.group(0) if float(m.group(1)) <= 2 else rem(float(m.group(1))), line))
    return "\n".join(out)


MESH = ("radial-gradient(40% 55% at 8% 12%, rgba(167,139,250,.38), transparent 70%), "
        "radial-gradient(35% 50% at 92% 8%, rgba(244,114,182,.30), transparent 70%), "
        "radial-gradient(45% 60% at 85% 95%, rgba(56,189,248,.26), transparent 70%), "
        "radial-gradient(40% 50% at 15% 95%, rgba(196,181,253,.30), transparent 70%)")

_ROOT = Template("""
:root {
  --tx-scale: 1;
  --tx-ink: $ink; --tx-ink-2: $ink2; --tx-ink-3: $ink3; --tx-hair: $hair; --tx-hair-2: $hair2;
  --tx-indigo: $indigo; --tx-indigo-ink: $indigo_ink; --tx-indigo-wash: $indigo_wash;
  --tx-ok: $ok; --tx-warn: $warn; --tx-bad: $bad; --tx-dot-ok: $dot_ok; --tx-dot-warn: $dot_warn; --tx-dot-idle: $dot_idle;
  --tx-glass: $glass; --tx-pane: $pane; --tx-canvas: $canvas;
  --tx-mesh: $mesh;
  /* surfaces, shadows and states: every colour the CSS paints comes from here, so the dark theme only swaps tokens */
  --tx-card: #fff; --tx-float: rgba(255,255,255,.92); --tx-sg-bg: rgba(255,255,255,.9); --tx-toast: rgba(255,255,255,.97);
  --tx-code-block: #f8f8fb; --tx-code-bg: rgba(20,20,40,.04); --tx-code-ink: var(--tx-ink-2);
  --tx-window-border: rgba(255,255,255,.75);
  --tx-window-shadow: inset 0 1px 0 rgba(255,255,255,.6), 0 30px 80px rgba(40,32,90,.12);
  --tx-pane-shadow: 0 1px 2px rgba(20,20,40,.04);
  --tx-card-shadow: 0 1px 2px rgba(20,20,40,.03), 0 12px 40px rgba(40,32,90,.05);
  --tx-soft-shadow: 0 1px 2px rgba(20,20,40,.03);
  --tx-lift-shadow: 0 1px 2px rgba(20,20,40,.03), 0 10px 30px rgba(40,32,90,.07);
  --tx-ask-shadow: 0 1px 2px rgba(20,20,40,.04), 0 16px 48px rgba(40,32,90,.07);
  --tx-float-border: var(--tx-hair-2); --tx-float-shadow: 0 10px 30px rgba(40,32,90,.10);
  --tx-composer-shadow: 0 2px 10px rgba(40,32,90,.05);
  --tx-pop-shadow: 0 16px 48px rgba(40,32,90,.12), 0 1px 2px rgba(20,20,40,.04);
  --tx-dialog-shadow: 0 30px 80px rgba(40,32,90,.18), 0 1px 2px rgba(20,20,40,.05);
  --tx-backdrop: rgba(28,24,56,.16);
  --tx-track: rgba(20,20,40,.05); --tx-seg-on: #fff; --tx-seg-on-ink: var(--tx-indigo-ink);
  --tx-seg-on-shadow: 0 1px 2px rgba(20,20,40,.1);
  --tx-nav-hover: rgba(255,255,255,.55); --tx-nav-on: rgba(255,255,255,.9); --tx-nav-on-shadow: 0 1px 2px rgba(20,20,40,.06);
  --tx-nav-bar: var(--tx-indigo); --tx-accent: var(--tx-indigo); --tx-tile-bg: var(--tx-indigo-wash);
  --tx-chrome-hover: rgba(255,255,255,.7);
  --tx-btn-hover: #4b4fb4; --tx-btn-shadow: 0 1px 2px rgba(75,79,180,.35);
  --tx-ghost-border: var(--tx-hair); --tx-ghost-hover: rgba(20,20,40,.03); --tx-ghost-hover-border: rgba(20,20,40,.14);
  --tx-hover-2: rgba(20,20,40,.02); --tx-send-bg: var(--tx-hair-2);
  --tx-fade-0: rgba(255,255,255,0); --tx-fade-1: rgba(255,255,255,.96); --tx-fade-2: rgba(255,255,255,.99);
  --tx-sev-m: #b9b9c4; --tx-sev-l: #d6d6de; --tx-bad-wash: rgba(196,49,75,.06);
  --tx-mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  --tx-chat-h: 70px;       /* the composer (48 px) and its 12 px margin plus a 10 px gap: the sticky gate sits on it */
  --tx-top-offset: 84px;   /* what scrollIntoView keeps clear at the top: the sticky top bar plus air */
  --tx-col: 1120px;        /* the content column, gutters included */
}
""")

_CSS = """
html { font-size: calc(15px * var(--tx-scale)) !important; }  /* px */
html, body, [data-testid="stApp"] {
  font-feature-settings: "cv11", "ss01", "ss03"; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
[data-testid="stApp"] { background: var(--tx-mesh), var(--tx-canvas); color: var(--tx-ink); letter-spacing: -.005em; }
[data-testid="stApp"] strong, [data-testid="stApp"] b { font-weight: 600; }
[data-testid="stMarkdownContainer"] { line-height: 1.55; }

/* Material Symbols Rounded, weight 300: a self-hosted variable subset (ui/static), with Streamlit's own as fallback */
@font-face { font-family: "TX Symbols"; font-style: normal; font-weight: 100 700; font-display: block;
  src: url("app/static/MaterialSymbolsRounded.woff2") format("woff2"); }
.tx-ms, [data-testid="stIconMaterial"], [data-testid="stExpanderIcon"] {
  font-family: "TX Symbols", "Material Symbols Rounded" !important; font-weight: 300 !important;
  font-variation-settings: "wght" 300, "opsz" 20, "FILL" 0, "GRAD" 0; font-style: normal; letter-spacing: normal;
  text-transform: none; white-space: nowrap; direction: ltr; font-feature-settings: "liga";
  -webkit-font-smoothing: antialiased;
}
.tx-ms { display: inline-block; font-size: 20px; line-height: 1; vertical-align: middle; user-select: none; flex: none; }

/* ---------------------------------------------------------------- default chrome off */
header[data-testid="stHeader"] { background: transparent !important; height: 0 !important; min-height: 0 !important;
  pointer-events: none; box-shadow: none; }
[data-testid="stDecoration"], [data-testid="stStatusWidget"], [data-testid="stToolbarActions"],
[data-testid="stMainMenu"], [data-testid="stAppDeployButton"], [data-testid="stSidebarResizeHandle"] { display: none !important; }
header [data-testid="stToolbar"] { height: 0; min-height: 0; padding: 0; background: transparent; }
/* the expand-nav button (the nav is collapsed): a ghost icon button in the pane's top-left corner */
[data-testid="stExpandSidebarButton"] { position: absolute; top: 21px; left: 20px; width: 34px; height: 34px;
  border-radius: 10px; pointer-events: auto; color: var(--tx-ink-2); background: transparent; z-index: 60; }
[data-testid="stExpandSidebarButton"]:hover { background: var(--tx-hair-2); }
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] { font-size: 20px; width: 20px; height: 20px; }

/* ---------------------------------------------------------------- one app window: nav + content pane */
[data-testid="stAppViewContainer"] {
  top: 24px !important; left: 24px !important; right: 24px !important; bottom: 24px !important;
  width: auto !important; height: auto !important; border-radius: 28px; overflow: hidden;
  background: var(--tx-glass); border: 1px solid var(--tx-window-border);
  box-shadow: var(--tx-window-shadow);
}
section[data-testid="stSidebar"] { background: transparent !important; border: none !important; box-shadow: none !important; }
section[data-testid="stSidebar"][data-collapsed="false"] { width: 248px !important; min-width: 248px !important;
  max-width: 248px !important; }
[data-testid="stSidebarContent"] { background: transparent !important; margin: 0; width: 100%; height: 100%;
  border: none; border-radius: 0; box-shadow: none; padding: 22px 14px; display: flex; flex-direction: column; }
[data-testid="stSidebarHeader"] { position: absolute; top: 21px; right: 12px; height: 30px; min-height: 0; padding: 0;
  margin: 0; z-index: 2; }
[data-testid="stLogoSpacer"] { display: none; }
[data-testid="stSidebarCollapseButton"] button { width: 30px; height: 30px; border-radius: 9px; color: var(--tx-ink-3); }
[data-testid="stSidebarCollapseButton"] button:hover { background: var(--tx-chrome-hover); color: var(--tx-ink); }
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"] { font-size: 18px; }
[data-testid="stSidebarUserContent"] { padding: 0 !important; flex: 1 1 auto; display: flex; flex-direction: column;
  min-height: 0; }
[data-testid="stSidebarUserContent"] > div { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] { flex: 1 1 auto; gap: 0; }
[data-testid="stSidebarUserContent"] [data-testid="stElementContainer"]:has(.tx-side),
[data-testid="stSidebarUserContent"] [data-testid="stElementContainer"]:has(.tx-side) div:has(.tx-side) {
  flex: 1 1 auto; display: flex; flex-direction: column; margin: 0 !important; }

/* the pane: main is the scroller (a flex item of the window, or of a column wrapper once the composer exists) */
[data-testid="stAppViewContainer"] > div:has(> main.stMain) { flex: 1 1 0; min-width: 0; min-height: 0; display: flex;
  flex-direction: column; height: auto !important; align-self: stretch; }
main.stMain {
  flex: 1 1 0; min-width: 0; min-height: 0; width: auto !important; height: auto !important; align-self: stretch;
  margin: 8px 8px 8px 0; border-radius: 22px; background: var(--tx-pane);
  box-shadow: var(--tx-pane-shadow); padding: 0 !important;
}
[data-testid="stAppViewContainer"]:has(section[data-testid="stSidebar"][data-collapsed="true"]) main.stMain { margin-left: 8px; }
[data-testid="stMainBlockContainer"] { max-width: none !important; width: 100% !important; padding: 0 0 28px !important;
  margin: 0 !important; background: none; border: 0; box-shadow: none; flex-shrink: 0; }
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] { gap: 0; }
/* with the composer and no gate, the end of the page must clear the white fade above the composer */
main.stMain:has([data-testid="stBottom"]):not(:has(.st-key-gate)) [data-testid="stMainBlockContainer"] {
  padding-bottom: 110px !important; }
/* the content column: 1120 px, 40 px gutters (the top bar spans the pane) */
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] > * {
  width: 100% !important; max-width: var(--tx-col); margin-left: auto; margin-right: auto; padding-left: 40px;
  padding-right: 40px; box-sizing: border-box; }
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] > [data-testid="stLayoutWrapper"]:has(> .st-key-topbar) {
  max-width: none; padding: 0; position: sticky; top: 0; z-index: 40; }

/* ---------------------------------------------------------------- type helpers */
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { font-size: 12.5px; line-height: 1.5;
  color: var(--tx-ink-3); opacity: 1; }
[data-testid="stMarkdownContainer"] a { color: var(--tx-indigo-ink); }
[data-testid="stMarkdownContainer"] code { font-family: var(--tx-mono); font-size: .87em; color: var(--tx-ink-2);
  background: var(--tx-code-bg); border-radius: 6px; padding: .08em .36em; }
/* our HTML blocks have no paragraph margin for Streamlit's -1rem pull-up to cancel */
[data-testid="stMarkdownContainer"]:has(> .tx-blk), [data-testid="stMarkdownContainer"]:has(> .tx-side),
[data-testid="stMarkdownContainer"]:has(> .tx-crumb), [data-testid="stMarkdownContainer"]:has(> .tx-strip),
[data-testid="stMarkdownContainer"]:has(> .tx-hero), [data-testid="stMarkdownContainer"]:has(> .tx-sg),
[data-testid="stMarkdownContainer"]:has(> .tx-foot), [data-testid="stMarkdownContainer"]:has(> .tx-presenter),
[data-testid="stMarkdownContainer"]:has(> .tx-menu-h), [data-testid="stMarkdownContainer"]:has(> .tx-agent),
[data-testid="stMarkdownContainer"]:has(> .tx-recap-row), [data-testid="stMarkdownContainer"]:has(> .tx-pop-t) {
  margin-bottom: 0 !important; }
.tx-blk { min-width: 0; }

/* ---------------------------------------------------------------- buttons */
[data-testid="stApp"] button[kind], [data-testid="stPopoverBody"] button[kind], [role="dialog"] button[kind] {
  font-family: inherit; }
button[data-testid^="stBaseButton-primary"], button[data-testid^="stBaseButton-secondary"],
button[data-testid^="stBaseButton-tertiary"], [data-testid="stPopoverButton"], [data-testid="stDownloadButton"] button {
  min-height: 36px; height: 36px; padding: 0 16px; border-radius: 10px; box-shadow: none; gap: 7px;
}
button[data-testid^="stBaseButton-"] p, [data-testid="stPopoverButton"] p, [data-testid="stDownloadButton"] button p {
  font-size: 13.5px; font-weight: 500; letter-spacing: -.005em; white-space: nowrap; color: inherit !important; }
button[data-testid^="stBaseButton-"] [data-testid="stIconMaterial"], [data-testid="stPopoverButton"] [data-testid="stIconMaterial"] {
  font-size: 18px; }
button[data-testid^="stBaseButton-primary"], [data-testid="stPopover"] button[kind="primary"],
[data-testid="stDownloadButton"] button[kind="primary"] {
  background: var(--tx-indigo); border: 1px solid var(--tx-indigo); color: #fff;
  box-shadow: var(--tx-btn-shadow); }
button[data-testid^="stBaseButton-primary"]:hover, [data-testid="stPopover"] button[kind="primary"]:hover,
[data-testid="stDownloadButton"] button[kind="primary"]:hover { background: var(--tx-btn-hover); border-color: var(--tx-btn-hover); color: #fff; }
button[data-testid^="stBaseButton-secondary"], [data-testid="stPopover"] button[kind="secondary"],
[data-testid="stDownloadButton"] button[kind="secondary"] {
  background: transparent; border: 1px solid var(--tx-ghost-border); color: var(--tx-ink); }
button[data-testid^="stBaseButton-secondary"]:hover, [data-testid="stDownloadButton"] button[kind="secondary"]:hover {
  background: var(--tx-ghost-hover); border-color: var(--tx-ghost-hover-border); color: var(--tx-ink); }
button[data-testid^="stBaseButton-secondary"]:focus:not(:active) { color: var(--tx-ink); border-color: var(--tx-ghost-border); }
button:focus-visible, [role="tab"]:focus-visible, [role="radio"]:focus-visible { outline: 2px solid rgba(91,95,199,.45) !important;
  outline-offset: 2px; box-shadow: none !important; }
button:disabled, button[disabled] { cursor: not-allowed; }
button[data-testid^="stBaseButton-primary"]:disabled, [data-testid="stPopover"] button[kind="primary"]:disabled {
  background: var(--tx-indigo); border-color: var(--tx-indigo); color: #fff; opacity: .45; box-shadow: none; }
button[data-testid^="stBaseButton-secondary"]:disabled, [data-testid="stPopover"] button[kind="secondary"]:disabled {
  background: transparent; color: var(--tx-ink-3); opacity: .6; }
[data-testid="stPopoverButton"] > div > div:nth-child(2) { display: none; }  /* no dropdown chevron */

/* ---------------------------------------------------------------- top bar (sticky, inside the pane) */
.st-key-topbar { padding: 0 22px; gap: 0; border-bottom: 1px solid var(--tx-hair-2);
  /* an opaque copy of the pane's colour, so nothing scrolls visibly underneath */
  background-color: var(--tx-canvas);
  background-image: linear-gradient(var(--tx-pane), var(--tx-pane)), linear-gradient(var(--tx-glass), var(--tx-glass)), var(--tx-mesh);
  background-attachment: fixed; }
.st-key-tb-row { display: grid !important; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center;
  height: 60px !important; min-height: 60px; column-gap: 16px; }
.st-key-tb-row > [data-testid="stLayoutWrapper"] { width: auto !important; min-width: 0; }
.st-key-tb-left { justify-content: center; min-width: 0; }
.st-key-tb-mid { justify-content: center; width: auto !important; }
.st-key-tb-right { flex-wrap: nowrap; width: auto !important; justify-content: flex-end; gap: 6px !important; }
[data-testid="stAppViewContainer"]:has(section[data-testid="stSidebar"][data-collapsed="true"]) .st-key-tb-left { padding-left: 42px; }
.tx-crumb { font-size: 13.5px; color: var(--tx-ink-2); display: flex; gap: 8px; align-items: center; white-space: nowrap;
  min-width: 0; line-height: 1.55; }
.tx-crumb b { color: var(--tx-ink); font-weight: 500; }
.tx-crumb .tx-run { overflow: hidden; text-overflow: ellipsis; }
.tx-chip-rec { flex: none; font-size: 11.5px; color: var(--tx-ink-2); background: var(--tx-hair-2); padding: 2px 8px;
  border-radius: 999px; line-height: 1.5; }
/* segmented control: Local · AgentCore */
.st-key-topbar [data-testid="stButtonGroup"] { width: auto; }
.st-key-topbar [data-testid="stButtonGroup"] [role="radiogroup"] { background: var(--tx-track); border-radius: 999px;
  padding: 3px; gap: 0; border: none; box-shadow: none; flex-wrap: nowrap; }
.st-key-topbar [data-testid="stButtonGroup"] button { border: none !important; border-radius: 999px !important;
  background: transparent !important; color: var(--tx-ink-2); min-height: 34px !important; height: 34px;
  padding: 0 16px !important; box-shadow: none; margin: 0 !important; outline: none !important;
  display: inline-flex; align-items: center; justify-content: center; }
/* icon and label centred as one unit, with equal space on both sides of the segment */
.st-key-topbar [data-testid="stButtonGroup"] button > div,
.st-key-topbar [data-testid="stButtonGroup"] button > div > span { display: inline-flex; align-items: center;
  justify-content: center; gap: 7px; margin: 0 !important; padding: 0 !important; }
.st-key-topbar [data-testid="stButtonGroup"] button span:has(> [data-testid="stIconMaterial"]),
.st-key-topbar [data-testid="stButtonGroup"] button > div > span > span { margin: 0 !important; display: inline-flex;
  align-items: center; }
/* the selected segment is already obvious, so no focus ring around it (it drew a second, offset border) */
.st-key-topbar [data-testid="stButtonGroup"] button:focus,
.st-key-topbar [data-testid="stButtonGroup"] button:focus-visible { outline: none !important; }
.st-key-topbar [data-testid="stButtonGroup"] button:focus-visible:not([aria-checked="true"]) {
  box-shadow: inset 0 0 0 1.5px rgba(91,95,199,.35) !important; }
.st-key-topbar [data-testid="stButtonGroup"] button p { font-size: 13px; font-weight: 500; color: inherit;
  margin: 0 !important; line-height: 1 !important; }
.st-key-topbar [data-testid="stButtonGroup"] button [data-testid="stIconMaterial"] { font-size: 17px; color: inherit;
  width: 17px; height: 17px; }
.st-key-topbar [data-testid="stButtonGroup"] button:hover { color: var(--tx-ink); }
.st-key-topbar [data-testid="stButtonGroup"] button[aria-checked="true"] { background: var(--tx-seg-on) !important;
  color: var(--tx-seg-on-ink); box-shadow: var(--tx-seg-on-shadow); }
.st-key-topbar [data-testid="stButtonGroup"] button:disabled { opacity: .55; }
/* the "…" menu (ghost) and New Run (a small indigo square) */
.st-key-tb-right [data-testid="stPopoverButton"] { width: 34px; height: 34px; min-height: 34px; padding: 0;
  justify-content: center; }
.st-key-more_menu [data-testid="stPopoverButton"] { background: transparent !important; border: none !important;
  color: var(--tx-ink-2); box-shadow: none !important; }
.st-key-more_menu [data-testid="stPopoverButton"]:hover { background: var(--tx-hair-2) !important; color: var(--tx-ink); }
.st-key-tb-right [data-testid="stPopoverButton"] [data-testid="stIconMaterial"] { font-size: 20px !important;
  width: 20px !important; height: 20px !important; line-height: 20px !important; margin: 0 !important;
  display: inline-flex !important; align-items: center; justify-content: center; }
/* icon-only buttons: the box was 15 px for a 20 px glyph and a hidden 1 px label pushed it sideways; centre it exactly */
.st-key-tb-right [data-testid="stPopoverButton"] div, .st-key-tb-right [data-testid="stPopoverButton"] span {
  gap: 0 !important; margin: 0 !important; }
.st-key-tb-right [data-testid="stPopoverButton"] span:not([data-testid]):not(:has(*)) { position: absolute !important;
  width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.st-key-tb-right [data-testid="stPopoverButton"] > div { display: flex; align-items: center; justify-content: center;
  width: 100%; height: 100%; }
.st-key-new_run_menu [data-testid="stPopoverButton"] [data-testid="stMarkdownContainer"] {
  position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.st-key-new_run_menu [data-testid="stPopoverButton"] > div, .st-key-more_menu [data-testid="stPopoverButton"] > div {
  justify-content: center; }
/* the theme switch: a ghost icon button left of "…", its icon exactly centred and its label only for screen readers */
.st-key-theme_toggle button { width: 34px; height: 34px; min-height: 34px; padding: 0 !important; border: none !important;
  background: transparent !important; color: var(--tx-ink-2); box-shadow: none !important; display: flex;
  align-items: center; justify-content: center; gap: 0 !important; position: relative; }
.st-key-theme_toggle button:hover { background: var(--tx-hair-2) !important; color: var(--tx-ink); }
.st-key-theme_toggle button div, .st-key-theme_toggle button span { gap: 0 !important; margin: 0 !important;
  padding: 0 !important; }
.st-key-theme_toggle button > div { display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
.st-key-theme_toggle button [data-testid="stMarkdownContainer"] { position: absolute !important; width: 1px; height: 1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.st-key-theme_toggle button [data-testid="stIconMaterial"] { font-size: 20px !important; width: 20px !important;
  height: 20px !important; line-height: 20px !important; display: inline-flex !important; align-items: center;
  justify-content: center; color: inherit; }
[data-testid="stPopoverBody"] { border-radius: 16px; border: 1px solid var(--tx-hair); background: var(--tx-card);
  box-shadow: var(--tx-pop-shadow); padding: 16px 18px 18px;
  min-width: 340px; width: 420px !important; max-width: 420px !important; }
[data-testid="stPopoverBody"] [data-testid="stVerticalBlock"] { gap: 10px; }
[data-testid="stPopoverBody"] [data-testid="stMarkdownContainer"] p { font-size: 13.5px; }
.tx-menu-h { font-size: 12px; font-weight: 500; color: var(--tx-ink-3); margin: 8px 0 -2px; }
.tx-pop-t { font-size: 14px; font-weight: 600; color: var(--tx-ink); letter-spacing: -.01em; }

/* presenter progress strip, styled like the segmented control */
.tx-strip { display: flex; justify-content: center; padding: 0 0 12px; }
.tx-strip-track { display: inline-flex; align-items: center; gap: 2px; background: var(--tx-track);
  border-radius: 999px; padding: 3px; }
.tx-strip .tx-s { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px;
  font-size: 13px; font-weight: 500; color: var(--tx-ink-3); white-space: nowrap; line-height: 1.5; }
.tx-strip .tx-s .tx-ms { font-size: 15px; }
.tx-strip .tx-approved { color: var(--tx-ink-2); }
.tx-strip .tx-approved .tx-ms { color: var(--tx-ok); }
.tx-strip .tx-current { background: var(--tx-seg-on); color: var(--tx-seg-on-ink); box-shadow: var(--tx-seg-on-shadow); }
.tx-strip .tx-failed { color: var(--tx-bad); }
.tx-strip .tx-c { width: 6px; height: 6px; border-radius: 50%; background: var(--tx-dot-idle); }
.tx-strip .tx-current .tx-c { background: var(--tx-indigo); }
.tx-strip .tx-failed .tx-c { background: var(--tx-bad); }

/* ---------------------------------------------------------------- the nav */
.tx-side { display: flex; flex-direction: column; flex: 1 1 auto; gap: 2px; min-height: 0; }
.tx-brand { display: flex; align-items: center; gap: 10px; padding: 4px 10px 22px; font-weight: 600; font-size: 14px;
  letter-spacing: -.01em; color: var(--tx-ink); line-height: 1.55; }
.tx-brand-mark { width: 26px; height: 26px; border-radius: 8px; background: linear-gradient(135deg, #7c7ff0, #5b5fc7);
  display: grid; place-items: center; color: #fff; flex: none; }
.tx-brand-mark .tx-ms { font-size: 16px; font-weight: 400 !important; font-variation-settings: "wght" 400, "opsz" 20; }
.tx-nav-label { font-size: 12px; color: var(--tx-ink-3); padding: 0 10px 8px; line-height: 1.55; }
.tx-nav { position: relative; display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 10px; align-items: center;
  padding: 9px 10px; border-radius: 12px; color: var(--tx-ink) !important; text-decoration: none !important;
  line-height: 1.55; transition: background .15s ease; }
a.tx-nav:hover { background: var(--tx-nav-hover); }
.tx-nav .tx-ms { color: var(--tx-ink-2); display: flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; margin: 0; }
.tx-nav-name { display: block; font-weight: 500; font-size: 14px; letter-spacing: -.01em; }
.tx-nav-meta { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--tx-ink-3); }
.tx-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--tx-dot-idle); display: inline-block; }
.tx-approved .tx-dot { background: var(--tx-dot-ok); }
.tx-awaiting_approval .tx-dot { background: var(--tx-dot-warn); }
.tx-running .tx-dot { background: var(--tx-indigo); animation: tx-pulse 1.6s ease-in-out infinite; }
.tx-failed .tx-dot { background: var(--tx-bad); }
@keyframes tx-pulse { 50% { opacity: .3; } }
.tx-nav.tx-active { background: var(--tx-nav-on); box-shadow: var(--tx-nav-on-shadow); }
.tx-nav.tx-active::before { content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
  border-radius: 3px; background: var(--tx-nav-bar); }
.tx-nav.tx-active .tx-ms { color: var(--tx-accent); }
.tx-nav-foot { margin-top: auto; padding: 12px 10px 2px; font-size: 12px; color: var(--tx-ink-3); line-height: 1.5; }
.tx-presenter { display: flex; align-items: center; gap: 10px; padding: 14px 10px 0; font-size: 13px; font-weight: 500;
  color: var(--tx-ink); }
.tx-initials { flex: none; width: 28px; height: 28px; border-radius: 50%; display: grid; place-items: center;
  background: var(--tx-indigo-wash); color: var(--tx-indigo-ink); font-size: 11.5px; font-weight: 600; }

/* ---------------------------------------------------------------- welcome */
.st-key-welcome { max-width: 800px; margin: 0 auto; padding-top: calc(9vh + 27px); gap: 0; }
.tx-hero { text-align: center; }
.tx-hello { font-size: 44px; line-height: 1.1; font-weight: 600; letter-spacing: -.035em; color: var(--tx-ink);
  margin: 0 0 12px; text-wrap: balance; }
.tx-hello-sub { font-size: 16px; line-height: 1.55; color: var(--tx-ink-2); margin: 0 0 39px; }
.st-key-welcome-input { background: var(--tx-card); border: 1px solid var(--tx-hair-2); border-radius: 22px;
  box-shadow: var(--tx-ask-shadow); padding: 14px 14px 14px 20px;
  gap: 14px !important; align-items: center; overflow: visible !important; }
.st-key-welcome-input:focus-within { border-color: rgba(91,95,199,.28); }
.st-key-welcome-input > div:has([data-testid="stFileUploader"]) { flex: 1 1 auto !important; min-width: 0; width: auto !important; }
.st-key-welcome-input > div:has(.stButton) { flex: 0 0 auto !important; width: auto !important; }
.st-key-welcome-input [data-testid="stFileUploader"] section,
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] { background: transparent; border: none; padding: 0;
  min-height: 36px; border-radius: 0; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] { position: relative; justify-content: flex-start;
  align-items: center; display: flex; gap: 14px; }
.st-key-welcome-input [data-testid="stFileUploaderDropzoneInstructions"] { display: none; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"]::after {
  content: "Drop the business requirements here, or pick a suggestion below";
  order: 2; color: var(--tx-ink-3); font-size: 15.5px; line-height: 1.4; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; min-width: 0; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button {
  order: 1; width: 36px; height: 36px; min-height: 36px; padding: 0; border-radius: 50%; border: 1px solid var(--tx-hair);
  background: transparent; color: transparent; overflow: hidden; position: relative; flex: none; margin: 0; box-shadow: none; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button > * { visibility: hidden; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button::before {
  content: "add"; font-family: "TX Symbols", "Material Symbols Rounded"; font-feature-settings: "liga";
  font-variation-settings: "wght" 300, "opsz" 20; font-weight: 300; font-size: 20px; color: var(--tx-ink-2);
  position: absolute; inset: 0; display: grid; place-items: center; visibility: visible; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button:hover { background: var(--tx-hair-2); }
.st-key-welcome-input [data-testid="stFileUploaderFile"] { padding: 4px 0 0; }
.st-key-welcome-input [data-testid="stFileUploaderFileName"] { font-size: 13px; }
.st-key-welcome-input .stButton button [data-testid="stIconMaterial"] { font-size: 18px; }
.st-key-welcome [data-testid="stExpander"] { margin-top: 12px; }
.st-key-suggestions { display: grid !important; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px !important;
  margin-top: 19px; align-items: stretch; }
.st-key-suggestions > [data-testid="stLayoutWrapper"] { width: auto !important; min-width: 0; }
.st-key-suggestions [class*="st-key-sg-"] {
  position: relative; height: 100%; min-height: 132px; background: var(--tx-sg-bg); border: 1px solid var(--tx-hair-2);
  border-radius: 18px; box-shadow: var(--tx-soft-shadow); padding: 18px 20px 20px; gap: 0;
  transition: border-color .15s ease, box-shadow .15s ease; }
.st-key-suggestions [class*="st-key-sg-"]:hover { border-color: rgba(91,95,199,.22);
  box-shadow: var(--tx-lift-shadow); }
.st-key-suggestions [class*="st-key-sg-"]:has(button:focus-visible) { outline: 2px solid rgba(91,95,199,.45); outline-offset: 2px; }
.st-key-suggestions [class*="st-key-sg-"] [data-testid="stElementContainer"]:has(button) {
  position: absolute; inset: 0; margin: 0; z-index: 2; width: auto !important; height: auto; }
.st-key-suggestions [class*="st-key-sg-"] [data-testid="stElementContainer"]:has(button) > div,
.st-key-suggestions [class*="st-key-sg-"] [data-testid="stElementContainer"]:has(button) [data-testid="stButton"] { width: 100%; height: 100%; }
.st-key-suggestions [class*="st-key-sg-"] button { width: 100%; height: 100% !important; opacity: 0; cursor: pointer; }
.tx-sg > .tx-ms { color: var(--tx-accent); font-size: 22px; }
.tx-sg-text { margin: 14px 0 6px; font-size: 14.5px; font-weight: 500; letter-spacing: -.01em; line-height: 1.4; color: var(--tx-ink); }
.tx-sg-text em, .tx-sg-text b { font-style: normal; font-weight: 500; color: var(--tx-indigo-ink); }
.tx-sg-cap { font-size: 12.5px; color: var(--tx-ink-3); line-height: 23.25px; }
.tx-foot { margin-top: 34px; font-size: 12.5px; color: var(--tx-ink-3); text-align: center; line-height: 1.55; }

/* dialogs: the six agents and the legacy estate */
.tx-agent { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 14px; align-items: start; padding: 14px 0;
  border-bottom: 1px solid var(--tx-hair-2); }
.tx-agent:last-child { border-bottom: none; }
.tx-agent-title { font-weight: 600; font-size: 14px; color: var(--tx-ink); letter-spacing: -.01em; line-height: 1.45; }
.tx-agent-does { font-size: 13.5px; color: var(--tx-ink-2); line-height: 1.5; margin-top: 2px; }
.tx-agent-an { font-size: 12.5px; color: var(--tx-ink-3); margin-top: 3px; }
.tx-legacy-h { font-size: 12.5px; font-weight: 500; color: var(--tx-ink-3); margin: 16px 0 8px; }
.tx-legacy-smells { margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; }
.tx-legacy-smells li { font-size: 13.5px; line-height: 1.45; color: var(--tx-ink); margin: 0; display: flex;
  align-items: baseline; gap: 9px; }
.tx-legacy-smells li::before { content: ""; flex: none; width: 6px; height: 6px; border-radius: 50%;
  background: var(--tx-dot-warn); transform: translateY(-2px); }
@media (max-width: 640px) { .tx-legacy-smells { grid-template-columns: 1fr; } }
.tx-legacy-foot { font-size: 12.5px; color: var(--tx-ink-3); margin-top: 14px; line-height: 1.5; }

/* ---------------------------------------------------------------- transcript */
.st-key-transcript { margin-top: 28px; gap: 22px; }
.st-key-transcript:not(:has(*)) { margin-top: 0; }
[data-testid="stChatMessage"] { background: transparent !important; padding: 0; gap: 12px; margin: 0; }
[data-testid="stChatMessageContent"] { padding: 0; min-width: 0; }
[data-testid="stChatMessage"] [data-testid^="stChatMessageAvatar"] { width: 28px; height: 28px; border-radius: 9px;
  background: var(--tx-indigo-wash); color: var(--tx-accent); border: none; margin-top: 2px; }
[data-testid="stChatMessage"] [data-testid^="stChatMessageAvatar"] [data-testid="stIconMaterial"] { font-size: 16px; }
[data-testid="stChatMessage"]:has(> [aria-label="Chat message from assistant"]):not(:has([class*="st-key-card-"], [class*="st-key-live-"])) [data-testid="stMarkdownContainer"] p {
  font-size: 14px; color: var(--tx-ink-2); }
[data-testid="stChatMessage"]:has(> [aria-label="Chat message from user"]) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has(> [aria-label="Chat message from user"]) [data-testid^="stChatMessageAvatar"] { display: none; }
[data-testid="stChatMessage"] > [aria-label="Chat message from user"] {
  flex: 0 1 auto; max-width: 72%; margin-left: auto !important; margin-right: 0 !important;
  background: var(--tx-indigo-wash); border-radius: 14px 14px 4px 14px; padding: 8px 14px; }
[data-testid="stChatMessage"] > [aria-label="Chat message from user"] p { margin: 0; font-size: 14px; color: var(--tx-ink);
  line-height: 1.55; }
[data-testid="stChatMessage"] > [aria-label="Chat message from user"] [data-testid="stMarkdownContainer"] {
  margin-bottom: 0 !important; }
[data-testid="stChatMessage"] > [aria-label="Chat message from user"] code { font-size: 12.5px; color: var(--tx-indigo-ink);
  background: none; padding: 0; }
[class*="st-key-stage-"] { scroll-margin-top: var(--tx-top-offset); }
/* stage cards carry their own stage symbol: no avatar column, so the card gets the full width */
[class*="st-key-stage-"] [data-testid="stChatMessage"] > [data-testid^="stChatMessageAvatar"] { display: none; }
[class*="st-key-stage-"] [data-testid="stChatMessageContent"] > [data-testid="stVerticalBlock"] { gap: 10px; }
.st-key-wrapup, .st-key-gate { scroll-margin-top: var(--tx-top-offset); }
[data-testid="stElementContainer"]:has(.tx-scroll) { display: none; }

/* ---------------------------------------------------------------- the gate: a floating bar over a white fade */
[data-testid="stLayoutWrapper"]:has(> .st-key-gate) {  /* the wrapper is the sticky box (it is as tall as the gate) */
  position: sticky; bottom: var(--tx-chat-h); z-index: 100; margin-top: 34px;
}
.st-key-gate { background: var(--tx-float); -webkit-backdrop-filter: blur(20px); backdrop-filter: blur(20px);
  border: 1px solid var(--tx-float-border); border-radius: 16px; box-shadow: var(--tx-float-shadow);
  padding: 10px 10px 10px 20px; gap: 10px; }
.st-key-gate [data-testid="stElementContainer"]:not(:has(button)) [data-testid="stMarkdownContainer"] p { margin: 0;
  font-size: 14px; line-height: 1.45; color: var(--tx-ink-2); text-wrap: balance; }
.st-key-gate [data-testid="stElementContainer"]:not(:has(button)) [data-testid="stMarkdownContainer"] strong {
  color: var(--tx-ink); font-weight: 500; }
.st-key-gate [data-testid="stAlert"] { margin: 0; }
.st-key-gate-row { flex-wrap: wrap; align-items: center; column-gap: 14px; row-gap: 8px; }
.st-key-gate-row > [data-testid="stElementContainer"] { flex: 1 1 240px; min-width: 0; width: auto !important; }
.st-key-gate-row > [data-testid="stLayoutWrapper"] { flex: 0 0 auto; width: auto !important; margin-left: auto; }
.st-key-gate-row [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
.st-key-gate [data-testid="stHorizontalBlock"] { gap: 14px !important; justify-content: flex-end; }
.st-key-gate[data-stale="true"], [data-stale="true"] .st-key-gate,
[data-testid="stElementContainer"][data-stale="true"]:has(.st-key-gate) { display: none; }
.st-key-gate-step { margin-top: 28px; background: var(--tx-card); border: 1px solid var(--tx-hair-2); border-radius: 18px;
  box-shadow: var(--tx-soft-shadow); padding: 20px 24px; gap: 12px; }
.st-key-gate-step [data-testid="stElementContainer"]:not(:has(button)) [data-testid="stMarkdownContainer"] p {
  font-size: 14px; color: var(--tx-ink-2); }
.st-key-gate-step [data-testid="stMarkdownContainer"] strong { color: var(--tx-ink); font-weight: 500; }
.st-key-wrapup { margin-top: 22px; }

/* the composer at the bottom of the pane, and the fade behind the gate */
[data-testid="stBottom"] > div { background: transparent !important; }
[data-testid="stBottom"]::before { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 170px;
  background: linear-gradient(to bottom, var(--tx-fade-0), var(--tx-fade-1) 45%, var(--tx-fade-2));
  pointer-events: none;
  border-radius: 0 0 22px 22px; }
main.stMain:not(:has(.st-key-gate)) [data-testid="stBottom"]::before { height: 100px; }  /* no gate: a shorter fade */
[data-testid="stBottomBlockContainer"] { max-width: var(--tx-col) !important; width: 100%; padding: 0 40px 12px !important;
  margin: 0 auto; background: none !important; }
[data-testid="stChatInput"] { background: var(--tx-float) !important; border: 1px solid var(--tx-float-border) !important;
  border-radius: 14px !important; box-shadow: var(--tx-composer-shadow); min-height: 48px; }
[data-testid="stChatInput"] > div { border: none !important; background: transparent !important; border-radius: 14px;
  min-height: 46px; padding: 0 7px 0 18px; align-items: center; }
[data-testid="stChatInput"]:focus-within { border-color: rgba(91,95,199,.35) !important; }
[data-testid="stChatInputTextArea"] { font-size: 14px !important; color: var(--tx-ink) !important; line-height: 1.5 !important;
  padding: 12px 0 !important; min-height: 46px !important; background: transparent !important; }
[data-testid="stChatInputTextArea"]::placeholder { color: var(--tx-ink-3); opacity: 1; }
[data-testid="stChatInputSubmitButton"] { width: 32px; height: 32px; min-width: 32px; border-radius: 9px !important;
  background: var(--tx-send-bg) !important; color: var(--tx-ink-2) !important; border: none; }
[data-testid="stChatInputSubmitButton"]:not(:disabled):hover { background: var(--tx-indigo) !important; color: #fff !important; }
[data-testid="stChatInputSubmitButton"] svg { width: 16px; height: 16px; }

/* ---------------------------------------------------------------- cards (views.py) */
[class*="st-key-card-"], [class*="st-key-live-"], .st-key-finale, .st-key-recap {
  background: var(--tx-card); border: 1px solid var(--tx-hair-2); border-radius: 18px;
  box-shadow: var(--tx-card-shadow); padding: 26px 30px 24px; gap: 0; }
[class*="st-key-card-"] > [data-testid="stElementContainer"] [data-testid="stAlert"],
[class*="st-key-live-"] > [data-testid="stElementContainer"] [data-testid="stAlert"] { margin: 4px 0 16px; }
.tx-head { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; min-width: 0; }
.tx-tile { flex: none; width: 32px; height: 32px; border-radius: 10px; background: var(--tx-tile-bg);
  color: var(--tx-accent); display: grid; place-items: center; }
.tx-tile .tx-ms { font-size: 20px; }
.tx-tile.tx-lg { width: 44px; height: 44px; border-radius: 13px; }
.tx-tile.tx-lg .tx-ms { font-size: 24px; }
.tx-head-t { flex: 1 1 auto; min-width: 0; }
.tx-step { font-size: 13px; color: var(--tx-ink-3); line-height: 1.55; }
.tx-title { font-size: 15px; font-weight: 600; letter-spacing: -.01em; color: var(--tx-ink); line-height: 1.55; }
.tx-title .tx-elapsed { color: var(--tx-ink-3); font-weight: 400; font-variant-numeric: tabular-nums; }
.tx-state { flex: none; margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 12.5px;
  color: var(--tx-ink-3); white-space: nowrap; font-variant-numeric: tabular-nums; }
.tx-state .tx-ms { font-size: 16px; font-weight: 400 !important; font-variation-settings: "wght" 400, "opsz" 20; }
.tx-state.tx-done .tx-ms { color: var(--tx-ok); }
.tx-state.tx-warning .tx-ms { color: var(--tx-warn); }
.tx-state.tx-failed .tx-ms { color: var(--tx-bad); }
.tx-state.tx-running .tx-ms { color: var(--tx-accent); }
.tx-state .tx-sep { color: var(--tx-ink-3); opacity: .6; margin: 0 2px; }
.tx-h { font-size: 22px; line-height: 1.3; font-weight: 600; letter-spacing: -.02em; color: var(--tx-ink); margin: 0 0 6px; }
.tx-lede { color: var(--tx-ink-2); font-size: 14.5px; line-height: 1.55; margin: 0; max-width: 78ch; }
.tx-revised { display: flex; align-items: center; gap: 6px; margin-top: 4px; font-size: 12.5px; color: var(--tx-ink-3); }
.tx-revised .tx-ms { font-size: 16px; color: var(--tx-accent); }
.tx-lead { margin-bottom: 22px; }
/* KPIs: one row, hairline dividers, thin tabular numbers (GUIDELINES.md "Numbers") */
.tx-kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); padding: 4px 0 20px;
  border-bottom: 1px solid var(--tx-hair-2); }
.tx-kpis + .tx-kpis { padding-top: 20px; }
.tx-kpi { padding: 0 24px; border-left: 1px solid var(--tx-hair); min-width: 0; }
.tx-kpi:first-child { padding-left: 0; border-left: 0; }
.tx-kpi-l { font-size: 12.5px; color: var(--tx-ink-3); font-weight: 500; line-height: 23.25px; }
.tx-kpi-v { font-size: 34px; font-weight: 300; letter-spacing: -.02em; line-height: 1.15; margin: 4px 0 2px;
  font-variant-numeric: tabular-nums lining-nums; color: var(--tx-ink); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.tx-kpi-v small { font-size: 16px; color: var(--tx-ink-3); font-weight: 400; letter-spacing: 0; }
.tx-kpi-v .tx-cur { font-size: .7em; font-weight: 300; letter-spacing: 0; margin-right: .03em; }
.tx-kpi-v .tx-arr { font-size: .5em; color: var(--tx-ink-3); font-weight: 400; margin: 0 .3em; vertical-align: .32em; }
.tx-kpi-s { font-size: 12.5px; color: var(--tx-ink-2); line-height: 1.55; }
.tx-kpi-s.tx-bad { color: var(--tx-bad); } .tx-kpi-s.tx-good { color: var(--tx-ok); } .tx-kpi-s.tx-warn { color: var(--tx-warn); }
.tx-ev2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); margin: 18px 0 6px; }
.tx-ev { display: flex; gap: 12px; align-items: baseline; min-width: 0; }
.tx-ev + .tx-ev { padding-left: 24px; border-left: 1px solid var(--tx-hair); }
.tx-ev-l { font-size: 12px; color: var(--tx-ink-3); white-space: nowrap; display: flex; align-items: center; gap: 5px; }
.tx-ev-l .tx-ms { font-size: 15px; }
.tx-ev-model .tx-ev-l { color: var(--tx-indigo-ink); }
.tx-ev-v { font-size: 14px; font-weight: 500; color: var(--tx-ink); line-height: 1.45; min-width: 0; font-variant-numeric: tabular-nums; }
.tx-telemetry { font-size: 12px; color: var(--tx-ink-3); margin: 14px 0 0; font-variant-numeric: tabular-nums; }
/* tabs */
[data-testid="stTabs"] { margin-top: 22px; }
.st-key-finale [data-testid="stTabs"] { margin-top: 0; }
[data-testid="stTabs"] [role="tablist"] { gap: 22px; border-bottom: 1px solid var(--tx-hair-2); box-shadow: none; }
[data-testid="stTabs"] [role="tab"] { padding: 10px 0 !important; min-height: 0; height: auto; background: transparent !important;
  margin: 0; }
[data-testid="stTabs"] [role="tab"] p { font-size: 13.5px; font-weight: 500; color: var(--tx-ink-2); }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] p { color: var(--tx-ink); }
[data-testid="stTabs"] [role="tab"]:hover p { color: var(--tx-ink); }
[data-testid="stTabs"] .react-aria-SelectionIndicator { background: var(--tx-indigo) !important; height: 2px !important;
  border-radius: 2px; }
[data-testid="stTabPanel"] { padding-top: 6px !important; }
[data-testid="stTabPanel"] > [data-testid="stVerticalBlock"] { gap: 14px; }
/* section titles and key-value facts inside the tabs */
.tx-sub { font-size: 13.5px; font-weight: 600; color: var(--tx-ink); letter-spacing: -.005em; margin: 10px 0 0;
  line-height: 1.45; }
.tx-sub span { color: var(--tx-ink-3); font-weight: 400; }
.tx-kv { display: grid; gap: 12px 28px; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
.tx-kv > div { min-width: 0; }
.tx-kv-l { font-size: 12px; color: var(--tx-ink-3); line-height: 1.5; }
.tx-kv-v { font-size: 13.5px; font-weight: 500; color: var(--tx-ink); line-height: 1.45; overflow-wrap: anywhere;
  font-variant-numeric: tabular-nums; }
.tx-text { font-size: 13.5px; color: var(--tx-ink-2); line-height: 1.6; }
.tx-summary { display: flex; gap: 10px; align-items: flex-start; font-size: 13.5px; line-height: 1.6;
  color: var(--tx-ink-2); padding-top: 4px; }
.tx-summary .tx-ms { font-size: 17px; color: var(--tx-accent); margin-top: 2px; }
.tx-summary b { color: var(--tx-ink); font-weight: 500; }
.tx-note { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; line-height: 1.5; color: var(--tx-ink-3); }
.tx-note .tx-ms { font-size: 16px; margin-top: 1px; }
/* tables: 12 px grey headers, hairline rows, no outer box, no vertical lines */
.tx-tbl-wrap { overflow-x: auto; }
.tx-tbl-wrap.tx-tscroll { overflow-y: auto; }
table.tx-tbl { width: 100%; border-collapse: collapse; border: none; font-size: 13.5px; line-height: 1.5;
  font-variant-numeric: tabular-nums; background: transparent; margin: 0; color: var(--tx-ink); }
table.tx-tbl th { text-align: left; font-weight: 500; font-size: 12px; color: var(--tx-ink-3); padding: 12px 12px 8px 0;
  border: none; border-bottom: 1px solid var(--tx-hair); background: var(--tx-card); white-space: nowrap; }
.tx-tscroll table.tx-tbl th { position: sticky; top: 0; z-index: 1; }
table.tx-tbl td { padding: 12px 12px 12px 0; border: none; border-bottom: 1px solid var(--tx-hair-2); vertical-align: top;
  color: var(--tx-ink); height: auto; font-size: 13.5px; }
table.tx-tbl tr:last-child td { border-bottom: 1px solid var(--tx-hair-2); }
table.tx-tbl thead, table.tx-tbl tbody, table.tx-tbl tr { border: none !important; background: transparent !important; }
table.tx-tbl th:last-child, table.tx-tbl td:last-child { padding-right: 0; }
table.tx-tbl td.tx-id { color: var(--tx-ink-3); white-space: nowrap; width: 64px; }
table.tx-tbl td.tx-sevcol { width: 100px; }
table.tx-tbl td.tx-code { font-family: var(--tx-mono); font-size: 12px; color: var(--tx-code-ink); line-height: 1.7; }
table.tx-tbl td.tx-nw, table.tx-tbl th.tx-nw { white-space: nowrap; }
table.tx-tbl td.tx-num, table.tx-tbl th.tx-num { white-space: nowrap; text-align: right; }
table.tx-tbl td.tx-muted { color: var(--tx-ink-2); }
.tx-sev { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.tx-sev i { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--tx-sev-m); }
.tx-sev.tx-t-c i, .tx-sev.tx-t-bad i { background: var(--tx-bad); }
.tx-sev.tx-t-h i, .tx-sev.tx-t-warn i { background: var(--tx-dot-warn); }
.tx-sev.tx-t-ok i { background: var(--tx-dot-ok); }
.tx-sev.tx-t-l i, .tx-sev.tx-t-idle i { background: var(--tx-sev-l); }
.tx-flow { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; font-size: 13px; color: var(--tx-ink-2); }
.tx-flow b { font-size: 20px; font-weight: 300; color: var(--tx-ink); font-variant-numeric: tabular-nums lining-nums;
  margin-right: 4px; letter-spacing: -.01em; }
.tx-flow .tx-arr { color: var(--tx-ink-3); }
.tx-flow .tx-good b { color: var(--tx-ok); } .tx-flow .tx-bad b { color: var(--tx-bad); }
/* the FinOps alert */
.tx-alert { display: flex; gap: 12px; align-items: flex-start; padding: 12px 16px; border-radius: 12px;
  background: var(--tx-bad-wash); margin: 0 0 6px; }
.tx-alert .tx-ms { color: var(--tx-bad); font-size: 18px; margin-top: 1px; font-weight: 400 !important;
  font-variation-settings: "wght" 400, "opsz" 20; }
.tx-alert-t { font-size: 13.5px; font-weight: 600; color: var(--tx-bad); line-height: 1.45; }
.tx-alert-x { font-size: 13px; color: var(--tx-ink-2); line-height: 1.5; margin-top: 1px; }
[class*="st-key-card-"] [data-testid="stVegaLiteChart"], [class*="st-key-card-"] [data-testid="stArrowVegaLiteChart"] {
  margin: 4px 0 18px; }
/* the bridge strip */
.tx-bridge { margin-top: 22px; padding: 14px 16px; border-radius: 14px; background: var(--tx-indigo-wash); display: grid;
  grid-template-columns: auto minmax(0, 1fr); gap: 4px 16px; font-size: 13px; color: var(--tx-ink-2); line-height: 1.55; }
.tx-bridge b { color: var(--tx-indigo-ink); font-weight: 600; font-size: 12px; line-height: 1.7; white-space: nowrap; }
/* the live card */
.tx-live-notes { display: flex; flex-direction: column; gap: 4px; margin: -6px 0 14px; }
.tx-phases, .tx-timeline { display: flex; flex-direction: column; }
.tx2-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--tx-hair-2); min-width: 0; }
.tx2-row:last-child { border-bottom: none; }
.tx2-row > .tx-ms { font-size: 18px; }
.tx-row-l { font-weight: 500; font-size: 13.5px; color: var(--tx-ink); white-space: nowrap; }
.tx-row-l.tx-quiet { color: var(--tx-ink-3); font-weight: 400; }
.tx-row-n { flex: 1 1 auto; min-width: 0; color: var(--tx-ink-3); font-size: 12.5px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.tx-row-t { flex: none; color: var(--tx-ink-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.tx2-chip { flex: none; display: inline-flex; align-items: center; gap: 3px; padding: 1px 8px 1px 6px; border-radius: 999px;
  font-size: 11.5px; font-weight: 500; line-height: 1.5; background: var(--tx-hair-2); color: var(--tx-ink-2); }
.tx2-chip .tx-ms { font-size: 13px; }
.tx2-chip.tx-model { background: var(--tx-indigo-wash); color: var(--tx-indigo-ink); }
.tx2-narration { display: flex; gap: 8px; align-items: flex-start; color: var(--tx-ink-2); font-size: 13.5px; line-height: 1.55;
  margin-top: 12px; }
.tx2-narration .tx-ms { font-size: 16px; color: var(--tx-ink-3); margin-top: 2px; }
@keyframes tx2-spin { to { transform: rotate(360deg); } }
.tx2-spin { animation: tx2-spin 1.1s linear infinite; }
/* the finale */
.tx-finale-head { display: flex; align-items: center; gap: 16px; margin-bottom: 22px; }
.tx-finale-t { font-size: 22px; line-height: 1.3; font-weight: 600; letter-spacing: -.02em; color: var(--tx-ink); }
.tx-finale-t2 { font-size: 22px; line-height: 1.3; font-weight: 600; letter-spacing: -.02em; color: var(--tx-indigo-ink); }
.st-key-finale .tx-telemetry { margin: 16px 0 18px; }
.st-key-recap h3 { font-size: 22px; font-weight: 600; margin: 0; letter-spacing: -.02em; }
.tx-recap-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.tx-pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px; font-size: 12px;
  font-weight: 500; background: var(--tx-hair-2); color: var(--tx-ink-2); }

/* disclosures (timeline, what the agent said, raw data): quiet rows, no boxes */
[data-testid="stExpander"] details { border: 1px solid var(--tx-hair); border-radius: 12px; background: var(--tx-card); }
[data-testid="stExpander"] summary { padding: 10px 14px; border-radius: 12px; min-height: 0; }
[data-testid="stExpander"] summary:hover { background: var(--tx-hover-2); }
[data-testid="stExpander"] summary p { font-size: 13.5px; font-weight: 500; color: var(--tx-ink); }
[data-testid="stExpander"] summary [data-testid="stIconMaterial"], [data-testid="stExpanderIcon"] { font-size: 18px;
  color: var(--tx-ink-2); }
[data-testid="stExpander"] summary svg { color: var(--tx-ink-3); }
[data-testid="stExpanderDetails"] { padding: 4px 14px 14px; }
[class*="st-key-process-"] { gap: 6px; }
[class*="st-key-process-"] [data-testid="stHorizontalBlock"] { gap: 16px; }
[class*="st-key-process-"] [data-testid="stExpander"] details, [class*="st-key-live-"] [data-testid="stExpander"] details {
  border: none; background: transparent; border-radius: 10px; }
[class*="st-key-process-"] [data-testid="stExpander"] summary, [class*="st-key-live-"] [data-testid="stExpander"] summary {
  padding: 8px 6px; }
[class*="st-key-process-"] [data-testid="stExpander"] summary p, [class*="st-key-live-"] [data-testid="stExpander"] summary p {
  font-size: 13px; font-weight: 500; color: var(--tx-ink-2); }
[class*="st-key-process-"] [data-testid="stExpanderDetails"], [class*="st-key-live-"] [data-testid="stExpanderDetails"] {
  padding: 0 6px 8px; }
[class*="st-key-process-"] [data-testid="stExpanderDetails"] [data-testid="stMarkdownContainer"] p { font-size: 13.5px;
  color: var(--tx-ink-2); }
[class*="st-key-live-"] [data-testid="stExpander"] { margin-top: 8px; border-top: 1px solid var(--tx-hair-2); }

/* ---------------------------------------------------------------- widgets */
[data-testid="stWidgetLabel"] p { font-size: 12.5px !important; color: var(--tx-ink-2); font-weight: 500; }
[data-baseweb="select"] > div { min-height: 36px; border-radius: 10px !important; border-color: var(--tx-hair) !important;
  background: var(--tx-card) !important; font-size: 13.5px; }
[data-baseweb="select"] [data-testid="stIconMaterial"] { color: var(--tx-ink-3); }
[data-baseweb="popover"] ul[role="listbox"], [data-baseweb="menu"] { border-radius: 12px; font-size: 13.5px; }
[data-testid="stTextArea"] textarea { font-size: 14px; line-height: 1.55; }
[data-testid="stTextArea"] [data-baseweb="textarea"] { border-radius: 12px; border-color: var(--tx-hair) !important; background: var(--tx-card); }
[data-testid="stTextArea"] [data-baseweb="textarea"]:focus-within { border-color: rgba(91,95,199,.45) !important; }
[data-testid="stCheckbox"] label p, [data-testid="stToggle"] label p, label[data-baseweb="checkbox"] p { font-size: 13.5px;
  color: var(--tx-ink); }
[data-testid="stCode"] pre, [data-testid="stCode"] { border-radius: 12px; }
[data-testid="stCode"] pre { background: var(--tx-code-block) !important; border: 1px solid var(--tx-hair-2); }
[data-testid="stCode"] code { font-size: 12px; font-family: var(--tx-mono); }
[data-testid="stAlertContainer"] { border-radius: 12px; padding: 12px 16px; border: none; }
[data-testid="stAlertContainer"] p { font-size: 13.5px; line-height: 1.5; }
[data-testid="stAlertContainer"] [data-testid="stIconMaterial"] { font-size: 18px; }
[data-testid="stJson"] { font-size: 12px; }
[data-testid="stFileUploaderDropzone"] { border-radius: 12px; border-color: var(--tx-hair); background: var(--tx-card); }
[data-testid="stFileUploaderDropzoneInstructions"] span { font-size: 13px; }
[data-testid="stFileUploaderDropzoneInstructions"] small { font-size: 12px; color: var(--tx-ink-3); }
/* st.dialog: role="dialog" sits on a transparent section element; the white box is its parent div */
[data-testid="stDialog"] :has(> [role="dialog"]) { border-radius: 20px !important; background: var(--tx-card) !important;
  border: 1px solid var(--tx-hair-2) !important; box-shadow: var(--tx-dialog-shadow) !important; }
[role="dialog"] h2, [role="dialog"] [data-testid="stHeading"] h2, [data-testid="stDialog"] h2 { font-size: 17px !important;
  font-weight: 600 !important; letter-spacing: -.015em; color: var(--tx-ink); }
[role="dialog"] h2 [data-testid="stIconMaterial"] { font-size: 20px; color: var(--tx-accent); }
[role="dialog"] h2 p { font-size: 17px !important; font-weight: 600 !important; color: var(--tx-ink) !important; }
[role="dialog"] [data-testid="stMarkdownContainer"] p { font-size: 14px; color: var(--tx-ink-2); line-height: 1.6; }
[role="dialog"] [data-testid="stMarkdownContainer"] strong { color: var(--tx-ink); font-weight: 500; }
[role="dialog"] [data-testid="stForm"] { padding: 0; border: none; }
[role="dialog"] button[aria-label="Close"] { border-radius: 9px; color: var(--tx-ink-3); }
div[data-testid="stDialog"] { background-color: var(--tx-backdrop) !important; }
[data-testid="stToast"] { border-radius: 12px; background: var(--tx-toast); border: 1px solid var(--tx-hair-2);
  box-shadow: var(--tx-float-shadow); padding: 12px 14px; }
[data-testid="stToast"] p { font-size: 13.5px; color: var(--tx-ink); }
[data-testid="stToast"] [data-testid="stIconMaterial"], [data-testid="stToast"] [data-testid*="Icon"] {
  color: var(--tx-accent); font-size: 18px; }

@media (max-width: 760px) {
  [data-testid="stAppViewContainer"] { top: 0 !important; left: 0 !important; right: 0 !important; bottom: 0 !important;
    border-radius: 0; }
  main.stMain { margin: 0 !important; height: 100%; border-radius: 0; }
  [data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] > * { padding-left: 16px; padding-right: 16px; }
  [data-testid="stBottomBlockContainer"] { padding: 0 16px 12px !important; }
  .st-key-suggestions { grid-template-columns: 1fr; }
  .tx-kpis, .tx-ev2 { grid-template-columns: 1fr !important; }
  .tx-kpi { padding: 10px 0; border-left: 0; }
  .tx-ev + .tx-ev { padding-left: 0; border-left: 0; }
}
"""

CSS = "<style>\n" + px_to_rem(_ROOT.substitute(**TOKENS, mesh=MESH) + _CSS) + "\n</style>"

# Presenter mode (?presenter=1): everything scales UNIFORMLY through the root size every rem hangs off, so the page
# stays delicate at a distance instead of getting chunky. The nav starts collapsed; the progress strip joins the top
# bar, which stays an opaque band, so nothing scrolls visibly under it.
PRESENTER_CSS = "<style>\n" + px_to_rem("""
:root { --tx-scale: 1.2; --tx-top-offset: 150px; }
""") + "\n</style>"


# The dark theme (docs/design/mockup-stage-dark.html): the same language on a night palette. Injected only when the
# theme is dark, after CSS: it swaps the tokens above and repaints the pieces Streamlit draws with its own (light) theme,
# including the ones it renders in portals (popovers, select menus, tooltips, dialogs, toasts).
DARK_MESH = ("radial-gradient(40% 55% at 8% 12%, rgba(99,102,241,.30), transparent 70%), "
             "radial-gradient(35% 50% at 92% 8%, rgba(219,39,119,.20), transparent 70%), "
             "radial-gradient(45% 60% at 85% 95%, rgba(14,165,233,.18), transparent 70%), "
             "radial-gradient(40% 50% at 15% 95%, rgba(139,92,246,.22), transparent 70%)")
DARK_TOKENS = {
    "ink": "#ececf2", "ink2": "#b3b3c2", "ink3": "#8f8fa0", "hair": "rgba(255,255,255,.10)",
    "hair2": "rgba(255,255,255,.06)", "indigo": "#5e62d0", "indigo_ink": "#a9acff", "indigo_wash": "rgba(124,128,245,.14)",
    "ok": "#5cd394", "warn": "#e8b84f", "bad": "#f2708a", "dot_ok": "#5cd394", "dot_warn": "#e8b84f", "dot_idle": "#4b4b5c",
    "glass": "rgba(22,21,32,.58)", "pane": "rgba(16,15,24,.84)", "canvas": "#0c0b13", "card": "#191823",
}
_DARK = Template("""
:root {
  color-scheme: dark;
  --tx-ink: $ink; --tx-ink-2: $ink2; --tx-ink-3: $ink3; --tx-hair: $hair; --tx-hair-2: $hair2;
  --tx-indigo: $indigo; --tx-indigo-ink: $indigo_ink; --tx-indigo-wash: $indigo_wash;
  --tx-ok: $ok; --tx-warn: $warn; --tx-bad: $bad; --tx-dot-ok: $dot_ok; --tx-dot-warn: $dot_warn; --tx-dot-idle: $dot_idle;
  --tx-glass: $glass; --tx-pane: $pane; --tx-canvas: $canvas;
  --tx-mesh: $mesh;
  --tx-card: $card; --tx-float: rgba(28,27,40,.92); --tx-sg-bg: rgba(25,24,35,.92); --tx-toast: rgba(30,29,43,.97);
  --tx-code-block: #13121b; --tx-code-bg: rgba(255,255,255,.07); --tx-code-ink: #c3c3d1;
  --tx-window-border: rgba(255,255,255,.08);
  --tx-window-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 30px 80px rgba(0,0,0,.55);
  --tx-pane-shadow: 0 1px 2px rgba(0,0,0,.3);
  --tx-card-shadow: 0 1px 2px rgba(0,0,0,.3), 0 12px 40px rgba(0,0,0,.35);
  --tx-soft-shadow: 0 1px 2px rgba(0,0,0,.3);
  --tx-lift-shadow: 0 1px 2px rgba(0,0,0,.3), 0 10px 30px rgba(0,0,0,.4);
  --tx-ask-shadow: 0 1px 2px rgba(0,0,0,.3), 0 16px 48px rgba(0,0,0,.4);
  --tx-float-border: rgba(255,255,255,.07); --tx-float-shadow: 0 10px 30px rgba(0,0,0,.45);
  --tx-composer-shadow: 0 10px 30px rgba(0,0,0,.45);
  --tx-pop-shadow: 0 16px 48px rgba(0,0,0,.55), 0 1px 2px rgba(0,0,0,.3);
  --tx-dialog-shadow: 0 30px 80px rgba(0,0,0,.6), 0 1px 2px rgba(0,0,0,.3);
  --tx-backdrop: rgba(5,4,12,.55);
  --tx-track: rgba(255,255,255,.06); --tx-seg-on: rgba(255,255,255,.12); --tx-seg-on-ink: #e6e7ff;
  --tx-seg-on-shadow: 0 1px 2px rgba(0,0,0,.4);
  --tx-nav-hover: rgba(255,255,255,.04); --tx-nav-on: rgba(255,255,255,.07); --tx-nav-on-shadow: none;
  --tx-nav-bar: #8b8ef5; --tx-accent: #a9acff; --tx-tile-bg: rgba(124,128,245,.16);
  --tx-chrome-hover: rgba(255,255,255,.08);
  --tx-btn-hover: #5558c4; --tx-btn-shadow: 0 1px 2px rgba(0,0,0,.5);
  --tx-ghost-border: rgba(255,255,255,.14); --tx-ghost-hover: rgba(255,255,255,.05);
  --tx-ghost-hover-border: rgba(255,255,255,.22);
  --tx-hover-2: rgba(255,255,255,.03); --tx-send-bg: rgba(255,255,255,.07);
  --tx-fade-0: rgba(16,15,24,0); --tx-fade-1: rgba(16,15,24,.96); --tx-fade-2: rgba(16,15,24,.99);
  --tx-sev-m: #6b6b7c; --tx-sev-l: #4b4b5c; --tx-bad-wash: rgba(242,112,138,.10);
  --tx-menu: #201f2d;
}
html, body { background: var(--tx-canvas); }
html, body, [data-testid="stApp"], [data-testid="stMarkdownContainer"], [data-testid="stPopoverBody"], [role="dialog"],
[data-testid="stToast"], [data-testid="stText"], [data-testid="stCaptionContainer"] { color: var(--tx-ink); }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color: var(--tx-ink-3); }
::selection { background: rgba(124,128,245,.38); color: #fff; }
* { scrollbar-color: rgba(255,255,255,.16) transparent; }
/* Streamlit's own widgets, drawn with its light theme: inputs, selects and their menus, toggles, uploader, tooltips */
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="textarea"] {
  background: var(--tx-card) !important; border-color: var(--tx-hair) !important; }
[data-baseweb="select"] div, [data-baseweb="select"] input, [data-baseweb="select"] span { color: var(--tx-ink) !important; }
[data-baseweb="select"] svg { color: var(--tx-ink-3); fill: currentColor; }
[data-baseweb="textarea"] textarea, [data-baseweb="input"] input, [data-baseweb="base-input"] input {
  color: var(--tx-ink) !important; -webkit-text-fill-color: var(--tx-ink); background: transparent !important;
  caret-color: var(--tx-ink); }
[data-baseweb="textarea"] textarea::placeholder, [data-baseweb="input"] input::placeholder {
  color: var(--tx-ink-3) !important; -webkit-text-fill-color: var(--tx-ink-3); }
[data-baseweb="popover"] > div, [data-baseweb="menu"], [data-baseweb="popover"] ul[role="listbox"] {
  background: var(--tx-menu) !important; color: var(--tx-ink); border-color: var(--tx-hair) !important; }
[data-baseweb="popover"] ul[role="listbox"] { border: 1px solid var(--tx-hair) !important; box-shadow: var(--tx-pop-shadow); }
[data-baseweb="popover"] li[role="option"], [data-baseweb="menu"] li { color: var(--tx-ink) !important; background: transparent; }
[data-baseweb="popover"] li[role="option"]:hover, [data-baseweb="popover"] li[role="option"][aria-selected="true"],
[data-baseweb="popover"] li[role="option"][data-highlighted="true"] { background: rgba(255,255,255,.07) !important; }
[data-baseweb="popover"] [data-testid="stPopoverBody"] { background: var(--tx-card) !important; }
[data-baseweb="tooltip"] > div, [data-baseweb="tooltip"] [data-testid="stTooltipContent"], [data-testid="stTooltipContent"] {
  background: #2a2938 !important; color: var(--tx-ink) !important; border: 1px solid var(--tx-hair); }
[data-testid="stTooltipContent"] p, [data-testid="stTooltipContent"] * { color: var(--tx-ink) !important; }
[data-testid="stTooltipIcon"] svg, [data-testid="stTooltipHoverTarget"] svg { color: var(--tx-ink-3); }
label[data-baseweb="checkbox"] > div:first-child { background-color: rgba(255,255,255,.16); }
label[data-baseweb="checkbox"] input:checked + div, label[data-baseweb="checkbox"] > div[aria-checked="true"] {
  background-color: var(--tx-indigo); }
[data-testid="stFileUploaderDropzoneInstructions"] span, [data-testid="stFileUploaderDropzoneInstructions"] div {
  color: var(--tx-ink-2) !important; }
[data-testid="stFileUploaderFile"], [data-testid="stFileUploaderFileName"] { color: var(--tx-ink) !important; }
[data-testid="stFileUploaderFile"] small { color: var(--tx-ink-3) !important; }
/* react-aria selects (Streamlit 1.6x), their dropdown portal and the switches */
[data-testid="stSelectbox"] [role="group"][data-rac] { background: var(--tx-card) !important; border-color: var(--tx-hair) !important; }
[data-testid="stSelectbox"] input { color: var(--tx-ink) !important; -webkit-text-fill-color: var(--tx-ink); }
[data-testid="stSelectbox"] input::placeholder { color: var(--tx-ink-3) !important; -webkit-text-fill-color: var(--tx-ink-3); }
[data-testid="stSelectbox"] button svg { color: var(--tx-ink-3); }
[data-testid="stSelectboxVirtualDropdown"] { background: var(--tx-menu) !important; border: 1px solid var(--tx-hair) !important;
  box-shadow: var(--tx-pop-shadow) !important; color: var(--tx-ink); }
[data-testid="stSelectboxVirtualDropdown"] [role="option"] { color: var(--tx-ink) !important; background: transparent !important; }
[data-testid="stSelectboxVirtualDropdown"] [role="option"][data-focused="true"],
[data-testid="stSelectboxVirtualDropdown"] [role="option"][data-selected="true"],
[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover { background: rgba(255,255,255,.07) !important; }
[data-testid="stCheckbox"] label > div:not([data-testid]) { background-color: rgba(255,255,255,.18) !important; }
[data-testid="stCheckbox"][data-selected="true"] label > div:not([data-testid]) { background-color: var(--tx-indigo) !important; }
[data-testid="stCheckbox"] label > div:not([data-testid]) > div { background-color: #ececf2 !important; }
[data-testid="stTextAreaRootElement"], [data-testid="stTextInputRootElement"] { background: var(--tx-card) !important;
  border-color: var(--tx-hair) !important; }
[data-testid="stTextAreaRootElement"]:focus-within { border-color: rgba(124,128,245,.55) !important; }
textarea, input { color: var(--tx-ink); }
[data-testid="stTextArea"] textarea { color: var(--tx-ink) !important; -webkit-text-fill-color: var(--tx-ink); caret-color: var(--tx-ink); }
[data-testid="stTextArea"] textarea::placeholder { color: var(--tx-ink-3) !important; -webkit-text-fill-color: var(--tx-ink-3); }
[data-testid="stTextArea"] [data-testid="InputInstructions"], [data-testid="InputInstructions"] { color: var(--tx-ink-3) !important; }
code { background: var(--tx-code-bg); color: var(--tx-code-ink); }
[data-testid="stCaptionContainer"] code, [data-testid="stMarkdownContainer"] code { background: var(--tx-code-bg) !important;
  color: var(--tx-code-ink); }
/* alerts on tints of the dark palette */
[data-testid="stAlertContainer"] { background: rgba(255,255,255,.05) !important; color: var(--tx-ink) !important; }
[data-testid="stAlertContainer"] p, [data-testid="stAlertContainer"] strong { color: var(--tx-ink) !important; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) { background: rgba(242,112,138,.10) !important; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) { background: rgba(232,184,79,.10) !important; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) { background: rgba(92,211,148,.10) !important; }
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) { background: var(--tx-indigo-wash) !important; }
[data-testid="stAlertContentError"] [data-testid="stIconMaterial"] { color: var(--tx-bad); }
[data-testid="stAlertContentWarning"] [data-testid="stIconMaterial"] { color: var(--tx-warn); }
[data-testid="stAlertContentSuccess"] [data-testid="stIconMaterial"] { color: var(--tx-ok); }
[data-testid="stAlertContentInfo"] [data-testid="stIconMaterial"] { color: var(--tx-accent); }
/* code: a dark theme for st.code and the raw JSON viewer */
[data-testid="stCode"] pre, [data-testid="stCode"] code { color: #d7d7e3 !important; }
[data-testid="stCode"] .token.comment, [data-testid="stCode"] .token.prolog { color: #7c7c90 !important; }
[data-testid="stCode"] .token.keyword, [data-testid="stCode"] .token.boolean, [data-testid="stCode"] .token.important {
  color: #b9a3ff !important; }
[data-testid="stCode"] .token.string, [data-testid="stCode"] .token.char, [data-testid="stCode"] .token.attr-value {
  color: #8fd8b0 !important; }
[data-testid="stCode"] .token.number, [data-testid="stCode"] .token.constant { color: #f0b482 !important; }
[data-testid="stCode"] .token.function, [data-testid="stCode"] .token.class-name, [data-testid="stCode"] .token.property,
[data-testid="stCode"] .token.attr-name, [data-testid="stCode"] .token.key, [data-testid="stCode"] .token.atrule {
  color: #8cc4ff !important; }
[data-testid="stCode"] .token.punctuation, [data-testid="stCode"] .token.operator { color: #a3a3b6 !important; }
[data-testid="stCode"] .linenumber, [data-testid="stCode"] .react-syntax-highlighter-line-number { color: #5c5c70 !important; }
[data-testid="stCode"] button { background: var(--tx-card) !important; color: var(--tx-ink-2) !important; }
[data-testid="stJson"], [data-testid="stJson"] .react-json-view { background: transparent !important; color: var(--tx-ink) !important; }
[data-testid="stJson"] span, [data-testid="stJson"] div { color: #a3a3b6 !important; }
[data-testid="stJson"] .object-key, [data-testid="stJson"] .object-key span { color: #8cc4ff !important; }
[data-testid="stJson"] .variable-value, [data-testid="stJson"] .variable-value div,
[data-testid="stJson"] .variable-value span { color: #f0b482 !important; }
[data-testid="stJson"] .variable-value .string-value, [data-testid="stJson"] .string-value { color: #8fd8b0 !important; }
[data-testid="stJson"] svg { color: var(--tx-ink-3) !important; fill: currentColor; }
/* tabs, expanders, status, toasts and the dialog close button */
[data-testid="stTabs"] [role="tablist"]::after { background-color: transparent !important; }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary span { color: var(--tx-ink); }
[data-testid="stExpander"] summary { background: transparent !important; }
[data-testid="stExpander"] summary:hover { background: var(--tx-hover-2) !important; }
[data-testid="stExpanderDetails"] { color: var(--tx-ink); }
[data-testid="stToast"] button, [role="dialog"] button[aria-label="Close"] { color: var(--tx-ink-3) !important; }
[data-testid="stChatInputSubmitButton"] svg { color: var(--tx-ink-2); }
/* Streamlit fills disabled text with its light ink: use the dark tokens (the composer while an agent works or at the end) */
[data-testid="stChatInputTextArea"] { -webkit-text-fill-color: var(--tx-ink) !important; }
[data-testid="stChatInputTextArea"]::placeholder { -webkit-text-fill-color: var(--tx-ink-3) !important; }
[data-testid="stChatInputTextArea"]:disabled::placeholder { -webkit-text-fill-color: rgba(143,143,160,.8) !important; }
input:disabled, textarea:disabled { -webkit-text-fill-color: var(--tx-ink-3) !important; }
""")
DARK_CSS = "<style>\n" + px_to_rem(_DARK.substitute(**DARK_TOKENS, mesh=DARK_MESH)) + "\n</style>"


def style_text(css: str) -> str:
    """What sits between <style> and </style>. st.html runs DOMPurify, which drops a whole style element whose text
    looks like markup, so this must never contain a "<" followed by a letter (tests/test_ui.py checks it)."""
    return css.split("<style>", 1)[-1].rsplit("</style>", 1)[0]


def css_block(extra: str) -> str:
    """views.CSS may come with or without <style> tags."""
    extra = (extra or "").strip()
    return extra if not extra or extra.lstrip().startswith("<style") else f"<style>\n{extra}\n</style>"


def ms(symbol: str) -> str:
    """A Material Symbols glyph for raw HTML (markdown uses :material/<symbol>: instead)."""
    return f'<span class="tx-ms" aria-hidden="true">{escape(symbol)}</span>'


def brand_html() -> str:
    return f'<div class="tx-brand"><span class="tx-brand-mark">{ms("conversion_path")}</span>Migration Workshop</div>'


def crumb_html(run_id: str | None, chip: str | None = None) -> str:
    """The top bar's breadcrumb: "Inventory & Orders / Run <id>" (or "New Run"), plus the Replay or demo chip."""
    where = f'Run <span class="tx-run">{escape(run_id)}</span>' if run_id else "New Run"
    tag = f'<span class="tx-chip-rec">{escape(chip)}</span>' if chip else ""
    return f'<div class="tx-crumb"><b>Inventory &amp; Orders</b><span>/</span>{where}{tag}</div>'


def nav_html(rows: list[tuple[str, str, str, bool, str]], runtime: str = "") -> str:
    """rows: (stage, status, model id, active, anchor or "") in pipeline order: the brand, the named-agent nav and, at
    the bottom, the models in use and where they run."""
    html = ['<div class="tx-side">', brand_html(), '<div class="tx-nav-label">Agents</div>']
    for stage, status, model, active, anchor in rows:
        meta = STAGE_META[stage]
        cls = f"tx-nav tx-{status}{' tx-active' if active else ''}"
        tip = escape(f"{meta.title} · model {model}", quote=True)
        body = (f'{ms(meta.symbol)}<span><span class="tx-nav-name">{escape(meta.short)}</span>'
                f'<span class="tx-nav-meta"><i class="tx-dot"></i>{STATUS_WORDS.get(status, title_case(status))}</span>'
                '</span>')
        if anchor:  # NAV_JS scrolls to the stage's latest card (a keyed container has a class, not an id)
            html.append(f'<a class="{cls}" href="#" data-tx-target="st-key-{escape(anchor)}" title="{tip}" '
                        f'target="_self">{body}</a>')
        else:
            html.append(f'<div class="{cls}" title="{tip}">{body}</div>')
    models = list(dict.fromkeys(friendly_model(row[2]) for row in rows if row[2]))
    foot = escape(" · ".join(models))
    if runtime:
        foot += ("<br>" if foot else "") + escape(runtime)
    if foot:
        html.append(f'<div class="tx-nav-foot">{foot}</div>')
    html.append("</div>")
    return "".join(html)


def presenter_html(name: str) -> str:
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    return (f'<div class="tx-presenter"><span class="tx-initials">{escape(initials)}</span>'
            f'<span>{escape(name)}</span></div>')


def progress_html(rows: list[tuple[str, str]]) -> str:
    """rows: (stage, approved | current | failed | pending): the compact strip presenter mode shows in the top bar."""
    parts = []
    for stage, state in rows:
        lead = ms("check") if state == "approved" else '<span class="tx-c"></span>'
        parts.append(f'<span class="tx-s tx-{state}" role="listitem">{lead}{escape(STAGE_META[stage].short)}</span>')
    return (f'<div class="tx-strip" role="list" aria-label="Migration progress"><span class="tx-strip-track">'
            f'{"".join(parts)}</span></div>')


def suggestion_html(symbol: str, sentence: str, caption: str) -> str:
    """sentence may contain <em>…</em> for the indigo entity; everything else is escaped by the caller."""
    return (f'<div class="tx-sg">{ms(symbol)}<div class="tx-sg-text">{sentence}</div>'
            f'<div class="tx-sg-cap">{escape(caption)}</div></div>')


def _tile(symbol: str) -> str:
    return f'<div class="tx-tile">{ms(symbol)}</div>'


def agents_html() -> str:
    rows = []
    for n, meta in enumerate(STAGE_META.values(), start=1):
        rows.append(f'<div class="tx-agent">{_tile(meta.symbol)}<div>'
                    f'<div class="tx-agent-title">{n} · {escape(meta.title)}</div>'
                    f'<div class="tx-agent-does">{escape(meta.does)}.</div>'
                    f'<div class="tx-agent-an">In AWS Transform: {escape(meta.analogue)}</div></div></div>')
    return "".join(rows)


# The "before" picture (legacy-infra/README.md, condensed): what the agents will discover and migrate.
LEGACY_ESTATE = [
    ("dns", "One EC2 Instance, Set Up by Hand",
     "t3.large, Amazon Linux 2023, gp2 volume, one subnet in a single AZ. No load balancer, no Auto Scaling."),
    ("code", "A Flask Monolith from 2017",
     "nginx in front of the Flask dev server: a JSON API for the warehouse scanners and a small dashboard."),
    ("database", "Everything on the Box",
     "SQLite on the root volume, CSV reports from a nightly cron job, logs in a local file."),
]
LEGACY_SMELLS = ["Hard-coded secrets (admin123)", "SQL injection in search", "Flask dev server as root",
                 "SQLite on local disk, no backups", "No health check, single AZ", "Oversized t3.large on gp2"]


def legacy_html() -> str:
    """The legacy-estate dialog: three facts and the headline smells (11 deliberate ones, 5 more for keen eyes)."""
    rows = [f'<div class="tx-agent">{_tile(symbol)}<div>'
            f'<div class="tx-agent-title">{escape(title)}</div><div class="tx-agent-does">{escape(text)}</div>'
            '</div></div>' for symbol, title, text in LEGACY_ESTATE]
    smells = "".join(f"<li>{escape(smell)}</li>" for smell in LEGACY_SMELLS)
    return ("".join(rows) + '<div class="tx-legacy-h">Planted for the Agents to Find</div>'
            f'<ul class="tx-legacy-smells">{smells}</ul>'
            '<div class="tx-legacy-foot">11 deliberate smells, 5 more for keen eyes. The Terraform in '
            '<code>legacy-infra/</code> rebuilds this server exactly, smells included.</div>')


_SCROLL_JS = Template("""<div class="tx-scroll" data-nonce="$nonce"></div><script>
(function () {
  try {
    var sel = $sel, land = $land;
    function scroller(el) {
      for (var p = el && el.parentElement; p; p = p.parentElement) {
        var oy = getComputedStyle(p).overflowY;
        if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight) { return p; }
      }
      return document.scrollingElement;
    }
    function box(el) { return el.getBoundingClientRect(); }
    function rootPx(name, fallback) {
      var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
      return isNaN(v) ? fallback : v;
    }
    // the lowest viewport y a result may reach: the top of the sticky gate (it sits on the composer), minus the
    // white fade above the gate
    function floor(view) {
      var y = Math.min(box(view).bottom, innerHeight);
      var bar = document.querySelector('[data-testid="stBottom"]');
      if (bar && box(bar).height) { y = Math.min(y, box(bar).top); }
      var gate = document.querySelector(".st-key-gate");
      if (gate) { y -= box(gate).height + 10; }
      return y - 24;
    }
    // where to scroll: {view, top} (top = the scroller's scrollTop), or null
    function plan() {
      var block = document.querySelector('[data-testid="stMainBlockContainer"]');
      var el = sel === "top" ? null : document.querySelector(sel);
      if (!el && !block) { return null; }
      var view = scroller(el || block), cur = view.scrollTop, vtop = box(view).top;
      var max = Math.max(0, view.scrollHeight - view.clientHeight);
      function clamp(v) { return Math.max(0, Math.min(max, Math.round(v))); }
      if (!el) { return {view: view, top: 0}; }
      function at(node) { return box(node).top - vtop + cur; }  // y of a node in the scroller's content
      var off = rootPx("--tx-top-offset", 84), align = at(el) - off;
      if (!land) {
        // the first screen of the page: scroll right to the top so the top bar stays whole
        if (block && box(el).top - box(block).top < 360) { return {view: view, top: 0}; }
        return {view: view, top: clamp(align)};
      }
      // a finished result: everything above its tabs (header, headline, KPIs, evidence) must clear the gate
      var lim = floor(view) - vtop;
      var tabs = el.querySelector('[data-testid="stTabs"]');
      var hero = tabs ? at(tabs) : at(el) + box(el).height;
      if (hero <= lim) { return {view: view, top: 0}; }           // fits with the top bar still on screen
      if (hero - align <= lim) { return {view: view, top: clamp(align)}; }
      // too tall for the band (the FinOps chart sits above the headline): scroll just far enough for the KPIs
      // (or, without KPIs, the headline) to clear the gate, but never past the headline itself
      var kpis = el.querySelector(".tx-kpis"), lead = el.querySelector(".tx2-headline");
      var anchor = kpis || lead;
      if (!anchor) { return {view: view, top: clamp(align)}; }
      var need = at(anchor) + box(anchor).height - lim;
      var cap = lead ? at(lead) - off
                     : at(kpis) - off - 6 * parseFloat(getComputedStyle(document.documentElement).fontSize);
      return {view: view, top: clamp(Math.max(align, Math.min(need, cap)))};
    }
    function go(behavior) {
      var p = plan();
      if (p) { p.view.scrollTo({top: p.top, behavior: behavior}); }
    }
    // the presenter takes over (scrolls, clicks, types): the settle passes below then leave the page alone
    var touched = false, inputs = ["wheel", "touchstart", "keydown", "pointerdown"];
    function touch() { touched = true; }
    inputs.forEach(function (name) { window.addEventListener(name, touch, {capture: true, passive: true}); });
    function settle(last) {  // late layout (charts, fonts) or Streamlit's follow-the-bottom may have moved things
      var p = touched ? null : plan();
      if (p && Math.abs(p.view.scrollTop - p.top) > 24) { p.view.scrollTo({top: p.top, behavior: "auto"}); }
      if (last) { inputs.forEach(function (name) { window.removeEventListener(name, touch, {capture: true}); }); }
    }
    requestAnimationFrame(function () {
      go("smooth");
      setTimeout(function () { settle(false); }, 700);
      setTimeout(function () { settle(true); }, 1600);
    });
  } catch (e) { /* never break the page for a scroll */ }
})();
</script>""")

# Focus the "Ask for Changes" textarea when the dialog opens, so the presenter can type straight away. It retries for
# about a second (the modal moves focus to itself as it opens) and stops at the first key or click. Fails safe.
FOCUS_JS = """<div class="tx-scroll"></div><script>
(function () {
  try {
    var stop = false, t0 = Date.now();
    function halt() { stop = true; }
    document.addEventListener("keydown", halt, {capture: true, once: true});
    document.addEventListener("pointerdown", halt, {capture: true, once: true});
    (function tick() {
      if (stop) { return; }
      var t = document.querySelector('[role="dialog"] textarea');
      if (t && document.activeElement !== t) { t.focus({preventScroll: true}); }
      if (Date.now() - t0 < 1200) { setTimeout(tick, 80); }
      else {
        document.removeEventListener("keydown", halt, {capture: true});
        document.removeEventListener("pointerdown", halt, {capture: true});
      }
    })();
  } catch (e) { /* the presenter can still click into the box */ }
})();
</script>"""


# One delegated click handler (installed once per page): a nav row scrolls its stage's latest card into view.
NAV_JS = """<div class="tx-scroll"></div><script>
(function () {
  try {
    if (window.__txNav) { return; }
    window.__txNav = true;
    document.addEventListener("click", function (ev) {
      var a = ev.target && ev.target.closest ? ev.target.closest("a[data-tx-target]") : null;
      if (!a) { return; }
      ev.preventDefault();
      var el = document.querySelector("." + a.getAttribute("data-tx-target"));
      if (el) { el.scrollIntoView({block: "start", behavior: "smooth"}); }
    }, true);
  } catch (e) { /* the nav still shows status without it */ }
})();
</script>"""


def scroll_html(selector: str, nonce: str, land: bool = False) -> str:
    """A 0-height script (st.html with unsafe_allow_javascript) that scrolls `selector` (or "top") into view once.
    land=True is for a finished result: its header, headline and KPIs must land above the sticky gate.
    The element is hidden by CSS; if the script fails, nothing else is affected."""
    return _SCROLL_JS.substitute(sel=json.dumps(selector), nonce=escape(nonce, quote=True),
                                 land="true" if land else "false")
