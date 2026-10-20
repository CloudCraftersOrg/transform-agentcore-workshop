"""Small HCL helpers: parse Terraform files, resolve simple references, edit variable defaults, render values."""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import hcl2

_OPTS = hcl2.SerializationOptions(with_comments=False, strip_string_quotes=True)
_PURE_REF = re.compile(r"^\$\{(!?)(var|local)\.([A-Za-z0-9_-]+)\}$")
_VAR_IN_TEMPLATE = re.compile(r"\$\{var\.([A-Za-z0-9_-]+)\}")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class HclParseError(ValueError):
    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.line, self.column = line, column


def parse(text: str) -> dict:
    """Parse HCL text into a dict (string quotes stripped). Raises HclParseError with a line number."""
    try:
        return hcl2.loads(text, serialization_options=_OPTS)
    except Exception as exc:  # lark raises several exception types
        line, column = getattr(exc, "line", None), getattr(exc, "column", None)
        token = getattr(exc, "token", None)
        expected = sorted(getattr(exc, "expected", None) or [])
        if token is not None:
            shown = "end of file" if token.type == "$END" else repr(str(token).strip()[:30] or token.type)
            message = f"unexpected {shown}"
            if expected:
                message += f" (expected one of: {', '.join(expected[:6])})"
        else:
            message = str(exc).splitlines()[0][:200]
        raise HclParseError(message, line if isinstance(line, int) and line > 0 else None, column) from exc


def body(entry: dict) -> dict:
    """Drop the parser's `__is_block__` marker."""
    return {k: v for k, v in entry.items() if k != "__is_block__"}


@dataclass
class TfConfig:
    """A parsed Terraform directory: {file name: parsed document}."""

    docs: dict[str, dict] = field(default_factory=dict)

    def blocks(self, kind: str, labels: int) -> list[tuple[str, tuple[str, ...], dict]]:
        """Every `kind` block as (file, labels, body); e.g. blocks("resource", 2)."""
        found = []
        for fname, doc in self.docs.items():
            for entry in doc.get(kind, []):
                found.extend((fname, labs, b) for labs, b in _unwrap(entry, labels))
        return found

    def variables(self) -> dict[str, dict]:
        return {labs[0]: b for _, labs, b in self.blocks("variable", 1)}

    def modules(self) -> dict[str, dict]:
        return {labs[0]: b for _, labs, b in self.blocks("module", 1)}

    def resources(self, kind: str = "resource") -> dict[tuple[str, str], dict]:
        return {(labs[0], labs[1]): b for _, labs, b in self.blocks(kind, 2)}

    def outputs(self) -> list[str]:
        return [labs[0] for _, labs, _ in self.blocks("output", 1)]

    def locals(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for doc in self.docs.values():
            for entry in doc.get("locals", []):
                merged.update(body(entry))
        return merged

    def required_providers(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for doc in self.docs.values():
            for tf in doc.get("terraform", []):
                for rp in tf.get("required_providers", []):
                    merged.update(body(rp))
        return merged

    def resolve(self, value: Any, depth: int = 0) -> Any:
        """Resolve `${var.x}`, `${local.x}`, `${!var.x}` and `"${var.x}-suffix"` to literal values when possible."""
        if not isinstance(value, str) or depth > 5:
            return value
        match = _PURE_REF.match(value)
        if match:
            negate, kind, name = match.groups()
            if kind == "var":
                var = self.variables().get(name, {})
                resolved = self.resolve(var["default"], depth + 1) if "default" in var else value
            else:
                local_values = self.locals()
                resolved = self.resolve(local_values[name], depth + 1) if name in local_values else value
            if negate:
                return (not resolved) if isinstance(resolved, bool) else value
            return resolved
        if "${" in value:
            variables = self.variables()

            def sub(m: re.Match) -> str:
                default = variables.get(m.group(1), {}).get("default")
                return str(default) if isinstance(default, (str, int, float)) else m.group(0)

            return _VAR_IN_TEMPLATE.sub(sub, value)
        return value


def _unwrap(entry: dict, labels: int, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], dict]]:
    if labels == 0:
        return [(prefix, body(entry))]
    out = []
    for key, value in entry.items():
        if key == "__is_block__":
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                out.extend(_unwrap(item, labels - 1, (*prefix, key)))
    return out


# --- Variable defaults: edit variables.tf text in place -------------------------------------------------------------


def render(value: Any, indent: int = 0) -> str:
    """Render a Python value as an HCL expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value).replace("${", "$${")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(v, indent) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        pad = " " * (indent + 2)
        width = max(len(_key(k)) for k in value)
        lines = [f"{pad}{_key(k).ljust(width)} = {render(v, indent + 2)}" for k, v in value.items()]
        return "{\n" + "\n".join(lines) + "\n" + " " * indent + "}"
    return json.dumps(str(value))


def inline(value: Any) -> str:
    """One-line rendering for logs."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_key(k)} = {inline(v)}" for k, v in value.items()) + "}"
    return render(value)


def _key(key: Any) -> str:
    key = str(key)
    return key if _IDENT.match(key) else json.dumps(key)


def _bracket_delta(line: str) -> int:
    """Net count of open brackets in a line, ignoring quoted strings and comments."""
    delta, in_str, escaped = 0, False, False
    for ch in line:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#":
            break
        elif ch in "[{(":
            delta += 1
        elif ch in "]})":
            delta -= 1
    return delta


def variable_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """Line range [start, end] of `variable "name" { ... }` (end is the closing brace line)."""
    head = re.compile(rf'^\s*variable\s+"?{re.escape(name)}"?\s*\{{')
    for start, line in enumerate(lines):
        if head.match(line):
            depth = 0
            for end in range(start, len(lines)):
                depth += _bracket_delta(lines[end])
                if depth <= 0:
                    return start, end
            return None
    return None


def coerce(value: Any, type_expr: str) -> Any:
    """Coerce a tool-supplied value to a variable's declared type (raises ValueError when impossible)."""
    t = (type_expr or "any").replace("${", "").replace("}", "").strip()
    if t == "string":
        if isinstance(value, (dict, list)):
            raise ValueError("expected a string")
        return "true" if value is True else "false" if value is False else str(value)
    if t == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        if isinstance(value, str):
            value = float(value) if "." in value else int(value)
        if not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        return int(value) if float(value).is_integer() else value
    if t == "bool":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
        return value
    if t.startswith(("list", "set", "tuple")):
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",")] if "," in value else [value]
        if not isinstance(value, list):
            raise ValueError("expected a list")
        return value
    if t.startswith(("map", "object")):
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    return value


def set_defaults(text: str, values: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Set `default = ...` for the given variables. Returns (new_text, changes, errors)."""
    lines = text.splitlines()
    try:
        declared = TfConfig({"variables.tf": parse(text)}).variables()
    except HclParseError as exc:
        return text, [], [f"variables.tf does not parse (line {exc.line}): {exc}; fix it with edit_file first"]
    changes, errors = [], []
    for name, raw in values.items():
        if name not in declared:
            hint = suggest(name, declared)
            errors.append(f"unknown variable {name!r}{hint}")
            continue
        span = variable_span(lines, name)
        if span is None:
            errors.append(f"could not locate variable {name!r} in the text; use edit_file")
            continue
        try:
            value = coerce(raw, str(declared[name].get("type", "any")))
        except (ValueError, TypeError) as exc:
            errors.append(f"{name}: {exc} (declared type {declared[name].get('type')})")
            continue
        old = declared[name].get("default", "<none>")
        if old == value:
            continue
        start, end = span
        default_at = next((i for i in range(start + 1, end) if re.match(r"^\s*default\s*=", lines[i])), None)
        if default_at is None:
            lines[end:end] = f"  default = {render(value, 2)}".splitlines()
        else:
            stop, depth = default_at, _bracket_delta(lines[default_at])
            while depth > 0 and stop + 1 < end:
                stop += 1
                depth += _bracket_delta(lines[stop])
            prefix = re.match(r"^\s*default\s*=\s*", lines[default_at]).group(0)  # keep the aligned `=`
            lines[default_at:stop + 1] = f"{prefix}{render(value, 2)}".splitlines()
        changes.append(f"{name}: {inline(old) if old != '<none>' else '(none)'} -> {inline(value)}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changes, errors


def suggest(name: str, candidates: Any, renames: dict[str, str] | None = None) -> str:
    """'; did you mean "x"?' using known renames, then fuzzy matching."""
    import difflib

    pool = list(candidates)
    if renames and name in renames:
        return f'; did you mean "{renames[name]}"?'
    if name.startswith("cluster_") and name[len("cluster_"):] in pool:
        return f'; did you mean "{name[len("cluster_"):]}"? (renamed in v21)'
    close = difflib.get_close_matches(name, pool, n=2, cutoff=0.6)
    return f"; did you mean {' or '.join(repr(c) for c in close)}?" if close else ""


def tfvars_example(variables_text: str) -> str:
    """terraform.tfvars.example generated from the variable defaults in variables.tf."""
    variables = TfConfig({"variables.tf": parse(variables_text)}).variables()
    out = ["# Generated from the defaults in variables.tf. Copy to terraform.tfvars to override per environment.", ""]
    group: list[tuple[str, str]] = []  # consecutive one-line assignments share the `=` column, like terraform fmt

    def flush() -> None:
        width = max((len(n) for n, _ in group), default=0)
        out.extend(f"{n.ljust(width)} = {v}" for n, v in group)
        group.clear()

    for name, var in variables.items():
        if "default" not in var:
            continue
        value = render(var["default"])
        if "\n" in value:
            flush()
            out.append(f"{name} = {value}")
        else:
            group.append((name, value))
    flush()
    return "\n".join(out) + "\n"
