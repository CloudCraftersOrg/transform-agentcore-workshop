"""Layered Terraform validation, fast enough for a live demo.

1. Always: `terraform fmt` (when the binary exists) and an HCL parse with python-hcl2, with line numbers.
2. Always: offline checks against knowledge/module-inputs.json (module and resource arguments with did-you-mean
   hints, nested node-group attributes, required inputs, references, duplicates) plus a few security guardrails.
3. When the terraform binary exists: `terraform init -backend=false` + `terraform validate`, reusing a warm
   module/provider cache keyed by the module and provider versions (see `warmup()`).

Without the binary (as on AgentCore Runtime) the result is reported with `level = "offline"`.
"""

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .hcl import HclParseError, TfConfig, parse, suggest

KNOWLEDGE = Path(__file__).parent / "knowledge"
BLUEPRINT_DIR = KNOWLEDGE / "blueprint"
MODULE_META_ARGS = {"source", "version", "providers", "count", "for_each", "depends_on"}
RESOURCE_META_ARGS = {"count", "for_each", "depends_on", "provider", "lifecycle", "provisioner", "connection"}
# Pre-v21 argument names that models keep producing (terraform-aws-modules/eks).
EKS_RENAMES = {"node_groups": "eks_managed_node_groups", "cluster_addons": "addons", "desired_capacity": "desired_size",
               "instance_type": "instance_types", "cluster_endpoint_public_access": "endpoint_public_access"}
INIT_TIMEOUT_S, VALIDATE_TIMEOUT_S = 240, 120
# `terraform init` downloads and runs whatever the sources point at: only these may reach the online layer.
ALLOWED_MODULE_SOURCES = ("terraform-aws-modules/vpc/aws", "terraform-aws-modules/eks/aws")  # the blueprint's modules
ALLOWED_PROVIDER = re.compile(r"^(registry\.terraform\.io/)?hashicorp/[A-Za-z0-9][A-Za-z0-9_-]*$")


def cache_dir() -> Path:
    return Path(os.getenv("TF_WORKSHOP_CACHE", "~/.cache/transform-workshop/terraform")).expanduser()


@functools.cache
def knowledge() -> dict:
    """Offline schemas: module inputs/outputs and selected AWS resource arguments."""
    return json.loads((KNOWLEDGE / "module-inputs.json").read_text())


def blueprint_files() -> dict[str, str]:
    """The validated blueprint as {file name: text}."""
    names = ["versions.tf", "variables.tf", "main.tf", "outputs.tf"]
    return {name: (BLUEPRINT_DIR / name).read_text() for name in names}


def terraform_bin() -> str | None:
    """Path to terraform, or None when it is missing or TERRAFORM_OFFLINE=1."""
    if os.getenv("TERRAFORM_OFFLINE", "").lower() in ("1", "true", "yes"):
        return None
    return os.getenv("TERRAFORM_BIN") or shutil.which("terraform")


@dataclass
class Finding:
    severity: str  # error | warning
    check: str  # parse | fmt | module | attribute | resource | reference | duplicate | policy | terraform
    message: str
    file: str = ""
    line: int | None = None

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}: " if self.file and self.line else f"{self.file}: " if self.file else ""
        return f"{where}{self.message}"


@dataclass
class Report:
    valid: bool
    level: str  # terraform | offline
    findings: list[Finding] = field(default_factory=list)
    terraform_output: str = ""
    formatted: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def text(self, max_items: int = 12) -> str:
        """Concise, numbered report for the model and for `data.validate_output`."""
        head = f"{'VALID' if self.valid else 'INVALID'} (level: {self.level}, {self.seconds:.1f}s)"
        lines = [head]
        if self.errors:
            lines.append(f"Errors ({len(self.errors)}):")
            lines += [f"  {i}. {e}" for i, e in enumerate(self.errors[:max_items], 1)]
        if self.warnings:
            lines.append(f"Warnings ({len(self.warnings)}):")
            lines += [f"  - {w}" for w in self.warnings[:max_items]]
        lines += [f"  * {n}" for n in self.notes]
        return "\n".join(lines)


# --- Layer 1: parse -------------------------------------------------------------------------------------------------


def parse_all(files: dict[str, str]) -> tuple[TfConfig, list[Finding]]:
    cfg, findings = TfConfig(), []
    for name, text in files.items():
        try:
            cfg.docs[name] = parse(text)
        except HclParseError as exc:
            snippet = ""
            if exc.line and exc.line <= len(text.splitlines()):
                snippet = f" near `{text.splitlines()[exc.line - 1].strip()[:80]}`"
            findings.append(Finding("error", "parse", f"HCL syntax error: {exc}{snippet}", name, exc.line))
    return cfg, findings


# --- Layer 2: offline checks ----------------------------------------------------------------------------------------


def version_matches(constraint: str, version: str) -> bool:
    """Minimal Terraform version-constraint check (~>, >=, <=, >, <, =, !=)."""

    def nums(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    have = nums(version)
    for part in constraint.split(","):
        m = re.match(r"\s*(~>|>=|<=|!=|>|<|=)?\s*v?([\d.]+)", part)
        if not m:
            return False
        op, want = m.group(1) or "=", nums(m.group(2))
        padded = have[: len(want)]
        if op == "~>":
            upper = (*want[:-2], want[-2] + 1) if len(want) > 1 else (want[0] + 1,)
            if not (have >= want and have[: len(upper)] < upper):
                return False
        elif not {"=": padded == want, "!=": padded != want, ">=": have >= want, "<=": padded <= want,
                  ">": have > want, "<": padded < want}[op]:
            return False
    return True


def _line_of(text: str, pattern: str, after: int = 0) -> int | None:
    rx = re.compile(pattern)
    for i, line in enumerate(text.splitlines()[after:], after + 1):
        if rx.search(line):
            return i
    return None


def _code_only(text: str) -> str:
    """Text without comments, for reference scanning."""
    out = []
    for line in text.splitlines():
        in_str, cut = False, len(line)
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str and (ch == "#" or line[i:i + 2] == "//"):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def check_duplicates(cfg: TfConfig) -> list[Finding]:
    findings, seen = [], {}
    for kind, labels in [("variable", 1), ("output", 1), ("module", 1), ("resource", 2), ("data", 2)]:
        for fname, labs, _ in cfg.blocks(kind, labels):
            key = (kind, labs)
            if key in seen:
                label = " ".join(f'"{x}"' for x in labs)
                findings.append(Finding("error", "duplicate", f"duplicate {kind} {label} (also in {seen[key]}); "
                                        "keep one", fname))
            seen.setdefault(key, fname)
    local_seen: dict[str, str] = {}
    for fname, doc in cfg.docs.items():
        for entry in doc.get("locals", []):
            for name in entry:
                if name != "__is_block__" and name in local_seen:
                    findings.append(Finding("error", "duplicate", f"duplicate local value {name!r}", fname))
                local_seen.setdefault(name, fname)
    return findings


def check_modules(cfg: TfConfig, texts: dict[str, str]) -> list[Finding]:
    findings, schemas = [], knowledge()["modules"]
    for fname, (name,), block in cfg.blocks("module", 1):
        text = texts[fname]
        start = _line_of(text, rf'^\s*module\s+"{re.escape(name)}"') or 1
        source = str(block.get("source", "")).removeprefix("registry.terraform.io/")
        if not module_source_allowed(source):
            continue  # reported by check_sources
        schema = schemas.get(source.split("//", 1)[0])
        if schema is None:
            findings.append(Finding("warning", "module", f'module "{name}": no offline schema for {source}; '
                                    "its arguments are only checked by terraform validate", fname, start))
            continue
        constraint = str(block.get("version", ""))
        if not constraint:
            findings.append(Finding("error", "module", f'module "{name}": pin version = "{schema["constraint"]}"',
                                    fname, start))
        elif not version_matches(constraint, schema["version"]):
            findings.append(Finding("error", "module", f'module "{name}": version "{constraint}" does not match the '
                                    f'validated {schema["version"]}; keep version = "{schema["constraint"]}" (older '
                                    "majors use different argument names)", fname,
                                    _line_of(text, r"^\s*version\s*=", start - 1)))
        inputs = schema["inputs"]
        renames = EKS_RENAMES if source.endswith("/eks/aws") else {}
        for arg, value in block.items():
            if arg in MODULE_META_ARGS:
                continue
            line = _line_of(text, rf"^\s*{re.escape(arg)}\s*=", start - 1)
            if arg not in inputs:
                findings.append(Finding("error", "module", f'module "{name}": unknown argument "{arg}"'
                                        f"{suggest(arg, inputs, renames)}", fname, line))
                continue
            attrs, container = inputs[arg].get("attributes"), inputs[arg].get("container")
            if not attrs or not isinstance(value, dict):
                continue
            items = value.items() if container == "map" else [("", value)]
            for key, item in items:
                if not isinstance(item, dict):
                    continue
                for attr in item:
                    if attr not in attrs:
                        path = ".".join(p for p in (arg, key, attr) if p)
                        findings.append(Finding(
                            "error", "attribute", f'module "{name}": {path} is not an attribute of {arg} '
                            f"(terraform silently ignores it){suggest(attr, attrs, renames)}", fname,
                            _line_of(text, rf"^\s*{re.escape(attr)}\s*=", start - 1)))
        for req in (n for n, spec in inputs.items() if spec.get("required")):
            if req not in block:
                findings.append(Finding("error", "module", f'module "{name}": missing required argument "{req}"',
                                        fname, start))
    return findings


def module_source_allowed(source: str) -> bool:
    """Registry sources from the allow-list (a `//subdir` of an allowed package is fine); nothing else."""
    return str(source).strip().removeprefix("registry.terraform.io/").split("//", 1)[0] in ALLOWED_MODULE_SOURCES


def check_sources(cfg: TfConfig, texts: dict[str, str]) -> list[Finding]:
    """Module and provider sources outside the allow-list: `terraform init` must never fetch them."""
    findings: list[Finding] = []
    allowed = ", ".join(ALLOWED_MODULE_SOURCES)
    for fname, (name,), block in cfg.blocks("module", 1):
        source = str(block.get("source", ""))
        if not module_source_allowed(source):
            findings.append(Finding("error", "source", f'module "{name}": blocked module source "{source}" (allowed: '
                                    f"{allowed}); use the blueprint modules", fname,
                                    _line_of(texts.get(fname, ""), rf'^\s*module\s+"{re.escape(name)}"')))
    for name, spec in cfg.required_providers().items():
        source = str((spec.get("source") if isinstance(spec, dict) else None) or f"hashicorp/{name}")
        if not ALLOWED_PROVIDER.match(source):
            findings.append(Finding("error", "source", f'provider "{name}": blocked provider source "{source}" (only '
                                    "hashicorp/* providers are allowed)", "versions.tf" if "versions.tf" in texts
                                    else "", None))
    for fname, text in texts.items():  # files the offline parser could not read: scan every `source = "..."`
        if fname in cfg.docs:
            continue
        for m in re.finditer(r'(?m)^\s*source\s*=\s*"([^"]*)"', _code_only(text)):
            value = m.group(1)
            if not (module_source_allowed(value) or ALLOWED_PROVIDER.match(value)):
                findings.append(Finding("error", "source", f'blocked module source "{value}" (allowed: {allowed} and '
                                        "hashicorp/* providers)", fname, text[:m.start()].count("\n") + 1))
    return findings


def _check_block(schema: dict, block: dict, where: str, fname: str, text: str, start: int) -> list[Finding]:
    findings = []
    known = list(schema.get("arguments", [])) + list(schema.get("blocks", {}))
    for key, value in block.items():
        if key in RESOURCE_META_ARGS or key == "__is_block__":
            continue
        if key not in known:
            findings.append(Finding("error", "resource", f'{where}: unknown argument "{key}"{suggest(key, known)}',
                                    fname, _line_of(text, rf"^\s*{re.escape(key)}\s*(=|\{{)", start - 1)))
        elif key in schema.get("blocks", {}):
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict):
                    findings += _check_block(schema["blocks"][key], item, f"{where}.{key}", fname, text, start)
    for req in schema.get("required", []):
        if req not in block:
            findings.append(Finding("error", "resource", f'{where}: missing required argument "{req}"', fname, start))
    return findings


def check_resources(cfg: TfConfig, texts: dict[str, str]) -> list[Finding]:
    findings, kb = [], knowledge()
    for kind, schemas in [("resource", kb["resources"]), ("data", kb["data_sources"])]:
        for fname, (rtype, rname), block in cfg.blocks(kind, 2):
            schema = schemas.get(rtype)
            if schema is None:
                continue
            prefix = "data." if kind == "data" else ""
            start = _line_of(texts[fname], rf'^\s*{kind}\s+"{rtype}"\s+"{re.escape(rname)}"') or 1
            findings += _check_block(schema, block, f"{prefix}{rtype}.{rname}", fname, texts[fname], start)
    return findings


def check_references(cfg: TfConfig, texts: dict[str, str]) -> list[Finding]:
    findings = []
    variables, local_names, modules = cfg.variables(), cfg.locals(), cfg.modules()
    resources, data = cfg.resources(), cfg.resources("data")
    schemas = knowledge()["modules"]
    for fname, text in texts.items():
        code = _code_only(text)

        def report(message: str, token: str, fname: str = fname, code: str = code) -> None:
            if not any(f.message == message and f.file == fname for f in findings):
                findings.append(Finding("error", "reference", message, fname, _line_of(code, re.escape(token))))

        for m in re.finditer(r"(?<![\w.])var\.([A-Za-z_][\w-]*)", code):
            if m.group(1) not in variables:
                report(f"var.{m.group(1)} is not declared{suggest(m.group(1), variables)}", m.group(0))
        for m in re.finditer(r"(?<![\w.])local\.([A-Za-z_][\w-]*)", code):
            if m.group(1) not in local_names:
                report(f"local.{m.group(1)} is not declared{suggest(m.group(1), local_names)}", m.group(0))
        for m in re.finditer(r"(?<![\w.])module\.([A-Za-z_][\w-]*)(?:\.([A-Za-z_][\w-]*))?", code):
            name, output = m.groups()
            if name not in modules:
                report(f"module.{name} is not declared{suggest(name, modules)}", m.group(0))
                continue
            source = str(modules[name].get("source", "")).removeprefix("registry.terraform.io/")
            outputs = schemas.get(source, {}).get("outputs")
            if output and outputs and output not in outputs:
                report(f'module.{name} has no output "{output}"{suggest(output, outputs)}', m.group(0))
        for m in re.finditer(r"(?<![\w.\"])data\.(aws_\w+)\.([A-Za-z_][\w-]*)", code):
            if (m.group(1), m.group(2)) not in data:
                report(f"data.{m.group(1)}.{m.group(2)} is not declared", m.group(0))
        for m in re.finditer(r"(?<![\w.\"])(aws_[a-z0-9_]+)\.([A-Za-z_][\w-]*)", code):
            if (m.group(1), m.group(2)) not in resources and m.group(1) not in ("aws_region",):
                declared = [f"{t}.{n}" for t, n in resources if t == m.group(1)]
                hint = f"; declared: {', '.join(declared)}" if declared else ""
                report(f"resource {m.group(1)}.{m.group(2)} is not declared{hint}", m.group(0))
    return findings


def is_graviton(instance_type: str) -> bool:
    family = str(instance_type).removeprefix("db.").split(".")[0]
    return family == "a1" or bool(re.match(r"^[a-z]+\d+g[a-z]*$", family))


def check_policy(cfg: TfConfig, texts: dict[str, str]) -> list[Finding]:
    """Security and consistency guardrails that terraform validate cannot see."""
    findings = []

    def err(message: str, fname: str, pattern: str) -> None:
        findings.append(Finding("error", "policy", message, fname, _line_of(texts.get(fname, ""), pattern)))

    for fname, (rtype, rname), block in cfg.blocks("resource", 2):
        r = {k: cfg.resolve(v) for k, v in block.items()}
        if rtype == "aws_db_instance":
            if r.get("storage_encrypted") is not True:
                err(f"{rtype}.{rname}: storage_encrypted must be true (encryption at rest)", fname, "storage_encrypted")
            if r.get("publicly_accessible") is True:
                err(f"{rtype}.{rname}: publicly_accessible must be false (private database)", fname, "publicly_acc")
        if rtype == "aws_ecr_repository":
            if r.get("image_tag_mutability") != "IMMUTABLE" and "${" not in str(r.get("image_tag_mutability", "")):
                err(f"{rtype}.{rname}: image_tag_mutability must be IMMUTABLE", fname, "image_tag_mutability")
            scans = [b.get("scan_on_push") for b in r.get("image_scanning_configuration") or [] if isinstance(b, dict)]
            if True not in scans:
                err(f"{rtype}.{rname}: enable image_scanning_configuration {{ scan_on_push = true }}", fname,
                    rf'"{rtype}"')

    variables = cfg.variables()
    arch = cfg.resolve("${var.node_architecture}") if "node_architecture" in variables else None
    for fname, _, block in cfg.blocks("module", 1):
        groups = block.get("eks_managed_node_groups")
        if not isinstance(groups, dict):
            continue
        for gname, group in groups.items():
            if not isinstance(group, dict):
                continue
            types = cfg.resolve(group.get("instance_types"))
            ami = str(cfg.resolve(group.get("ami_type", "")))
            group_arch = arch if isinstance(arch, str) and "${" not in arch else (
                "arm64" if "ARM_64" in ami else "x86_64" if "x86_64" in ami else None)
            if isinstance(types, list) and group_arch in ("arm64", "x86_64"):
                wrong = [t for t in types if isinstance(t, str) and is_graviton(t) != (group_arch == "arm64")]
                if wrong:
                    err(f"node group {gname}: instance types {wrong} do not match architecture {group_arch} "
                        "(Graviton families end in g, e.g. t4g, m7g)", fname, "instance_types")
            lo, hi = cfg.resolve(group.get("min_size")), cfg.resolve(group.get("max_size"))
            if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
                err(f"node group {gname}: min_size {lo} is greater than max_size {hi}", fname, "min_size")
    az = cfg.resolve("${var.az_count}") if "az_count" in variables else None
    if isinstance(az, int) and az < 2:
        err(f"az_count is {az}; use at least 2 Availability Zones", "variables.tf", 'variable "az_count"')
    return findings


def offline_checks(cfg: TfConfig, texts: dict[str, str], full: bool = True) -> list[Finding]:
    findings = check_duplicates(cfg) + check_modules(cfg, texts) + check_resources(cfg, texts)
    if full:  # reference checks need every file parsed
        findings += check_references(cfg, texts)
    return findings + check_policy(cfg, texts)


# --- Layer 3: terraform binary with a warm cache --------------------------------------------------------------------


def _tf_env() -> dict[str, str]:
    plugins = cache_dir() / "plugin-cache"
    plugins.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TF_PLUGIN_CACHE_DIR": str(plugins), "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "1",
            "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}


def run_tf(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([terraform_bin() or "terraform", *args], check=False, cwd=cwd, env=_tf_env(),
                          capture_output=True, text=True, timeout=timeout)


def signature(cfg: TfConfig) -> str:
    """Hash of everything `terraform init` depends on: module calls and providers."""
    modules = sorted((n, str(b.get("source")), str(b.get("version", ""))) for n, b in cfg.modules().items())
    providers = {k: v for k, v in cfg.required_providers().items()}
    implied = sorted({t.split("_")[0] for t, _ in [*cfg.resources(), *cfg.resources("data")]})
    blob = json.dumps({"modules": modules, "providers": providers, "implied": implied}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def init_dir(workdir: Path, cfg: TfConfig) -> tuple[bool, str]:
    """Make `workdir` validate-ready: link a warm .terraform for this signature, or init and save one."""
    warm = cache_dir() / "warm" / signature(cfg)
    if (warm / ".terraform").is_dir():
        (workdir / ".terraform").symlink_to(warm / ".terraform", target_is_directory=True)
        if (warm / ".terraform.lock.hcl").exists():
            shutil.copy(warm / ".terraform.lock.hcl", workdir / ".terraform.lock.hcl")
        return True, "warm cache"
    proc = run_tf(["init", "-backend=false", "-input=false", "-no-color"], workdir, INIT_TIMEOUT_S)
    if proc.returncode != 0:
        return False, _diagnostics_text(proc.stderr or proc.stdout)
    try:  # save for next time (atomic rename; another process may win the race)
        staging = Path(tempfile.mkdtemp(prefix="warm-", dir=warm.parent if warm.parent.exists() else None))
        shutil.copytree(workdir / ".terraform", staging / ".terraform", symlinks=True)
        if (workdir / ".terraform.lock.hcl").exists():
            shutil.copy(workdir / ".terraform.lock.hcl", staging / ".terraform.lock.hcl")
        warm.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(warm)
    except OSError:
        pass
    return True, "terraform init"


def _diagnostics_text(raw: str) -> str:
    lines = [ln.strip("│╷╵ ").rstrip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:1500]


class TerraformUnavailable(RuntimeError):
    """The binary exists but cannot be used right now (e.g. init without network)."""


def terraform_layer(files: dict[str, str], cfg: TfConfig) -> tuple[list[Finding], dict[str, str], str, list[str]]:
    """Run fmt, init (warm) and validate. Returns (findings, formatted files, validate output, notes)."""
    findings, notes = [], []
    with tempfile.TemporaryDirectory(prefix="tfv-") as tmp:
        workdir = Path(tmp)
        for name, text in files.items():
            (workdir / name).write_text(text)
        fmt = run_tf(["fmt", "-no-color", "-list=true", "."], workdir, 60)
        formatted = {name: (workdir / name).read_text() for name in files} if fmt.returncode == 0 else {}
        if fmt.returncode != 0:
            findings.append(Finding("error", "fmt", "terraform fmt: " + _diagnostics_text(fmt.stderr)[:600]))
            return findings, {}, "", notes
        started = time.time()
        ok, how = init_dir(workdir, cfg)
        if not ok:  # usually no network and no warm cache: degrade to offline instead of failing the run
            raise TerraformUnavailable(f"terraform init failed ({how.splitlines()[0][:200] if how else 'no output'})")
        proc = run_tf(["validate", "-json", "-no-color"], workdir, VALIDATE_TIMEOUT_S)
        if how == "warm cache" and re.search(r"not installed|terraform init|Inconsistent dependency", proc.stdout):
            (workdir / ".terraform").unlink()  # stale warm entry: initialize this directory for real
            (workdir / ".terraform.lock.hcl").unlink(missing_ok=True)
            proc = run_tf(["init", "-backend=false", "-input=false", "-no-color"], workdir, INIT_TIMEOUT_S)
            how = "terraform init (retry)"
            proc = run_tf(["validate", "-json", "-no-color"], workdir, VALIDATE_TIMEOUT_S)
        notes.append(f"terraform validate via {how} in {time.time() - started:.1f}s")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            findings.append(Finding("error", "terraform", _diagnostics_text(proc.stderr or proc.stdout)))
            return findings, formatted, proc.stderr, notes
        for d in result.get("diagnostics", []):
            rng = d.get("range") or {}
            message = d.get("summary", "")
            if d.get("detail"):
                message += f": {d['detail'].splitlines()[0]}"
            findings.append(Finding(d.get("severity", "error"), "terraform", message, rng.get("filename", ""),
                                    (rng.get("start") or {}).get("line")))
        output = "Success! The configuration is valid." if result.get("valid") else (
            f"terraform validate: {result.get('error_count', 0)} error(s)")
        return findings, formatted, output, notes


# --- Entry point ----------------------------------------------------------------------------------------------------


def validate(files: dict[str, str], offline: bool | None = None) -> Report:
    """Validate {file name: text} for one Terraform root module (only *.tf files are considered)."""
    started = time.time()
    tf_files = {n: t for n, t in files.items() if n.endswith(".tf")}
    use_tf = bool(terraform_bin()) and not offline
    report = Report(valid=False, level="terraform" if use_tf else "offline")
    if not tf_files:
        report.findings.append(Finding("error", "parse", "no .tf files found; call get_blueprint first"))
        return report
    cfg, parse_errors = parse_all(tf_files)
    report.findings += parse_errors
    blocked = check_sources(cfg, tf_files)
    report.findings += blocked
    if blocked and use_tf:
        use_tf, report.level = False, "offline"
        report.notes.append("terraform init/validate skipped: blocked module source outside the allow-list (init "
                            "would download and run it); offline checks only")
    if use_tf and parse_errors:
        # python-hcl2 is stricter than terraform on a few constructs: let terraform fmt decide.
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in tf_files.items():
                (Path(tmp) / name).write_text(text)
            fmt = run_tf(["fmt", "-no-color", "-check", "."], Path(tmp), 60)
        if fmt.returncode in (0, 3) and not fmt.stderr.strip():
            for f in parse_errors:
                f.severity = "warning"
                f.message += " (offline parser only; terraform parses it)"
    texts = {n: tf_files[n] for n in cfg.docs}
    offline_findings = offline_checks(cfg, texts, full=not parse_errors)
    tf_output = ""
    if use_tf and not report.errors:
        try:
            tf_findings, report.formatted, tf_output, report.notes = terraform_layer(tf_files, cfg)
        except (TerraformUnavailable, subprocess.TimeoutExpired, OSError) as exc:
            tf_findings = []
            report.level = "offline"
            report.notes.append(f"terraform unavailable ({exc}); fell back to offline checks")
        tf_valid = not any(f.severity == "error" for f in tf_findings)
        if report.level == "terraform" and tf_valid:
            # terraform is authoritative for what it checks; keep the checks it cannot do.
            for f in offline_findings:
                if f.severity == "error" and f.check in ("module", "resource", "reference"):
                    f.severity = "warning"
        # offline findings carry did-you-mean hints: drop terraform duplicates on the same line
        seen = {(f.file, f.line) for f in offline_findings if f.severity == "error"}
        report.findings += offline_findings
        report.findings += [f for f in tf_findings if (f.file, f.line) not in seen or f.severity != "error"]
    else:
        report.findings += offline_findings
    report.valid = not report.errors
    report.terraform_output = tf_output or ("Offline validation passed: HCL parse, module/resource arguments, "
                                            "references and policy checks." if report.valid else "")
    report.seconds = time.time() - started
    return report


def warmup() -> list[str]:
    """Pre-download modules/providers and validate the blueprint twice (cold, then warm). Run before the talk."""
    log = []
    if not terraform_bin():
        return ["terraform binary not found: validation will run offline (layers 1 and 2 only)."]
    for label in ("first run", "warm run"):
        report = validate(blueprint_files())
        log.append(f"{label}: {report.text()}")
    return log
