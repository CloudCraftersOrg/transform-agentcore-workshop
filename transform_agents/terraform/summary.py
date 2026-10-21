"""What the Terraform actually builds (read back from the code) and how it compares with the migration spec."""

import math
from typing import Any

from .hcl import TfConfig
from .validate import is_graviton


def _first(items: dict) -> tuple[Any, dict]:
    return next(iter(items.items()), (None, {}))


def facts(cfg: TfConfig) -> dict[str, Any]:
    """Resolved key values (variable defaults substituted) for result.data and the spec checks."""
    r = cfg.resolve
    variables = cfg.variables()
    modules = cfg.modules()
    eks = next((b for b in modules.values() if str(b.get("source", "")).endswith("eks/aws")), {})
    vpc = next((b for b in modules.values() if str(b.get("source", "")).endswith("vpc/aws")), {})
    groups = eks.get("eks_managed_node_groups")
    _, group = _first(groups) if isinstance(groups, dict) else (None, {})
    group = group if isinstance(group, dict) else {}
    types = r(group.get("instance_types"))
    arch = r("${var.node_architecture}") if "node_architecture" in variables else None
    if not isinstance(arch, str) or "${" in arch:
        ami = str(r(group.get("ami_type", "")))
        arch = "arm64" if "ARM_64" in ami else "x86_64" if "x86_64" in ami else (
            "arm64" if isinstance(types, list) and types and all(is_graviton(t) for t in types) else "x86_64")
    resources = cfg.resources()
    _, db = _first({k: v for k, v in resources.items() if k[0] == "aws_db_instance"})
    repos = [b for k, b in resources.items() if k[0] == "aws_ecr_repository"]
    az = r("${var.az_count}") if "az_count" in variables else None
    if not isinstance(az, int):
        azs = r(vpc.get("azs"))
        az = len(azs) if isinstance(azs, list) else None
    nat = None
    if vpc:
        enabled, single = r(vpc.get("enable_nat_gateway", False)), r(vpc.get("single_nat_gateway", False))
        nat = 0 if enabled is False else 1 if single is True else az if isinstance(az, int) else None
    providers = cfg.blocks("provider", 1)
    region = next((r(b.get("region")) for _, labs, b in providers if labs[0] == "aws" and "region" in b), None)
    tags = r("${var.tags}") if "tags" in variables else None
    return {
        "region": region,
        "cluster_name": r(eks.get("name")),
        "kubernetes_version": r(eks.get("kubernetes_version")),
        "compute": {"node_instance_types": types, "node_min": r(group.get("min_size")),
                    "node_max": r(group.get("max_size")),
                    "capacity_type": r(group.get("capacity_type", "ON_DEMAND")), "architecture": arch},
        "database": {"engine": r(db.get("engine")), "version": r(db.get("engine_version")),
                     "instance_class": r(db.get("instance_class")), "multi_az": r(db.get("multi_az", False)),
                     "storage_gib": r(db.get("allocated_storage")),
                     "backup_retention_days": r(db.get("backup_retention_period")),
                     "deletion_protection": r(db.get("deletion_protection", False)),
                     "storage_encrypted": r(db.get("storage_encrypted", False)),
                     "publicly_accessible": r(db.get("publicly_accessible", False)),
                     "manage_master_user_password": r(db.get("manage_master_user_password", False))} if db else {},
        "network": {"vpc_cidr": r(vpc.get("cidr")), "azs": az, "nat_gateways": nat},
        "registry": {"repositories": [r(b.get("name")) for b in repos],
                     "immutable_tags": all(r(b.get("image_tag_mutability")) == "IMMUTABLE" for b in repos)},
        "tags": tags if isinstance(tags, dict) else {},
        "modules": [{"name": n, "source": b.get("source"), "version": b.get("version")} for n, b in modules.items()],
        "outputs": cfg.outputs(),
    }


def _cpu(value: object) -> float:
    """Kubernetes CPU quantity ("500m", "1") in vCPU; 0 when unparsable."""
    text = str(value or "0").strip()
    try:
        return float(text[:-1]) / 1000 if text.endswith("m") else float(text)
    except ValueError:
        return 0.0


def _float(value: object) -> float | None:
    try:
        return float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return None


def _int(value: object, default: int) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def expected_values(spec: dict, modernization: dict) -> dict[str, tuple[Any, str]]:
    """Values the mapping rules in the system prompt derive from the spec: {key: (expected, rule)}."""
    svc, target, wl = spec.get("service") or {}, spec.get("target") or {}, spec.get("workload") or {}
    data, nfr, fin = spec.get("data") or {}, spec.get("nfr") or {}, spec.get("finops") or {}
    exp: dict[str, tuple[Any, str]] = {}
    if not spec:
        return exp  # no spec: the blueprint defaults stand
    env = fin.get("environment") or "prod"
    prod = env == "prod"
    if svc.get("name"):
        exp["name"] = (svc["name"], "spec.service.name (cluster name prefix and tags.app)")
    exp["environment_tag"] = (env, "tags.environment = spec.finops.environment")
    if target.get("region"):
        exp["region"] = (target["region"], "spec.target.region")
    if target.get("kubernetes_version"):
        exp["kubernetes_version"] = (str(target["kubernetes_version"]), "spec.target.kubernetes_version")
    if fin.get("prefer_graviton") is not None or target.get("cpu_architecture"):
        arm = bool(fin.get("prefer_graviton")) or target.get("cpu_architecture") == "arm64"
        exp["architecture"] = ("arm64" if arm else "x86_64", "finops.prefer_graviton or target.cpu_architecture")
        big = _cpu(((modernization.get("resources") or {}).get("limits") or {}).get("cpu")) >= 1
        exp["node_instance_types"] = ((["m7g.large"] if big else ["t4g.medium"]) if arm else ["t3.medium"],
                                      "t4g.medium (m7g.large if a pod needs >= 1 vCPU); x86_64: t3.medium")
        size = "small" if prod else "micro"
        exp["db_instance_class"] = (f"db.t4g.{size}" if arm else f"db.t3.{size}", "db.t4g/db.t3, small for prod")
    exp["node_capacity_type"] = ("ON_DEMAND" if prod else "SPOT", "ON_DEMAND for prod, SPOT otherwise")
    node_min = max(2, math.ceil(_int(wl.get("replicas_min"), 2) / 2))
    if wl.get("replicas_min"):
        exp["node_min"] = (node_min, "max(2, ceil(workload.replicas_min / 2))")
    if wl.get("replicas_max"):
        exp["node_max"] = (max(2 * node_min, math.ceil(_int(wl["replicas_max"], 2) / 2)),
                           "max(2 * node_min, ceil(workload.replicas_max / 2))")
    slo = _float(nfr.get("availability_slo_pct"))
    if slo is not None:
        azs = 3 if slo >= 99.99 else 2
        exp["az_count"] = (azs, "3 if nfr.availability_slo_pct >= 99.99 else 2")
        exp["nat_gateways"] = (1 if slo < 99.95 else azs, "single NAT unless availability_slo_pct >= 99.95")
    repo = (modernization.get("image") or {}).get("repository") or modernization.get("service_name") or svc.get("name")
    if repo:
        exp["ecr_repository"] = (repo, "modernization.image.repository")
    if data.get("version"):
        exp["db_version"] = (str(data["version"]), "spec.data.version")
    if data.get("multi_az") is not None:
        exp["db_multi_az"] = (bool(data["multi_az"]), "spec.data.multi_az")
    rpo = _float(nfr.get("rpo_minutes"))
    exp["db_backup_retention_days"] = (7 if prod or (rpo is not None and rpo <= 60) else 1,
                                       "7 if prod or nfr.rpo_minutes <= 60, else 1")
    exp["db_deletion_protection"] = (prod, "true for prod")
    return exp


def spec_checks(f: dict, spec: dict, modernization: dict) -> list[dict[str, Any]]:
    """Compare what the Terraform builds with what the mapping rules expect from the spec."""
    compute, db, net = f.get("compute") or {}, f.get("database") or {}, f.get("network") or {}
    tags = f.get("tags") or {}
    actual = {
        "name": {"cluster_name": f.get("cluster_name"), "tags.app": tags.get("app")},
        "environment_tag": tags.get("environment"),
        "region": f.get("region"),
        "kubernetes_version": None if f.get("kubernetes_version") is None else str(f.get("kubernetes_version")),
        "architecture": compute.get("architecture"),
        "node_instance_types": compute.get("node_instance_types"),
        "db_instance_class": db.get("instance_class"),
        "node_capacity_type": compute.get("capacity_type"),
        "node_min": compute.get("node_min"),
        "node_max": compute.get("node_max"),
        "az_count": net.get("azs"),
        "nat_gateways": net.get("nat_gateways"),
        "ecr_repository": (f.get("registry") or {}).get("repositories") or [],
        "db_version": None if db.get("version") is None else str(db.get("version")),
        "db_multi_az": db.get("multi_az"),
        "db_backup_retention_days": db.get("backup_retention_days"),
        "db_deletion_protection": db.get("deletion_protection"),
    }
    checks = []
    for key, (want, rule) in expected_values(spec, modernization).items():
        got = actual.get(key)
        if key == "name":
            ok = str(got["cluster_name"] or "").startswith(want) and got["tags.app"] == want
        elif key == "ecr_repository":
            ok = want in got
        else:
            ok = got == want
        checks.append({"check": key, "status": "pass" if ok else "mismatch", "expected": want, "actual": got,
                       "rule": rule})
    return checks


def mismatch_lines(checks: list[dict]) -> list[str]:
    return [f"{c['check']}: expected {c['expected']!r} ({c['rule']}), found {c['actual']!r}"
            for c in checks if c["status"] != "pass"]
