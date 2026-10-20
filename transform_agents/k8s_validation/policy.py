"""Deterministic Kubernetes policy baseline (spec TC5 + SEC4), evaluated on rendered manifests.

Each rule has a stable id (POL-001...) so findings can be tracked across runs, reports and the UI.
The checks are plain Python over parsed YAML: no cluster, network or LLM needed.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
# Overlays that are not production: availability rules (POL-010, POL-016) only warn there (spec NFR3 is for prod).
NON_PROD_TARGET = re.compile(r"(^|/)(local|dev|kind|test|ci|sandbox)$")
AVAILABILITY_RULES = {"POL-010", "POL-016"}
# Dev-only datastores an overlay may run in-cluster (the spec puts the real database on RDS).
DEV_DATASTORE_IMAGE = re.compile(r"(^|/)(postgres|postgresql|mysql|mariadb|redis|valkey|mongo)(:|@|$)")


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    hint: str


RULES: dict[str, Rule] = {r.id: r for r in [
    Rule("POL-001", "resource requests and limits", "high",
         "set resources.requests and resources.limits (cpu and memory) on the container"),
    Rule("POL-002", "liveness probe", "high", "add a livenessProbe (httpGet on the health path) to the container"),
    Rule("POL-003", "readiness probe", "high", "add a readinessProbe (httpGet on the readiness path) to the container"),
    Rule("POL-004", "runAsNonRoot", "high",
         "set securityContext.runAsNonRoot: true (and a numeric runAsUser such as 10001) on the pod or container"),
    Rule("POL-005", "readOnlyRootFilesystem", "high",
         "add `readOnlyRootFilesystem: true` to the container securityContext; mount an emptyDir at /tmp if needed"),
    Rule("POL-006", "allowPrivilegeEscalation=false", "high",
         "add `allowPrivilegeEscalation: false` to the container securityContext"),
    Rule("POL-007", "drop ALL capabilities", "medium",
         "add `capabilities: {drop: [\"ALL\"]}` to the container securityContext"),
    Rule("POL-008", "seccompProfile RuntimeDefault", "medium",
         "add `seccompProfile: {type: RuntimeDefault}` to the pod securityContext"),
    Rule("POL-009", "pinned image tag", "high",
         "use an explicit, immutable tag (e.g. 1.0.0) instead of `latest` or no tag; kustomize `images:` can set it"),
    Rule("POL-010", "at least 2 replicas", "high",
         "set spec.replicas >= 2, or an HPA with minReplicas >= 2 targeting the Deployment"),
    Rule("POL-011", "PodDisruptionBudget", "high",
         "add a PodDisruptionBudget (maxUnavailable: 1) whose selector matches the pod labels, and list it in kustomization.yaml"),
    Rule("POL-012", "HorizontalPodAutoscaler", "medium",
         "add an autoscaling/v2 HPA (cpu target) whose scaleTargetRef names the Deployment, and list it in kustomization.yaml"),
    Rule("POL-013", "Service selector matches pods", "critical",
         "make the Service spec.selector a subset of the pod template labels"),
    Rule("POL-014", "Service targetPort matches containerPort", "critical",
         "set the Service targetPort to the containerPort number or its port name"),
    Rule("POL-015", "probe ports match containerPort", "high",
         "point probe ports at a declared containerPort number or port name"),
    Rule("POL-016", "PDB allows voluntary disruption", "medium",
         "use `maxUnavailable: 1` (or a minAvailable lower than the minimum replicas)"),
]}


@dataclass
class Finding:
    id: str
    status: str  # fail | warn
    resource: str  # Kind/name
    message: str
    container: str | None = None
    target: str = ""
    fix: str = ""  # instance-specific fix snippet (falls back to the rule hint)

    @property
    def rule(self) -> Rule:
        return RULES[self.id]

    @property
    def severity(self) -> str:
        return self.rule.severity if self.status == "fail" else "low"

    def key(self) -> tuple:
        return self.id, self.resource, self.container, self.message

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(title=self.rule.title, severity=self.severity, fix=self.fix or self.rule.hint)
        return out


@dataclass
class Workload:
    kind: str
    name: str
    namespace: str
    pod_spec: dict
    pod_labels: dict[str, str]
    selector: dict[str, str] = field(default_factory=dict)
    replicas: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.kind}/{self.name}"

    @property
    def long_running(self) -> bool:
        return self.kind in ("Deployment", "StatefulSet")

    def containers(self, include_init: bool = True) -> list[dict]:
        main = [c for c in _l(self.pod_spec.get("containers")) if isinstance(c, dict)]
        init = [c for c in _l(self.pod_spec.get("initContainers")) if isinstance(c, dict)] if include_init else []
        return main + init

    @property
    def dev_datastore(self) -> bool:
        images = [str(c.get("image", "")) for c in self.containers(include_init=False)]
        return bool(images) and all(DEV_DATASTORE_IMAGE.search(i) for i in images)


def _d(value: Any) -> dict:
    """Coerce to dict: manifests edited by an LLM may be valid YAML but the wrong shape."""
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _ports(container: dict) -> list[dict]:
    return [p for p in _l(container.get("ports")) if isinstance(p, dict)]


def _ns(doc: dict) -> str:
    return str(_d(doc.get("metadata")).get("namespace") or "default")


def _name(doc: dict) -> str:
    return str(_d(doc.get("metadata")).get("name") or "?")


def workloads(docs: list[dict]) -> list[Workload]:
    out = []
    for doc in docs:
        kind, spec = doc.get("kind"), _d(doc.get("spec"))
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
            template = _d(spec.get("template"))
        elif kind == "CronJob":
            template = _d(_d(_d(spec.get("jobTemplate")).get("spec")).get("template"))
        else:
            continue
        labels = _d(_d(template.get("metadata")).get("labels"))
        selector = _d(_d(spec.get("selector")).get("matchLabels")) if kind != "CronJob" else {}
        replicas = spec.get("replicas") if kind in ("Deployment", "StatefulSet", "ReplicaSet") else None
        replicas = replicas if isinstance(replicas, int) else (1 if kind in ("Deployment", "StatefulSet") else None)
        out.append(Workload(kind, _name(doc), _ns(doc), _d(template.get("spec")), dict(labels), dict(selector),
                            replicas))
    return out


def parse_image(image: str) -> tuple[str, str | None, str | None]:
    """Split an image reference into (name, tag, digest)."""
    name, _, digest = image.partition("@")
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
        return name, tag, digest or None
    return name, None, digest or None


def _matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _sc(container: dict) -> dict:
    return _d(container.get("securityContext"))


def _yaml_labels(labels: dict[str, str], indent: int) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}{k}: {v}" for k, v in labels.items())


def is_production(target: str) -> bool:
    return not NON_PROD_TARGET.search(target)


def check_policies(docs: list[dict], target: str = "", replicas_min: int = 2) -> list[Finding]:
    """Evaluate every rule against rendered manifests. Returns only failures and warnings."""
    docs = [d for d in docs if isinstance(d, dict)]
    found: list[Finding] = []
    production = is_production(target)
    loads = workloads(docs)
    services = [d for d in docs if d.get("kind") == "Service"]
    pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    hpas = [d for d in docs if d.get("kind") == "HorizontalPodAutoscaler"]

    def add(rule_id: str, ref: str, message: str, container: str | None = None, status: str = "fail",
            fix: str = "") -> None:
        if rule_id in AVAILABILITY_RULES and not production and status == "fail":
            status, message = "warn", f"{message} (accepted in non-production overlay {target})"
        found.append(Finding(rule_id, status, ref, message, container, target, fix))

    for w in loads:
        if w.dev_datastore:  # dev-only database in an overlay (see exempt_workloads): baseline not enforced
            continue
        pod_sc = _d(w.pod_spec.get("securityContext"))
        for c in w.containers():
            cname = str(c.get("name", "?"))
            sc = _sc(c)
            res = _d(c.get("resources"))
            missing = [f"{kind}.{r}" for kind in ("requests", "limits") for r in ("cpu", "memory")
                       if not _d(res.get(kind)).get(r)]
            if missing:
                add("POL-001", w.ref, f"missing resources: {', '.join(missing)}", cname)
            non_root = sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot"))
            run_as = sc.get("runAsUser", pod_sc.get("runAsUser"))
            if non_root is not True or run_as == 0:
                add("POL-004", w.ref, "runAsNonRoot is not true" if run_as != 0 else "runs as uid 0", cname)
            if sc.get("readOnlyRootFilesystem") is not True:
                add("POL-005", w.ref, "securityContext.readOnlyRootFilesystem is not true", cname,
                    fix=f"in container `{cname}` securityContext add `readOnlyRootFilesystem: true`")
            if sc.get("allowPrivilegeEscalation") is not False:
                add("POL-006", w.ref, "securityContext.allowPrivilegeEscalation is not false", cname)
            drops = [str(x).upper() for x in _l(_d(sc.get("capabilities")).get("drop"))]
            if "ALL" not in drops:
                add("POL-007", w.ref, "capabilities.drop does not include ALL", cname)
            seccomp = _d(sc.get("seccompProfile") or pod_sc.get("seccompProfile")).get("type")
            if seccomp not in ("RuntimeDefault", "Localhost"):
                add("POL-008", w.ref, "seccompProfile.type is not RuntimeDefault", cname)
            image = str(c.get("image") or "")
            name, tag, digest = parse_image(image)
            if not image or (not digest and (tag is None or tag == "latest")):
                add("POL-009", w.ref, f"image `{image or '(none)'}` is {'untagged' if tag is None else 'latest'}",
                    cname, fix=f"pin the tag, e.g. `image: {name}:1.0.0`, or set newTag for `{name}` under "
                               "`images:` in that target's kustomization.yaml")

        if not w.long_running:
            continue
        for c in w.containers(include_init=False):
            cname = str(c.get("name", "?"))
            for probe_key, rule_id in (("livenessProbe", "POL-002"), ("readinessProbe", "POL-003")):
                probe = _d(c.get(probe_key))
                if not probe:
                    add(rule_id, w.ref, f"{probe_key} missing", cname)
                    continue
                handler = _d(probe.get("httpGet") or probe.get("tcpSocket") or probe.get("grpc"))
                port = handler.get("port")
                own = {p.get("containerPort"): p.get("name") for p in _ports(c)}
                named_missing = isinstance(port, str) and port not in own.values()
                number_missing = isinstance(port, int) and own and port not in own
                if named_missing or number_missing:
                    add("POL-015", w.ref, f"{probe_key} port {port!r} is not a declared containerPort/name", cname)

        hpa = next((h for h in hpas if _ns(h) == w.namespace and _scale_target(h) == (w.kind, w.name)), None)
        hpa_min = _d(hpa.get("spec")).get("minReplicas", 1) if hpa else 0
        hpa_min = hpa_min if isinstance(hpa_min, int) else 1
        if (w.replicas or 1) < replicas_min and hpa_min < replicas_min:
            add("POL-010", w.ref, f"replicas={w.replicas} and no HPA with minReplicas >= {replicas_min}")
        if hpa is None and w.kind == "Deployment":
            add("POL-012", w.ref, "no HorizontalPodAutoscaler targets this Deployment",
                fix=_hpa_snippet(w.name, replicas_min))

        pdb = next((p for p in pdbs if _ns(p) == w.namespace and _matches(
            _d(_d(_d(p.get("spec")).get("selector")).get("matchLabels")), w.pod_labels)), None)
        if pdb is None:
            add("POL-011", w.ref, "no PodDisruptionBudget selects these pods",
                fix=_pdb_snippet(w.name, w.selector or w.pod_labels))
        else:
            spec = _d(pdb.get("spec"))
            floor = max(w.replicas or 1, hpa_min)
            min_avail = spec.get("minAvailable")
            if isinstance(min_avail, int) and min_avail >= floor:
                add("POL-016", f"PodDisruptionBudget/{_name(pdb)}",
                    f"minAvailable={min_avail} >= replicas ({floor}) blocks node drains")
            if spec.get("maxUnavailable") in (0, "0%"):
                add("POL-016", f"PodDisruptionBudget/{_name(pdb)}", "maxUnavailable=0 blocks node drains")

    for svc in services:
        spec = _d(svc.get("spec"))
        selector = _d(spec.get("selector"))
        if not selector or spec.get("type") == "ExternalName":
            continue
        ref = f"Service/{_name(svc)}"
        targets = [w for w in loads if w.namespace == _ns(svc) and _matches(selector, w.pod_labels) and w.long_running]
        if not targets:
            add("POL-013", ref, f"selector {selector} matches no Deployment/StatefulSet pod labels")
            continue
        for p in [p for p in _l(spec.get("ports")) if isinstance(p, dict)]:
            target_port = p.get("targetPort", p.get("port"))
            for w in targets:
                declared = {(cp.get("containerPort"), cp.get("name")) for c in w.containers(False) for cp in _ports(c)}
                if not declared:
                    add("POL-014", ref, f"{w.ref} declares no containerPort to match targetPort {target_port!r}",
                        status="warn")
                elif not any(target_port in pair for pair in declared):
                    add("POL-014", ref, f"targetPort {target_port!r} not in {w.ref} ports "
                                        f"{sorted(str(n or c) for c, n in declared)}")
    return found


def _scale_target(hpa: dict) -> tuple[str | None, str | None]:
    ref = _d(_d(hpa.get("spec")).get("scaleTargetRef"))
    return ref.get("kind"), ref.get("name")


def exempt_workloads(docs: list[dict]) -> list[str]:
    """Workloads skipped by the baseline: dev-only in-cluster datastores (production uses a managed database)."""
    return [w.ref for w in workloads([d for d in docs if isinstance(d, dict)]) if w.dev_datastore]


def _pdb_snippet(name: str, labels: dict[str, str]) -> str:
    return (f"apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: {name}\n"
            f"spec:\n  maxUnavailable: 1\n  selector:\n    matchLabels:\n{_yaml_labels(labels, 6)}\n")


def _hpa_snippet(name: str, replicas_min: int) -> str:
    return (f"apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: {name}\nspec:\n"
            f"  scaleTargetRef:\n    apiVersion: apps/v1\n    kind: Deployment\n    name: {name}\n"
            f"  minReplicas: {replicas_min}\n  maxReplicas: 6\n  metrics:\n    - type: Resource\n      resource:\n"
            "        name: cpu\n        target:\n          type: Utilization\n          averageUtilization: 70\n")


def builtin_schema_check(docs: list[Any]) -> list[tuple[str, str]]:
    """Offline structural checks used when kubeconform or its schemas are unavailable. Returns (resource, problem)."""
    expected = {"Deployment": "apps/v1", "StatefulSet": "apps/v1", "DaemonSet": "apps/v1", "Service": "v1",
                "ConfigMap": "v1", "Secret": "v1", "Namespace": "v1", "ServiceAccount": "v1",
                "PersistentVolumeClaim": "v1", "CronJob": "batch/v1", "Job": "batch/v1",
                "HorizontalPodAutoscaler": "autoscaling/v2", "PodDisruptionBudget": "policy/v1",
                "Ingress": "networking.k8s.io/v1", "NetworkPolicy": "networking.k8s.io/v1"}
    problems: list[tuple[str, str]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            problems.append(("?", "document is not a mapping"))
            continue
        kind, api = doc.get("kind"), doc.get("apiVersion")
        ref = f"{kind}/{_name(doc)}"
        if not isinstance(kind, str) or not isinstance(api, str):
            problems.append((ref, "missing apiVersion or kind"))
            continue
        if not _d(doc.get("metadata")).get("name"):
            problems.append((ref, "missing metadata.name"))
        if kind in expected and api != expected[kind]:
            problems.append((ref, f"apiVersion {api} (expected {expected[kind]})"))
    for w in workloads([d for d in docs if isinstance(d, dict)]):
        if not w.containers(False):
            problems.append((w.ref, "pod template has no containers"))
        if w.kind in ("Deployment", "StatefulSet") and not _matches(w.selector, w.pod_labels):
            problems.append((w.ref, "spec.selector.matchLabels does not match the pod template labels"))
        for c in w.containers():
            if not c.get("name") or not c.get("image"):
                problems.append((w.ref, "container without name or image"))
            for p in _ports(c):
                if not isinstance(p.get("containerPort"), int):
                    problems.append((w.ref, f"containerPort {p.get('containerPort')!r} is not an integer"))
    return problems
