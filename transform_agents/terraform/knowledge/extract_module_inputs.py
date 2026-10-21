"""Regenerate knowledge/module-inputs.json from a real `terraform init` (needs the terraform binary and internet).

    uv run python transform_agents/terraform/knowledge/extract_module_inputs.py

It downloads terraform-aws-modules/vpc/aws ~> 6.0 and eks/aws ~> 21.0 plus the AWS provider ~> 6.0, then records:
- every module input (type, required, short default and description, object attributes for map(object(...)) types)
  and every module output;
- the arguments and nested blocks of the AWS resources and data sources an EKS platform typically uses.
The terraform agent uses this file for offline validation (no terraform binary on AgentCore Runtime).
"""

import json
import tempfile
from datetime import date
from pathlib import Path

from transform_agents.terraform.hcl import parse
from transform_agents.terraform.validate import KNOWLEDGE, run_tf, terraform_bin

MODULES = {"vpc": ("terraform-aws-modules/vpc/aws", "~> 6.0"), "eks": ("terraform-aws-modules/eks/aws", "~> 21.0")}
AWS_PROVIDER = "~> 6.0"
RESOURCES = [
    "aws_db_instance", "aws_db_subnet_group", "aws_db_parameter_group", "aws_security_group", "aws_security_group_rule",
    "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule", "aws_ecr_repository",
    "aws_ecr_lifecycle_policy", "aws_ecr_repository_policy", "aws_iam_role", "aws_iam_role_policy", "aws_iam_policy",
    "aws_iam_role_policy_attachment", "aws_eks_pod_identity_association", "aws_eks_addon", "aws_eks_access_entry",
    "aws_s3_bucket", "aws_s3_bucket_public_access_block", "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_versioning", "aws_s3_bucket_lifecycle_configuration", "aws_s3_bucket_policy", "aws_vpc_endpoint",
    "aws_kms_key", "aws_kms_alias", "aws_secretsmanager_secret", "aws_secretsmanager_secret_version",
    "aws_cloudwatch_log_group", "aws_cloudwatch_metric_alarm", "aws_sns_topic", "aws_backup_vault", "aws_backup_plan",
    "aws_backup_selection", "aws_db_option_group", "aws_elasticache_cluster", "aws_lb", "aws_acm_certificate",
]
DATA_SOURCES = ["aws_availability_zones", "aws_iam_policy_document", "aws_caller_identity", "aws_region",
                "aws_partition", "aws_eks_cluster", "aws_eks_cluster_auth", "aws_ami", "aws_ssm_parameter"]


def object_attributes(type_expr: str) -> tuple[str | None, list[str] | None]:
    """('map'|'object'|'list', top-level attribute names) for object-typed variables, else (None, None)."""
    t = type_expr.removeprefix("${").removesuffix("}").replace(" ", "")
    container = next((c for p, c in [("map(object(", "map"), ("object(", "object"), ("list(object(", "list"),
                                     ("set(object(", "list")] if t.startswith(p)), None)
    if container is None:
        return None, None
    i, depth, buf, names, in_name, in_str = t.index("object({") + len("object({"), 0, "", [], True, False
    while i < len(t):
        ch = t[i]
        if in_str:
            in_str = ch != '"'
        elif ch == '"':
            in_str = True
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            in_name, buf = True, ""
        elif ch == "=" and depth == 0 and in_name:
            names.append(buf)
            in_name = False
        elif in_name and depth == 0:
            buf += ch
        i += 1
    return container, names


def short(value, limit: int = 80):
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else f"({type(value).__name__}, {len(text)} chars)"


def module_schema(module_dir: Path) -> tuple[dict, list[str]]:
    inputs, outputs = {}, []
    for tf in sorted(module_dir.glob("*.tf")):
        doc = parse(tf.read_text())
        for entry in doc.get("variable", []):
            for name, var in entry.items():
                info = {"type": str(var.get("type", "any")).removeprefix("${").removesuffix("}"),
                        "required": "default" not in var,
                        "description": str(var.get("description", "")).strip()[:160]}
                if "default" in var:
                    info["default"] = short(var["default"])
                container, attrs = object_attributes(str(var.get("type", "")))
                if attrs:
                    info["container"], info["attributes"] = container, attrs
                inputs[name] = info
        for entry in doc.get("output", []):
            outputs.extend(k for k in entry if k != "__is_block__")
    return dict(sorted(inputs.items())), sorted(outputs)


def block_schema(block: dict, depth: int = 0) -> dict:
    attrs = block.get("attributes", {})
    out: dict = {"arguments": sorted(n for n, a in attrs.items() if a.get("optional") or a.get("required"))}
    required = sorted(n for n, a in attrs.items() if a.get("required"))
    if required:
        out["required"] = required
    blocks = {}
    for name, bt in block.get("block_types", {}).items():
        blocks[name] = block_schema(bt["block"], depth + 1) if depth < 3 else {"arguments": []}
    for name, a in attrs.items():  # "attributes as blocks", e.g. aws_security_group.ingress
        t = a.get("type")
        if (a.get("optional") or a.get("required")) and isinstance(t, list) and t[0] in ("set", "list") \
                and isinstance(t[1], list) and t[1][0] == "object":
            blocks[name] = {"arguments": sorted(t[1][1])}
    if blocks:
        out["blocks"] = dict(sorted(blocks.items()))
    return out


def main() -> None:
    if not terraform_bin():
        raise SystemExit("terraform binary not found")
    with tempfile.TemporaryDirectory(prefix="tf-extract-") as tmp:
        root = Path(tmp)
        calls = "\n".join(f'module "{n}" {{\n  source  = "{s}"\n  version = "{v}"\n}}' for n, (s, v) in MODULES.items())
        (root / "main.tf").write_text('terraform {\n  required_providers {\n    aws = { source = "hashicorp/aws", '
                                      f'version = "{AWS_PROVIDER}" }}\n  }}\n}}\n{calls}\n')
        proc = run_tf(["init", "-backend=false", "-input=false", "-no-color"], root, 600)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr)
        installed = {m["Key"]: m for m in json.loads((root / ".terraform/modules/modules.json").read_text())["Modules"]}
        modules = {}
        for name, (source, constraint) in MODULES.items():
            inputs, outputs = module_schema(root / installed[name]["Dir"])
            modules[source] = {"constraint": constraint, "version": installed[name]["Version"],
                               "inputs": inputs, "outputs": outputs}
        schema = json.loads(run_tf(["providers", "schema", "-json"], root, 300).stdout)
        aws = schema["provider_schemas"]["registry.terraform.io/hashicorp/aws"]
        lock = (root / ".terraform.lock.hcl").read_text()
        aws_version = lock.split('"registry.terraform.io/hashicorp/aws"')[1].split('version')[1].split('"')[1]
    out = {
        "generated": {"by": "extract_module_inputs.py", "date": date.today().isoformat(), "aws_provider": aws_version},
        "modules": modules,
        "resources": {r: block_schema(aws["resource_schemas"][r]["block"]) for r in RESOURCES},
        "data_sources": {d: block_schema(aws["data_source_schemas"][d]["block"]) for d in DATA_SOURCES},
    }
    target = KNOWLEDGE / "module-inputs.json"
    target.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {target} ({target.stat().st_size // 1024} KiB): "
          + ", ".join(f"{s} {m['version']} ({len(m['inputs'])} inputs)" for s, m in modules.items())
          + f", aws {aws_version}, {len(RESOURCES)} resources, {len(DATA_SOURCES)} data sources")


if __name__ == "__main__":
    main()
