# Stage contracts

Every agent receives an `AgentRequest` (see `transform_agents/core/contract.py`) and streams contract events that end in exactly one `result` (or `error`).
This file defines, for each stage, what goes **in** (`inputs` / `files` / `options`) and what comes **out** (`result.data` and artifact paths).
The orchestrator builds every stage's request from the earlier stages' `result.data` and `result.artifacts`.

General rules:
- `result.data` must be a JSON object that follows the schema below. Fields may be added; required fields must not be renamed or dropped.
- Artifact paths are relative and use the prefixes given below, so later stages can pick them up.
- **Revision**: when `request.feedback` is non-empty, the orchestrator also passes the stage's previous artifacts in `files` and its previous data as `inputs.previous`. The agent applies the feedback by editing the existing files instead of starting over.
- Agents never create AWS resources. Read-only AWS calls (describe, CloudWatch metrics, Pricing API) are allowed but must degrade gracefully when there are no credentials.

---

## 1. discovery — Discovery & Assessment

**In**
- `files`: the legacy estate, with each file prefixed `legacy/` (`legacy/app/**`, `legacy/terraform/**`).
- `options.live_aws` (bool, default false): look up the running EC2 instance (tag `app=legacy-inventory`) and its CloudWatch CPU metrics.

**Out `data`**
```json
{
  "application": {"name": "legacy-inventory", "language": "python", "language_version": "3.9", "framework": "flask 2.0.3",
                   "entrypoint": "app.py", "listen_port": 5000, "loc": 180},
  "components": [{"name": "inventory-api", "kind": "http-api|web-ui|batch", "routes": ["/api/items"], "notes": "..."}],
  "dependencies": {"python_packages": ["Flask==2.0.3"], "database": {"engine": "sqlite", "location": "/opt/...", "tables": ["items", "orders"]},
                    "local_state": ["/opt/legacy-inventory/reports", "/var/log/legacy-inventory/app.log"], "external_services": []},
  "infrastructure": {"provider": "aws", "region": "us-west-2", "instance_type": "t3.large", "vcpu": 2, "memory_gib": 8,
                      "os": "Amazon Linux 2023", "volume": {"type": "gp2", "size_gib": 30}, "process_manager": "systemd",
                      "reverse_proxy": "nginx", "scheduled_jobs": ["nightly report via cron"], "source": "terraform|live"},
  "utilization": {"cpu_avg_pct": 4.0, "cpu_p95_pct": 12.0, "mem_avg_pct": 18.0, "period_days": 14, "source": "cloudwatch|estimate"},
  "findings": [{"id": "LF-01", "severity": "critical|high|medium|low", "category": "security|reliability|operability|cost",
                "title": "SQL injection in search", "evidence": "app.py: list_items()", "recommendation": "..."}],
  "strategy": {"recommended": "replatform|refactor|rehost", "rationale": "...", "target": "containerized microservice on EKS + RDS PostgreSQL"},
  "readiness_score": 0
}
```
**Artifacts**: `reports/assessment.md`

## 2. requirements — Requirements analysis

**In**
- `files`: `requirements/<uploaded file name>` (markdown or text).
- `inputs.assessment`: discovery `data`.

**Out `data`** (the migration spec, which every later stage reads)
```json
{
  "service": {"name": "inventory-service", "description": "..."},
  "target": {"platform": "eks", "region": "us-west-2", "kubernetes_version": "1.33", "cpu_architecture": "arm64", "iac": "terraform", "registry": "ecr"},
  "workload": {"container_port": 8080, "health_path": "/healthz", "readiness_path": "/readyz", "replicas_min": 2, "replicas_max": 6,
                "autoscaling_metric": "cpu", "autoscaling_target_pct": 70, "peak_rps": 40, "p95_latency_ms": 300},
  "data": {"engine": "postgres", "version": "16", "managed_service": "rds", "migrate_from": "sqlite", "multi_az": true},
  "nfr": {"availability_slo_pct": 99.9, "rpo_minutes": 15, "rto_minutes": 60, "logging": "json-stdout"},
  "security": {"secrets": "aws-secrets-manager", "encryption_at_rest": true, "private_database": true, "non_root": true, "read_only_root_fs": true},
  "finops": {"monthly_budget_usd": 450, "prefer_graviton": true, "environment": "prod", "growth_factor_next_year": 3},
  "requirements": [{"id": "FR1", "text": "...", "category": "functional|nfr|security|constraint|goal", "priority": "must|should|could",
                     "traces_to": ["LF-01"], "acceptance": "..."}],
  "conflicts": [{"description": "...", "resolution": "..."}],
  "open_questions": [{"id": "Q-01", "question": "...", "assumption": "...", "impact": "...",
                      "status": "open|resolved", "resolution": "the reviewer's decision (when resolved)"}]
}
```
On a revision, the agent resolves every open question the reviewer's feedback answers (`resolve_question` tool). The entry stays in the list with `status: "resolved"`. The revised summary starts with `Revised:`.
**Artifacts**: `reports/migration-spec.md` (human-readable spec plus a traceability matrix)

## 3. modernization — Code modernization

**In**
- `files`: `legacy/app/**`.
- `inputs.assessment`, `inputs.spec`.

**Out `data`**
```json
{
  "service_name": "inventory-service",
  "image": {"repository": "inventory-service", "tag": "1.0.0", "context": "microservice", "dockerfile": "microservice/Dockerfile", "port": 8080},
  "endpoints": {"health": "/healthz", "readiness": "/readyz", "api": ["/api/items", "/api/items/{id}", "/api/orders"]},
  "env": [{"name": "DATABASE_URL", "source": "secret|configmap", "required": true, "description": "..."}],
  "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
  "manifests": ["k8s/base/deployment.yaml"],
  "changes": [{"file": "microservice/app/main.py", "change": "...", "addresses": ["LF-01", "SEC5"]}],
  "tests": {"command": "pytest -q", "passed": true, "output": "..."}
}
```
**Artifacts**
- `microservice/**`: refactored app, `requirements.txt`, `Dockerfile`, `.dockerignore`, `tests/`.
- `k8s/base/**`: Kustomize base with `kustomization.yaml`, `namespace.yaml`, `serviceaccount.yaml`, `configmap.yaml`, `deployment.yaml`, `service.yaml`, `hpa.yaml`, `pdb.yaml`, `cronjob-report.yaml`.
- `k8s/overlays/local/**`: kind overlay with SQLite or in-cluster Postgres, 1–2 replicas, and the image `inventory-service:dev`.

## 4. terraform — Target infrastructure as code

**In**
- `inputs.spec`, `inputs.assessment`, `inputs.modernization`.

**Out `data`**
```json
{
  "valid": true, "validate_output": "Success! ...",
  "modules": [{"source": "terraform-aws-modules/eks/aws", "version": "~> 21.0"}],
  "compute": {"node_instance_types": ["t4g.medium"], "node_min": 2, "node_max": 4, "capacity_type": "ON_DEMAND", "architecture": "arm64"},
  "database": {"engine": "postgres", "version": "16", "instance_class": "db.t4g.micro", "multi_az": true, "storage_gib": 20},
  "network": {"vpc_cidr": "10.42.0.0/16", "azs": 2, "nat_gateways": 1},
  "registry": {"repositories": ["inventory-service"]},
  "outputs": ["cluster_name", "ecr_repository_url", "db_endpoint"],
  "spec_coverage": [{"requirement": "SEC3", "how": "RDS in private subnets, SG only from EKS nodes"}]
}
```
**Artifacts**: `terraform/**` (`versions.tf`, `variables.tf`, `main.tf`, `outputs.tf`, `terraform.tfvars.example`)

## 5. k8s_validation — Kubernetes validation

**In**
- `files`: `microservice/**` and `k8s/**` (the modernization artifacts).
- `inputs.spec`, `inputs.modernization`.
- `options.live_deploy` (bool, default false): build the image and deploy it to the local kind cluster. This only works when the agent runs locally.
- `options.kind_cluster` (default `transform-workshop`).
- `options.simulate_drift` (bool, default false): a demo aid. Before the first check, deterministically inject typical hand-edit violations (`:latest` tag, missing limits, writable root FS, PDB dropped). They are reported as `data.drift_injected` and labelled as simulated.

**Out `data`**
```json
{
  "passed": true, "score": 92,
  "checks": [{"id": "schema", "tool": "kubeconform|builtin", "status": "pass|fail|warn|skipped", "resource": "Deployment/inventory-service", "details": "..."}],
  "fixes_applied": [{"file": "k8s/base/deployment.yaml", "change": "added readOnlyRootFilesystem"}],
  "live_deploy": {"status": "pass|fail|skipped", "cluster": "kind-transform-workshop", "image": "inventory-service:dev",
                   "rollout": "...", "smoke_tests": [{"request": "GET /healthz", "status": 200}]}
}
```
**Artifacts**: `reports/validation-report.md`, plus any fixed `k8s/**` files (same paths, so they replace the originals).

## 6. finops — Right-sizing and cost

**In**
- `inputs.assessment`, `inputs.spec`, `inputs.modernization`, `inputs.terraform`, `inputs.k8s_validation`.
- `files`: `k8s/**`, `terraform/**`.

**Out `data`**
```json
{
  "currency": "USD", "pricing_source": "aws-pricing-api|static-2026-10",
  "current": {"monthly_usd": 0, "items": [{"item": "EC2 t3.large on-demand", "monthly_usd": 0}]},
  "target_as_designed": {"monthly_usd": 0, "items": []},
  "target_optimized": {"monthly_usd": 0, "items": []},
  "budget_usd": 450, "within_budget": true,
  "rightsizing": {"pod_requests": {"cpu": "100m", "memory": "160Mi"}, "pod_limits": {"cpu": "500m", "memory": "256Mi"},
                   "replicas_min": 2, "replicas_max": 6, "node_instance_types": ["t4g.medium"], "node_min": 2, "node_max": 4,
                   "db_instance_class": "db.t4g.micro"},
  "recommendations": [{"id": "FO-01", "title": "...", "monthly_savings_usd": 0, "effort": "low|medium|high", "risk": "low|medium|high", "applies_to": "k8s|terraform|process"}]
}
```
**Artifacts**: `reports/finops-report.md`
