# transform-agentcore-workshop

Step-by-step AgentCore workshop recreating AWS Transform's migration agents: discovery → requirements → code modernization → Terraform → K8s validation → FinOps.

Six agents (Strands Agents + Amazon Nova / DeepSeek on Amazon Bedrock) run locally or on **Amazon Bedrock AgentCore Runtime**.
They migrate a legacy Flask monolith on EC2 into a containerized microservice on Kubernetes. A Streamlit chatbot
drives the pipeline, with a human approval gate after every agent.

**→ Start here: [WORKSHOP.md](WORKSHOP.md)**. It covers setup, deployment, rehearsal, the 60-minute run of show, the fallback plan and teardown.

```bash
export AWS_PROFILE=<your-sso-profile> AWS_REGION=us-west-2
make install tools kind-up preflight
make ui                      # http://localhost:8501
```

| Folder | Contents |
|---|---|
| [legacy-infra/](legacy-infra/) | The current infrastructure: a Flask + SQLite monolith on one EC2 instance (Terraform) |
| [transform_agents/](transform_agents/) | The six agents, the shared core and the orchestrator |
| [ui/](ui/) | Streamlit chatbot |
| [deploy/](deploy/) | Deployment to AgentCore Runtime (AgentCore CLI) |
| [samples/](samples/) | The requirements file uploaded on stage, and the recorded run that Replay plays back |
| [scripts/](scripts/) | kind cluster, repo-local tools, the legacy estate (up, load, down), preflight, the talk's numbers |
| [tests/](tests/) | About 410 unit tests (Bedrock mocked, `make test`) plus live smoke tests (`make test-live`) |

| Document | What it is |
|---|---|
| [WORKSHOP.md](WORKSHOP.md) | The runbook: setup, deploy, rehearsal, run of show, plan B, teardown |
| [docs/KEYNOTE-SCRIPT.md](docs/KEYNOTE-SCRIPT.md) | The word-for-word keynote, with Q&A and the cheat sheet of numbers |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | Stage contracts: inputs, outputs, artifacts and the `options.*` reference |
| [docs/design/](docs/design/) | The approved UI mockups (light and dark) and [GUIDELINES.md](docs/design/GUIDELINES.md) |
| [docs/specs/](docs/specs/) and [MAINTAINING.md](MAINTAINING.md) | The current, maintained specs |
| [docs/PLAN.md](docs/PLAN.md) | The historical build plan (2026-10-19/20); the model benchmark lives in it |
