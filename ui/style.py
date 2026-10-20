"""Look and feel: stage metadata, design tokens, the CSS and small HTML fragments (nav, progress strip, scroll)."""

import json
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
        "Discovery / assessment", symbol="travel_explore", short="Discovery",
        doing="Reading the code and the infrastructure",
        bridge="Discovery and assessment at portfolio scale: hundreds of servers, dependency mapping, application "
               "grouping and the business case.",
        benefit="Weeks of archaeology become an evidence-backed starting point."),
    "requirements": StageMeta(
        ":material/checklist:", "Requirements", "Turn the uploaded request into a traceable migration spec",
        "Job objective intake + HITL clarifications", symbol="checklist", short="Requirements",
        doing="Turning the request into a migration spec",
        bridge="Every job follows the same pattern: agents propose, people review and approve each step.",
        benefit="Speed without giving up control."),
    "modernization": StageMeta(
        ":material/deployed_code:", "Code Modernization",
        "Refactor the monolith into a microservice, Dockerfile and manifests",
        "Code transformation + containerization", symbol="deployed_code", short="Modernization",
        doing="Porting the business logic",
        bridge="Code transformation and containerization: .NET porting, Java and Python upgrades, mainframe "
               "reimagining, and AWS Transform custom for your own patterns.",
        benefit="The repetitive 80% is done consistently; experts spend their time on the 20% that matters."),
    "terraform": StageMeta(
        ":material/stacks:", "Terraform IaC", "Adapt a validated EKS blueprint: VPC, EKS, ECR, RDS",
        "Network / infrastructure conversion", symbol="stacks", short="Infrastructure",
        doing="Adapting the validated blueprint",
        bridge="Network and infrastructure conversion to infrastructure as code: CloudFormation, CDK or Terraform.",
        benefit="A landing zone in days instead of a quarter of hand-written templates."),
    "k8s_validation": StageMeta(
        ":material/verified:", "K8s Validation", "Schema and policy checks, auto-fixes, optional live deploy to kind",
        "Test & validation", symbol="verified", short="Validation",
        doing="Checking policies and deploying",
        bridge="Validation is part of every job: tests, test environments and cutover checks.",
        benefit="You don't find out in production."),
    "finops": StageMeta(
        ":material/savings:", "FinOps Right-sizing", "Current vs target monthly cost, right-sizing and savings",
        "Assessment TCO / business case", symbol="savings", short="FinOps",
        doing="Pricing the current and the target estate",
        bridge="Assessment and business case: know the cost before you migrate.",
        benefit="No surprises on the first invoice."),
}

# Design tokens (Fluent-style light theme; spec in the UX review). Use these for charts and inline HTML so CSS,
# charts and markup stay consistent.
TOKENS = {
    "canvas": "#F6F4FB", "glass": "rgba(255,255,255,.72)", "card": "rgba(255,255,255,.96)", "border": "#E0E0E0",
    "text": "#242424", "text2": "#616161", "disabled": "#707070",
    "indigo": "#5B5FC7", "indigo_pressed": "#4F52B2", "indigo_tint": "#EBEBF8",
    "success": "#0E700E", "success_bg": "#F1FAF1", "warning": "#8A3707", "warning_bg": "#FFF9F5",
    "danger": "#C50F1F", "danger_bg": "#FDF3F4", "info": "#0F6CBD", "info_bg": "#EBF3FC",
    "chart_today": "#8A8886", "chart_over": "#C50F1F", "chart_optimized": "#5B5FC7", "chart_budget": "#616161",
}

# Container keys shared by app.py (shell) and views.py (cards); CSS in this module targets `.st-key-<key>`.
#   views.py creates: live-<stage>, card-<stage>, kpis-<stage>, bridge-<stage>, finale
#   app.py creates:   topbar, gate (+ gate-row), welcome-input, suggestions, recorded-chip
#                     (and welcome, sg-<name>, gate-step, wrapup, recap, transcript, stage-<job id>, fake-chip)

STATUS_WORDS = {"pending": "Not started", "running": "In progress", "awaiting_approval": "Needs review",
                "approved": "Approved", "failed": "Failed"}

# Friendly model names for the nav; the raw Bedrock model id stays in the tooltip.
MODEL_NAMES = [("nova-2-lite", "Nova 2 Lite"), ("nova-2-pro", "Nova 2 Pro"), ("nova-premier", "Nova Premier"),
               ("nova-pro", "Nova Pro"), ("nova-lite", "Nova Lite"), ("nova-micro", "Nova Micro"),
               ("deepseek.v3.2", "DeepSeek V3.2"), ("deepseek.v3", "DeepSeek V3"), ("deepseek.r1", "DeepSeek R1"),
               ("mistral-large-3", "Mistral Large 3"), ("ministral-3-14b", "Ministral 3 14B")]


def friendly_model(model_id: str) -> str:
    low = (model_id or "").lower()
    return next((name for needle, name in MODEL_NAMES if needle in low), model_id or "")


def rem(px: float) -> str:
    """Sizes are written in px (spec at 1280x720, 100% zoom, 18 px root) and emitted in rem, so presenter mode
    scales everything by changing one CSS variable (--tx-scale)."""
    return f"{px / 18:.4g}rem"


_CSS = Template("""
<style>
:root {
  --tx-scale: 1;
  --tx-canvas: $canvas; --tx-glass: $glass; --tx-card: $card; --tx-border: $border;
  --tx-text: $text; --tx-text2: $text2; --tx-disabled: $disabled;
  --tx-indigo: $indigo; --tx-indigo-pressed: $indigo_pressed; --tx-indigo-tint: $indigo_tint;
  --tx-success: $success; --tx-success-bg: $success_bg; --tx-warning: $warning; --tx-warning-bg: $warning_bg;
  --tx-danger: $danger; --tx-danger-bg: $danger_bg; --tx-info: $info; --tx-info-bg: $info_bg;
  --tx-shadow-card: 0 2px 8px rgba(0,0,0,.06);
  --tx-shadow-glass: 0 8px 32px rgba(36,36,36,.08);
  --tx-shadow-float: 0 8px 28px rgba(36,36,36,.14);
  --tx-chat-h: 99px;       /* the bottom bar (chat input + its padding): the sticky gate sits right on top of it */
  --tx-top-offset: 72px;   /* what scrollIntoView keeps clear at the top (Streamlit header + air) */
  --tx-sheet: var(--tx-glass);  /* the main sheet's fill (presenter mode makes it near-opaque) */
  /* the canvas glow; "fixed" layers line up with the page canvas wherever they are painted (bottom band, gate) */
  --tx-glow:
    radial-gradient(ellipse 46% 52% at 6% 4%, rgba(167,139,250,.30), transparent 72%),
    radial-gradient(ellipse 40% 46% at 96% 8%, rgba(244,114,182,.22), transparent 72%),
    radial-gradient(ellipse 52% 50% at 72% 100%, rgba(56,189,248,.22), transparent 72%);
}
html { font-size: calc(18px * var(--tx-scale)) !important; }

/* ---------------------------------------------------------------- canvas and the two glass layers */
[data-testid="stApp"] {
  background: var(--tx-glow), var(--tx-canvas);
  color: var(--tx-text);
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMain"] { background: transparent; }
[data-testid="stMainBlockContainer"] {
  max-width: 1100px; width: calc(100% - 28px); padding: 20px 32px 32px; margin: 64px auto 12px;
  min-height: calc(100vh - 120px); flex-shrink: 0;  /* a flex child of the scroll container: never shrink */
  background: var(--tx-sheet);
  -webkit-backdrop-filter: blur(24px) saturate(140%); backdrop-filter: blur(24px) saturate(140%);
  border: 1px solid rgba(255,255,255,.9); border-radius: 24px; box-shadow: var(--tx-shadow-glass);
}
[data-testid="stSidebar"] { background: transparent; border-right: none; box-shadow: none; }
[data-testid="stSidebar"][data-collapsed="false"] { width: 300px !important; min-width: 300px !important;
  max-width: 300px !important; }
[data-testid="stSidebarContent"] {
  margin: 12px 0 12px 12px; width: calc(100% - 12px); height: calc(100% - 24px); border-radius: 24px;
  background: var(--tx-glass);
  -webkit-backdrop-filter: blur(24px) saturate(140%); backdrop-filter: blur(24px) saturate(140%);
  border: 1px solid rgba(255,255,255,.9); box-shadow: var(--tx-shadow-glass);
}
[data-testid="stSidebarContent"] { padding: 0; }
[data-testid="stSidebarHeader"] { height: 44px; min-height: 44px; padding: 0 12px; margin-bottom: 0; }
[data-testid="stSidebarUserContent"] { padding: 0 12px 24px !important; }
[data-testid="stBottom"] > div { background: transparent; }
[data-testid="stBottomBlockContainer"] { max-width: 1100px; width: calc(100% - 28px); padding: 12px 32px 20px; }
/* The bottom band: an opaque continuation of the sheet behind the chat bar, so no text shows through around it.
   The sheet's own fill over the "fixed" canvas glow matches the glass sheet without a backdrop blur. Its top 12 px
   fade in; the sticky gate sits right on top of the band and covers that seam with its own backdrop (below). */
[data-testid="stBottomBlockContainer"]:has([data-testid="stChatInput"]) {
  background-image: linear-gradient(var(--tx-sheet), var(--tx-sheet)), var(--tx-glow);
  background-color: var(--tx-canvas); background-attachment: fixed;
  border-left: 1px solid rgba(255,255,255,.9); border-right: 1px solid rgba(255,255,255,.9);
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 12px);
  mask-image: linear-gradient(to bottom, transparent 0, #000 12px);
}
/* with a chat bar, the sheet runs into the band: one surface down to the screen's edge, no seam at the page end */
[data-testid="stAppViewContainer"]:has([data-testid="stChatInput"]) [data-testid="stMainBlockContainer"] {
  margin-bottom: 0; border-bottom: none; border-bottom-left-radius: 0; border-bottom-right-radius: 0;
}

/* ---------------------------------------------------------------- type */
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li { line-height: 1.556; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {
  font-size: $f15; line-height: 1.45; color: var(--tx-text2); opacity: 1;
}
[data-testid="stMarkdownContainer"] a { color: var(--tx-indigo); }
[data-testid="stMarkdownContainer"] code { font-size: .85em; color: var(--tx-text); background: #F3F3F6;
  border-radius: 6px; padding: .1em .35em; }
/* Streamlit pulls markdown up by 1rem to cancel a paragraph margin; our HTML-only blocks have none */
[data-testid="stMarkdownContainer"]:has(> .tx-brand, > .tx-strip, > .tx-sg-icon, > .tx-nav-h, > .tx-presenter,
  > .tx-recap-row, > .tx-foot, > .tx-agent, > .tx-hello) { margin-bottom: 0 !important; }
.tx-ms { font-family: "Material Symbols Rounded"; font-weight: normal; font-style: normal; font-size: 1.25em;
  line-height: 1; letter-spacing: normal; text-transform: none; display: inline-block; white-space: nowrap;
  direction: ltr; font-feature-settings: "liga"; -webkit-font-smoothing: antialiased; vertical-align: middle;
  user-select: none; }

/* ---------------------------------------------------------------- buttons */
button[data-testid="stBaseButton-primary"], button[data-testid="stBaseButton-primaryFormSubmit"],
[data-testid="stPopover"] button[kind="primary"] {
  background: var(--tx-indigo); border: 1px solid var(--tx-indigo); color: #FFFFFF; font-weight: 600;
  border-radius: 8px; box-shadow: none;
}
button[data-testid="stBaseButton-primary"]:hover, button[data-testid="stBaseButton-primaryFormSubmit"]:hover,
[data-testid="stPopover"] button[kind="primary"]:hover {
  background: var(--tx-indigo-pressed); border-color: var(--tx-indigo-pressed); color: #FFFFFF;
}
button[data-testid="stBaseButton-primary"]:active, [data-testid="stPopover"] button[kind="primary"]:active {
  background: #444791; border-color: #444791; color: #FFFFFF;
}
button[data-testid="stBaseButton-secondary"], button[data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stPopover"] button[kind="secondary"], button[data-testid="stBaseButton-secondary"]:focus:not(:active) {
  background: #FFFFFF; border: 1px solid #D1D1D1; color: var(--tx-text); font-weight: 600; border-radius: 8px;
}
button[data-testid="stBaseButton-secondary"]:hover, [data-testid="stPopover"] button[kind="secondary"]:hover {
  background: #F5F5F5; border-color: #C7C7C7; color: var(--tx-text);
}
button[data-testid="stBaseButton-secondary"]:active { background: #E0E0E0; color: var(--tx-text); }
button:disabled, button[disabled] { opacity: 1; cursor: not-allowed; }
button[data-testid="stBaseButton-primary"]:disabled, [data-testid="stPopover"] button[kind="primary"]:disabled,
button[data-testid="stBaseButton-secondary"]:disabled, [data-testid="stPopover"] button[kind="secondary"]:disabled {
  background: #F0F0F0; border-color: #E0E0E0; color: #BDBDBD;  /* Fluent disabled: neutral, never a pale accent */
}
button:disabled p, button:disabled [data-testid="stIconMaterial"] { color: #BDBDBD; }
button p { white-space: nowrap; }

/* ---------------------------------------------------------------- top bar */
.st-key-topbar { padding: 0 0 12px; margin-bottom: 4px; border-bottom: 1px solid rgba(36,36,36,.07); }
/* title | switch | actions: a grid, so the switch is centred and nothing wraps at 1280 px */
.st-key-tb-row { display: grid !important; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center;
  column-gap: 16px; }
.st-key-tb-row > [data-testid="stLayoutWrapper"] { width: auto !important; min-width: 0; }
.st-key-tb-left { gap: 4px; justify-content: center; width: auto !important; }
.st-key-tb-left [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
.st-key-tb-mid { justify-content: center; width: auto !important; }
.st-key-tb-right { flex-wrap: nowrap; width: auto !important; }
@media (max-width: 1180px) {  /* 1280 px at 125% zoom with the nav open: the switch keeps its full labels */
  .st-key-tb-row { grid-template-columns: minmax(0, 1fr) auto auto; }
  .st-key-tb-row .tx-brand { font-size: $f15; overflow: hidden; text-overflow: ellipsis; }
  .st-key-topbar [data-testid="stButtonGroup"] button [data-testid="stIconMaterial"] { display: none; }
  .st-key-topbar [data-testid="stButtonGroup"] button { padding: 2px 14px; }
}
.tx-brand { font-size: $f16; font-weight: 600; color: var(--tx-text); white-space: nowrap; line-height: 1.3; }
.tx-brand .tx-x { color: var(--tx-indigo); margin: 0 .28em; font-weight: 600; }
.st-key-recorded-chip, .st-key-fake-chip {
  display: inline-flex; width: auto !important; flex: 0 0 auto; align-self: flex-start; padding: 1px 10px 1px 8px;
  border-radius: 999px; background: #FFFFFF; border: 1px solid var(--tx-border);
}
[data-testid="stLayoutWrapper"]:has(> .st-key-recorded-chip), [data-testid="stLayoutWrapper"]:has(> .st-key-fake-chip) {
  width: auto !important; align-self: flex-start;
}
.st-key-recorded-chip p, .st-key-fake-chip p { font-size: $f14; font-weight: 600; color: var(--tx-text2);
  white-space: nowrap; margin: 0; line-height: 1.6; }
.st-key-topbar [data-testid="stButtonGroup"] { justify-content: center; }
.st-key-topbar [data-testid="stButtonGroup"] > div {
  background: #FFFFFF; border: 1px solid var(--tx-border); border-radius: 999px; padding: 3px; gap: 2px;
  box-shadow: var(--tx-shadow-card);
}
.st-key-topbar [data-testid="stButtonGroup"] button {
  border: none !important; border-radius: 999px !important; background: transparent; color: var(--tx-text2);
  font-weight: 600; min-height: 36px; padding: 2px 18px; box-shadow: none;
}
.st-key-topbar [data-testid="stButtonGroup"] button:hover { background: #F5F5F5; color: var(--tx-text); }
.st-key-topbar [data-testid="stButtonGroup"] button[kind="segmented_controlActive"],
.st-key-topbar [data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"] {
  background: var(--tx-indigo-tint); color: var(--tx-indigo-pressed);
}
.st-key-topbar [data-testid="stButtonGroup"] button[kind="segmented_controlActive"] p,
.st-key-topbar [data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"] p {
  color: var(--tx-indigo-pressed);
}
[data-testid="stPopoverButton"] div[aria-hidden="true"] { display: none; }  /* no dropdown chevron */
[data-testid="stPopoverBody"] {
  border-radius: 16px; border: 1px solid var(--tx-border); box-shadow: var(--tx-shadow-float);
  padding: 16px 20px 20px; min-width: 360px; max-width: 460px; background: #FFFFFF;
}
.tx-menu-h { font-size: $f14; font-weight: 600; color: var(--tx-text2); margin: 4px 0 -4px; }

/* presenter progress strip */
.tx-strip { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 6px 8px;
  padding: 8px 0 0; font-size: $f14; font-weight: 600; color: var(--tx-text2); }
.tx-strip .tx-s { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.tx-strip .tx-c { width: 12px; height: 12px; border-radius: 50%; border: 2px solid #8A8886; box-sizing: border-box; }
.tx-strip .tx-approved .tx-c { background: var(--tx-indigo); border-color: var(--tx-indigo); }
.tx-strip .tx-current { color: var(--tx-text); }
.tx-strip .tx-current .tx-c { border-color: var(--tx-indigo);
  background: linear-gradient(90deg, var(--tx-indigo) 50%, #FFFFFF 50%); }
.tx-strip .tx-failed .tx-c { border-color: var(--tx-danger); background: var(--tx-danger); }
.tx-strip .tx-l { width: 22px; height: 2px; border-radius: 2px; background: #D1D1D1; }
.tx-strip .tx-l.tx-done { background: var(--tx-indigo); }

/* ---------------------------------------------------------------- sidebar: named-agent nav */
.tx-nav-h { font-size: $f14; font-weight: 600; color: var(--tx-text2); padding: 8px 12px 6px; }
.tx-nav { position: relative; display: flex; gap: 10px; align-items: center; padding: 10px 10px 10px 12px; margin: 0 0 4px;
  border-radius: 12px; color: var(--tx-text); text-decoration: none; }
a.tx-nav:hover { background: rgba(255,255,255,.7); text-decoration: none; color: var(--tx-text); }
.tx-nav .tx-ms { flex: none; font-size: 24px; color: var(--tx-text2); width: 24px; text-align: center; }
.tx-nav > div { min-width: 0; }
.tx-nav-name { font-weight: 600; font-size: $f17; line-height: 1.3; }
.tx-nav-meta { display: block; font-size: $f14; line-height: 1.4; color: var(--tx-text2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tx-nav-meta .tx-dot { margin: 0 5px 1px 0; vertical-align: middle; }
.tx-nav-meta .tx-sep { margin: 0 3px; }
.tx-nav.tx-active { background: #FFFFFF; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
.tx-nav.tx-active::before { content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
  border-radius: 3px; background: var(--tx-indigo); }
.tx-nav.tx-active .tx-ms { color: var(--tx-indigo); }
.tx-dot { flex: none; width: 8px; height: 8px; border-radius: 50%; box-sizing: border-box; display: inline-block; }
.tx-pending .tx-dot { border: 1.5px solid #8A8886; }
.tx-running .tx-dot { background: var(--tx-indigo); animation: tx-pulse 1.6s ease-in-out infinite; }
.tx-awaiting_approval .tx-dot { background: var(--tx-warning); }
.tx-approved .tx-dot { background: var(--tx-success); }
.tx-failed .tx-dot { background: var(--tx-danger); }
.tx-running .tx-st { color: var(--tx-indigo-pressed); }
.tx-awaiting_approval .tx-st { color: var(--tx-warning); }
.tx-approved .tx-st { color: var(--tx-success); }
.tx-failed .tx-st { color: var(--tx-danger); }
@keyframes tx-pulse { 50% { opacity: .35; } }
.tx-presenter { display: flex; align-items: center; gap: 10px; margin-top: 20px; padding: 14px 12px 0;
  border-top: 1px solid rgba(36,36,36,.08); font-size: $f15; font-weight: 600; color: var(--tx-text); }
.tx-initials { flex: none; width: 32px; height: 32px; border-radius: 50%; display: grid; place-items: center;
  background: var(--tx-indigo-tint); color: var(--tx-indigo-pressed); font-size: $f14; font-weight: 600; }

/* ---------------------------------------------------------------- welcome */
.st-key-welcome { max-width: 860px; margin: 0 auto; padding-top: 3vh; gap: 20px; }
.tx-hello { font-size: $f40; line-height: $f48; font-weight: 700; color: var(--tx-text); text-align: center;
  letter-spacing: -.01em; margin: 0; text-wrap: balance; }
.tx-hello-sub { font-size: $f18; line-height: $f28; color: var(--tx-text2); text-align: center; margin: 8px 0 8px; }
.st-key-welcome-input {
  background: #FFFFFF; border: 1px solid var(--tx-border); border-radius: 24px;
  box-shadow: 0 4px 20px rgba(36,36,36,.08); padding: 10px 10px 10px 12px;
}
.st-key-welcome-input:focus-within, .st-key-welcome-input:hover { border-color: #C5C6EE; }
.st-key-welcome-input > div:has([data-testid="stFileUploader"]) { flex: 1 1 auto !important; min-width: 0; width: auto !important; }
.st-key-welcome-input > div:has(.stButton) { flex: 0 0 auto !important; width: auto !important; }
.st-key-welcome-input [data-testid="stFileUploader"] section,
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] {
  background: transparent; border: none; padding: 6px 4px; min-height: 52px;
}
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] { position: relative; justify-content: flex-start;
  align-items: center; display: flex; }
.st-key-welcome-input [data-testid="stFileUploaderDropzoneInstructions"] { display: none; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"]::after {
  content: "Drop the business requirements here, or pick a suggestion below";
  order: 2; color: var(--tx-text2); font-size: $f17; line-height: 1.4; margin-left: 12px;
}
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button {
  order: 1; width: 40px; height: 40px; min-height: 40px; padding: 0; border-radius: 50%; border: 1px solid #D1D1D1;
  background: #FFFFFF; color: transparent; overflow: hidden; position: relative; flex: none; margin: 0;
}
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button > * { visibility: hidden; }
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button::before {
  content: "add"; font-family: "Material Symbols Rounded"; font-feature-settings: "liga"; font-size: 24px;
  color: var(--tx-indigo); position: absolute; inset: 0; display: grid; place-items: center; visibility: visible;
}
.st-key-welcome-input [data-testid="stFileUploaderDropzone"] button:hover { background: var(--tx-indigo-tint); }
.st-key-welcome-input [data-testid="stFileUploaderFile"] { padding-left: 8px; }
.st-key-welcome [data-testid="stExpander"] details { background: #FFFFFF; }
.st-key-suggestions { gap: 16px; align-items: stretch; }
.st-key-suggestions > [data-testid="stLayoutWrapper"] { flex: 1 1 0; align-self: stretch; }
.st-key-suggestions [class*="st-key-sg-"] {
  position: relative; height: 100%; background: var(--tx-card); border: 1px solid var(--tx-border);
  border-radius: 20px; box-shadow: var(--tx-shadow-card); padding: 20px; gap: 0;
  transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease;
}
.st-key-suggestions [class*="st-key-sg-"]:hover { border-color: #C5C6EE; box-shadow: 0 6px 18px rgba(36,36,36,.10);
  transform: translateY(-1px); }
.st-key-suggestions [class*="st-key-sg-"]:has(button:focus-visible) { outline: 2px solid var(--tx-indigo);
  outline-offset: 2px; }
.st-key-suggestions [class*="st-key-sg-"] [data-testid="stElementContainer"]:has(button) {
  position: absolute; inset: 0; margin: 0; z-index: 2; width: auto !important; height: auto;
}
.st-key-suggestions [class*="st-key-sg-"] [data-testid="stElementContainer"]:has(button) > div,
.st-key-suggestions [class*="st-key-sg-"] [data-testid="stElementContainer"]:has(button) [data-testid="stButton"] {
  width: 100%; height: 100%;
}
.st-key-suggestions [class*="st-key-sg-"] button { width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.tx-sg-icon { width: 40px; height: 40px; border-radius: 12px; display: grid; place-items: center;
  background: var(--tx-indigo-tint); color: var(--tx-indigo); }
.tx-sg-icon .tx-ms { font-size: 22px; }
.tx-sg-text { font-size: $f17; line-height: 1.45; color: var(--tx-text); margin-top: 14px; }
.tx-sg-text b { color: var(--tx-indigo); font-weight: 600; }
.tx-sg-cap { font-size: $f15; line-height: 1.4; color: var(--tx-text2); margin-top: 4px; }
.tx-foot { text-align: center; font-size: $f15; color: var(--tx-text2); margin-top: 8px; }

/* six agents dialog */
.tx-agent { display: flex; gap: 14px; align-items: flex-start; padding: 12px 0; border-bottom: 1px solid #EDEDED; }
.tx-agent:last-child { border-bottom: none; }
.tx-agent .tx-sg-icon { flex: none; }
.tx-agent-title { font-weight: 600; font-size: $f17; color: var(--tx-text); }
.tx-agent-does { font-size: $f16; color: var(--tx-text); line-height: 1.45; }
.tx-agent-an { font-size: $f15; color: var(--tx-text2); margin-top: 2px; }
/* legacy estate dialog */
.tx-legacy-h { font-size: $f15; font-weight: 600; color: var(--tx-text2); margin: 14px 0 6px; }
.tx-legacy-smells { margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: 1fr 1fr;
  gap: 6px 20px; }
.tx-legacy-smells li { font-size: $f16; line-height: 1.4; color: var(--tx-text); margin: 0; display: flex;
  align-items: center; gap: 8px; }
.tx-legacy-smells .tx-ms { font-size: 18px; color: var(--tx-warning); }
@media (max-width: 640px) { .tx-legacy-smells { grid-template-columns: 1fr; } }
.tx-legacy-foot { font-size: $f15; color: var(--tx-text2); margin-top: 10px; }

/* ---------------------------------------------------------------- transcript */
[data-testid="stChatMessage"] { background: transparent; padding: 4px 0; gap: 14px; }
[data-testid="stChatMessage"] [data-testid^="stChatMessageAvatar"] {
  width: 36px; height: 36px; border-radius: 12px; background: var(--tx-indigo-tint); color: var(--tx-indigo);
  border: none;
}
[data-testid="stChatMessage"]:has(> [aria-label="Chat message from user"]) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has(> [aria-label="Chat message from user"]) [data-testid^="stChatMessageAvatar"] {
  background: #F0F0F0; color: var(--tx-text2);
}
[data-testid="stChatMessage"] > [aria-label="Chat message from user"] {
  flex: 0 1 auto; max-width: 72%; margin-left: auto !important; margin-right: 0 !important;
  background: var(--tx-indigo-tint); border-radius: 16px 4px 16px 16px;
  padding: 10px 16px;
}
[data-testid="stChatMessage"] > [aria-label="Chat message from user"] p { margin: 0; }
[class*="st-key-stage-"] { scroll-margin-top: var(--tx-top-offset); }
/* stage cards carry their own stage symbol: no avatar column, so the card gets the full width */
[class*="st-key-stage-"] [data-testid="stChatMessage"] > [data-testid^="stChatMessageAvatar"] { display: none; }
.st-key-wrapup, .st-key-gate { scroll-margin-top: var(--tx-top-offset); }
[data-testid="stElementContainer"]:has(.tx-scroll) { display: none; }

/* ---------------------------------------------------------------- the gate: always on screen after a result */
[data-testid="stLayoutWrapper"]:has(> .st-key-gate) {  /* the wrapper is the sticky box (it is as tall as the gate) */
  position: sticky; bottom: var(--tx-chat-h); z-index: 30;  /* flush on the bottom band: no gap to see through */
}
/* the gate's backdrop: from its middle down past its bottom edge (into the band's fade), the full sheet width, so
   nothing shows through at the rounded corners or between the gate and the chat bar */
[data-testid="stLayoutWrapper"]:has(> .st-key-gate)::after {
  content: ""; position: absolute; left: -32px; right: -32px; top: 50%; bottom: -14px; z-index: -1;
  background-image: linear-gradient(var(--tx-sheet), var(--tx-sheet)), var(--tx-glow);
  background-color: var(--tx-canvas); background-attachment: fixed; pointer-events: none;
}
.st-key-gate {
  position: relative;
  background: #FFFFFF; border: 1px solid var(--tx-border); border-radius: 18px; box-shadow: var(--tx-shadow-float);
  padding: 10px 14px 10px 24px; gap: 10px; margin-top: 8px;
}
.st-key-gate::before { content: ""; position: absolute; left: 0; top: 14px; bottom: 14px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--tx-indigo); }
.st-key-gate [data-testid="stMarkdownContainer"] p { margin: 0; font-size: $f17; line-height: 1.35;
  text-wrap: balance; }
.st-key-gate [data-testid="stAlert"] { margin: 0; }
.st-key-gate button { min-height: 42px; padding: 4px 18px; }
/* one row (about 64 px): "Step n of 6 is ready for review. Next: ..." on the left, the buttons on the right;
   when the row gets too narrow the buttons wrap under the text */
.st-key-gate-row { flex-wrap: wrap; align-items: center; column-gap: 16px; row-gap: 8px; }
.st-key-gate-row > [data-testid="stElementContainer"] { flex: 1 1 240px; min-width: 0; width: auto !important; }
.st-key-gate-row > [data-testid="stLayoutWrapper"] { flex: 0 0 auto; width: auto !important; margin-left: auto; }
.st-key-gate-row [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
.st-key-gate[data-stale="true"], [data-stale="true"] .st-key-gate,
[data-testid="stElementContainer"][data-stale="true"]:has(.st-key-gate) { display: none; }
.st-key-gate-step { background: var(--tx-card); border: 1px solid var(--tx-border); border-radius: 20px;
  box-shadow: var(--tx-shadow-card); padding: 20px 24px; }

/* ---------------------------------------------------------------- cards (views.py containers) */
[class*="st-key-card-"], [class*="st-key-live-"], .st-key-finale, .st-key-recap {
  background: var(--tx-card); border: 1px solid var(--tx-border); border-radius: 20px;
  box-shadow: var(--tx-shadow-card); padding: 24px; gap: 16px;
}
[class*="st-key-live-"] { position: relative; }
[class*="st-key-live-"]::before { content: ""; position: absolute; left: 0; top: 20px; bottom: 20px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--tx-indigo); }
[class*="st-key-card-"] h3, [class*="st-key-live-"] h3 { font-size: $f24; line-height: $f32; font-weight: 600; }
[class*="st-key-kpis-"] { gap: 16px; }
[class*="st-key-kpis-"] [data-testid="stMetric"], .st-key-finale [data-testid="stMetric"] {
  background: #F7F7FB; border-radius: 16px; padding: 14px 18px; height: 100%;
}
[class*="st-key-bridge-"] {
  position: relative; background: var(--tx-indigo-tint); border-radius: 16px; padding: 14px 20px 14px 23px; gap: 4px;
}
[class*="st-key-bridge-"]::before { content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--tx-indigo); }
[class*="st-key-bridge-"] p { color: var(--tx-text); font-size: $f16; margin: 0; }
[class*="st-key-bridge-"] strong { color: var(--tx-indigo-pressed); font-weight: 600; }
.st-key-finale { padding: 32px; }
.st-key-recap h3 { font-size: $f28; line-height: $f36; font-weight: 700; margin: 0; }
.tx-recap-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.tx-pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px;
  font-size: $f14; font-weight: 600; background: var(--tx-success-bg); color: var(--tx-success); }

/* bordered containers (st.container(border=True)) inside the transcript read as cards */
[data-testid="stMain"] [data-testid="stVerticalBlock"][class*="BorderWrapper"],
[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--tx-card); border-color: var(--tx-border); border-radius: 20px; box-shadow: var(--tx-shadow-card);
}

/* ---------------------------------------------------------------- widgets */
[data-testid="stMetricLabel"] p { font-size: $f15; color: var(--tx-text2); font-weight: 400; }
[data-testid="stMetricValue"] { font-size: $f36; font-weight: 600; color: var(--tx-text); line-height: 1.25; }
[data-testid="stMetricDelta"] { font-size: $f15; font-weight: 600; }
[data-testid="stExpander"] details { border: 1px solid var(--tx-border); border-radius: 12px; background: #FFFFFF; }
[data-testid="stExpander"] summary { padding: 12px 16px; border-radius: 12px; }
[data-testid="stExpander"] summary:hover { background: #FAFAFA; }
[data-testid="stExpander"] summary p { font-weight: 600; font-size: $f16; }
[data-testid="stExpanderDetails"] { padding: 4px 16px 16px; }
[data-testid="stTabs"] [role="tablist"] { gap: 20px; }
[data-testid="stTabs"] [role="tab"] { padding: 8px 2px; }
[data-testid="stTabs"] [role="tab"] p { font-size: $f16; font-weight: 600; color: var(--tx-text2); }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] p { color: var(--tx-text); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background: var(--tx-indigo); height: 3px; border-radius: 3px; }
[data-testid="stTabs"] [data-baseweb="tab-border"] { background: var(--tx-border); height: 1px; }
[data-testid="stDataFrame"] { border: 1px solid var(--tx-border); border-radius: 12px; overflow: hidden; }
[data-testid="stTable"] table, [data-testid="stMarkdownContainer"] table {
  border-collapse: separate; border-spacing: 0; border: 1px solid var(--tx-border); border-radius: 12px;
  overflow: hidden; font-size: $f16; background: #FFFFFF;
}
[data-testid="stTable"] th, [data-testid="stMarkdownContainer"] th {
  background: #F5F5F7; color: #424242; font-weight: 600; font-size: $f16; text-align: left;
  border: none; border-bottom: 1px solid var(--tx-border); padding: 8px 12px;
}
[data-testid="stTable"] td, [data-testid="stMarkdownContainer"] td {
  font-size: $f16; height: 40px; padding: 6px 12px; border: none; border-bottom: 1px solid #EDEDED; color: var(--tx-text);
}
[data-testid="stTable"] tr:last-child td, [data-testid="stMarkdownContainer"] tr:last-child td { border-bottom: none; }
[data-testid="stBadge"], .stBadge, [data-testid="stMarkdownBadge"] span, span[class*="stMarkdownBadge"] {
  font-size: $f14; font-weight: 600; border-radius: 999px; padding: 2px 10px;
}
[data-testid="stAlert"] { border-radius: 12px; }
[data-testid="stAlertContainer"] { border-radius: 12px; }
[data-testid="stCode"] pre, [data-testid="stCode"] { border-radius: 12px; }
[data-testid="stChatInput"] {
  background: #FFFFFF; border: 1px solid var(--tx-border); border-radius: 24px; box-shadow: 0 4px 20px rgba(36,36,36,.10);
}
[data-testid="stChatInput"] textarea { font-size: $f17; }
[data-testid="stChatInput"]:focus-within { border-color: var(--tx-indigo); }
/* st.dialog: role="dialog" sits on a transparent section element; the white box is its parent div */
[data-testid="stDialog"] :has(> [role="dialog"]) { border-radius: 20px; background: #FFFFFF;
  box-shadow: 0 16px 48px rgba(36,36,36,.18); border: 1px solid var(--tx-border); }
[role="dialog"] h2 { font-size: $f24; font-weight: 600; }
[role="dialog"] [data-testid="stForm"] { padding: 0; border: none; }
[role="dialog"] [data-testid="stTextArea"] textarea { font-size: $f17; }
[data-testid="stToast"] { border-radius: 12px; background: #FFFFFF; box-shadow: var(--tx-shadow-float); }
</style>
""")

_SIZES = {f"f{px}": rem(px) for px in (14, 15, 16, 17, 18, 24, 28, 32, 36, 40, 48)}
CSS = _CSS.substitute(**TOKENS, **_SIZES)

# Presenter mode (?presenter=1): type +20% through the one CSS variable every rem size hangs off.
PRESENTER_CSS = """<style>
:root { --tx-scale: 1.2; --tx-chat-h: 112px; --tx-top-offset: 100px; --tx-band-h: 64px;
  --tx-sheet: rgba(255,255,255,.9); }
/* near-opaque sheet without blur: spares the GPU */
[data-testid="stMainBlockContainer"] { -webkit-backdrop-filter: none; backdrop-filter: none; margin-top: 76px; }
/* the header band: opaque canvas (glow included), so nothing scrolls visibly under the progress strip */
[data-testid="stHeader"] { height: var(--tx-band-h); min-height: var(--tx-band-h);
  background-image: var(--tx-glow); background-color: var(--tx-canvas); background-attachment: fixed; }
[data-testid="stHeader"]::after { content: ""; position: absolute; left: 0; right: 0; top: 100%; height: 12px;
  background-image: var(--tx-glow); background-color: var(--tx-canvas); background-attachment: fixed;
  -webkit-mask-image: linear-gradient(#000, transparent); mask-image: linear-gradient(#000, transparent);
  pointer-events: none; }
.tx-strip { position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 1000001; padding: 6px 18px;
  background: #FFFFFF; border: 1px solid var(--tx-border); border-radius: 999px;
  box-shadow: var(--tx-shadow-card); flex-wrap: nowrap; }
</style>"""


def style_text(css: str) -> str:
    """What sits between <style> and </style>. st.html runs DOMPurify, which drops a whole style element whose text
    looks like markup, so this must never contain a "<" (tests/test_ui.py checks it)."""
    return css.split("<style>", 1)[-1].rsplit("</style>", 1)[0]


def css_block(extra: str) -> str:
    """views.CSS may come with or without <style> tags."""
    extra = (extra or "").strip()
    return extra if not extra or extra.lstrip().startswith("<style") else f"<style>\n{extra}\n</style>"


def ms(symbol: str) -> str:
    """A Material Symbols glyph for raw HTML (markdown uses :material/<symbol>: instead)."""
    return f'<span class="tx-ms" aria-hidden="true">{escape(symbol)}</span>'


def brand_html() -> str:
    return '<div class="tx-brand">Transform<span class="tx-x">×</span>AgentCore workshop</div>'


def nav_html(rows: list[tuple[str, str, str, bool, str]]) -> str:
    """rows: (stage, status, model id, active, anchor or "") in pipeline order: the sidebar's named-agent nav."""
    html = ['<div class="tx-nav-h">Agents</div>']
    for stage, status, model, active, anchor in rows:
        meta = STAGE_META[stage]
        cls = f"tx-nav tx-{status}{' tx-active' if active else ''}"
        tip = escape(f"{meta.title} · model {model}", quote=True)
        body = (f'{ms(meta.symbol)}<div><div class="tx-nav-name">{escape(meta.short)}</div>'
                f'<div class="tx-nav-meta"><span class="tx-dot"></span><span class="tx-st">'
                f'{STATUS_WORDS.get(status, status)}</span><span class="tx-sep">·</span>'
                f'<span>{escape(friendly_model(model))}</span>'
                '</div></div>')
        if anchor:  # NAV_JS scrolls to the stage's latest card (a keyed container has a class, not an id)
            html.append(f'<a class="{cls}" href="#" data-tx-target="st-key-{escape(anchor)}" title="{tip}" '
                        f'target="_self">{body}</a>')
        else:
            html.append(f'<div class="{cls}" title="{tip}">{body}</div>')
    return "".join(html)


def presenter_html(name: str) -> str:
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    return (f'<div class="tx-presenter"><span class="tx-initials">{escape(initials)}</span>'
            f'<span>{escape(name)}</span></div>')


def progress_html(rows: list[tuple[str, str]]) -> str:
    """rows: (stage, approved | current | failed | pending): the compact strip presenter mode shows in the top bar."""
    parts = []
    for i, (stage, state) in enumerate(rows):
        if i:
            parts.append(f'<span class="tx-l{" tx-done" if rows[i - 1][1] == "approved" else ""}"></span>')
        parts.append(f'<span class="tx-s tx-{state}"><span class="tx-c"></span>{escape(STAGE_META[stage].short)}</span>')
    return f'<div class="tx-strip" role="list" aria-label="Migration progress">{"".join(parts)}</div>'


def suggestion_html(symbol: str, sentence: str, caption: str) -> str:
    """sentence may contain <b>…</b> for the indigo entity; everything else is escaped by the caller."""
    return (f'<div class="tx-sg-icon">{ms(symbol)}</div><div class="tx-sg-text">{sentence}</div>'
            f'<div class="tx-sg-cap">{escape(caption)}</div>')


def agents_html() -> str:
    rows = []
    for n, meta in enumerate(STAGE_META.values(), start=1):
        rows.append(f'<div class="tx-agent"><div class="tx-sg-icon">{ms(meta.symbol)}</div><div>'
                    f'<div class="tx-agent-title">{n} · {escape(meta.title)}</div>'
                    f'<div class="tx-agent-does">{escape(meta.does)}.</div>'
                    f'<div class="tx-agent-an">In AWS Transform: {escape(meta.analogue)}</div></div></div>')
    return "".join(rows)


# The "before" picture (legacy-infra/README.md, condensed): what the agents will discover and migrate.
LEGACY_ESTATE = [
    ("dns", "One EC2 instance, set up by hand",
     "t3.large, Amazon Linux 2023, gp2 volume, one subnet in a single AZ. No load balancer, no Auto Scaling."),
    ("code", "A Flask monolith from 2017",
     "nginx in front of the Flask dev server: a JSON API for the warehouse scanners and a small dashboard."),
    ("database", "Everything on the box",
     "SQLite on the root volume, CSV reports from a nightly cron job, logs in a local file."),
]
LEGACY_SMELLS = ["Hard-coded secrets (admin123)", "SQL injection in search", "Flask dev server as root",
                 "SQLite on local disk, no backups", "No health check, single AZ", "Oversized t3.large on gp2"]


def legacy_html() -> str:
    """The legacy-estate dialog: three facts and the headline smells (11 deliberate ones, 5 more for keen eyes)."""
    rows = [f'<div class="tx-agent"><div class="tx-sg-icon">{ms(symbol)}</div><div>'
            f'<div class="tx-agent-title">{escape(title)}</div><div class="tx-agent-does">{escape(text)}</div>'
            '</div></div>' for symbol, title, text in LEGACY_ESTATE]
    smells = "".join(f'<li>{ms("warning")}{escape(smell)}</li>' for smell in LEGACY_SMELLS)
    return ("".join(rows) + '<div class="tx-legacy-h">Planted for the agents to find</div>'
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
    // the lowest viewport y a result may reach: the top of the sticky gate (it sits on the chat bar)
    function floor(view) {
      var y = Math.min(box(view).bottom, innerHeight);
      var bar = document.querySelector('[data-testid="stBottom"]');
      if (bar && box(bar).height) { y = Math.min(y, box(bar).top); }
      var gate = document.querySelector(".st-key-gate");
      if (gate) { y -= box(gate).height; }
      return y - 12;
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
      var off = rootPx("--tx-top-offset", 72), align = at(el) - off;
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
      var kpis = el.querySelector('[class*="st-key-kpis-"]'), lead = el.querySelector(".tx2-headline");
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

# Focus the "Ask for changes" textarea when the dialog opens, so the presenter can type straight away. It retries for
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
