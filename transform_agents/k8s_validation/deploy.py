"""Live deploy to the local kind cluster: docker build, kind load, kubectl apply -k, rollout status, smoke tests.

Every step has a timeout and the whole deploy shares one time budget. Re-runs are idempotent: the namespace is kept
for the audience, and pods are restarted only when the freshly built image differs from the running one.
"""

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .kube import find_binary, run
from .policy import Workload, _d, _l, _name, _ns, _ports, parse_image, workloads

IMAGE_ID_ANNOTATION = "transform-workshop/image-id"
FATAL_REASONS = {"CrashLoopBackOff", "ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName",
                 "CreateContainerConfigError", "CreateContainerError", "RunContainerError"}


@dataclass
class AppTarget:
    """What to deploy and probe, derived from the rendered local overlay."""
    namespace: str
    deployment: str
    image: str
    pod_labels: dict[str, str]
    service: str | None = None
    service_port: int | str | None = None
    container_port: int | None = None
    rollouts: list[str] = field(default_factory=list)  # e.g. ["deployment/inventory-service"]


def pick_target(docs: list[dict], app_names: set[str] | None = None, repository: str | None = None) -> AppTarget | None:
    """Choose the app Deployment (by service name, then image repository, then first non-datastore Deployment)."""
    deps = [w for w in workloads(docs) if w.kind == "Deployment" and not w.dev_datastore and w.containers(False)]
    if not deps:
        return None

    def score(w: Workload) -> int:
        image_repo = parse_image(str(w.containers(False)[0].get("image", "")))[0].rsplit("/", 1)[-1]
        return (2 if w.name in (app_names or set()) else 0) + (1 if repository and image_repo == repository else 0)

    app = max(deps, key=score)
    main = app.containers(False)[0]
    target = AppTarget(app.namespace, app.name, str(main.get("image", "")), app.pod_labels,
                       container_port=next((p.get("containerPort") for p in _ports(main)), None))
    for doc in docs:
        spec = _d(doc.get("spec"))
        selector = _d(spec.get("selector"))
        ports = [p for p in _l(spec.get("ports")) if isinstance(p, dict)]
        if (doc.get("kind") == "Service" and _ns(doc) == app.namespace and selector and ports
                and all(app.pod_labels.get(k) == v for k, v in selector.items())):
            target.service, target.service_port = _name(doc), ports[0].get("port")
            break
    target.rollouts = [f"{w.kind.lower()}/{w.name}" for w in workloads(docs)
                       if w.kind in ("Deployment", "StatefulSet") and w.namespace == app.namespace]
    return target


def unavailable_reason(cluster: str) -> str | None:
    """Why a live deploy cannot run here (None when docker, kind, kubectl and the cluster are all available)."""
    missing = [b for b in ("docker", "kind", "kubectl") if not find_binary(b)]
    if missing:
        return f"missing binaries: {', '.join(missing)} (run scripts/install-tools.sh; live deploy only works locally)"
    if not run([find_binary("docker"), "info", "--format", "{{.ServerVersion}}"], 10).ok:
        return "docker daemon is not reachable"
    clusters = run([find_binary("kind"), "get", "clusters"], 15)
    if cluster not in clusters.out.split():
        return f"kind cluster '{cluster}' not found (run scripts/kind-up.sh)"
    return None


def build_and_load(context_dir: Path, dockerfile: Path, image: str, cluster: str, timeout: float = 240) -> dict:
    """docker build + kind load. Safe to run in a background thread while the agent is still fixing manifests."""
    docker, kind = find_binary("docker"), find_binary("kind")
    if not dockerfile.is_file():
        return {"ok": False, "error": f"no Dockerfile at {dockerfile.name} in the build context"}
    # --provenance=false keeps the image ID stable across cached rebuilds, so re-runs skip kind load and restarts.
    cmd = [docker, "build", "-q", "--provenance=false", "-t", image, "-f", str(dockerfile), str(context_dir)]
    build = run(cmd, timeout)
    if not build.ok and "provenance" in build.err:  # legacy builder without attestation flags
        build = run([c for c in cmd if c != "--provenance=false"], timeout)
    if not build.ok:
        return {"ok": False, "error": f"docker build failed:\n{build.tail(12)}", "build_s": build.seconds}
    image_id = run([docker, "image", "inspect", "-f", "{{.Id}}", image], 15).out.strip()
    load = run([kind, "load", "docker-image", image, "--name", cluster], 120)
    if not load.ok:
        return {"ok": False, "error": f"kind load failed:\n{load.tail(6)}", "build_s": build.seconds}
    return {"ok": True, "image": image, "image_id": image_id, "build_s": build.seconds, "load_s": load.seconds}


def apply_and_verify(overlay_dir: Path, target: AppTarget, cluster: str, image_id: str, budget_s: float = 60,
                     health: str = "/healthz", ready: str = "/readyz") -> dict:
    """kubectl apply -k, restart only if the image changed, wait for rollout, then smoke test via port-forward."""
    kubectl = [find_binary("kubectl"), "--context", f"kind-{cluster}"]
    deadline = time.monotonic() + budget_s
    out: dict[str, Any] = {"namespace": target.namespace}
    before = _get_json(kubectl, target.namespace, "deployment", target.deployment)

    applied = run([*kubectl, "apply", "-k", str(overlay_dir)], 40)
    if not applied.ok and "immutable" in applied.err:  # e.g. selector changed between runs: recreate
        applied = run([*kubectl, "apply", "--force", "-k", str(overlay_dir)], 60)
    out["apply_s"] = applied.seconds
    if not applied.ok:
        return {**out, "status": "fail", "reason": f"kubectl apply failed: {applied.tail(8)}"}

    # Same tag, new image: restart only when apply did not already start a rollout (generation unchanged).
    after = _get_json(kubectl, target.namespace, "deployment", target.deployment)
    previous_id = ((before or {}).get("metadata", {}).get("annotations") or {}).get(IMAGE_ID_ANNOTATION)
    same_spec = before and after and before["metadata"].get("generation") == after["metadata"].get("generation")
    if same_spec and previous_id and previous_id != image_id:
        run([*kubectl, "-n", target.namespace, "rollout", "restart", f"deployment/{target.deployment}"], 15)
        out["restarted"] = True
    if image_id:
        run([*kubectl, "-n", target.namespace, "annotate", "deployment", target.deployment,
             f"{IMAGE_ID_ANNOTATION}={image_id}", "--overwrite"], 15)

    started, messages = time.monotonic(), []
    for ref in target.rollouts:
        while True:
            status = run([*kubectl, "-n", target.namespace, "rollout", "status", ref, "--timeout=5s"], 20)
            if status.ok:
                messages.append((status.out.strip().splitlines() or ["rolled out"])[-1])
                break
            fatal = fatal_pod_state(kubectl, target) if ref == f"deployment/{target.deployment}" else ""
            if fatal or time.monotonic() > deadline:
                messages.append(fatal or (status.out.strip().splitlines() or [status.tail(2)])[-1])
                out.update(rollout="; ".join(messages), rollout_s=round(time.monotonic() - started, 1))
                why = f"pods are failing ({fatal})" if fatal else "timed out"
                return {**out, "status": "fail", "reason": f"rollout of {ref} {why}",
                        "diagnostics": diagnostics(kubectl, target)}
    out.update(rollout="; ".join(messages), rollout_s=round(time.monotonic() - started, 1))

    started = time.monotonic()
    tests, error = smoke_via_port_forward(kubectl, target, max(deadline - time.monotonic(), 15), health, ready)
    out.update(smoke_tests=tests, smoke_s=round(time.monotonic() - started, 1))
    if error:
        return {**out, "status": "fail", "reason": error, "diagnostics": diagnostics(kubectl, target)}
    failed = [t["request"] for t in tests if not t["passed"]]
    return {**out, "status": "fail" if failed else "pass", "reason": f"failed: {', '.join(failed)}" if failed else ""}


def _get_json(kubectl: list[str], namespace: str, *what: str, selector: str = "") -> dict | None:
    cmd = [*kubectl, "-n", namespace, "get", *what, "-o", "json"] + (["-l", selector] if selector else [])
    p = run(cmd, 15)
    try:
        return json.loads(p.out) if p.ok else None
    except json.JSONDecodeError:
        return None


def _current_pods(kubectl: list[str], target: AppTarget) -> list[dict]:
    """Pods of the Deployment's newest ReplicaSet (old crash-looping pods must not fail a good rollout)."""
    selector = ",".join(f"{k}={v}" for k, v in target.pod_labels.items())
    dep = _get_json(kubectl, target.namespace, "deployment", target.deployment) or {}
    revision = (dep.get("metadata", {}).get("annotations") or {}).get("deployment.kubernetes.io/revision")
    sets = (_get_json(kubectl, target.namespace, "replicasets", selector=selector) or {}).get("items", [])
    newest = next((rs for rs in sets if (rs["metadata"].get("annotations") or {}).get(
        "deployment.kubernetes.io/revision") == revision), None)
    pod_hash = ((newest or {}).get("metadata", {}).get("labels") or {}).get("pod-template-hash")
    if not pod_hash:
        return []
    pods = _get_json(kubectl, target.namespace, "pods", selector=f"pod-template-hash={pod_hash}") or {}
    return [p for p in pods.get("items", []) if not p["metadata"].get("deletionTimestamp")]


def fatal_pod_state(kubectl: list[str], target: AppTarget) -> str:
    """A container state that will not recover on its own (crash loop, bad image, bad config), or ''."""
    for pod in _current_pods(kubectl, target):
        for cs in pod.get("status", {}).get("containerStatuses") or []:
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
            if reason in FATAL_REASONS:
                return f"{pod['metadata']['name']}: {reason}"
    return ""


def diagnostics(kubectl: list[str], target: AppTarget) -> str:
    """Compact pod states, warning events and recent logs so the agent can decide on a fix."""
    lines: list[str] = []
    for pod in _current_pods(kubectl, target):
        for cs in pod.get("status", {}).get("containerStatuses") or []:
            state = cs.get("state", {})
            waiting, last = state.get("waiting") or {}, (cs.get("lastState") or {}).get("terminated") or {}
            detail = waiting.get("reason") or ("Running" if "running" in state else "Terminated")
            if waiting.get("message"):
                detail += f": {waiting['message'][:200]}"
            if last:
                detail += f" (last exit {last.get('exitCode')} {last.get('reason', '')})"
            lines.append(f"pod {pod['metadata']['name']} [{cs.get('name')}] ready={cs.get('ready')} "
                         f"restarts={cs.get('restartCount')} {detail}")
    events = run([*kubectl, "-n", target.namespace, "get", "events", "--field-selector", "type=Warning",
                  "--sort-by=.lastTimestamp", "-o", "custom-columns=OBJ:.involvedObject.name,REASON:.reason,MSG:.message",
                  "--no-headers"], 15)
    noise = ("FailedGetResourceMetric", "FailedComputeMetricsReplicas")  # HPA before metrics-server has data
    lines += [f"event {e[:220]}" for e in events.out.strip().splitlines() if not any(n in e for n in noise)][-6:]
    logs = run([*kubectl, "-n", target.namespace, "logs", f"deployment/{target.deployment}", "--tail=10"], 15)
    lines += [f"log {line[:200]}" for line in (logs.out or logs.err).strip().splitlines()[-10:] if line.strip()]
    return "\n".join(lines) or "no diagnostics available"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def smoke_via_port_forward(kubectl: list[str], target: AppTarget, timeout: float = 20, health: str = "/healthz",
                           ready: str = "/readyz") -> tuple[list[dict], str]:
    """Port-forward to the Service (or the Deployment) and run the HTTP smoke tests."""
    port = _free_port()
    remote = f"svc/{target.service}" if target.service else f"deployment/{target.deployment}"
    remote_port = target.service_port if target.service else target.container_port
    if remote_port is None:
        return [], "no Service port or containerPort to port-forward to"
    proc = subprocess.Popen([*kubectl, "-n", target.namespace, "port-forward", remote, f"{port}:{remote_port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        ready_by = time.monotonic() + min(timeout, 15)
        while time.monotonic() < ready_by:
            if proc.poll() is not None:
                return [], f"port-forward exited: {(proc.stderr.read() if proc.stderr else '')[-300:]}"
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            return [], f"port-forward to {remote}:{remote_port} not ready"
        return smoke_tests(f"http://127.0.0.1:{port}", health, ready), ""
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def http(method: str, url: str, body: dict | None = None, timeout: float = 5) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)
    try:
        return status, json.loads(raw or b"null")
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")[:200]


def smoke_tests(base_url: str, health: str = "/healthz", ready: str = "/readyz") -> list[dict]:
    """The FR1/FR4 contract checks: health, readiness, list items, place an order, refuse an oversell with 409."""
    results: list[dict] = []

    def check(label: str, expected: int, method: str, path: str, body: dict | None = None) -> Any:
        started = time.monotonic()
        status, payload = http(method, base_url + path, body)
        results.append({"request": label, "status": status, "expected": expected, "passed": status == expected,
                        "ms": int((time.monotonic() - started) * 1000)})
        return payload

    check(f"GET {health}", 200, "GET", health)
    check(f"GET {ready}", 200, "GET", ready)
    items = check("GET /api/items", 200, "GET", "/api/items")
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or []
    item = next((i for i in items or [] if isinstance(i, dict) and "id" in i), None)
    if item is None:
        results.append({"request": "POST /api/orders", "status": 0, "expected": 201, "passed": False,
                        "note": "no items returned by GET /api/items"})
        return results
    check("POST /api/orders (qty 1)", 201, "POST", "/api/orders", {"item_id": item["id"], "quantity": 1})
    oversell = int(item.get("quantity") or 0) + 1_000_000
    check("POST /api/orders (oversell)", 409, "POST", "/api/orders", {"item_id": item["id"], "quantity": oversell})
    return results
