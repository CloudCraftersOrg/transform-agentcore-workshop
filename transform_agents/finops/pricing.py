"""Price book for us-west-2: a committed snapshot (default) or the live AWS Pricing API.

The snapshot keeps the demo fast and deterministic. `options.live_pricing=true` fetches the same
fields from the read-only Pricing, Savings Plans, EC2 and EKS APIs and falls back to the snapshot on
any failure. Refresh the snapshot with `uv run python transform_agents/finops/knowledge/refresh_prices.py`.
"""

import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).parent / "knowledge" / "prices-us-west-2.json"
REGION, LOCATION, PREFIX = "us-west-2", "US West (Oregon)", "USW2"

EC2_TYPES = [
    "t3.small", "t3.medium", "t3.large", "m6i.large", "c6i.large",  # x86 (legacy + blueprint defaults)
    "t4g.small", "t4g.medium", "t4g.large", "t4g.xlarge",
    "m7g.medium", "m7g.large", "m7g.xlarge", "c7g.medium", "c7g.large", "c7g.xlarge",
]
RDS_CLASSES = ["db.t3.micro", "db.t3.small", "db.t4g.micro", "db.t4g.small", "db.t4g.medium", "db.t4g.large",
               "db.m7g.large"]


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def snapshot_source(prices: dict[str, Any]) -> str:
    """Contract value for `pricing_source`, e.g. "static-2026-10"."""
    return f"static-{prices['fetched_at'][:7]}"


def get_price_book(live: bool = False) -> tuple[dict[str, Any], str, str | None]:
    """Return (prices, pricing_source, warning). Live mode degrades to the snapshot on any error."""
    snapshot = load_snapshot()
    if not live:
        return snapshot, snapshot_source(snapshot), None
    try:
        return fetch_live_prices(), "aws-pricing-api", None
    except Exception as exc:  # noqa: BLE001 - no credentials, throttling, network: degrade to snapshot
        return snapshot, snapshot_source(snapshot), f"live pricing failed ({type(exc).__name__}: {exc}); used snapshot"


def add_missing_prices(prices: dict[str, Any], ec2_types: list[str], rds_classes: list[str]) -> list[str]:
    """Fetch types the design uses but the price book lacks (live, read-only). Returns warnings."""
    ec2 = [t for t in ec2_types if t and t not in prices["ec2"]]
    rds = [c for c in rds_classes if c and c not in prices["rds_postgres"]]
    if not ec2 and not rds:
        return []
    try:
        session = _session()
        client = session.client("pricing", region_name="us-east-1")
        if ec2:
            specs, sp = _instance_specs(session, ec2), _savings_plan_rates(session, ec2)
            for t in ec2:
                prices["ec2"][t] = {**specs[t], "on_demand_hourly": _ec2_prices(client, t),
                                    "compute_sp_1yr_hourly": sp.get(t), "spot_hourly_avg_7d": _spot_avg(session, t)}
        for c in rds:
            prices["rds_postgres"][c] = _rds_prices(client, c)
    except Exception as exc:  # noqa: BLE001 - offline: the caller substitutes a known type
        return [f"{', '.join(ec2 + rds)} not in the price snapshot and live lookup failed ({type(exc).__name__})"]
    return [f"{', '.join(ec2 + rds)} not in the snapshot: fetched live from the AWS Pricing API"]


# --------------------------------------------------------------------------- live fetch


def _session():
    import boto3

    return boto3.session.Session()


def _products(pricing, service: str, **filters: str) -> list[dict]:
    flt = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
    out: list[dict] = []
    for page in pricing.get_paginator("get_products").paginate(ServiceCode=service, Filters=flt):
        out += [json.loads(item) for item in page["PriceList"]]
    return out


def _first_tier(product: dict, term: str = "OnDemand", **term_attrs: str) -> float:
    """Price of the first tier (beginRange 0) of a product's OnDemand or matching Reserved term."""
    for t in product["terms"].get(term, {}).values():
        if any(t.get("termAttributes", {}).get(k) != v for k, v in term_attrs.items()):
            continue
        dims = [d for d in t["priceDimensions"].values() if d["unit"] not in ("Quantity",)]
        dims.sort(key=lambda d: float(d.get("beginRange", "0")))
        return float(dims[0]["pricePerUnit"]["USD"])
    raise LookupError(f"no {term} price for {product['product']['attributes'].get('usagetype')}")


def _one(pricing, service: str, usagetype: str, **filters: str) -> float:
    matches = [p for p in _products(pricing, service, usagetype=usagetype, **filters)]
    if not matches:
        raise LookupError(f"{service} {usagetype} not found")
    return _first_tier(matches[0])


def _ec2_prices(pricing, itype: str) -> float:
    items = _products(pricing, "AmazonEC2", location=LOCATION, instanceType=itype, operatingSystem="Linux",
                      tenancy="Shared", preInstalledSw="NA", capacitystatus="Used")
    return _first_tier(items[0])


def _rds_prices(pricing, db_class: str) -> dict[str, Any]:
    items = _products(pricing, "AmazonRDS", location=LOCATION, instanceType=db_class, databaseEngine="PostgreSQL")
    out: dict[str, Any] = {}
    ri = {"LeaseContractLength": "1yr", "OfferingClass": "standard", "PurchaseOption": "No Upfront"}
    for p in items:
        attrs = p["product"]["attributes"]
        usage = attrs["usagetype"]
        if "InstanceUsage:" in usage:
            out["single_az_hourly"] = _first_tier(p)
            out["ri_1yr_no_upfront_single_az_hourly"] = _first_tier(p, "Reserved", **ri)
            out["vcpu"], out["memory_gib"] = int(attrs["vcpu"]), float(attrs["memory"].split()[0])
        elif "Multi-AZUsage:" in usage:
            out["multi_az_hourly"] = _first_tier(p)
            out["ri_1yr_no_upfront_multi_az_hourly"] = _first_tier(p, "Reserved", **ri)
    return out


def _savings_plan_rates(session, types: list[str]) -> dict[str, float]:
    sp = session.client("savingsplans", region_name="us-east-1")
    resp = sp.describe_savings_plans_offering_rates(
        savingsPlanTypes=["Compute"], savingsPlanPaymentOptions=["No Upfront"], products=["EC2"],
        serviceCodes=["AmazonEC2"], operations=["RunInstances"], usageTypes=[f"{PREFIX}-BoxUsage:{t}" for t in types],
        filters=[{"name": "region", "values": [REGION]}, {"name": "tenancy", "values": ["shared"]},
                 {"name": "productDescription", "values": ["Linux/UNIX"]}],
    )
    return {r["usageType"].split(":", 1)[1]: float(r["rate"]) for r in resp["searchResults"]
            if r["savingsPlanOffering"]["durationSeconds"] == 31536000}


def _instance_specs(session, types: list[str]) -> dict[str, dict]:
    ec2 = session.client("ec2", region_name=REGION)
    specs = {}
    for it in ec2.describe_instance_types(InstanceTypes=types)["InstanceTypes"]:
        net = it["NetworkInfo"]
        specs[it["InstanceType"]] = {
            "vcpu": it["VCpuInfo"]["DefaultVCpus"],
            "memory_mib": it["MemoryInfo"]["SizeInMiB"],
            "arch": "arm64" if "arm64" in it["ProcessorInfo"]["SupportedArchitectures"] else "x86_64",
            # EKS VPC CNI max pods without prefix delegation: ENIs * (IPs per ENI - 1) + 2
            "max_pods": net["MaximumNetworkInterfaces"] * (net["Ipv4AddressesPerInterface"] - 1) + 2,
            "burstable": bool(it.get("BurstablePerformanceSupported")),
        }
    return specs


def _spot_avg(session, itype: str) -> float | None:
    ec2 = session.client("ec2", region_name=REGION)
    now = datetime.now(UTC)
    hist = ec2.describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"],
                                           StartTime=now - timedelta(days=7), EndTime=now)["SpotPriceHistory"]
    return round(statistics.mean(float(h["SpotPrice"]) for h in hist), 5) if hist else None


def _eks_versions(session) -> tuple[dict[str, dict], str | None]:
    eks = session.client("eks", region_name=REGION)
    versions, default = {}, None
    for v in eks.describe_cluster_versions()["clusterVersions"]:
        versions[v["clusterVersion"]] = {
            "status": v["versionStatus"],
            "end_of_standard_support": v["endOfStandardSupportDate"].astimezone(UTC).date().isoformat(),
            "end_of_extended_support": v["endOfExtendedSupportDate"].astimezone(UTC).date().isoformat(),
        }
        if v.get("defaultVersion"):
            default = v["clusterVersion"]
    return versions, default


def fetch_live_prices() -> dict[str, Any]:
    """Fetch every price the cost model needs. Read-only calls; ~40 requests run in parallel."""
    session = _session()
    pricing = session.client("pricing", region_name="us-east-1")
    one = lambda svc, usage, **f: (lambda: _one(pricing, svc, f"{PREFIX}-{usage}", **f))
    jobs = {
        **{f"ec2:{t}": (lambda t=t: _ec2_prices(pricing, t)) for t in EC2_TYPES},
        **{f"rds:{c}": (lambda c=c: _rds_prices(pricing, c)) for c in RDS_CLASSES},
        **{f"spot:{t}": (lambda t=t: _spot_avg(session, t)) for t in EC2_TYPES},
        "sp": lambda: _savings_plan_rates(session, EC2_TYPES),
        "specs": lambda: _instance_specs(session, EC2_TYPES),
        "eks_versions": lambda: _eks_versions(session),
        "ebs_gp2": one("AmazonEC2", "EBS:VolumeUsage.gp2"),
        "ebs_gp3": one("AmazonEC2", "EBS:VolumeUsage.gp3"),
        "rds_gp2_single": one("AmazonRDS", "RDS:GP2-Storage", databaseEngine="PostgreSQL"),
        "rds_gp2_multi": one("AmazonRDS", "RDS:Multi-AZ-GP2-Storage", databaseEngine="PostgreSQL"),
        "rds_gp3_single": one("AmazonRDS", "RDS:GP3-Storage", databaseEngine="PostgreSQL"),
        "rds_gp3_multi": one("AmazonRDS", "RDS:Multi-AZ-GP3-Storage", databaseEngine="PostgreSQL"),
        "eks_cluster": one("AmazonEKS", "AmazonEKS-Hours:perCluster"),
        "eks_extended": one("AmazonEKS", "AmazonEKS-Hours:extendedSupport"),
        "nat_hour": one("AmazonEC2", "NatGateway-Hours"),
        "nat_gb": one("AmazonEC2", "NatGateway-Bytes"),
        "alb_hour": one("AWSELB", "LoadBalancerUsage", productFamily="Load Balancer-Application"),
        "alb_lcu": one("AWSELB", "LCUUsage", productFamily="Load Balancer-Application"),
        "ipv4": one("AmazonVPC", "PublicIPv4:InUseAddress"),
        "logs_ingest": one("AmazonCloudWatch", "DataProcessing-Bytes"),
        "logs_storage": one("AmazonCloudWatch", "TimedStorage-ByteHrs"),
        "dt_out": one("AWSDataTransfer", "DataTransfer-Out-Bytes"),
        "ecr": one("AmazonECR", "TimedStorage-ByteHrs"),
        "secret": one("AWSSecretsManager", "AWSSecretsManager-Secrets"),
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        r = {k: f.result(timeout=60) for k, f in futures.items()}  # any failure raises -> caller falls back

    sp, specs = r["sp"], r["specs"]
    versions, default_version = r["eks_versions"]
    return {
        "source": "aws-pricing-api",
        "fetched_at": date.today().isoformat(),
        "region": REGION,
        "location": LOCATION,
        "currency": "USD",
        "hours_per_month": 730,
        "notes": "On-demand Linux, shared tenancy. compute_sp_1yr_hourly = Compute Savings Plan 1-yr No Upfront rate; "
                 "spot_hourly_avg_7d = mean spot price over the 7 days before fetched_at.",
        "ec2": {t: {**specs[t], "on_demand_hourly": r[f"ec2:{t}"], "compute_sp_1yr_hourly": sp.get(t),
                    "spot_hourly_avg_7d": r[f"spot:{t}"]} for t in EC2_TYPES},
        "ebs_gb_month": {"gp2": r["ebs_gp2"], "gp3": r["ebs_gp3"]},
        "rds_postgres": {c: r[f"rds:{c}"] for c in RDS_CLASSES},
        "rds_storage_gb_month": {
            "gp2": {"single_az": r["rds_gp2_single"], "multi_az": r["rds_gp2_multi"]},
            "gp3": {"single_az": r["rds_gp3_single"], "multi_az": r["rds_gp3_multi"]},
        },
        "eks": {"cluster_hourly": r["eks_cluster"], "extended_support_hourly": r["eks_extended"],
                "default_version": default_version, "versions": versions},
        "nat_gateway": {"hourly": r["nat_hour"], "per_gb": r["nat_gb"]},
        "alb": {"hourly": r["alb_hour"], "lcu_hourly": r["alb_lcu"]},
        "public_ipv4_hourly": r["ipv4"],
        "cloudwatch_logs": {"ingest_per_gb": r["logs_ingest"], "storage_gb_month": r["logs_storage"]},
        "data_transfer_out_per_gb": r["dt_out"],
        "ecr_storage_gb_month": r["ecr"],
        "secrets_manager_secret_month": r["secret"],
    }
