"""Infrastructure facts: legacy Terraform (python-hcl2), instance specs, and read-only AWS lookups.

AWS calls are describe/get only and degrade to {"status": "skipped", "reason": ...} without credentials.
"""

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import hcl2
from hcl2 import SerializationOptions

HCL_OPTIONS = SerializationOptions(strip_string_quotes=True, explicit_blocks=False, with_comments=False)

# vCPU / memory (GiB) / architecture for the instance types a small legacy estate typically uses.
INSTANCE_SPECS: dict[str, tuple[int, float, str]] = {
    "t2.micro": (1, 1, "x86_64"), "t2.small": (1, 2, "x86_64"), "t2.medium": (2, 4, "x86_64"),
    "t2.large": (2, 8, "x86_64"), "t2.xlarge": (4, 16, "x86_64"),
    "t3.micro": (2, 1, "x86_64"), "t3.small": (2, 2, "x86_64"), "t3.medium": (2, 4, "x86_64"),
    "t3.large": (2, 8, "x86_64"), "t3.xlarge": (4, 16, "x86_64"), "t3.2xlarge": (8, 32, "x86_64"),
    "t3a.medium": (2, 4, "x86_64"), "t3a.large": (2, 8, "x86_64"), "t3a.xlarge": (4, 16, "x86_64"),
    "t4g.micro": (2, 1, "arm64"), "t4g.small": (2, 2, "arm64"), "t4g.medium": (2, 4, "arm64"),
    "t4g.large": (2, 8, "arm64"), "t4g.xlarge": (4, 16, "arm64"),
    "m5.large": (2, 8, "x86_64"), "m5.xlarge": (4, 16, "x86_64"), "m5.2xlarge": (8, 32, "x86_64"),
    "m6i.large": (2, 8, "x86_64"), "m6i.xlarge": (4, 16, "x86_64"), "m7i.large": (2, 8, "x86_64"),
    "m6g.large": (2, 8, "arm64"), "m6g.xlarge": (4, 16, "arm64"), "m7g.large": (2, 8, "arm64"),
    "m7g.xlarge": (4, 16, "arm64"), "c5.large": (2, 4, "x86_64"), "c5.xlarge": (4, 8, "x86_64"),
    "c6g.large": (2, 4, "arm64"), "c7g.large": (2, 4, "arm64"), "r5.large": (2, 16, "x86_64"),
    "r6g.large": (2, 16, "arm64"),
}
OS_BY_AMI = [("al2023", "Amazon Linux 2023"), ("amzn2", "Amazon Linux 2"), ("ubuntu", "Ubuntu"),
             ("debian", "Debian"), ("rhel", "Red Hat Enterprise Linux"), ("windows", "Windows Server")]
# System python3 shipped by each OS (the legacy unit runs /usr/bin/python3) and its upstream end of life.
SYSTEM_PYTHON = {"Amazon Linux 2023": ("3.9", "2025-10"), "Amazon Linux 2": ("3.7", "2023-06")}
DEFAULT_ROOT_VOLUME = {"type": "gp2", "size_gib": 8}  # what EC2 uses when root_block_device leaves them unset
ESTIMATE = {"cpu_avg_pct": 4.0, "cpu_p95_pct": 12.0, "mem_avg_pct": 18.0, "period_days": 14, "source": "estimate",
            "notes": "estimate, not measured: live AWS lookup disabled or unavailable; typical figures for a "
                     "low-traffic internal Flask app on a 2 vCPU host"}


def instance_specs(instance_type: str) -> dict[str, Any]:
    spec = INSTANCE_SPECS.get(instance_type.strip().lower())
    if not spec:
        return {"instance_type": instance_type, "error": "unknown instance type (not in the lookup table)"}
    vcpu, mem, arch = spec
    return {"instance_type": instance_type, "vcpu": vcpu, "memory_gib": mem, "arch": arch,
            "burstable": instance_type.startswith("t")}


def parse_terraform(files: dict[str, str], root: str = "legacy/terraform/") -> dict[str, Any]:
    """Parse every *.tf under `root` and extract the facts discovery needs, plus rule signals."""
    tf = {p: c for p, c in files.items() if p.startswith(root) and p.endswith(".tf")}
    if not tf:
        return {"source": "none", "error": f"no Terraform files under {root}", "signals": []}
    blocks: dict[str, list] = {}
    errors = []
    for path, text in sorted(tf.items()):
        try:
            for kind, items in hcl2.loads(text, serialization_options=HCL_OPTIONS).items():
                blocks.setdefault(kind, []).extend(items if isinstance(items, list) else [items])
        except Exception as exc:  # noqa: BLE001 - report the bad file and keep parsing the others
            errors.append(f"{path}: {type(exc).__name__}: {str(exc)[:120]}")

    variables = {k: v.get("default") for item in blocks.get("variable", []) for k, v in item.items()}
    locals_ = {k: v for item in blocks.get("locals", []) for k, v in item.items()}
    resources = _typed(blocks.get("resource", []))
    data = _typed(blocks.get("data", []))
    resolve = _resolver(variables, locals_)

    provider = next((v for item in blocks.get("provider", []) for k, v in item.items() if k == "aws"), {})
    tags = {}
    for dt in _as_list(provider.get("default_tags")):
        tags.update({k: resolve(v) for k, v in (dt.get("tags") or {}).items()})
    region = resolve(provider.get("region")) or resolve(variables.get("region"))

    instances = resources.get("aws_instance", {})
    out: dict[str, Any] = {"source": "terraform", "files": sorted(p.removeprefix("legacy/") for p in tf),
                           "provider": "aws", "region": region,
                           "parse_errors": errors, "instance": None, "security_groups": [], "tags": tags}
    if instances:
        name, inst = next(iter(instances.items()))
        root_vol = (_as_list(inst.get("root_block_device")) or [{}])[0]
        meta = (_as_list(inst.get("metadata_options")) or [{}])[0]
        ami_ref, ami_source = str(inst.get("ami", "")), str(inst.get("ami", ""))
        if m := re.search(r"data\.aws_ssm_parameter\.(\w+)", ami_ref):
            ami_source = "ssm:" + str(data.get("aws_ssm_parameter", {}).get(m.group(1), {}).get("name", "?"))
        elif m := re.search(r"data\.aws_ami\.(\w+)", ami_ref):
            ami = data.get("aws_ami", {}).get(m.group(1), {})
            names = [v for f in _as_list(ami.get("filter")) for v in _as_list(f.get("values"))]
            ami_source = f"aws_ami filter: {', '.join(map(str, names)) or '?'}"
        instance_type = resolve(inst.get("instance_type"))
        vol_type, vol_size = resolve(root_vol.get("volume_type")), resolve(root_vol.get("volume_size"))
        tags.update({k: resolve(v) for k, v in (inst.get("tags") or {}).items()})
        out["instance"] = {
            "resource": f"aws_instance.{name}", "instance_type": instance_type, "count": inst.get("count", 1),
            "ami_source": ami_source, "os": next((os for key, os in OS_BY_AMI if key in ami_source.lower()), "unknown"),
            # A plain EBS type (a price key for FinOps); `defaulted` flags AWS defaults assumed for unset values.
            "root_volume": {"type": vol_type or DEFAULT_ROOT_VOLUME["type"],
                            "size_gib": vol_size or DEFAULT_ROOT_VOLUME["size_gib"],
                            "encrypted": _truthy(root_vol.get("encrypted")), "defaulted": not (vol_type and vol_size)},
            "imds_http_tokens": resolve(meta.get("http_tokens")) or "not set",
            "user_data": "user_data" in inst or "user_data_base64" in inst,
            **{k: v for k, v in instance_specs(instance_type or "").items() if k in ("vcpu", "memory_gib", "arch")},
        }
    out["security_groups"] = _security_groups(resources, resolve)
    out["high_availability"] = {
        "instances": len(instances), "load_balancer": bool(resources.keys() & {"aws_lb", "aws_alb", "aws_elb"}),
        "autoscaling_group": "aws_autoscaling_group" in resources,
    }
    out["backups"] = bool(resources.keys() & {"aws_backup_plan", "aws_dlm_lifecycle_policy", "aws_db_instance"})
    out["signals"] = _infra_signals(out)
    return out


def _infra_signals(infra: dict) -> list[dict[str, str]]:
    signals = []
    inst, ha, files = infra["instance"], infra["high_availability"], ", ".join(infra["files"])
    if inst and ha["instances"] <= 1 and not ha["autoscaling_group"] and str(inst["count"]) in ("1", "None"):
        signals.append({"key": "single_instance", "evidence": f"{inst['resource']}: one instance, no load balancer "
                        "or Auto Scaling group, single Availability Zone"})
    if inst and not infra["backups"]:
        signals.append({"key": "no_backups", "evidence": f"{files}: no aws_backup_plan, DLM snapshot policy or "
                        "managed database; data lives only on the instance's EBS root volume"})
    open_rules = [f"{sg['resource']}: {r['protocol']}/{r['ports']} from {r['cidr']}"
                  for sg in infra["security_groups"] for r in sg["ingress"] if r["cidr"] in ("0.0.0.0/0", "::/0")]
    if open_rules:
        signals.append({"key": "public_http", "evidence": "; ".join(open_rules)})
    if inst:
        gaps = []
        if inst["root_volume"]["encrypted"] is not True:
            gaps.append("root volume `encrypted` not set (relies on the account default)")
        if inst["imds_http_tokens"] == "optional":
            gaps.append("IMDSv1 allowed (http_tokens = optional)")
        if gaps:
            signals.append({"key": "volume_encryption", "evidence": f"{inst['resource']}: {'; '.join(gaps)}"})
    return signals


def _security_groups(resources: dict, resolve) -> list[dict]:
    """Inline ingress blocks plus standalone ingress rule resources (old and new AWS provider styles)."""
    groups = []
    for name, sg in resources.get("aws_security_group", {}).items():
        rules = [_rule(r, resolve) for r in _as_list(sg.get("ingress"))]
        groups.append({"resource": f"aws_security_group.{name}", "ingress": [r for rs in rules for r in rs]})
    for kind in ("aws_security_group_rule", "aws_vpc_security_group_ingress_rule"):
        for name, rule in resources.get(kind, {}).items():
            if kind == "aws_security_group_rule" and resolve(rule.get("type")) != "ingress":
                continue
            groups.append({"resource": f"{kind}.{name}", "ingress": _rule(rule, resolve)})
    return groups


def _rule(rule: dict, resolve) -> list[dict]:
    lo, hi = resolve(rule.get("from_port")), resolve(rule.get("to_port"))
    ports = str(lo) if lo == hi else f"{lo}-{hi}"
    protocol = resolve(rule.get("protocol") or rule.get("ip_protocol"))
    cidrs = _as_list(rule.get("cidr_blocks")) + _as_list(rule.get("cidr_ipv4")) + _as_list(rule.get("ipv6_cidr_blocks"))
    return [{"protocol": protocol, "ports": ports, "cidr": resolve(c)} for c in cidrs] or \
        [{"protocol": protocol, "ports": ports, "cidr": "security-group reference"}]


# ---- live AWS (read-only) ----

def describe_live_instance(region: str, tag_key: str = "app", tag_value: str = "legacy-inventory") -> dict[str, Any]:
    """EC2 DescribeInstances by tag, plus the root EBS volume. Never raises."""
    try:
        ec2 = _client("ec2", region)
        resp = ec2.describe_instances(Filters=[{"Name": f"tag:{tag_key}", "Values": [tag_value]},
                                               {"Name": "instance-state-name", "Values": ["running", "stopped"]}])
        found = [i for r in resp["Reservations"] for i in r["Instances"]]
        if not found:
            return {"status": "skipped", "reason": f"no running instance tagged {tag_key}={tag_value} in {region}"}
        inst = found[0]
        out = {"status": "ok", "instance_id": inst["InstanceId"], "instance_type": inst["InstanceType"],
               "state": inst["State"]["Name"], "availability_zone": inst["Placement"]["AvailabilityZone"],
               "architecture": inst.get("Architecture"), "platform": inst.get("PlatformDetails"),
               "launch_time": inst["LaunchTime"].isoformat(), "image_id": inst.get("ImageId"),
               "imds_http_tokens": inst.get("MetadataOptions", {}).get("HttpTokens"), "matches": len(found),
               "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])}}
        root = next((m for m in inst.get("BlockDeviceMappings", []) if m["DeviceName"] == inst.get("RootDeviceName")), None)
        if root and "Ebs" in root:
            vol = ec2.describe_volumes(VolumeIds=[root["Ebs"]["VolumeId"]])["Volumes"][0]
            out["root_volume"] = {"type": vol["VolumeType"], "size_gib": vol["Size"], "encrypted": vol["Encrypted"]}
        return out
    except Exception as exc:  # noqa: BLE001 - no credentials, no permission, throttling, network...
        return {"status": "skipped", "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}


def cpu_utilization(region: str, instance_id: str, days: int = 14) -> dict[str, Any]:
    """CloudWatch CPUUtilization: mean of hourly averages and p95 of hourly maxima.

    Falls back to the last 24 h at 5-minute granularity when there is no hourly data yet.
    """
    try:
        cw = _client("cloudwatch", region)
        end = datetime.now(UTC)
        for period, span, label in ((3600, timedelta(days=days), days), (300, timedelta(hours=24), 1)):  # noqa: B007
            points = cw.get_metric_statistics(
                Namespace="AWS/EC2", MetricName="CPUUtilization", StartTime=end - span, EndTime=end, Period=period,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}], Statistics=["Average", "Maximum"],
            )["Datapoints"]
            if points:
                break
        if not points:
            return {"status": "skipped", "reason": f"no CPUUtilization datapoints for {instance_id} in the last {days} days"}
        # Memory needs the CloudWatch agent; find its exact dimension set first (it may add more than InstanceId).
        mem_metrics = cw.list_metrics(Namespace="CWAgent", MetricName="mem_used_percent",
                                      Dimensions=[{"Name": "InstanceId", "Value": instance_id}])["Metrics"]
        mem = cw.get_metric_statistics(
            Namespace="CWAgent", MetricName="mem_used_percent", StartTime=end - span, EndTime=end, Period=period,
            Dimensions=mem_metrics[0]["Dimensions"], Statistics=["Average"],
        )["Datapoints"] if mem_metrics else []
        return {
            "status": "ok", "cpu_avg_pct": round(sum(p["Average"] for p in points) / len(points), 1),
            "cpu_p95_pct": round(percentile([p["Maximum"] for p in points], 95), 1),
            "mem_avg_pct": round(sum(p["Average"] for p in mem) / len(mem), 1) if mem else None,
            "period_days": label, "granularity_s": period, "datapoints": len(points), "source": "cloudwatch",
        }
    except Exception as exc:  # noqa: BLE001 - never fail the assessment on a metrics lookup
        return {"status": "skipped", "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _client(service: str, region: str):
    import boto3
    from botocore.config import Config

    cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2, "mode": "standard"})
    return boto3.client(service, region_name=region, config=cfg)


# ---- HCL helpers ----

def _typed(items: list[dict]) -> dict[str, dict[str, dict]]:
    """[{"aws_instance": {"app": {...}}}, ...] -> {"aws_instance": {"app": {...}}}"""
    out: dict[str, dict[str, dict]] = {}
    for item in items:
        for kind, named in item.items():
            out.setdefault(kind, {}).update(named)
    return out


def _resolver(variables: dict, locals_: dict):
    def resolve(value):
        if isinstance(value, str) and (m := re.fullmatch(r"\$\{(var|local)\.(\w+)\}", value)):
            return (variables if m.group(1) == "var" else locals_).get(m.group(2), value)
        return value
    return resolve


def _as_list(value) -> list:
    return [] if value is None else value if isinstance(value, list) else [value]


def _truthy(value) -> bool | None:
    return None if value is None else str(value).lower() == "true"
