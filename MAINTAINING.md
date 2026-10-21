# Maintaining the workshop

This repo holds a live, 60-minute conference demo. Six Strands agents on Amazon Bedrock AgentCore re-create the loop AWS
Transform's migration agents run, driven from a Streamlit chatbot with a human approval gate after every step. Keeping it
healthy means three things: the **code keeps working**, the **demo content stays true** (numbers, recording, expiring
AWS facts), and the **look stays as approved**.

| Area | Spec |
|---|---|
| System, contracts, pipeline, clients, deployment topology | [docs/specs/01-architecture.md](docs/specs/01-architecture.md) |
| The six agents, models, knowledge, budgets | [docs/specs/02-agents.md](docs/specs/02-agents.md) |
| UI and design system (light and dark) | [docs/specs/03-ui.md](docs/specs/03-ui.md) |
| Operations: AWS, deploy, costs, security, versions | [docs/specs/04-operations.md](docs/specs/04-operations.md) |
| Demo content and its expiry calendar | [docs/specs/05-demo-content.md](docs/specs/05-demo-content.md) |

Presenter-facing docs: [WORKSHOP.md](WORKSHOP.md) (runbook) and [docs/KEYNOTE-SCRIPT.md](docs/KEYNOTE-SCRIPT.md) (script).

## Golden rules

1. **Never create AWS resources without a human's explicit go-ahead.** That covers `scripts/legacy-up.sh`,
   `deploy/deploy.sh` and `deploy/destroy.sh`. Everything else is local or read-only.
2. **Contracts first.** [docs/CONTRACTS.md](docs/CONTRACTS.md) is normative. Change it in the same commit as the code
   that changes a stage's inputs, outputs or options.
3. **Blueprints and validators, not free-form generation.** Agents adapt curated knowledge and loop on deterministic
   checks. The model decides and explains; Python counts.
4. **Model families are a user decision:** Amazon Nova, Mistral, DeepSeek, Meta Llama 3.x and Amazon Titan via Bedrock
   Converse. Change models only with the benchmark procedure in [02-agents.md](docs/specs/02-agents.md).
5. **The approved look is the mockups** in [docs/design/](docs/design/), with
   [GUIDELINES.md](docs/design/GUIDELINES.md) for capitalization and numbers. Change a mockup first, then the code.
6. **Script = screen = recording.** After anything that changes agent output, re-record and update the numbers
   ([05-demo-content.md](docs/specs/05-demo-content.md)).
7. **Light and dark stay in lockstep.** Every colour is a token. A change to one theme must not alter the other.
8. **Honesty on stage:** the demo is a re-creation, not AWS Transform. Simulated drift and replays are labelled. Don't
   add unverifiable claims about AWS Transform.
9. **Keep the repo publishable:** no account ids, personal paths, secrets or `.tfvars` in tracked files.
10. **Leave it green:** `uv run pytest -q` and `uv run ruff check .` pass on every commit.

## Routines

**Before every talk**
- T-7 days: `make preflight`; deploy the agents if AgentCore mode is part of the talk (`deploy/deploy.sh`, on a good
  network); run the full pipeline once; check the expiry calendar
  ([05-demo-content.md §5](docs/specs/05-demo-content.md#5-expiry-calendar)).
- T-1 day: deploy the legacy EC2 and generate load; rehearse in the UI in presenter mode; re-record with
  `--live-aws`; `make numbers`; update the cheat sheet; re-verify the AWS Transform capability lines in the script.
- T-1 hour: [WORKSHOP.md §6](WORKSHOP.md#6-day-of-checklist).
- After the talk: `scripts/legacy-down.sh`, `deploy/destroy.sh` (if deployed), `make kind-down`. Tag the commit you
  presented, e.g. `git tag talk-2026-11-05`.

**Monthly** (or before reusing the demo after a gap)
- `make preflight`. It catches expired SSO, models that are no longer enabled, Docker, kind and cold caches.
- `make test` and one full local pipeline run. Compare against `make numbers`; small drift is normal, big drift means
  something changed.
- Walk the expiry calendar.

**Quarterly**
- Refresh the price snapshot (`uv run python transform_agents/finops/knowledge/refresh_prices.py`), then update the
  finops tests' pinned date if needed, re-record, and update the numbers.
- Check the Terraform module versions (`extract_module_inputs.py`) and the EKS version support dates.
- Dependency review: `uv lock --upgrade` in a branch; bump the AgentCore CLI, kind and the vendored binaries **one at a
  time**, with the verification in [04-operations.md](docs/specs/04-operations.md).
- Update `discovery/codescan.py` `LATEST` (the "outdated framework" finding depends on it).

**When AWS changes something**

| Event | Do |
|---|---|
| A Bedrock model is retired or newly enabled | `make preflight`; re-benchmark per [02-agents.md](docs/specs/02-agents.md); update `core/config.py`, the PLAN record and the cheat sheet |
| An EKS version changes support status | Check the planted storyline ([05-demo-content.md §4](docs/specs/05-demo-content.md#4-the-planted-finding-kubernetes-133)); refresh prices |
| A new AgentCore CLI or Runtime release | Read its changelog; upgrade on a branch; `DRY_RUN=1 deploy/deploy.sh`; smoke test with `deploy/smoke-local.sh hello` |
| An AWS Transform launch or rename | Update KEYNOTE Part 1 and the "Real AWS Transform" lines, plus `STAGE_META.bridge` |

## Change checklist (definition of done)

- [ ] The relevant spec's invariants still hold; if one had to change, the spec was updated in this change.
- [ ] CONTRACTS.md matches any change to inputs, outputs or options; consumers and fixtures are updated.
- [ ] Unit tests were added or updated; `uv run pytest -q` and `uv run ruff check .` pass.
- [ ] Agent changes: the stage's live test passed at least twice (`uv run pytest -q -m live tests/test_<stage>*.py`).
- [ ] UI changes: checked in **light and dark**, and in presenter mode, at 4K density and at 1280×720; icon-only buttons
      are centred; no "<" followed by a letter in CSS.
- [ ] Demo-visible changes: re-recorded (`make record`), `make numbers` copied into the cheat sheet and WORKSHOP §5,
      and `make replay` still works.
- [ ] No AWS resources were created without approval; no secrets or personal identifiers were added.
- [ ] Docs updated: WORKSHOP.md for presenter-facing changes, specs for maintainer-facing ones, and the "Last verified"
      date bumped on any spec you re-checked.

## Quick commands

```bash
make install tools kind-up      # first time
make preflight                  # before anything important
make test                       # ~410 unit tests, no AWS
make test-live                  # 7 live tests against Bedrock (cents; k8s needs kind)
make lint
make ui                         # http://localhost:8501  (?presenter=1, ?theme=dark, ?run=<id>)
make run-cli                    # headless pipeline (MODE=local|agentcore|replay|fake)
make replay                     # the tracked recording (REPLAY_SPEED=4 for a quick look)
make record RUN=runs/<id>       # make a complete run the new recording
make numbers                    # print the numbers the talk quotes
```

## Known debt (summary)

Each item is detailed, with its fix, in the owning spec:
- Duplicated helpers (deadline wrappers, legacy loaders, model display names): [02](docs/specs/02-agents.md),
  [03](docs/specs/03-ui.md).
- requirements and terraform can end in an `error` rather than a deterministic fallback:
  [02](docs/specs/02-agents.md).
- FinOps results depend on the run date (EKS support status, `date.today()`):
  [05](docs/specs/05-demo-content.md).
- Declared but unused `strands-agents-tools`, and undeclared `altair`/`pandas`: [01](docs/specs/01-architecture.md).
- No regeneration script for the Material Symbols font subset; no committed 4K capture script:
  [03](docs/specs/03-ui.md).
- The tracked recording was made with Live AWS Discovery off: [05](docs/specs/05-demo-content.md).
