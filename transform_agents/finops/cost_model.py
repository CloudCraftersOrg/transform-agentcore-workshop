"""Deterministic cost engine: pod right-sizing, node bin-packing and monthly cost scenarios.

Every number in the FinOps report comes from this module. The LLM only chooses which optimizations
to apply and explains the trade-offs. Prices come from `pricing.py`; a month is 730 hours.
"""

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

HOURS = 730

# --- Right-sizing assumptions (printed in the report) -------------------------------------------
VM_BASELINE_MIB = 1024  # OS + nginx + SSM/CloudWatch agents on the legacy VM: not part of the app process
MEM_HEADROOM = 1.2  # +20% over the app's observed working set
CPU_LIMIT_FACTOR = 2.5  # CPU burst room above the request (CPU is throttled, never OOM-killed)
MEM_LIMIT_FACTOR = 1.5  # memory limit as a multiple of the request
CPU_STEP_M, MEM_STEP_MI = 50, 32
MIN_CPU_M, MIN_MEM_MI = 50, 128

# --- Node model: EKS AL2023 kubelet reservations plus nominal add-on requests --------------------
HA_MIN_NODES = 2  # one node per AZ, so prod survives the loss of a node or an AZ
KERNEL_MEM_SHARE = 0.95  # share of the nominal instance memory the kubelet sees
EVICTION_MI = 100
DAEMONSETS = {"aws-node": (25, 64), "kube-proxy": (100, 64), "eks-pod-identity-agent": (25, 32), "fluent-bit": (50, 64)}
SYSTEM_PODS = [("coredns", 2, 100, 70), ("metrics-server", 1, 100, 200), ("aws-load-balancer-controller", 2, 50, 128)]

# --- Nominal usage that no input provides (printed as assumptions) -------------------------------
EGRESS_GB = 50  # internet data transfer out per month, the same before and after
NAT_GB = 20  # patches and image layers pulled through the NAT gateway
ALB_LCU = 1.0  # average load balancer capacity units (40 rps peak)
LOG_GB = 10  # CloudWatch Logs ingestion per month (JSON app logs + EKS control-plane logs)
LOG_HORIZON_MONTHS = 12  # log groups that never expire: storage after one year
ECR_GB = 1
NONPROD_HOURS = 220  # dev/test nodes running 10 h x 22 working days
LOG_HYGIENE_FACTOR = 0.6  # drop probe access logs and the EKS "api" log type
LOG_HYGIENE_RETENTION_DAYS = 30

GRAVITON = {"t3": "t4g", "t3a": "t4g", "m5": "m7g", "m6i": "m7g", "m6a": "m7g", "c5": "c7g", "c6i": "c7g",
            "r5": "r7g", "r6i": "r7g"}


def ceil_to(value: float, step: int) -> int:
    return int(math.ceil(round(value, 6) / step) * step)


def usd(value: float) -> float:
    """Round to cents, half up."""
    return math.floor(value * 100 + 0.5 + 1e-9) / 100


def cents(items: list[dict]) -> int:
    return sum(round(i["monthly_usd"] * 100) for i in items)


def total(items: list[dict]) -> float:
    return cents(items) / 100


def line(name: str, category: str, qty: float, unit_price: float, unit: str, hours: int | None = None) -> dict:
    """One cost line with its formula, e.g. `2 x 0.0336 USD/h x 730 h`."""
    calc = f"{qty:g} x {unit_price:g} USD/{unit}" + (f" x {hours} h" if hours else "")
    return {"item": name, "category": category, "monthly_usd": usd(qty * unit_price * (hours or 1)), "calc": calc}


# =============================================================================== right-sizing


@dataclass(frozen=True)
class Pod:
    name: str
    cpu_m: int
    mem_mi: int


def rightsize(*, vcpu: float, memory_gib: float, cpu_p95_pct: float, mem_pct: float, peak_rps: float,
              replicas_min: int, replicas_max: int, hpa_target_pct: float, growth_factor: float,
              declared_mem_mi: dict[str, int] | None = None, measured_mem_mi: float | None = None) -> dict[str, Any]:
    """Pod requests/limits.

    CPU: the VM's p95 CPU is the peak hour; replicas_min pods must carry it below the HPA target.
    Memory: container data first. A measured working set (+ headroom) or the requests/limits declared in the
    manifests drive the recommendation; the legacy-VM estimate (memory in use minus the OS/agents baseline, plus
    headroom) is only an upper bound, because it still counts page cache and everything else on the VM.
    """
    vm_cpu_m = vcpu * 1000 * cpu_p95_pct / 100
    cpu_req = max(MIN_CPU_M, ceil_to(vm_cpu_m / replicas_min / (hpa_target_pct / 100), CPU_STEP_M))
    cpu_lim = ceil_to(cpu_req * CPU_LIMIT_FACTOR, CPU_STEP_M)
    vm_mem_mi = memory_gib * 1024 * mem_pct / 100
    app_mem_mi = max(0.0, vm_mem_mi - VM_BASELINE_MIB)
    vm_req = max(MIN_MEM_MI, ceil_to(app_mem_mi * MEM_HEADROOM, MEM_STEP_MI))
    vm_lim = ceil_to(vm_req * MEM_LIMIT_FACTOR, MEM_STEP_MI)
    memory = container_memory(vm_req, vm_lim, declared_mem_mi or {}, measured_mem_mi)
    mem_req, mem_lim = memory["request_mi"], memory["limit_mi"]
    pod_capacity_m = cpu_req * hpa_target_pct / 100  # CPU one pod absorbs before the HPA scales out
    replicas_peak = max(replicas_min, math.ceil(round(vm_cpu_m / pod_capacity_m, 6)))
    replicas_growth = max(replicas_min, math.ceil(round(vm_cpu_m * growth_factor / pod_capacity_m, 6)))
    return {
        "requests": {"cpu": f"{cpu_req}m", "memory": f"{mem_req}Mi"},
        "limits": {"cpu": f"{cpu_lim}m", "memory": f"{mem_lim}Mi"},
        "replicas_min": replicas_min,
        "replicas_max": max(replicas_max, replicas_growth),
        "replicas_at_peak": replicas_peak,
        "replicas_at_growth": replicas_growth,
        "replicas_max_ok": replicas_growth <= replicas_max,
        "rps_per_pod_at_peak": round(peak_rps / replicas_peak, 1),
        "evidence": {"vm_cpu_p95_m": round(vm_cpu_m), "vm_mem_used_mi": round(vm_mem_mi),
                     "app_mem_mi": round(app_mem_mi), "millicores_per_rps": round(vm_cpu_m / peak_rps, 2),
                     "growth_factor": growth_factor},
        "memory_basis": memory["basis"],
        "measured_memory_mi": measured_mem_mi,
        "vm_estimate": {"requests": {"memory": f"{vm_req}Mi"}, "limits": {"memory": f"{vm_lim}Mi"}},
        "memory_gap_note": memory["gap_note"],
        "formula": [
            f"vm_cpu_p95 = {vcpu:g} vCPU x 1000 x {cpu_p95_pct:g}% = {vm_cpu_m:g}m (whole VM at {peak_rps:g} rps peak)",
            f"cpu_request = ceil50({vm_cpu_m:g}m / {replicas_min} replicas / {hpa_target_pct:g}% HPA) = {cpu_req}m",
            f"cpu_limit = ceil50({cpu_req}m x {CPU_LIMIT_FACTOR}) = {cpu_lim}m",
            *memory["formula"],
            (f"memory upper bound (legacy VM): {memory_gib:g} GiB x {mem_pct:g}% - {VM_BASELINE_MIB}Mi OS/agents = "
             f"{app_mem_mi:.0f}Mi; ceil32(x {MEM_HEADROOM}) = {vm_req}Mi request, ceil32(x {MEM_LIMIT_FACTOR}) = "
             f"{vm_lim}Mi limit"),
            (f"replicas: peak {replicas_peak}, with {growth_factor:g}x growth {replicas_growth} "
            f"(each pod absorbs {pod_capacity_m:g}m before scaling)"),
        ],
    }


def container_memory(vm_req: int, vm_lim: int, declared: dict[str, int], measured: float | None) -> dict[str, Any]:
    """Memory request/limit from container data (measured working set > manifest), capped by the VM upper bound."""
    need = max(MIN_MEM_MI, ceil_to(measured * MEM_HEADROOM, MEM_STEP_MI)) if measured else None
    req_decl, lim_decl = declared.get("requests"), declared.get("limits")
    formula = [f"memory measured in the container: {measured:g}Mi working set; ceil32(x {MEM_HEADROOM}), floor "
               f"{MIN_MEM_MI}Mi = {need}Mi"] if need else []
    if req_decl:
        req, basis = max(req_decl, need or 0), "manifest+measured" if need else "manifest"
    elif need:
        req, basis = need, "measured"
    else:
        req, basis = vm_req, "vm-estimate"
    if req > vm_req and not (need and need >= req):  # declared above the VM bound with no measurement behind it
        formula.append(f"declared {req}Mi exceeds the legacy-VM upper bound: capped at {vm_req}Mi")
        req = vm_req
    floor = ceil_to(req * MEM_LIMIT_FACTOR, MEM_STEP_MI)
    lim = min(lim_decl, max(vm_lim, floor)) if lim_decl and lim_decl >= req else floor
    source = {"manifest": "k8s manifests", "manifest+measured": "k8s manifests, checked against the measurement",
              "measured": "measured + headroom", "vm-estimate": "no container data: legacy-VM estimate"}[basis]
    formula.append(f"memory_request = {req}Mi, memory_limit = {lim}Mi ({source})")
    gap = ""
    if basis != "vm-estimate" and vm_req > req:
        seen = f"measured {measured:g}Mi" if need else f"declared {req_decl}Mi"
        gap = (f"The legacy-VM estimate ({vm_req}Mi request / {vm_lim}Mi limit) starts from the whole VM's memory in "
               f"use and only subtracts a nominal {VM_BASELINE_MIB} MiB for the OS, nginx and agents, so page cache "
               f"and everything else on the host still count. The container data ({seen}) is what the pod really "
               f"needs, so the recommendation keeps {req}Mi / {lim}Mi and uses the VM figure only as an upper bound.")
    return {"request_mi": req, "limit_mi": lim, "basis": basis, "formula": formula, "gap_note": gap}


# =============================================================================== node fit


def kube_reserved_cpu_m(vcpu: int) -> int:
    """EKS AMI formula: 6% of the first core, 1% of the second, 0.5% of cores 3-4, 0.25% above."""
    shares = [0.06 if c == 1 else 0.01 if c == 2 else 0.005 if c <= 4 else 0.0025 for c in range(1, vcpu + 1)]
    return round(1000 * sum(shares))


def allocatable(spec: dict) -> dict[str, int]:
    """What pods can request on one node after kubelet reservations and DaemonSets."""
    ds_cpu = sum(c for c, _ in DAEMONSETS.values())
    ds_mem = sum(m for _, m in DAEMONSETS.values())
    kube_mem = 255 + 11 * spec["max_pods"]
    return {"cpu_m": spec["vcpu"] * 1000 - kube_reserved_cpu_m(spec["vcpu"]) - ds_cpu,
            "memory_mi": int(spec["memory_mib"] * KERNEL_MEM_SHARE) - kube_mem - EVICTION_MI - ds_mem,
            "pods": spec["max_pods"] - len(DAEMONSETS)}


def system_pods() -> list[Pod]:
    return [Pod(name, cpu, mem) for name, count, cpu, mem in SYSTEM_PODS for _ in range(count)]


def pack(pods: list[Pod], alloc: dict[str, int]) -> int | None:
    """First-fit decreasing bin packing. Returns the node count, or None if a pod can't fit an empty node."""
    free: list[list[int]] = []  # per node: [cpu, memory, pod slots]
    weight = lambda p: p.cpu_m / alloc["cpu_m"] + p.mem_mi / alloc["memory_mi"]
    for pod in sorted(pods, key=weight, reverse=True):
        if pod.cpu_m > alloc["cpu_m"] or pod.mem_mi > alloc["memory_mi"] or alloc["pods"] < 1:
            return None
        for node in free:
            if node[0] >= pod.cpu_m and node[1] >= pod.mem_mi and node[2] >= 1:
                node[0], node[1], node[2] = node[0] - pod.cpu_m, node[1] - pod.mem_mi, node[2] - 1
                break
        else:
            free.append([alloc["cpu_m"] - pod.cpu_m, alloc["memory_mi"] - pod.mem_mi, alloc["pods"] - 1])
    return len(free)


@dataclass
class Context:
    """Everything the engine needs besides the design itself."""

    prices: dict
    as_of: date
    environment: str = "prod"
    slo_pct: float = 99.9
    spec_multi_az: bool = True
    prefer_graviton: bool = True
    app: Pod = Pod("app", 200, 128)  # pod requests used for fitting
    declared_app: Pod | None = None  # requests in the k8s manifests (as designed)
    cron: Pod | None = None
    replicas_min: int = 2
    replicas_max: int = 6
    replicas_growth: int = 6
    candidates: list[str] = field(default_factory=list)

    @property
    def prod(self) -> bool:
        return self.environment == "prod"

    @property
    def min_nodes(self) -> int:
        return HA_MIN_NODES if self.prod else 1


def nodes_required(ctx: Context, node_type: str, app: Pod, replicas_max: int | None = None) -> dict[str, Any]:
    """Nodes needed so peak pods fit and, at baseline, the cluster survives losing one node (N+1)."""
    spec = ctx.prices["ec2"][node_type]
    alloc = allocatable(spec)
    peak = [app] * (replicas_max or ctx.replicas_max) + ([ctx.cron] if ctx.cron else []) + system_pods()
    base = [app] * ctx.replicas_min + system_pods()
    n_peak, n_base = pack(peak, alloc), pack(base, alloc)
    if n_peak is None or n_base is None or alloc["cpu_m"] <= 0 or alloc["memory_mi"] <= 0:
        return {"fits": False, "nodes": None, "allocatable": alloc}
    return {"fits": True, "nodes": max(ctx.min_nodes, n_peak, n_base + 1 if ctx.prod else n_base),
            "nodes_for_peak": n_peak, "nodes_for_baseline_n_plus_1": n_base + 1, "allocatable": alloc}


def node_fit(ctx: Context, disk_gib: int = 20, volume_type: str = "gp3",
             candidates: list[str] | None = None) -> list[dict[str, Any]]:
    """Bin-pack the pods onto each candidate type; cheapest monthly cost first (ties: fewer nodes)."""
    types = candidates or ctx.candidates or [t for t, s in ctx.prices["ec2"].items()
                                            if s["arch"] == "arm64" or not ctx.prefer_graviton]
    rows = []
    for t in types:
        spec = ctx.prices["ec2"].get(t)
        if spec is None:
            rows.append({"instance_type": t, "fits": False, "reason": "no price in the price book"})
            continue
        fit = nodes_required(ctx, t, ctx.app)
        row = {"instance_type": t, "arch": spec["arch"], "vcpu": spec["vcpu"], "memory_gib": spec["memory_mib"] / 1024,
               "hourly_usd": spec["on_demand_hourly"], **fit}
        if fit["fits"]:
            n = fit["nodes"]
            row["monthly_usd"] = usd(n * spec["on_demand_hourly"] * HOURS
                                     + n * disk_gib * ctx.prices["ebs_gb_month"][volume_type])
        rows.append(row)
    return sorted(rows, key=lambda r: (not r["fits"], r.get("monthly_usd", 1e9), r.get("nodes") or 0))


# =============================================================================== scenarios


@dataclass(frozen=True)
class CurrentEstate:
    instance_type: str = "t3.large"
    volume_type: str = "gp2"
    volume_gib: int = 30
    public_ips: int = 1


@dataclass(frozen=True)
class TargetDesign:
    kubernetes_version: str = "1.33"
    node_type: str = "t4g.medium"
    node_count: int = 2  # always-on nodes (desired size)
    node_max: int = 4
    capacity_type: str = "ON_DEMAND"
    node_hours: int = HOURS
    node_disk_gib: int = 20
    node_volume_type: str = "gp3"
    db_class: str = "db.t4g.micro"
    db_multi_az: bool = True
    db_storage_gib: int = 20
    db_storage_type: str = "gp2"
    nat_gateways: int = 1
    load_balancers: int = 1
    azs: int = 2
    log_ingest_gb: float = LOG_GB
    log_retention_days: int | None = None  # None = never expire
    secrets: int = 1
    ecr_repositories: int = 1
    compute_savings_plan: bool = False
    rds_reserved: bool = False


def eks_support(version: str, prices: dict, as_of: date) -> dict[str, Any]:
    """Standard vs extended support for a Kubernetes version on `as_of` (extended costs an hourly surcharge)."""
    versions = prices["eks"]["versions"]
    info = versions.get(str(version))
    if info is None:
        key = lambda v: tuple(int(x) for x in v.split("."))
        oldest = min(versions, key=key)
        too_old = key(str(version)) < key(oldest) if str(version).replace(".", "").isdigit() else False
        return {"version": version, "status": "UNSUPPORTED" if too_old else "UNKNOWN", "extended": too_old}
    extended = date.fromisoformat(info["end_of_standard_support"]) <= as_of
    ended = "end_of_extended_support" in info and date.fromisoformat(info["end_of_extended_support"]) <= as_of
    # The status is computed for `as_of`; the snapshot's own "status" field is only its value on the fetch date.
    status = "END_OF_SUPPORT" if ended else ("EXTENDED_SUPPORT" if extended else "STANDARD_SUPPORT")
    return {"version": version, **info, "status": status, "extended": extended}


def estimate_current(c: CurrentEstate, prices: dict) -> list[dict]:
    ec2 = prices["ec2"][c.instance_type]
    return [
        line(f"EC2 {c.instance_type} on-demand (Linux)", "compute", 1, ec2["on_demand_hourly"], "h", HOURS),
        line(f"EBS {c.volume_type} root volume {c.volume_gib} GiB", "storage", c.volume_gib,
             prices["ebs_gb_month"][c.volume_type], "GB-month"),
        line(f"Public IPv4 address x {c.public_ips}", "network", c.public_ips, prices["public_ipv4_hourly"], "h",
             HOURS),
        line(f"Data transfer out (nominal {EGRESS_GB} GB)", "network", EGRESS_GB, prices["data_transfer_out_per_gb"],
             "GB"),
    ]


def estimate_target(d: TargetDesign, prices: dict, as_of: date) -> list[dict]:
    eks, ec2 = prices["eks"], prices["ec2"][d.node_type]
    items = [line("EKS control plane", "control-plane", 1, eks["cluster_hourly"], "h", HOURS)]
    if eks_support(d.kubernetes_version, prices, as_of)["extended"]:
        items.append(line(f"EKS extended support surcharge (Kubernetes {d.kubernetes_version})", "control-plane", 1,
                          eks["extended_support_hourly"], "h", HOURS))
    if d.compute_savings_plan:
        rate, label = ec2["compute_sp_1yr_hourly"], "Compute Savings Plan 1-yr"
    elif d.capacity_type == "SPOT":
        rate, label = ec2["spot_hourly_avg_7d"], "Spot, 7-day average"
    else:
        rate, label = ec2["on_demand_hourly"], "on-demand"
    hours_label = "" if d.node_hours == HOURS else f", {d.node_hours} h/month"
    items.append(line(f"Worker nodes {d.node_count} x {d.node_type} ({label}{hours_label})", "compute", d.node_count,
                      rate, "h", d.node_hours))
    items.append(line(f"Node root volumes {d.node_count} x {d.node_disk_gib} GiB {d.node_volume_type}", "storage",
                      d.node_count * d.node_disk_gib, prices["ebs_gb_month"][d.node_volume_type], "GB-month"))
    db = prices["rds_postgres"][d.db_class]
    az = "multi_az" if d.db_multi_az else "single_az"
    db_rate = db[f"ri_1yr_no_upfront_{az}_hourly"] if d.rds_reserved else db[f"{az}_hourly"]
    az_label = "Multi-AZ" if d.db_multi_az else "Single-AZ"
    items.append(line(f"RDS PostgreSQL {d.db_class} {az_label} ({'RI 1-yr' if d.rds_reserved else 'on-demand'})",
                      "database", 1, db_rate, "h", HOURS))
    storage = prices["rds_storage_gb_month"].get(d.db_storage_type, prices["rds_storage_gb_month"]["gp2"])
    items.append(line(f"RDS storage {d.db_storage_gib} GiB {d.db_storage_type} ({az_label})", "database",
                      d.db_storage_gib, storage[az], "GB-month"))
    if d.nat_gateways:
        nat = prices["nat_gateway"]
        items.append(line(f"NAT gateway x {d.nat_gateways}", "network", d.nat_gateways, nat["hourly"], "h", HOURS))
        items.append(line(f"NAT data processing (nominal {NAT_GB} GB)", "network", NAT_GB, nat["per_gb"], "GB"))
    if d.load_balancers:
        alb = prices["alb"]
        items.append(line(f"Application Load Balancer x {d.load_balancers}", "network", d.load_balancers,
                          alb["hourly"], "h", HOURS))
        items.append(line(f"ALB capacity units (nominal {ALB_LCU:g} LCU)", "network", ALB_LCU * d.load_balancers,
                          alb["lcu_hourly"], "LCU-h", HOURS))
    ips = d.load_balancers * d.azs + d.nat_gateways
    if ips:
        items.append(line(f"Public IPv4 addresses x {ips} (ALB per AZ + NAT)", "network", ips,
                          prices["public_ipv4_hourly"], "h", HOURS))
    logs = prices["cloudwatch_logs"]
    months = LOG_HORIZON_MONTHS if d.log_retention_days is None else min(LOG_HORIZON_MONTHS, d.log_retention_days / 30)
    retention = ("never expire, after 12 months" if d.log_retention_days is None
                 else f"{d.log_retention_days}-day retention")
    items.append(line(f"CloudWatch Logs ingestion ({d.log_ingest_gb:g} GB)", "observability", d.log_ingest_gb,
                      logs["ingest_per_gb"], "GB"))
    items.append(line(f"CloudWatch Logs storage ({retention})", "observability", round(d.log_ingest_gb * months, 2),
                      logs["storage_gb_month"], "GB-month"))
    if d.secrets:
        items.append(line(f"Secrets Manager secret x {d.secrets}", "security", d.secrets,
                          prices["secrets_manager_secret_month"], "secret-month"))
    if d.ecr_repositories:
        items.append(line(f"ECR image storage (nominal {ECR_GB} GB)", "storage", ECR_GB * d.ecr_repositories,
                          prices["ecr_storage_gb_month"], "GB-month"))
    items.append(line(f"Data transfer out (nominal {EGRESS_GB} GB)", "network", EGRESS_GB,
                      prices["data_transfer_out_per_gb"], "GB"))
    return items


# =============================================================================== optimizations


@dataclass(frozen=True)
class Optimization:
    id: str
    title: str
    applies_to: str  # k8s | terraform | process
    effort: str
    risk: str
    summary: str  # one line for the LLM's catalog


CATALOG = [
    Optimization("eks_standard_support", "Create the cluster on a Kubernetes version in EKS standard support",
                 "terraform", "low", "low", "drop the extended-support surcharge (only if the version is in extended)"),
    Optimization("graviton_nodes", "Graviton (arm64) worker nodes", "terraform", "low", "low",
                 "map x86 node types to their Graviton equivalent (t3->t4g, m6i->m7g, c6i->c7g)"),
    Optimization("rightsize_and_bin_pack", "Right-size pods and pick the cheapest node type that fits", "k8s", "low",
                 "low", "use rightsize_pods requests and the cheapest node_fit candidate (keeps 2+ nodes, N+1)"),
    Optimization("gp3_volumes", "gp3 instead of gp2 for node volumes and RDS storage", "terraform", "low", "low",
                 "same or lower price per GB with 3,000 baseline IOPS"),
    Optimization("single_nat_gateway", "One shared NAT gateway", "terraform", "low", "medium",
                 "one NAT instead of one per AZ (egress depends on one AZ; inbound traffic unaffected)"),
    Optimization("log_hygiene", "Log retention and noise reduction", "k8s", "low", "low",
                 "30-day retention, drop probe access logs and the EKS api log type (-40% volume)"),
    Optimization("compute_savings_plan", "1-year Compute Savings Plan (no upfront) for the node baseline", "process",
                 "low", "low", "commit to the always-on node spend; flexible across instance families and regions"),
    Optimization("rds_reserved_instance", "1-year RDS reserved instance (no upfront)", "process", "low", "low",
                 "reserve the database instance class"),
]
CATALOG_BY_ID = {o.id: o for o in CATALOG}

# Asked for in prod but refused: they break the 99.9% SLO or the spec.
REJECTED_IN_PROD = {
    "spot_nodes": "Spot can reclaim nodes with 2 minutes' notice; with 2 nodes that risks the 99.9% SLO. Use Spot "
                  "for dev/test (see nonprod) or as a mixed, PDB-protected node group later.",
    "single_az_database": "the spec requires Multi-AZ RDS (99.9% SLO, RPO 15 min, RTO 1 h).",
    "single_node": "one node is a single point of failure (requirement G2: survive a node or AZ loss).",
    "remove_nat_gateway": "private nodes need egress for patches and AWS APIs without VPC endpoints.",
    "offhours_scale_down": "production must serve the warehouse 24x7; scale down dev/test instead (see nonprod).",
}
NONPROD_ONLY = ["spot_nodes", "offhours_scale_down"]


def _graviton(node_type: str, prices: dict) -> str | None:
    family, size = node_type.split(".", 1)
    target = f"{GRAVITON.get(family, '')}.{size}"
    return target if target in prices["ec2"] else None


def apply_optimization(opt_id: str, d: TargetDesign, ctx: Context) -> tuple[TargetDesign, str]:
    """Return the changed design and what changed (or why nothing changed)."""
    p = ctx.prices
    if opt_id == "eks_standard_support":
        support = eks_support(d.kubernetes_version, p, ctx.as_of)
        target = p["eks"].get("default_version")
        if not support["extended"]:
            return d, f"Kubernetes {d.kubernetes_version} is in {support['status']}; no surcharge"
        info = p["eks"]["versions"].get(target, {})
        return replace(d, kubernetes_version=target), (
            f"Kubernetes {d.kubernetes_version} left standard support on "
            f"{support.get('end_of_standard_support', '?')}; "
            f"use {target} (EKS default, standard support until {info.get('end_of_standard_support', '?')})")
    if opt_id == "graviton_nodes":
        target = _graviton(d.node_type, p) if p["ec2"][d.node_type]["arch"] != "arm64" else None
        if not target:
            return d, f"{d.node_type} is already Graviton or has no Graviton equivalent in the price book"
        return replace(d, node_type=target), f"{d.node_type} -> {target}"
    if opt_id == "rightsize_and_bin_pack":
        best = next((r for r in node_fit(ctx, d.node_disk_gib, d.node_volume_type) if r["fits"]), None)
        if best is None:
            return d, "no candidate node type fits the right-sized pods"
        pods = f"pods {ctx.app.cpu_m}m/{ctx.app.mem_mi}Mi x {ctx.replicas_max} at peak + system pods, N+1"
        if ctx.declared_app and (ctx.declared_app.cpu_m, ctx.declared_app.mem_mi) != (ctx.app.cpu_m, ctx.app.mem_mi):
            pods = (f"requests {ctx.declared_app.cpu_m}m/{ctx.declared_app.mem_mi}Mi -> "
                    f"{ctx.app.cpu_m}m/{ctx.app.mem_mi}Mi; {pods}")
        if (best["instance_type"], best["nodes"]) == (d.node_type, d.node_count):
            return d, f"{d.node_count} x {d.node_type} is already the cheapest fit ({pods})"
        return (replace(d, node_type=best["instance_type"], node_count=best["nodes"],
                        node_max=max(d.node_max, best["nodes"])),
                f"{d.node_count} x {d.node_type} -> {best['nodes']} x {best['instance_type']} ({pods})")
    if opt_id == "gp3_volumes":
        if (d.node_volume_type, d.db_storage_type) == ("gp3", "gp3"):
            return d, "volumes are already gp3"
        return replace(d, node_volume_type="gp3", db_storage_type="gp3"), "node volumes and RDS storage -> gp3"
    if opt_id == "single_nat_gateway":
        if d.nat_gateways <= 1:
            return d, f"design already has {d.nat_gateways} NAT gateway"
        return replace(d, nat_gateways=1), f"{d.nat_gateways} NAT gateways -> 1"
    if opt_id == "log_hygiene":
        if d.log_retention_days is not None and d.log_retention_days <= LOG_HYGIENE_RETENTION_DAYS:
            return d, "log retention is already 30 days or less"
        return (replace(d, log_ingest_gb=round(d.log_ingest_gb * LOG_HYGIENE_FACTOR, 2),
                        log_retention_days=LOG_HYGIENE_RETENTION_DAYS),
                (f"retention {d.log_retention_days or 'never expire'} -> 30 days, ingestion "
                f"{d.log_ingest_gb:g} -> {d.log_ingest_gb * LOG_HYGIENE_FACTOR:g} GB"))
    if opt_id == "compute_savings_plan":
        if d.compute_savings_plan or d.capacity_type == "SPOT":
            return d, "already covered (Savings Plan or Spot)"
        if not p["ec2"][d.node_type].get("compute_sp_1yr_hourly"):
            return d, f"no Compute Savings Plan rate for {d.node_type} in the price book"
        commit = d.node_count * p["ec2"][d.node_type]["compute_sp_1yr_hourly"]
        return replace(d, compute_savings_plan=True), f"commit {commit:.4f} USD/h for 1 year (no upfront)"
    if opt_id == "rds_reserved_instance":
        if d.rds_reserved:
            return d, "already reserved"
        return replace(d, rds_reserved=True), f"reserve 1 x {d.db_class} {'Multi-AZ' if d.db_multi_az else 'Single-AZ'}"
    if opt_id == "spot_nodes":
        return replace(d, capacity_type="SPOT"), "node group capacity_type = SPOT (non-prod)"
    if opt_id == "offhours_scale_down":
        return replace(d, node_hours=NONPROD_HOURS), f"nodes run {NONPROD_HOURS} h/month (non-prod)"
    raise KeyError(opt_id)


def required_fixes(d: TargetDesign, ctx: Context) -> list[tuple[str, TargetDesign, str]]:
    """Spec/SLO gaps in the design. The optimized scenario always fixes them, even when that costs more."""
    fixes = []
    if ctx.spec_multi_az and not d.db_multi_az:
        d = replace(d, db_multi_az=True)
        fixes.append(("multi_az_database", d, "spec requires Multi-AZ RDS; terraform has multi_az = false"))
    if ctx.prod and d.capacity_type == "SPOT":
        d = replace(d, capacity_type="ON_DEMAND")
        fixes.append(("on_demand_in_prod", d, "Spot nodes in prod put the 99.9% SLO at risk"))
    fit = nodes_required(ctx, d.node_type, ctx.declared_app or ctx.app)
    needed = fit["nodes"] if fit["fits"] else None
    if needed and d.node_count < needed:
        why = (f"{d.node_count} x {d.node_type} can't hold {ctx.replicas_max} pods at peak plus system pods with N+1; "
               f"needs {needed}")
        d = replace(d, node_count=needed, node_max=max(d.node_max, needed))
        fixes.append(("node_capacity", d, why))
    return fixes


def nonprod_advice(d: TargetDesign, prices: dict) -> list[dict]:
    """Savings for a dev/test copy of the optimized node pool (not applied to prod)."""
    ec2 = prices["ec2"][d.node_type]
    od, spot, n = ec2["on_demand_hourly"], ec2["spot_hourly_avg_7d"], d.node_count
    return [
        {"id": "spot_nodes", "title": "Spot capacity for dev/test node groups",
         "monthly_savings_usd": usd(n * (od - spot) * HOURS),
         "calc": f"{n} x ({od:g} - {spot:g}) USD/h x {HOURS} h"},
        {"id": "offhours_scale_down", "title": "Scale dev/test nodes to zero outside 08:00-18:00 on weekdays",
         "monthly_savings_usd": usd(n * od * (HOURS - NONPROD_HOURS)),
         "calc": f"{n} x {od:g} USD/h x ({HOURS} - {NONPROD_HOURS}) h"},
    ]


def compare(current: CurrentEstate, design: TargetDesign, ctx: Context, optimizations: list[str],
            budget_usd: float) -> dict[str, Any]:
    """Current vs as designed vs optimized, with the marginal saving of every applied optimization."""
    prices, as_of = ctx.prices, ctx.as_of
    current_items = estimate_current(current, prices)
    designed_items = estimate_target(design, prices, as_of)
    requested = list(dict.fromkeys(o.strip() for o in optimizations if o and o.strip()))

    d, prev = design, cents(designed_items)
    fixes = []
    for fix_id, fixed, why in required_fixes(design, ctx):
        now = cents(estimate_target(fixed, prices, as_of))
        fixes.append({"id": fix_id, "reason": why, "monthly_delta_usd": (now - prev) / 100})
        d, prev = fixed, now

    allowed = [o.id for o in CATALOG] + ([] if ctx.prod else NONPROD_ONLY)
    applied, skipped = [], []
    for opt_id in allowed:
        if opt_id not in requested:
            continue
        changed, note = apply_optimization(opt_id, d, ctx)
        if changed == d and not (opt_id == "rightsize_and_bin_pack" and "->" in note):  # k8s-only change still counts
            skipped.append({"id": opt_id, "reason": note})
            continue
        now = cents(estimate_target(changed, prices, as_of))
        opt = CATALOG_BY_ID.get(opt_id)
        applied.append({"id": opt_id, "title": opt.title if opt else opt_id, "monthly_savings_usd": (prev - now) / 100,
                        "change": note, "effort": opt.effort if opt else "low", "risk": opt.risk if opt else "medium",
                        "applies_to": opt.applies_to if opt else "terraform"})
        d, prev = changed, now
    rejected = [{"id": o, "reason": REJECTED_IN_PROD[o]} for o in requested if o in REJECTED_IN_PROD and ctx.prod]
    unknown = [o for o in requested if o not in allowed and o not in REJECTED_IN_PROD]
    available = []  # money left on the table: catalog optimizations not requested that would still save
    for opt_id in [o for o in allowed if o not in requested]:
        changed, note = apply_optimization(opt_id, d, ctx)
        saving = (prev - cents(estimate_target(changed, prices, as_of))) / 100
        if saving > 0:
            available.append({"id": opt_id, "would_save_usd": saving, "change": note})

    optimized_items = estimate_target(d, prices, as_of)
    cur, des, opt_total = total(current_items), total(designed_items), total(optimized_items)
    growth = nodes_required(ctx, d.node_type, ctx.app, max(ctx.replicas_max, ctx.replicas_growth))
    growth_design = replace(d, node_count=max(d.node_count, growth["nodes"] or d.node_count))
    return {
        "current": {"monthly_usd": cur, "items": current_items},
        "target_as_designed": {"monthly_usd": des, "items": designed_items, "design": asdict(design)},
        "target_optimized": {"monthly_usd": opt_total, "items": optimized_items, "design": asdict(d)},
        "required_fixes": fixes,
        "applied": applied,
        "skipped": skipped,
        "rejected": rejected,
        "unknown": unknown,
        "available": available,
        "deltas": {
            "as_designed_vs_current_usd": usd(des - cur),
            "as_designed_vs_current_ratio": round(des / cur, 1) if cur else None,
            "optimized_vs_current_usd": usd(opt_total - cur),
            "optimized_vs_as_designed_usd": usd(opt_total - des),
            "optimized_vs_current_ratio": round(opt_total / cur, 1) if cur else None,
        },
        "budget_usd": budget_usd,
        "within_budget": opt_total <= budget_usd,
        "within_budget_as_designed": des <= budget_usd,
        "budget_headroom_usd": usd(budget_usd - opt_total),
        "eks_support": {"as_designed": eks_support(design.kubernetes_version, prices, as_of),
                        "optimized": eks_support(d.kubernetes_version, prices, as_of)},
        "growth": {"replicas": max(ctx.replicas_max, ctx.replicas_growth), "nodes": growth_design.node_count,
                   "monthly_usd": total(estimate_target(growth_design, prices, as_of))},
        "nonprod": nonprod_advice(d, prices),
    }
