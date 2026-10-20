"""Requirements tools: parse the uploaded document, keep the spec draft, validate it.

Python parses the document, prefills what it can extract, normalizes types, derives numbers and checks
consistency. The model fills the rest of the draft through small tool calls (set_fields, set_lists):
priorities, acceptance criteria, traces to findings, conflicts and open questions.
"""

import copy
import json
import math
import re
import threading
from pathlib import Path
from typing import Any

from strands import ToolContext, tool

from transform_agents.core import ResultBox, Workspace

REPORT = "reports/migration-spec.md"
MAX_CHECKS = 8  # after this many failed checks the model is told to submit; problems travel with the spec
APPENDIX_MARKER = "<!-- generated: tables rendered from the validated spec -->"
CATEGORY_BY_PREFIX = {"G": "goal", "BR": "goal", "FR": "functional", "NFR": "nfr", "SEC": "security", "TC": "constraint"}
CATEGORIES = ("functional", "nfr", "security", "constraint", "goal")
PRIORITIES = ("must", "should", "could")
REQ_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*(?:\*\*)?(?P<id>(?:G|BR|FR|NFR|SEC|TC)\d+)(?:\*\*)?[.):]?\s+(?P<text>.+)$")
OPEN_HEADING = re.compile(r"open (points|questions|issues)|to be confirmed|tbd|unresolved", re.IGNORECASE)
GROWTH = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\b[^.\n]*\b(traffic|load|requests|growth|users)", re.IGNORECASE)

# Every scalar field of the spec: (type, doc). Types: str, int, num, bool.
FIELDS: dict[str, tuple[str, str]] = {
    "service.name": ("str", "name of the NEW microservice (prefilled by the platform naming rule; keep it)"),
    "service.description": ("str", "one sentence: what the service does and for whom"),
    "target.platform": ("str", "container platform: eks"),
    "target.region": ("str", "AWS region, e.g. us-west-2"),
    "target.kubernetes_version": ("str", 'EKS version as a string, e.g. "1.33"'),
    "target.cpu_architecture": ("str", "arm64 (Graviton) or amd64"),
    "target.iac": ("str", "infrastructure as code tool: terraform"),
    "target.registry": ("str", "container registry: ecr"),
    "workload.container_port": ("int", "port of the NEW container: platform standard 8080, not the legacy port"),
    "workload.health_path": ("str", "liveness endpoint (platform standard: /healthz)"),
    "workload.readiness_path": ("str", "readiness endpoint (platform standard: /readyz)"),
    "workload.replicas_min": ("int", "minimum replicas; at least 2 when the SLO is 99.9% or higher"),
    "workload.replicas_max": ("int", "maximum replicas; leave null to derive replicas_min x growth factor"),
    "workload.autoscaling_metric": ("str", "cpu, memory or rps"),
    "workload.autoscaling_target_pct": ("int", "autoscaler target utilization percent (platform standard: 70)"),
    "workload.peak_rps": ("num", "expected peak requests per second (from the document)"),
    "workload.p95_latency_ms": ("num", "p95 latency target in milliseconds (from the document)"),
    "data.engine": ("str", "target database engine: postgres or mysql"),
    "data.version": ("str", 'major engine version as a string (platform default for postgres: "16")'),
    "data.managed_service": ("str", "rds or aurora"),
    "data.migrate_from": ("str", "current database engine (from the assessment), e.g. sqlite"),
    "data.multi_az": ("bool", "true when the database must survive the loss of an Availability Zone"),
    "nfr.availability_slo_pct": ("num", "monthly availability SLO in percent, e.g. 99.9"),
    "nfr.rpo_minutes": ("num", "recovery point objective in minutes"),
    "nfr.rto_minutes": ("num", "recovery time objective in minutes (1 hour = 60)"),
    "nfr.logging": ("str", "json-stdout when logs must be structured JSON on stdout"),
    "security.secrets": ("str", "aws-secrets-manager or kubernetes-secrets"),
    "security.encryption_at_rest": ("bool", "data encrypted at rest"),
    "security.private_database": ("bool", "true when the database must not be reachable from the internet"),
    "security.non_root": ("bool", "containers run as a non-root user"),
    "security.read_only_root_fs": ("bool", "containers use a read-only root filesystem"),
    "finops.monthly_budget_usd": ("num", "monthly production budget in USD (from the document)"),
    "finops.prefer_graviton": ("bool", "prefer Graviton (arm64) instances"),
    "finops.environment": ("str", "environment this spec sizes: prod"),
    "finops.growth_factor_next_year": ("num", "expected traffic multiplier next year (prefilled from the document)"),
}
# Platform standards; every other field starts as null and must come from the document or the assessment.
DEFAULTS = {"target.kubernetes_version": "1.33", "data.version": "16", "workload.container_port": 8080,
            "workload.health_path": "/healthz", "workload.readiness_path": "/readyz",
            "workload.autoscaling_target_pct": 70, "finops.environment": "prod"}
ENUMS = {"target.platform": ("eks",), "target.cpu_architecture": ("arm64", "amd64"),
         "workload.autoscaling_metric": ("cpu", "memory", "rps"), "data.engine": ("postgres", "mysql"),
         "data.managed_service": ("rds", "aurora")}
LIST_DOCS = {
    "requirements": 'one entry per document requirement: {"id": "NFR2", "priority": "must|should|could", '
                    '"acceptance": "one measurable sentence", "traces_to": ["LF-xx"]}. text and category are '
                    "filled in from the document automatically.",
    "conflicts": '{"description": "which requirements pull against each other and why", "resolution": "decision"}',
    "open_questions": '{"id": "Q-01", "question": "...", "assumption": "what we assume until answered", '
                      '"impact": "what changes if the assumption is wrong"}: one per open point in the document',
    "gaps": '{"finding_id": "LF-xx", "handling": "the concrete action the migration takes"}: critical/high '
            "assessment findings that no requirement traces",
}
SYNONYMS = {"x86_64": "amd64", "x86": "amd64", "aarch64": "arm64", "graviton": "arm64", "postgresql": "postgres",
            "aurora-postgresql": "postgres", "amazon rds": "rds", "rds-postgres": "rds", "amazon eks": "eks",
            "non-functional": "nfr", "non_functional": "nfr", "business goal": "goal", "goals": "goal",
            "technical constraint": "constraint", "constraints": "constraint", "func": "functional"}


# ---- document parsing ----

def parse_requirements(text: str) -> dict[str, list]:
    """Requirement ids (G1, FR1, NFR1, SEC1, TC1...) with text and category, plus the open points."""
    items, open_points, section = [], [], ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            section = line.strip("# ").strip()
            continue
        clean = line.replace("**", "").replace("`", "")
        if m := REQ_LINE.match(line.replace("**", "")):
            prefix = re.match(r"[A-Z]+", m.group("id")).group(0)
            items.append({"id": m.group("id"), "category": CATEGORY_BY_PREFIX.get(prefix, "constraint"),
                          "text": m.group("text").replace("`", "").strip(), "section": section})
        elif OPEN_HEADING.search(section) and re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", clean):
            open_points.append(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", clean).strip())
    return {"requirements": items, "open_points": open_points}


def spec_template(assessment: dict | None = None, growth: float | None = None) -> dict[str, Any]:
    """Nested spec skeleton: platform defaults and facts Python can extract filled in, everything else null."""
    spec: dict[str, Any] = {}
    for path in FIELDS:
        section, key = path.split(".")
        spec.setdefault(section, {})[key] = DEFAULTS.get(path)
    app = (assessment or {}).get("application", {})
    if app.get("name"):
        spec["service"]["name"] = app["name"].removeprefix("legacy-").removesuffix("-service") + "-service"
    engine = (assessment or {}).get("dependencies", {}).get("database", {}).get("engine")
    if engine and engine != "none":
        spec["data"]["migrate_from"] = engine
    spec["finops"]["growth_factor_next_year"] = growth or 1
    return spec | {"requirements": [], "conflicts": [], "open_questions": [], "gaps": []}


def canonical_id(value: Any) -> str:
    """'g1.' / 'G1 (goal)' / '**NFR2**' -> 'G1' / 'G1' / 'NFR2'."""
    text = str(value or "").strip().upper()
    m = re.search(r"\b([A-Z]{1,4}\d+)\b", text)
    return m.group(1) if m else text


def mentioned_growth(texts: list[str]) -> float | None:
    """Traffic multiplier the document mentions, e.g. "maybe 3x more traffic next year" -> 3.0."""
    return next((float(m.group(1)) for t in texts if (m := GROWTH.search(t))), None)


# ---- normalization and validation ----

def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Forgive harmless slips ('USD 450', '99.9%', 16 vs '16', x86_64, nesting) so the model fixes real problems only."""
    spec = copy.deepcopy(spec) if isinstance(spec, dict) else {}
    sections = list(dict.fromkeys(p.split(".")[0] for p in FIELDS))
    for name in sections:  # hoist sections the model nested one level too deep, e.g. service.target
        for parent in list(spec.values()):
            if name not in spec and isinstance(parent, dict) and isinstance(parent.get(name), dict):
                spec[name] = parent.pop(name)
    for path, (kind, _) in FIELDS.items():
        section, key = path.split(".")
        if isinstance(spec.get(section), dict) and spec[section].get(key) is not None:
            spec[section][key] = _coerce(spec[section][key], kind)
            if isinstance(spec[section][key], str) and kind == "str" and path in ENUMS:
                value = spec[section][key].strip().lower()
                spec[section][key] = SYNONYMS.get(value, value)
    for req in spec.get("requirements") or []:
        if isinstance(req, dict):
            req["id"] = canonical_id(req.get("id"))
            for key in ("priority", "category"):
                if isinstance(req.get(key), str):
                    value = req[key].strip().lower()
                    req[key] = SYNONYMS.get(value, value)
            if isinstance(req.get("traces_to"), str):
                req["traces_to"] = [t.strip() for t in req["traces_to"].split(",") if t.strip()]
            req["traces_to"] = [str(t).strip().upper() for t in req.get("traces_to") or []]
    ordered = {}  # template order keeps the JSON readable for reviewers and later stages
    for name in [*sections, "requirements", "conflicts", "open_questions", "gaps", *spec]:
        if name in spec and name not in ordered:
            value = spec[name]
            keys = [p.split(".")[1] for p in FIELDS if p.startswith(name + ".")]
            ordered[name] = {k: value[k] for k in [*keys, *value] if k in value} if isinstance(value, dict) else value
    return ordered


def complete_requirements(spec: dict[str, Any], doc_reqs: list[dict]) -> None:
    """Fill each requirement's text and category from the document when the model left them out."""
    by_id = {r["id"]: r for r in doc_reqs}
    reqs = [r for r in spec.get("requirements") or [] if isinstance(r, dict)]
    for req in reqs:
        if doc := by_id.get(req.get("id")):
            req["text"] = req.get("text") or doc["text"]
            req["category"] = req.get("category") if req.get("category") in CATEGORIES else doc["category"]
    order = {r["id"]: i for i, r in enumerate(doc_reqs)}
    reqs.sort(key=lambda r: order.get(r.get("id"), len(order)))
    keys = ("id", "text", "category", "priority", "traces_to", "acceptance")  # contract order first
    spec["requirements"] = [{k: r.get(k) for k in keys} | {k: v for k, v in r.items() if k not in keys} for r in reqs]


def check_spec(spec: dict[str, Any], doc_reqs: list[dict], open_points: list[str],
               assessment: dict | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block submission; each one says how to fix it."""
    errors, warnings = [], []
    for path, (kind, doc) in FIELDS.items():
        value = _get(spec, path)
        if value is None:
            errors.append(f"{path} is missing: {doc}")
        elif not _is_type(value, kind):
            errors.append(f"{path} must be a {kind}, got {value!r}")
        elif path in ENUMS and value not in ENUMS[path]:
            errors.append(f"{path} must be one of {', '.join(ENUMS[path])}, got {value!r}")
    num = {p: _get(spec, p) for p, (k, _) in FIELDS.items() if k in ("int", "num") and _is_type(_get(spec, p), k)}
    slo, rmin, rmax = num.get("nfr.availability_slo_pct"), num.get("workload.replicas_min"), num.get("workload.replicas_max")
    if slo is not None and rmin is not None and slo >= 99.9 and rmin < 2:
        errors.append(f"workload.replicas_min must be >= 2 because the SLO is {slo}% (one replica cannot survive a node loss)")
    if rmin is not None and rmax is not None and rmax < rmin:
        errors.append("workload.replicas_max must be >= workload.replicas_min")
    for path in ("finops.monthly_budget_usd", "workload.peak_rps", "workload.p95_latency_ms"):
        if path in num and num[path] <= 0:
            errors.append(f"{path} must be greater than 0")
    region = _get(spec, "target.region")
    if isinstance(region, str) and not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-\d", region):
        errors.append(f"target.region {region!r} is not an AWS region code like us-west-2")

    findings = {f["id"]: f for f in (assessment or {}).get("findings", []) if isinstance(f, dict) and "id" in f}
    reqs = spec.get("requirements")
    if not isinstance(reqs, list) or not reqs:
        errors.append("requirements must be a non-empty list: " + LIST_DOCS["requirements"])
        reqs = []
    seen: set[str] = set()
    for i, req in enumerate(reqs):
        if not isinstance(req, dict):
            errors.append(f"requirements[{i}] must be an object")
            continue
        rid = req.get("id") or f"requirements[{i}]"
        if rid in seen:
            errors.append(f"requirement id {rid} appears more than once; keep one entry per id")
        seen.add(rid)
        if req.get("priority") not in PRIORITIES:
            errors.append(f"{rid}: priority must be one of {', '.join(PRIORITIES)}")
        if req.get("category") not in CATEGORIES:
            errors.append(f"{rid}: category must be one of {', '.join(CATEGORIES)}")
        if not str(req.get("text") or "").strip():
            errors.append(f"{rid}: text is missing (only needed for ids that are not in the document)")
        if len(str(req.get("acceptance") or "").strip()) < 10:
            errors.append(f"{rid}: acceptance must be one measurable sentence")
        unknown = [t for t in req.get("traces_to") or [] if findings and t not in findings]
        if unknown:
            errors.append(f"{rid}: traces_to has unknown finding ids {unknown}; valid ids: {', '.join(findings)}")
    missing = [r["id"] for r in doc_reqs if r["id"] not in seen]
    if missing:
        errors.append(f"requirements is missing {', '.join(missing)}: every requirement in the document needs an entry")

    gaps = [g for g in spec.get("gaps") or [] if isinstance(g, dict)]
    for g in gaps:
        if findings and g.get("finding_id") not in findings:
            errors.append(f"gaps: unknown finding_id {g.get('finding_id')!r}")
        if not str(g.get("handling") or "").strip():
            errors.append(f"gaps: {g.get('finding_id')} needs a 'handling' sentence")
    traced = {t for r in reqs if isinstance(r, dict) for t in r.get("traces_to") or []}
    covered = traced | {g.get("finding_id") for g in gaps}
    for fid, f in findings.items():
        if f.get("severity") in ("critical", "high") and fid not in covered:
            errors.append(f"{fid} ({f['severity']}: {f.get('title')}) is not traced by any requirement: add it to "
                          "traces_to of the requirement that addresses it, or list it in gaps with its handling")

    questions = spec.get("open_questions")
    if not isinstance(questions, list):
        errors.append("open_questions must be a list: " + LIST_DOCS["open_questions"])
        questions = []
    for i, q in enumerate(questions):
        for key in ("id", "question", "assumption", "impact"):
            if not isinstance(q, dict) or not str(q.get(key) or "").strip():
                errors.append(f"open_questions[{i}] needs a non-empty {key}")
    qids = [q.get("id") for q in questions if isinstance(q, dict)]
    if len(qids) != len(set(qids)):
        errors.append("open_questions ids must be unique")
    if len(questions) < len(open_points):
        errors.append(f"the document lists {len(open_points)} open points but open_questions has {len(questions)}: "
                      f"add one per open point: {open_points}")
    for i, c in enumerate(spec.get("conflicts") or []):
        if not isinstance(c, dict) or not c.get("description") or not c.get("resolution"):
            errors.append(f"conflicts[{i}] needs a description and a resolution")

    growth = num.get("finops.growth_factor_next_year")
    doc_growth = mentioned_growth(open_points + [r["text"] for r in doc_reqs])
    if doc_growth and growth is not None and growth != doc_growth:
        errors.append(f"the document mentions {doc_growth:g}x traffic growth: set finops.growth_factor_next_year "
                      f"to {doc_growth:g} (whether to size for it now belongs in open_questions)")
    if rmin and rmax and growth and growth > 1 and rmax < math.ceil(rmin * growth):
        errors.append(f"workload.replicas_max={rmax} leaves no headroom for the x{growth:g} growth: size replicas_min "
                      f"for today and set replicas_max to at least replicas_min x growth = {math.ceil(rmin * growth)}")
    legacy_name = (assessment or {}).get("application", {}).get("name")
    if legacy_name and _get(spec, "service.name") == legacy_name:
        errors.append(f"service.name must name the new microservice, not the legacy app {legacy_name!r} "
                      f"(platform naming rule: {spec_template(assessment)['service']['name']})")
    if slo is not None and slo >= 99.9 and _get(spec, "data.multi_az") is False:
        warnings.append("an SLO of 99.9% or more usually needs data.multi_az = true")
    if not spec.get("conflicts"):
        warnings.append("no conflicts recorded: check cost cap vs. high availability and growth")
    return errors, warnings


# ---- report ----

def render_appendix(spec: dict[str, Any], assessment: dict | None = None) -> str:
    """Spec summary, traceability matrix, finding coverage, conflicts and open questions as markdown tables.

    Tolerates partial or malformed drafts (a last-chance submit keeps whatever the model sent): missing values
    render as "?", list entries that are not objects are shown as text or skipped, so rendering never raises."""
    t, w, d, n, s, f = (_section(spec, k) for k in ("target", "workload", "data", "nfr", "security", "finops"))

    def v(section: dict, key: str) -> Any:
        value = section.get(key)
        return "?" if value is None else value

    def yes(section: dict, key: str) -> str:
        return {True: "yes", False: "no"}.get(section.get(key), "?")

    rows = [
        ("Target", (f"{v(t, 'platform')} in {v(t, 'region')}, Kubernetes {v(t, 'kubernetes_version')}, "
                    f"{v(t, 'cpu_architecture')}, IaC {v(t, 'iac')}, registry {v(t, 'registry')}")),
        ("Workload", (f"port {v(w, 'container_port')}, probes {v(w, 'health_path')} and {v(w, 'readiness_path')}, "
                      f"replicas {v(w, 'replicas_min')}-{v(w, 'replicas_max')} on {v(w, 'autoscaling_metric')} "
                      f"{v(w, 'autoscaling_target_pct')}%, peak {v(w, 'peak_rps')} rps, "
                      f"p95 {v(w, 'p95_latency_ms')} ms")),
        ("Data", (f"{v(d, 'engine')} {v(d, 'version')} on {v(d, 'managed_service')}, Multi-AZ {yes(d, 'multi_az')}, "
                  f"migrate from {v(d, 'migrate_from')}")),
        ("NFR", (f"SLO {v(n, 'availability_slo_pct')}%, RPO {v(n, 'rpo_minutes')} min, RTO {v(n, 'rto_minutes')} min, "
                 f"logging {v(n, 'logging')}")),
        ("Security", (f"secrets {v(s, 'secrets')}, encryption at rest {yes(s, 'encryption_at_rest')}, private DB "
                      f"{yes(s, 'private_database')}, non-root {yes(s, 'non_root')}, read-only root FS "
                      f"{yes(s, 'read_only_root_fs')}")),
        ("FinOps", (f"USD {v(f, 'monthly_budget_usd')}/month ({v(f, 'environment')}), prefer Graviton "
                    f"{yes(f, 'prefer_graviton')}, growth x{v(f, 'growth_factor_next_year')} next year")),
    ]
    reqs = _entries(spec.get("requirements"), "text")
    out = [APPENDIX_MARKER, "", "## Target specification", "", "| Area | Values |", "|---|---|"]
    out += [f"| {k} | {_cell(val)} |" for k, val in rows]
    out += ["", "## Traceability matrix", "", "| ID | Category | Priority | Requirement | Traces to | Acceptance |",
            "|---|---|---|---|---|---|"]
    out += [f"| {_cell(r.get('id', '-'))} | {_cell(r.get('category') or '-')} | {_cell(r.get('priority') or '-')} | "
            f"{_cell(r.get('text', ''))} | {_cell(', '.join(map(str, _list(r.get('traces_to')))) or '-')} | "
            f"{_cell(r.get('acceptance') or '-')} |" for r in reqs]
    findings = [x for x in _list((assessment or {}).get("findings"))
                if isinstance(x, dict) and x.get("severity") in ("critical", "high")]
    if findings:
        gaps = _entries(spec.get("gaps"), "handling")
        out += ["", "## Assessment coverage (critical and high findings)", "",
                "| Finding | Severity | Title | Covered by |", "|---|---|---|---|"]
        for x in findings:
            covered_by = [str(r.get("id", "?")) for r in reqs if x.get("id") in _list(r.get("traces_to"))]
            gap = next((g.get("handling") for g in gaps if g.get("finding_id") == x.get("id")), None)
            covered = ", ".join(covered_by) or (f"gap: {gap}" if gap else "NOT COVERED")
            out.append(f"| {_cell(x.get('id', '?'))} | {x['severity']} | {_cell(x.get('title', ''))} | {_cell(covered)} |")
    if conflicts := _entries(spec.get("conflicts"), "description"):
        out += ["", "## Conflicts", "", "| Conflict | Resolution |", "|---|---|"]
        out += [f"| {_cell(c.get('description', ''))} | {_cell(c.get('resolution') or '-')} |" for c in conflicts]
    if questions := _entries(spec.get("open_questions"), "question"):
        out += ["", "## Open questions", "", "| ID | Question | Status | Assumption or decision | Impact |",
                "|---|---|---|---|---|"]
        for q in questions:
            resolved = q.get("status") == "resolved"
            answer = f"Decided: {q.get('resolution')}" if resolved else (q.get("assumption") or "-")
            out.append(f"| {_cell(q.get('id', '-'))} | {_cell(q.get('question', ''))} | "
                       f"{'Resolved by reviewer' if resolved else 'Open'} | {_cell(answer)} | "
                       f"{_cell(q.get('impact') or '-')} |")
    return "\n".join(out) + "\n"


def narrative_report(ws: Workspace, spec: dict[str, Any], summary: str) -> str:
    """The model's narrative without generated tables, or a minimal one built from the summary."""
    narrative = ws.files.get(REPORT, "").split(APPENDIX_MARKER)[0].rstrip()
    name = _get(spec, "service.name") if isinstance(spec, dict) else None
    return narrative or f"# Migration spec: {name or 'service'}\n\n## Summary\n\n{summary}"


def finalize_report(ws: Workspace, spec: dict[str, Any], summary: str, assessment: dict | None) -> None:
    """Keep the model's narrative, (re)append the generated tables, or write a minimal narrative."""
    ws.write(REPORT, narrative_report(ws, spec, summary) + "\n\n" + render_appendix(spec, assessment))


def assessment_digest(assessment: dict | None) -> str:
    """Compact view of the discovery result for the prompt."""
    if not assessment:
        return "No assessment was provided: leave traces_to empty and gaps empty."
    app, db = assessment.get("application", {}), assessment.get("dependencies", {}).get("database", {})
    strategy = assessment.get("strategy", {})
    endpoints = [e.split(" -> ")[0] for c in assessment.get("components", []) for e in c.get("endpoints", [])]
    lines = [(f"- application: {app.get('name')}, {app.get('framework')} on {app.get('language')} "
              f"{app.get('language_version')}, port {app.get('listen_port')}, database {db.get('engine')} "
              f"(tables: {', '.join(db.get('tables', []))})"),
             f"- legacy endpoints: {', '.join(endpoints) or 'unknown'}",
             f"- strategy: {strategy.get('recommended')} -> {strategy.get('target')}",
             "- findings (id severity category: title):"]
    lines += [f"  {f['id']} {f.get('severity')} {f.get('category')}: {f.get('title')}"
              for f in assessment.get("findings", []) if isinstance(f, dict)]
    return "\n".join(lines)


def load_sample_requirements() -> dict[str, str]:
    """Local default: samples/requirements/* as request files under requirements/."""
    root = Path(__file__).resolve().parents[2] / "samples" / "requirements"
    return {f"requirements/{p.name}": p.read_text() for p in sorted(root.glob("*")) if p.suffix in (".md", ".txt")}


# ---- tools ----

class RequirementsContext:
    """Per-request state: the parsed documents and the spec draft the model fills in through tools.

    The draft lives in Python, so the model sends small updates (a few fields, a few list entries) instead of
    re-sending one large JSON document; large nested tool inputs are where Nova's tool calls get malformed.
    """

    def __init__(self, ws: Workspace, assessment: dict | None = None, previous: dict | None = None):
        # Guards the draft: derive() replaces it with a normalized copy, so a concurrent upsert() could be lost.
        # Tools run sequentially today (core.runner); the lock keeps that safe if parallel tool calls come back.
        self.lock = threading.RLock()
        self.ws, self.assessment = ws, assessment or {}
        self.docs = {p: ws.files[p] for p in ws.listing("requirements/")}
        parsed = [parse_requirements(text) for text in self.docs.values()]
        self.doc_reqs = [r for p in parsed for r in p["requirements"]]
        self.open_points = [o for p in parsed for o in p["open_points"]]
        self.growth = mentioned_growth(self.open_points + [r["text"] for r in self.doc_reqs])
        self.draft = normalize_spec(previous) if previous else spec_template(assessment, self.growth)
        self.checks = 0
        self.submits = 0

    def set_fields(self, fields: dict) -> tuple[int, list[str]]:
        """Apply {"section.field": value}; nested {"section": {...}} is flattened. Returns (applied, unknown)."""
        flat = {}
        for key, value in (fields or {}).items():
            if "." not in key and isinstance(value, dict):
                flat |= {f"{key}.{k}": v for k, v in value.items()}
            else:
                flat[key] = value
        unknown = [k for k in flat if k not in FIELDS]
        with self.lock:
            for path, value in flat.items():
                if path in FIELDS:
                    section, key = path.split(".")
                    self.draft.setdefault(section, {})[key] = value
            self.derive()
        return len(flat) - len(unknown), unknown

    def derive(self) -> None:
        """Fill replicas_max = ceil(replicas_min x growth) when left null: arithmetic stays in Python."""
        with self.lock:
            self.draft = normalize_spec(self.draft)
            w, growth = self.draft.get("workload", {}), self.draft.get("finops", {}).get("growth_factor_next_year")
            if (w.get("replicas_max") is None and isinstance(w.get("replicas_min"), int)
                    and isinstance(growth, int | float)):
                w["replicas_max"] = math.ceil(w["replicas_min"] * max(growth, 1))

    def upsert(self, name: str, items: list, key: str) -> None:
        """Merge entries by id (partial updates allowed); {"id": ..., "remove": true} deletes one."""
        with self.lock:
            current = {str(x.get(key)): x for x in self.draft.get(name) or [] if isinstance(x, dict)}
            for x in list(current.values()) if name == "requirements" else []:  # re-key legacy/odd ids
                current[canonical_id(x.get(key))] = current.pop(str(x.get(key))) | {key: canonical_id(x.get(key))}
            for item in items:
                if isinstance(item, str):
                    item = json.loads(item)
                item_id = canonical_id(item.get(key)) if name == "requirements" else str(item.get(key))
                if item.get("remove"):
                    current.pop(item_id, None)
                else:
                    current[item_id] = {**current.get(item_id, {}), **item, key: item_id}
            self.draft[name] = list(current.values())

    def set_lists(self, requirements: list, conflicts: list | None, open_questions: list, gaps: list) -> dict:
        """Apply one set_lists call atomically; returns the list sizes."""
        with self.lock:
            self.upsert("requirements", requirements, "id")
            self.upsert("open_questions", open_questions, "id")
            self.upsert("gaps", gaps, "finding_id")
            if conflicts is not None:
                self.draft["conflicts"] = conflicts
            return {k: len(_list(self.draft.get(k))) for k in ("requirements", "conflicts", "open_questions", "gaps")}

    def resolve_question(self, question_id: str, decision: str) -> bool:
        """Mark an open question as answered by the reviewer; it stays in the list (one entry per open point)."""
        with self.lock:
            for q in _list(self.draft.get("open_questions")):
                if isinstance(q, dict) and str(q.get("id")) == str(question_id):
                    q.update(status="resolved", resolution=decision.strip())
                    return True
            return False

    def snapshot(self) -> dict[str, Any]:
        """A deep copy of the draft, taken under the lock."""
        with self.lock:
            return copy.deepcopy(self.draft)

    def validate(self) -> tuple[list[str], list[str]]:
        with self.lock:
            self.derive()
            complete_requirements(self.draft, self.doc_reqs)
            return check_spec(self.draft, self.doc_reqs, self.open_points, self.assessment)


def make_tools(ctx: RequirementsContext, box: ResultBox) -> list:
    """Strands tools bound to one request."""

    @tool
    def read_requirements() -> str:
        """Read the uploaded requirement documents. Returns their full text, the requirement ids found in them
        and the open points that must become open_questions."""
        return _json({"documents": list(ctx.docs), "content": "\n\n".join(ctx.docs.values()),
                      "requirement_ids": [r["id"] for r in ctx.doc_reqs],
                      "open_points": ctx.open_points})

    @tool
    def get_spec_template() -> str:
        """Return the current spec draft (platform defaults filled, null = still to fill from the document),
        the meaning of every field, the list formats and the rules check_spec enforces."""
        return _json({"draft": ctx.snapshot(), "field_docs": {k: d for k, (_, d) in FIELDS.items()},
                      "list_formats": LIST_DOCS,
                      "rules": ["every requirement id in the document has exactly one entry in requirements",
                                "traces_to only uses assessment finding ids",
                                "every critical/high finding is traced by a requirement or listed in gaps",
                                "one open_questions entry per open point",
                                "replicas_min >= 2 when the SLO >= 99.9; replicas_max >= replicas_min",
                                "numbers come from the document; never invent them"]})

    @tool
    def set_fields(fields: dict) -> str:
        """Set scalar fields of the spec draft.

        Args:
            fields: Flat map of "section.field" to value, e.g. {"target.region": "us-west-2",
                "workload.peak_rps": 40, "data.multi_az": true}.
        """
        with ctx.lock:
            had_max = _get(ctx.draft, "workload.replicas_max") is not None
            applied, unknown = ctx.set_fields(fields)
            draft = ctx.snapshot()
        derived = _get(draft, "workload.replicas_max")
        note = f" workload.replicas_max = {derived} (replicas_min x growth factor)." if derived and not had_max else ""
        still_null = [p for p in FIELDS if _get(draft, p) is None]
        return (f"set {applied} fields.{note}" + (f" Unknown fields (ignored): {unknown}." if unknown else "")
                + (f" Still null: {', '.join(still_null)}." if still_null else " All scalar fields are filled."))

    @tool
    def set_lists(requirements: list[dict] | None = None, conflicts: list[dict] | None = None,
                  open_questions: list[dict] | None = None, gaps: list[dict] | None = None) -> str:
        """Add or update list entries of the spec draft. Entries merge by id (gaps by finding_id), so you can send
        only the entries or fields that change; {"id": "Q-04", "remove": true} deletes an entry.

        Args:
            requirements: [{"id": "NFR2", "priority": "must", "acceptance": "...", "traces_to": ["LF-11"]}]
            conflicts: [{"description": "...", "resolution": "..."}]; replaces the whole conflicts list.
            open_questions: [{"id": "Q-01", "question": "...", "assumption": "...", "impact": "..."}]
            gaps: [{"finding_id": "LF-03", "handling": "..."}]
        """
        try:
            n = ctx.set_lists(requirements or [], conflicts, open_questions or [], gaps or [])
        except (AttributeError, TypeError, json.JSONDecodeError):
            return "error: every list entry must be a JSON object"
        return (f"draft: {n['requirements']} requirements, {n['conflicts']} conflicts, "
                f"{n['open_questions']} open questions, {n['gaps']} gaps")

    @tool
    def resolve_question(question_id: str, decision: str) -> str:
        """Record the reviewer's answer to an open question (use it in revisions when the feedback answers one).

        Args:
            question_id: The open question id, e.g. "Q-02".
            decision: The decision, in the reviewer's words, e.g. "Dev/test is out of scope for now".
        """
        if not decision.strip():
            return "error: decision must not be empty"
        if ctx.resolve_question(question_id, decision):
            still_open = [f'{q.get("id")}: "{q.get("question")}"' for q in _list(ctx.snapshot().get("open_questions"))
                          if isinstance(q, dict) and q.get("status") != "resolved"]
            if not still_open:
                return f"{question_id} resolved: {decision.strip()}. No open questions left."
            return (f"{question_id} resolved: {decision.strip()}.\nStill open:\n- " + "\n- ".join(still_open) +
                    "\nDoes the feedback also answer or decide any of these? If so, resolve them too.")
        ids = [q.get("id") for q in _list(ctx.snapshot().get("open_questions")) if isinstance(q, dict)]
        return f"error: no open question {question_id!r}; ids are {ids}"

    @tool(name="check_spec")
    def check_spec_tool() -> str:
        """Validate the spec draft. Returns OK or the exact problems to fix with set_fields / set_lists."""
        ctx.checks += 1
        errors, warnings = ctx.validate()
        draft = ctx.snapshot()
        if errors and ctx.checks >= MAX_CHECKS:
            return (f"{len(errors)} problem(s) remain after {ctx.checks} checks: stop fixing, write {REPORT} and call "
                    "submit_result(summary); the remaining problems are recorded with the spec.")
        if errors:
            return f"{len(errors)} problem(s); fix them, then call check_spec again:\n- " + "\n- ".join(errors + warnings)
        tail = ("\nWarnings (fix if you agree):\n- " + "\n- ".join(warnings)) if warnings else ""
        return (f"OK: {len(draft['requirements'])} requirements, {len(draft['open_questions'])} open "
                f"questions.{tail}\nNext: write {REPORT}, then call submit_result(summary).")

    @tool(context=True)
    def submit_result(summary: str, tool_context: ToolContext) -> str:
        """Submit the spec draft as the result. Call once, after check_spec returned OK and the report is written.

        Args:
            summary: 2-4 sentences for the reviewer: target, key numbers, and open questions that need an answer.
        """
        ctx.submits += 1
        errors, _ = ctx.validate()
        last_chance = ctx.submits >= 3 or ctx.checks >= MAX_CHECKS
        if errors and not last_chance:
            return "error: the draft is not valid yet; fix these and call check_spec:\n- " + "\n- ".join(errors)
        if REPORT not in ctx.ws.files and not last_chance:
            return f"error: write {REPORT} with write_file first, then call submit_result again"
        spec = ctx.snapshot()
        if errors:  # accepted after repeated attempts so the pipeline keeps moving; reviewers see the problems
            spec["validation_problems"] = errors
        tool_context.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
        return box.set(summary, spec)

    return [read_requirements, get_spec_template, set_fields, set_lists, resolve_question, check_spec_tool, submit_result]


# ---- helpers ----

def _get(spec: dict, path: str) -> Any:
    section, key = path.split(".")
    value = spec.get(section)
    return value.get(key) if isinstance(value, dict) else None


def _is_type(value: Any, kind: str) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "num":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str) and bool(value.strip())


def _coerce(value: Any, kind: str) -> Any:
    if kind == "str" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if kind == "bool" and isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
        return value.strip().lower() in ("true", "yes")
    if kind in ("int", "num") and isinstance(value, str) and (m := re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))):
        value = float(m.group(0))
    if kind in ("int", "num") and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _section(spec: Any, name: str) -> dict:
    value = spec.get(name) if isinstance(spec, dict) else None
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else [] if value is None else [value]


def _entries(value: Any, text_key: str) -> list[dict]:
    """List entries as dicts: a bare string becomes {text_key: string}; anything else is dropped."""
    return [x if isinstance(x, dict) else {text_key: x} for x in _list(value) if isinstance(x, dict | str) and x]


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _json(value: Any) -> str:
    return json.dumps(value, default=str)
