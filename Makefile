# Transform × AgentCore workshop. `make` (or `make help`) lists every target.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# AWS_PROFILE is passed through only when you set it (env or `make ... AWS_PROFILE=x`); otherwise boto3 and the
# aws CLI use the default credential chain (env vars, SSO default profile, instance role).
AWS_REGION ?= us-west-2
export AWS_REGION
ifdef AWS_PROFILE
export AWS_PROFILE
endif

UV ?= uv
UI_PORT ?= 8501
REQ ?= samples/requirements/migration-requirements.md
# MODE: local | agentcore | replay | fake.
# REPLAY_FROM: recorded (samples/recorded-run) | latest (newest complete run in runs/) | fixtures | runs/<id>.
# AGENTS: a subset for deploy-agents / destroy-agents, e.g. AGENTS="hello discovery".
# RUN: the run directory for `make record`.  CLEAN_RUNS=1 lets `make clean` delete runs/.
MODE ?= local
REPLAY_FROM ?= recorded
AGENTS ?=
RUN ?=
CLEAN_RUNS ?=

# Run a script written by another workstream, with a clear message when it is not there yet.
define run_script
	@if [ -f "$(1)" ]; then bash "$(1)" $(2); else echo "✗ $(1) not found"; exit 1; fi
endef

.PHONY: help install test test-live lint ui run-cli replay record legacy-up legacy-down kind-up kind-down tools \
        deploy-agents destroy-agents preflight clean

help: ## List the targets
	@echo "Transform × AgentCore workshop  (AWS_PROFILE=$(or $(AWS_PROFILE),<default chain>) AWS_REGION=$(AWS_REGION))"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install Python dependencies (uv sync --all-extras)
	$(UV) sync --all-extras

test: ## Unit tests (Bedrock mocked, no AWS calls)
	$(UV) run pytest -q

test-live: ## Live smoke tests against Bedrock (costs cents)
	$(UV) run pytest -q -m live

lint: ## Ruff lint
	$(UV) run ruff check .

ui: ## Streamlit chatbot on http://localhost:8501 (override with UI_PORT=...)
	$(UV) run streamlit run ui/app.py --server.port $(UI_PORT)

run-cli: ## Headless pipeline with auto-approve (MODE=local|agentcore|replay|fake, REQ=file)
	$(UV) run python -m transform_agents.orchestrator --requirements $(REQ) --mode $(MODE) --auto-approve

replay: ## Replay a recorded run headless (REPLAY_FROM=recorded|latest|fixtures|runs/<id>)
	$(UV) run python -m transform_agents.orchestrator --requirements $(REQ) --mode replay \
		--replay-from $(REPLAY_FROM) --auto-approve

record: ## Copy a complete run into samples/recorded-run for replay (RUN=runs/<id>)
	@if [ -z "$(RUN)" ]; then echo "usage: make record RUN=runs/<id>"; exit 2; fi
	$(UV) run python -m transform_agents.orchestrator.record $(RUN)

legacy-up: ## Deploy the legacy EC2 estate (creates AWS resources)
	$(call run_script,scripts/legacy-up.sh)

legacy-down: ## Destroy the legacy EC2 estate
	$(call run_script,scripts/legacy-down.sh)

kind-up: ## Create the local kind cluster
	$(call run_script,scripts/kind-up.sh)

kind-down: ## Delete the local kind cluster
	$(call run_script,scripts/kind-down.sh)

tools: ## Install repo-local kind and kubeconform into .tools/bin (terraform: install it yourself)
	$(call run_script,scripts/install-tools.sh)

deploy-agents: ## Deploy the agents to AgentCore Runtime (AGENTS="hello discovery" for a subset)
	$(call run_script,deploy/deploy.sh,$(AGENTS))

destroy-agents: ## Remove the agents from AgentCore Runtime
	$(call run_script,deploy/destroy.sh,$(AGENTS))

preflight: ## Pre-stage checks (credentials, Bedrock models, docker, kind, terraform, warm-ups)
	@bash scripts/preflight.sh

clean: ## Delete caches (runs/ too only with CLEAN_RUNS=1)
	rm -rf .pytest_cache .ruff_cache
	find . -path ./.venv -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +
	@if [ "$(CLEAN_RUNS)" = "1" ]; then rm -rf runs && echo "deleted runs/"; \
	else echo "kept runs/ (recorded runs are the offline fallback; CLEAN_RUNS=1 deletes them)"; fi
