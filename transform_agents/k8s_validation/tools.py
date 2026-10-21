"""Deterministic validation session plus the Strands tools the k8s_validation agent calls.

The tools do all the checking (kustomize, kubeconform, policy baseline, kind deploy); the LLM only decides
which small edit_file patches to make, re-runs the checks, and submits. Results are cached by a content
fingerprint, so calling a check twice without edits is free.
"""

import hashlib
import shutil
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from strands import tool

from transform_agents.core import ResultBox, Workspace

from .deploy import apply_and_verify, build_and_load, pick_target, unavailable_reason
from .kube import Rendered, SchemaResult, find_targets, load_docs, render, schema_check
from .policy import RULES, SEVERITY_ORDER, Finding, check_policies, exempt_workloads

MAX_TOOL_CALLS = 30  # soft budget: after this the domain tools ask the model to submit
MAX_DEPLOYS = 3
BUILD_WAIT_S = 150  # max wait for the background docker build + kind load
NEW_FILE_RULES = {"POL-011": "pdb.yaml", "POL-012": "hpa.yaml"}  # rules fixed by adding a manifest
WRITABLE_PREFIX = "k8s/"  # the only tree this agent may change: microservice/** belongs to modernization
DEFAULT_RESOURCES = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}
CREATES_RESOLVE = {"PodDisruptionBudget": ["POL-011", "POL-016"], "HorizontalPodAutoscaler": ["POL-012", "POL-010"]}


@dataclass
class TargetCheck:
    rendered: Rendered
    schema: SchemaResult
    findings: list[Finding]
    exempt: list[str]


@dataclass
class Validation:
    fingerprint: str
    targets: dict[str, TargetCheck] = field(default_factory=dict)

    def grouped(self) -> list[tuple[Finding, list[str]]]:
        """Findings deduplicated across targets: (finding, [targets where it occurs])."""
        groups: dict[tuple, tuple[Finding, list[str]]] = {}
        for name, tc in self.targets.items():
            for f in tc.findings:
                groups.setdefault(f.key(), (f, []))[1].append(name)
        return sorted(groups.values(), key=lambda g: (g[0].status != "fail", SEVERITY_ORDER[g[0].severity], g[0].id))

    def failing_rules(self) -> set[str]:
        return {f.id for f, _ in self.grouped() if f.status == "fail"}

    def failing_keys(self) -> set[tuple]:
        return {f.key() for f, _ in self.grouped() if f.status == "fail"}

    @property
    def render_ok(self) -> bool:
        return bool(self.targets) and all(tc.rendered.status != "fail" for tc in self.targets.values())

    @property
    def schema_ok(self) -> bool:
        return all(tc.schema.status != "fail" for tc in self.targets.values())

    @property
    def ok(self) -> bool:
        return self.render_ok and self.schema_ok and not self.failing_rules()


class ValidationSession:
    """Everything one k8s_validation run knows: workspace, options, cached checks, deploy state, tool budget."""

    def __init__(self, ws: Workspace, inputs: dict | None = None, options: dict | None = None):
        inputs, options = inputs or {}, options or {}
        spec, mod = inputs.get("spec") or {}, inputs.get("modernization") or {}
        image, workload = mod.get("image") or {}, spec.get("workload") or {}
        self.ws = ws
        self.original = dict(ws.files)
        self.live_deploy = bool(options.get("live_deploy"))
        self.cluster = str(options.get("kind_cluster") or "transform-workshop")
        self.replicas_min = int(workload.get("replicas_min") or 2)
        self.app_names = {n for n in (mod.get("service_name"), (spec.get("service") or {}).get("name")) if n}
        self.repository = image.get("repository")
        self.build_context = str(image.get("context") or "microservice")
        self.dockerfile = str(image.get("dockerfile") or f"{self.build_context}/Dockerfile")
        endpoints = mod.get("endpoints") or {}
        self.health_path = endpoints.get("health") or workload.get("health_path") or "/healthz"
        self.ready_path = endpoints.get("readiness") or workload.get("readiness_path") or "/readyz"
        self.feedback = ""  # reviewer feedback on a revision run (set by the agent)
        self.previous_fixes = list((inputs.get("previous") or {}).get("fixes_applied") or [])  # revision runs
        resources = mod.get("resources") if isinstance(mod.get("resources"), dict) else {}
        self.resources = {k: {**DEFAULT_RESOURCES[k], **(resources.get(k) or {})} for k in DEFAULT_RESOURCES}
        self.drift: list[dict] = []  # SIMULATED drift injected before the first check (options.simulate_drift)
        self.pristine: dict[str, str] = {}  # the k8s/** files as received, before the simulated drift
        self.first: Validation | None = None
        self.deploy_result: dict | None = None
        self.deploy_fingerprint = ""
        self.deploys = 0
        self.tool_calls = 0
        self.check_runs = 0
        self.fix_notes: list[dict] = []
        self.notes = ""
        self.submit_refusals = 0
        self._cache: dict[str, Validation] = {}
        self._lock = threading.Lock()
        self._deploy_lock = threading.Lock()
        self._image: tuple[str, Future] | None = None

    # --- fingerprints and sources --------------------------------------------------------------------------------

    def files(self, prefix: str = "") -> dict[str, str]:
        """Atomic snapshot of workspace files: tools run in parallel threads and may edit concurrently."""
        snapshot = dict(self.ws.files)
        return {p: c for p, c in sorted(snapshot.items()) if p.startswith(prefix)}

    def materialize(self, prefix: str) -> Path:
        return Workspace(self.files(prefix.rstrip("/") + "/")).materialize(prefix)

    def fingerprint(self, *prefixes: str) -> str:
        digest = hashlib.sha256()
        for path, content in self.files().items():
            if path.startswith(prefixes or ("k8s/",)):
                digest.update(path.encode() + b"\0" + content.encode() + b"\0")
        return digest.hexdigest()[:16]

    def has_manifests(self) -> bool:
        return any(p.endswith((".yaml", ".yml")) for p in self.files("k8s/"))

    def source_index(self) -> dict[tuple[str, str], list[str]]:
        """(kind, name) -> workspace files defining or patching it."""
        index: dict[tuple[str, str], list[str]] = {}
        for path, content in self.files("k8s/").items():
            if not path.endswith((".yaml", ".yml")):
                continue
            try:
                for doc in yaml.safe_load_all(content):
                    meta = doc.get("metadata") if isinstance(doc, dict) else None
                    if isinstance(meta, dict) and doc.get("kind") and meta.get("name"):
                        index.setdefault((str(doc["kind"]), str(meta["name"])), []).append(path)
            except yaml.YAMLError:
                continue
        return index

    def edit_files(self, finding: Finding, targets: list[str], index: dict | None = None) -> list[str]:
        """Which workspace files the agent should edit to fix a finding."""
        index = index if index is not None else self.source_index()
        manifests = self.files("k8s/")
        kind, _, name = finding.resource.partition("/")
        files = list(index.get((kind, name), []))
        in_base = "base" in targets or len(targets) > 1
        scope = [f for f in files if f.startswith("k8s/base/")] if in_base else \
            [f for f in files if any(f.startswith(f"k8s/{t}/") for t in targets)]
        files = scope or files
        if finding.id in ("POL-011", "POL-012"):  # resource missing: it must be added to a kustomization
            folder = files[0].rsplit("/", 1)[0] if files else "k8s/base"
            files = [p for p in manifests if p.rsplit("/", 1)[0] == folder and "/kustomization." in "/" + p]
        if finding.id == "POL-009":
            image_name = finding.message.split("`")[1].rsplit(":", 1)[0] if "`" in finding.message else ""
            files += [p for p, text in manifests.items() if "kustomization." in p and image_name
                      and image_name in text and any(p.startswith(f"k8s/{t}/") for t in targets)]
        return list(dict.fromkeys(files))

    # --- validation ----------------------------------------------------------------------------------------------

    def validate(self) -> Validation:
        """Render every Kustomize target, then schema-check and policy-check it. Cached by content."""
        with self._lock:
            fp = self.fingerprint()
            if fp in self._cache:
                return self._cache[fp]
            root = self.materialize("k8s")
            try:
                v = Validation(fp)
                renders = [render(root, t) for t in find_targets(root)] or [self._raw_all()]
                for r in renders:
                    docs = r.docs or []
                    v.targets[r.target] = TargetCheck(r, schema_check(r), check_policies(docs, r.target,
                                                                                         self.replicas_min),
                                                      exempt_workloads(docs))
            finally:
                shutil.rmtree(root, ignore_errors=True)
            self._cache[fp] = v
            self.first = self.first or v
            return v

    def _raw_all(self) -> Rendered:
        docs: list = []
        for path, content in self.files("k8s/").items():
            if path.endswith((".yaml", ".yml")) and "kustomization." not in path:
                try:
                    docs += load_docs(content)
                except yaml.YAMLError:
                    continue
        return Rendered("k8s", "raw", "warn", "no kustomization.yaml found; validated raw files",
                        yaml.safe_dump_all(docs, sort_keys=False), docs)

    def local_overlay(self, v: Validation) -> str | None:
        overlays = [t for t in v.targets if t.startswith("overlays/")]
        return "overlays/local" if "overlays/local" in overlays else (overlays[0] if overlays else None)

    # --- live deploy ---------------------------------------------------------------------------------------------

    def start_image_build(self) -> None:
        """Build and kind-load the image in the background while the agent works on the manifests."""
        v = self.validate()
        overlay = self.local_overlay(v)
        target = pick_target(v.targets[overlay].rendered.docs or [], self.app_names, self.repository) \
            if overlay else None
        if target and target.image:
            self._image_future(target.image)

    def _image_future(self, image: str) -> Future:
        key = f"{image}|{self.fingerprint(self.build_context + '/')}"
        if self._image and self._image[0] == key:
            return self._image[1]
        future: Future = Future()
        context = self.build_context
        sources = Workspace(self.files(context + "/"))  # snapshot now; the agent may keep editing k8s/

        def work() -> None:
            try:
                reason = unavailable_reason(self.cluster)
                if reason:
                    future.set_result({"ok": False, "skipped": True, "error": reason})
                    return
                root = sources.materialize(context)
                try:
                    dockerfile = root / Path(self.dockerfile).relative_to(context) \
                        if self.dockerfile.startswith(context + "/") else root / "Dockerfile"
                    future.set_result(build_and_load(root, dockerfile, image, self.cluster))
                finally:
                    shutil.rmtree(root, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001 - never let a background thread die silently
                future.set_result({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        threading.Thread(target=work, name="k8s-validation-image-build", daemon=True).start()
        self._image = (key, future)
        return future

    def deploy(self, budget_s: float = 75) -> dict:
        """Deploy the local overlay to kind and smoke test it. Always returns a contract `live_deploy` dict.

        Serialized and cached: a second call without manifest or code changes returns the previous result.
        """
        with self._deploy_lock:
            if self.deploy_result is not None and not self.deploy_stale:
                return self.deploy_result
            return self._deploy(budget_s)

    def _deploy(self, budget_s: float) -> dict:
        started = time.monotonic()
        result: dict[str, Any] = {"status": "skipped", "cluster": f"kind-{self.cluster}", "image": "",
                                  "rollout": "", "smoke_tests": []}
        v = self.validate()
        overlay = self.local_overlay(v)
        if overlay is None:
            return self._record({**result, "reason": "no k8s/overlays/<name> to deploy"})
        if v.targets[overlay].rendered.status == "fail":
            return self._record({**result, "status": "fail", "reason": f"{overlay} does not render"})
        target = pick_target(v.targets[overlay].rendered.docs or [], self.app_names, self.repository)
        if target is None:
            return self._record({**result, "status": "fail", "reason": f"no Deployment found in {overlay}"})
        result.update(image=target.image, namespace=target.namespace, overlay=overlay)
        try:
            built = self._image_future(target.image).result(timeout=BUILD_WAIT_S)
        except FutureTimeout:
            built = {"ok": False, "error": f"image build/load still running after {BUILD_WAIT_S}s"}
        if not built.get("ok"):
            status = "skipped" if built.get("skipped") else "fail"
            return self._record({**result, "status": status, "reason": built.get("error", "image build failed")})
        root = self.materialize("k8s")
        try:
            remaining = budget_s - (time.monotonic() - started)
            outcome = apply_and_verify(root / overlay, target, self.cluster, built.get("image_id", ""),
                                       max(remaining, 30), self.health_path, self.ready_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        timings = {"build_s": built.get("build_s"), "load_s": built.get("load_s"), "apply_s": outcome.pop("apply_s", 0),
                   "rollout_s": outcome.pop("rollout_s", 0), "smoke_s": outcome.pop("smoke_s", 0),
                   "total_s": round(time.monotonic() - started, 1)}
        svc = target.service or target.deployment
        port = target.service_port or target.container_port
        access = (f"kubectl --context kind-{self.cluster} -n {target.namespace} port-forward "
                  f"{'svc' if target.service else 'deployment'}/{svc} 8080:{port}")
        return self._record({**result, **outcome, "timings": timings, "access": access})

    def _record(self, result: dict) -> dict:
        self.deploys += 1
        self.deploy_result = result
        self.deploy_fingerprint = self.fingerprint("k8s/", self.build_context + "/")
        return result

    @property
    def deploy_stale(self) -> bool:
        return self.deploy_result is not None and self.deploy_fingerprint != self.fingerprint(
            "k8s/", self.build_context + "/")

    @property
    def over_budget(self) -> bool:
        return self.tool_calls >= MAX_TOOL_CALLS


# --- tool output formatting (shared by the tools and the preflight prompt) -------------------------------------

def format_schema(v: Validation) -> str:
    lines = []
    for name, tc in v.targets.items():
        r, s = tc.rendered, tc.schema
        if r.status == "fail":
            lines.append(f"RENDER FAIL {name}: {r.details}")
        elif r.status == "warn":
            lines.append(f"render warn {name}: {r.details}")
        lines.append(f"{s.status.upper()} {name} ({s.tool}): {s.details}")
        lines += [f"  INVALID {ref}: {msg[:300]}" for ref, msg in s.invalid]
    overall = "PASS" if v.render_ok and v.schema_ok else "FAIL"
    return f"kubeconform_check: {overall} (strict schema validation of the rendered Kustomize output)\n" + \
        "\n".join(lines)


def format_policy(session: ValidationSession, v: Validation) -> str:
    groups = v.grouped()
    failing = [g for g in groups if g[0].status == "fail"]
    index = session.source_index()
    passed = len(RULES) - len({f.id for f, _ in failing})
    lines = [(f"policy_check: {'PASS' if not failing else 'FAIL'} - {len(failing)} failing findings, "
              f"{passed}/{len(RULES)} rules pass (targets: {', '.join(v.targets) or 'none'})")]
    for f, targets in groups:
        container = f" container={f.container}" if f.container else ""
        lines.append(f"{f.status.upper()} {f.id} {f.rule.title} [{f.severity}] {f.resource}{container} "
                     f"(in: {', '.join(targets)})")
        lines.append(f"  problem: {f.message}")
        if f.status != "fail":
            continue
        files = session.edit_files(f, targets, index)
        if f.id in NEW_FILE_RULES and files:
            kustomization = files[0]
            new_file = f"{kustomization.rsplit('/', 1)[0]}/{NEW_FILE_RULES[f.id]}"
            listed = NEW_FILE_RULES[f.id] in session.ws.files.get(kustomization, "")
            if new_file not in session.ws.files or listed:
                lines.append(f"  fix: write_file {new_file} with this content:\n```yaml\n{f.fix.rstrip()}\n```")
            if not listed:
                lines.append(f"  fix: edit_file {kustomization}: add `- {NEW_FILE_RULES[f.id]}` to the resources: list")
        else:
            lines.append(f"  edit: {', '.join(files) or 'k8s/base/'}")
            fix = (f.fix or _resources_fix(session, f) or f.rule.hint).rstrip()
            lines.append(f"  fix: {fix}" if "\n" not in fix else f"  fix:\n```yaml\n{fix}\n```")
    exempt = sorted({w for tc in v.targets.values() for w in tc.exempt})
    if exempt:
        lines.append(f"exempt (dev-only datastore, baseline not enforced): {', '.join(exempt)}")
    return "\n".join(lines)


def _resources_fix(session: ValidationSession, f) -> str:
    """POL-001: the exact keys to add, with the values modernization declared (or safe defaults)."""
    if f.id != "POL-001" or "missing resources:" not in f.message:
        return ""
    missing = [m.strip() for m in f.message.split(":", 1)[1].split(",")]
    blocks = []
    for kind in ("requests", "limits"):
        keys = [m.split(".", 1)[1] for m in missing if m.startswith(kind + ".")]
        if keys:
            blocks.append(f"{kind}:\n" + "\n".join(f"  {k}: {session.resources[kind][k]}" for k in keys))
    where = f"container {f.container}" if f.container else "the container"
    return f"# {where}: add under resources: (same indentation as the keys already there)\n" + "\n".join(blocks)


def write_tools(ws: Workspace) -> list:
    """write_file / edit_file restricted to k8s/: this stage must never override modernization's code."""

    def check(path: str) -> tuple[str, str]:
        try:
            norm = ws.normalize(path)
        except ValueError as exc:
            return "", f"error: {exc}"
        if not norm.startswith(WRITABLE_PREFIX):
            return "", (f"error: {norm} is outside {WRITABLE_PREFIX}; this agent may only change Kubernetes "
                        "manifests under k8s/ (microservice/** belongs to the modernization stage)")
        return norm, ""

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a manifest under k8s/. Always send the complete file content.

        Args:
            path: Relative path under k8s/ (e.g. "k8s/base/pdb.yaml").
            content: Full file content.
        """
        norm, problem = check(path)
        if problem:
            return problem
        ws.write(norm, content)
        return f"wrote {norm} ({len(content)} bytes)"

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace one exact, unique snippet in an existing manifest under k8s/.

        Args:
            path: Relative path under k8s/.
            old: Exact text to replace; must appear exactly once.
            new: Replacement text.
        """
        norm, problem = check(path)
        if problem:
            return problem
        with ws.lock:
            current = ws.files.get(norm)
            if current is None:
                return f"error: {norm} not found"
            count = current.count(old)
            if count != 1:
                return f"error: snippet found {count} times in {norm}; include more surrounding context so it is unique"
            ws.write(norm, current.replace(old, new))
        return f"edited {norm}"

    return [write_file, edit_file]


def format_deploy(result: dict) -> str:
    lines = [f"deploy_to_kind: {result['status'].upper()} cluster={result['cluster']} image={result.get('image')}"]
    if result.get("reason"):
        lines.append(f"reason: {result['reason']}")
    if result.get("rollout"):
        lines.append(f"rollout: {result['rollout']}")
    for t in result.get("smoke_tests") or []:
        lines.append(f"  {'ok  ' if t['passed'] else 'FAIL'} {t['request']} -> {t['status']} (expected {t['expected']})")
    if result.get("timings"):
        lines.append("timings: " + ", ".join(f"{k}={v}" for k, v in result["timings"].items() if v is not None))
    if result.get("diagnostics"):
        lines.append("diagnostics:\n" + result["diagnostics"])
    return "\n".join(lines)


# --- Strands tools --------------------------------------------------------------------------------------------

def validation_tools(session: ValidationSession, box: ResultBox) -> list:
    """Domain tools bound to one session. Pair them with core.workspace_tools(ws) for read/edit."""
    budget_msg = "Tool budget exhausted. Call submit_result now with what you have."

    @tool
    def render_manifests(target: str = "overlays/local") -> str:
        """Render a Kustomize target (`kubectl kustomize k8s/<target>`) and return the YAML.

        Args:
            target: "base" or "overlays/<name>", e.g. "overlays/local".
        """
        v = session.validate()
        tc = v.targets.get(target.strip().removeprefix("k8s/").strip("/"))
        if tc is None:
            return f"error: unknown target {target!r}; available: {', '.join(v.targets)}"
        text = tc.rendered.text
        head = f"# {tc.rendered.tool} {tc.rendered.status}: {tc.rendered.details}\n"
        return head + (text if len(text) < 12000 else text[:12000] + "\n# ... (truncated)")

    @tool
    def kubeconform_check() -> str:
        """Render every Kustomize target and validate it against the Kubernetes JSON schemas (kubeconform -strict)."""
        if session.over_budget:
            return budget_msg
        session.check_runs += 1
        return format_schema(session.validate())

    @tool
    def policy_check() -> str:
        """Check the rendered manifests against the policy baseline (TC5/SEC4). Lists each failure with the file
        to edit and a fix snippet."""
        if session.over_budget:
            return budget_msg
        session.check_runs += 1
        return format_policy(session, session.validate())

    @tool
    def deploy_to_kind() -> str:
        """Build the image, load it into the local kind cluster, apply k8s/overlays/local, wait for the rollout
        and run HTTP smoke tests (health, readiness, list items, order, oversell -> 409). Takes up to ~60 s."""
        if session.deploys >= MAX_DEPLOYS:
            return "deploy limit reached; call submit_result now"
        return format_deploy(session.deploy())

    @tool
    def submit_result(summary: str, fixes: list[str] | None = None, notes: str = "") -> str:
        """Submit the validation result. Call once, at the very end. The report and check tables are generated
        from the deterministic check results.

        Args:
            summary: 2-4 sentences for the reviewer: what was checked, what you fixed, what remains.
            fixes: One entry per change, formatted "<file>: <what changed>", e.g.
                "k8s/base/deployment.yaml: added readOnlyRootFilesystem: true".
            notes: Optional open issues or follow-ups.
        """
        v = session.validate()
        problems = []
        if not v.ok:
            problems.append(f"checks still fail ({', '.join(sorted(v.failing_rules())) or 'render/schema'})")
        if session.feedback and session.files("k8s/") == {p: c for p, c in session.original.items()
                                                           if p.startswith("k8s/")}:
            problems.append("the reviewer feedback was not applied (no k8s/ file changed)")
        if session.live_deploy and (session.deploy_result is None or session.deploy_stale):
            problems.append("live deploy was requested: call deploy_to_kind " +
                            ("again (manifests changed since the last deploy)" if session.deploy_stale else "first"))
        if problems and session.submit_refusals == 0 and not session.over_budget:
            session.submit_refusals += 1
            return "not submitted yet: " + "; ".join(problems) + \
                ". Fix it, or call submit_result again to submit as-is and explain in notes."
        entries = fixes if isinstance(fixes, list) else str(fixes or "").splitlines()
        for entry in entries:
            file, sep, change = str(entry).partition(":")
            session.fix_notes.append({"file": file.strip() if sep else "?", "change": (change if sep else file).strip()})
        session.notes = notes
        return box.set(summary, {"submitted": True})

    tools = [render_manifests, kubeconform_check, policy_check, submit_result]
    if session.live_deploy:
        tools.insert(3, deploy_to_kind)
    return tools
