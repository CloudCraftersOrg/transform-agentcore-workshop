"""SIMULATED drift (`options.simulate_drift`): typical hand-edits injected into k8s/base before the first check.

The modernization output already passes every policy, so on stage the agent would have nothing to fix. These four
deterministic edits (no model involved) recreate what a rushed manual change usually breaks; the agent then detects
and fixes them like any other finding. Each change is recorded so the report can label it as SIMULATED.
"""

import re

from .policy import parse_image

BASE = "k8s/base/"


def _deployment_file(files: dict[str, str]) -> str | None:
    preferred = BASE + "deployment.yaml"
    if preferred in files:
        return preferred
    return next((p for p in sorted(files) if p.startswith(BASE) and p.endswith((".yaml", ".yml"))
                 and re.search(r"(?m)^kind:\s*Deployment\s*$", files[p])), None)


def _pin_latest(text: str) -> tuple[str, str]:
    """First `image:` -> `<name>:latest`."""
    m = re.search(r"(?m)^([ \t]*-?[ \t]*image:[ \t]*)([\"']?)([^\s\"']+)\2[ \t]*$", text)
    if not m:
        return text, ""
    name, tag, digest = parse_image(m.group(3))
    if tag == "latest" and not digest:
        return text, ""
    new = f"{m.group(1)}{name}:latest"
    return text[:m.start()] + new + text[m.end():], f"image {m.group(3)} -> {name}:latest (unpinned tag)"


def _drop_limits(text: str) -> tuple[str, str]:
    """Remove the first container's `resources.limits` block (block or inline form)."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        res = re.match(r"^(\s*)resources:\s*$", line)
        if not res:
            continue
        indent = len(res.group(1))
        for j in range(i + 1, len(lines)):
            stripped = lines[j].strip()
            if stripped and len(lines[j]) - len(lines[j].lstrip()) <= indent:
                break  # left the resources block without finding limits
            lim = re.match(r"^(\s*)limits:(.*)$", lines[j])
            if not lim:
                continue
            end = j + 1
            if not lim.group(2).strip():  # block form: drop the more-indented children too
                while end < len(lines) and (not lines[end].strip() or
                                            len(lines[end]) - len(lines[end].lstrip()) > len(lim.group(1))):
                    end += 1
            return "".join(lines[:j] + lines[end:]), "removed resources.limits from the container"
        return text, ""
    return text, ""


def _writable_root(text: str) -> tuple[str, str]:
    new, n = re.subn(r"(?m)^([ \t]*readOnlyRootFilesystem:[ \t]*)true[ \t]*$", r"\1false", text, count=1)
    return (new, "readOnlyRootFilesystem: true -> false") if n else (text, "")


def _drop_pdb(text: str) -> tuple[str, str]:
    new, n = re.subn(r"(?m)^[ \t]*-[ \t]*[\"']?pdb\.ya?ml[\"']?[ \t]*(\n|$)", "", text, count=1)
    return (new, "removed pdb.yaml from resources (the PodDisruptionBudget is no longer deployed)") if n else (text, "")


def inject_drift(files: dict[str, str]) -> tuple[dict[str, str], list[dict]]:
    """Return (drifted files, [{file, change, rule}]) for up to four simulated hand-edits. Never raises."""
    out = dict(files)
    injected: list[dict] = []
    dep = _deployment_file(out)
    if dep:
        for rule, edit in (("POL-009", _pin_latest), ("POL-001", _drop_limits), ("POL-005", _writable_root)):
            text, change = edit(out[dep])
            if change:
                out[dep] = text
                injected.append({"file": dep, "change": change, "rule": rule})
    kust = next((BASE + n for n in ("kustomization.yaml", "kustomization.yml") if BASE + n in out), None)
    if kust:
        text, change = _drop_pdb(out[kust])
        if change:
            out[kust] = text
            injected.append({"file": kust, "change": change, "rule": "POL-011"})
    return out, injected
