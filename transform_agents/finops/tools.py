"""FinOps tools: thin Strands wrappers around the deterministic engine, bound to one request.

`FinOpsSession` gathers the inputs (assessment, spec, k8s/terraform files) with sensible defaults, so the
agent works with an empty request too. Tools can be called in any order: each one computes what it needs.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from strands import tool

from transform_agents.core import AgentRequest, ResultBox, Workspace

from . import cost_model as cm
from . import pricing
from .parsers import parse_cpu_m, parse_k8s, parse_mem_mi, parse_terraform, primary_workload
from .report import render

REPORT_PATH = "reports/finops-report.md"
MAX_COMPARES = 3
DEFAULT_OPTIMIZATIONS = [o.id for o in cm.CATALOG]  # deterministic fallback: every prod-safe optimization
LEVELS = ("low", "medium", "high")


def _get(d: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(d, dict) or d.get(key) is None:
            return default
        d = d[key]
    return d


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


@dataclass
class FinOpsSession:
    request: AgentRequest
    ws: Workspace
    as_of: date = field(default_factory=date.today)
    prices: dict | None = None
    pricing_source: str = ""
    k8s: dict | None = None
    tf: dict | None = None
    rightsizing: dict | None = None
    fit: list | None = None
    comparison: dict | None = None
    target: cm.TargetDesign | None = None
    optimizations: list[str] = field(default_factory=list)
    compares: int = 0
    rejected_submits: int = 0
    warnings: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # every compare result, for the number audit
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)  # tools run in parallel threads

    @classmethod
    def from_request(cls, request: AgentRequest) -> "FinOpsSession":
        as_of = request.options.get("as_of")
        return cls(request=request, ws=Workspace(request.files),
                   as_of=date.fromisoformat(as_of) if as_of else date.today())

    # ------------------------------------------------------------------ inputs
    @property
    def assessment(self) -> dict:
        return self.request.inputs.get("assessment") or {}

    @property
    def spec(self) -> dict:
        return self.request.inputs.get("spec") or {}

    @property
    def service(self) -> str:
        return (_get(self.spec, "service", "name") or _get(self.request.inputs, "modernization", "service_name")
                or "inventory-service")

    @property
    def budget(self) -> float:
        return float(_get(self.spec, "finops", "monthly_budget_usd", default=450))

    @property
    def environment(self) -> str:
        return str(_get(self.spec, "finops", "environment", default="prod"))

    def load_prices(self) -> dict:
        with self.lock:
            return self._load_prices()

    def _load_prices(self) -> dict:
        if self.prices is None:
            live = bool(self.request.options.get("live_pricing"))
            self.prices, self.pricing_source, warning = pricing.get_price_book(live)
            if warning:
                self.warnings.append(warning)
        return self.prices

    def parse_k8s(self) -> dict:
        with self.lock:
            if self.k8s is None:
                self.k8s = parse_k8s(self.ws.files)
                self.warnings += self.k8s["warnings"]
            return self.k8s

    def parse_tf(self) -> dict:
        with self.lock:
            if self.tf is None:
                self.tf = parse_terraform(self.ws.files)
                self.warnings += self.tf["warnings"]
            return self.tf

    def workload(self) -> dict[str, Any]:
        """Replicas, HPA target, declared requests and the CronJob pod, from k8s > modernization > spec > defaults."""
        k8s, w = self.parse_k8s(), _get(self.spec, "workload", default={})
        main = primary_workload(k8s) or {}
        declared = {k: main[k] for k in ("requests", "limits") if k in main}
        if not declared:
            declared = _get(self.request.inputs, "modernization", "resources", default={}) or {}
        cron = next((c for c in k8s["cronjobs"] if "requests" in c), None)
        return {
            "replicas_min": int(main.get("replicas_min") or w.get("replicas_min") or 2),
            "replicas_max": int(main.get("replicas_max") or w.get("replicas_max") or 6),
            "hpa_target_pct": float(main.get("hpa_target_pct") or w.get("autoscaling_target_pct") or 70),
            "peak_rps": float(w.get("peak_rps") or 40),
            "growth_factor": float(_get(self.spec, "finops", "growth_factor_next_year", default=1) or 1),
            "declared": declared,
            "cron": None if cron is None else cm.Pod("cronjob", parse_cpu_m(cron["requests"]["cpu"]),
                                                     parse_mem_mi(cron["requests"]["memory"])),
        }

    def legacy_vm(self) -> dict[str, Any]:
        infra, util = self.assessment.get("infrastructure") or {}, self.assessment.get("utilization") or {}
        itype, prices = infra.get("instance_type") or "t3.large", self.load_prices()
        with self.lock:
            if itype not in prices["ec2"]:
                self.warnings += pricing.add_missing_prices(prices, [itype], [])
            if itype not in prices["ec2"]:
                self.warnings.append(f"no price for the legacy {itype}; priced as t3.large")
                prices["ec2"][itype] = prices["ec2"]["t3.large"]  # cached so the lookup is not retried
        spec = prices["ec2"][itype]
        return {
            "instance_type": itype,
            "vcpu": float(infra.get("vcpu") or spec["vcpu"]),
            "memory_gib": float(infra.get("memory_gib") or spec["memory_mib"] / 1024),
            "volume_type": _get(infra, "volume", "type", default="gp2"),
            "volume_gib": int(_get(infra, "volume", "size_gib", default=30)),
            "cpu_p95_pct": float(util.get("cpu_p95_pct") or 12.0),
            "mem_pct": float(util.get("mem_p95_pct") or util.get("mem_avg_pct") or 18.0),
            "utilization_source": util.get("source") or "default",
        }

    def measured_memory_mi(self) -> float | None:
        """Container working set an earlier stage measured (k8s_validation live deploy or modernization), if any."""
        for path in (("k8s_validation", "live_deploy", "resource_usage"), ("k8s_validation", "resource_usage"),
                     ("modernization", "measured"), ("modernization", "resources", "measured")):
            usage = _get(self.request.inputs, *path)
            if not isinstance(usage, dict):
                continue
            try:
                if usage.get("memory_mi") is not None:
                    return round(float(usage["memory_mi"]), 1)
                if usage.get("memory") is not None:
                    return float(parse_mem_mi(usage["memory"]))
            except (TypeError, ValueError):
                continue
        return None

    # ------------------------------------------------------------------ engine steps
    def do_rightsize(self) -> dict:
        if self.rightsizing is None:
            vm, w = self.legacy_vm(), self.workload()
            declared = {}
            for kind in ("requests", "limits"):
                try:
                    declared[kind] = parse_mem_mi(w["declared"][kind]["memory"])
                except (KeyError, TypeError, ValueError):
                    pass
            self.rightsizing = cm.rightsize(vcpu=vm["vcpu"], memory_gib=vm["memory_gib"],
                                            cpu_p95_pct=vm["cpu_p95_pct"], mem_pct=vm["mem_pct"],
                                            peak_rps=w["peak_rps"],
                                            replicas_min=w["replicas_min"], replicas_max=w["replicas_max"],
                                            hpa_target_pct=w["hpa_target_pct"], growth_factor=w["growth_factor"],
                                            declared_mem_mi=declared, measured_mem_mi=self.measured_memory_mi())
        return self.rightsizing

    def context(self) -> cm.Context:
        rs, w, prices = self.do_rightsize(), self.workload(), self.load_prices()
        req = rs["requests"]
        declared = w["declared"].get("requests")
        return cm.Context(
            prices=prices, as_of=self.as_of, environment=self.environment,
            slo_pct=float(_get(self.spec, "nfr", "availability_slo_pct", default=99.9)),
            spec_multi_az=bool(_get(self.spec, "data", "multi_az", default=True)),
            prefer_graviton=bool(_get(self.spec, "finops", "prefer_graviton", default=True)),
            app=cm.Pod("app", parse_cpu_m(req["cpu"]), parse_mem_mi(req["memory"])),
            declared_app=cm.Pod("app", parse_cpu_m(declared["cpu"]), parse_mem_mi(declared["memory"]))
            if declared else None,
            cron=w["cron"], replicas_min=w["replicas_min"], replicas_max=w["replicas_max"],
            replicas_growth=rs["replicas_at_growth"],
        )

    def design(self) -> cm.TargetDesign:
        """The target as designed: terraform files > inputs.terraform > spec > defaults."""
        with self.lock:
            if self.target is None:
                self.target = self._design()
            return self.target

    def _design(self) -> cm.TargetDesign:
        tf, td, prices = self.parse_tf(), self.request.inputs.get("terraform") or {}, self.load_prices()
        ng = tf["node_groups"][0] if tf["node_groups"] else {}
        db, tdb = tf["database"] or {}, td.get("database") or {}
        compute = td.get("compute") or {}
        has_tf = bool(tf["files_parsed"])
        node_type = (ng.get("instance_types") or compute.get("node_instance_types") or ["t4g.medium"])[0]
        db_class = db.get("instance_class") or tdb.get("instance_class") or "db.t4g.micro"
        self.warnings += pricing.add_missing_prices(prices, [node_type], [db_class])
        if node_type not in prices["ec2"]:
            self.warnings.append(f"no price for {node_type}; priced as t4g.medium")
            node_type = "t4g.medium"
        if db_class not in prices["rds_postgres"]:
            self.warnings.append(f"no price for {db_class}; priced as db.t4g.micro")
            db_class = "db.t4g.micro"
        node_min = int(ng.get("desired") or compute.get("node_min") or 2)
        return cm.TargetDesign(
            kubernetes_version=str(tf["kubernetes_version"] or _get(self.spec, "target", "kubernetes_version")
                                   or "1.33"),
            node_type=node_type,
            node_count=node_min,
            node_max=int(ng.get("max") or compute.get("node_max") or max(node_min, 4)),
            capacity_type=str(ng.get("capacity_type") or compute.get("capacity_type") or "ON_DEMAND").upper(),
            node_disk_gib=int(ng.get("disk_gib") or 20),
            node_volume_type=ng.get("volume_type") or "gp3",
            db_class=db_class,
            db_multi_az=bool(db["multi_az"] if db else tdb.get("multi_az", _get(self.spec, "data", "multi_az",
                                                                                default=True))),
            db_storage_gib=int(db.get("storage_gib") or tdb.get("storage_gib") or 20),
            db_storage_type=db.get("storage_type") or "gp3",
            nat_gateways=tf["nat_gateways"] if has_tf else int(_get(td, "network", "nat_gateways", default=1)),
            load_balancers=max(1, tf["load_balancers"]),  # the API needs an internet-facing ALB even without Ingress
            azs=int(tf["azs"] or _get(td, "network", "azs", default=2)),
            log_retention_days=tf["log_retention_days"],
            secrets=tf["secrets"] or 1,
            ecr_repositories=tf["ecr_repositories"] or 1,
        )

    def do_node_fit(self, candidates: list[str] | None = None) -> list[dict]:
        d = self.design()
        self.fit = cm.node_fit(self.context(), d.node_disk_gib, d.node_volume_type, candidates or None)
        return self.fit

    def do_compare(self, optimizations: list[str]) -> dict:
        self.optimizations = list(optimizations)
        legacy = self.legacy_vm()
        current = cm.CurrentEstate(instance_type=legacy["instance_type"], volume_type=legacy["volume_type"],
                                   volume_gib=legacy["volume_gib"])
        self.comparison = cm.compare(current, self.design(), self.context(), optimizations, self.budget)
        self.history.append(self.comparison)
        return self.comparison

    # ------------------------------------------------------------------ result
    def recommendations(self, llm_json: str) -> tuple[list[dict], list[str]]:
        """Applied optimizations (+ dev/test advice) with tool-computed savings; the LLM only adds rationale."""
        notes: list[str] = []
        try:
            llm = json.loads(llm_json) if llm_json and llm_json.strip() else []
            llm = llm.get("recommendations", []) if isinstance(llm, dict) else llm
            llm = [r for r in llm if isinstance(r, dict)]
        except json.JSONDecodeError:
            llm, notes = [], ["recommendations_json was not valid JSON; used the catalog text"]
        by_id = {str(r.get("id") or r.get("optimization")): r for r in llm}
        recs = []
        alert = eks_alert(self.comparison, self.prices)
        for a in self.comparison["applied"]:
            mine = by_id.get(a["id"], {})
            recs.append({"optimization": a["id"], "title": a["title"], "monthly_savings_usd": a["monthly_savings_usd"],
                         "effort": mine.get("effort") if mine.get("effort") in LEVELS else a["effort"],
                         "risk": mine.get("risk") if mine.get("risk") in LEVELS else a["risk"],
                         "applies_to": a["applies_to"], "scope": "prod", "change": a["change"],
                         "rationale": alert if a["id"] == "eks_standard_support" and alert
                         else str(mine.get("rationale") or a["change"])})
        if alert and not any(r["optimization"] == "eks_standard_support" for r in recs):
            # not applied (e.g. reviewer feedback): still the first thing the audience must see, but not in the totals
            avail = next((a for a in self.comparison["available"] if a["id"] == "eks_standard_support"), {})
            opt = cm.CATALOG_BY_ID["eks_standard_support"]
            recs.append({"optimization": opt.id, "title": opt.title,
                         "monthly_savings_usd": avail.get("would_save_usd", 0.0), "effort": opt.effort,
                         "risk": opt.risk, "applies_to": "terraform", "scope": "proposed",
                         "change": avail.get("change", ""), "rationale": alert + " (not applied to the optimized "
                                                                                 "scenario)"})
        recs.sort(key=lambda r: r["optimization"] != "eks_standard_support")  # FO-01 when extended support
        for n in self.comparison["nonprod"]:
            mine = by_id.get(n["id"], {})
            recs.append({"optimization": n["id"], "title": f"{n['title']} (dev/test only)",
                         "monthly_savings_usd": n["monthly_savings_usd"], "effort": "low", "risk": "low",
                         "applies_to": "process", "scope": "nonprod", "change": n["calc"],
                         "rationale": str(mine.get("rationale") or "Dev/test tolerates interruptions and downtime.")})
        for r in [r for r in llm if r.get("id") == "custom" and r.get("title")][:3]:
            recs.append({"optimization": "custom", "title": str(r["title"]), "monthly_savings_usd": 0.0,
                         "effort": r.get("effort") if r.get("effort") in LEVELS else "medium",
                         "risk": r.get("risk") if r.get("risk") in LEVELS else "low",
                         "applies_to": r.get("applies_to") if r.get("applies_to") in ("k8s", "terraform", "process")
                         else "process", "scope": "prod", "quantified": False,
                         "rationale": str(r.get("rationale", ""))})
        ignored = [i for i in by_id if i not in {r["optimization"] for r in recs} and i != "custom"]
        if ignored:
            notes.append(f"ignored recommendations that were not applied: {', '.join(ignored)}")
        for i, r in enumerate(recs, 1):
            r["id"] = f"FO-{i:02d}"
        return [{"id": r.pop("id"), **r} for r in recs], notes

    def data(self, recommendations: list[dict]) -> dict[str, Any]:
        c, rs, w = self.comparison, self.do_rightsize(), self.workload()
        opt = c["target_optimized"]["design"]
        legacy, design = self.legacy_vm(), c["target_as_designed"]["design"]
        support = c["eks_support"]["as_designed"]
        return {
            "currency": "USD",
            "pricing_source": self.pricing_source,
            "pricing_date": self.prices["fetched_at"],
            "analysis_date": self.as_of.isoformat(),
            "current": c["current"],
            "target_as_designed": c["target_as_designed"],
            "target_optimized": c["target_optimized"],
            "budget_usd": c["budget_usd"],
            "within_budget": c["within_budget"],
            "within_budget_as_designed": c["within_budget_as_designed"],
            "budget_headroom_usd": c["budget_headroom_usd"],
            "deltas": c["deltas"],
            "rightsizing": {
                "pod_requests": rs["requests"], "pod_limits": rs["limits"],
                "replicas_min": rs["replicas_min"], "replicas_max": rs["replicas_max"],
                "node_instance_types": [opt["node_type"]], "node_min": opt["node_count"],
                "node_max": max(opt["node_max"], c["growth"]["nodes"]), "db_instance_class": opt["db_class"],
                "kubernetes_version": opt["kubernetes_version"],
            },
            "declared_resources": {**w["declared"], "replicas": f"{w['replicas_min']}-{w['replicas_max']}"},
            "rightsizing_formula": rs["formula"],
            "memory_sizing": {"basis": rs["memory_basis"], "measured_mi": rs["measured_memory_mi"],
                              "vm_estimate": rs["vm_estimate"], "gap_note": rs["memory_gap_note"]},
            "eks_support_alert": eks_alert(c, self.prices),
            "recommendations": recommendations,
            "applied_optimizations": [a["id"] for a in c["applied"]],
            "required_fixes": c["required_fixes"],
            "skipped": c["skipped"],
            "rejected": c["rejected"],
            "nonprod": c["nonprod"],
            "growth": c["growth"],
            "eks_support": c["eks_support"],
            "node_fit": [{k: r.get(k) for k in ("instance_type", "vcpu", "memory_gib", "nodes", "monthly_usd", "fits",
                                                "allocatable")} for r in (self.fit or self.do_node_fit())[:6]],
            "assumptions": {
                "current_instance": legacy["instance_type"],
                "designed_nodes": f"{design['node_count']} x {design['node_type']}",
                "growth_factor": w["growth_factor"],
                "notes": [
                    (f"Month = {cm.HOURS} h. us-west-2 prices from `{self.pricing_source}` (fetched "
                    f"{self.prices['fetched_at']}); Linux on-demand unless stated."),
                    (f"Legacy utilization ({legacy['utilization_source']}): CPU p95 {legacy['cpu_p95_pct']:g}%, "
                    f"memory {legacy['mem_pct']:g}% of {legacy['vcpu']:g} vCPU / {legacy['memory_gib']:g} GiB."),
                    (f"Nominal usage (no input provides it): {cm.EGRESS_GB} GB/month internet egress (same before and "
                    f"after), {cm.NAT_GB} GB through NAT, {cm.ALB_LCU:g} ALB LCU, {cm.LOG_GB} GB/month CloudWatch "
                    f"Logs, {cm.ECR_GB} GB ECR."),
                    ("An internet-facing ALB is assumed for the API even when the manifests have no Ingress yet; it "
                    "uses one public IPv4 address per AZ. The legacy VM has one public IPv4 address."),
                    ("Node fit: EKS AL2023 kubelet reservations, 4 DaemonSets (VPC CNI, kube-proxy, pod identity, "
                    "fluent-bit) and system pods (CoreDNS x2, metrics-server, AWS Load Balancer Controller x2); "
                    "at least 2 nodes and N+1 at baseline."),
                    ("Nodes are sized for HPA max replicas all month: the design has no node autoscaler (Karpenter or "
                    "Cluster Autoscaler would let the baseline shrink)."),
                    (f"Memory right-sizing uses container data first ({rs['memory_basis']}); the legacy-VM figure "
                     f"(memory in use minus a {cm.VM_BASELINE_MIB} MiB OS/agents baseline) is only an upper bound. "
                     "Confirm with `kubectl top pods` or VPA recommendations after the first week."),
                    ("Savings Plan and RI rates are 1-year, no upfront (Compute Savings Plan rate from the Savings "
                    "Plans API)."),
                    f"Kubernetes {design['kubernetes_version']}: {support['status']} on {self.as_of.isoformat()}"
                    + (f" (standard support ended {support['end_of_standard_support']}; extended support adds "
                       f"{self.prices['eks']['extended_support_hourly']:g} USD/h)" if support.get("extended") else "")
                    + ".",
                    ("Not modelled: RDS backups within the free allocation, the S3 reports bucket (cents), t4g CPU "
                    "credit surplus (utilization is low), the 100 GB/month free egress tier."),
                ],
            },
            "warnings": list(dict.fromkeys(self.warnings)),
        }

    def known_sources(self) -> list[Any]:
        """Everything a tool returned in this session, plus the inputs: the only legitimate numbers."""
        return [self.request.inputs, self.history, self.fit, self.prices, self.rightsizing]

    def finalize(self, box: ResultBox, summary: str = "", value_case: str = "", risks: str = "",
                 recommendations_json: str = "", fallback: bool = False) -> str:
        """Build data + report from the tool results and record them in the ResultBox."""
        if self.comparison is None:
            self.do_compare(self.optimizations or DEFAULT_OPTIMIZATIONS)
        recs, notes = self.recommendations(recommendations_json)
        data = self.data(recs)
        summary = summary.strip() or default_summary(data)
        narrative = {"summary": summary, "value_case": value_case.strip() or default_value_case(data),
                     "risks": risks.strip()}
        text = " ".join([*narrative.values(), *(r["rationale"] for r in recs)])
        data["unverified_numbers"] = unverified_numbers(text, [data, *self.known_sources()])
        if fallback:
            data["fallback"] = True
        self.ws.write(REPORT_PATH, render(data, narrative, self.service, self.as_of.isoformat()))
        box.set(summary, data)
        return f"wrote {REPORT_PATH}; {'; '.join(notes) or 'recommendations merged'}"


def _dollars(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def eks_alert(c: dict, prices: dict | None) -> str:
    """The one line the audience must not miss when the design runs a Kubernetes version in extended support."""
    support = c["eks_support"]["as_designed"]
    if not support.get("extended") or not prices:
        return ""
    surcharge = next((i["monthly_usd"] for i in c["target_as_designed"]["items"] if "extended support" in i["item"]),
                     0.0)
    target = prices["eks"].get("default_version") or "a newer version"
    since = f" since {support['end_of_standard_support']}" if support.get("end_of_standard_support") else ""
    return (f"⚠️ Kubernetes {support['version']} is in EKS extended support{since}: +{_dollars(surcharge)}/month. "
            f"Upgrade to {target} (standard support) in terraform/variables.tf → kubernetes_version.")


def compare_headline(c: dict) -> str:
    """Pre-phrased facts so the LLM can quote numbers without doing arithmetic or mixing up their direction."""
    cur, des, opt = (c[k]["monthly_usd"] for k in ("current", "target_as_designed", "target_optimized"))
    dl = c["deltas"]
    lines = [(f"current {cur:.2f} | as designed {des:.2f} ({dl['as_designed_vs_current_ratio']}x today) | optimized "
             f"{opt:.2f} USD/month"),
             (f"- The optimized target costs {dl['optimized_vs_current_usd']:.2f} USD/month MORE than today "
             f"({dl['optimized_vs_current_ratio']}x); that difference is the price of HA, not a saving."),
             f"- The applied optimizations save {-dl['optimized_vs_as_designed_usd']:.2f} USD/month vs as designed.",
             (f"- Budget {c['budget_usd']:g}: optimized {'WITHIN' if c['within_budget'] else 'OVER'} (headroom "
             f"{c['budget_headroom_usd']:.2f}); as designed {'within' if c['within_budget_as_designed'] else 'OVER'}.")]
    if c["available"]:
        lines.append("- Not applied yet, would still save: "
                     + ", ".join(f"{a['id']} ({a['would_save_usd']:.2f})" for a in c["available"])
                     + ". Apply them unless reviewer feedback or the SLO rules them out.")
    support = c["eks_support"]["as_designed"]
    if support.get("extended"):
        surcharge = next((i["monthly_usd"] for i in c["target_as_designed"]["items"]
                          if "extended support" in i["item"]), 0.0)
        lines.append(f"- As designed, Kubernetes {support['version']} is in EKS extended support: a {surcharge:.2f} "
                     "USD/month surcharge (a cost, not a benefit).")
    return "\n".join(lines)


def default_summary(data: dict) -> str:
    verdict = "within" if data["within_budget"] else "over"
    return (f"Current EC2 estate: USD {data['current']['monthly_usd']:,.2f}/month. Target as designed: USD "
            f"{data['target_as_designed']['monthly_usd']:,.2f}/month; optimized: USD "
            f"{data['target_optimized']['monthly_usd']:,.2f}/month, {verdict} the USD {data['budget_usd']:,.0f} "
            f"budget.")


def default_value_case(data: dict) -> str:
    return (f"The target costs USD {data['deltas']['optimized_vs_current_usd']:,.2f}/month more than the single EC2 "
            "instance because it removes the single point of failure: two nodes in two AZs, a Multi-AZ managed "
            "PostgreSQL with backups, a load balancer with health checks and autoscaling. That is what the 99.9% SLO, "
            "RPO 15 min and the security fixes require.")


_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*\.\d{1,2}(?!\d)")


def unverified_numbers(text: str, sources: list[Any]) -> list[str]:
    """Decimal numbers in the LLM's narrative that no tool or input produced (guards against LLM arithmetic)."""
    known: set[float] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            known.update({round(float(v), 2), round(abs(float(v)), 2)})
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            for m in _NUM_RE.findall(v):
                known.add(round(float(m.replace(",", "")), 2))

    walk(sources)
    found = {m for m in _NUM_RE.findall(text)}
    return sorted(m for m in found if round(float(m.replace(",", "")), 2) not in known)


# ====================================================================== Strands tools


def build_tools(s: FinOpsSession, box: ResultBox) -> list:
    """The agent's tools, bound to one session."""

    @tool
    def get_prices() -> str:
        """Load the us-west-2 price book (AWS Pricing API snapshot, or live when options.live_pricing is true)."""
        p = s.load_prices()
        ec2 = {t: v["on_demand_hourly"] for t, v in p["ec2"].items() if t in ("t3.large", "t4g.small", "t4g.medium",
                                                                           "t4g.large", "m7g.large", "c7g.large")}
        key = {"ec2_on_demand_hourly": ec2, "ebs_gb_month": p["ebs_gb_month"],
               "rds_db.t4g.micro_hourly": {k: v for k, v in p["rds_postgres"]["db.t4g.micro"].items()
                                           if "hourly" in k},
               "eks_cluster_hourly": p["eks"]["cluster_hourly"], "eks_extended_support_hourly":
               p["eks"]["extended_support_hourly"], "nat_gateway": p["nat_gateway"], "alb": p["alb"],
               "public_ipv4_hourly": p["public_ipv4_hourly"], "cloudwatch_logs": p["cloudwatch_logs"]}
        return f"pricing_source={s.pricing_source} fetched_at={p['fetched_at']} {_json(key)}"

    @tool
    def parse_k8s_resources() -> str:
        """Parse k8s/** manifests: Deployment requests/limits and replicas, HPA min/max/target, CronJobs, ingress."""
        k = s.parse_k8s()
        main = primary_workload(k)
        return _json({"primary_workload": main, "cronjobs": k["cronjobs"], "ingresses": k["ingresses"],
                      "pdbs": k["pdbs"], "files": len(k["files_parsed"]), "warnings": k["warnings"]})

    @tool
    def parse_terraform_sizing() -> str:
        """Parse terraform/**: node groups, Kubernetes version and its EKS support status, RDS, NAT, logs."""
        tf, d = s.parse_tf(), s.design()
        p = s.load_prices()
        support = cm.eks_support(d.kubernetes_version, p, s.as_of)
        head = f"Kubernetes {d.kubernetes_version}: {support['status']}"
        if support["extended"]:
            head += (f" (standard support ended {support.get('end_of_standard_support', '?')}): the cluster pays an "
                     f"extra {p['eks']['extended_support_hourly']:g} USD/h until it runs a standard-support version "
                     f"such as {p['eks']['default_version']} (optimization eks_standard_support).")
        return head + "\n" + _json({"design_used_for_pricing": d.__dict__, "eks_support": support, "terraform": {
            k: tf[k] for k in ("node_groups", "database", "nat_gateways", "azs", "load_balancers", "eks_present")},
            "files": len(tf["files_parsed"]), "warnings": tf["warnings"]})

    @tool
    def rightsize_pods() -> str:
        """Derive pod CPU/memory requests and limits and replica counts: memory from container data (measured or
        declared in the manifests) with the legacy VM's utilization as an upper bound; CPU from the VM's p95."""
        rs = s.do_rightsize()
        return _json({**rs, "declared_in_manifests": s.workload()["declared"]})

    @tool
    def node_fit(candidates: list[str] | None = None) -> str:
        """Bin-pack the right-sized pods (HPA max replicas + system pods, N+1) onto node types; cheapest first.

        Args:
            candidates: Optional EC2 instance types to compare (default: all Graviton types in the price book).
        """
        rows = s.do_node_fit(candidates)
        return _json([{k: r.get(k) for k in ("instance_type", "nodes", "monthly_usd", "fits", "reason")}
                      for r in rows[:8]])

    @tool
    def estimate_cost(scenario: str, optimizations: list[str] | None = None) -> str:
        """Monthly cost line items of one scenario.

        Args:
            scenario: "current", "as_designed" or "optimized".
            optimizations: Optimization ids for the "optimized" scenario (default: the last compare_scenarios call).
        """
        if optimizations or s.comparison is None:
            s.do_compare(optimizations or s.optimizations or DEFAULT_OPTIMIZATIONS)
        c = s.comparison
        key = {"current": "current", "as_designed": "target_as_designed"}.get(scenario, "target_optimized")
        return _json({"scenario": key, "monthly_usd": c[key]["monthly_usd"],
                      "items": [{"item": i["item"], "monthly_usd": i["monthly_usd"]} for i in c[key]["items"]]})

    @tool
    def compare_scenarios(optimizations: list[str]) -> str:
        """Compute current vs target as designed vs target optimized, per-optimization savings and the budget verdict.

        Args:
            optimizations: Optimization ids to apply to the target (see the catalog in the task).
        """
        if s.compares >= MAX_COMPARES:
            return "limit reached: call submit_result now"
        s.compares += 1
        c = s.do_compare(optimizations)
        keys = ("required_fixes", "applied", "skipped", "rejected", "unknown", "available", "deltas",
                "budget_headroom_usd", "growth", "nonprod")
        return compare_headline(c) + "\n" + _json({k: c[k] for k in keys})

    @tool
    def submit_result(summary: str, value_case: str, recommendations_json: str, risks: str = "") -> str:
        """Write reports/finops-report.md and submit the final result. Call exactly once, at the very end.

        Args:
            summary: 2-4 sentences: the verdict, the three monthly totals and the budget (copy numbers exactly).
            value_case: Markdown, 1-2 short paragraphs: why the target costs more than one EC2 instance and what it
                buys.
            recommendations_json: JSON list of {"id": "<optimization id>", "rationale": "..."} for each applied
                optimization, plus up to 2 {"id": "custom", "title", "rationale", "effort", "risk", "applies_to"}.
            risks: Markdown bullet list of risks and follow-ups.
        """
        if s.rejected_submits == 0:  # enforce "no LLM arithmetic" once; afterwards accept and flag in the report
            bad = unverified_numbers(f"{summary} {value_case} {risks} {recommendations_json}", s.known_sources())
            if bad:
                s.rejected_submits += 1
                return (f"error: these numbers do not come from any tool: {', '.join(bad)}. Do not compute numbers; "
                        "quote tool outputs exactly or drop the number, then call submit_result again.")
        return s.finalize(box, summary, value_case, risks, recommendations_json)

    return [get_prices, parse_k8s_resources, parse_terraform_sizing, rightsize_pods, node_fit, estimate_cost,
            compare_scenarios, submit_result]
