"""Discovery tools: deterministic scanners the LLM calls, and the validated submit_result tool.

Facts (inventory, evidence) come from Python. The model only judges: severity per finding,
the 6R strategy, the readiness score, and the narrative of reports/assessment.md.
"""

import json
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from strands import ToolContext, tool

from transform_agents.core import ResultBox, Workspace

from .codescan import scan_app
from .infra import ESTIMATE, SYSTEM_PYTHON, cpu_utilization, describe_live_instance, instance_specs, parse_terraform

RULES: list[dict] = yaml.safe_load((Path(__file__).parent / "knowledge" / "assessment_rules.yaml").read_text())
SEVERITIES = ("critical", "high", "medium", "low")
STRATEGIES = ("rehost", "replatform", "refactor", "repurchase", "retain", "retire")
DEFAULT_TARGET = "containerized microservice on Amazon EKS + Amazon RDS for PostgreSQL"
REPORT = "reports/assessment.md"
APPENDIX_MARKER = "<!-- generated: inventory and findings tables rendered from the tool results -->"
MAX_SUBMIT_ATTEMPTS = 3


class DiscoveryContext:
    """Per-request facts, computed once and shared by every tool call."""

    def __init__(self, ws: Workspace, live_aws: bool = False, region: str = "us-west-2",
                 tag: tuple[str, str] = ("app", "legacy-inventory")):
        self.ws, self.live_aws, self.region, self.tag = ws, live_aws, region, tag
        self.submit_attempts = 0

    @cached_property
    def code(self) -> dict[str, Any]:
        return scan_app(self.ws.files)

    @cached_property
    def infra(self) -> dict[str, Any]:
        return parse_terraform(self.ws.files)

    @cached_property
    def live(self) -> dict[str, Any]:
        if not self.live_aws:
            return {"status": "skipped", "reason": "options.live_aws is false"}
        return describe_live_instance(self.infra.get("region") or self.region, *self.tag)

    @cached_property
    def utilization(self) -> dict[str, Any]:
        if self.live.get("status") == "ok":
            measured = cpu_utilization(self.infra.get("region") or self.region, self.live["instance_id"])
            if measured.get("status") == "ok":
                measured.pop("status")
                if measured["mem_avg_pct"] is None:
                    measured["mem_avg_pct"] = ESTIMATE["mem_avg_pct"]
                    measured["notes"] = "CPU measured in CloudWatch; memory is an estimate (no CloudWatch agent)"
                return measured
            reason = measured["reason"]
        else:
            reason = self.live["reason"]
        return {**ESTIMATE, "notes": f"{ESTIMATE['notes']} ({reason})"}

    def signals(self) -> list[dict[str, str]]:
        """Rule signals from every source, including the cross-cutting ones."""
        signals = [*self.code["signals"], *self.infra["signals"]]
        facts = self.facts()
        app, infra, util = facts["application"], facts["infrastructure"], self.utilization
        if py := SYSTEM_PYTHON.get(infra["os"]):
            signals.append({"key": "outdated_stack", "evidence": f"system python3 {py[0]} on {infra['os']} "
                            f"(end of life {py[1]}) runs {app['entrypoint']}"})
        if infra.get("vcpu") and util["cpu_p95_pct"] < 40:
            gp2 = "; gp2 root volume (gp3 costs ~20% less)" if str(infra["volume"]["type"]).startswith("gp2") else ""
            signals.append({"key": "overprovisioned", "evidence": (
                f"{infra['instance_type']} ({infra['vcpu']} vCPU, {infra['memory_gib']} GiB): CPU avg "
                f"{util['cpu_avg_pct']}%, p95 {util['cpu_p95_pct']}%, memory {util['mem_avg_pct']}% over "
                f"{util['period_days']} days (source: {util['source']}){gp2}")})
        return signals

    def candidate_findings(self, signals: list[dict] | None = None) -> list[dict[str, str]]:
        """Signals mapped onto the curated rules: id, category, title, merged evidence, recommendation."""
        evidence: dict[str, list[str]] = {}
        for s in self.signals() if signals is None else signals:
            evidence.setdefault(s["key"], []).append(s["evidence"])
        return [{"id": r["id"], "category": r["category"], "title": r["title"],
                 "evidence": "; ".join(dict.fromkeys(evidence[r["key"]])), "recommendation": r["recommendation"]}
                for r in RULES if r["key"] in evidence]

    def facts(self) -> dict[str, Any]:
        """The fact sections of the discovery contract (everything except findings/strategy/score)."""
        code, infra, inst, live = self.code, self.infra, self.infra.get("instance") or {}, self.live
        os_name = inst.get("os", "unknown")
        instance_type = live.get("instance_type") or inst.get("instance_type")
        specs = instance_specs(instance_type or "")
        volume = live.get("root_volume") or inst.get("root_volume") or {}
        infrastructure = {
            "provider": "aws", "region": infra.get("region") or self.region, "instance_type": instance_type,
            "vcpu": specs.get("vcpu"), "memory_gib": specs.get("memory_gib"), "os": os_name,
            "volume": {"type": volume.get("type"), "size_gib": volume.get("size_gib"),
                       "defaulted": bool(volume.get("defaulted"))},
            "process_manager": code["runtime"]["process_manager"], "reverse_proxy": code["runtime"]["reverse_proxy"],
            "scheduled_jobs": code["runtime"]["scheduled_jobs"],
            "source": "live" if live.get("status") == "ok" else infra["source"],
            "architecture": specs.get("arch"), "ami_source": inst.get("ami_source"),
            "ingress": [f"{r['protocol']}/{r['ports']} from {r['cidr']}"
                        for sg in infra.get("security_groups", []) for r in sg["ingress"]],
            "high_availability": infra.get("high_availability"), "backups": infra.get("backups"),
        }
        if live.get("status") == "ok":
            infrastructure |= {"instance_id": live["instance_id"], "availability_zone": live["availability_zone"]}
        application = {**code["application"], "language_version": SYSTEM_PYTHON.get(os_name, ("unknown",))[0]}
        return {"application": application, "components": code["components"], "dependencies": code["dependencies"],
                "infrastructure": infrastructure, "utilization": self.utilization}


def validate_submission(ctx: DiscoveryContext, severities: dict, strategy: str, rationale: str,
                        readiness_score: Any) -> list[str]:
    """Precise, fixable error messages for the model; empty list means valid."""
    ids = {f["id"]: f["title"] for f in ctx.candidate_findings()}
    sev = {str(k).strip().upper(): str(v).strip().lower() for k, v in (severities or {}).items()}
    errors = [f"missing severity for {i} ({t})" for i, t in ids.items() if i not in sev]
    errors += [f"unknown finding id {k}; valid ids: {', '.join(ids)}" for k in sev if k not in ids]
    errors += [f"{k}: severity '{v}' must be one of {', '.join(SEVERITIES)}" for k, v in sev.items() if v not in SEVERITIES]
    if str(strategy).strip().lower() not in STRATEGIES:
        errors.append(f"strategy '{strategy}' must be one of {', '.join(STRATEGIES)}")
    if len(str(rationale or "").strip()) < 20:
        errors.append("strategy_rationale must explain the choice in 1-3 sentences, citing finding ids")
    try:
        if not 0 <= int(readiness_score) <= 100:
            errors.append("readiness_score must be an integer from 0 to 100")
    except (TypeError, ValueError):
        errors.append("readiness_score must be an integer from 0 to 100")
    if REPORT not in ctx.ws.files:
        errors.append(f"write {REPORT} with write_file before submitting")
    return errors


def assemble(ctx: DiscoveryContext, severities: dict, strategy: str, rationale: str, readiness_score: Any,
             target: str = DEFAULT_TARGET, recommendations: dict | None = None,
             extra_findings: list | None = None) -> dict[str, Any]:
    """Merge the model's judgement with the deterministic facts into the contract shape (CONTRACTS.md §1)."""
    sev = {str(k).strip().upper(): str(v).strip().lower() for k, v in (severities or {}).items()}
    recs = {str(k).strip().upper(): v for k, v in (recommendations or {}).items() if v}
    fallback = {r["id"]: r["fallback_severity"] for r in RULES}
    findings = [{**f, "severity": sev.get(f["id"]) if sev.get(f["id"]) in SEVERITIES else fallback[f["id"]],
                 "recommendation": recs.get(f["id"], f["recommendation"])} for f in ctx.candidate_findings()]
    keys = ("id", "severity", "category", "title", "evidence", "recommendation")
    for extra in extra_findings or []:
        if isinstance(extra, dict) and all(extra.get(k) for k in keys) and extra["id"] not in {f["id"] for f in findings}:
            findings.append({k: extra[k] for k in keys})
    findings = [{k: f[k] for k in keys} for f in findings]
    try:
        score = max(0, min(100, int(readiness_score)))
    except (TypeError, ValueError):
        score = fallback_score(findings)
    strategy = str(strategy).strip().lower()
    return {**ctx.facts(), "findings": findings,
            "strategy": {"recommended": strategy if strategy in STRATEGIES else "replatform",
                         "rationale": str(rationale).strip(), "target": target or DEFAULT_TARGET},
            "readiness_score": score}


def fallback_score(findings: list[dict]) -> int:
    """Used only in degraded mode: 100 minus a fixed penalty per severity."""
    penalty = {"critical": 10, "high": 5, "medium": 2, "low": 1}
    return max(0, 100 - sum(penalty[f["severity"]] for f in findings))


def fallback_assessment(ctx: DiscoveryContext) -> dict[str, Any]:
    """Deterministic result when the model did not submit: curated fallback severities, replatform."""
    findings = ctx.candidate_findings()
    rationale = ("Fallback (model did not submit): the app needs targeted code changes and a managed database "
                 "rather than a rewrite, so replatform it to containers on EKS with RDS.")
    data = assemble(ctx, {}, "replatform", rationale, None)
    data["readiness_score"] = fallback_score(data["findings"]) if findings else 80
    return data


def render_appendix(data: dict[str, Any]) -> str:
    """Inventory and findings tables, rendered from data so they always match the submitted result."""
    app, deps, infra, util = data["application"], data["dependencies"], data["infrastructure"], data["utilization"]
    db = deps["database"]
    rows = [
        ("Application", (f"{app['name']}: {app['language']} {app['language_version']}, {app['framework']}, "
                        f"entrypoint {app['entrypoint']}, port {app['listen_port']}, {app['loc']} LOC")),
        ("Components", ", ".join(f"{c['name']} ({c['kind']}: {', '.join(c['routes'])})" for c in data["components"])),
        ("Database", f"{db['engine']} at {db['location']} (tables: {', '.join(db['tables'])})"),
        ("Local state", ", ".join(deps["local_state"]) or "none"),
        ("Packages", ", ".join(deps["python_packages"])),
        ("Host", (f"{infra['instance_type']} ({infra['vcpu']} vCPU, {infra['memory_gib']} GiB), {infra['os']}, "
                 f"{infra['volume']['type']} {infra['volume']['size_gib']} GiB"
                 f"{' (AWS default)' if infra['volume'].get('defaulted') else ''}, {infra['region']} "
                 f"(source: {infra['source']})")),
        ("Runtime", (f"{infra['process_manager']}, {infra['reverse_proxy']} reverse proxy; "
                    f"{'; '.join(infra['scheduled_jobs']) or 'no scheduled jobs'}")),
        ("Network", ", ".join(infra.get("ingress") or []) or "unknown"),
        ("Utilization", (f"CPU avg {util['cpu_avg_pct']}%, p95 {util['cpu_p95_pct']}%, memory {util['mem_avg_pct']}% "
                        f"over {util['period_days']} days (source: {util['source']})")),
    ]
    lines = [APPENDIX_MARKER, "", "## Inventory", "", "| Item | Value |", "|---|---|"]
    lines += [f"| {k} | {_cell(v)} |" for k, v in rows]
    lines += ["", "## Findings", "", "| ID | Severity | Category | Finding | Evidence | Recommendation |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {f['id']} | {f['severity']} | {f['category']} | {_cell(f['title'])} | {_cell(f['evidence'])} | "
              f"{_cell(f['recommendation'])} |" for f in data["findings"]]
    return "\n".join(lines) + "\n"


def finalize_report(ws: Workspace, data: dict[str, Any], summary: str) -> None:
    """Keep the model's narrative, (re)append the generated tables, or write a fallback report."""
    narrative = ws.files.get(REPORT, "").split(APPENDIX_MARKER)[0].rstrip()
    if not narrative:
        s = data["strategy"]
        narrative = (f"# Assessment: {data['application']['name']}\n\n## Summary\n\n{summary}\n\n"
                     f"## Strategy\n\n**{s['recommended']}** to {s['target']}. {s['rationale']}\n\n"
                     f"**Readiness score:** {data['readiness_score']}/100")
    ws.write(REPORT, narrative + "\n\n" + render_appendix(data))


def make_tools(ctx: DiscoveryContext, box: ResultBox) -> list:
    """Strands tools bound to one request."""

    @tool
    def scan_codebase() -> str:
        """Static analysis of legacy/app: framework, routes, port, database, local state, ops files (systemd, cron,
        nginx) and code-level findings with file:line evidence. Call once."""
        code = ctx.code
        keys = {s["key"] for s in code["signals"]}
        return _json({"application": code["application"], "components": code["components"],
                      "dependencies": code["dependencies"], "runtime": code["runtime"],
                      "findings": [f for f in ctx.candidate_findings() if _rule_key(f["id"]) in keys]})

    @tool
    def parse_legacy_terraform() -> str:
        """Parse legacy/terraform/*.tf: instance type, AMI/OS, root volume, security group ingress, tags,
        availability setup, plus infrastructure findings. Call once."""
        infra = ctx.infra
        keys = {s["key"] for s in infra["signals"]}
        facts = {k: v for k, v in infra.items() if k != "signals"}
        return _json({"terraform": facts, "findings": [f for f in ctx.candidate_findings() if _rule_key(f["id"]) in keys]})

    @tool(name="instance_specs")
    def instance_specs_tool(instance_type: str) -> str:
        """Look up vCPU, memory (GiB) and CPU architecture of an EC2 instance type.

        Args:
            instance_type: EC2 instance type, e.g. "t3.large".
        """
        return _json(instance_specs(instance_type))

    @tool
    def describe_live_instance() -> str:
        """Read-only EC2 lookup of the running legacy instance (tag app=legacy-inventory): type, AZ, root volume."""
        return _json(ctx.live)

    @tool
    def get_utilization() -> str:
        """CPU/memory utilization: CloudWatch (14 days, avg + p95 of hourly maxima) when live AWS is enabled,
        otherwise a clearly labelled estimate. Also returns the cost finding when the host is over-provisioned."""
        cost = [f for f in ctx.candidate_findings() if f["category"] == "cost"]
        return _json({"utilization": ctx.utilization, "findings": cost})

    @tool(context=True)
    def submit_result(summary: str, severities: dict[str, str], strategy: str, strategy_rationale: str,
                      readiness_score: int, tool_context: ToolContext, target: str = DEFAULT_TARGET,
                      recommendations: dict[str, str] | None = None, extra_findings: list[dict] | None = None) -> str:
        """Submit the assessment. Call once, after writing reports/assessment.md. Facts are merged in automatically.

        Args:
            summary: 2-4 sentences for the reviewer: strategy, readiness and the most important risks.
            severities: Severity for EVERY finding id returned by the tools, e.g. {"LF-01": "critical", "LF-08": "high"}.
                Allowed: critical, high, medium, low.
            strategy: 6R strategy: rehost, replatform or refactor (also repurchase, retain, retire).
            strategy_rationale: 1-3 sentences explaining the choice, citing finding ids.
            readiness_score: 0-100, how ready the app is to run on the target platform today.
            target: Target platform description.
            recommendations: Optional overrides of the default recommendation, by finding id.
            extra_findings: Optional findings the tools missed (only if the reviewer asks), each with id, severity,
                category, title, evidence, recommendation.
        """
        ctx.submit_attempts += 1
        errors = validate_submission(ctx, severities, strategy, strategy_rationale, readiness_score)
        if errors and ctx.submit_attempts < MAX_SUBMIT_ATTEMPTS:
            return "error: fix these and call submit_result again:\n- " + "\n- ".join(errors)
        data = assemble(ctx, severities, strategy, strategy_rationale, readiness_score, target, recommendations,
                        extra_findings)
        if errors:
            data["validation_warnings"] = errors
        tool_context.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
        return box.set(summary, data)

    tools = [scan_codebase, parse_legacy_terraform, instance_specs_tool, get_utilization, submit_result]
    if ctx.live_aws:
        tools.insert(3, describe_live_instance)
    return tools


def load_legacy_estate(root: Path | None = None) -> dict[str, str]:
    """Read legacy-infra/{app,terraform} from the repo as request files prefixed `legacy/` (local runs, tests)."""
    root = root or Path(__file__).resolve().parents[2] / "legacy-infra"
    skip = (".terraform", "__pycache__", ".tfstate", ".lock.hcl", ".pyc")
    files = {}
    for sub in ("app", "terraform"):
        for path in sorted((root / sub).rglob("*")) if (root / sub).is_dir() else []:
            rel = path.relative_to(root).as_posix()
            if path.is_file() and not any(s in rel for s in skip):
                try:
                    files[f"legacy/{rel}"] = path.read_text()
                except UnicodeDecodeError:
                    continue
    return files


def _rule_key(finding_id: str) -> str:
    return next(r["key"] for r in RULES if r["id"] == finding_id)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _json(value: Any) -> str:
    return json.dumps(value, default=str)
