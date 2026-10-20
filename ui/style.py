"""Look and feel: stage metadata, the header banner, the sidebar stepper and a little CSS."""

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True)
class StageMeta:
    icon: str
    title: str
    does: str
    analogue: str


STAGE_META = {
    "discovery": StageMeta("🔎", "Discovery & Assessment", "Inventory the EC2 box and the Flask code, find risks, pick a 6R strategy",
                           "Discovery / assessment"),
    "requirements": StageMeta("📋", "Requirements", "Turn the uploaded request into a traceable migration spec",
                              "Job objective intake + HITL clarifications"),
    "modernization": StageMeta("🧩", "Code Modernization", "Refactor the monolith into a microservice, Dockerfile and manifests",
                               "Code transformation + containerization"),
    "terraform": StageMeta("🏗️", "Terraform IaC", "Adapt a validated EKS blueprint: VPC, EKS, ECR, RDS",
                           "Network / infrastructure conversion"),
    "k8s_validation": StageMeta("☸️", "K8s Validation", "Schema and policy checks, auto-fixes, optional live deploy to kind",
                                "Test & validation"),
    "finops": StageMeta("💰", "FinOps Right-sizing", "Current vs target monthly cost, right-sizing and savings",
                        "Assessment TCO / business case"),
}

STATUS_LABEL = {"pending": "pending", "running": "running", "awaiting_approval": "review", "approved": "approved",
                "failed": "failed"}

CSS = """
<style>
.block-container { padding-top: 3.2rem; max-width: 1180px; }
.tx-banner { background: linear-gradient(120deg, #232F3E 0%, #0F1B2A 60%, #16263F 100%);
  border: 1px solid #24344D; border-bottom: 3px solid #FF9900; border-radius: 14px;
  padding: 18px 24px 16px; margin-bottom: 14px; }
.tx-title { font-size: 1.7rem; font-weight: 700; color: #FFFFFF; letter-spacing: .2px; line-height: 1.25; }
.tx-title .tx-x { color: #FF9900; margin: 0 .4rem; }
.tx-sub { color: #AAB7C4; font-size: .92rem; margin-top: 6px; }
.tx-chip { display: inline-block; padding: 1px 10px; border-radius: 999px; margin: 8px 6px 0 0; font-size: .76rem;
  background: rgba(255,153,0,.10); color: #FFB547; border: 1px solid rgba(255,153,0,.35); }
.tx-step { display: flex; gap: 10px; align-items: flex-start; padding: 7px 10px; margin-bottom: 6px;
  border-radius: 10px; border-left: 3px solid #3A4A60; background: rgba(255,255,255,.025); }
.tx-step.tx-running { border-left-color: #3B82F6; background: rgba(59,130,246,.09); }
.tx-step.tx-awaiting_approval { border-left-color: #FF9900; background: rgba(255,153,0,.09); }
.tx-step.tx-approved { border-left-color: #2EA043; }
.tx-step.tx-failed { border-left-color: #E5484D; background: rgba(229,72,77,.09); }
.tx-step-icon { font-size: 1.2rem; line-height: 1.5rem; }
.tx-running .tx-step-icon { animation: tx-pulse 1.2s ease-in-out infinite; }
@keyframes tx-pulse { 50% { opacity: .3; } }
.tx-step-title { font-weight: 600; font-size: .88rem; color: #E6EDF3; }
.tx-step-model { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .7rem; color: #8B9BB0;
  word-break: break-all; }
.tx-badge { font-size: .64rem; padding: 1px 7px; border-radius: 999px; margin-left: 6px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .4px; white-space: nowrap; }
.tx-badge-pending { background: #243145; color: #9AA7B8; }
.tx-badge-running { background: rgba(59,130,246,.22); color: #8AB8FF; }
.tx-badge-awaiting_approval { background: rgba(255,153,0,.22); color: #FFB547; }
.tx-badge-approved { background: rgba(46,160,67,.22); color: #56D364; }
.tx-badge-failed { background: rgba(229,72,77,.22); color: #FF8589; }
.tx-live { color: #8AB8FF; font-size: .9rem; }
.tx-done { color: #56D364; font-size: .9rem; }
.tx-fail { color: #FF8589; font-size: .9rem; }
[data-testid="stChatMessage"] { background: rgba(255,255,255,.015); border-radius: 12px; }
[data-testid="stMetricValue"] { font-size: 1.35rem; }
</style>
"""


def banner_html() -> str:
    chips = ["Strands Agents", "Amazon Nova · DeepSeek · Mistral on Bedrock", "AgentCore Runtime",
             "EC2 monolith → EKS microservice"]
    return (
        '<div class="tx-banner">'
        '<div class="tx-title">AWS Transform<span class="tx-x">×</span>Amazon Bedrock AgentCore</div>'
        '<div class="tx-sub">Six migration agents, a human approval gate after every step.</div>'
        + "".join(f'<span class="tx-chip">{escape(c)}</span>' for c in chips)
        + "</div>"
    )


def stepper_html(rows: list[tuple[str, str, str]]) -> str:
    """rows: (stage, status, model id) in pipeline order."""
    html = []
    for n, (stage, status, model) in enumerate(rows, start=1):
        meta = STAGE_META[stage]
        html.append(
            f'<div class="tx-step tx-{status}"><div class="tx-step-icon">{meta.icon}</div><div>'
            f'<div class="tx-step-title">{n} · {escape(meta.title)}'
            f'<span class="tx-badge tx-badge-{status}">{STATUS_LABEL.get(status, status)}</span></div>'
            f'<div class="tx-step-model">{escape(model)}</div></div></div>'
        )
    return "".join(html)
