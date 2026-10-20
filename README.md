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
| [docs/](docs/) | Design plan, model benchmark and stage contracts |
