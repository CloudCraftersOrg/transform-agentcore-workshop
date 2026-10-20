"""Deterministic half of the modernization agent: spec -> parameters, pattern rendering, schema conversion.

The LLM never writes platform code. It calls `scaffold_from_pattern`, which uses these functions, and then
ports only the domain routes and tests.
"""

import json
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from transform_agents.core import Workspace

KNOWLEDGE = Path(__file__).parent / "knowledge"
PATTERN_DIR = KNOWLEDGE / "pattern"
EXAMPLE_DIR = KNOWLEDGE / "example"

# Files the LLM owns (kept on re-scaffold); everything else in the pattern is the locked platform layer.
DOMAIN_FILES = (
    "microservice/app/api.py",
    "microservice/app/models.py",
    "microservice/app/report.py",
    "microservice/app/seed.sql",
    "microservice/tests/test_api.py",
)
LOCKED_FILES = tuple(
    p.relative_to(PATTERN_DIR).as_posix()
    for p in sorted(PATTERN_DIR.rglob("*"))
    if p.is_file()
    and p.suffix in {".py", ""}
    and "__pycache__" not in p.parts
    and p.relative_to(PATTERN_DIR).as_posix() not in DOMAIN_FILES
)
_DNS = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_QUANTITY = re.compile(r"^\d+(\.\d+)?(m|Mi|Gi|Ki)?$")
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass
class Params:
    """Values substituted into the pattern (defaults match docs/CONTRACTS.md)."""

    service_name: str = "inventory-service"
    namespace: str = "inventory"
    image: str = "inventory-service"
    image_tag: str = "1.0.0"
    local_tag: str = "dev"
    port: int = 8080
    service_port: int = 80
    health_path: str = "/healthz"
    readiness_path: str = "/readyz"
    replicas_min: int = 2
    replicas_max: int = 6
    cpu_target_pct: int = 70
    cpu_request: str = "100m"
    memory_request: str = "128Mi"
    cpu_limit: str = "500m"
    memory_limit: str = "256Mi"
    report_schedule: str = "0 2 * * *"

    @classmethod
    def from_inputs(cls, spec: dict[str, Any], assessment: dict[str, Any], ws: Workspace) -> "Params":
        """Derive parameters from the migration spec, the assessment and the legacy cron file."""
        p = cls()
        service = (spec.get("service") or {}).get("name") or _service_from_app(assessment.get("application") or {})
        if service:
            p.service_name = p.image = _slug(service)
            p.namespace = _slug(p.service_name.removesuffix("-service")) or p.service_name
        work = spec.get("workload") or {}
        p.port = _int(work.get("container_port"), p.port)
        p.health_path = work.get("health_path") or p.health_path
        p.readiness_path = work.get("readiness_path") or p.readiness_path
        p.replicas_min = max(1, _int(work.get("replicas_min"), p.replicas_min))
        p.replicas_max = max(p.replicas_min, _int(work.get("replicas_max"), p.replicas_max))
        p.cpu_target_pct = _int(work.get("autoscaling_target_pct"), p.cpu_target_pct)
        p.report_schedule = legacy_cron_schedule(ws) or p.report_schedule
        return p

    def override(self, **values: Any) -> list[str]:
        """Apply non-empty overrides only if the result is valid; returns validation errors (empty = applied)."""
        names = {f.name for f in fields(self)}
        try:
            changes = {k: type(getattr(self, k))(v) for k, v in values.items() if v not in (None, "", 0) and k in names}
        except (TypeError, ValueError) as exc:
            return [f"bad override: {exc}"]
        candidate = replace(self, **changes)
        if errors := candidate.validate():
            return errors
        for name, value in changes.items():
            setattr(self, name, value)
        return []

    def validate(self) -> list[str]:
        errors = [
            f"{n} must be a DNS-1123 label"
            for n in ("service_name", "namespace", "image")
            if not _DNS.match(getattr(self, n))
        ]
        if not 1 <= self.port <= 65535:
            errors.append("port must be 1-65535")
        if not (1 <= self.replicas_min <= self.replicas_max):
            errors.append("need 1 <= replicas_min <= replicas_max")
        if self.image_tag in ("", "latest") or self.local_tag == "latest":
            errors.append("image tags must be pinned (not 'latest')")
        errors += [
            f"{n} must start with /" for n in ("health_path", "readiness_path") if not getattr(self, n).startswith("/")
        ]
        errors += [
            f"{n} is not a Kubernetes quantity"
            for n in ("cpu_request", "memory_request", "cpu_limit", "memory_limit")
            if not _QUANTITY.match(getattr(self, n))
        ]
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:63]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _service_from_app(application: dict[str, Any]) -> str:
    name = application.get("name") or ""
    return f"{name.removeprefix('legacy-')}-service" if name else ""


def legacy_cron_schedule(ws: Workspace) -> str | None:
    """First crontab schedule found in the legacy ops files (e.g. '0 2 * * *')."""
    for path in ws.listing("legacy/"):
        if path.endswith((".cron", "crontab")):
            for line in ws.read(path).splitlines():
                parts = line.split()
                if len(parts) >= 6 and not line.lstrip().startswith("#"):
                    return " ".join(parts[:5])
    return None


# ---------------------------------------------------------------- schema conversion (SQLite DDL -> SQLAlchemy Core)


@dataclass
class ColumnDef:
    name: str
    sa_type: str
    primary_key: bool = False
    nullable: bool = True
    unique: bool = False
    default: str | None = None
    references: str | None = None


@dataclass
class TableDef:
    name: str
    columns: list[ColumnDef]
    autoincrement: bool = False
    extra: list[str] | None = None  # table-level constraints rendered as code


_COLUMN_KEYWORDS = {
    "PRIMARY",
    "NOT",
    "NULL",
    "UNIQUE",
    "DEFAULT",
    "REFERENCES",
    "CHECK",
    "COLLATE",
    "CONSTRAINT",
    "AUTOINCREMENT",
    "GENERATED",
}


def extract_create_tables(source: str) -> list[str]:
    """Every CREATE TABLE statement in a .sql or .py file (balanced parentheses)."""
    statements = []
    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)[\"'`\]]?\s*\(", source, re.IGNORECASE
    ):
        depth, start = 1, match.end()
        for i in range(start, len(source)):
            depth += {"(": 1, ")": -1}.get(source[i], 0)
            if depth == 0:
                statements.append(f"{match.group(1)}\0{source[start:i]}")
                break
    return statements


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in body:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    return [p for p in [*parts, current.strip()] if p]


def _sa_type(sql_type: str) -> tuple[str, str | None]:
    """SQLite affinity rules -> SQLAlchemy type (+ a note when the mapping is lossy)."""
    t = sql_type.upper()
    if "INT" in t:
        return ("BigInteger" if "BIG" in t else "Integer"), None
    if m := re.search(r"VARCHAR\s*\((\d+)\)", t):
        return f"String({m.group(1)})", None
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "Text", None
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "Float", None
    if "BOOL" in t:
        return "Boolean", None
    if "BLOB" in t:
        return "LargeBinary", None
    if any(k in t for k in ("NUMERIC", "DECIMAL")):
        return "Numeric(asdecimal=False)", None
    if any(k in t for k in ("DATE", "TIME")):
        return "Text", f"{sql_type} kept as ISO text (same JSON as the legacy app)"
    return "Text", f"unknown type {sql_type!r} mapped to Text"


def parse_table(statement: str) -> tuple[TableDef, list[str]]:
    name, body = statement.split("\0", 1)
    table, notes = TableDef(name=name, columns=[], extra=[]), []
    for item in _split_top_level(body):
        upper = item.upper()
        if upper.startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK", "CONSTRAINT")):
            _table_constraint(table, item, notes)
            continue
        tokens = item.split()
        col_name = tokens[0].strip("\"'`[]")
        type_tokens = []
        for tok in tokens[1:]:
            if tok.upper().split("(")[0] in _COLUMN_KEYWORDS:
                break
            type_tokens.append(tok)
        sa_type, note = _sa_type(" ".join(type_tokens) or "TEXT")
        if note:
            notes.append(f"{name}.{col_name}: {note}")
        col = ColumnDef(name=col_name, sa_type=sa_type)
        col.primary_key = "PRIMARY KEY" in upper
        col.nullable = "NOT NULL" not in upper and not col.primary_key
        col.unique = bool(re.search(r"\bUNIQUE\b", upper))
        table.autoincrement |= "AUTOINCREMENT" in upper
        if m := re.search(r"\bDEFAULT\s+('(?:[^']|'')*'|\([^)]*\)|[\w.+-]+)", item, re.IGNORECASE):
            col.default = m.group(1)
        if m := re.search(r"\bREFERENCES\s+[\"'`]?(\w+)[\"'`]?\s*\(\s*[\"'`]?(\w+)", item, re.IGNORECASE):
            col.references = f"{m.group(1)}.{m.group(2)}"
        table.columns.append(col)
    return table, notes


def _table_constraint(table: TableDef, item: str, notes: list[str]) -> None:
    cols = lambda s: [c.strip(" \"'`[]") for c in s.split(",")]
    if m := re.match(r"(?i)PRIMARY\s+KEY\s*\(([^)]*)\)", item):
        for col in table.columns:
            if col.name in cols(m.group(1)):
                col.primary_key, col.nullable = True, False
    elif m := re.match(r"(?i)UNIQUE\s*\(([^)]*)\)", item):
        table.extra.append(f"UniqueConstraint({', '.join(_q(c) for c in cols(m.group(1)))})")
    elif m := re.match(r"(?i)FOREIGN\s+KEY\s*\(\s*(\w+)\s*\)\s*REFERENCES\s+(\w+)\s*\(\s*(\w+)", item):
        for col in table.columns:
            if col.name == m.group(1):
                col.references = f"{m.group(2)}.{m.group(3)}"
    else:
        notes.append(f"{table.name}: constraint not converted: {item}")


def render_models(tables: list[TableDef]) -> str:
    """Python source for app/models.py."""
    imports, blocks = {"Column", "MetaData", "Table"}, []
    for t in tables:
        lines = [f"{_var(t.name)} = Table(", f"    {_q(t.name)},", "    metadata,"]
        for c in t.columns:
            imports.add(re.match(r"\w+", c.sa_type).group(0))
            args = [_q(c.name), c.sa_type]
            if c.references:
                imports.add("ForeignKey")
                args.append(f"ForeignKey({_q(c.references)})")
            if c.primary_key:
                args.append("primary_key=True")
            elif not c.nullable:
                args.append("nullable=False")
            if c.unique:
                args.append("unique=True")
            if c.default is not None:
                imports.add("text")
                args.append(f"server_default=text({_q(c.default)})")
            lines.append(f"    Column({', '.join(args)}),")
        for extra in t.extra or []:
            imports.add(extra.split("(")[0])
            lines.append(f"    {extra},")
        if t.autoincrement:
            lines.append("    sqlite_autoincrement=True,")
        blocks.append("\n".join([*lines, ")"]))
    header = (
        '"""Tables converted from the legacy SQLite schema by scaffold_from_pattern (portable to PostgreSQL)."""'
        f"\n\nfrom sqlalchemy import {', '.join(sorted(imports, key=str.lower))}\n\nmetadata = MetaData()\n"
    )
    return header + "\n\n" + "\n\n\n".join(blocks) + "\n"


def _q(value: str) -> str:
    return json.dumps(value)


def _var(name: str) -> str:
    import keyword

    var = re.sub(r"\W", "_", name)
    return f"{var}_table" if keyword.iskeyword(var) or var in {"metadata", "text"} else var


def convert_legacy_schema(ws: Workspace) -> tuple[list[TableDef], list[str]]:
    """Find CREATE TABLE statements in legacy .sql files (then .py files) and parse them."""
    tables, notes, seen = [], [], set()
    paths = [p for p in ws.listing("legacy/") if p.endswith(".sql")] + [
        p for p in ws.listing("legacy/") if p.endswith(".py")
    ]
    for path in paths:
        for statement in extract_create_tables(ws.read(path)):
            table, table_notes = parse_table(statement)
            if table.name not in seen and table.columns:
                seen.add(table.name)
                tables.append(table)
                notes += table_notes
    return tables, notes


# ---------------------------------------------------------------- rendering


def substitute(template: str, params: Params) -> str:
    values = {k: str(v) for k, v in params.as_dict().items()}
    rendered = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)
    if leftover := _PLACEHOLDER.findall(rendered):
        raise ValueError(f"unknown placeholders: {sorted(set(leftover))}")
    return rendered


def render_report(template: str, tables: list[TableDef], report_name: str) -> str:
    """Default report = every non-key column of the first legacy table (the LLM checks it against the legacy report)."""
    if not tables:
        return template
    first = tables[0]
    pk = next((c.name for c in first.columns if c.primary_key), first.columns[0].name)
    columns = [c.name for c in first.columns if not c.primary_key] or [pk]
    block = (
        f'REPORT_NAME = "{report_name}"\nCOLUMNS = {json.dumps(columns)}\n'
        f'QUERY = "SELECT {", ".join(columns)} FROM {first.name} ORDER BY {pk}"\n'
    )
    return re.sub(r"REPORT_NAME = .*?\nQUERY = .*?\n", block, template, count=1, flags=re.DOTALL)


def legacy_seed(ws: Workspace) -> str | None:
    for path in ws.listing("legacy/"):
        if path.rsplit("/", 1)[-1].startswith("seed") and path.endswith(".sql"):
            return ws.read(path)
    return None


def scaffold(ws: Workspace, params: Params) -> dict[str, Any]:
    """Render the pattern into the workspace. Domain files that were changed since rendering are kept."""
    written, kept = [], []
    tables, notes = convert_legacy_schema(ws)
    for src in sorted(PATTERN_DIR.rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(PATTERN_DIR).as_posix()
        content = substitute(src.read_text(), params)
        if rel == "microservice/app/models.py" and tables:
            content = render_models(tables)
        elif rel == "microservice/app/report.py":
            content = render_report(content, tables, params.namespace)
        elif rel == "microservice/app/seed.sql" and (seed := legacy_seed(ws)):
            content = seed
        if rel in DOMAIN_FILES and ws.files.get(rel, content) != content:
            kept.append(rel)  # ported/edited by the model or a previous run
            continue
        written.append(ws.write(rel, content))
    return {"written": written, "kept": kept, "tables": tables, "notes": notes}


def example_text() -> str:
    """The curated before/after worked example shown to the model."""
    parts = []
    for name, label in [("legacy_app.py", "BEFORE"), ("api.py", "AFTER"), ("test_api.py", "AFTER")]:
        body = (EXAMPLE_DIR / name).read_text().replace("# ruff: noqa\n", "")
        parts.append(f"### {label}: example/{name}\n```python\n{body.rstrip()}\n```")
    return "\n\n".join(parts)
