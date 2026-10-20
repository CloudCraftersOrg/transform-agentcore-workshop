# Agents spec

> Last verified: 2026-10-20 · Scope: transform_agents/core and the six stage agents (+ hello)

## 1. Purpose & scope

This spec covers the shared core ([transform_agents/core/](../../transform_agents/core/)) and the agents built on it:
discovery, requirements, modernization, terraform, k8s_validation, finops, plus the `hello` reference agent. Use it to
change a prompt, model, tool, knowledge file or budget, or to add a stage. Out of scope: orchestrator, clients and
deployment ([01-architecture.md](01-architecture.md)), UI (03-ui.md), operations (04-operations.md), and talk numbers
and recording ([05-demo-content.md](05-demo-content.md)). Per-stage I/O is normative in [docs/CONTRACTS.md](../CONTRACTS.md).
Design in one line: Python collects facts, renders curated knowledge, validates and counts; the model judges, adapts
and explains; every stage runs the model loop under a deadline and finishes deterministically.

## 2. Invariants

1. Every `handle(request)` MUST end with exactly one `result` or `error` event, and nothing after it. Stage agents drop
   `run_agent`'s own `result`/`artifact` events, finalize, then re-emit artifacts and one `result`.
2. `result.data` MUST follow [docs/CONTRACTS.md](../CONTRACTS.md) (required fields never renamed or dropped). Its facts
   MUST come from deterministic state (tool results, validators, parsed code), never from model claims.
3. Models MUST be Amazon Nova, Mistral, DeepSeek, Meta Llama 3.x or Amazon Titan through Bedrock Converse
   (`BedrockModel`). Anthropic and OpenAI MUST NOT be used (a user decision). Ids live only in `DEFAULT_MODELS`.
4. Generating agents MUST start from curated `knowledge/` (blueprint, pattern, worked example) and loop on deterministic
   validators. They MUST NOT ask a model for platform code or EKS Terraform from scratch.
5. Numbers MUST come from Python tools or the inputs, never from the model (finops `unverified_numbers`, requirements
   `derive()`, discovery `assemble()`).
6. Agents MUST NOT force `tool_choice` or use Strands `structured_output`. Results arrive through a `submit_result`
   tool into a `ResultBox`, with `extract_json` on the final text as the last resort.
7. Tools MUST run sequentially (`SequentialToolExecutor()` in `run_agent`). Agents MUST NOT build a Strands `Agent` or
   `BedrockModel` outside `run_agent`/`build_model`.
8. Agents MUST NOT create or modify AWS resources. Only read-only calls (EC2/CloudWatch describe/get, Pricing, Savings
   Plans, EKS DescribeClusterVersions) plus Bedrock inference, degrading to "skipped" without credentials.
9. Every subprocess MUST have a timeout and an argv list; `shell=True` MUST NOT be used. Model output reaches a
   subprocess only as files or argv elements. Model-written code runs in its own process group, killed on timeout.
10. Workspace paths MUST pass `Workspace.normalize` (no absolute paths, no `..`). Writers MUST enforce the write scope:
    modernization `microservice/` and `k8s/` minus `LOCKED_FILES`; k8s_validation `k8s/` only.
11. Streamed text MUST pass `TextScrubber` and final text `LEAKED_MARKER_RE`, so DeepSeek `<｜DSML｜…>` markers never
    reach the UI. Tool calls written as text are replayed visibly (terraform `leaked_call`).
12. Model-written tests MUST run without cloud credentials: `clean_env()` allowlist, throwaway `HOME`, SQLite.
13. The same code MUST work in-process and on AgentCore Runtime: inputs from the request, knowledge from
    `Path(__file__).parent / "knowledge"`, repo-relative reads only as the documented fallbacks (§3.6), missing
    binaries degrade instead of failing.
14. Time budgets (§4) MUST hold: a deadline guards every model loop, and a deterministic finish runs after it.
15. A model summary MUST NOT hide a failed validator (modernization `WARNING: validation incomplete`, terraform verdict
    first, k8s_validation fallback summary).

## 3. How the shared core works

### 3.1 Contract ([contract.py](../../transform_agents/core/contract.py))
`AgentRequest{run_id, instruction, feedback, inputs, files, options}`. `from_payload` accepts `prompt` as an alias of
`instruction` (the `agentcore invoke` shape), requires `files` to map str to str, and raises `ValueError` otherwise.
Helpers: `status()`, `error()`, `preview()` (400 chars). Events: `status | text | tool_start | tool_end | artifact |
result | error`. Revision = non-empty `feedback`; the orchestrator then adds the previous artifacts to `files` and the
previous data as `inputs.previous`.

### 3.2 Workspace ([workspace.py](../../transform_agents/core/workspace.py))
An in-memory `{path: text}` map. `write()` records changed paths and `artifacts()` returns only those, so a stage ships
only what it wrote. An `RLock` guards read-modify-write. `materialize(prefix)` copies a subtree to a temp dir for CLI
validators (the caller deletes it). `workspace_tools(ws, writable=True)` gives `list_files` and `read_file`, plus
`write_file` and `edit_file` (the snippet must occur exactly once) when writable.

### 3.3 ResultBox and submit tools ([runner.py](../../transform_agents/core/runner.py))
`ResultBox.set(summary, data)` stores the result and tells the model to reply in one line and stop. `json_submit_tool`
(`submit_result(summary, result_json)`) is used only by `hello`. Stage agents define validated `submit_result` tools
that refuse once (discovery up to 3 times) with fixable errors, then accept and record the problems in `data`.
Discovery, requirements and terraform also set `request_state["stop_event_loop"]` via `ToolContext`, saving a turn.

### 3.4 `run_agent` event mapping
`status("agent started", model)` → Strands `data` deltas become `text` (scrubbed) → `toolUse` becomes `tool_start`
(string args previewed at 80 chars) → `toolResult` becomes `tool_end` → one `artifact` per changed file → `result{summary,
data, artifacts, metrics{model, latency_s, input_tokens, output_tokens, cycles, stop_reason, submitted}}`. `data` is
`box.data`, else `extract_json(final_text)`, else `{}`. An exception in the stream becomes one `error`. `run_agent`
sets no deadline; each stage adds its own (§4).

### 3.5 AgentCore wrapper ([app.py](../../transform_agents/core/app.py), `<stage>/runtime.py`)
`make_app(stage, handle)` registers a `BedrockAgentCoreApp` entrypoint; a bad payload becomes `error("bad request: …")`, and an
exception from `handle()` becomes one final `error` event (unless a terminal event was already sent). Each `runtime.py` is the same 9-line shim (`python -m transform_agents.<stage>.runtime`
serves `POST /invocations` on :8080). [deploy_tool.py](../../deploy/deploy_tool.py) deploys every stage whose
`runtime.py` assigns `app`, plus `hello`.

### 3.6 Models, region and local fallbacks ([config.py](../../transform_agents/core/config.py))
- `region()`: `AWS_REGION` > `AWS_DEFAULT_REGION` > `us-west-2`. `model_id_for(stage)`: `MODEL_<STAGE>` > `MODEL_ID` >
  `DEFAULT_MODELS[stage]`.
- `build_model(stage, max_tokens=8192)`: streaming; `MODEL_MAX_TOKENS` overrides every stage (modernization's 12000
  too); `MODEL_READ_TIMEOUT` (default 120 s); connect 10 s; 4 adaptive retries. Deploys pass `MODEL_ID`,
  `MODEL_MAX_TOKENS` and `MODEL_<STAGE>` through to the runtime env.
- Documented repo-relative fallbacks (absent in the runtime zip, which ships only `transform_agents/`): discovery
  `tools.load_legacy_estate()` → `legacy-infra/`; requirements `load_sample_requirements()` → `samples/requirements/`;
  modernization `load_bundled_legacy()` → `legacy-infra/app`; k8s_validation `find_binary()`/`kubeconform_cache()` →
  `.tools/`, else `PATH` and the temp dir.

## 4. Per-agent sheet

Links point into `transform_agents/<stage>/`. Test counts are from `uv run pytest --collect-only` (411 unit and 7 live
in total).

### 4.1 discovery ([agent.py](../../transform_agents/discovery/agent.py), [tools.py](../../transform_agents/discovery/tools.py), [codescan.py](../../transform_agents/discovery/codescan.py), [infra.py](../../transform_agents/discovery/infra.py))
| Aspect | Detail |
|---|---|
| Purpose / analogue | Inventory the estate. The model assigns severities to curated findings and picks the 6R strategy, readiness score and narrative. Analogue: AWS Transform discovery and assessment. |
| Default model | `us.amazon.nova-2-lite-v1:0` |
| In → out | `files` `legacy/app/**`, `legacy/terraform/**` → [§1 data](../CONTRACTS.md#1-discovery--discovery--assessment); `reports/assessment.md` (model narrative + tables appended after `APPENDIX_MARKER` by `finalize_report`) |
| Tools to model | `write_file`, `edit_file` (+ `read_file`, `list_files` on revisions), `scan_codebase`, `parse_legacy_terraform`, `instance_specs`, `get_utilization`, `describe_live_instance` (only with `live_aws`), `submit_result` |
| Knowledge | [knowledge/assessment_rules.yaml](../../transform_agents/discovery/knowledge/assessment_rules.yaml) (LF-01…LF-17: key, category, title, recommendation, `fallback_severity`); curated tables `LATEST`, `INSTANCE_SPECS`, `SYSTEM_PYTHON`, `ESTIMATE` |
| Budgets | `TIMEOUT_S = 60` (`with_deadline`); `MAX_SUBMIT_ATTEMPTS = 3`; boto3 connect 5 s, read 15 s, 2 attempts |
| Fallbacks | Timeout or no submit → `fallback_assessment()` (YAML severities, replatform, score 100 − 10/5/2/1 per critical/high/medium/low). A model or API `error` ends the stage. |
| Binaries / AWS | None. Read-only `ec2:DescribeInstances`, `DescribeVolumes`, `cloudwatch:GetMetricStatistics` (14 days) |
| Options | `live_aws`, `region`, `timeout_s` |
| Tests | [test_discovery.py](../../tests/test_discovery.py): 20 unit, plus live `test_discovery_live_end_to_end` (< 90 s) |

### 4.2 requirements ([agent.py](../../transform_agents/requirements/agent.py), [tools.py](../../transform_agents/requirements/tools.py))
| Aspect | Detail |
|---|---|
| Purpose / analogue | Turn the uploaded document into the validated migration spec every later stage reads. Analogue: job objective intake plus HITL clarifications. |
| Default model | `us.amazon.nova-2-lite-v1:0` |
| In → out | `files` `requirements/*`, `inputs.assessment` → [§2 spec](../CONTRACTS.md#2-requirements--requirements-analysis) (also `gaps`; `validation_problems` when accepted invalid); `reports/migration-spec.md` |
| Tools to model | `write_file`, `edit_file` (+ read and list on revisions), `read_requirements`, `get_spec_template`, `set_fields`, `set_lists`, `resolve_question`, `check_spec`, `submit_result(summary)`. The draft lives in `RequirementsContext`, and the model sends small deltas (Nova malforms large nested inputs). |
| Knowledge | No files. The schema is in code: `FIELDS` (35 scalars), `DEFAULTS` (k8s `"1.33"`, postgres `"16"`, port 8080, `/healthz`, `/readyz`, 70 %, `prod`), `ENUMS`, `SYNONYMS`, rules in `check_spec()` |
| Budgets | `TIMEOUT_S = 90`, shared by both passes; one resume (`RESUME_PROMPT`) only if under half the budget was used; `MAX_CHECKS = 8`; the 3rd submit or 8th check is the last chance |
| Fallbacks | Valid draft without a submit → the draft is used. Otherwise `error`; there is no deterministic spec. |
| Binaries / AWS | None |
| Options | `timeout_s` |
| Tests | [test_requirements.py](../../tests/test_requirements.py): 42 unit, plus live `test_requirements_live_end_to_end` (< 90 s) |

### 4.3 modernization ([agent.py](../../transform_agents/modernization/agent.py), [tools.py](../../transform_agents/modernization/tools.py), [pattern.py](../../transform_agents/modernization/pattern.py), [checks.py](../../transform_agents/modernization/checks.py))
| Aspect | Detail |
|---|---|
| Purpose / analogue | Port the Flask monolith into a 12-factor microservice plus Kustomize manifests by adapting a tested pattern. The model writes only `app/api.py` and `tests/test_api.py` (and fixes `report.py`). Analogue: code transformation and containerization. |
| Default model | `deepseek.v3.2` (`max_tokens=12000`) |
| In → out | `files` `legacy/app/**`, `inputs.spec`, `inputs.assessment` → [§3 data](../CONTRACTS.md#3-modernization--code-modernization), built by `build_data()` from validator results; artifacts `microservice/**`, `k8s/**` only |
| Tools to model | `list_files`, `read_file`; guarded `write_file`/`edit_file`; `scaffold_from_pattern` (no arguments; the override variant only with feedback); `run_service_tests`; `validate_manifests`; `submit_result(summary, changes_json)`; `docker_build_check` (only with `docker_build`) |
| Knowledge | [knowledge/pattern/](../../transform_agents/modernization/knowledge/pattern/) (`{{placeholder}}` = `Params` field; `substitute()` raises on unknown); [knowledge/example/](../../transform_agents/modernization/knowledge/example/) (BEFORE `legacy_app.py`, AFTER `api.py` + `test_api.py`, shown via `example_text()`); `PLATFORM_CHANGES`, `HELPERS` |
| Budgets | `TIME_BUDGET_S = 150` (env `MODERNIZATION_TIME_BUDGET_S`); the model loop gets budget − min(30, budget/4); `CONTINUE_MIN_S 40`; `MAX_PASSES 2`; `SOFT_LIMIT_S 90` and `SUBMIT_MARGIN_S 20` (the tools ask for a submit); `MAX_TEST_RUNS 4`; `TEST_TIMEOUT_S 90`; `KUBECTL_TIMEOUT_S 30`; `MIN_CHECK_S 5`; prewarm 300 s off the clock; docker build ≤ 600 s, run 60 s |
| Fallbacks | Continuation pass. `finalize()` scaffolds if the model never did and re-runs stale tests and manifests within the remaining budget. `RunState.closed` blocks late tool threads. `metrics.fallback` is set when there was no submit. |
| Binaries / AWS | `uv` (Python 3.12 env from `requirements-dev.txt`; else the current interpreter if flask, sqlalchemy and pytest import), `kubectl kustomize` (skipped if missing), `docker` (optional) |
| Options | `time_budget_s`, `docker_build` |
| Tests | [test_modernization.py](../../tests/test_modernization.py): 26 unit (pattern + example `"20 passed"`, credential-free env, process-group kill); live [test_modernization_live.py](../../tests/test_modernization_live.py): agent run, plus `test_generated_image_builds_and_serves` (Docker only) |

### 4.4 terraform ([agent.py](../../transform_agents/terraform/agent.py), [tools.py](../../transform_agents/terraform/tools.py), [validate.py](../../transform_agents/terraform/validate.py), [summary.py](../../transform_agents/terraform/summary.py))
| Aspect | Detail |
|---|---|
| Purpose / analogue | Map the spec onto a validated VPC + EKS + ECR + RDS blueprint, then loop on a layered validator. Analogue: network and infrastructure conversion to IaC. |
| Default model | `us.amazon.nova-2-lite-v1:0`. Mistral Large 3 via `MODEL_TERRAFORM`; its text-written calls are replayed. |
| In → out | `inputs.spec`, `inputs.modernization`, `inputs.assessment` → [§4 data](../CONTRACTS.md#4-terraform--target-infrastructure-as-code) built by `TerraformRun.finalize()` from the code (adds `spec_checks`, `validation_level`, …); `terraform/**` (+ regenerated `terraform.tfvars.example`) |
| Tools to model | `list_files`, `read_file`, `write_file`, `edit_file`, `get_blueprint`, `get_module_inputs` (offline schema), `set_variables` (edits `variables.tf` defaults), `terraform_validate`, `submit_result(summary, spec_coverage)` |
| Knowledge | [knowledge/blueprint/](../../transform_agents/terraform/knowledge/blueprint/), [module-inputs.json](../../transform_agents/terraform/knowledge/module-inputs.json), [extract_module_inputs.py](../../transform_agents/terraform/knowledge/extract_module_inputs.py). The mapping rules are in `SYSTEM_PROMPT` and mirrored in `summary.expected_values()`. |
| Budgets | `DEADLINE_S 100`; `SILENCE_S 45`; `MAX_PROSE_CHARS 2500`; `MAX_RUNS 3` started within `CONTINUE_WITHIN_S 50`; `max_validations` 4 (hard stop at +2); `INIT_TIMEOUT_S 240`, `VALIDATE_TIMEOUT_S 120`, fmt 60 s |
| Fallbacks | Follow-up runs via `_continuation()`. A late model error still yields a `result` if the Terraform is valid and matches the spec. With no submit, the summary leads with the verdict and coverage comes from the passing spec checks. A failed `terraform init` drops to level `offline`. |
| Binaries / AWS | `terraform` optional (`TERRAFORM_BIN`; `TERRAFORM_OFFLINE=1`; cache `TF_WORKSHOP_CACHE`). Only `ALLOWED_MODULE_SOURCES` and `hashicorp/*` providers reach `init`. |
| Options | `max_validations` |
| Tests | [test_terraform.py](../../tests/test_terraform.py): 48 unit; live [test_terraform_live.py](../../tests/test_terraform_live.py) (< 90 s, ≤ 4 validate attempts) |

### 4.5 k8s_validation ([agent.py](../../transform_agents/k8s_validation/agent.py), [tools.py](../../transform_agents/k8s_validation/tools.py), [policy.py](../../transform_agents/k8s_validation/policy.py), [kube.py](../../transform_agents/k8s_validation/kube.py), [deploy.py](../../transform_agents/k8s_validation/deploy.py), [drift.py](../../transform_agents/k8s_validation/drift.py))
| Aspect | Detail |
|---|---|
| Purpose / analogue | Deterministic preflight (kustomize render, kubeconform, POL rules). The model patches failures, then an optional kind deploy with smoke tests. Analogue: test and validation. |
| Default model | `us.amazon.nova-2-lite-v1:0` |
| In → out | `files` `microservice/**`, `k8s/**`; `inputs.spec`, `inputs.modernization` → [§5 data](../CONTRACTS.md#5-k8s_validation--kubernetes-validation) from `report.build_data()`; `reports/validation-report.md` plus changed `k8s/**` (all `k8s/**` on a revision) |
| Tools to model | `list_files`, `read_file`, `write_file`/`edit_file` (restricted to `k8s/`), `render_manifests`, `kubeconform_check`, `policy_check`, `deploy_to_kind` (only with `live_deploy`), `submit_result(summary, fixes, notes)` |
| Knowledge | [knowledge/policy-baseline.md](../../transform_agents/k8s_validation/knowledge/policy-baseline.md) is documentation, not loaded at runtime; a test enforces that it lists every rule. Rules live in `policy.RULES` and `check_policies()`. |
| Budgets | `TIME_BUDGET_S 150`; `HARD_TOOL_CALLS 45` (soft `MAX_TOOL_CALLS 30`); `MAX_DEPLOYS 3`; `BUILD_WAIT_S 150`; deploy 75 s; docker build 240 s; kind load 120 s; kustomize 30 s; kubeconform 25 s |
| Fallbacks | Regressions are reverted (`_regressed`, `_revert_k8s`). The finish re-validates and deploys if stale. Unfixed simulated drift ships the pristine files (`_undrift`). No kubectl → `raw_render`; no kubeconform or network → `builtin_schema_check` (warn). |
| Binaries / AWS | `kubectl`, `kubeconform`; `docker` + `kind` for the deploy. `find_binary` prefers `.tools/bin`. No AWS. |
| Options | `live_deploy`, `kind_cluster`, `simulate_drift`, `time_budget_s` |
| Tests | [test_k8s_validation.py](../../tests/test_k8s_validation.py): 53 unit; live [test_k8s_validation_live.py](../../tests/test_k8s_validation_live.py) (kind, < 120 s) |

### 4.6 finops ([agent.py](../../transform_agents/finops/agent.py), [tools.py](../../transform_agents/finops/tools.py), [cost_model.py](../../transform_agents/finops/cost_model.py), [pricing.py](../../transform_agents/finops/pricing.py))
| Aspect | Detail |
|---|---|
| Purpose / analogue | Price current vs target as designed vs optimized, right-size pods and nodes, check the budget. The model picks optimizations and writes the value case. Analogue: assessment TCO and business case. |
| Default model | `us.amazon.nova-2-lite-v1:0` |
| In → out | `inputs.{assessment, spec, modernization, terraform, k8s_validation}`, `files` `k8s/**`, `terraform/**` (an empty request works on defaults) → [§6 data](../CONTRACTS.md#6-finops--right-sizing-and-cost); `reports/finops-report.md` |
| Tools to model | `get_prices`, `parse_k8s_resources`, `parse_terraform_sizing`, `rightsize_pods`, `node_fit`, `estimate_cost`, `compare_scenarios` (≤ `MAX_COMPARES 3`), `submit_result(summary, value_case, recommendations_json, risks)`. No workspace tools. |
| Knowledge | [knowledge/prices-us-west-2.json](../../transform_agents/finops/knowledge/prices-us-west-2.json), [refresh_prices.py](../../transform_agents/finops/knowledge/refresh_prices.py); `cm.CATALOG` (8 prod-safe optimizations), `REJECTED_IN_PROD`, `HOURS 730`, `EGRESS_GB 50`, `NONPROD_HOURS 220` |
| Budgets | `TIME_BUDGET_S 55`; 60 s per live-price future |
| Fallbacks | Timeout, error or no submit → `finalize(fallback=True)` with every catalog optimization (`metrics.fallback`). A narrative with numbers no tool produced is refused once, then flagged in `data.unverified_numbers`. FO ids are assigned in Python, with the extended-support alert first. |
| Binaries / AWS | None. Read-only Pricing (us-east-1), Savings Plans rates, EC2 `DescribeInstanceTypes`/`DescribeSpotPriceHistory`, EKS `DescribeClusterVersions`: with `live_pricing`, or for a type missing from the snapshot |
| Options | `live_pricing`, `as_of`, `time_budget_s` |
| Tests | [test_finops.py](../../tests/test_finops.py): 38 unit (exact arithmetic, `AS_OF = 2026-10-19`); live [test_finops_live.py](../../tests/test_finops_live.py) (< 60 s, submitted) |

### 4.7 hello ([agent.py](../../transform_agents/hello/agent.py))
| Aspect | Detail |
|---|---|
| Purpose | The smallest complete agent: a template, a model probe, the first runtime deployed, and the `STAGE_OVERRIDE=<stage>=hello` stand-in. No analogue. Not in `STAGES`. |
| Model / tools | `us.amazon.nova-2-lite-v1:0` (`MODEL_HELLO`); `workspace_tools` + `json_submit_tool`; no knowledge, options, binaries or budget beyond `MODEL_READ_TIMEOUT` |
| In → out | Any `files` → `data{file_count, files}`, `hello/report.md`; fallback: `extract_json` on the final text |
| Tests | No dedicated test; covered through `STAGE_OVERRIDE` in [test_orchestrator.py](../../tests/test_orchestrator.py) and packaging in [test_deploy.py](../../tests/test_deploy.py) |

## 5. Change recipes

### (a) Change a stage's model
1. The candidate must be in an allowed family (Invariant 3) and support tool use over Converse streaming in us-west-2
   (inference-profile id where needed, e.g. `us.` for Nova). The 2026-10-19 benchmark ([PLAN.md](../PLAN.md) "Models")
   rejected Llama 3.3 70B and Magistral (no tool calls), DeepSeek R1 (no Converse tools), Devstral 2 (about 50 s per
   turn), and demoted Mistral Large 3 (about a third of its calls came back as text).
2. Access: `MODEL_<STAGE>=<id> make preflight` makes one Converse call per distinct model.
3. Probe, a cheap multi-turn tool loop (list → write → submit):
   ```bash
   MODEL_HELLO=<id> AWS_PROFILE=<profile> AWS_REGION=us-west-2 uv run python - <<'EOF'
   import asyncio, json
   from transform_agents.core import AgentRequest
   from transform_agents.hello.agent import handle
   async def main():
       async for e in handle(AgentRequest(files={"app/app.py": "print(1)\n", "README.md": "# x\n"})):
           if e["type"] == "tool_start": print("tool", e["name"])
           elif e["type"] in ("result", "error"): print(json.dumps({k: e.get(k) for k in ("type", "data", "metrics", "message")}))
   asyncio.run(main())
   EOF
   ```
   Pass: structured `tool_start` events, `data.file_count == 2`, `metrics.submitted` true, no `｜DSML｜` leaks,
   single-digit seconds.
4. Benchmark: `MODEL_<STAGE>=<id> AWS_PROFILE=… AWS_REGION=us-west-2 uv run pytest -q -m live tests/test_<stage>*.py -s`
   **three times**. Require 3/3 passes within the live-test ceiling, then compare latency and tokens with §6. Confirm no
   fallback fired: `submitted`, terraform `runs == 1` with no `text-call-*` ids, requirements `resumed == false`,
   modernization `passes == 1`, finops no `fallback`. Live tests overwrite fixtures (§6): `git checkout tests/fixtures` if you reject.
5. Run one full pipeline (§6).
6. Update together:
   - `DEFAULT_MODELS` and the docstring date in [config.py](../../transform_agents/core/config.py), plus a dated row in
     [PLAN.md](../PLAN.md) "Models" and §4 and §7 here;
   - `MODEL_NAMES` in [ui/views.py](../../ui/views.py), with [test_views.py](../../tests/test_views.py) and the nav assertion in
     [test_ui.py](../../tests/test_ui.py);
   - the Q&A model line and the cheat sheet in [KEYNOTE-SCRIPT.md](../KEYNOTE-SCRIPT.md),
     [WORKSHOP.md](../../WORKSHOP.md) and [deploy/README.md](../../deploy/README.md).
   Then re-record ([05-demo-content.md](05-demo-content.md)) and redeploy the runtimes, which carry a copy of the code.
   Don't leave `MODEL_<STAGE>` set as a permanent substitute.

### (b) Refresh the FinOps price snapshot
1. `AWS_PROFILE=… uv run python transform_agents/finops/knowledge/refresh_prices.py` (about 40 read-only calls in
   parallel). It rewrites the JSON with `fetched_at` = today, so `pricing_source` becomes `static-YYYY-MM`.
2. Review the diff: prices, `eks.versions`, `default_version`. Add any types the design now uses to
   `EC2_TYPES`/`RDS_CLASSES` in [pricing.py](../../transform_agents/finops/pricing.py).
3. `uv run pytest -q tests/test_finops.py`. The exact pins (71.89, 598.79, 208.29, 190.63, 365.00, 52.26, …) and
   `"static-2026-10"` break when prices or the month change. Recompute each from the formula in its test comment
   before you update it. Keep `AS_OF = 2026-10-19` unless the new EKS table changes 1.33's status on that date; if it
   moves, re-check every `eks_support` assertion.
4. Update the `pricing_source` example in [CONTRACTS.md](../CONTRACTS.md). Confirm the planted finding still holds:
   1.33 in extended support at the talk date, before 2027-07-29 (after that date `eks_support()` reports `END_OF_SUPPORT`).
5. Full pipeline run, then `make numbers`, then update the keynote ($72 → $623 → $208, +$365/month) and re-record.

### (c) Refresh Terraform module inputs and bump the blueprint
1. With terraform and internet: `uv run python transform_agents/terraform/knowledge/extract_module_inputs.py`. It
   resolves the newest vpc `~> 6.0`, eks `~> 21.0` and aws `~> 6.0`, then rewrites `module-inputs.json`.
2. `uv run pytest -q tests/test_terraform.py` (`test_knowledge_matches_pinned_module_versions`,
   `test_blueprint_is_valid_offline`). Then `uv run python -m transform_agents.terraform.tools --warmup` (cold, then
   warm, `terraform validate` of the blueprint; `--validate <dir> [--offline]` for spot checks).
3. If inputs were renamed or removed, adapt the blueprint `.tf` files, `BLUEPRINT_SUMMARY` (tools.py), `EKS_RENAMES`
   (validate.py: old names get did-you-mean hints) and the `summary.facts()` paths.
4. For a major bump, change together: the constraints in blueprint `main.tf`/`versions.tf`; `MODULES` and
   `AWS_PROVIDER` in the extractor; the guardrail line in `SYSTEM_PROMPT` and `BLUEPRINT_SUMMARY`; the version tests;
   the CONTRACTS `modules` example; and `ALLOWED_MODULE_SOURCES` if a source changes. Warm caches re-key automatically
   (`signature()`).
5. Run the live test 3× and a full pipeline, then update §7.

### (d) Update the modernization pattern
1. Edit [knowledge/pattern/](../../transform_agents/modernization/knowledge/pattern/) (platform) or
   [knowledge/example/](../../transform_agents/modernization/knowledge/example/) (worked example). Placeholders must be
   `Params` fields. A new `.py` or suffix-less platform file is locked automatically; a new model-owned file goes into `DOMAIN_FILES`.
2. Keep the pattern manifests passing `checks.policy_checks` and the POL rules (recipe e). Keep `PLATFORM_CHANGES`,
   `HELPERS` and the porting rules in `SYSTEM_PROMPT` true to the pattern.
3. `uv run pytest -q tests/test_modernization.py`. The example test expects `"20 passed"` (update it if the example's
   tests change) and needs a warm uv cache: `uv run python -c "from transform_agents.modernization.checks import prewarm_test_env as p; print(p())"`.
4. Re-verify the image with Docker only, no Bedrock. This builds it, runs it read-only as UID 10001 with `/tmp` tmpfs,
   and GETs health, readiness and each legacy GET route (all must be 200):
   ```bash
   uv run python - <<'EOF'
   import json
   from transform_agents.core import Workspace
   from transform_agents.modernization import checks
   from transform_agents.modernization.pattern import EXAMPLE_DIR, Params, scaffold
   from transform_agents.modernization.tools import smoke_paths
   ws = Workspace({f"legacy/app/{n}": (EXAMPLE_DIR / n).read_text() for n in ("schema.sql", "seed.sql")}
                  | {"legacy/app/app.py": (EXAMPLE_DIR / "legacy_app.py").read_text()})
   p = Params(service_name="tickets-service", image="tickets-service", namespace="tickets")
   scaffold(ws, p)
   ws.write("microservice/app/api.py", (EXAMPLE_DIR / "api.py").read_text())
   ws.write("microservice/tests/test_api.py", (EXAMPLE_DIR / "test_api.py").read_text())
   print(json.dumps(checks.docker_build_check(ws, p, smoke_paths(ws)), indent=1))
   EOF
   ```
5. Run the modernization live test 3× (it writes the fixture only on success), then
   `pytest -m live tests/test_modernization_live.py::test_generated_image_builds_and_serves`, then the k8s live test
   (it consumes that fixture). Re-record.

### (e) Add or modify a K8s policy rule (POL-xxx) and the drift injector
1. In [policy.py](../../transform_agents/k8s_validation/policy.py), add `Rule("POL-0NN", title, severity, hint)` to
   `RULES` (stable ids, never reused) and evaluate it in `check_policies()` with `add(rule_id, ref, message, container,
   fix=<snippet>)`. Add it to `AVAILABILITY_RULES` if it applies to production only. If the fix is a new manifest,
   extend `NEW_FILE_RULES` and `CREATES_RESOLVE` (tools.py).
2. Add the row to `knowledge/policy-baseline.md` (`test_knowledge_doc_lists_every_rule`); the score penalty follows
   severity (`report.PENALTY`). Add tests in [test_k8s_validation.py](../../tests/test_k8s_validation.py).
3. Make the modernization pattern comply, and mirror the rule in `checks.policy_checks` if modernization should
   self-check. Otherwise every run starts with a failure. Run the live test 3×.
4. Drift ([drift.py](../../transform_agents/k8s_validation/drift.py)): write `_edit(text) -> (text, change)`, returning
   `(text, "")` when it doesn't apply, regex-only and deterministic, and register `(rule_id, _edit)` in
   `inject_drift()`. Then update the `test_inject_drift_*` tests ("four"), the `simulate_drift` row in CONTRACTS.md,
   `DRIFT` in [fakes.py](../../transform_agents/orchestrator/fakes.py), the keynote ("4 simulated violations") and the recording.

### (f) Add a tool to an agent
1. Put the logic in a plain, tested module function. The `@tool` stays a thin wrapper whose docstring `Args:` becomes
   the schema. It returns short text or JSON, with `error: …` strings for problems the model can fix.
2. Bind it to per-request state through a closure (`make_tools(ctx, box)`, `build_tools(...)`), never to module
   globals. Register it in the tool list (conditionally if an option gates it) and name it in the `SYSTEM_PROMPT` workflow.
3. Respect the budget helpers (`state.remaining()`, `over_budget`, tool-call budgets). If the tool takes `ToolContext`,
   add it to terraform's `CONTEXT_TOOLS`.
4. Subprocesses: argv list, timeout, `find_binary`, degrade when the binary is missing (vendor it in
   [deploy_tool.py](../../deploy/deploy_tool.py) if the runtime needs it). AWS: read-only, botocore `Config` with short
   timeouts, `skipped` without credentials, and the IAM action in [runtime-extras.json](../../deploy/policies/runtime-extras.json).
5. Add a `TOOLS` label and a `PHASES` entry in [ui/views.py](../../ui/views.py) (otherwise the label is "Run <name>").
   Unit-test it with a scripted model, as the `test_handle_*` tests do, then run the live test 3×.

### (g) Add a brand-new stage end to end
1. Package `transform_agents/<stage>/`: `__init__.py`; `agent.py` (`STAGE`, `SYSTEM_PROMPT`, `build_prompt`,
   `async def handle` with a deadline and deterministic finish); `tools.py`; `knowledge/`; `runtime.py` (copy the shim).
2. Add it to `STAGES` (pipeline order) and `DEFAULT_MODELS` in [config.py](../../transform_agents/core/config.py). Add a
   [CONTRACTS.md](../CONTRACTS.md) section (In, Out `data`, Artifacts) and its Options-reference rows.
3. Orchestrator: `STAGE_INPUTS` (and downstream consumers) and a `case` in `stage_request()` (`case _` raises) in
   [pipeline.py](../../transform_agents/orchestrator/pipeline.py); `Options` and CLI flags for new options; `DATA`,
   `ARTIFACTS` and `SUMMARIES` in [fakes.py](../../transform_agents/orchestrator/fakes.py); `STAGES` in
   [scripts/demo_numbers.py](../../scripts/demo_numbers.py).
4. UI: `STAGE_META` ([style.py](../../ui/style.py)), `NEXT_STEP` ([app.py](../../ui/app.py)), and `PHASES`, `IDEAS`,
   `CONTRACT_KEYS`, `OVERVIEW`, `DETAILS`, the facts function and `TOOLS` ([views.py](../../ui/views.py)). Follow the mockups (03-ui.md).
5. Deploy discovers stages from `STAGES` plus a `runtime.py` that defines `app`. Add `STAGE_ENV`, `MAIN_ENV_DEFAULTS`,
   vendored binaries and IAM extras as needed. A subset deploy deletes the other runtimes (`FORCE_SUBSET`, 04-operations.md).
6. Tests: `tests/test_<stage>.py` (scripted model), `tests/test_<stage>_live.py` (`@pytest.mark.live`), and
   `tests/fixtures/<stage>_result.json` (the replay fallback, read by the views tests).
7. Docs: §4 and §7 here, [01-architecture.md](01-architecture.md), [README.md](README.md),
   [WORKSHOP.md](../../WORKSHOP.md), the keynote. Re-record.

### (h) Change prompts safely
- Keep the exact tool names, the final `submit_result` step with its argument names, and "call once, at the end". Keep
  the revision branches in `build_prompt` (requirements: `resolve_question` and the `Revised:` prefix; the others: edit
  the existing files).
- Keep the rules the code relies on: numbers only from tools; "do not add tables" where `finalize_report` appends
  them; "do not write the validation report" (k8s_validation); at most 12 requirements per `set_lists` call.
- Change terraform mapping rules together with `summary.expected_values()` (`test_expected_values_follow_mapping_rules`).
  Keep discovery's severity rubric consistent with `fallback_severity`.
- Run the `build_prompt` unit tests, then the live test 3×. Compare cycles and tokens with §6, run the full pipeline,
  and re-record if the output changed.

## 6. Verification

```bash
uv run pytest -q                                   # 411 unit tests, no AWS (7 live deselected by addopts)
uv run pytest -q tests/test_core.py tests/test_<stage>*.py
uv run ruff check .
AWS_PROFILE=<profile> AWS_REGION=us-west-2 uv run pytest -q -m live tests/test_<stage>*.py -s
make tools && make kind-up                         # k8s live prerequisites (kind, kubeconform in .tools/bin)
PATH="$PWD/.tools/bin:$PATH" AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
  uv run pytest -q -m live tests/test_k8s_validation*.py -s
AWS_PROFILE=<profile> AWS_REGION=us-west-2 uv run python -m transform_agents.orchestrator \
  --requirements samples/requirements/migration-requirements.md --mode local --auto-approve --live-deploy \
  --simulate-drift --revise requirements="Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."
make preflight                                     # SSO, model access, Docker, kind, terraform + uv + kubeconform warm-ups
make numbers RUN=runs/<id>                         # the numbers the talk quotes
```

Live tests write `tests/fixtures/<stage>_result.json`, which feeds the replay fallback and `test_views.py`:
modernization and k8s_validation only on a pass, terraform and finops on every run, discovery and requirements only
with `SAVE_FIXTURES=1`.

What good looks like: recorded run `20261020-141613-1592` ([samples/recorded-run/](../../samples/recorded-run/)),
with `live_deploy` and `simulate_drift`.

| Stage | Latency | Tokens in / out | Cycles | Outcome | Live-test ceiling |
|---|---|---|---|---|---|
| discovery | 9.8 s | 13,238 / 1,168 | 3 | 17 findings (LF-01–03 critical), readiness 50, replatform | < 90 s |
| requirements | 18.1 s, revision 11.2 s | 49,968 / 2,383; 51,599 / 593 | 8; 7 | 24 requirements, 3 open questions, revision resolved 2 | < 90 s |
| modernization | 57.0 s | 85,919 / 3,238 | 9 | 1 pass, 1 test run, 23 passed, manifests pass | ≤ 155 s |
| terraform | 17.1 s | 31,277 / 966 | 6 | 1 run, valid at level `terraform`, Kubernetes 1.33, Postgres 16 | < 90 s |
| k8s_validation | 15.8 s | 36,563 / 792 | 9 | POL-001/005/009/011 → none, score 98, smoke 200/200/200/201/409 | < 120 s |
| finops | 10.9 s | 24,187 / 1,003 | 6 | $71.89 → $622.88 → $207.57, within the $450 budget | < 60 s |

In total: about 140 s of agent time and about 250k tokens (about 300k with the first requirements attempt). Every
stage had `submitted: true` and no fallback.

## 7. Time-sensitive facts

| Fact | Value (as of) | Where | Refresh |
|---|---|---|---|
| Default models | Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`) for 5 stages + hello; `deepseek.v3.2` for modernization; benchmark 2026-10-19, us-west-2 | [config.py](../../transform_agents/core/config.py), [PLAN.md](../PLAN.md) | Recipe (a); `make preflight` monthly |
| Latest framework versions | flask, werkzeug, jinja2 3.1; django 5.2; fastapi 0.115 (curated 2026-10) | `LATEST` in [codescan.py](../../transform_agents/discovery/codescan.py) | By hand from PyPI; drives LF-05 evidence |
| System Python on AL2023 | 3.9, upstream EOL 2025-10 (AL2: 3.7, EOL 2023-06) | `SYSTEM_PYTHON` in [infra.py](../../transform_agents/discovery/infra.py); `PLATFORM_CHANGES` text | Re-check when AL2023 changes its system python3 |
| EKS support table | 1.31–1.37 with dates; `default_version` 1.36; 1.33: standard ended 2026-07-29, extended ends 2027-07-29 | `eks.versions` in the price JSON | Recipe (b). `eks_support()` keeps reporting extended past end of extended support (§8). |
| Kubernetes default (planted) | `"1.33"` in requirements `DEFAULTS`, blueprint `variables.tf`, finops `_design()` fallback | see column | Keep it deliberately ([05-demo-content.md](05-demo-content.md) §4) |
| Terraform modules | vpc 6.7.3 (`~> 6.0`), eks 21.29.0 (`~> 21.0`), aws provider 6.69.1 (`~> 6.0`), extracted 2026-10-19 | [module-inputs.json](../../transform_agents/terraform/knowledge/module-inputs.json) | Recipe (c), quarterly |
| Price snapshot | `fetched_at` 2026-10-19, us-west-2, `static-2026-10`; tests pin `AS_OF` 2026-10-19 | [prices-us-west-2.json](../../transform_agents/finops/knowledge/prices-us-west-2.json) | Recipe (b), quarterly |
| Pattern runtime | `python:3.12-slim`; Flask 3.1.3, SQLAlchemy 2.1.5, psycopg 3.3.6, gunicorn 26.2.0, pytest 9.1.1 | [knowledge/pattern/microservice/](../../transform_agents/modernization/knowledge/pattern/microservice/) | Recipe (d) |
| Local tools | kind v0.33.0, kubeconform v0.8.0, kind node v1.36.4 | [install-tools.sh](../../scripts/install-tools.sh), [kind-config.yaml](../../scripts/kind-config.yaml) | 04-operations.md |

## 8. Known debt (verified 2026-10-20)

1. **Duplicated deadline helpers:** `with_deadline` and `timeout_result` are verbatim copies in discovery/agent.py and
   requirements/agent.py. Move them to core.
2. **Three diverging legacy-estate loaders.** `pipeline.load_legacy_estate` skips `terraform/tests`, files over 200 KB
   and binaries. discovery `tools.load_legacy_estate` (`rglob`) includes `terraform/tests` and has no size cap.
   modernization `load_bundled_legacy` reads `app/` only, and a binary file there fails as "setup failed".
3. **Fallback gaps:** requirements has no deterministic fallback (it ends in `error` without a valid draft).
   Discovery's fallback covers timeouts and missing submits, but a model or API `error` ends the stage.
4. **Postgres default mismatch:** `"16"` in the spec (requirements `DEFAULTS`, CONTRACTS examples) vs `"17"` for the
   blueprint `db_engine_version`. It surfaces only when the spec lacks `data.version`.
5. **Blueprint default region `us-east-1`,** while `region()`, the price snapshot and the contracts use `us-west-2`.
6. **kubeconform runs without `-kubernetes-version`** (master schemas), while the target is EKS 1.33 and kind runs 1.36.4.
7. **Unused dependency `strands-agents-tools`:** never imported, yet it ships in every runtime (uv.lock export).
8. ~~**`eks_support()`**~~ **Fixed 2026-10-20:** the status is now computed for `as_of` (the snapshot's own `status`
   no longer overwrites it), and a version past `end_of_extended_support` reports `END_OF_SUPPORT`. Regression test:
   `tests/test_finops.py::test_eks_support_status_follows_the_date_not_the_snapshot`.
9. **Implicit live pricing:** `add_missing_prices()` calls the Pricing API for a type missing from the snapshot even
   with `live_pricing` false. pricing.py's boto3 clients have no botocore `Config` (default timeouts; discovery uses 5/15 s).
10. **Two K8s policy implementations**, `modernization/checks.policy_checks` (ids like `pod-non-root`) and
    `k8s_validation/policy.py` (POL-xxx), which can drift. Likewise, the terraform mapping rules live both in
    `SYSTEM_PROMPT` prose and in `summary.expected_values()`, and only the latter is tested.
11. **Options are coerced unvalidated:** finops `float(options.get("time_budget_s", 55))` and terraform
    `int(options.get("max_validations", 4))` raise on `null`. Since 2026-10-20 `make_app` catches the crash and ends the
    SSE stream with one `error` event (test `tests/test_core.py::test_make_app_turns_a_crash_into_one_error_event`), but
    the options should still be validated at the source.
12. **Model-writable dependency files:** `LOCKED_FILES` covers only `.py` and suffix-less pattern files, so
    `requirements*.txt` and `pytest.ini` are model-writable, and `run_service_tests` installs `requirements-dev.txt` via uv (network).
13. **Fixtures overwritten before asserting:** the terraform and finops live tests write their fixture first (§6).
14. **Small items:** a stale "tools run in parallel threads" comment in finops/tools.py; `MODEL_READ_TIMEOUT` isn't
    passed to runtimes (`GLOBAL_PASSTHROUGH_ENV` = `MODEL_ID`, `MODEL_MAX_TOKENS`); `hello` has no dedicated unit test.

Resolved: the options read in code but missing from CONTRACTS.md (`docker_build`, `time_budget_s`, `timeout_s`,
`max_validations`, `live_pricing`, `as_of`, `region`) are now in its [Options reference](../CONTRACTS.md#options-reference).

## 9. References

- [docs/CONTRACTS.md](../CONTRACTS.md) (normative I/O) · [01-architecture.md](01-architecture.md) ·
  [05-demo-content.md](05-demo-content.md) · [MAINTAINING.md](../../MAINTAINING.md) · [CLAUDE.md](../../CLAUDE.md)
- [docs/PLAN.md](../PLAN.md) (model benchmark record) · [docs/KEYNOTE-SCRIPT.md](../KEYNOTE-SCRIPT.md) (cheat sheet) ·
  [scripts/demo_numbers.py](../../scripts/demo_numbers.py) · [scripts/preflight.sh](../../scripts/preflight.sh)
- [pipeline.py](../../transform_agents/orchestrator/pipeline.py) (`STAGE_INPUTS`, `stage_request`) ·
  [clients.py](../../transform_agents/orchestrator/clients.py) (`LocalClient`, `STAGE_OVERRIDE`) ·
  [deploy/deploy_tool.py](../../deploy/deploy_tool.py)
