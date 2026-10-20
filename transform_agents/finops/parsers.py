"""Deterministic parsers for the sizing facts in `k8s/**` (YAML) and `terraform/**` (HCL)."""

import json
import re
from typing import Any

import hcl2
import yaml

# --------------------------------------------------------------------------- quantities

_MEM_UNITS = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024**2,
              "k": 1e3 / 2**20, "M": 1e6 / 2**20, "G": 1e9 / 2**20, "T": 1e12 / 2**20}


def parse_cpu_m(value: Any) -> int:
    """Kubernetes CPU quantity -> millicores ("250m" -> 250, "0.5" -> 500, 2 -> 2000)."""
    text = str(value).strip()
    return round(float(text[:-1])) if text.endswith("m") else round(float(text) * 1000)


def parse_mem_mi(value: Any) -> int:
    """Kubernetes memory quantity -> MiB ("128Mi" -> 128, "1Gi" -> 1024, "256M" -> 244, bytes -> MiB)."""
    text = str(value).strip()
    for unit in sorted(_MEM_UNITS, key=len, reverse=True):
        if text.endswith(unit):
            return round(float(text[: -len(unit)]) * _MEM_UNITS[unit])
    return round(float(text) / 2**20)


def fmt_cpu(m: int) -> str:
    return f"{m}m"


def fmt_mem(mi: int) -> str:
    return f"{mi}Mi"


# --------------------------------------------------------------------------- kubernetes


def _resources(containers: list[dict]) -> dict[str, dict[str, str]]:
    """Sum requests/limits over a pod's containers, returned as k8s quantities."""
    out: dict[str, dict[str, str]] = {}
    for kind in ("requests", "limits"):
        cpu = mem = 0
        seen = False
        for c in containers or []:
            res = (c.get("resources") or {}).get(kind) or {}
            if "cpu" in res:
                cpu, seen = cpu + parse_cpu_m(res["cpu"]), True
            if "memory" in res:
                mem, seen = mem + parse_mem_mi(res["memory"]), True
        if seen:
            out[kind] = {"cpu": fmt_cpu(cpu), "memory": fmt_mem(mem)}
    return out


def parse_k8s(files: dict[str, str]) -> dict[str, Any]:
    """Extract replicas, requests/limits, HPA bounds, CronJobs and ingress from k8s manifests.

    Uses `k8s/base/**` when present (the local kind overlay is not production sizing).
    """
    paths = [p for p in files if p.startswith("k8s/") and p.endswith((".yaml", ".yml"))]
    base = [p for p in paths if p.startswith("k8s/base/")]
    paths = sorted(base or [p for p in paths if "/overlays/local/" not in p] or paths)
    out: dict[str, Any] = {"files_parsed": paths, "deployments": [], "hpas": [], "cronjobs": [],
                           "ingresses": 0, "load_balancer_services": 0, "pdbs": [], "warnings": []}
    for path in paths:
        try:
            docs = [d for d in yaml.safe_load_all(files[path]) if isinstance(d, dict)]
        except yaml.YAMLError as exc:
            out["warnings"].append(f"{path}: invalid YAML ({str(exc).splitlines()[0]})")
            continue
        for doc in docs:
            kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name", "?")
            spec = doc.get("spec") or {}
            if kind in ("Deployment", "StatefulSet"):
                pod = (spec.get("template") or {}).get("spec") or {}
                out["deployments"].append({"name": name, "kind": kind, "replicas": spec.get("replicas", 1),
                                           "containers": [c.get("name") for c in pod.get("containers") or []],
                                           **_resources(pod.get("containers"))})
            elif kind == "HorizontalPodAutoscaler":
                target = None
                for m in spec.get("metrics") or []:
                    res = m.get("resource") or {}
                    if res.get("name") == "cpu":
                        target = (res.get("target") or {}).get("averageUtilization")
                out["hpas"].append({"name": name, "target": (spec.get("scaleTargetRef") or {}).get("name"),
                                    "min": spec.get("minReplicas", 1), "max": spec.get("maxReplicas"),
                                    "cpu_target_pct": target})
            elif kind == "CronJob":
                pod = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
                out["cronjobs"].append({"name": name, "schedule": spec.get("schedule"),
                                        **_resources(pod.get("containers"))})
            elif kind == "Ingress":
                out["ingresses"] += 1
            elif kind == "Service" and spec.get("type") == "LoadBalancer":
                out["load_balancer_services"] += 1
            elif kind == "PodDisruptionBudget":
                out["pdbs"].append({"name": name, "minAvailable": spec.get("minAvailable"),
                                    "maxUnavailable": spec.get("maxUnavailable")})
    if not paths:
        out["warnings"].append("no k8s manifests found; using spec/modernization values")
    for d in out["deployments"]:
        if "requests" not in d:
            out["warnings"].append(f"Deployment/{d['name']} has no resource requests")
    return out


def primary_workload(k8s: dict[str, Any]) -> dict[str, Any] | None:
    """The Deployment an HPA targets (else the first one), merged with its HPA bounds."""
    deps = k8s.get("deployments") or []
    if not deps:
        return None
    hpa_targets = {h["target"]: h for h in k8s.get("hpas") or []}
    dep = next((d for d in deps if d["name"] in hpa_targets), deps[0])
    hpa = hpa_targets.get(dep["name"])
    return {**dep, "replicas_min": hpa["min"] if hpa else dep["replicas"],
            "replicas_max": (hpa["max"] or dep["replicas"]) if hpa else dep["replicas"],
            "hpa_target_pct": hpa["cpu_target_pct"] if hpa else None}


# --------------------------------------------------------------------------- terraform

def _unquote(value: Any) -> Any:
    """python-hcl2 v7+ keeps HCL string quotes ("\"x\"") and block markers; strip both."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if isinstance(value, list):
        return [_unquote(v) for v in value]
    if isinstance(value, dict):
        return {_unquote(k): _unquote(v) for k, v in value.items() if k != "__is_block__"}
    return value


def _blocks(doc: dict, kind: str) -> list[tuple[str, dict]]:
    """[(name, body)] for `variable` / `module` blocks."""
    return [(name, body) for block in doc.get(kind, []) for name, body in block.items()]


def _resources_of(doc: dict, rtype: str) -> list[tuple[str, dict]]:
    return [(name, body) for block in doc.get("resource", []) for t, named in block.items() if t == rtype
            for name, body in named.items()]


_WHOLE_REF = re.compile(r"\$\{(!?)(var|local)\.([A-Za-z0-9_-]+)\}")
_WRAPPED_VAR = re.compile(r"\$\{var\.([A-Za-z0-9_-]+)\}")
_BARE_VAR = re.compile(r"\bvar\.([A-Za-z0-9_-]+)")


class _Resolver:
    """Resolves `${var.x}`, `${!var.x}` and `${local.x}` from variable defaults (overridden by tfvars) and locals.

    Bare numeric/bool variables inside expressions are substituted too, so `range(var.az_count)` becomes
    `range(2)` and can be counted.
    """

    def __init__(self, variables: dict[str, Any], locals_: dict[str, Any] | None = None):
        self.vars, self.locals = variables, {}
        self.locals = {k: self(v) for k, v in (locals_ or {}).items()}

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            whole = _WHOLE_REF.fullmatch(value)
            if whole:
                neg, scope, name = whole.groups()
                table = self.vars if scope == "var" else self.locals
                if name in table and not neg:
                    return table[name]
                if name in table and isinstance(table[name], bool):
                    return not table[name]
            text = _WRAPPED_VAR.sub(lambda m: str(self.vars.get(m.group(1), m.group(0))), value)
            scalars = {k: v for k, v in self.vars.items() if isinstance(v, (int, float, bool))}
            return _BARE_VAR.sub(lambda m: str(scalars[m.group(1)]).lower() if m.group(1) in scalars
                                 else m.group(0), text)
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        return value


def _count(value: Any) -> int | None:
    """Length of a list, or of `slice(x, 0, N)` / `[for i in range(N) : ...]` expressions."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        m = re.search(r"slice\([^,]+,\s*0\s*,\s*(\d+)\s*\)", value) or re.search(r"range\(\s*(\d+)\s*\)", value)
        if m:
            return int(m.group(1))
    return None


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return default


def parse_terraform(files: dict[str, str]) -> dict[str, Any]:
    """Extract node groups, Kubernetes version, RDS, NAT, load balancers and logs from terraform/**."""
    tf_paths = sorted(p for p in files if p.startswith("terraform/") and p.endswith(".tf"))
    var_paths = sorted(p for p in files if p.startswith("terraform/")
                       and (p.endswith(("/terraform.tfvars", ".auto.tfvars"))))
    out: dict[str, Any] = {"files_parsed": tf_paths + var_paths, "eks_present": False, "kubernetes_version": None,
                           "node_groups": [], "database": None, "nat_gateways": 0, "azs": None,
                           "load_balancers": 0, "log_retention_days": None, "control_plane_log_retention_days": None,
                           "secrets": 0, "ecr_repositories": 0,
                           "warnings": []}
    docs = []
    for path in tf_paths:
        try:
            docs.append(_unquote(hcl2.loads(files[path])))
        except Exception as exc:  # noqa: BLE001 - lark raises many error types; report and continue
            out["warnings"].append(f"{path}: HCL parse error ({str(exc).splitlines()[0][:120]})")
    variables = {name: body.get("default") for d in docs for name, body in _blocks(d, "variable")}
    for path in var_paths:
        try:
            variables.update(_unquote(hcl2.loads(files[path])))
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append(f"{path}: tfvars parse error ({str(exc).splitlines()[0][:120]})")
    locals_ = {k: v for d in docs for block in d.get("locals", []) for k, v in block.items()}
    resolve = _Resolver(variables, locals_)
    modules = [(name, resolve(body)) for d in docs for name, body in _blocks(d, "module")]
    resources = lambda rtype: [(n, resolve(b)) for d in docs for n, b in _resources_of(d, rtype)]

    for _, mod in modules:
        source = str(mod.get("source", ""))
        if "terraform-aws-modules/eks" in source:
            out["eks_present"] = True
            out["kubernetes_version"] = mod.get("kubernetes_version") or mod.get("cluster_version")
            out["control_plane_log_retention_days"] = _as_int(mod.get("cloudwatch_log_group_retention_in_days"), 90)
            for name, ng in (mod.get("eks_managed_node_groups") or {}).items():
                out["node_groups"].append(_node_group(name, ng))
            if (mod.get("compute_config") or {}).get("enabled"):
                out["warnings"].append("EKS Auto Mode detected: priced as managed nodes (Auto Mode fee not modelled)")
        elif "terraform-aws-modules/vpc" in source:
            out["nat_gateways"], out["azs"] = _nat_count(mod)
        elif "terraform-aws-modules/rds" in source:
            out["database"] = _database(mod)
    for _, cluster in resources("aws_eks_cluster"):
        out["eks_present"], out["kubernetes_version"] = True, cluster.get("version", out["kubernetes_version"])
    for name, ng in resources("aws_eks_node_group"):
        scaling = (ng.get("scaling_config") or [{}])[0]
        out["node_groups"].append(_node_group(name, {**ng, **scaling}))
    for _, db in resources("aws_db_instance"):
        out["database"] = _database(db)
    nat = resources("aws_nat_gateway")
    if nat:
        out["nat_gateways"] = sum(_as_int(b.get("count"), 1) or 1 for _, b in nat)
    out["load_balancers"] = sum(1 for t in ("aws_lb", "aws_alb") for _, b in resources(t)
                                if b.get("load_balancer_type", "application") == "application")
    for _, lg in resources("aws_cloudwatch_log_group"):  # explicit app log groups (default: never expire)
        if lg.get("retention_in_days") is not None:
            out["log_retention_days"] = _as_int(lg["retention_in_days"], out["log_retention_days"])
    out["secrets"] = len(resources("aws_secretsmanager_secret")) + int(bool(
        out["database"] and out["database"].get("managed_master_secret")))
    out["ecr_repositories"] = len(resources("aws_ecr_repository"))
    if out["kubernetes_version"] is not None:
        out["kubernetes_version"] = str(out["kubernetes_version"])
    if not tf_paths:
        out["warnings"].append("no terraform files found; using inputs.terraform/spec values")
    elif not out["eks_present"]:
        out["warnings"].append("no EKS cluster found in terraform")
    return out


def _node_group(name: str, ng: dict) -> dict[str, Any]:
    min_size = _as_int(ng.get("min_size"), 1)
    types = ng.get("instance_types") or ["t3.medium"]  # EKS managed node group default
    bdm = ng.get("block_device_mappings")
    ebs = next((((v or {}).get("ebs") or {}) for v in bdm.values()), {}) if isinstance(bdm, dict) else {}
    ami = str(ng.get("ami_type", ""))
    return {
        "name": name,
        "instance_types": [str(t) for t in types] if isinstance(types, list) else [str(types)],
        "min": min_size,
        "max": _as_int(ng.get("max_size"), min_size),
        "desired": _as_int(ng.get("desired_size"), min_size),
        "capacity_type": str(ng.get("capacity_type") or "ON_DEMAND"),
        "architecture": None if "${" in ami or not ami else "arm64" if "ARM" in ami.upper() else "x86_64",
        "disk_gib": _as_int(ebs.get("volume_size") or ng.get("disk_size"), 20),
        "volume_type": str(ebs.get("volume_type") or "gp3"),  # EKS AL2023 AMI default: 20 GiB gp3
        "volume_type_explicit": bool(ebs.get("volume_type")),
    }


def _database(db: dict) -> dict[str, Any]:
    return {
        "engine": db.get("engine"),
        "instance_class": db.get("instance_class"),
        "multi_az": _as_bool(db.get("multi_az"), False),
        "storage_gib": _as_int(db.get("allocated_storage"), 20),
        "storage_type": str(db.get("storage_type") or "gp2"),  # AWS default without iops
        "managed_master_secret": _as_bool(db.get("manage_master_user_password"), False),
    }


def _nat_count(vpc: dict) -> tuple[int, int | None]:
    """(NAT gateways, AZs) following the terraform-aws-modules/vpc rules."""
    subnets = _count(vpc.get("private_subnets"))
    n_azs = _count(vpc.get("azs")) or subnets
    if not _as_bool(vpc.get("enable_nat_gateway"), False):
        return 0, n_azs
    if _as_bool(vpc.get("single_nat_gateway"), False):
        return 1, n_azs
    if _as_bool(vpc.get("one_nat_gateway_per_az"), False):
        return n_azs or 2, n_azs
    return subnets or n_azs or 2, n_azs  # module default: one NAT gateway per private subnet
