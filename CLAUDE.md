# CLAUDE.md

Guidance for AI coding agents working in this repo. Read [MAINTAINING.md](MAINTAINING.md) for the routines and the
change checklist, and the spec for the area you touch ([docs/specs/](docs/specs/)).

## What this is

A 60-minute conference workshop. Six Strands agents (discovery, requirements, modernization, terraform, k8s_validation,
finops) on Amazon Bedrock models take a legacy Flask monolith on EC2 to a microservice on Kubernetes. A Streamlit UI
gates every step with human approval. The same agent code runs in-process and on Amazon Bedrock AgentCore Runtime.
Stability on stage matters more than features.

## Commands

```bash
uv run pytest -q                      # unit tests (~410), no AWS; must stay green
uv run ruff check .                   # lint; must stay clean
uv run pytest -q -m live tests/test_<stage>*.py   # live Bedrock test for one stage (costs cents)
make preflight                        # environment check (SSO, models, Docker, kind, caches)
make ui                               # Streamlit UI (?presenter=1, ?theme=dark, ?run=<id>)
make replay REPLAY_SPEED=8            # replay the tracked recording
make numbers                          # numbers the talk quotes, from the recording
```

AWS: commands use `AWS_PROFILE` and `AWS_REGION` (us-west-2). If a call fails with an expired token, ask the human to
run `aws sso login`; never try to work around it.

## Map

- `transform_agents/core/`: contract (`AgentRequest`, events), `Workspace` + tools, `run_agent`, `make_app`, models in `config.py`
- `transform_agents/<stage>/`: `agent.py` (prompt + loop), `tools.py`, `knowledge/` (blueprints, rules, prices), `runtime.py`
- `transform_agents/orchestrator/`: `Pipeline` (gates, persistence in `runs/`), clients (Local, AgentCore, Replay, Fake), `record`, CLI
- `ui/`: `app.py` (shell), `views.py` (cards), `style.py` (tokens, CSS, title case), `bridge.py` (background jobs)
- `deploy/`: AgentCore CLI project and scripts · `legacy-infra/`: the "before" estate · `scripts/`: kind, tools, preflight, legacy
- `docs/CONTRACTS.md` (normative) · `docs/design/` (mockups and GUIDELINES) · `docs/specs/` (maintained specs) · `docs/PLAN.md` (historical)

## Hard rules

1. **Never create, modify or delete AWS resources** without the human's explicit approval in the current
   conversation. That means no `scripts/legacy-up.sh`, `deploy/deploy.sh`, `deploy/destroy.sh`, `terraform apply` or
   `cdk deploy`. Read-only calls and Bedrock invocations for tests are fine.
2. **Model families:** only Amazon Nova, Mistral, DeepSeek, Meta Llama 3.x and Amazon Titan via Bedrock Converse. Don't
   switch to Anthropic or OpenAI models; it's a deliberate user decision.
3. **Agents:** adapt curated `knowledge/` and loop on deterministic validators. Numbers come from tools, never from the
   model. No forced `tool_choice`, no Strands `structured_output`. Every `handle()` ends with exactly one `result` or
   `error`. Every subprocess has a timeout, and no `shell=True` with model input. Keep the time budgets.
4. **Contracts:** change [docs/CONTRACTS.md](docs/CONTRACTS.md) together with any change to stage inputs, outputs or
   options, and update the consumers, fixtures and recording.
5. **UI:**
   - Match the mockups in `docs/design/` and follow GUIDELINES.md: Title Case for labels that name things, sentence
     case for sentences; Inter weights 300–600 only; hero numbers in Light 300.
   - No emojis, no orange, no Microsoft branding. Every colour is a token, in both light and dark themes.
   - Never write "<" followed by a letter inside CSS text or comments: Streamlit's HTML sanitizer drops the whole style
     block, and a test guards this.
   - Icon-only buttons must be exactly centred.
6. **Never restart or kill a UI server the human is using.** Check `runs/*/state.json` for a stage in `running` first;
   prefer starting your own server on a free port. Streamlit doesn't hot-reload CSS from imported modules, so a restart
   is needed to see style changes.
7. **Demo content:** after changes that alter agent output, re-record (`make record RUN=…`) and sync the numbers into
   `docs/KEYNOTE-SCRIPT.md` and `WORKSHOP.md` §5 (`make numbers`). Keep the planted Kubernetes 1.33 finding unless the
   human decides otherwise ([05-demo-content.md](docs/specs/05-demo-content.md)).
8. **Keep it publishable:** no account ids, personal paths or secrets in tracked files. Don't commit `runs/`, `.tools/`,
   deploy build output or Terraform state.
9. **Git:** work on a branch; commit or push only when the human asks.

## Definition of done

Tests and lint pass. Live tests for changed agents pass at least twice. UI changes are checked in light and dark (and in
presenter mode) at 4K density. The specs and docs that describe the change are updated. See the full checklist in
[MAINTAINING.md](MAINTAINING.md#change-checklist-definition-of-done).
