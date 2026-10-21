"""Unit tests for the FinOps agent: parsers, cost engine, tools and the fallback path (no network, no LLM)."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from transform_agents.core import AgentRequest, ResultBox
from transform_agents.finops import agent as finops_agent
from transform_agents.finops import cost_model as cm
from transform_agents.finops import pricing
from transform_agents.finops.parsers import parse_cpu_m, parse_k8s, parse_mem_mi, parse_terraform, primary_workload
from transform_agents.finops.tools import FinOpsSession, build_tools, unverified_numbers

FIXTURES = Path(__file__).parent / "fixtures" / "finops_inputs"
AS_OF = date(2026, 10, 19)
ALERT = ("⚠️ Kubernetes 1.33 is in EKS extended support since 2026-07-29: +$365/month. Upgrade to 1.36 (standard "
         "support) in terraform/variables.tf → kubernetes_version.")


def fixture_request(**options) -> AgentRequest:
    files = {str(p.relative_to(FIXTURES)): p.read_text() for p in FIXTURES.rglob("*")
             if p.is_file() and str(p.relative_to(FIXTURES)).startswith(("k8s/", "terraform/"))}
    inputs = {k: json.loads((FIXTURES / f"{k}.json").read_text())
              for k in ("assessment", "spec", "modernization", "terraform", "k8s_validation")}
    return AgentRequest(run_id="test", inputs=inputs, files=files, options={"as_of": AS_OF.isoformat(), **options})


@pytest.fixture(scope="module")
def prices() -> dict:
    return pricing.load_snapshot()


@pytest.fixture
def ctx(prices) -> cm.Context:
    return cm.Context(prices=prices, as_of=AS_OF, app=cm.Pod("app", 200, 544), declared_app=cm.Pod("app", 100, 128),
                      cron=cm.Pod("cronjob", 50, 64), replicas_min=2, replicas_max=6, replicas_growth=6)


def assert_consistent(scenario: dict) -> None:
    assert scenario["monthly_usd"] == round(sum(i["monthly_usd"] for i in scenario["items"]), 2)


# ------------------------------------------------------------------ parsers


def test_quantities():
    assert [parse_cpu_m(v) for v in ("250m", "0.5", 2, "1.5")] == [250, 500, 2000, 1500]
    assert [parse_mem_mi(v) for v in ("128Mi", "1Gi", "256M", "134217728", "2048Ki")] == [128, 1024, 244, 128, 2]


def test_parse_k8s_fixture_ignores_local_overlay():
    k = parse_k8s(fixture_request().files)
    assert all(p.startswith("k8s/base/") for p in k["files_parsed"])
    main = primary_workload(k)
    assert main["requests"] == {"cpu": "100m", "memory": "128Mi"}
    assert main["limits"] == {"cpu": "500m", "memory": "256Mi"}
    assert (main["replicas_min"], main["replicas_max"], main["hpa_target_pct"]) == (2, 6, 70)
    cron = k["cronjobs"][0]
    assert cron["requests"] == {"cpu": "50m", "memory": "64Mi"} and cron["schedule"] == "0 2 * * *"
    assert k["pdbs"][0]["minAvailable"] == 1 and k["ingresses"] == 0


def test_parse_k8s_sums_containers_and_reports_bad_yaml():
    doc = """apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 3
  template:
    spec:
      containers:
        - {name: a, resources: {requests: {cpu: 100m, memory: 64Mi}}}
        - {name: b, resources: {requests: {cpu: "0.25", memory: 1Gi}}}
"""
    k = parse_k8s({"k8s/app.yaml": doc, "k8s/broken.yaml": "a: [b"})
    assert k["deployments"][0]["requests"] == {"cpu": "350m", "memory": "1088Mi"}
    assert primary_workload(k)["replicas_min"] == 3
    assert any("broken.yaml" in w for w in k["warnings"])


def test_parse_terraform_fixture():
    tf = parse_terraform(fixture_request().files)
    assert tf["eks_present"] and tf["kubernetes_version"] == "1.33"
    ng = tf["node_groups"][0]
    assert (ng["instance_types"], ng["min"], ng["max"], ng["desired"]) == (["t4g.medium"], 2, 4, 2)
    assert (ng["capacity_type"], ng["disk_gib"], ng["volume_type"]) == ("ON_DEMAND", 20, "gp3")
    assert tf["database"] == {"engine": "postgres", "instance_class": "db.t4g.micro", "multi_az": True,
                              "storage_gib": 20, "storage_type": "gp3", "managed_master_secret": True}
    assert (tf["nat_gateways"], tf["azs"], tf["secrets"], tf["ecr_repositories"]) == (1, 2, 1, 1)
    assert tf["warnings"] == []


def test_parse_terraform_tfvars_override_and_nat_per_az():
    files = {**fixture_request().files, "terraform/terraform.tfvars":
             'single_nat_gateway = false\nkubernetes_version = "1.36"\naz_count = 3\n'}
    tf = parse_terraform(files)
    assert (tf["nat_gateways"], tf["azs"], tf["kubernetes_version"]) == (3, 3, "1.36")


def test_parse_terraform_plain_resources_and_defaults():
    hcl = """
resource "aws_eks_cluster" "c" { version = "1.35" }
resource "aws_eks_node_group" "ng" {
  instance_types = ["m7g.large"]
  capacity_type  = "SPOT"
  scaling_config {
    min_size     = 3
    max_size     = 5
    desired_size = 3
  }
}
resource "aws_nat_gateway" "nat" { count = 2 }
resource "aws_db_instance" "db" {
  instance_class    = "db.t4g.small"
  allocated_storage = 50
}
resource "aws_lb" "api" { load_balancer_type = "application" }
resource "aws_cloudwatch_log_group" "app" { retention_in_days = 14 }
"""
    tf = parse_terraform({"terraform/main.tf": hcl, "terraform/broken.tf": "resource {"})
    ng = tf["node_groups"][0]
    assert tf["kubernetes_version"] == "1.35"
    assert (ng["instance_types"], ng["min"], ng["desired"]) == (["m7g.large"], 3, 3)
    assert ng["capacity_type"] == "SPOT"
    assert tf["database"]["multi_az"] is False and tf["database"]["storage_type"] == "gp2"  # AWS defaults
    assert (tf["nat_gateways"], tf["load_balancers"], tf["log_retention_days"]) == (2, 1, 14)
    assert any("broken.tf" in w for w in tf["warnings"])


# ------------------------------------------------------------------ right-sizing and node fit


def test_rightsize_arithmetic():
    rs = cm.rightsize(vcpu=2, memory_gib=8, cpu_p95_pct=12, mem_pct=18, peak_rps=40, replicas_min=2, replicas_max=6,
                      hpa_target_pct=70, growth_factor=3)
    # 2*1000*12% = 240m; 240/2/0.7 = 171.4 -> 200m; x2.5 = 500m
    assert rs["requests"] == {"cpu": "200m", "memory": "544Mi"}  # 8192*18% = 1474.6 - 1024 = 450.6 * 1.2 = 540.7 -> 544
    assert rs["limits"] == {"cpu": "500m", "memory": "832Mi"}  # 544 * 1.5 = 816 -> 832
    assert (rs["replicas_at_peak"], rs["replicas_at_growth"], rs["replicas_max"]) == (2, 6, 6)  # 720m / 140m = 5.1
    assert rs["replicas_max_ok"] and rs["rps_per_pod_at_peak"] == 20.0
    assert rs["evidence"] == {"vm_cpu_p95_m": 240, "vm_mem_used_mi": 1475, "app_mem_mi": 451,
                              "millicores_per_rps": 6.0, "growth_factor": 3}


VM = {"vcpu": 2, "memory_gib": 8, "cpu_p95_pct": 12, "mem_pct": 18, "peak_rps": 40, "replicas_min": 2,
      "replicas_max": 6, "hpa_target_pct": 70, "growth_factor": 3}


@pytest.mark.parametrize(("declared", "measured", "requests", "limits", "basis"), [
    ({"requests": 128, "limits": 256}, None, "128Mi", "256Mi", "manifest"),  # the modernization manifests
    ({"requests": 128, "limits": 256}, 59, "128Mi", "256Mi", "manifest+measured"),  # 59 x 1.2 = 71 -> 96 -> floor 128
    ({"requests": 128, "limits": 256}, 300, "384Mi", "576Mi", "manifest+measured"),  # 300 x 1.2 = 360 -> 384; x1.5
    ({}, 59, "128Mi", "192Mi", "measured"),  # 128 x 1.5 = 192
    ({"requests": 2048, "limits": 4096}, None, "544Mi", "832Mi", "manifest"),  # above the VM upper bound: capped
    ({}, None, "544Mi", "832Mi", "vm-estimate"),
])
def test_rightsize_memory_prefers_container_data(declared, measured, requests, limits, basis):
    rs = cm.rightsize(**VM, declared_mem_mi=declared, measured_mem_mi=measured)
    assert (rs["requests"]["memory"], rs["limits"]["memory"], rs["memory_basis"]) == (requests, limits, basis)
    assert rs["requests"]["cpu"] == "200m" and rs["limits"]["cpu"] == "500m"  # CPU still from the VM's p95
    assert rs["vm_estimate"] == {"requests": {"memory": "544Mi"}, "limits": {"memory": "832Mi"}}
    assert bool(rs["memory_gap_note"]) is (basis != "vm-estimate" and requests != "544Mi")


def test_measured_container_memory_is_read_from_the_inputs():
    req = fixture_request()
    req.inputs["k8s_validation"]["live_deploy"]["resource_usage"] = {"memory_mi": 59.2}
    s = FinOpsSession.from_request(req)
    assert s.measured_memory_mi() == 59.2
    rs = s.do_rightsize()
    assert rs["memory_basis"] == "manifest+measured" and rs["requests"]["memory"] == "128Mi"
    assert "container data (measured 59.2Mi)" in rs["memory_gap_note"]
    assert FinOpsSession.from_request(fixture_request()).measured_memory_mi() is None


def test_eks_extended_support_is_fo01_even_when_not_applied():
    s = FinOpsSession.from_request(fixture_request())
    box = ResultBox()
    s.do_compare(["log_hygiene"])
    s.finalize(box)
    first = box.data["recommendations"][0]
    assert (first["id"], first["optimization"], first["applies_to"], first["scope"]) == (
        "FO-01", "eks_standard_support", "terraform", "proposed")
    assert first["monthly_savings_usd"] == 365.0 and first["rationale"].startswith(ALERT)
    assert box.data["recommendations"][1]["optimization"] == "log_hygiene"
    standard = FinOpsSession.from_request(fixture_request())
    standard.request.files["terraform/variables.tf"] = standard.request.files["terraform/variables.tf"].replace(
        '"1.33"', '"1.36"')
    standard.ws = type(standard.ws)(standard.request.files)
    standard.finalize(ResultBox())
    assert standard.comparison["eks_support"]["as_designed"]["extended"] is False


def test_rightsize_floors_and_growth_beyond_hpa_max():
    rs = cm.rightsize(vcpu=2, memory_gib=4, cpu_p95_pct=1, mem_pct=10, peak_rps=10, replicas_min=2, replicas_max=3,
                      hpa_target_pct=80, growth_factor=10)
    assert rs["requests"] == {"cpu": "50m", "memory": "128Mi"} and rs["limits"] == {"cpu": "150m", "memory": "192Mi"}
    # 20m*10 = 200m / (50m*0.8) = 5 replicas > max 3
    assert (rs["replicas_at_growth"], rs["replicas_max"], rs["replicas_max_ok"]) == (5, 5, False)


def test_kubelet_reservations_and_allocatable(prices):
    assert [cm.kube_reserved_cpu_m(n) for n in (1, 2, 4, 8)] == [60, 70, 80, 90]
    # t4g.medium: 2000 - 70 - 200 DaemonSets; int(4096*0.95)=3891 - (255+11*17) - 100 - 224; 17 - 4 pods
    assert cm.allocatable(prices["ec2"]["t4g.medium"]) == {"cpu_m": 1730, "memory_mi": 3125, "pods": 13}


def test_pack_first_fit_decreasing():
    alloc = {"cpu_m": 1000, "memory_mi": 1000, "pods": 3}
    assert cm.pack([cm.Pod("a", 400, 100)] * 5, alloc) == 3  # 2 + 2 + 1 (CPU bound)
    assert cm.pack([cm.Pod("a", 100, 100)] * 7, alloc) == 3  # pod-slot bound
    assert cm.pack([cm.Pod("big", 1200, 100)], alloc) is None


def test_node_fit_picks_cheapest_with_ha_and_n_plus_1(ctx):
    rows = cm.node_fit(ctx, disk_gib=20, volume_type="gp3")
    best = rows[0]
    assert (best["instance_type"], best["nodes"]) == ("t4g.medium", 2)
    assert best["monthly_usd"] == 52.26  # 2 x 0.0336 x 730 = 49.056 + 2 x 20 GiB x 0.08 = 3.20
    small = next(r for r in rows if r["instance_type"] == "t4g.small")
    assert (small["nodes"], small["monthly_usd"]) == (4, 55.46)  # 4 x 0.0168 x 730 = 49.056 + 6.40
    assert all(r["arch"] == "arm64" for r in rows if r["fits"])  # prefer_graviton


# ------------------------------------------------------------------ cost scenarios


def test_estimate_current_exact(prices):
    items = cm.estimate_current(cm.CurrentEstate(), prices)
    assert [i["monthly_usd"] for i in items] == [60.74, 3.00, 3.65, 4.50]  # 0.0832*730, 30*0.10, 0.005*730, 50*0.09
    assert cm.total(items) == 71.89


def test_estimate_target_as_designed_exact(prices):
    items = {i["item"]: i["monthly_usd"] for i in cm.estimate_target(cm.TargetDesign(), prices, AS_OF)}
    assert items["EKS control plane"] == 73.00
    assert items["EKS extended support surcharge (Kubernetes 1.33)"] == 365.00  # 0.50 USD/h x 730
    assert items["Worker nodes 2 x t4g.medium (on-demand)"] == 49.06
    assert items["RDS PostgreSQL db.t4g.micro Multi-AZ (on-demand)"] == 23.36
    assert items["RDS storage 20 GiB gp2 (Multi-AZ)"] == 4.60
    assert items["NAT gateway x 1"] == 32.85 and items["Application Load Balancer x 1"] == 16.43
    assert items["Public IPv4 addresses x 3 (ALB per AZ + NAT)"] == 10.95
    assert items["CloudWatch Logs storage (never expire, after 12 months)"] == 3.60  # 120 GB x 0.03
    assert round(sum(items.values()), 2) == 598.79


def test_eks_support_status(prices):
    assert cm.eks_support("1.33", prices, AS_OF)["status"] == "EXTENDED_SUPPORT"
    assert cm.eks_support("1.33", prices, date(2026, 7, 1))["extended"] is False
    assert cm.eks_support("1.36", prices, AS_OF)["status"] == "STANDARD_SUPPORT"
    assert cm.eks_support("1.20", prices, AS_OF) == {"version": "1.20", "status": "UNSUPPORTED", "extended": True}
    assert cm.eks_support("1.40", prices, AS_OF)["extended"] is False


def test_compare_is_consistent_and_within_budget(ctx):
    opts = [o.id for o in cm.CATALOG] + ["spot_nodes", "bogus"]
    c = cm.compare(cm.CurrentEstate(), cm.TargetDesign(), ctx, opts, 450)
    for key in ("current", "target_as_designed", "target_optimized"):
        assert_consistent(c[key])
    savings = {a["id"]: a["monthly_savings_usd"] for a in c["applied"]}
    # gp3 on RDS: same price as gp2 in us-west-2 (more baseline IOPS); nodes are already gp3
    assert savings == {"eks_standard_support": 365.00, "rightsize_and_bin_pack": 0.0, "gp3_volumes": 0.0,
                       "log_hygiene": 5.42, "compute_savings_plan": 13.73, "rds_reserved_instance": 6.35}
    des, opt = c["target_as_designed"]["monthly_usd"], c["target_optimized"]["monthly_usd"]
    assert round(des - sum(savings.values()), 2) == opt == 208.29
    assert c["target_optimized"]["design"]["kubernetes_version"] == "1.36"
    assert (c["within_budget"], c["within_budget_as_designed"], c["budget_headroom_usd"]) == (True, False, 241.71)
    assert c["deltas"] == {"as_designed_vs_current_usd": 526.90, "as_designed_vs_current_ratio": 8.3,
                           "optimized_vs_current_usd": 136.40,
                           "optimized_vs_as_designed_usd": -390.50, "optimized_vs_current_ratio": 2.9}
    assert [r["id"] for r in c["rejected"]] == ["spot_nodes"] and c["unknown"] == ["bogus"] and c["available"] == []
    partial = cm.compare(cm.CurrentEstate(), cm.TargetDesign(), ctx, ["compute_savings_plan"], 450)
    assert [(a["id"], a["would_save_usd"]) for a in partial["available"]] == [
        ("eks_standard_support", 365.0), ("log_hygiene", 5.42), ("rds_reserved_instance", 6.35)]
    assert [n["monthly_savings_usd"] for n in c["nonprod"]] == [29.08, 34.27]  # 2x(0.0336-0.01368)x730, 2x0.0336x510


def test_fixture_optimized_total_with_container_sized_pods():
    """Pods at 200m/128Mi (manifest memory) fit 2 x t4g.small: every number below is exact."""
    s = FinOpsSession.from_request(fixture_request())
    c = s.do_compare([o.id for o in cm.CATALOG])
    savings = {a["id"]: a["monthly_savings_usd"] for a in c["applied"]}
    # 2 x 0.0168 x 730 = 24.528 -> 24.53 vs 49.06 on t4g.medium; Savings Plan 2 x (0.0168 - 0.0121) x 730 = 6.86
    assert savings == {"eks_standard_support": 365.00, "rightsize_and_bin_pack": 24.53, "log_hygiene": 5.42,
                       "compute_savings_plan": 6.86, "rds_reserved_instance": 6.35}
    assert c["target_optimized"]["design"]["node_type"] == "t4g.small"
    assert round(598.79 - sum(savings.values()), 2) == c["target_optimized"]["monthly_usd"] == 190.63
    assert c["deltas"]["optimized_vs_current_usd"] == 118.74 and c["budget_headroom_usd"] == 259.37
    assert [n["monthly_savings_usd"] for n in c["nonprod"]] == [16.53, 17.14]
    for key in ("current", "target_as_designed", "target_optimized"):
        assert_consistent(c[key])


def test_graviton_and_single_nat(ctx):
    design = cm.TargetDesign(kubernetes_version="1.36", node_type="t3.medium", nat_gateways=2)
    c = cm.compare(cm.CurrentEstate(), design, ctx, ["graviton_nodes", "single_nat_gateway"], 450)
    savings = {a["id"]: a["monthly_savings_usd"] for a in c["applied"]}
    assert savings == {"graviton_nodes": 11.68, "single_nat_gateway": 36.50}  # 2x0.008x730; 32.85 + 3.65 (its IPv4)
    assert c["target_optimized"]["design"]["node_type"] == "t4g.medium"


def test_required_fixes_make_the_design_meet_the_spec(ctx):
    design = cm.TargetDesign(kubernetes_version="1.36", node_count=1, db_multi_az=False, capacity_type="SPOT")
    c = cm.compare(cm.CurrentEstate(), design, ctx, [], 450)
    fixes = {f["id"]: f["monthly_delta_usd"] for f in c["required_fixes"]}
    assert set(fixes) == {"multi_az_database", "on_demand_in_prod", "node_capacity"}
    assert fixes["multi_az_database"] == 13.98  # (0.032-0.016)x730 + 20 GiB x (0.23-0.115)
    opt = c["target_optimized"]["design"]
    assert (opt["db_multi_az"], opt["capacity_type"], opt["node_count"]) == (True, "ON_DEMAND", 2)
    designed = c["target_as_designed"]["monthly_usd"]
    assert round(designed + sum(fixes.values()), 2) == c["target_optimized"]["monthly_usd"]


def test_nonprod_allows_spot_and_offhours(prices):
    ctx = cm.Context(prices=prices, as_of=AS_OF, environment="dev")
    c = cm.compare(cm.CurrentEstate(), cm.TargetDesign(kubernetes_version="1.36"), ctx,
                   ["spot_nodes", "offhours_scale_down"], 450)
    assert [a["id"] for a in c["applied"]] == ["spot_nodes", "offhours_scale_down"] and c["rejected"] == []
    nodes = next(i for i in c["target_optimized"]["items"] if i["category"] == "compute")
    assert nodes["monthly_usd"] == 6.02  # 2 x 0.01368 x 220 h


# ------------------------------------------------------------------ pricing


def test_snapshot_has_every_price_the_model_needs(prices):
    assert pricing.snapshot_source(prices) == "static-2026-10" and prices["hours_per_month"] == 730
    for t in pricing.EC2_TYPES:
        ec2 = set(prices["ec2"][t])
        assert {"on_demand_hourly", "compute_sp_1yr_hourly", "spot_hourly_avg_7d", "max_pods", "arch"} <= ec2
    for c in pricing.RDS_CLASSES:
        rds = set(prices["rds_postgres"][c])
        assert {"single_az_hourly", "multi_az_hourly", "ri_1yr_no_upfront_multi_az_hourly"} <= rds
    assert prices["eks"]["default_version"] in prices["eks"]["versions"]


def test_live_pricing_falls_back_to_snapshot(monkeypatch):
    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(pricing, "fetch_live_prices", boom)
    prices, source, warning = pricing.get_price_book(live=True)
    assert source == "static-2026-10" and "no credentials" in warning and prices["ec2"]


def test_missing_price_offline_is_reported(monkeypatch, prices):
    monkeypatch.setattr(pricing, "_session", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    book = json.loads(json.dumps(prices))
    assert "live lookup failed" in pricing.add_missing_prices(book, ["x9g.large"], [])[0]
    assert pricing.add_missing_prices(book, ["t4g.medium"], ["db.t4g.micro"]) == []


# ------------------------------------------------------------------ tools, session and handle()


def test_minimal_request_uses_defaults():
    s = FinOpsSession.from_request(AgentRequest(options={"as_of": AS_OF.isoformat()}))
    box = ResultBox()
    s.finalize(box, fallback=True)
    d = box.data
    assert d["current"]["monthly_usd"] == 71.89 and d["budget_usd"] == 450 and d["within_budget"] is True
    assert d["target_as_designed"]["design"]["node_type"] == "t4g.medium" and d["fallback"] is True
    assert "reports/finops-report.md" in s.ws.artifacts()


def test_tools_chain_builds_contract_data_and_report():
    s = FinOpsSession.from_request(fixture_request())
    box = ResultBox()
    tools = {t.tool_name: t for t in build_tools(s, box)}
    assert "pricing_source=static-2026-10" in tools["get_prices"]()
    assert '"EXTENDED_SUPPORT"' in tools["parse_terraform_sizing"]()
    sized = json.loads(tools["rightsize_pods"]())
    assert sized["requests"] == {"cpu": "200m", "memory": "128Mi"}  # memory from the manifests, not the VM
    assert sized["limits"] == {"cpu": "500m", "memory": "256Mi"} and sized["memory_basis"] == "manifest"
    assert sized["vm_estimate"] == {"requests": {"memory": "544Mi"}, "limits": {"memory": "832Mi"}}
    assert json.loads(tools["node_fit"](candidates=["t4g.medium", "nope.large"]))[1]["fits"] is False
    head = tools["compare_scenarios"](optimizations=["eks_standard_support", "compute_savings_plan"])
    assert head.startswith("current 71.89 | as designed 598.79 (8.3x today) | optimized 220.06")  # -365.00 -13.73
    assert "costs 148.17 USD/month MORE than today" in head and "save 378.73 USD/month" in head
    assert "Kubernetes 1.33 is in EKS extended support: a 365.00 USD/month surcharge" in head
    # 2 x t4g.medium -> 2 x t4g.small on the Savings Plan rate: 2 x (0.0242 - 0.0121) x 730 = 17.666 -> 17.66
    assert ("Not applied yet, would still save: rightsize_and_bin_pack (17.66), log_hygiene (5.42), "
            "rds_reserved_instance (6.35)") in head
    assert len(s.history) == 1
    assert json.loads(tools["estimate_cost"](scenario="current"))["monthly_usd"] == 71.89
    recs = json.dumps([{"id": "eks_standard_support", "rationale": "1.33 is in extended support",
                        "monthly_savings_usd": 999, "risk": "medium"},
                       {"id": "custom", "title": "Install Karpenter", "rationale": "shrink the baseline"},
                       {"id": "gp3_volumes", "rationale": "not applied"}])
    msg = tools["submit_result"](summary="ok", value_case="Costs more; buys HA.", recommendations_json=recs,
                                 risks="- r")
    assert "ignored recommendations that were not applied: gp3_volumes" in msg
    d = box.data
    required = {"currency", "pricing_source", "current", "target_as_designed", "target_optimized", "budget_usd",
                "within_budget", "rightsizing", "recommendations"}
    assert required <= set(d) and d["pricing_source"] == "static-2026-10"
    assert set(d["rightsizing"]) >= {"pod_requests", "pod_limits", "replicas_min", "replicas_max",
                                     "node_instance_types", "node_min", "node_max", "db_instance_class"}
    first = d["recommendations"][0]
    assert (first["id"], first["monthly_savings_usd"], first["risk"]) == ("FO-01", 365.0, "medium")  # 999 ignored
    assert first["optimization"] == "eks_standard_support" and first["applies_to"] == "terraform"
    assert first["rationale"] == ALERT == d["eks_support_alert"]
    last = d["recommendations"][-1]
    assert last["title"] == "Install Karpenter" and last["monthly_savings_usd"] == 0 and last["quantified"] is False
    report = s.ws.read("reports/finops-report.md")
    for heading in ("| Scenario |", "## What you pay for", "## Recommendations", "## Right-sizing", "### Node fit"):
        assert heading in report
    assert f"> {ALERT}" in report and f"| FO-01 | {first['title']} | 365.00 |" in report
    assert "| Pod requests | 100m CPU, 128Mi | - | 544Mi memory | 200m CPU, 128Mi |" in report
    assert "The legacy-VM estimate (544Mi request / 832Mi limit) starts from the whole VM's memory" in report


def test_submit_rejects_llm_arithmetic_once():
    s = FinOpsSession.from_request(fixture_request())
    box = ResultBox()
    tools = {t.tool_name: t for t in build_tools(s, box)}
    tools["compare_scenarios"](optimizations=[o.id for o in cm.CATALOG])
    first = tools["submit_result"](summary="Costs 190.63, i.e. 24.75 per node-day.", value_case="v",
                                   recommendations_json="[]")
    assert first.startswith("error: these numbers do not come from any tool: 24.75") and box.data is None
    tools["submit_result"](summary="Costs 190.63, i.e. 24.75 per node-day.", value_case="v", recommendations_json="[]")
    assert box.data["unverified_numbers"] == ["24.75"]
    assert "> Note: the narrative quotes numbers the cost model did not produce: 24.75" in s.ws.read(
        "reports/finops-report.md")


def test_unverified_numbers_flags_llm_arithmetic():
    data = {"a": 71.89, "b": [{"c": "saves 13.73 USD"}]}
    text = "from 71.89 to 13.73, a 99.9% SLO, about 58.16 cheaper"
    assert unverified_numbers(text, [data, {"slo": 99.9}]) == ["58.16"]


async def _fake_run_agent(events):
    for ev in events:
        yield ev


async def test_handle_falls_back_when_the_llm_fails(monkeypatch):
    monkeypatch.setattr(finops_agent, "run_agent", lambda **kw: _fake_run_agent([
        {"type": "status", "stage": "finops", "message": "agent started"},
        {"type": "error", "stage": "finops", "message": "ThrottlingException: slow down"}]))
    events = [ev async for ev in finops_agent.handle(fixture_request())]
    final = [e for e in events if e["type"] in ("result", "error")]
    assert len(final) == 1 and final[0]["type"] == "result"
    r = final[0]
    assert r["metrics"]["fallback"] is True and "ThrottlingException" in r["metrics"]["fallback_reason"]
    assert r["data"]["target_optimized"]["monthly_usd"] == 190.63  # every prod-safe optimization
    assert "reports/finops-report.md" in r["artifacts"]


async def test_handle_passes_through_a_submitted_result(monkeypatch):
    def fake(**kw):
        submit = next(t for t in kw["tools"] if t.tool_name == "submit_result")
        submit(summary="done", value_case="v", recommendations_json="[]")
        return _fake_run_agent([{"type": "result", "stage": "finops", "summary": kw["box"].summary,
                                 "data": kw["box"].data, "artifacts": kw["workspace"].artifacts(),
                                 "metrics": {"submitted": True}}])

    monkeypatch.setattr(finops_agent, "run_agent", fake)
    events = [ev async for ev in finops_agent.handle(fixture_request())]
    assert [e["type"] for e in events] == ["result"] and events[0]["summary"] == "done"


async def test_handle_enforces_the_time_budget(monkeypatch):
    async def slow(**kw):
        yield {"type": "status", "stage": "finops", "message": "agent started"}
        import asyncio
        await asyncio.sleep(5)

    monkeypatch.setattr(finops_agent, "run_agent", slow)
    events = [ev async for ev in finops_agent.handle(fixture_request(time_budget_s=0.3))]
    assert events[-1]["type"] == "result" and "time budget" in events[-1]["metrics"]["fallback_reason"]


def test_design_override_via_replace_keeps_dataclass_frozen():
    d = cm.TargetDesign()
    with pytest.raises(AttributeError):
        d.node_count = 3  # type: ignore[misc]
    assert replace(d, node_count=3).node_count == 3


def test_eks_support_status_follows_the_date_not_the_snapshot():
    from datetime import date

    from transform_agents.finops.cost_model import eks_support
    from transform_agents.finops.pricing import load_snapshot

    prices = load_snapshot()
    later = eks_support("1.34", prices, date(2026, 12, 10))  # snapshot says STANDARD_SUPPORT; ended 2026-12-02
    assert later["status"] == "EXTENDED_SUPPORT" and later["extended"] is True
    gone = eks_support("1.31", prices, date(2026, 12, 1))  # extended support ended 2026-11-26
    assert gone["status"] == "END_OF_SUPPORT"
    today = eks_support("1.33", prices, date(2026, 10, 19))
    assert today["status"] == "EXTENDED_SUPPORT" and today["extended"] is True
