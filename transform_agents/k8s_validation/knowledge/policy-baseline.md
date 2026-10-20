# Kubernetes policy baseline (TC5 + SEC4)

The k8s_validation agent enforces this baseline with deterministic Python checks (`policy.py`) on the
**rendered** Kustomize output of `k8s/base` and every `k8s/overlays/<name>`. Rule ids are stable, so findings can
be tracked across runs, reports and the UI. In the spirit of Kyverno or OPA Gatekeeper baselines, but with no
cluster needed.

Source requirements (samples/requirements/migration-requirements.md):
- **TC5**: manifests must pass the policy baseline: resource requests/limits, probes, non-root, no `latest` tags,
  PodDisruptionBudget.
- **SEC4**: containers run as non-root with a read-only root filesystem.
- **NFR3**: at least 2 replicas in production, CPU autoscaling.
- **NFR6**: health and readiness endpoints for the orchestrator.

| Id | Rule | Severity | Applies to | Fix |
|----|------|----------|------------|-----|
| POL-001 | resource requests and limits | high | every container (incl. init, CronJob) | `resources.requests` and `resources.limits` for cpu and memory |
| POL-002 | liveness probe | high | Deployment/StatefulSet containers | `livenessProbe.httpGet` on the health path |
| POL-003 | readiness probe | high | Deployment/StatefulSet containers | `readinessProbe.httpGet` on the readiness path |
| POL-004 | runAsNonRoot | high | every container | `runAsNonRoot: true` plus a numeric `runAsUser` (pod or container) |
| POL-005 | readOnlyRootFilesystem | high | every container | `readOnlyRootFilesystem: true`; mount an `emptyDir` at `/tmp` |
| POL-006 | allowPrivilegeEscalation=false | high | every container | `allowPrivilegeEscalation: false` |
| POL-007 | drop ALL capabilities | medium | every container | `capabilities: {drop: ["ALL"]}` |
| POL-008 | seccompProfile RuntimeDefault | medium | every pod | `seccompProfile: {type: RuntimeDefault}` |
| POL-009 | pinned image tag | high | every container | explicit tag or digest, never `latest` or untagged |
| POL-010 | at least 2 replicas | high | Deployment/StatefulSet | `replicas >= 2` or an HPA with `minReplicas >= 2` |
| POL-011 | PodDisruptionBudget | high | Deployment/StatefulSet | a PDB (`maxUnavailable: 1`) whose selector matches the pod labels |
| POL-012 | HorizontalPodAutoscaler | medium | Deployment | an `autoscaling/v2` HPA whose `scaleTargetRef` names the Deployment |
| POL-013 | Service selector matches pods | critical | Service | selector is a subset of the pod template labels |
| POL-014 | Service targetPort matches containerPort | critical | Service | `targetPort` is a declared `containerPort` number or name |
| POL-015 | probe ports match containerPort | high | probes | probe `port` is a declared `containerPort` number or name |
| POL-016 | PDB allows voluntary disruption | medium | PodDisruptionBudget | `maxUnavailable >= 1`, or a `minAvailable` lower than the replicas |

Scoping decisions:
- **Non-production overlays** (`local`, `dev`, `kind`, `test`, `ci`, `sandbox`): the availability rules POL-010 and
  POL-016 only *warn*, because NFR3 applies to production. The local kind overlay may run a single replica.
- **Dev-only datastores** (an in-cluster `postgres`, `mysql`, `redis`... in an overlay) are exempt and reported
  as `skipped`. Production uses the managed database (RDS).

Scoring: start at 100 and subtract per failing check (critical 25, high 10, medium 5, low 2), 30 for a render
failure, 20 for a schema failure, 20 for a failed live deploy, and 2 per warning. `passed` means no failing check
and no failed live deploy.

Schema validation: `kubeconform -strict -ignore-missing-schemas` against the upstream Kubernetes JSON schemas
(strict mode rejects unknown fields such as a misspelled `readOnlyRootFileSystem`). When kubeconform or the
network is missing, built-in structural checks run instead and the check is reported as `warn`.
