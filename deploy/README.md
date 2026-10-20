# Deploying the agents to Amazon Bedrock AgentCore Runtime

One command deploys every workshop agent (the `hello` smoke test plus the six stages in
`transform_agents/core/config.py` `STAGES`) as its own AgentCore Runtime in `us-west-2`:

```bash
AWS_PROFILE=<your-profile> deploy/deploy.sh        # e.g. AWS_PROFILE=personal-dev; or: make deploy-agents
```

Deploying for the first time? Follow the [first deploy checklist](#first-deploy-checklist).

The command packages each agent as an arm64 CodeZip, deploys the zips with the **AgentCore CLI**
(`@aws/agentcore`, which drives CDK and CloudFormation underneath), and writes `deploy/agent-arns.json`
(`{stage: runtimeArn}`). The orchestrator and the Streamlit UI read that file in AgentCore mode.

The local code and the cloud runtimes are the same code: each runtime runs `transform_agents/<stage>/runtime.py:app`,
the `BedrockAgentCoreApp` you already run locally on `:8080`.

## Contents

- [First deploy checklist](#first-deploy-checklist)
- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [One-time setup](#one-time-setup)
- [Day-to-day commands](#day-to-day-commands)
- [Teardown](#teardown)
- [What gets created](#what-gets-created)
- [IAM execution role](#iam-execution-role)
- [Environment variables](#environment-variables)
- [Tool binaries (kubeconform, kubectl, uv, terraform)](#tool-binaries-kubeconform-kubectl-uv-terraform)
- [Verifying without deploying](#verifying-without-deploying)
- [How the UI and orchestrator use the ARNs](#how-the-ui-and-orchestrator-use-the-arns)
- [Cost](#cost)
- [Troubleshooting](#troubleshooting)
- [Why the AgentCore CLI rather than Terraform](#why-the-agentcore-cli-rather-than-terraform)

## First deploy checklist

Do this the day before the session, on a good network (a wired connection or a phone hotspot, not venue Wi-Fi):
the deploy uploads about 470 MB.

1. **Log in and pick the account.**
   `aws sso login --profile <your-profile>`, then `export AWS_PROFILE=<your-profile> AWS_REGION=us-west-2`.
   Check that `aws sts get-caller-identity` shows the account you expect.
2. **Preflight.** `make preflight` checks credentials, Bedrock model access and the local tools.
3. **Optional rehearsal.** `DRY_RUN=1 deploy/deploy.sh` packages and synthesizes all 7 agents and creates nothing.
   It still needs valid credentials, and it ends with "AWS environment needs bootstrapping" on a fresh account;
   that is expected.
4. **Deploy.** `deploy/deploy.sh`. On the first deploy in an account and region, the CLI first runs the
   **CDK bootstrap**: it creates the `CDKToolkit` CloudFormation stack (an S3 asset bucket, an ECR repository and
   the CDK deploy roles). That adds about 1–2 minutes. Then it packages the 7 zips (1–3 minutes), uploads them, and
   CloudFormation creates the runtimes and their IAM roles. Plan for roughly 10–20 minutes the first time; these
   are estimates, not measurements. The script ends by writing `deploy/agent-arns.json`.
5. **Smoke test.** `deploy/invoke.sh hello '{"prompt":"hi"}'` should stream status events and end with a `result`.
   `deploy/status.sh` should list all 7 runtimes as `READY`.
6. **UI.** `make ui`, then choose the **☁️ AgentCore Runtime** mode in the sidebar.
7. **After the workshop,** tear everything down: see [Teardown](#teardown).

## How it works

```
deploy/
├── deploy.sh destroy.sh invoke.sh status.sh logs.sh agentcore.sh smoke-local.sh
├── deploy_tool.py            helpers: stage discovery, code dirs, agentcore.json, what is deployed, ARN file, invoke, status
├── policies/runtime-extras.json   extra IAM statements attached to every runtime role
├── .cli/                     the AgentCore CLI, pinned to @aws/agentcore 0.31.1 (npm ci, node_modules gitignored)
├── agentcore/                the AgentCore CLI project ("deploy/" is the project root)
│   ├── agentcore.json        generated: one CodeZip runtime per stage (gitignored)
│   ├── aws-targets.json      generated: account + region from STS (gitignored)
│   ├── .cli/deployed-state.json   written by `agentcore deploy`: runtime ids and ARNs (gitignored)
│   └── cdk/                  the CDK app the CLI generated (@aws/agentcore-cdk L3 constructs), committed as is
└── app/<stage>/              generated code locations (gitignored), one per runtime:
    ├── main.py               entrypoint: exposes vendored tools, then serves transform_agents.<stage>.runtime:app
    ├── pyproject.toml        dependencies pinned from uv.lock, plus aws-opentelemetry-distro
    ├── transform_agents/     a copy of our package (core and every agent; no __pycache__ or .terraform)
    ├── vendor/bin/           linux-arm64 tools for that stage (k8s_validation: kubeconform + kubectl; modernization: uv;
    │                         terraform on request)
    └── build-manifest.json   what was packaged
```

`deploy.sh` runs these steps:

1. `deploy_tool.py prepare` finds the stages that have a `runtime.py` defining `app` and skips the others with
   a warning. For a subset deploy it first works out which runtimes are already deployed (see
   [subset deploys](#subset-deploys)). It then builds `app/<stage>/` for each one and writes `agentcore.json` and
   `aws-targets.json`.
2. `agentcore validate` checks those files.
3. `agentcore deploy --yes` packages each `app/<stage>/` with
   `uv pip install -r pyproject.toml --python-platform aarch64-manylinux_2_28 --only-binary :all: --target …`,
   zips it, uploads it to the CDK asset bucket, and creates or updates one CloudFormation stack,
   `AgentCore-TransformWorkshop-default`.
4. `deploy_tool.py arns` copies the runtime ARNs from `agentcore/.cli/deployed-state.json` into `deploy/agent-arns.json`.
   If that file is missing, it reads them from the stack outputs instead. If neither exists, it leaves
   `agent-arns.json` as it is.

Two details about the runtimes:

- **Entrypoint.** The runtimes are Python 3.12, arm64, `PUBLIC` network mode and protocol `HTTP`. Each one starts as
  `opentelemetry-instrument main.py`, so the AWS Distro for OpenTelemetry (ADOT) sends Strands spans
  (`invoke_agent`, `execute_event_loop_cycle`, `execute_tool`, `chat`) and Bedrock spans to CloudWatch.
- **Dependency pins.** ADOT pins exact OpenTelemetry versions. So the build takes every pin from `uv.lock` except the
  `opentelemetry-*` family, and lets ADOT's pins decide those.

## Prerequisites

- Node ≥ 20 with npm, `uv`, AWS CLI v2. Docker is only needed for `smoke-local.sh`.
- AWS credentials for the target account. Use an SSO profile with rights to run CloudFormation and to create IAM
  roles, S3 objects and `bedrock-agentcore` resources (AdministratorAccess works):
  `aws sso login --profile <your-profile>`.
- Bedrock access to the models in `transform_agents/core/config.py`: Nova 2 Lite (`us.` profile), DeepSeek V3.2 and
  Mistral Large 3 in `us-west-2`. If the account has never used them, invoke each model once from the console or
  the CLI first (`make preflight` checks this).

## One-time setup

| Step | Who does it | Manual command |
|---|---|---|
| Install the pinned CLI and the CDK app deps | `deploy.sh` (`npm ci` into `deploy/.cli` and `deploy/agentcore/cdk`) | `npm ci --prefix deploy/.cli && npm ci --prefix deploy/agentcore/cdk` |
| **CDK bootstrap** (creates the `CDKToolkit` stack: asset S3 bucket, ECR repo, deploy roles) | the first `agentcore deploy --yes` does it automatically | `cd deploy/agentcore/cdk && npx cdk bootstrap aws://<account>/us-west-2` |
| **CloudWatch Transaction Search** (traces in GenAI Observability) | `agentcore deploy` enables it if it is missing. Check with `aws xray get-trace-segment-destination` (→ `CloudWatchLogs`). | Console: CloudWatch → Application Signals → Transaction Search → Enable |

To see whether the region is already bootstrapped, run `aws cloudformation describe-stacks --stack-name CDKToolkit`.
The author's account (`<account-id>`) was not bootstrapped in `us-west-2` on 2026-10-20, so its first deploy
creates `CDKToolkit` first.

## Day-to-day commands

All commands honour `AWS_PROFILE` and `AWS_REGION` (default `us-west-2`).

| What | Command |
|---|---|
| Deploy everything | `deploy/deploy.sh` or `deploy/deploy.sh --all` (or `make deploy-agents`) |
| Deploy a subset | `deploy/deploy.sh hello discovery` (or `make deploy-agents AGENTS="hello discovery"`); see [subset deploys](#subset-deploys) |
| Preview without creating anything | `DRY_RUN=1 deploy/deploy.sh` (packages and synthesizes, then stops before the bootstrap check or the deploy; needs valid credentials) |
| Invoke one agent, with pretty SSE events | `deploy/invoke.sh hello 'List the files and report.'` |
| Invoke with a full JSON payload | `deploy/invoke.sh discovery @payload.json --out events.jsonl` |
| Invoke with the AgentCore CLI (on stage) | `deploy/agentcore.sh invoke --runtime hello "List the files and report."` |
| Status | `deploy/status.sh` (`agentcore status` plus `GetAgentRuntime` for every ARN) |
| Logs | `deploy/logs.sh terraform` (last 30 min), `deploy/logs.sh terraform --since 2h --level error` |
| Traces | `deploy/logs.sh discovery traces`, `deploy/logs.sh discovery trace <traceId>`, or the CloudWatch console → GenAI Observability → Bedrock AgentCore |
| Any CLI command | `deploy/agentcore.sh <args>`, for example `deploy/agentcore.sh traces list --runtime finops` |
| Remove some agents | `deploy/destroy.sh terraform finops` (shows the plan and asks first) |
| Remove everything | `deploy/destroy.sh` (or `make destroy-agents`); `AUTO_APPROVE=1` skips the prompt |

Notes:

- <a id="subset-deploys"></a>**Subset deploys.** All runtimes live in one CloudFormation stack, so a runtime that
  is missing from `agentcore.json` is deleted. `deploy.sh hello discovery` therefore deploys those two *plus* every
  agent that is already deployed. To find those, `prepare` reads the outputs of the stack
  `AgentCore-TransformWorkshop-default` (read-only `DescribeStacks`). If that call fails, it uses the CLI state in
  `agentcore/.cli/deployed-state.json`. If neither is available (for example a fresh clone whose credentials may not call `DescribeStacks`),
  the subset deploy is **refused** with an explanation. Then either deploy everything (`deploy.sh --all`), or set
  `FORCE_SUBSET=1` to deploy only the named agents; CloudFormation then deletes any other runtime in the stack.
  Use `destroy.sh <stage>` to remove an agent.
- **Every deploy repackages and re-uploads all configured agents.** The CLI's zips embed timestamps, so their hashes
  always change: about 470 MB is uploaded and every runtime gets a new version. Expect 1–3 minutes of packaging plus
  the upload and CloudFormation time. Deploy before the session, not over venue Wi-Fi. A build also leaves about
  2.5 GB of gitignored staging in `deploy/agentcore/.cache` and `deploy/agentcore/cdk/cdk.out`; you can delete it.
- **`agentcore invoke` output.** `agentcore invoke` always sends `{"prompt": "…"}`, and the agents accept that as the
  instruction. The CLI only renders plain-text SSE, so it prints our structured events as raw `data:` lines.
  `deploy/invoke.sh` renders them (status, tool calls, artifacts, result) and accepts the full request contract
  (`files`, `inputs`, `options`).

## Teardown

`destroy.sh` does not rely on local files. It looks up the stack `AgentCore-TransformWorkshop-default` (read-only),
prints what it will delete, and asks `[y/N]` before changing anything. Set `AUTO_APPROVE=1` to skip the prompt; it is
required when stdin is not a terminal.

- `deploy/destroy.sh` lists the runtimes and the stack resources by type, then deletes the whole stack
  (`aws cloudformation delete-stack`, then waits for it to finish, a few minutes). It then clears the local
  record: the CLI state target, the runtimes in `agentcore.json`, `app/` and `agent-arns.json`. It works the same from
  a fresh clone.
- `deploy/destroy.sh terraform finops` shows which runtimes go and which stay, then redeploys exactly the remaining
  ones. If you name every deployed runtime, the whole stack is deleted instead.
- It refuses while the stack is `*_IN_PROGRESS`. It also warns when `aws-targets.json` names a different account or
  region than your current credentials, so set `AWS_PROFILE` and `AWS_REGION` to the ones you deployed with.
- **Kept:** the `CDKToolkit` bootstrap stack, the runtime log groups and the Transaction Search setting. For a full
  cleanup, delete the agents' stack first. Then empty the versioned bucket `cdk-hnb659fds-assets-<account-id>-us-west-2`
  and run `aws cloudformation delete-stack --stack-name CDKToolkit`. The log groups are
  `/aws/bedrock-agentcore/runtimes/*`.

## What gets created

One CloudFormation stack, `AgentCore-TransformWorkshop-default`, holds the following for each stage:

- `AWS::BedrockAgentCore::Runtime` named `TransformWorkshop_<stage>`: CodeZip, `PYTHON_3_12`, entry point
  `["opentelemetry-instrument", "main.py"]`, `PUBLIC` network, idle session timeout 600 s, max lifetime 3600 s,
  and the tags `workshop=transform-agentcore` and `stage=<stage>`.
- An IAM execution role (trusted by `bedrock-agentcore.amazonaws.com`), the CDK default policy, and the inline policy
  from `policies/runtime-extras.json`.
- The runtime's log group `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`. The service creates it on first use.

The zips go to the CDK asset bucket. The synth on 2026-10-20 produced these sizes; the limits are 250 MB zipped and
750 MB unzipped:

| Agent | Zip | Unzipped |
|---|---|---|
| hello, discovery, requirements, finops | 61 MB | 174 MB |
| modernization (+ uv) | 80 MB | 217 MB |
| k8s_validation (+ kubeconform, kubectl) | 84 MB | 244 MB |
| terraform (with `VENDOR_TERRAFORM=1`) | 92 MB | 282 MB |

## IAM execution role

Each runtime role gets the statements of the CDK construct (`@aws/agentcore-cdk` `AgentCoreRuntime`):

- `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` and `bedrock:CountTokens` on
  `arn:aws:bedrock:*::foundation-model/*` and `arn:aws:bedrock:*:<account>:inference-profile/*`. This covers the
  `us.` cross-region profiles, which route to us-east-1, us-east-2 and us-west-2. It is deliberately not narrowed to
  the default model ids, so `MODEL_<STAGE>` overrides keep working.
- `logs:CreateLogGroup/CreateLogStream/PutLogEvents/DescribeLogStreams/GetLogEvents/FilterLogEvents/PutResourcePolicy`
  on `/aws/bedrock-agentcore/runtimes/*`, plus `logs:DescribeLogGroups`.
- `xray:PutTraceSegments` and `xray:PutTelemetryRecords`.
- AgentCore configuration-bundle actions, scoped to `configuration-bundle/*`. The CLI adds these; the agents do not
  use them.

On top of that, `policies/runtime-extras.json` adds:

| Sid | Actions | Why |
|---|---|---|
| AgentReadOnlyPricing | `pricing:GetProducts`, `DescribeServices`, `GetAttributeValues`; `savingsplans:DescribeSavingsPlansOfferingRates`; `eks:DescribeClusterVersions` | FinOps live prices (`options.live_pricing`) |
| AgentReadOnlyEc2Inventory | `ec2:DescribeInstances`, `DescribeVolumes` (Discovery `live_aws`); `ec2:DescribeInstanceTypes`, `DescribeSpotPriceHistory` (FinOps live prices) | Exactly the calls the agents make. Not `ec2:Describe*`, which includes `DescribeInstanceAttribute` and can read EC2 user data. |
| AgentReadOnlyCloudWatchMetrics | `cloudwatch:GetMetricStatistics`, `GetMetricData`, `ListMetrics` | Discovery utilization |
| ObservabilitySampling | `xray:GetSamplingRules`, `GetSamplingTargets` | ADOT sampling (AWS-documented runtime role) |
| ObservabilityMetrics | `cloudwatch:PutMetricData` limited to namespace `bedrock-agentcore` | AgentCore runtime metrics |

The agents never create AWS resources. Everything they are granted is read-only, apart from telemetry.
`tests/test_deploy.py` scans `transform_agents/` for boto3 calls and fails when one is not granted here.

## Environment variables

`deploy_tool.py prepare` copies these variables from your shell into the runtime configuration (`envVars`) at deploy
time:

- `MODEL_ID`, `MODEL_MAX_TOKENS`, and `MODEL_<STAGE>` (into that stage only). Example:
  `MODEL_TERRAFORM=us.amazon.nova-2-lite-v1:0 deploy/deploy.sh terraform`.
- `TERRAFORM_OFFLINE` (terraform stage only).
- Fixed: `TF_WORKSHOP_CACHE=/tmp/transform-workshop/terraform` for the terraform stage, because the code directory and
  `~` are not writable.

`AWS_REGION` and `AWS_DEFAULT_REGION` are not runtime properties. `main.py` sets them with `os.environ.setdefault`
(the deploy region baked in at build time) before the agent is imported, so a value the platform injects always
wins. Changing an environment variable means redeploying (`deploy.sh <stage>`).

## Tool binaries (kubeconform, kubectl, uv, terraform)

All binaries are linux/arm64, pinned in `deploy_tool.py` `BINARIES`, sha256-verified against the upstream checksum,
and cached in `deploy/.cache/bin`.

- **kubeconform 0.8.0** (13 MB) and **kubectl 1.33.13** (57 MB) are vendored into `k8s_validation` by default.
  - kubeconform downloads its schemas from GitHub at run time; `PUBLIC` network mode allows this. If the download
    fails, the agent falls back to built-in checks.
  - kubectl is only used for `kubectl kustomize`, which runs offline, so the overlays render with their patches as they
    do locally. There is no cluster in the cloud: `options.live_deploy` stays local-only.
- **uv 0.12.15** (`uv-aarch64-unknown-linux-gnu`, 43 MB) is vendored into `modernization`. `run_service_tests`
  finds it with `shutil.which("uv")` and runs the generated microservice's pytest in an isolated environment
  (`uv run --no-project --with-requirements requirements-dev.txt`). Its wheels (Flask, SQLAlchemy, pytest) come from
  PyPI at run time over the `PUBLIC` network; the agent starts this download in the background at the start of a run.
  The generated `main.py` sets `UV_CACHE_DIR=/tmp/uv-cache` and `UV_PYTHON_INSTALL_DIR=/tmp/uv-python`, because the
  code directory and `~` are read-only. A runtime `envVar` with the same name still wins.
- **terraform 1.15.4** (linux/arm64, 108 MB) is **opt-in**: `VENDOR_TERRAFORM=1 deploy/deploy.sh terraform`. Without
  it, the terraform agent validates offline with python-hcl2, module and resource schema, reference and policy checks.
  That is how it is designed to run on AgentCore. With the binary it runs a real `terraform init` and
  `terraform validate`, but every new runtime session starts cold and downloads the EKS module and the AWS provider,
  which takes minutes per session. That is too slow for the live demo.
- **The executable bit.** The CLI's zip does not preserve Unix permissions, and the code directory is read-only. So
  `main.py` copies `vendor/bin/*` to `/tmp/vendor-bin`, runs `chmod 755` and prepends that directory to `PATH`. The
  agents locate tools with `shutil.which`, so they find them there. Set `VENDOR_BIN_DIR` to use another directory.
  `NO_BINARIES=1` skips vendoring altogether; the modernization service-test check then reports `skipped` in the
  cloud.

## Verifying without deploying

| Check | Command | Result on 2026-10-20 |
|---|---|---|
| Unit tests (no AWS) | `uv run pytest tests/test_deploy.py` | 45 passed |
| Config schema | `deploy/agentcore.sh validate` | Valid |
| Package and synth all agents (read-only AWS calls: STS and DescribeStacks) | `DRY_RUN=1 deploy/deploy.sh` | Validate, dependency sync, CDK build and synth pass for all 7 runtimes. It stops at "AWS environment needs bootstrapping", so nothing is created. Needs valid credentials. |
| The same without credentials | `uv run python deploy/deploy_tool.py prepare --account 123456789012`, `deploy/agentcore.sh package -d .`, then `cd deploy/agentcore/cdk && npm run build && node_modules/.bin/cdk synth --quiet` | 7 zips (61–84 MB) and a template with 7 runtimes and the narrowed IAM policy |
| Package is self-contained | `deploy/smoke-local.sh hello` | The CLI-built zip runs in a read-only linux/arm64 `python:3.12-slim` container as `opentelemetry-instrument python main.py`. `/ping` returns Healthy. `/invocations` streams the full event contract from Bedrock in about 5 s, and 9 or more OTel spans are emitted. |
| All 7 runtimes import from a synthesized zip | linux/arm64 container, read-only `/var/task` | each `transform_agents.<stage>.runtime:app` imports with the pinned dependencies only |
| uv runs the modernization service tests from the zip | `NO_INVOKE=1 deploy/smoke-local.sh modernization`, then `run_service_tests` on the worked example in the same container | `uv` resolves to `/tmp/vendor-bin/uv` (0.12.15, aarch64-unknown-linux-gnu) with `~` read-only. The runner is `uv`: 20 passed, API parity complete, 2.0 s cold (wheels from PyPI) and 1.1 s warm. |
| Vendored tools run from the zip | `deploy/smoke-local.sh k8s_validation @payload.json` | kubeconform validates the base and the overlay (7 then 8 resources valid, overlays rendered by kubectl), the result is `passed: true`, and 68 spans are emitted. `terraform version` → v1.15.4 when vendored. |

`smoke-local.sh` calls Bedrock with your credentials, which are exported into the container for that run. Use
`NO_INVOKE=1` to check `/ping` and the vendored tools only, without credentials.

## How the UI and orchestrator use the ARNs

`deploy/agent-arns.json` maps each stage to its runtime ARN, in pipeline order:

```json
{
  "hello": "arn:aws:bedrock-agentcore:us-west-2:<account-id>:runtime/TransformWorkshop_hello-XXXXXXXXXX",
  "discovery": "arn:aws:bedrock-agentcore:us-west-2:<account-id>:runtime/TransformWorkshop_discovery-XXXXXXXXXX"
}
```

`transform_agents/orchestrator/clients.py` (`AgentCoreClient`) loads this file; `AGENTCORE_ARN_<STAGE>` overrides one
entry. The client calls `bedrock-agentcore:InvokeAgentRuntime` with `qualifier=DEFAULT` and the same request payload
as local mode, then parses the SSE stream into the same events. To use it, pick **☁️ AgentCore Runtime** in the UI, or
run `python -m transform_agents.orchestrator --mode agentcore`. The caller needs
`bedrock-agentcore:InvokeAgentRuntime` on those ARNs. The file is rewritten after every deploy or destroy.
`uv run python deploy/deploy_tool.py arns` regenerates it from the CLI state, or from the stack outputs on a fresh
clone.

## Cost

AgentCore Runtime is consumption-based. Billing is per second: CPU only while code is running (I/O wait, such as
waiting on Bedrock, is free), and memory for the session. List rates are about $0.09–0.13 per vCPU-hour and
$0.009–0.017 per GB-hour; check the AgentCore pricing page for your region.

A full six-stage pipeline run comes to well under $0.10 of Runtime, plus a few cents of Bedrock tokens
(roughly 100–200k tokens). Deployed agents that nobody invokes cost nothing. The 600 s idle session timeout caps how
long a finished session keeps its memory.

Fixed costs are negligible: about 0.5 GB of zips in S3 (≈ $0.01 a month), CloudWatch log and span ingestion
(cents), and an empty CDKToolkit stack.

## Troubleshooting

- **"AWS environment needs bootstrapping".** This only appears with `--dry-run` or without `--yes`. `deploy.sh` passes
  `--yes`, which bootstraps automatically.
- **Expired SSO token.** Run `aws sso login --profile <your-profile>`.
- **Stack stuck in a failed state.** Look at the CloudFormation console events for
  `AgentCore-TransformWorkshop-default`. `deploy.sh` refuses to deploy while the stack is in `*_IN_PROGRESS` or
  `*_FAILED`.
- **"refusing a subset deploy".** `deploy.sh <stage>` could not find out what is deployed: the CloudFormation lookup
  failed and there is no local CLI state. Fix the credentials or region and retry, or use `deploy.sh --all`. If you
  are sure, `FORCE_SUBSET=1 deploy/deploy.sh <stage>` deploys only that stage.
- **Lost `deploy/agentcore/.cli/deployed-state.json`** (another laptop, a fresh clone, `git clean -X`). It is
  gitignored because it holds the account id and the ARNs. The scripts read the stack instead:
  `deploy.sh` (all agents) updates the existing stack in place, and the CLI writes its state again. `destroy.sh`
  finds the stack by name, and `deploy_tool.py arns` rebuilds `agent-arns.json` from the stack outputs.
  `agentcore status/logs/traces` only work again after one `deploy.sh`.
- **An agent errors only in the cloud.** Run `deploy/logs.sh <stage> --level error`, then
  `deploy/smoke-local.sh <stage>` to reproduce with the exact zip.
- **A stage is missing from the deploy.** Its `transform_agents/<stage>/runtime.py` does not exist or does not define
  `app`; the warning names it.

## Why the AgentCore CLI rather than Terraform

**AgentCore CLI (chosen).**

- It is what the presenter shows on stage: `agentcore status`, `invoke`, `logs` and `traces` work against these
  runtimes with no extra wiring, because the CLI owns the deployed state.
- It packages the arm64 zips with AWS's own packager, including manylinux fallbacks and the 250 MB check.
- It creates the IAM roles, enables Transaction Search and bootstraps CDK.
- The extra ceremony is small: a generated `agentcore.json` with one CodeZip runtime per stage, plus a build step that
  copies our package and pinned dependencies into each code location.
- The cost is a CDK bootstrap stack, and a CloudFormation stack that redeploys every runtime on each deploy, because
  the zips are not byte-reproducible.

**Terraform (`aws_bedrockagentcore_agent_runtime`, AWS provider ≥ 6.22 for a `code_configuration` zip on
`PYTHON_3_12`).**

- It would need our own packaging script, S3 bucket, IAM role and observability wiring.
- `agentcore logs/traces/invoke` would not know about the runtimes.
- It would give per-runtime targeting and byte-stable zips.
- The same `app/<stage>/` directories and `policies/runtime-extras.json` would carry over unchanged if that is ever
  needed.
