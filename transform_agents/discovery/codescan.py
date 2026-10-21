"""Static analysis of the legacy application: plain Python (ast + regex), no LLM.

`scan_app(files)` returns facts (application, components, dependencies, runtime) and
`signals`: rule keys with evidence (file:line function) that tools.py turns into findings.
"""

import ast
import posixpath
import re
from typing import Any

SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIAL", re.IGNORECASE)
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
HEALTH_PATHS = {"/status", "/health", "/healthz", "/livez", "/ping"}
READY_PATHS = {"/ready", "/readyz"}
DB_DRIVERS = {"sqlite3": "sqlite", "psycopg2": "postgres", "psycopg": "postgres", "pymysql": "mysql", "MySQLdb": "mysql"}
EXTERNAL_LIBS = {"requests", "httpx", "urllib3", "boto3", "smtplib", "redis", "pika", "kafka"}
# Latest major.minor we know about (curated, 2026-10); older majors are flagged as outdated.
LATEST = {"flask": "3.1", "werkzeug": "3.1", "jinja2": "3.1", "django": "5.2", "fastapi": "0.115"}
FRAMEWORKS = ("flask", "django", "fastapi")


def scan_app(files: dict[str, str], root: str = "legacy/app/") -> dict[str, Any]:
    """Scan every file under `root` and return facts plus rule signals with evidence."""
    app_files = {p[len(root):]: c for p, c in files.items() if p.startswith(root)}
    signals: list[dict[str, str]] = []
    packages = _requirements(app_files.get("requirements.txt", ""))
    py = {p: c for p, c in app_files.items() if p.endswith(".py")}

    routes, facts = [], _PyFacts()
    for path, src in sorted(py.items()):
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            signals.append({"key": "parse_error", "evidence": f"{path}:{exc.lineno} {exc.msg}"})
            continue
        facts.collect(path, src, tree)
        routes += _routes(path, tree)

    ops = _ops(app_files)
    tables = sorted({t for p, c in app_files.items() if p.endswith(".sql")
                     for t in re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)", c, re.IGNORECASE)})
    name = ops["service_name"] or "legacy-app"
    framework = next((f"{n.lower()} {v}" for n, v in packages.items() if n.lower() in FRAMEWORKS), None)
    if not framework:
        framework = next((f for f in FRAMEWORKS if f in facts.imports), "unknown")

    # ---- rule signals (keys map to knowledge/assessment_rules.yaml) ----
    for s in facts.sql_concat:
        signals.append({"key": "sql_injection", "evidence": s})
    if facts.secrets:
        signals.append({"key": "hardcoded_secrets", "evidence": "; ".join(facts.secrets)})
    unauth = [f"{'/'.join(r['methods'])} {r['path']} ({r['handler']})" for r in routes
              if MUTATING & set(r["methods"]) and not r["auth"]]
    static_token = [f"{r['handler']}() compares a header to the hard-coded {r['auth']}" for r in routes
                    if r["auth"] and r["auth"] != "header"]
    if unauth or static_token:
        signals.append({"key": "unauthenticated_writes",
                        "evidence": "; ".join(([f"no auth on {', '.join(unauth)}"] if unauth else []) + static_token)})
    if ops["run_as"] == "root":
        signals.append({"key": "runs_as_root", "evidence": "; ".join(ops["root_evidence"])})
    outdated = [f"{n}=={v} (current {LATEST[n.lower()]})" for n, v in packages.items() if _older_major(n, v)]
    if outdated:
        signals.append({"key": "outdated_stack", "evidence": f"requirements.txt: {', '.join(outdated)}"})
    if ops["nginx"] and not ops["nginx"]["tls"]:
        signals.append({"key": "public_http", "evidence": f"{ops['nginx']['file']}: listen "
                        f"{','.join(map(str, ops['nginx']['listen']))} without TLS"})
    if facts.db_engine in ("sqlite", None) and facts.db_location:
        signals.append({"key": "local_database", "evidence": facts.db_evidence})
    if facts.file_writes:
        signals.append({"key": "local_files", "evidence": "; ".join(facts.file_writes)})
    if facts.dev_server:
        evidence = facts.dev_server + (f"; systemd ExecStart={ops['exec_start']}" if ops["exec_start"] else "")
        if not any(n in packages for n in ("gunicorn", "uwsgi", "waitress")):
            signals.append({"key": "dev_server", "evidence": evidence})
    if facts.path_constants:
        env = "reads no environment variables" if not facts.uses_env else "partly env-based"
        signals.append({"key": "hardcoded_config", "evidence": f"{', '.join(facts.path_constants)}; {env}"})
    if facts.log_files:
        signals.append({"key": "file_logging", "evidence": "; ".join(facts.log_files)})
    if ops["cron"]:
        signals.append({"key": "host_cron", "evidence": "; ".join(f"{c['file']}: {c['line']}" for c in ops["cron"])})
    health = [r for r in routes if r["path"] in HEALTH_PATHS]
    ready = [r for r in routes if r["path"] in READY_PATHS]
    if not ready:
        if health:
            h = health[0]
            what = "returns a static string, no DB check" if h["static"] else "liveness only"
            evidence = f"{h['file']}:{h['line']} GET {h['path']} ({what}); no readiness endpoint"
        else:
            evidence = "no health or readiness route found"
        signals.append({"key": "no_probes", "evidence": evidence})

    base = name.removeprefix("legacy-")
    return {
        "application": {
            "name": name, "language": "python", "language_version": None, "framework": framework,
            "entrypoint": facts.entrypoint or (min(py) if py else None),
            "listen_port": facts.listen_port, "loc": sum(_loc(c) for c in py.values()),
            "files": len(app_files),
        },
        "components": _components(base, routes, ops),
        "dependencies": {
            "python_packages": [f"{n}=={v}" if v else n for n, v in packages.items()],
            "database": {"engine": facts.db_engine or "none", "location": facts.db_location, "tables": tables},
            "local_state": sorted(set(facts.local_state)),
            "external_services": sorted(facts.imports & EXTERNAL_LIBS),
        },
        "runtime": {
            "process_manager": ops["process_manager"], "run_as": ops["run_as"], "exec_start": ops["exec_start"],
            "reverse_proxy": "nginx" if ops["nginx"] else None, "proxy": ops["nginx"],
            "scheduled_jobs": [c["summary"] for c in ops["cron"]],
        },
        "signals": signals,
    }


class _PyFacts:
    """Collects facts across Python modules with a single AST walk per file."""

    def __init__(self):
        self.imports: set[str] = set()
        self.secrets: list[str] = []
        self.path_constants: list[str] = []
        self.sql_concat: list[str] = []
        self.file_writes: list[str] = []
        self.log_files: list[str] = []
        self.local_state: list[str] = []
        self.db_engine: str | None = None
        self.db_location: str | None = None
        self.db_evidence = ""
        self.dev_server = ""
        self.entrypoint: str | None = None
        self.listen_port: int | None = None
        self.uses_env = False

    def collect(self, path: str, src: str, tree: ast.Module) -> None:
        consts = _module_constants(tree)
        for name, (value, line) in consts.items():
            if isinstance(value, str) and SECRET_NAME.search(name) and value:
                self.secrets.append(f"{path}:{line} {name}={value[:3]}***")
            elif isinstance(value, str) and value.startswith("/"):
                self.path_constants.append(f"{path}:{line} {name}={value}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.imports |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                self.uses_env = True
        for fn, node in _calls_with_function(tree):
            self._call(path, src, consts, fn, node)

    def _call(self, path: str, src: str, consts: dict, fn: ast.FunctionDef | None, node: ast.Call) -> None:
        where = f"{path}:{node.lineno}" + (f" {fn.name}()" if fn else "")
        func = _dotted(node.func)
        attr = func.rsplit(".", 1)[-1]
        if attr in ("execute", "executemany") and node.args and _is_built_string(node.args[0]):
            snippet = (ast.get_source_segment(src, node.args[0]) or "").replace("\n", " ")
            self.sql_concat.append(f"{where}: {snippet[:90]}")
        elif func == "open" and _open_mode(node) and any(m in _open_mode(node) for m in "wax"):
            dirs = _referenced_paths(fn or node, consts, exclude=self.db_location)
            self.file_writes.append(f"{where} writes to {', '.join(dirs) or 'a local file'}")
            self.local_state += [d.split("=", 1)[1] for d in dirs]
        elif func in ("logging.basicConfig", "logging.FileHandler"):
            target = _kwarg(node, "filename") or (node.args[0] if func.endswith("FileHandler") and node.args else None)
            if target is not None:
                value = _resolve(target, consts)
                self.log_files.append(f"{where} {func}(filename={value})")
                self.local_state.append(value.split("=", 1)[-1])
        elif func.endswith(".connect") and func.split(".")[0] in DB_DRIVERS:
            self.db_engine = DB_DRIVERS[func.split(".")[0]]
            if node.args and self.db_location is None:
                value = _resolve(node.args[0], consts)
                self.db_location = value.split("=", 1)[-1]
                self.db_evidence = f"{where} {func}({value})"
        elif attr == "run" and fn is None and (func.split(".")[0] in ("app", "application") or _kwarg(node, "port")):
            kwargs = {k.arg: ast.get_source_segment(src, k.value) for k in node.keywords if k.arg}
            port = _kwarg(node, "port")
            self.listen_port = port.value if isinstance(port, ast.Constant) else self.listen_port
            self.entrypoint = path
            self.dev_server = f"{path}:{node.lineno} {func}({', '.join(f'{k}={v}' for k, v in kwargs.items())})"


def _routes(path: str, tree: ast.Module) -> list[dict[str, Any]]:
    """Flask-style @app.route(path, methods=[...]) handlers with auth and template hints."""
    consts = _module_constants(tree)
    secret_names = {n for n in consts if SECRET_NAME.search(n)}
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in fn.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ("route", "get", "post", "put", "delete", "patch")
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            methods_node = _kwarg(dec, "methods")
            if dec.func.attr != "route":
                methods = [dec.func.attr.upper()]
            elif isinstance(methods_node, (ast.List, ast.Tuple)):
                methods = [e.value.upper() for e in methods_node.elts if isinstance(e, ast.Constant)]
            else:
                methods = ["GET"]
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            attrs = {_dotted(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            decorators = {_dotted(d.func if isinstance(d, ast.Call) else d) for d in fn.decorator_list}
            auth = None
            if any("login_required" in d or "auth" in d for d in decorators) or "request.authorization" in attrs:
                auth = "header"
            elif "request.headers" in attrs or "request.headers.get" in attrs:
                auth = next(iter(names & secret_names), "header")
            calls = {_dotted(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
            body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
            found.append({
                "file": path, "line": dec.lineno, "handler": fn.name, "methods": methods,
                "path": re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", dec.args[0].value),
                "auth": auth, "ui": bool(calls & {"render_template", "redirect"}),
                "writes_files": "open" in calls,
                "static": len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Constant),
            })
    return found


def _components(base: str, routes: list[dict], ops: dict) -> list[dict[str, Any]]:
    """Group routes into API, web UI and batch components."""
    groups: dict[str, list[dict]] = {"http-api": [], "web-ui": [], "batch": []}
    for r in routes:
        kind = "web-ui" if r["ui"] else "batch" if ("report" in r["path"] or r["writes_files"]) else "http-api"
        groups[kind].append(r)
    cron = "; ".join(c["summary"] for c in ops["cron"])
    notes = {
        "http-api": "JSON API" + (" plus a health check" if any(r["path"] in HEALTH_PATHS for r in groups["http-api"])
                                  else ""),
        "web-ui": "server-rendered HTML (Jinja2 templates) with form posts",
        "batch": "HTTP-triggered job that writes files to local disk" + (f"; triggered by {cron}" if cron else ""),
    }
    names = {"http-api": f"{base}-api", "web-ui": f"{base}-web-ui", "batch": f"{base}-report-job"}
    return [
        {"name": names[kind], "kind": kind, "routes": sorted({r["path"] for r in rs}),
         "endpoints": [f"{'/'.join(r['methods'])} {r['path']} -> {r['handler']}()" for r in rs], "notes": notes[kind]}
        for kind, rs in groups.items() if rs
    ]


def _ops(app_files: dict[str, str]) -> dict[str, Any]:
    """systemd unit, cron entries and nginx config shipped next to the app."""
    out: dict[str, Any] = {"service_name": None, "process_manager": None, "run_as": None, "exec_start": None,
                           "root_evidence": [], "cron": [], "nginx": None}
    for path, text in sorted(app_files.items()):
        base = posixpath.basename(path)
        if base.endswith(".service"):
            out["service_name"] = base.removesuffix(".service")
            out["process_manager"] = "systemd"
            user = re.search(r"^User=(\S+)", text, re.MULTILINE)
            exec_start = re.search(r"^ExecStart=(.+)$", text, re.MULTILINE)
            out["exec_start"] = exec_start.group(1).strip() if exec_start else None
            out["run_as"] = user.group(1) if user else "root"
            if out["run_as"] == "root":
                out["root_evidence"].append(f"{path}: {'User=root' if user else 'no User= (defaults to root)'}")
        elif base.endswith(".cron") or base == "crontab":
            for line in text.splitlines():
                parts = line.split()
                if len(parts) < 6 or line.lstrip().startswith("#"):
                    continue
                schedule, rest = " ".join(parts[:5]), parts[5:]
                user = rest[0] if len(rest) > 1 and rest[0] in ("root", "ec2-user", "nobody", out["run_as"]) else None
                command = " ".join(rest[1:] if user else rest)
                url = re.search(r"https?://\S+", command)
                _minute, hour, *calendar = schedule.split()
                daily = calendar == ["*", "*", "*"] and hour.isdigit()
                what = ("nightly" if daily and (int(hour) < 6 or int(hour) >= 22) else "daily" if daily else "scheduled")
                out["cron"].append({"file": path, "line": line.strip(), "schedule": schedule, "user": user,
                                    "summary": f"{what} job via cron ({schedule}): {url.group(0) if url else command}"})
                if user == "root":
                    out["root_evidence"].append(f"{path}: cron job runs as root")
        elif base.startswith("nginx") or base.endswith(".conf") and "proxy_pass" in text:
            listen = [int(p) for p in re.findall(r"^\s*listen\s+(?:[\d.]+:)?(\d+)", text, re.MULTILINE)]
            upstream = re.search(r"proxy_pass\s+(\S+);", text)
            out["nginx"] = {"file": path, "listen": listen, "tls": "ssl" in text,
                            "upstream": upstream.group(1) if upstream else None}
    if out["run_as"] is None and out["root_evidence"]:
        out["run_as"] = "root"
    return out


# ---- small AST helpers ----

def _requirements(text: str) -> dict[str, str]:
    pkgs = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-\[\]]+)\s*(?:==|>=|~=)?\s*([\w.]*)", line)
        if m and m.group(1):
            pkgs[m.group(1)] = m.group(2)
    return pkgs


def _older_major(name: str, version: str) -> bool:
    latest = LATEST.get(name.lower())
    if not latest or not version:
        return False
    return int(version.split(".")[0]) < int(latest.split(".")[0])


def _loc(src: str) -> int:
    return sum(1 for line in src.splitlines() if line.strip() and not line.strip().startswith("#"))


def _module_constants(tree: ast.Module) -> dict[str, tuple[Any, int]]:
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    out[target.id] = (node.value.value, node.lineno)
    return out


def _calls_with_function(tree: ast.Module):
    """Yield (enclosing function or None, call node) for every call in the module."""
    seen = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and id(node) not in seen:
                    seen.add(id(node))
                    yield fn, node
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in seen:
            yield None, node


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_built_string(node: ast.AST) -> bool:
    """SQL assembled at runtime: concatenation, %-formatting, f-strings or .format()."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"


def _open_mode(call: ast.Call) -> str:
    mode = call.args[1] if len(call.args) > 1 else _kwarg(call, "mode")
    return mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else ""


def _resolve(node: ast.AST, consts: dict) -> str:
    if isinstance(node, ast.Name) and node.id in consts:
        return f"{node.id}={consts[node.id][0]}"
    if isinstance(node, ast.Constant):
        return str(node.value)
    return ast.unparse(node)


def _referenced_paths(scope: ast.AST, consts: dict, exclude: str | None) -> list[str]:
    """Absolute-path constants referenced in a function (the data flow is simple enough for this app)."""
    names = {n.id for n in ast.walk(scope) if isinstance(n, ast.Name)}
    return sorted(f"{n}={consts[n][0]}" for n in names
                  if n in consts and isinstance(consts[n][0], str) and consts[n][0].startswith("/")
                  and consts[n][0] != exclude)
