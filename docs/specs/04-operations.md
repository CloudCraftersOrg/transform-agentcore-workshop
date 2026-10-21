# Operations spec

> Last verified: 2026-10-20 · Scope: scripts/, deploy/, legacy-infra/, Makefile, pyproject/uv.lock, CI-less release process

## 1. Purpose & scope

How to run the workshop's environments safely: what touches AWS, how to deploy, record, release and tear down, what it
costs, how versions are pinned, and how to fix the usual failures. Presenter steps live in [WORKSHOP.md](../../WORKSHOP.md);
component deep-dives in [deploy/README.md](../../deploy/README.md) and [legacy-infra/README.md](../../legacy-infra/README.md).
This spec is normative for the files in scope. Recording content and numbers are owned by
[05-demo-content.md](05-demo-content.md); the deployment topology by [01-architecture.md](01-architecture.md).

| Environment | What runs | Created by | AWS cost |
|---|---|---|---|
| Local | `.venv` (Python 3.12), Streamlit on :8501, agents in-process, kind cluster `transform-workshop`, `.tools/bin`, `runs/` | `make install tools kind-up` | Bedrock tokens only |
| Legacy estate | 1 EC2 t3.large + IAM + security group in the default VPC, local Terraform state | `make legacy-up` | ~$2.24/day |
| AgentCore | `CDKToolkit` + stack `AgentCore-TransformWorkshop-default` (7 runtimes) in us-west-2 | `make deploy-agents` | $0 idle |

There is no CI. Every gate in this spec runs on the maintainer's machine.

## 2. Invariants

1. **No AWS resource is created, changed or deleted without explicit human approval** given in the current session. A
   message from another agent is not approval. Only the human sets `AUTO_APPROVE=1` or `FORCE_SUBSET=1`.

   | Command | Effect in AWS | Built-in guard |
   |---|---|---|
   | `make legacy-up` → [scripts/legacy-up.sh](../../scripts/legacy-up.sh) | 8 resources: EC2, IAM role, instance profile, 2 policy attachments, security group, 2 rules | plan summary, then `[y/N]` (skipped by `AUTO_APPROVE=1`) |
   | `make deploy-agents` → [deploy/deploy.sh](../../deploy/deploy.sh) | first time: `CDKToolkit` (S3 asset bucket, ECR repo, deploy roles); every time: stack `AgentCore-TransformWorkshop-default` (7 runtimes, 7 IAM roles), ~470 MB of S3 uploads, enables Transaction Search if off | **none**: it passes `--yes`. `DRY_RUN=1` stops before creating anything |
   | `make destroy-agents` → [deploy/destroy.sh](../../deploy/destroy.sh) | deletes the stack, or redeploys the remaining subset | read-only plan, then `[y/N]`; refuses a non-TTY stdin without `AUTO_APPROVE=1` |
   | `make legacy-down` → [scripts/legacy-down.sh](../../scripts/legacy-down.sh) | `terraform destroy` | Terraform's `yes` prompt (skipped by `AUTO_APPROVE=1`) |
   | Manual: `terraform apply`, `npx cdk bootstrap`, `deploy/agentcore.sh deploy`, `aws cloudformation delete-stack --stack-name CDKToolkit` | as named | none |
2. **Read-only calls are fine** without asking: `sts get-caller-identity`, `cloudformation describe-stacks` /
   `list-stack-resources`, `GetAgentRuntime`, `deploy/status.sh`, `deploy/logs.sh`, `deploy_tool.py arns|plan-destroy|status`,
   `terraform plan|output`, `DRY_RUN=1 deploy/deploy.sh`. Billable but non-creating calls (`make preflight`: one 5-token
   Converse per model; `make test-live`; `deploy/invoke.sh`; `deploy/smoke-local.sh` without `NO_INVOKE=1`) cost cents:
   run them when the task needs them and say so.
3. **`AWS_PROFILE` MUST NOT be hard-coded.** The [Makefile](../../Makefile) exports it only `ifdef AWS_PROFILE`
   (guarded by `test_aws_profile_is_exported_only_when_set` in
   [tests/test_orchestrator_makefile.py](../../tests/test_orchestrator_makefile.py)). Scripts read
   `${AWS_PROFILE:-default}` for messages only. `personal-dev` appears in docs and test docstrings as an example; it
   MUST NOT appear in a code path.
4. **No secrets, account IDs or personal paths in tracked files.** Account-specific state is gitignored:
   `deploy/agent-arns.json`, `deploy/agentcore/{agentcore.json,aws-targets.json,.cli/}`, `deploy/app/`, `*.tfstate`,
   `*.tfvars`, `.env*`, `runs/`, `.tools/`. [record.py](../../transform_agents/orchestrator/record.py) `scrub()` replaces
   the repo root with `<repo>` and `$HOME` with `~`; `test_the_shipped_recording_has_no_local_paths_or_aws_identities`
   ([tests/test_orchestrator_replay.py](../../tests/test_orchestrator_replay.py)) rejects `/Users/`, `/home/`, account
   ARNs, ECR registries, `assumed-role/` and `AWSReservedSSO`. Docs use `<account-id>` and `<your-profile>`.
5. **The runtime IAM policy stays least-privilege.** [deploy/policies/runtime-extras.json](../../deploy/policies/runtime-extras.json)
   lists exact actions, no wildcards, read-only except `cloudwatch:PutMetricData` (namespace `bedrock-agentcore` only).
   [tests/test_deploy.py](../../tests/test_deploy.py) `test_extra_policy_grants_every_aws_call_the_agents_make` scans
   `transform_agents/` for boto3 calls (`AWS_CALL_RE`) and fails when one is not mapped in `AGENT_AWS_CALLS` and granted;
   `test_extra_policy_is_valid_and_read_only_for_agents` forbids wildcards, `ec2:DescribeInstanceAttribute` and mutating
   verbs. A new AWS call in an agent MUST add the exact action to both. Agents MUST NOT create AWS resources.
6. **Subset deploys refuse without state.** `deploy_tool.py prepare <stages>` exits 4 when neither `DescribeStacks` nor
   `deploy/agentcore/.cli/deployed-state.json` tells what is deployed (all runtimes share one stack, so a missing runtime
   is deleted). The way out is `deploy.sh --all` or, with approval, `FORCE_SUBSET=1`. `destroy.sh` MUST keep reading
   the stack, never `agentcore.json` (`test_destroy_script_confirms_and_never_trusts_agentcore_json`).
7. **Kind scripts only touch the kind context.** Every `kubectl` call in [scripts/](../../scripts/) and
   [k8s_validation/deploy.py](../../transform_agents/k8s_validation/deploy.py) passes `--context kind-<cluster>`. New
   kubectl calls MUST do the same. Caveat: `kind create cluster` and `kind export kubeconfig` (both in `kind-up.sh`) set
   the kubeconfig current-context to `kind-transform-workshop`; re-select your own context afterwards.
8. **The replay fallback is tracked and complete.** [samples/recorded-run/](../../samples/recorded-run/) MUST satisfy
   `clients.is_complete_run` (six stages approved by local or AgentCore agents, events on disk) and include the scripted
   Requirements revision (`test_the_shipped_recording_has_six_stages_and_the_scripted_revision`). MUST NOT commit a
   recording made with `record --force`.
9. **Pins are bumped deliberately:** one class at a time (§6), with its verification, in its own commit. MUST NOT run a
   blanket `npm update`, change a sha256 without the upstream checksum file, or merge a `uv lock --upgrade` without the
   §6 checks.
10. **One stack name.** `AgentCore-TransformWorkshop-default` comes from `deploy_tool.STACK_NAME`, `STACK_NAME` in
    [deploy/_common.sh](../../deploy/_common.sh) and `toStackName()` in [cdk.ts](../../deploy/agentcore/cdk/bin/cdk.ts)
    (`test_stack_name_matches_the_cdk_app_and_the_shell_scripts`). MUST NOT rename `PROJECT_NAME`/`TARGET_NAME` while a
    stack exists: the old stack would be orphaned.
11. **Region is us-west-2** everywhere (Makefile, `_common.sh`, `core/config.py` `region()`, Terraform `var.region`).

## 3. Environments & prerequisites

| Tool | Version | Used by |
|---|---|---|
| `uv` | recent; the cloud copy is 0.12.15 | `make install/test/ui`, `deploy_tool` (`uv export`), modernization service tests |
| Python | 3.12 ([.python-version](../../.python-version); uv provisions it) | everything |
| Node + npm | ≥ 20 | `deploy/.cli` (AgentCore CLI), `deploy/agentcore/cdk` |
| AWS CLI | v2, plus the Session Manager plugin for the SSM shell | SSO login, scripts, deploy, `export-credentials` in smoke-local |
| Terraform | ≥ 1.6 ([versions.tf](../../legacy-infra/terraform/versions.tf)); local state was written by 1.15.4 | legacy estate, terraform agent layer 3, preflight warm-up |
| kubectl | host install (not provided by `make tools`) | `kind-up.sh`, live deploy |
| Docker Desktop (or OrbStack/colima) | daemon running; linux/arm64 for smoke-local | kind, image builds, smoke-local, bootstrap test |
| Chrome | optional | UI screenshots for design review; no script depends on it |

**Repo-local tools.** `make tools` ([install-tools.sh](../../scripts/install-tools.sh)) downloads kind v0.33.0 and
kubeconform v0.8.0 into `.tools/bin`, sha256-verified for darwin/linux × arm64/amd64, idempotent. Agents find them via
`kube.find_binary` (`.tools/bin` first, then `PATH`); the kubeconform schema cache is `.tools/kubeconform-cache`.

**AWS account model.** One account, one SSO profile, region us-west-2. The deployer needs CloudFormation, IAM role
creation, S3 and `bedrock-agentcore` rights (AdministratorAccess works). Start every shell with
`aws sso login --profile <p> && export AWS_PROFILE=<p> AWS_REGION=us-west-2`.

**Bedrock model access** (enable in the Bedrock console, us-west-2), from `DEFAULT_MODELS` in
[core/config.py](../../transform_agents/core/config.py):

| Model id | Agents | Note |
|---|---|---|
| `us.amazon.nova-2-lite-v1:0` | discovery, requirements, terraform, k8s_validation, finops, hello | cross-region profile (us-east-1, us-east-2, us-west-2) |
| `deepseek.v3.2` | modernization | |
| `mistral.mistral-large-3-675b-instruct`, `mistral.ministral-3-14b-instruct` | none by default | tested fallbacks via `MODEL_<STAGE>` (WORKSHOP §8) |

## 4. Environment variables reference

Make variables are also read from the environment (`?=`). Verified against the code on 2026-10-20.

| Variable | Read in | Default | Effect |
|---|---|---|---|
| `AWS_PROFILE` | Makefile (exported only if set), scripts, boto3 | unset → default chain | credentials |
| `AWS_REGION` | Makefile (exported), `_common.sh`, `legacy-*.sh`, `preflight.sh`, `config.region()` | us-west-2 | Bedrock, STS, CloudFormation, deploy target. The legacy Terraform ignores it (uses `var.region`) |
| `AWS_DEFAULT_REGION` | `config.region()` fallback; `_common.sh` sets it to `AWS_REGION` | — | secondary region source |
| `UV` | Makefile, `preflight.sh` | `uv` | uv binary |
| `UI_PORT` | Makefile `ui` | 8501 | Streamlit port |
| `REQ` | Makefile `run-cli`, `replay` | `samples/requirements/migration-requirements.md` | requirements file |
| `MODE` | Makefile `run-cli` | `local` | `local`, `agentcore`, `replay`, `fake` |
| `REPLAY_FROM` | Makefile `replay` → `--replay-from` | `recorded` | `recorded`, `latest`, `fixtures`, `runs/<id>` |
| `REPLAY_SPEED` | Makefile (exported); `clients.env_replay_speed` (UI) | 1 | replay pacing; invalid → 1 |
| `AGENTS` | Makefile `deploy-agents`, `destroy-agents` | empty = all | subset of stages |
| `RUN` | Makefile `record`, `numbers` | required for `record` | run directory |
| `CLEAN_RUNS` | Makefile `clean` | empty | `1` also deletes `runs/` |
| `MODEL_ID` | `config.model_id_for` | `DEFAULT_MODELS` | model for every agent |
| `MODEL_<STAGE>` | `config.model_id_for` | — | model for one agent; wins over `MODEL_ID` |
| `MODEL_MAX_TOKENS` | `config.build_model` | the agent's value (default 8192) | max output tokens |
| `MODEL_READ_TIMEOUT` | `config.build_model` | 120 s | Bedrock stream read timeout |
| `MODERNIZATION_TIME_BUDGET_S`, `_SOFT_LIMIT_S`, `_TEST_TIMEOUT`, `_PREWARM_TIMEOUT` | modernization [agent.py](../../transform_agents/modernization/agent.py), [tools.py](../../transform_agents/modernization/tools.py), [checks.py](../../transform_agents/modernization/checks.py) | 150, 90, 90, 300 s | stage deadline; tools ask the model to submit; one generated-test run; `prewarm_test_env()` |
| `TERRAFORM_OFFLINE` | [terraform/validate.py](../../transform_agents/terraform/validate.py) | unset | `1`/`true`/`yes`: no terraform binary (offline layers only) |
| `TERRAFORM_BIN` | `validate.terraform_bin` | `PATH` | terraform binary |
| `TF_WORKSHOP_CACHE` | `validate.cache_dir` | `~/.cache/transform-workshop/terraform` | warm module/provider cache |
| `STAGE_OVERRIDE` | `clients.LocalClient` | empty | `hello` or `discovery=hello,…`: local stand-ins |
| `AGENTCORE_ARN_<STAGE>` | `clients.load_arns` | `deploy/agent-arns.json` | override one runtime ARN |
| `FAKE_DELAY` | `clients.make_client` | 0.02 | fake-mode pacing |
| `RUNS_DIR` | [orchestrator/paths.py](../../transform_agents/orchestrator/paths.py) | `<repo>/runs` | run storage |
| `UI_MODE`, `UI_ENABLE_FAKE`, `PRESENTER_NAME` | [ui/app.py](../../ui/app.py) | `local`, unset, unset | initial mode; `1` adds Fake mode; name shown in the sidebar |
| `AWS_ACCESS_KEY_ID` … (`AWS_CREDENTIAL_ENV`) | `ui/app.py` `_aws_credentials` | — | credential probe, cached 300 s per value set |
| `DRY_RUN` | `deploy.sh` | 0 | package and synth only |
| `FORCE_SUBSET` | `deploy.sh` → `--force-subset` | 0 | subset deploy without state (other runtimes are deleted) |
| `VERBOSE` | `deploy.sh` (CFN events), `legacy-up.sh` (full plan) | 0 | more output |
| `NO_BINARIES` | `_common.sh` `binary_flags` | 0 | vendor no tool binaries |
| `VENDOR_TERRAFORM` | `_common.sh` `binary_flags` | 0 | vendor terraform 1.15.4 |
| `AUTO_APPROVE` | `destroy.sh`, `legacy-up.sh`, `legacy-down.sh` | 0 | skip the prompt (human only) |
| `AGENTCORE_TELEMETRY_DISABLED` | `_common.sh` | 1 | AgentCore CLI analytics off |
| `SMOKE_PORT`, `SMOKE_IMAGE`, `NO_INVOKE` | [smoke-local.sh](../../deploy/smoke-local.sh) | 18080, `python:3.12-slim`, 0 | local package test; `NO_INVOKE=1` skips Bedrock and credentials |
| `VENDOR_BIN_DIR`; `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` | generated `main.py`; also `checks.uv_dirs` | `/tmp/vendor-bin`; `/tmp/uv-cache`, `/tmp/uv-python` (cloud, modernization) | where vendored tools land on `PATH`; writable uv dirs |
| `KIND_CLUSTER` | `kind-up.sh`, `kind-down.sh`, `preflight.sh` | `transform-workshop` | cluster name (not seen by the agent, §9) |
| `METRICS_SERVER`, `PREPULL_IMAGES`, `PRELOAD_IMAGES` | `kind-up.sh` | 1, `python:3.12-slim`, empty | metrics-server install, host pre-pull, node pre-load |
| `ALLOWED_CIDR` | `legacy-up.sh` → `TF_VAR_allowed_cidr` | `0.0.0.0/0` | who reaches port 80 |
| `TIMEOUT_SECONDS` | `legacy-up.sh` | 600 | wait for the app |
| `WORKERS` | `legacy-load.sh` | 4 | parallel clients (positional args: URL, MINUTES=5) |
| `NO_COLOR` | `preflight.sh` | unset | plain output |
| `PLATFORM`, `PORT`, `KEEP` | [bootstrap_in_docker.sh](../../legacy-infra/terraform/tests/bootstrap_in_docker.sh) | `linux/amd64`, 8088, 0 | container platform, port, keep container |
| `TF_VAR_<name>` | Terraform | `variables.tf` | e.g. `TF_VAR_region`, `TF_VAR_instance_type` |

**Deploy-time env copied into runtimes** (`deploy_tool.stage_env`, written as `envVars` in `agentcore.json`): `MODEL_ID`,
`MODEL_MAX_TOKENS`, `MODEL_<STAGE>` (that stage only), `TERRAFORM_OFFLINE` (terraform only). Fixed:
`TF_WORKSHOP_CACHE=/tmp/transform-workshop/terraform` (terraform). Set by `main.py` with `setdefault` (a runtime value
wins): `AWS_REGION`/`AWS_DEFAULT_REGION` = deploy region; `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR` (modernization).
**Gaps** (not copied; the cloud uses defaults): `MODEL_READ_TIMEOUT`, `MODERNIZATION_*`, `TERRAFORM_BIN`. `STAGE_OVERRIDE`
is local-only by design. A changed variable needs a redeploy (`deploy.sh <stage>`; already deployed runtimes stay).

## 5. Runbooks

**(a) One-time setup** (no AWS writes): `make install` (`uv sync --all-extras`) → `make tools` → `make kind-up` →
`make lint test` (Bedrock mocked) → `make preflight`. Optional: `make test-live` (cents; the k8s test needs kind).

**(b) Legacy estate** ([legacy-infra/terraform/](../../legacy-infra/terraform/), local backend):
- **Up (approval):** `make legacy-up`. STS identity → `terraform init` → plan summary (`VERBOSE=1` for all) → prompt →
  apply → polls `$app_url/api/items` every 10 s for up to `TIMEOUT_SECONDS` (first boot 2–4 min). With no changes it
  prints "Already deployed". Lock it down: `ALLOWED_CIDR="$(curl -s https://checkip.amazonaws.com)/32" make legacy-up`.
  Another region needs `TF_VAR_region=<r>` plus a matching `AWS_REGION` (discovery looks in `AWS_REGION`).
- **Load:** `scripts/legacy-load.sh "" 15` (URL from `terraform output app_url`). Run it at least a day before the talk
  so CloudWatch has CPU history (discovery reads 14 days).
- **Shell:** `aws ssm start-session --target <instance_id> --region us-west-2` (output `ssm_session_command`), then
  `sudo tail -n 50 /var/log/legacy-bootstrap.log`; re-run the bootstrap with `sudo bash /var/lib/cloud/instance/scripts/part-001`.
- **Bootstrap test without AWS:** `legacy-infra/terraform/tests/bootstrap_in_docker.sh` (`PLATFORM=linux/arm64` is
  faster on Apple Silicon). It renders user_data with `terraform init -backend=false` + `terraform console`, runs it twice
  in Amazon Linux 2023, and smoke-tests through nginx (~1 min).
- **Down (approval):** `make legacy-down`; no-op when the state is empty.
- **Cost:** ~$2.24/day, ~$68/month (README table). State is local only: losing `terraform.tfstate` orphans the
  resources; find them by tag `app=legacy-inventory` and delete them by hand.

**(c) AgentCore Runtime** ([deploy/](../../deploy/)):

First-deploy checklist (the day before at the latest):
1. Good network (wired or hotspot): every deploy repackages and uploads all 7 zips, ~470 MB (zip hashes always change).
2. `aws sts get-caller-identity` shows the intended account; `make preflight` passes.
3. `aws cloudformation describe-stacks --stack-name CDKToolkit` (read-only). If it is missing, the deploy creates it
   first (bucket `cdk-hnb659fds-assets-<account>-us-west-2`, ECR repo, roles): include that in the approval.
4. Optional: `DRY_RUN=1 deploy/deploy.sh`. On an unbootstrapped account it ends with "AWS environment needs
   bootstrapping"; that is expected.
5. `make deploy-agents` (approval): `ensure_cli` (`npm ci` into `deploy/.cli` and `deploy/agentcore/cdk` when missing) →
   STS → `deploy_tool.py prepare` → `agentcore validate` → `agentcore deploy --yes` → `deploy_tool.py arns`. Roughly
   10–20 min the first time (estimate).
6. `deploy/status.sh` shows 7 × `READY`; `deploy/invoke.sh hello 'List the files and report.'` ends with a `result`.
7. `make ui`, then pick **AgentCore** in the top-bar **Local · AgentCore** switch; the **…** menu shows "6/6 AgentCore
   runtime ARNs found".

| Task | Command |
|---|---|
| Deploy all / a subset | `deploy/deploy.sh [--all]` · `deploy/deploy.sh hello discovery` (keeps what is deployed) |
| Invoke | `deploy/invoke.sh <stage> '<text or JSON>' [--raw] [--out f.jsonl]` · `@payload.json` · `--local http://localhost:8080` |
| Status | `deploy/status.sh` (`agentcore status` + `GetAgentRuntime` per ARN) |
| Logs / traces | `deploy/logs.sh <stage> [--since 2h --level error]` · `deploy/logs.sh <stage> traces` · `… trace <id>` |
| Any CLI command | `deploy/agentcore.sh <args>` |
| Rebuild `agent-arns.json` (read-only) | `uv run python deploy/deploy_tool.py arns` |
| Destroy some / all (approval) | `deploy/destroy.sh terraform finops` · `deploy/destroy.sh` |
| Package test, no deploy | `deploy/smoke-local.sh <stage>` (Bedrock call) · `NO_INVOKE=1 deploy/smoke-local.sh <stage>` |

Vendored linux/arm64 binaries (`deploy_tool.BINARIES`, cached in `deploy/.cache/bin`):

| Binary | Version | Stage | Size | Default |
|---|---|---|---|---|
| kubeconform | 0.8.0 | k8s_validation | 13 MB | yes (schemas download from GitHub at run time) |
| kubectl | 1.33.13 | k8s_validation | 57 MB | yes (only `kubectl kustomize`, offline) |
| uv | 0.12.15 | modernization | 43 MB | yes (wheels from PyPI at run time) |
| terraform | 1.15.4 | terraform | 108 MB | opt-in: `VENDOR_TERRAFORM=1` (cold `init` per session: too slow on stage) |

**Packaging limits:** 250 MB zipped and 750 MB unzipped per runtime. Synth on 2026-10-20: 61/174 MB (hello, discovery,
requirements, finops, terraform without the binary), 80/217 MB (modernization), 84/244 MB (k8s_validation), 92/282 MB
(terraform with `VENDOR_TERRAFORM=1`). `NO_BINARIES=1` drops every tool. A build leaves ~2 GB of gitignored staging in
`deploy/agentcore/.cache` and `deploy/agentcore/cdk/cdk.out`; delete it freely.

**(d) kind cluster:** `make kind-up` needs kind, kubectl and Docker. It creates `transform-workshop` from
[kind-config.yaml](../../scripts/kind-config.yaml) (node pinned by digest), waits up to 120 s for Ready, pre-pulls
`python:3.12-slim`, installs metrics-server v0.9.0 with `--kubelet-insecure-tls` (`METRICS_SERVER=0` skips; needs
GitHub). Idempotent: ~40 s cold, ~1 s warm. The k8s_validation live deploy (Local mode only) builds the image, runs
`kind load`, `kubectl apply -k k8s/overlays/local`, waits for the rollout and smoke-tests over a port-forward. After a
Docker restart the node container may be stopped: `make kind-down && make kind-up`. `make kind-down` is a no-op when
the cluster is absent.

**(e) Preflight** ([preflight.sh](../../scripts/preflight.sh), bash 3.2-compatible, ~15 min before the talk). Hard
checks (exit 1): AWS credentials; one Converse per distinct model; Docker daemon; `.tools/bin` kind and kubeconform;
kind cluster with all nodes Ready; disk < 1 GB. Warnings: Terraform missing or warm-up not `VALID`
(`python -m transform_agents.terraform.tools --warmup`), modernization `prewarm_test_env()`, kubeconform cache
warm-up, disk < 5 GB. Info: AgentCore ARNs (n/6), legacy app reachability.

**(f) Recording the fallback** (content rules: [05-demo-content.md](05-demo-content.md)):
1. Prerequisites: kind up, Docker running, and, for live data, the legacy EC2 deployed and loaded.
2. Run headless with the exact feedback string the test expects (`FEEDBACK` in `test_orchestrator_replay.py`):
   `uv run python -m transform_agents.orchestrator --requirements samples/requirements/migration-requirements.md --mode local --auto-approve --live-aws --live-deploy --simulate-drift --revise requirements="Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."` (~2.5 min).
3. `make record RUN=runs/<id>`. It refuses incomplete runs (no `--force` for the tracked copy); keeps `state.json`,
   `inputs/`, every attempt's `events.jsonl`/`result.json` and `history/`; drops `workspace/`, `.running`, `.DS_Store`,
   `*.tmp` and any other file over 2 MB; scrubs the repo path and `$HOME`. It does not redact instance or AMI IDs
   (present with `--live-aws`; not secrets) and does not redact account IDs: the leak test catches those.
4. `make numbers`, `REPLAY_SPEED=4 make replay` (1 = recorded timings), `make test`, then commit `samples/recorded-run/`.
5. Re-record with `--live-aws` once the EC2 exists: the tracked recording has `live_aws: false` (§9).

**(g) Release checklist** (no CI; a release is a commit on `main` plus a tag of what was presented):
- *Every change in scope:* `make lint test`; deploy changes add `uv run pytest tests/test_deploy.py`,
  `deploy/agentcore.sh validate`, `DRY_RUN=1 deploy/deploy.sh` and, for packaging, `NO_INVOKE=1 deploy/smoke-local.sh <stage>`;
  legacy changes add `bootstrap_in_docker.sh`. `git status` shows nothing untracked that tracked files reference (fonts
  in `ui/.streamlit/config.toml`, scripts in the Makefile). `git grep -nE '\b[0-9]{12}\b|/Users/|AWSReservedSSO'` is empty.
- *T-7 days:* `make preflight`; deploy the agents if AgentCore mode is shown (approval); `make run-cli MODE=agentcore`
  once; deploy the legacy EC2 (approval) per WORKSHOP §4, or at the latest T-1 day (each day costs ~$2.24); check the
  expiry calendar in [05-demo-content.md](05-demo-content.md).
- *T-1 day:* `scripts/legacy-load.sh "" 15`; rehearse in the UI (presenter mode); re-record with `--live-aws` (f);
  `make numbers` into the cheat sheet; drill Plan B (Replay toggle in a fresh tab, WORKSHOP §8).
- *T-1 hour:* [WORKSHOP.md §6](../../WORKSHOP.md#6-day-of-checklist) (SSO login, Docker + `make kind-up`, preflight
  green, `deploy/status.sh` READY, legacy `curl`, `make replay`, UI settings).
- *After the talk:* (h), then `git tag talk-<yyyy-mm-dd>` on the presented commit.

**(h) Teardown** (each step needs approval): `make legacy-down` → `make destroy-agents` → `make kind-down`.
`destroy.sh` keeps `CDKToolkit`, the log groups `/aws/bedrock-agentcore/runtimes/*` and the Transaction Search
setting. Full cleanup: wait for the agents' stack to reach `DELETE_COMPLETE`; empty the versioned bucket
`cdk-hnb659fds-assets-<account>-us-west-2` (all versions and delete markers); run
`aws cloudformation delete-stack --stack-name CDKToolkit`; delete the log groups if wanted. Verify (read-only):
`aws cloudformation describe-stacks --stack-name AgentCore-TransformWorkshop-default` says it does not exist;
`aws ec2 describe-instances --filters Name=tag:app,Values=legacy-inventory Name=instance-state-name,Values=running`
is empty. Local: `make clean CLEAN_RUNS=1` (keeps `samples/recorded-run/`), `rm -rf deploy/agentcore/.cache deploy/agentcore/cdk/cdk.out`.

**(i) Costs** (us-west-2, on-demand, October 2026):

| Item | While it exists | Idle / after teardown |
|---|---|---|
| Legacy EC2 t3.large ($0.0832/h) | $2.00/day | $0 |
| gp2 30 GiB + public IPv4 + 2 CloudWatch agent metrics | $0.10 + $0.12 + $0.02 per day | $0 |
| **Legacy total** | **~$2.24/day, ~$68/month** | $0 |
| AgentCore Runtime (7) | well under $0.10 per full pipeline run (per-second CPU while code runs; memory per session; 600 s idle timeout) | $0 |
| Bedrock tokens | ~250k tokens per full run: a few cents; preflight and `test-live`: cents | $0 |
| CDK asset bucket | ~470 MB added per deploy; nothing here garbage-collects old zips (cents per month) | until emptied |
| CloudWatch logs and spans | cents | log groups kept until deleted |
| kind, Streamlit, local runs | free | — |
| EKS/RDS from the generated Terraform | never applied ($210–620/month if it were) | — |

## 6. Dependency & version management

| Class | Pin | Where |
|---|---|---|
| Python | 3.12 | [.python-version](../../.python-version); `requires-python >=3.12` in [pyproject.toml](../../pyproject.toml) and [uv.lock](../../uv.lock); `RUNTIME_VERSION = "PYTHON_3_12"` in [deploy_tool.py](../../deploy/deploy_tool.py); `uv run --python 3.12` in `checks._uv_run`; `python:3.12-slim` in the pattern Dockerfile, `kind-up.sh` `PREPULL_IMAGES`, `smoke-local.sh` `SMOKE_IMAGE`, `orchestrator/fakes.py` and `tests/fixtures/k8s_sample` |
| Python packages | floors in pyproject; exact in uv.lock: strands-agents 1.61.1, strands-agents-tools 0.8.9, bedrock-agentcore 1.24.2, boto3/botocore 1.43.115, pyyaml 6.0.3, python-hcl2 8.1.4, streamlit 1.66.0; dev: pytest 9.1.1, pytest-asyncio 1.4.0, ruff 0.17.1 | pyproject.toml, uv.lock. Deploys export uv.lock with `uv export --frozen` |
| Runtime extra | `aws-opentelemetry-distro==0.21.0`; uv.lock's `opentelemetry-*` pins are dropped so ADOT decides them | `EXTRA_REQUIREMENTS`, `UNPINNED_PREFIXES` in deploy_tool.py |
| AgentCore CLI | `@aws/agentcore` 0.31.1 (exact) | [deploy/.cli/package.json](../../deploy/.cli/package.json) + lock |
| CDK app | `@aws/agentcore-cdk` 0.1.0-alpha.54 (exact), `aws-cdk-lib` ~2.261.0, `aws-cdk` ~2.1126.0, `constructs` ~10.7.0, `typescript` ~5.9.3 | [deploy/agentcore/cdk/package.json](../../deploy/agentcore/cdk/package.json) + lock |
| Vendored binaries | terraform 1.15.4, kubeconform 0.8.0, kubectl 1.33.13, uv 0.12.15, each with URL and sha256 | `BINARIES` in deploy_tool.py |
| Host tools | kind v0.33.0, kubeconform v0.8.0, 4 sha256 each | [install-tools.sh](../../scripts/install-tools.sh) |
| kind node, metrics-server | `kindest/node:v1.36.4@sha256:099e0493…`; v0.9.0 | [kind-config.yaml](../../scripts/kind-config.yaml), [kind-up.sh](../../scripts/kind-up.sh) |
| Legacy Terraform | `>= 1.6`; `hashicorp/aws ~> 6.0`, locked 6.69.1; AL2023 AMI from SSM with `ignore_changes = [ami]` | [versions.tf](../../legacy-infra/terraform/versions.tf), [.terraform.lock.hcl](../../legacy-infra/terraform/.terraform.lock.hcl), main.tf |
| Blueprint Terraform | aws `~> 6.0`, vpc `~> 6.0` (schema 6.7.3), eks `~> 21.0` (21.29.0), provider schema 6.69.1, generated 2026-10-19 | `terraform/knowledge/blueprint/`, `module-inputs.json`, `extract_module_inputs.py` |
| Models, prices | `DEFAULT_MODELS`; price snapshot fetched 2026-10-19 | `core/config.py`; `finops/knowledge/prices-us-west-2.json` |
| Legacy app | Flask 2.0.3 on python3 3.9: deliberately end-of-life, never bump | `legacy-infra/app/requirements.txt` |

Upgrade procedures (one class per change):
- **Python packages:** `uv lock --upgrade-package <name>` → `make install` → `make lint test` → `make test-live` (twice
  for agent-facing packages) → `NO_INVOKE=1 deploy/smoke-local.sh hello` (deploy uses the lock). strands or
  bedrock-agentcore: also `deploy/smoke-local.sh hello` and re-record if events change.
- **ADOT:** edit `EXTRA_REQUIREMENTS` → `uv run pytest tests/test_deploy.py` → `deploy/smoke-local.sh hello` (expect
  `/ping` Healthy and the span count it reports; deploy/README recorded 9 or more).
- **AgentCore CLI / CDK app:** read the changelog; `npm install --prefix deploy/.cli --save-exact @aws/agentcore@<v>`
  (or edit the CDK `package.json`, then `npm install` to refresh the lock) → `cd deploy/agentcore/cdk && npm run build && npm test`
  → `deploy/agentcore.sh validate` → `uv run pytest tests/test_deploy.py` (`test_runtime_spec_matches_cli_schema`, stack
  name) → `DRY_RUN=1 deploy/deploy.sh` → smoke-local. Keep `empty_project()` in step with the CLI schema.
- **Vendored binaries:** change version, URL and sha256 (from the upstream checksum file) in `BINARIES` → delete the
  old `deploy/.cache/bin/<name>-*` → `uv run pytest tests/test_deploy.py` → `NO_INVOKE=1 deploy/smoke-local.sh k8s_validation`
  and `modernization` (`VENDOR_TERRAFORM=1 … terraform` for terraform) → update deploy/README. Keep kubeconform equal
  in `BINARIES` and `install-tools.sh` (same linux-arm64 sha256 today).
- **kind / node image / metrics-server:** bump `KIND_VERSION` and its 4 checksums; take the node digest from that kind
  release's notes → `make kind-down && make tools && make kind-up && make preflight` → one `--live-deploy` run.
- **Terraform:** legacy: `terraform -chdir=legacy-infra/terraform init -upgrade` (updates the lock), `bootstrap_in_docker.sh`,
  `terraform plan` (read-only) on a deployed estate. Blueprint: change the constraints, run
  `uv run python transform_agents/terraform/knowledge/extract_module_inputs.py`, `make preflight` (warm-up `VALID`),
  `uv run pytest tests/test_terraform.py`, `make test-live`, re-record. Module sources stay in `ALLOWED_MODULE_SOURCES`.
- **Python minor version:** change every location in the Python row together and check AgentCore supports the new
  `runtimeVersion`; full test, live, smoke-local and re-record.
- **Models and price snapshot:** owned by [02-agents.md](02-agents.md) (benchmark, preflight, re-record).

## 7. Security notes

- **Runtime IAM role.** CDK construct statements: `bedrock:InvokeModel`, `InvokeModelWithResponseStream`, `CountTokens`
  on `foundation-model/*` and `inference-profile/*` (deliberately not narrowed, so `MODEL_<STAGE>` works); 7 named
  `logs:` actions (create, put, describe, get, filter) on `/aws/bedrock-agentcore/runtimes/*` plus `DescribeLogGroups`; `xray:PutTraceSegments`, `PutTelemetryRecords`;
  `bedrock-agentcore:*ConfigurationBundle*` (incl. Create/Update/Delete, scoped to `configuration-bundle/*`, added by the
  CLI, unused). Plus the 5 statements of `runtime-extras.json`: pricing, savingsplans and `eks:DescribeClusterVersions`
  (FinOps), 4 exact EC2 `Describe*` actions, 3 CloudWatch reads, X-Ray sampling, `PutMetricData` limited to
  `bedrock-agentcore`. The synthesized template (local `cdk.out`) has 7 roles and 14 policies. Callers need
  `bedrock-agentcore:InvokeAgentRuntime` on the ARNs.
- **Generated code runs locally without cloud credentials** (`checks.clean_env`): an allowlist of env variables
  (`PATH`, locale, proxies, CA bundles, `LC_*`, `UV_*`), any name matching TOKEN/SECRET/PASSW/CREDENTIAL/API_KEY dropped,
  `HOME` set to a throwaway dir, the process group killed on timeout. **It is not a sandbox:** model-written tests run as
  your user with full filesystem and network access, and live deploy builds a model-written Dockerfile with your Docker
  daemon. Run rehearsals on a machine where that is acceptable.
- **Terraform module source allow-list** (`validate.ALLOWED_MODULE_SOURCES`): only `terraform-aws-modules/vpc/aws` and
  `eks/aws`, providers only `hashicorp/*`; anything else is an error before `terraform init` runs. Init uses
  `-backend=false`; generated Terraform is never applied.
- **k8s write guard:** `k8s_validation.tools.write_tools` only writes under `k8s/` (`WRITABLE_PREFIX`); the cluster is
  reached only via `--context kind-<cluster>`.
- **`kube.raw_render` containment:** resolves every kustomization entry and skips anything outside the materialized
  `k8s/` tree (e.g. `~/.kube/config`).
- **Secrets:** none tracked except the legacy app's deliberate ones. `smoke-local.sh` writes exported credentials to a
  `umask 077` temp file, passes them to the container and deletes the file on exit. Runtimes use their role, never your
  credentials. `deployed-state.json` stays local (account id, ARNs).
- **The legacy app is deliberately insecure:** hard-coded `ADMIN_TOKEN = "admin123"` (also in EC2 user data), SQL
  injection, Flask dev server as root, HTTP only, port 80 open to `0.0.0.0/0` by default. Use
  `ALLOWED_CIDR=<your-ip>/32`, never load real data, never expose it beyond the demo, destroy it after the session.
  Mitigations kept: no SSH (SSM only), IMDSv2 required.
- Runtimes use `PUBLIC` network mode: kubeconform fetches schemas from GitHub and uv fetches wheels from PyPI at run time.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ExpiredToken`, `Token has expired`, preflight ❌ AWS credentials | SSO session expired | the human runs `aws sso login --profile "$AWS_PROFILE"`; agents never work around it. Retry at the gate; the UI's credential probe is cached 300 s |
| `AccessDeniedException` / 403 on one model | model access not enabled (for `us.` profiles, the routed regions too) or model retired | enable it in the Bedrock console → Model catalog, or `MODEL_<STAGE>=<id>`; re-run `make preflight` |
| `ThrottlingException` | Bedrock quota; 4 adaptive retries exhausted | wait and Retry; switch model (WORKSHOP §8 list); on stage, Replay |
| Terraform stage stalls at `terraform init` | cold module/provider cache (240 s init timeout) | `make preflight` beforehand (warm-up); on stage restart with `TERRAFORM_OFFLINE=1 make ui` |
| "kind cluster 'transform-workshop' not found" / node not Ready | cluster never created, Docker restarted, or `KIND_CLUSTER` renamed | `make kind-up`; if the node container is stopped, `make kind-down && make kind-up` |
| "docker daemon is not reachable" / "docker is not running" | Docker Desktop stopped | start it, then `make kind-up` |
| "Port 8501 is already in use" | another UI (maybe the presenter's) | `UI_PORT=8502 make ui`; never kill a UI someone is using. Others: `SMOKE_PORT`, bootstrap `PORT`, local agent :8080 |
| UI CSS or theme change not visible | Streamlit doesn't hot-reload CSS from imported modules (`ui/style.py`) and reads `config.toml` at startup | restart your own UI server, hard-reload the browser |
| "no AgentCore Runtime ARN for <stage>" | `deploy/agent-arns.json` missing or partial | `uv run python deploy/deploy_tool.py arns` (read-only), or `AGENTCORE_ARN_<STAGE>`, or deploy (approval). On stage, switch to Local |
| "AWS environment needs bootstrapping" | `DRY_RUN=1` on an unbootstrapped account/region | expected; the real deploy bootstraps (approval), or `cd deploy/agentcore/cdk && npx cdk bootstrap aws://<account>/us-west-2` |
| "refusing a subset deploy", or a subset deploy re-adds a runtime you removed | DescribeStacks failed; no CLI state, or a stale `agentcore.json`/`deployed-state.json` (other account, deleted stack) was used instead | fix credentials/region so the stack lookup works; check `deploy/agentcore/aws-targets.json`; `deploy.sh --all`; `FORCE_SUBSET=1` only with approval; after the stack is gone, `deploy_tool.py forget` |
| Stack `*_IN_PROGRESS` / `*_FAILED` | an earlier deploy or delete | `destroy.sh` refuses `*_IN_PROGRESS`; read the stack events in the console, wait, retry |
| Replay plays an unexpected run | `recorded` falls back to `latest` (newest complete run in `runs/`) when `samples/recorded-run/state.json` is missing, then to fixtures; the UI's **Replay From** keeps its session choice | `make replay REPLAY_FROM=runs/<id>` or pick in **Replay From**; check `git status samples/` |
| Legacy app doesn't answer | bootstrap failed or still running | SSM shell, bootstrap log, re-run `part-001`; the demo works without it (Live AWS Discovery off) |
| kubectl talks to the kind cluster by default | `kind-up.sh` switched the current-context | `kubectl config use-context <yours>` |

## 9. Known debt and doc conflicts

Verified on 2026-10-20. "Fixed (uncommitted)" means the working tree already has the fix but it was not committed when checked.

| # | Item | Evidence | Status / action |
|---|---|---|---|
| 1 | $2.24 vs 2.25/day | README table sums to 2.24; `legacy-up.sh` prompt said 2.25 | Fixed (uncommitted): the prompt now says 2.24 |
| 2 | ~450 vs ~470 MB upload | zip sizes sum to 469 MB; WORKSHOP §4.2 said 450 | Fixed (uncommitted): WORKSHOP now says 470 |
| 3 | "Sidebar" for the AgentCore mode | the mode is the top-bar `segmented_control` (`ui/app.py` `topbar`); toggles are in the **…** menu | Fixed (uncommitted) in deploy/README, WORKSHOP and KEYNOTE-SCRIPT |
| 4 | Dated statements in deploy/README | "not bootstrapped on 2026-10-20" was removed (uncommitted); still dated: the synth size table and "Result on 2026-10-20" table; WORKSHOP §4.2 "Transaction Search is already on in this account" | Open: re-verify or remove at the next deploy |
| 5 | preflight's unreachable `MISSING` branch | `checks.prewarm_test_env` exists | Open: delete the `getattr` fallback and the `MISSING` case |
| 6 | Makefile "written by another workstream" | comment on `run_script` | Fixed (uncommitted) |
| 7 | Recording made with Live AWS off | `samples/recorded-run/state.json` `options.live_aws: false` (`live_deploy` and `simulate_drift` true) | Open: re-record with `--live-aws` after `legacy-up` (5f) |
| 8 | Local `legacy-infra/terraform/terraform.tfstate` | serial 9, 8 resources, `aws_instance` t3.large `running` at last refresh; `deploy/agent-arns.json` lists 7 runtimes | Open: the EC2 is probably billing ~$2.24/day and the AgentCore stack and `CDKToolkit` probably exist. Confirm read-only, then tear down with approval (5h) |
| 9 | kubectl 1.33.13 vendored vs kind node 1.36.4 | three minors apart, outside kubectl's ±1 skew | Open, low: the vendored kubectl only runs `kustomize` offline in the cloud; bump with §6 |
| 10 | "deploy.sh refuses while the stack is `*_IN_PROGRESS` or `*_FAILED`" | deploy/README Troubleshooting; no such check in `deploy.sh`/`deploy_tool.py` (only `plan_destroy` refuses `*_IN_PROGRESS`) | Open: implement it in `cmd_prepare` or reword the README |
| 11 | `KIND_CLUSTER` isn't seen by the agent | `k8s_validation` uses `options.kind_cluster` (default `transform-workshop`); no CLI flag or env | Open: keep the default name, or plumb it through |
| 12 | `legacy-up.sh` exports `AWS_REGION`, Terraform ignores it | provider uses `var.region` | Open: pass `TF_VAR_region="$AWS_REGION"` in the script |
| 13 | "Everything [the runtimes] are granted is read-only, apart from telemetry" | the CLI-added ConfigurationBundle actions include Create/Update/Delete; `test_deploy.py` only checks `runtime-extras.json` | Open: reword deploy/README |
| 14 | The IAM test's scanner is pattern-based | `AWS_CALL_RE` sees `describe_*`, `get_metric_*`, `list_metrics` and `get_paginator("…")` only | Open: widen it when an agent adds another verb |
| 15 | preflight looks for `.tools/bin/kubectl` | `install-tools.sh` never installs kubectl | Open, cosmetic |

## 10. References

- Code: [Makefile](../../Makefile) · [scripts/](../../scripts/) · [deploy/](../../deploy/) ([deploy_tool.py](../../deploy/deploy_tool.py), [runtime-extras.json](../../deploy/policies/runtime-extras.json)) · [legacy-infra/terraform/](../../legacy-infra/terraform/) · [pyproject.toml](../../pyproject.toml) · [uv.lock](../../uv.lock) · [record.py](../../transform_agents/orchestrator/record.py) · [clients.py](../../transform_agents/orchestrator/clients.py) · [core/config.py](../../transform_agents/core/config.py)
- Tests: [test_deploy.py](../../tests/test_deploy.py) · [test_orchestrator_replay.py](../../tests/test_orchestrator_replay.py) · [test_orchestrator_makefile.py](../../tests/test_orchestrator_makefile.py)
- Docs: [deploy/README.md](../../deploy/README.md) · [legacy-infra/README.md](../../legacy-infra/README.md) · [WORKSHOP.md](../../WORKSHOP.md) (§4 deploy, §6 day-of, §8 plan B, §9 teardown) · [MAINTAINING.md](../../MAINTAINING.md) · [CLAUDE.md](../../CLAUDE.md) · specs [01](01-architecture.md), [02](02-agents.md), [03](03-ui.md), [05](05-demo-content.md)
