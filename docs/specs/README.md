# Specs

These are the maintained specifications of the workshop. Each one covers one area and states its **invariants** (what a
change must never break), **how it works**, **change recipes** for the common changes, **verification** steps, the
**time-sensitive facts** that will go stale, and **known debt**.

Start with [MAINTAINING.md](../../MAINTAINING.md) for the routines and the change checklist. AI coding agents also read
[CLAUDE.md](../../CLAUDE.md).

| Spec | Covers | Read it before you… |
|---|---|---|
| [01-architecture.md](01-architecture.md) | Components, the request/event contract, stage data flow, run persistence, clients (Local, AgentCore, Replay), deployment topology | change a contract field, an option, the pipeline or a client |
| [02-agents.md](02-agents.md) | The shared core and the six stage agents: tools, knowledge, budgets, fallbacks, models | change an agent, a prompt, a model, a blueprint or the price snapshot, or add a stage |
| [03-ui.md](03-ui.md) | The Streamlit UI and its design system: screens, states, tokens, light and dark themes, presenter mode | touch anything under `ui/` or `docs/design/` |
| [04-operations.md](04-operations.md) | Environments, AWS, deployment, costs, security, version pins, troubleshooting, release checklist | deploy, upgrade a dependency, or prepare a talk |
| [05-demo-content.md](05-demo-content.md) | The content the talk depends on (script, numbers, recording, planted finding, sample inputs) and its expiry calendar | change numbers, re-record, or present after time has passed |

Other reference documents:

| Document | Role |
|---|---|
| [docs/CONTRACTS.md](../CONTRACTS.md) | The normative per-stage inputs, outputs and options |
| [docs/design/](../design/) | The approved mockups (light and dark) and [GUIDELINES.md](../design/GUIDELINES.md) (capitalization and numbers) |
| [WORKSHOP.md](../../WORKSHOP.md) | The presenter's runbook (setup, deploy, rehearse, run of show, plan B, teardown) |
| [docs/KEYNOTE-SCRIPT.md](../KEYNOTE-SCRIPT.md) | The spoken script and the numbers cheat sheet |
| [docs/PLAN.md](../PLAN.md) | Historical build plan and the original model benchmark (not maintained) |

Writing rules for specs:
- **Keep the "Last verified" date honest.** Bump it only after you've checked the spec against the code.
- **Write invariants as MUST / MUST NOT.** If a change needs to break one, update the spec in the same change and say why.
- **Reference files with relative links.** Point at function names rather than line numbers, which drift.
