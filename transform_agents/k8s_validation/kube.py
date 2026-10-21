"""Thin, timeout-guarded wrappers around kubectl kustomize and kubeconform, with offline fallbacks."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .policy import _d, _l, builtin_schema_check, parse_image

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_BIN = REPO_ROOT / ".tools" / "bin"


def find_binary(name: str) -> str | None:
    """Prefer the repo-local pinned tool (scripts/install-tools.sh), then PATH."""
    local = TOOLS_BIN / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


@dataclass
class Proc:
    ok: bool
    code: int
    out: str
    err: str
    seconds: float

    def tail(self, lines: int = 15) -> str:
        text = (self.err.strip() or self.out.strip())
        return "\n".join(text.splitlines()[-lines:])


def run(cmd: list[str], timeout: float, stdin: str | None = None, cwd: Path | None = None) -> Proc:
    """Run a command with a hard timeout. Never raises: failures come back as ok=False."""
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=max(timeout, 1), cwd=cwd,
                           check=False)
        return Proc(p.returncode == 0, p.returncode, p.stdout, p.stderr, round(time.monotonic() - started, 1))
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return Proc(False, -1, out, f"timed out after {timeout:.0f}s: {' '.join(cmd[:4])}",
                    round(time.monotonic() - started, 1))
    except OSError as exc:
        return Proc(False, -1, "", f"{type(exc).__name__}: {exc}", 0.0)


def load_docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d is not None]


# --- rendering ---------------------------------------------------------------------------------------------------

@dataclass
class Rendered:
    target: str
    tool: str  # kustomize | raw
    status: str  # pass | warn | fail
    details: str
    text: str = ""
    docs: list | None = None


def render(k8s_root: Path, target: str, timeout: float = 30) -> Rendered:
    """`kubectl kustomize k8s/<target>`; falls back to a raw read of the kustomization's resources."""
    kubectl = find_binary("kubectl")
    if kubectl:
        p = run([kubectl, "kustomize", str(k8s_root / target)], timeout)
        if p.ok:
            try:
                docs = load_docs(p.out)
                return Rendered(target, "kustomize", "pass", f"{len(docs)} resources", p.out, docs)
            except yaml.YAMLError as exc:
                return Rendered(target, "kustomize", "fail", f"rendered YAML does not parse: {exc}", p.out, [])
        error = p.tail(8).replace(str(k8s_root) + "/", "k8s/")
        raw = raw_render(k8s_root, target)
        return Rendered(target, "kustomize", "fail", f"kustomize failed: {error}", raw[0], raw[1])
    text, docs, note = raw_render(k8s_root, target)
    return Rendered(target, "raw", "warn", f"kubectl not found; raw resources without patches ({note})", text, docs)


def raw_render(k8s_root: Path, target: str) -> tuple[str, list, str]:
    """Best-effort mini-kustomize: resources, namespace and images only (patches are not applied)."""
    docs: list = []
    notes: list[str] = []
    root = k8s_root.resolve()

    def walk(directory: Path, namespace: str | None, images: list) -> None:
        kfile = next((directory / n for n in ("kustomization.yaml", "kustomization.yml", "Kustomization")
                      if (directory / n).is_file()), None)
        if kfile is None:
            notes.append(f"no kustomization in {directory.name}")
            return
        try:
            kust = _d(yaml.safe_load(kfile.read_text()))
        except yaml.YAMLError as exc:
            notes.append(f"{kfile.name}: {exc}")
            return
        ns = namespace or kust.get("namespace")
        imgs = [i for i in _l(kust.get("images")) if isinstance(i, dict)] + images
        for entry in _l(kust.get("resources") or kust.get("bases")):
            path = (directory / str(entry)).resolve()
            if not path.is_relative_to(root):  # never read outside the materialized k8s/ tree (e.g. ~/.kube/config)
                notes.append(f"{entry}: outside k8s/, skipped")
                continue
            if path.is_dir():
                walk(path, ns, imgs)
            elif path.is_file():
                try:
                    for doc in load_docs(path.read_text()):
                        docs.append(_apply_kustomize_basics(doc, ns, imgs))
                except yaml.YAMLError as exc:
                    notes.append(f"{entry}: {exc}")
        if kust.get("patches") or kust.get("patchesStrategicMerge"):
            notes.append("patches skipped")

    start = (k8s_root / target).resolve()
    if start.is_relative_to(root):
        walk(start, None, [])
    else:
        notes.append(f"{target}: outside k8s/, skipped")
    return yaml.safe_dump_all(docs, sort_keys=False), docs, "; ".join(notes) or "ok"


def _apply_kustomize_basics(doc: dict, namespace: str | None, images: list) -> dict:
    if not isinstance(doc, dict):
        return doc
    if namespace and doc.get("kind") != "Namespace" and isinstance(doc.setdefault("metadata", {}), dict):
        doc["metadata"]["namespace"] = namespace
    spec = _d(doc.get("spec"))
    template = _d(spec.get("template") or _d(_d(spec.get("jobTemplate")).get("spec")).get("template"))
    for c in [c for c in _l(_d(template.get("spec")).get("containers")) if isinstance(c, dict)]:
        name, tag, _ = parse_image(str(c.get("image", "")))
        for img in images:  # outermost kustomization wins, like kustomize
            if img.get("name") == name:
                new_name = img.get("newName", name)
                c["image"] = f"{new_name}@{img['digest']}" if img.get("digest") else f"{new_name}:{img.get('newTag', tag)}"
    return doc


def find_targets(k8s_root: Path) -> list[str]:
    """Kustomize targets: base plus every overlay with a kustomization file."""
    names = ("kustomization.yaml", "kustomization.yml", "Kustomization")
    targets = ["base"] if any((k8s_root / "base" / n).is_file() for n in names) else []
    overlays = k8s_root / "overlays"
    if overlays.is_dir():
        targets += [f"overlays/{d.name}" for d in sorted(overlays.iterdir())
                    if d.is_dir() and any((d / n).is_file() for n in names)]
    return targets


# --- schema validation -------------------------------------------------------------------------------------------

@dataclass
class SchemaResult:
    target: str
    tool: str  # kubeconform | builtin
    status: str  # pass | fail | warn | skipped
    details: str
    invalid: list[tuple[str, str]]  # (Kind/name, message)


def kubeconform_cache() -> str:
    base = REPO_ROOT / ".tools" if (REPO_ROOT / ".tools").is_dir() else Path(tempfile.gettempdir())
    cache = base / "kubeconform-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return str(cache)


def schema_check(rendered: Rendered, timeout: float = 25) -> SchemaResult:
    """kubeconform -strict -summary on rendered output; builtin structural checks when offline or missing."""
    target, docs = rendered.target, rendered.docs or []
    if not docs:
        return SchemaResult(target, "builtin", "skipped", "nothing rendered", [])
    binary = find_binary("kubeconform")
    if binary:
        p = run([binary, "-strict", "-summary", "-ignore-missing-schemas", "-output", "json", "-verbose",
                 "-cache", kubeconform_cache(), "-"], timeout, stdin=rendered.text)
        parsed = parse_kubeconform(p.out)
        if parsed is not None:
            summary, invalid, errors = parsed
            if not errors:
                status = "fail" if invalid else "pass"
                details = (f"{summary.get('valid', 0)} valid, {summary.get('invalid', 0)} invalid, "
                           f"{summary.get('skipped', 0)} skipped (no schema, e.g. CRDs)")
                return SchemaResult(target, "kubeconform", status, details, invalid)
            reason = f"kubeconform could not fetch schemas ({errors[0][1][:120]})"
        else:
            reason = f"kubeconform failed: {p.tail(3)}"
    else:
        reason = "kubeconform not installed (scripts/install-tools.sh)"
    problems = builtin_schema_check(docs)
    status = "fail" if problems else "warn"
    return SchemaResult(target, "builtin", status, f"{reason}; offline structural checks: "
                        f"{len(docs) - len({r for r, _ in problems})}/{len(docs)} ok", problems)


def parse_kubeconform(output: str) -> tuple[dict, list[tuple[str, str]], list[tuple[str, str]]] | None:
    """Parse `kubeconform -output json -verbose` into (summary, invalid, errors)."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    invalid, errors = [], []
    for res in data.get("resources") or []:
        ref = f"{res.get('kind')}/{res.get('name')}"
        if res.get("status") == "statusInvalid":
            details = "; ".join(f"{e.get('path')}: {e.get('msg')}" for e in res.get("validationErrors") or [])
            invalid.append((ref, details or str(res.get("msg", ""))[-200:]))
        elif res.get("status") == "statusError":
            msg = str(res.get("msg", ""))
            # Schema download problems mean "offline"; anything else (e.g. unparsable resource) is a real failure.
            (errors if "schema" in msg and ("download" in msg or "could not find" in msg) else invalid).append((ref, msg))
    return data.get("summary") or {}, invalid, errors
