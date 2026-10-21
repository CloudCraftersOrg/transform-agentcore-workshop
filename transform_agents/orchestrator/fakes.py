"""Realistic, deterministic stage results (from docs/CONTRACTS.md) for FakeClient: tests and offline UI work."""

from typing import Any

from transform_agents.core import AgentRequest
from transform_agents.core.config import DEFAULT_MODELS

ASSESSMENT = {
    "application": {"name": "legacy-inventory", "language": "python", "language_version": "3.9", "framework": "flask 2.0.3",
                    "entrypoint": "app.py", "listen_port": 5000, "loc": 180},
    "components": [
        {"name": "inventory-api", "kind": "http-api", "routes": ["/api/items", "/api/items/{id}", "/api/orders"],
         "notes": "JSON API used by warehouse scanners"},
        {"name": "dashboard", "kind": "web-ui", "routes": ["/"], "notes": "Jinja2 HTML dashboard"},
        {"name": "nightly-report", "kind": "batch", "routes": [], "notes": "cron writes CSV to local disk"},
    ],
    "dependencies": {"python_packages": ["Flask==2.0.3"],
                     "database": {"engine": "sqlite", "location": "/opt/legacy-inventory/data/inventory.db",
                                  "tables": ["items", "orders"]},
                     "local_state": ["/opt/legacy-inventory/reports", "/var/log/legacy-inventory/app.log"],
                     "external_services": []},
    "infrastructure": {"provider": "aws", "region": "us-west-2", "instance_type": "t3.large", "vcpu": 2, "memory_gib": 8,
                       "os": "Amazon Linux 2023", "volume": {"type": "gp2", "size_gib": 30}, "process_manager": "systemd",
                       "reverse_proxy": "nginx", "scheduled_jobs": ["nightly report via cron"], "source": "terraform"},
    "utilization": {"cpu_avg_pct": 4.0, "cpu_p95_pct": 12.0, "mem_avg_pct": 18.0, "period_days": 14, "source": "estimate"},
    "findings": [
        {"id": "LF-01", "severity": "critical", "category": "security", "title": "SQL injection in search",
         "evidence": "app.py: list_items()", "recommendation": "Use parameterized queries"},
        {"id": "LF-02", "severity": "high", "category": "security", "title": "Hard-coded secrets",
         "evidence": "app.py: SECRET_KEY, ADMIN_TOKEN", "recommendation": "Load from AWS Secrets Manager"},
        {"id": "LF-03", "severity": "high", "category": "reliability", "title": "Single EC2 instance, SQLite on local disk",
         "evidence": "terraform: aws_instance.app", "recommendation": "Multi-AZ EKS + RDS PostgreSQL"},
        {"id": "LF-04", "severity": "medium", "category": "operability", "title": "Logs and reports written to local disk",
         "evidence": "LOG_FILE, REPORT_DIR", "recommendation": "JSON logs to stdout, reports to S3 via CronJob"},
        {"id": "LF-05", "severity": "low", "category": "cost", "title": "t3.large at 4% average CPU",
         "evidence": "CloudWatch estimate", "recommendation": "Right-size on Graviton"},
    ],
    "strategy": {"recommended": "replatform", "rationale": "Small codebase, clear API boundary, stateful parts externalizable.",
                 "target": "containerized microservice on EKS + RDS PostgreSQL"},
    "readiness_score": 62,
}

SPEC = {
    "service": {"name": "inventory-service", "description": "Inventory & orders JSON API"},
    "target": {"platform": "eks", "region": "us-west-2", "kubernetes_version": "1.33", "cpu_architecture": "arm64",
               "iac": "terraform", "registry": "ecr"},
    "workload": {"container_port": 8080, "health_path": "/healthz", "readiness_path": "/readyz", "replicas_min": 2,
                 "replicas_max": 6, "autoscaling_metric": "cpu", "autoscaling_target_pct": 70, "peak_rps": 40,
                 "p95_latency_ms": 300},
    "data": {"engine": "postgres", "version": "16", "managed_service": "rds", "migrate_from": "sqlite", "multi_az": True},
    "nfr": {"availability_slo_pct": 99.9, "rpo_minutes": 15, "rto_minutes": 60, "logging": "json-stdout"},
    "security": {"secrets": "aws-secrets-manager", "encryption_at_rest": True, "private_database": True, "non_root": True,
                 "read_only_root_fs": True},
    "finops": {"monthly_budget_usd": 450, "prefer_graviton": True, "environment": "prod", "growth_factor_next_year": 3},
    "requirements": [
        {"id": "FR1", "text": "Keep the JSON API contract", "category": "functional", "priority": "must",
         "traces_to": [], "acceptance": "Existing scanner calls return identical payloads"},
        {"id": "NFR1", "text": "99.9% monthly availability", "category": "nfr", "priority": "must",
         "traces_to": ["LF-03"], "acceptance": "2+ replicas across 2 AZs, PDB minAvailable 1"},
        {"id": "SEC1", "text": "Secrets from AWS Secrets Manager", "category": "security", "priority": "must",
         "traces_to": ["LF-02"], "acceptance": "No credentials in code or image"},
        {"id": "SEC5", "text": "Fix SQL injection in search", "category": "security", "priority": "must",
         "traces_to": ["LF-01"], "acceptance": "Parameterized queries, injection test passes"},
        {"id": "G3", "text": "Stay under USD 450/month", "category": "goal", "priority": "must",
         "traces_to": ["LF-05"], "acceptance": "FinOps estimate within budget"},
    ],
    "conflicts": [{"description": "3x growth next year vs. USD 450 budget",
                   "resolution": "Size for today, HPA max 6 replicas absorbs growth"}],
    "open_questions": [
        {"id": "Q-01", "question": "Size for 3x traffic now?", "assumption": "No; autoscale", "impact": "cost"},
        {"id": "Q-02", "question": "Budget for dev/test?", "assumption": "Use the local kind overlay", "impact": "scope"},
    ],
}

MODERNIZATION = {
    "service_name": "inventory-service",
    "image": {"repository": "inventory-service", "tag": "1.0.0", "context": "microservice",
              "dockerfile": "microservice/Dockerfile", "port": 8080},
    "endpoints": {"health": "/healthz", "readiness": "/readyz", "api": ["/api/items", "/api/items/{id}", "/api/orders"]},
    "env": [{"name": "DATABASE_URL", "source": "secret", "required": True, "description": "PostgreSQL DSN"},
            {"name": "LOG_LEVEL", "source": "configmap", "required": False, "description": "Log level"}],
    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
    "manifests": ["k8s/base/deployment.yaml", "k8s/base/service.yaml", "k8s/base/hpa.yaml", "k8s/base/pdb.yaml"],
    "changes": [
        {"file": "microservice/app/main.py", "change": "Parameterized search query", "addresses": ["LF-01", "SEC5"]},
        {"file": "microservice/app/config.py", "change": "Settings from env / Secrets Manager", "addresses": ["LF-02", "SEC1"]},
        {"file": "microservice/app/main.py", "change": "Added /healthz and /readyz", "addresses": ["NFR6"]},
        {"file": "microservice/Dockerfile", "change": "Multi-stage, non-root, arm64", "addresses": ["SEC4", "TC2"]},
    ],
    "tests": {"command": "pytest -q", "passed": True, "output": "6 passed in 0.41s"},
}

TERRAFORM = {
    "valid": True, "validate_output": "Success! The configuration is valid.",
    "modules": [{"source": "terraform-aws-modules/eks/aws", "version": "~> 21.0"},
                {"source": "terraform-aws-modules/vpc/aws", "version": "~> 6.0"}],
    "compute": {"node_instance_types": ["t4g.medium"], "node_min": 2, "node_max": 4, "capacity_type": "ON_DEMAND",
                "architecture": "arm64"},
    "database": {"engine": "postgres", "version": "16", "instance_class": "db.t4g.micro", "multi_az": True, "storage_gib": 20},
    "network": {"vpc_cidr": "10.42.0.0/16", "azs": 2, "nat_gateways": 1},
    "registry": {"repositories": ["inventory-service"]},
    "outputs": ["cluster_name", "ecr_repository_url", "db_endpoint"],
    "spec_coverage": [
        {"requirement": "SEC3", "how": "RDS in private subnets, SG only from EKS nodes"},
        {"requirement": "NFR1", "how": "Managed node group across 2 AZs, RDS Multi-AZ"},
        {"requirement": "TC2", "how": "t4g.medium Graviton nodes"},
    ],
}

K8S_VALIDATION = {
    "passed": True, "score": 92,
    "checks": [
        {"id": "schema", "tool": "kubeconform", "status": "pass", "resource": "Deployment/inventory-service", "details": "valid"},
        {"id": "resources", "tool": "builtin", "status": "pass", "resource": "Deployment/inventory-service",
         "details": "requests and limits set"},
        {"id": "non-root", "tool": "builtin", "status": "pass", "resource": "Deployment/inventory-service",
         "details": "runAsNonRoot: true"},
        {"id": "read-only-fs", "tool": "builtin", "status": "warn", "resource": "Deployment/inventory-service",
         "details": "fixed: readOnlyRootFilesystem added"},
        {"id": "pdb", "tool": "builtin", "status": "pass", "resource": "PodDisruptionBudget/inventory-service",
         "details": "minAvailable: 1"},
    ],
    "fixes_applied": [{"file": "k8s/base/deployment.yaml", "change": "added readOnlyRootFilesystem"}],
    "live_deploy": {"status": "skipped", "cluster": "kind-transform-workshop", "image": "inventory-service:dev",
                    "rollout": "not requested", "smoke_tests": []},
}

FINOPS = {
    "currency": "USD", "pricing_source": "static-2026-10",
    "current": {"monthly_usd": 69.5, "items": [{"item": "EC2 t3.large on-demand", "monthly_usd": 60.7},
                                                {"item": "EBS gp2 30 GiB", "monthly_usd": 3.0},
                                                {"item": "Data transfer", "monthly_usd": 5.8}]},
    "target_as_designed": {"monthly_usd": 236.4, "items": [{"item": "EKS control plane", "monthly_usd": 73.0},
                                                            {"item": "2x t4g.medium nodes", "monthly_usd": 49.1},
                                                            {"item": "NAT gateway", "monthly_usd": 32.9},
                                                            {"item": "RDS db.t4g.micro Multi-AZ", "monthly_usd": 46.7},
                                                            {"item": "ALB", "monthly_usd": 22.3},
                                                            {"item": "ECR, logs, transfer", "monthly_usd": 12.4}]},
    "target_optimized": {"monthly_usd": 198.2, "items": [{"item": "EKS control plane", "monthly_usd": 73.0},
                                                          {"item": "2x t4g.small nodes", "monthly_usd": 24.5},
                                                          {"item": "NAT gateway", "monthly_usd": 32.9},
                                                          {"item": "RDS db.t4g.micro Multi-AZ", "monthly_usd": 46.7},
                                                          {"item": "ALB", "monthly_usd": 21.1}]},
    "budget_usd": 450, "within_budget": True,
    "rightsizing": {"pod_requests": {"cpu": "100m", "memory": "160Mi"}, "pod_limits": {"cpu": "500m", "memory": "256Mi"},
                    "replicas_min": 2, "replicas_max": 6, "node_instance_types": ["t4g.small"], "node_min": 2, "node_max": 4,
                    "db_instance_class": "db.t4g.micro"},
    "recommendations": [
        {"id": "FO-01", "title": "t4g.small nodes instead of t4g.medium", "monthly_savings_usd": 24.6, "effort": "low",
         "risk": "low", "applies_to": "terraform"},
        {"id": "FO-02", "title": "Compute Savings Plan (1 year)", "monthly_savings_usd": 9.8, "effort": "low",
         "risk": "low", "applies_to": "process"},
        {"id": "FO-03", "title": "Share the ALB through an Ingress group", "monthly_savings_usd": 3.8, "effort": "medium",
         "risk": "low", "applies_to": "k8s"},
    ],
}

# What the k8s_validation agent reports when options.simulate_drift injected hand-edit violations.
DRIFT = [{"file": "k8s/base/deployment.yaml", "change": "removed resources.limits (kubectl edit)"},
         {"file": "k8s/base/deployment.yaml", "change": "image tag changed to :latest"},
         {"file": "k8s/base/deployment.yaml", "change": "securityContext.runAsNonRoot removed"}]

DATA = {"discovery": ASSESSMENT, "requirements": SPEC, "modernization": MODERNIZATION, "terraform": TERRAFORM,
        "k8s_validation": K8S_VALIDATION, "finops": FINOPS}

ARTIFACTS = {
    "discovery": {"reports/assessment.md": "# Assessment: legacy-inventory\n\nReadiness **62/100**. Strategy: replatform.\n"},
    "requirements": {"reports/migration-spec.md": "# Migration spec: inventory-service\n\n| ID | Requirement |\n|---|---|\n"
                                                  "| FR1 | Keep the JSON API contract |\n"},
    "modernization": {
        "microservice/app/main.py": "from flask import Flask, jsonify\n\napp = Flask(__name__)\n\n\n"
                                    "@app.get('/healthz')\ndef healthz():\n    return jsonify(status='ok')\n",
        "microservice/requirements.txt": "Flask==3.1.0\ngunicorn==23.0.0\npsycopg[binary]==3.2.3\n",
        "microservice/Dockerfile": "FROM python:3.12-slim\nUSER 10001\nEXPOSE 8080\n"
                                   "CMD [\"gunicorn\", \"-b\", \"0.0.0.0:8080\", \"app.main:app\"]\n",
        "k8s/base/kustomization.yaml": "resources:\n  - deployment.yaml\n  - service.yaml\n",
        "k8s/base/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: inventory-service\n",
        "k8s/base/service.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: inventory-service\n",
    },
    "terraform": {
        "terraform/versions.tf": "terraform {\n  required_version = \">= 1.6\"\n}\n",
        "terraform/main.tf": "module \"eks\" {\n  source  = \"terraform-aws-modules/eks/aws\"\n  version = \"~> 21.0\"\n}\n",
        "terraform/outputs.tf": "output \"cluster_name\" {\n  value = module.eks.cluster_name\n}\n",
    },
    "k8s_validation": {
        "reports/validation-report.md": "# Validation report\n\nScore **92/100**, 1 fix applied.\n",
        "k8s/base/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: inventory-service\n"
                                    "# fixed: readOnlyRootFilesystem: true\n",
    },
    "finops": {"reports/finops-report.md": "# FinOps report\n\nOptimized target: **$198/month** (budget $450).\n"},
}

SUMMARIES = {
    "discovery": "Flask 2.0 monolith with SQLite on one t3.large (4% CPU). 1 critical and 2 high findings. Replatform.",
    "requirements": "Migration spec for inventory-service: EKS arm64, RDS PostgreSQL 16, 2-6 replicas, USD 450 budget.",
    "modernization": "Refactored into inventory-service: parameterized SQL, env-based secrets, health probes, non-root.",
    "terraform": "Adapted the EKS blueprint: VPC (2 AZ), EKS 1.33 on t4g.medium, ECR, RDS Multi-AZ. Validate passed.",
    "k8s_validation": "All policy checks pass after 1 fix (readOnlyRootFilesystem). Score 92/100.",
    "finops": "Current USD 69.5/month; target USD 236 as designed, USD 198 optimized. Within the USD 450 budget.",
}


def fake_result(stage: str, request: AgentRequest) -> dict[str, Any]:
    """A complete `result` event for `stage`; revisions echo the feedback in the summary."""
    summary = SUMMARIES[stage]
    if request.feedback:
        summary = f"Revised per feedback ({request.feedback!r}). {summary}"
    artifacts, data = dict(ARTIFACTS[stage]), dict(DATA[stage])
    if stage == "k8s_validation" and request.options.get("simulate_drift"):
        data["drift_injected"] = DRIFT
        data["fixes_applied"] = [*data["fixes_applied"], *({"file": d["file"], "change": f"reverted drift: {d['change']}"}
                                                           for d in DRIFT)]
        summary = f"Detected and fixed {len(DRIFT)} simulated manual edits. {summary}"
    return {
        "type": "result", "stage": stage, "summary": summary, "data": data, "artifacts": artifacts,
        "metrics": {"model": DEFAULT_MODELS[stage], "latency_s": 1.2, "input_tokens": 4200, "output_tokens": 900,
                    "cycles": 3, "submitted": True},
    }
