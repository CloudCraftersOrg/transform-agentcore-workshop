# Build plan — Transform × AgentCore workshop

Goal: in a 1-hour session, show step by step what AWS Transform's agents do at a low level by rebuilding them as six Claude-powered agents on Amazon Bedrock AgentCore. A legacy Flask monolith running on one EC2 instance ends up as a containerized microservice on Kubernetes.

## Target architecture

```
Streamlit chat UI ──► Orchestrator (approval gate after every stage)
 (upload reqs,          │ AgentClient: local (in-process) | cloud (InvokeAgentRuntime, SSE)
  approve/revise)       ▼
  1 Discovery ─► 2 Requirements ─► 3 Modernization ─► 4 Terraform ─► 5 K8s Validation ─► 6 FinOps
  (assess EC2   (parse uploaded   (monolith →        (EKS/ECR/VPC/   (kubeconform +      (rightsizing,
   + code)       reqs → spec)      microservice,      RDS IaC,        policy checks +     current vs
                                    Dockerfile, k8s)   tf validate)    live kind deploy)   target cost)
```

| # | Agent | AWS Transform analogue | Output artifacts |
|---|-------|------------------------|------------------|
| 1 | Discovery & Assessment | Discovery / assessment (inventory, dependencies, 6R) | `assessment.json`, `assessment.md` |
| 2 | Requirements | Job objective intake, HITL clarifications | `migration-spec.json`, `traceability.md` |
| 3 | Code Modernization | Transform custom / .NET / Java code transformation, containerization | `microservice/**`, `Dockerfile`, `k8s/**` |
| 4 | Terraform (IaC) | Network/infra conversion to Terraform | `terraform/**` (validated) |
| 5 | K8s Validation | Test & validation step | `validation-report.md` (+ fixed manifests) |
| 6 | FinOps / Right-sizing | Assessment TCO / business case | `finops-report.md`, right-sized values |

## Key technical decisions

- **Agent loop**: Strands Agents (`strands-agents`) with `BedrockModel` (the Bedrock Converse API). Each agent is wrapped in `BedrockAgentCoreApp` (`bedrock-agentcore` SDK), so the same code runs locally and on AgentCore Runtime.
- **Models**: Amazon Nova, Mistral and DeepSeek on Bedrock, picked per agent from a live benchmark on 2026-10-19 (us-west-2). Each one can be overridden with `MODEL_<AGENT>` or `MODEL_ID`.

  | Agent | Default model | Why |
  |---|---|---|
  | Discovery, Requirements, Terraform, K8s Validation, FinOps | `us.amazon.nova-2-lite-v1:0` | fastest reliable tool loop (~8s), cheap; Terraform: 11/11 valid runs |
  | Code Modernization | `deepseek.v3.2` | passed every code-gen check (non-root Dockerfile, probes, limits, Service) |

  Mistral Large 3 was the first pick for Terraform. It adapted the blueprint 6/6, but under load it wrote about a third of its tool calls as plain text, so Nova 2 Lite replaced it as the default for a predictable live demo. Mistral is still supported through `MODEL_TERRAFORM`.

  Rejected: Llama 3.3 70B and Magistral (didn't call tools), DeepSeek R1 (no tool use in Converse), Nova Premier and Llama 3.2 90B (end of life), Titan text (retired). Devstral 2 produced good code but took about 50s per turn.
- **Blueprints over free-form generation**: in the benchmark, no model could write EKS Terraform from scratch that passed validation. Their training predates the `terraform-aws-modules/eks` v21 argument renames, and they didn't converge even with the validate errors fed back. Every model did succeed when it **adapted a curated, validated blueprint**. So the generating agents (Modernization, Terraform) start from known-good patterns in `knowledge/`, edit them with `edit_file`, and loop on deterministic validators (`terraform validate`, kubeconform, policy checks). This mirrors AWS Transform's curated transformation definitions.
- **Structured outputs**: each agent finishes by calling a `submit_result` tool. Forced `tool_choice` is not used, and if the tool isn't called the agent falls back to parsing JSON from the final text.
- **One contract for every agent**: the request is `{run_id, instruction, feedback?, inputs, files}`. The agent streams SSE events `status | text | tool | artifact | result | error`. The final `result` holds `{summary, data, artifacts{path: content}}`. Artifacts travel in the payload, so local and cloud modes behave the same.
- **Orchestration**: a deterministic pipeline that builds each stage's inputs from earlier artifacts. Every run is saved to `runs/<id>/`, and every event stream is recorded so it can be replayed as an offline fallback on stage.
- **Kubernetes**: the agent writes EKS Terraform, which can be pre-provisioned. Live validation and deployment happen on a local **kind** cluster (`make kind-up`).
- **AgentCore features used**: Runtime (all 6 agents) and Observability (traces). If time allows: Code Interpreter (FinOps cost model) and Memory (spec and decisions per session).
- **Cost**: Bedrock has no free tier for inference, but these models are cheap. A full 6-agent pipeline run is roughly 100–200k tokens, which costs cents.
- **Deploy**: the AgentCore CLI (`@aws/agentcore`, `agentcore deploy`) is preferred. If it can't handle our layout, we fall back to Terraform `aws_bedrockagentcore_agent_runtime`. Agents that need binaries (terraform, kubeconform) degrade gracefully when the binary is missing.

- **Planted demo finding**: the spec and blueprint default to Kubernetes 1.33, which entered EKS extended support on 2026-07-29 (+$365/month). The FinOps agent flags it and its optimized plan upgrades to 1.36. This is kept on purpose as the talk's "aha" moment.

## Repository layout

```
legacy-infra/          current infrastructure: Flask monolith + SQLite on EC2 (Terraform, user_data, systemd, nginx)
samples/requirements/  the requirements file uploaded during the demo
transform_agents/
  core/                contract, LLM client, tool-runner loop → event stream, AgentCore app wrapper, workspace tools
  discovery/ requirements/ modernization/ iac_terraform/ k8s_validation/ finops/
  orchestrator/        pipeline, stage inputs, AgentClient (local | agentcore | replay)
ui/                    Streamlit chatbot
deploy/                AgentCore Runtime deployment for all agents
scripts/               kind cluster, pre-flight checks
docs/                  lab guide (timed modules), presenter runbook, architecture diagram
tests/                 unit tests (LLM mocked) + live smoke tests
```

## Execution phases (review → execute at every gate)

1. **Foundation**: root `pyproject.toml`, `core/` contract and loop, a toy echo agent. *Gate*: the echo agent streams correctly against Bedrock both in-process and through the AgentCore app on :8080.
2. **Parallel build** (one sub-agent per feature, each in its own directory, each running its own tests):
   A legacy-infra + samples · B discovery + requirements · C modernization · D terraform · E k8s-validation + kind scripts · F finops · G orchestrator + UI · H AgentCore deploy tooling.
3. **Review & integrate**: code review of every piece, unit tests, then a full local pipeline run against Bedrock that records replay fixtures.
4. **Live execution** (each step needs your OK because it creates AWS resources): `terraform apply` the legacy EC2, run the pipeline against it, deploy the generated microservice to kind, then deploy the agents to AgentCore Runtime and run the pipeline in cloud mode.
5. **Workshop material**: lab guide, presenter runbook with fallbacks, architecture diagram. Final review, then commit.

## 60-minute agenda (draft)

| Min | Module |
|-----|--------|
| 0–5 | Intro: AWS Transform agents → AgentCore building blocks |
| 5–10 | The legacy estate (EC2 + Flask monolith) |
| 10–17 | Agent 1 Discovery · Agent 2 Requirements (upload file) |
| 17–27 | Agent 3 Modernization (monolith → microservice) |
| 27–35 | Agent 4 Terraform (EKS IaC + validate) |
| 35–45 | Agent 5 K8s validation + live deploy to kind |
| 45–52 | Agent 6 FinOps right-sizing |
| 52–57 | AgentCore Runtime: same agents in the cloud, traces |
| 57–60 | Wrap-up |
