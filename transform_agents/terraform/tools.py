"""Strands tools for the Terraform agent, plus a presenter CLI.

    uv run python -m transform_agents.terraform.tools --warmup        # before the talk: warm module/provider cache
    uv run python -m transform_agents.terraform.tools --validate DIR  # layered validation of any Terraform dir
"""

import argparse
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from strands import ToolContext, tool

from transform_agents.core import ResultBox, Workspace

from .hcl import HclParseError, TfConfig, set_defaults, suggest, tfvars_example
from .summary import facts, mismatch_lines, spec_checks
from .validate import Report, blueprint_files, knowledge, parse_all, terraform_bin, validate, warmup

PREFIX = "terraform"
CONTEXT_TOOLS = {"terraform_validate", "submit_result"}  # tools that take a strands ToolContext
BLUEPRINT_SUMMARY = """main.tf (do not rewrite it) already builds:
- module "vpc" (terraform-aws-modules/vpc/aws ~> 6.0): var.az_count AZs, public + private /24 subnets from var.vpc_cidr,
  NAT per var.single_nat_gateway, S3 gateway endpoint.
- module "eks" (terraform-aws-modules/eks/aws ~> 21.0): cluster "<name>-eks" on var.kubernetes_version with addons
  (coredns, kube-proxy, vpc-cni, eks-pod-identity-agent); managed node group "default" in private subnets:
  AMI from var.node_architecture, var.node_instance_types, var.node_capacity_type,
  min/desired var.node_min, max var.node_max.
- aws_ecr_repository.app: var.ecr_repository_name, IMMUTABLE tags, scan on push, untagged-image lifecycle policy.
- aws_db_instance.db: PostgreSQL var.db_engine_version on var.db_instance_class, var.db_multi_az, encrypted gp3 storage
  with autoscaling, master password managed in Secrets Manager, private subnets, security group open only to the EKS
  nodes, rds.force_ssl = 1, var.db_backup_retention_days, var.db_deletion_protection, final snapshot in prod.
- aws_s3_bucket.reports: private, encrypted bucket for the nightly report (no local disk).
- EKS Pod Identity role for service account <name>/<name>: read the DB secret, write reports.
- Every resource is tagged with var.tags (provider default_tags)."""


@dataclass
class TerraformRun:
    """Per-request state shared by the tools: workspace, inputs, validation cache and budgets."""

    ws: Workspace
    spec: dict = field(default_factory=dict)
    modernization: dict = field(default_factory=dict)
    feedback: str = ""  # reviewer feedback overrides the spec mapping rules
    max_validations: int = 4
    attempts: int = 0
    submit_rejected: bool = False
    _report: Report | None = field(default=None, init=False, repr=False)
    _report_key: str = field(default="", init=False, repr=False)
    _baseline: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        # Revision runs start from the previous files: remember which spec checks already passed.
        self._baseline = {c["check"] for c in self.checks() if c["status"] == "pass"} if self.tf_files() else set()

    def open_mismatches(self) -> tuple[str, list[str]]:
        """(heading, problems) the agent must address. With reviewer feedback, only values that moved away from
        the spec rules count, and the model decides whether the feedback asked for them."""
        failing = [c for c in self.checks() if c["status"] != "pass"]
        if not self.feedback:
            return "Spec mismatches (fix them with set_variables):", mismatch_lines(failing)
        moved = [c for c in failing if c["check"] in self._baseline]
        return ("Changed away from the spec rules (revert unless the reviewer feedback asked for it):",
                mismatch_lines(moved))

    def open_problems(self) -> list[str]:
        """Validation errors plus the spec mismatches the agent still has to address."""
        return [str(e) for e in self.validate().errors] + self.open_mismatches()[1]

    def files(self) -> dict[str, str]:
        """Top-level files under terraform/ as {name: text}."""
        out = {}
        for path in self.ws.listing(PREFIX + "/"):
            name = path[len(PREFIX) + 1:]
            if "/" not in name:
                out[name] = self.ws.files[path]
        return out

    def tf_files(self) -> dict[str, str]:
        return {n: t for n, t in self.files().items() if n.endswith(".tf")}

    def validate(self) -> Report:
        """Validate terraform/ (cached while the files are unchanged)."""
        files = self.tf_files()
        key = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if self._report is None or key != self._report_key:
            self._report, self._report_key = validate(files), key
        return self._report

    def config(self) -> TfConfig | None:
        cfg, errors = parse_all(self.tf_files())
        return None if errors else cfg

    def checks(self) -> list[dict]:
        cfg = self.config()
        return spec_checks(facts(cfg), self.spec, self.modernization) if cfg else []

    def finalize(self, agent_data: dict | None) -> dict | None:
        """Format the files, regenerate terraform.tfvars.example and build result.data from the code itself."""
        if not self.tf_files():
            return None
        report = self.validate()
        for name, text in self.tf_files().items():  # rewrite all, so artifacts carry the complete terraform/ set
            self.ws.write(f"{PREFIX}/{name}", report.formatted.get(name, text))
        try:
            self.ws.write(f"{PREFIX}/terraform.tfvars.example", tfvars_example(self.files()["variables.tf"]))
        except (KeyError, HclParseError):
            pass
        cfg = self.config()
        f = facts(cfg) if cfg else {}
        checks = spec_checks(f, self.spec, self.modernization) if cfg else []
        coverage = normalize_coverage((agent_data or {}).get("spec_coverage")) or [
            {"requirement": c["check"], "how": f"{c['actual']} ({c['rule']})"} for c in checks if c["status"] == "pass"]
        return {
            "valid": report.valid,
            "validate_output": report.terraform_output if report.valid else report.text(),
            "validation_level": report.level,
            "validate_attempts": self.attempts,
            "modules": [{"source": m["source"], "version": m["version"]} for m in f.get("modules", [])],
            "compute": f.get("compute", {}),
            "database": f.get("database", {}),
            "network": f.get("network", {}),
            "registry": {"repositories": (f.get("registry") or {}).get("repositories", [])},
            "outputs": f.get("outputs", []),
            "region": f.get("region"),
            "cluster_name": f.get("cluster_name"),
            "kubernetes_version": f.get("kubernetes_version"),
            "tags": f.get("tags", {}),
            "spec_checks": checks,
            "spec_coverage": coverage,
            "files": sorted(f"{PREFIX}/{n}" for n in self.files()),
        }


def normalize_coverage(items: Any) -> list[dict[str, str]]:
    """Accept the shapes models produce and return [{"requirement", "how"}]."""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            return []
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            req = item.get("requirement") or item.get("id") or item.get("requirement_id") or ""
            how = item.get("how") or item.get("implementation") or item.get("description") or ""
            if req or how:
                out.append({"requirement": str(req), "how": str(how)})
    return out


def describe_schema(source: str, query: str = "") -> str:
    """Offline lookup in knowledge/module-inputs.json (modules, AWS resources and data sources)."""
    kb = knowledge()
    words = [w.lower() for w in query.replace(",", " ").split()]

    def hit(name: str) -> bool:
        return not words or any(w in name.lower() for w in words)

    key = source.strip().removeprefix("registry.terraform.io/")
    if key in kb["modules"]:
        mod = kb["modules"][key]
        lines = [f'{key} {mod["version"]} (use version = "{mod["constraint"]}"): {len(mod["inputs"])} inputs']
        matched = {n: i for n, i in mod["inputs"].items() if hit(n)}
        if not words:
            lines.append("inputs: " + ", ".join(matched))
            lines.append("outputs: " + ", ".join(mod["outputs"]))
            lines.append("Pass query=<words> for types, defaults and object attributes.")
            return "\n".join(lines)
        for name, info in list(matched.items())[:25]:
            default = "required" if info["required"] else f"default {json.dumps(info.get('default'))}"
            lines.append(f"- {name} ({info['type'][:60]}; {default}): {info['description'][:120]}")
            if info.get("attributes"):
                lines.append(f"    attributes: {', '.join(info['attributes'])}")
        outs = [o for o in mod["outputs"] if hit(o)]
        lines.append("outputs: " + (", ".join(outs) or "(none match)"))
        return "\n".join(lines) if matched or outs else "\n".join(lines) + "\nno inputs match; try other words"
    for kind in ("resources", "data_sources"):
        if key in kb[kind]:
            sch = kb[kind][key]
            args = [a for a in sch["arguments"] if hit(a)]
            lines = [f"{key} ({kind[:-1].replace('_', ' ')}, aws provider {kb['generated']['aws_provider']})",
                     "arguments: " + ", ".join(args), "required: " + (", ".join(sch.get("required", [])) or "(none)")]
            for bname, blk in sch.get("blocks", {}).items():
                lines.append(f"block {bname} {{ {', '.join(blk.get('arguments', []))} }}")
            return "\n".join(lines)
    known = list(kb["modules"]) + list(kb["resources"]) + list(kb["data_sources"])
    return f"error: no offline schema for {source!r}{suggest(key, known)}. Known: {', '.join(known)}"


def terraform_tools(run: TerraformRun, box: ResultBox) -> list:
    """The Terraform agent's domain tools, bound to one run."""

    @tool
    def get_blueprint() -> str:
        """Copy the validated EKS platform blueprint into terraform/ and show terraform/variables.tf.

        Call this first. The blueprint is already valid for terraform-aws-modules vpc ~> 6.0 and eks ~> 21.0;
        adapt its variable defaults to the spec instead of writing Terraform from scratch.
        """
        if run.tf_files():
            note = "terraform/ already has files (revision run): they were kept as they are."
        else:
            for name, text in blueprint_files().items():
                run.ws.write(f"{PREFIX}/{name}", text)
            run.ws.write(f"{PREFIX}/terraform.tfvars.example", tfvars_example(blueprint_files()["variables.tf"]))
            note = "Copied the blueprint into terraform/."
        files = ", ".join(f"terraform/{n}" for n in run.files())
        return (f"{note}\nFiles: {files}\n\n{BLUEPRINT_SUMMARY}\n\n--- terraform/variables.tf ---\n"
                f"{run.files().get('variables.tf', '(missing)')}\n"
                "Next: call set_variables once with every spec-driven value.")

    @tool
    def get_module_inputs(source: str, query: str = "") -> str:
        """Look up, offline, the real inputs and outputs of a pinned module or the arguments of an AWS resource.

        Args:
            source: Module source like "terraform-aws-modules/eks/aws", or a resource type like "aws_db_instance".
            query: Optional words that filter input names, e.g. "node endpoint".
        """
        return describe_schema(source, query)

    @tool
    def set_variables(values: dict[str, Any]) -> str:
        """Set default values in terraform/variables.tf. Send every spec-driven value in one call.

        Args:
            values: Variable name to new default, e.g. {"name": "orders", "node_min": 2,
                "node_instance_types": ["t4g.medium"], "db_multi_az": true, "tags": {"app": "orders"}}.
        """
        path = f"{PREFIX}/variables.tf"
        if path not in run.ws.files:
            return "error: terraform/variables.tf does not exist; call get_blueprint first"
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except json.JSONDecodeError:
                return "error: values must be an object of variable name -> value"
        text, changes, errors = set_defaults(run.ws.files[path], values or {})
        if changes:
            run.ws.write(path, text)
        lines = [f"Updated {path}: {len(changes)} change(s)."] + [f"- {c}" for c in changes]
        unchanged = [k for k in values if not any(c.startswith(f"{k}:") for c in changes)
                     and not any(f"'{k}'" in e or e.startswith(f"{k}:") for e in errors)]
        if unchanged:
            lines.append(f"Already set: {', '.join(unchanged)}")
        lines += [f"error: {e}" for e in errors]
        lines.append("Next: call terraform_validate.")
        return "\n".join(lines)

    @tool(context=True)
    def terraform_validate(tool_context: ToolContext) -> str:
        """Validate terraform/: fmt + HCL parse, offline module/resource argument checks, `terraform validate`
        when the binary is available, and checks that the values match the migration spec."""
        run.attempts += 1
        if run.attempts > run.max_validations + 2:
            tool_context.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
            return "Validation budget exhausted; stopping. The orchestrator records the current state."
        report = run.validate()
        heading, mismatches = run.open_mismatches()
        lines = [f"terraform_validate attempt {run.attempts}/{run.max_validations}: {report.text()}"]
        if report.valid and report.level == "terraform":
            lines.append(report.terraform_output)
        if mismatches:
            lines.append(heading)
            lines += [f"  - {m}" for m in mismatches]
        if run.attempts >= run.max_validations and (not report.valid or mismatches):
            lines.append("This was the last validation round: call submit_result now and list what is left.")
        elif not report.valid:
            lines.append("Fix every error (set_variables for defaults, edit_file otherwise), then validate again.")
        elif mismatches:
            lines.append("Fix the mismatches, then validate again.")
        else:
            lines.append("Next: call submit_result.")
        return "\n".join(lines)

    @tool(context=True)
    def submit_result(summary: str, spec_coverage: list[dict[str, Any]], tool_context: ToolContext) -> str:
        """Submit the final result once terraform_validate reports VALID. Call exactly once, at the very end.

        Args:
            summary: 2-4 sentences for the reviewer: what was adapted from the spec and the validation outcome.
            spec_coverage: One entry per spec requirement the infrastructure addresses, e.g.
                [{"requirement": "SEC3", "how": "RDS in private subnets, security group only from EKS nodes"}].
        """
        problems = run.open_problems()
        if problems and not run.submit_rejected and run.attempts < run.max_validations:
            run.submit_rejected = True
            hint = " (if the reviewer feedback asked for these, just call submit_result again)" if run.feedback else ""
            return ("error: not submitted yet. Open problems:\n" + "\n".join(f"- {p}" for p in problems[:10])
                    + f"\nFix them, call terraform_validate, then submit_result again{hint}.")
        box.set(summary, {"spec_coverage": normalize_coverage(spec_coverage)})
        tool_context.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
        return "result recorded"

    return [get_blueprint, get_module_inputs, set_variables, terraform_validate, submit_result]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--warmup", action="store_true", help="download modules/providers and validate the blueprint")
    parser.add_argument("--validate", metavar="DIR", help="run the layered validator on a Terraform directory")
    parser.add_argument("--offline", action="store_true", help="skip the terraform binary (as on AgentCore Runtime)")
    args = parser.parse_args(argv)
    if args.warmup:
        started = time.time()
        print(f"terraform: {terraform_bin() or 'not found'}")
        for line in warmup():
            print(line)
        print(f"warm-up done in {time.time() - started:.1f}s")
    elif args.validate:
        files = {p.name: p.read_text() for p in sorted(Path(args.validate).glob("*.tf"))}
        print(validate(files, offline=args.offline).text())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
