"""Deployment helpers behind deploy/*.sh: AgentCore CLI project generation, ARN file, invoke and status.

`deploy/` is an AgentCore CLI project (`agentcore/agentcore.json` + the vended CDK app in `agentcore/cdk/`).
For every stage agent this tool generates a CodeZip code directory `deploy/app/<stage>/` containing:

    main.py              entrypoint: exposes vendored binaries, then serves transform_agents.<stage>.runtime:app
    pyproject.toml       dependencies pinned from uv.lock (+ aws-opentelemetry-distro for AgentCore traces)
    transform_agents/    a copy of our package (core library + every agent)
    vendor/bin/          linux-arm64 tools (k8s_validation: kubeconform + kubectl; modernization: uv;
                         terraform with --with-terraform)
    build-manifest.json  what was packaged and why

`agentcore deploy` then packages each directory for arm64 (uv, manylinux wheels), uploads the zips and creates one
AgentCore Runtime per stage through CloudFormation. Nothing in this module creates AWS resources by itself: the only
AWS calls are read-only (STS identity, CloudFormation DescribeStacks/ListStackResources, GetAgentRuntime) plus
InvokeAgentRuntime in `invoke`.

What is deployed is read from the CloudFormation stack (read-only), falling back to the CLI's deployed state, so a
subset deploy or a destroy from a fresh clone never deletes runtimes it does not know about.

Usage (always from the repo root, through `uv run`):
    python deploy/deploy_tool.py stages [stage ...]
    python deploy/deploy_tool.py prepare [stage ...] [--all] [--force-subset] [--exact] [--region R] [--account A]
                                         [--no-binaries] [--with-terraform]
    python deploy/deploy_tool.py plan-destroy [stage ...] [--region R]
    python deploy/deploy_tool.py forget
    python deploy/deploy_tool.py arns [--region R]
    python deploy/deploy_tool.py invoke <stage> '<json payload | text | @file.json | ->' [--local URL] [--raw]
    python deploy/deploy_tool.py status
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
PACKAGE_DIR = REPO_ROOT / "transform_agents"
CONFIG_PY = PACKAGE_DIR / "core" / "config.py"
PROJECT_CONFIG_DIR = DEPLOY_DIR / "agentcore"
AGENTCORE_JSON = PROJECT_CONFIG_DIR / "agentcore.json"
AWS_TARGETS_JSON = PROJECT_CONFIG_DIR / "aws-targets.json"
DEPLOYED_STATE_JSON = PROJECT_CONFIG_DIR / ".cli" / "deployed-state.json"
APP_DIR = DEPLOY_DIR / "app"
ARNS_FILE = DEPLOY_DIR / "agent-arns.json"
BIN_CACHE = DEPLOY_DIR / ".cache" / "bin"
EXTRA_POLICY = DEPLOY_DIR / "policies" / "runtime-extras.json"

PROJECT_NAME = "TransformWorkshop"  # AgentCore project name: ^[A-Za-z][A-Za-z0-9]{0,22}$
TARGET_NAME = "default"
# Same rule as toStackName() in agentcore/cdk/bin/cdk.ts, so it never depends on a (gitignored) agentcore.json.
STACK_NAME = f"AgentCore-{PROJECT_NAME.replace('_', '-')}-{TARGET_NAME.replace('_', '-')}"
DEFAULT_REGION = "us-west-2"
RUNTIME_VERSION = "PYTHON_3_12"  # matches .python-version and the uv.lock resolution
EXTRA_REQUIREMENTS = ["aws-opentelemetry-distro==0.21.0"]  # ADOT: `opentelemetry-instrument main.py` -> traces
# ADOT pins exact OpenTelemetry versions, so the uv.lock pins of this family are dropped and re-resolved.
UNPINNED_PREFIXES = ("opentelemetry-",)
LIFECYCLE = {"idleRuntimeSessionTimeout": 600, "maxLifetime": 3600}  # cost guard: idle sessions end after 10 min
GLOBAL_PASSTHROUGH_ENV = ("MODEL_ID", "MODEL_MAX_TOKENS")
COPY_IGNORE = ("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store", ".terraform", "*.tfstate", "*.tfstate.*",
               "*.kubeconfig", ".ruff_cache")


@dataclass(frozen=True)
class Binary:
    name: str
    version: str
    url: str
    sha256: str  # of the download, copied from the upstream checksum files
    member: str  # path of the executable inside the archive ("" when the download is the executable itself)


# linux/arm64 only: AgentCore Runtime is arm64. Checksums: releases.hashicorp.com SHA256SUMS, kubeconform CHECKSUMS,
# dl.k8s.io kubectl.sha256, the uv release's <archive>.sha256.
BINARIES = {
    "terraform": Binary(
        "terraform", "1.15.4",
        "https://releases.hashicorp.com/terraform/1.15.4/terraform_1.15.4_linux_arm64.zip",
        "87da14c6700fee1bdaffedc943c4aca7bb078aad1f3eeccbdaae0484f90e1bec", "terraform"),
    "kubeconform": Binary(
        "kubeconform", "0.8.0",
        "https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-arm64.tar.gz",
        "1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87", "kubeconform"),
    "kubectl": Binary(
        "kubectl", "1.33.13",
        "https://dl.k8s.io/release/v1.33.13/bin/linux/arm64/kubectl",
        "9fe8aadd5aab9421978c7ac95de6fad304a3c4c0ce9dbf4127579779e0699080", ""),
    "uv": Binary(
        "uv", "0.12.15",
        "https://github.com/astral-sh/uv/releases/download/0.12.15/uv-aarch64-unknown-linux-gnu.tar.gz",
        "0e9a3499b0587d449c9ff684c0160da607826e4af1cee220bc87f378702d3e08", "uv-aarch64-unknown-linux-gnu/uv"),
}
# Vendored per stage by default. k8s_validation: kubeconform (13 MB; schemas download at run time over the PUBLIC
# network) and kubectl (57 MB; only `kubectl kustomize` is used, offline, so overlays render with their patches).
# modernization: uv (43 MB) runs the generated microservice's pytest in an isolated env (`uv run --no-project
# --with-requirements requirements-dev.txt`); its wheels download from PyPI at run time over the PUBLIC network.
STAGE_BINARIES = {"k8s_validation": ["kubeconform", "kubectl"], "modernization": ["uv"]}
# Opt-in (--with-terraform / VENDOR_TERRAFORM=1): +108 MB unzipped. With it the terraform agent runs a real
# `terraform init/validate` in the cloud, but every new session starts cold and downloads the EKS module and the AWS
# provider (minutes). Without it the agent validates offline (python-hcl2 + schema checks), as it is designed to do
# on AgentCore Runtime.
OPT_IN_BINARIES = {"terraform": ["terraform"]}
STAGE_ENV = {
    # The terraform agent keeps its module/provider cache under TF_WORKSHOP_CACHE (default ~/.cache): use /tmp.
    "terraform": {"TF_WORKSHOP_CACHE": "/tmp/transform-workshop/terraform"},
}
STAGE_PASSTHROUGH_ENV = {"terraform": ("TERRAFORM_OFFLINE",)}
# Defaults set by the generated main.py (os.environ.setdefault, so a runtime envVar still wins). They live in main.py
# rather than in envVars so that smoke-local.sh, which only runs the zip, behaves like the cloud.
MAIN_ENV_DEFAULTS = {
    # uv needs a writable cache (the code dir and ~ are read-only), and a writable place for a managed CPython in case
    # it cannot find a system python3.12.
    "modernization": {"UV_CACHE_DIR": "/tmp/uv-cache", "UV_PYTHON_INSTALL_DIR": "/tmp/uv-python"},
}


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


# --------------------------------------------------------------------------------------------- stages


def read_pipeline_stages(config_py: Path = CONFIG_PY) -> list[str]:
    """STAGES from transform_agents/core/config.py, read statically (no heavy imports)."""
    tree = ast.parse(config_py.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STAGES" for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, list) and all(isinstance(s, str) for s in value):
                return value
    raise ValueError(f"STAGES list not found in {config_py}")


def all_stages(config_py: Path = CONFIG_PY) -> list[str]:
    """Deployable agents in pipeline order: the hello smoke-test agent plus every pipeline stage."""
    return ["hello", *[s for s in read_pipeline_stages(config_py) if s != "hello"]]


def runtime_module_ok(runtime_py: Path) -> bool:
    """True when runtime.py assigns a module-level `app` (the BedrockAgentCoreApp)."""
    try:
        tree = ast.parse(runtime_py.read_text())
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "app" for t in targets):
            return True
    return False


def discover(requested: list[str] | None = None, package_dir: Path = PACKAGE_DIR,
             config_py: Path = CONFIG_PY) -> tuple[list[str], list[str]]:
    """Split the requested stages (default: all) into (available, skipped).

    A stage is available when transform_agents/<stage>/runtime.py exists and defines `app`.
    Unknown names raise ValueError; known-but-unfinished stages are skipped with a warning.
    """
    known = all_stages(config_py)
    wanted = list(dict.fromkeys(requested)) if requested else known
    unknown = [s for s in wanted if s not in known]
    if unknown:
        raise ValueError(f"unknown stage(s): {', '.join(unknown)} (known: {', '.join(known)})")
    available, skipped = [], []
    for stage in sorted(wanted, key=known.index):
        runtime_py = package_dir / stage / "runtime.py"
        if runtime_py.is_file() and runtime_module_ok(runtime_py):
            available.append(stage)
        else:
            skipped.append(stage)
            reason = "no runtime.py" if not runtime_py.is_file() else "runtime.py does not define `app`"
            warn(f"skipping {stage}: {reason} in {runtime_py.parent.relative_to(package_dir.parent)}")
    return available, skipped


# --------------------------------------------------------------------------------------------- packaging


def locked_requirements(repo_root: Path = REPO_ROOT) -> list[str]:
    """Runtime dependency pins exported from uv.lock (no dev group, no extras, no project itself)."""
    out = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project", "--no-header",
         "--no-annotate"],
        cwd=repo_root, check=True, capture_output=True, text=True).stdout
    return filter_requirements(out.splitlines())


def filter_requirements(lines: list[str]) -> list[str]:
    reqs = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "-", ".")):
            continue
        name = line.split("==")[0].split(";")[0].strip().lower()
        if name.startswith(UNPINNED_PREFIXES):
            continue
        reqs.append(line)
    return [*reqs, *EXTRA_REQUIREMENTS]


def render_pyproject(stage: str, requirements: list[str]) -> str:
    deps = "\n".join(f"    {json.dumps(r)}," for r in requirements)
    return (
        "# Generated by deploy/deploy_tool.py - do not edit. `agentcore deploy` runs\n"
        "# `uv pip install -r pyproject.toml --python-platform aarch64-manylinux* --only-binary :all:` on it.\n"
        "[project]\n"
        f'name = "transform-agent-{stage.replace("_", "-")}"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        f"dependencies = [\n{deps}\n]\n"
    )


MAIN_TEMPLATE = '''"""AgentCore Runtime entrypoint for the `{stage}` agent. Generated by deploy/deploy_tool.py - do not edit.

AgentCore starts it as `opentelemetry-instrument main.py` from the unzipped package root.
"""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("AWS_REGION", "{region}")
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
{env_defaults}

def _expose_vendored_binaries() -> None:
    """Zip deploys lose the executable bit and the code dir is read-only: copy tools to /tmp, chmod, add to PATH."""
    src = ROOT / "vendor" / "bin"
    if not src.is_dir():
        return
    dest = Path(os.environ.get("VENDOR_BIN_DIR", "/tmp/vendor-bin"))
    dest.mkdir(parents=True, exist_ok=True)
    for tool in src.iterdir():
        target = dest / tool.name
        if not target.exists() or target.stat().st_size != tool.stat().st_size:
            shutil.copyfile(tool, target)
        target.chmod(0o755)
    os.environ["PATH"] = f"{{dest}}{{os.pathsep}}{{os.environ.get('PATH', '')}}"


try:
    _expose_vendored_binaries()
except OSError as exc:  # the agents degrade gracefully when a tool is missing
    print(f"warning: vendored binaries unavailable: {{exc}}", file=sys.stderr)

from transform_agents.{stage}.runtime import app  # noqa: E402

if __name__ == "__main__":
    app.run()
'''


def render_main(stage: str, region: str, env_defaults: dict[str, str] | None = None) -> str:
    defaults = MAIN_ENV_DEFAULTS.get(stage, {}) if env_defaults is None else env_defaults
    lines = "".join(f"os.environ.setdefault({json.dumps(k)}, {json.dumps(v)})\n" for k, v in sorted(defaults.items()))
    return MAIN_TEMPLATE.format(stage=stage, region=region, env_defaults=lines)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_member(archive_name: str, data: bytes, member: str) -> bytes:
    if not member:
        return data
    if archive_name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.read(member)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        extracted = tf.extractfile(member)
        if extracted is None:
            raise ValueError(f"{member} is not a regular file in {archive_name}")
        return extracted.read()


def fetch_binary(binary: Binary, cache_dir: Path = BIN_CACHE) -> Path:
    """Download (once), verify the archive checksum and cache the linux-arm64 executable."""
    target = cache_dir / f"{binary.name}-{binary.version}-linux-arm64" / binary.name
    if target.is_file():
        return target
    print(f"downloading {binary.name} {binary.version} (linux/arm64)")
    with urllib.request.urlopen(binary.url, timeout=300) as resp:  # pinned https URL, sha256-verified below
        data = resp.read()
    got = sha256_bytes(data)
    if got != binary.sha256:
        raise RuntimeError(f"checksum mismatch for {binary.url}: got {got}, expected {binary.sha256}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(extract_member(binary.url, data, binary.member))
    tmp.chmod(0o755)
    tmp.replace(target)
    return target


def stage_env(stage: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Runtime environment variables: MODEL_* (and stage) passthrough from the deploying shell plus stage extras."""
    environ = dict(os.environ if environ is None else environ)
    env = {}
    for name in (*GLOBAL_PASSTHROUGH_ENV, f"MODEL_{stage.upper()}", *STAGE_PASSTHROUGH_ENV.get(stage, ())):
        if environ.get(name):
            env[name] = environ[name]
    env.update(STAGE_ENV.get(stage, {}))
    return env


def build_manifest(stage: str, region: str, requirements: list[str], binaries: list[str],
                   env: dict[str, str]) -> dict[str, Any]:
    return {
        "stage": stage,
        "entrypoint": "main.py",
        "module": f"transform_agents.{stage}.runtime:app",
        "runtime_version": RUNTIME_VERSION,
        "region": region,
        "requirements": len(requirements),
        "extra_requirements": EXTRA_REQUIREMENTS,
        "binaries": {b: BINARIES[b].version for b in binaries},
        "env": sorted(env),
        "main_env_defaults": MAIN_ENV_DEFAULTS.get(stage, {}),
    }


def stage_binaries(stage: str, with_binaries: bool = True, opt_in: bool = False) -> list[str]:
    if not with_binaries:
        return []
    return [*STAGE_BINARIES.get(stage, []), *(OPT_IN_BINARIES.get(stage, []) if opt_in else [])]


def build_stage_dir(stage: str, *, region: str, requirements: list[str], with_binaries: bool = True,
                    with_opt_in_binaries: bool = False, app_dir: Path = APP_DIR, package_dir: Path = PACKAGE_DIR,
                    bin_cache: Path = BIN_CACHE, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """(Re)create deploy/app/<stage>/ - the CodeZip code location for one runtime."""
    out = app_dir / stage
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(package_dir, out / "transform_agents", ignore=shutil.ignore_patterns(*COPY_IGNORE))
    (out / "main.py").write_text(render_main(stage, region))
    (out / "pyproject.toml").write_text(render_pyproject(stage, requirements))
    binaries = stage_binaries(stage, with_binaries, with_opt_in_binaries)
    for name in binaries:
        src = fetch_binary(BINARIES[name], bin_cache)
        dest = out / "vendor" / "bin" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    env = stage_env(stage, environ)
    manifest = build_manifest(stage, region, requirements, binaries, env)
    (out / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# --------------------------------------------------------------------------------------------- project config


def runtime_spec(stage: str, env: dict[str, str]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": stage,
        "description": f"Transform workshop agent: {stage}",
        "build": "CodeZip",
        "entrypoint": "main.py",
        "codeLocation": f"app/{stage}/",
        "runtimeVersion": RUNTIME_VERSION,
        "networkMode": "PUBLIC",
        "protocol": "HTTP",
        "instrumentation": {"enableOtel": True},
        # Relative to codeLocation; attached as an inline policy on the CDK-managed execution role.
        "additionalPolicies": ["../../policies/runtime-extras.json"],
        "lifecycleConfiguration": dict(LIFECYCLE),
        "tags": {"workshop": "transform-agentcore", "stage": stage},
    }
    if env:
        spec["envVars"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
    return spec


def empty_project() -> dict[str, Any]:
    return {
        "$schema": "https://schema.agentcore.aws.dev/v1/agentcore.json",
        "name": PROJECT_NAME,
        "version": 1,
        "managedBy": "CDK",
        "tags": {"agentcore:created-by": "agentcore-cli", "agentcore:project-name": PROJECT_NAME,
                 "workshop": "transform-agentcore"},
        "runtimes": [], "memories": [], "knowledgeBases": [], "credentials": [], "evaluators": [],
        "onlineEvalConfigs": [], "agentCoreGateways": [], "policyEngines": [], "configBundles": [], "abTests": [],
        "harnesses": [], "datasets": [], "capacityProviders": [], "payments": [],
    }


def read_project(path: Path = AGENTCORE_JSON) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text())
    return empty_project()


def configured_stages(path: Path = AGENTCORE_JSON) -> list[str]:
    return [r["name"] for r in read_project(path).get("runtimes", [])]


def write_project(stages: list[str], envs: dict[str, dict[str, str]], path: Path = AGENTCORE_JSON) -> dict:
    project = read_project(path)
    project["runtimes"] = [runtime_spec(s, envs.get(s, {})) for s in stages]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(project, indent=2) + "\n")
    return project


def write_targets(account: str, region: str, path: Path = AWS_TARGETS_JSON) -> None:
    path.write_text(json.dumps([{"name": TARGET_NAME, "description": "Transform workshop agents",
                                 "account": account, "region": region}], indent=2) + "\n")


def read_targets(path: Path = AWS_TARGETS_JSON) -> dict[str, str]:
    """The account/region of the last `prepare` ({} when there is none)."""
    try:
        targets = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return next((t for t in targets if isinstance(t, dict) and t.get("name") == TARGET_NAME), {})


def resolve_deploy_set(requested: list[str] | None, existing: list[str], known: list[str]) -> list[str]:
    """Stages that must be in agentcore.json after this deploy.

    No arguments -> every stage. A subset -> that subset *plus* what is already deployed, because CloudFormation
    deletes any runtime that disappears from the stack (use destroy.sh <stage> to remove one).
    """
    wanted = set(requested or known) | set(existing)
    return [s for s in known if s in wanted]


def aws_account(region: str) -> str:
    import boto3

    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


# --------------------------------------------------------------------------------------------- what is deployed


@dataclass(frozen=True)
class StackInfo:
    name: str
    status: str
    runtimes: dict[str, str]  # stage -> runtime ARN, from the stack outputs


@dataclass(frozen=True)
class Deployment:
    """What is deployed right now, as far as we can tell without changing anything."""

    stages: list[str]
    known: bool  # False: neither the stack lookup nor the CLI state could tell
    source: str  # where `stages` came from, for messages
    stack: StackInfo | None = None


RUNTIME_ARN_OUTPUT = "Runtime ARN for agent: "  # @aws/agentcore-cdk AgentEnvironment output description


def runtime_arns_from_outputs(outputs: list[dict[str, Any]] | None) -> dict[str, str]:
    """{stage: runtimeArn} from DescribeStacks outputs ("Runtime ARN for agent: <name>" -> arn:...:runtime/<id>)."""
    arns = {}
    for output in outputs or []:
        value, description = str(output.get("OutputValue", "")), str(output.get("Description", ""))
        if not (value.startswith("arn:") and ":runtime/" in value):
            continue
        if description.startswith(RUNTIME_ARN_OUTPUT):
            name = description[len(RUNTIME_ARN_OUTPUT):].strip()
        else:  # runtime id = <project>_<name>-<suffix>; runtime names cannot contain "-"
            name = value.rsplit("/", 1)[-1].rsplit("-", 1)[0].removeprefix(f"{PROJECT_NAME}_")
        arns[name] = value
    return arns


def cfn_client(region: str):
    import boto3

    return boto3.client("cloudformation", region_name=region)


def stack_info(region: str, stack_name: str = STACK_NAME, client: Any = None) -> StackInfo | None:
    """Read-only DescribeStacks. None when the stack does not exist; any other error propagates."""
    client = client or cfn_client(region)
    try:
        stacks = client.describe_stacks(StackName=stack_name).get("Stacks") or []
    except Exception as exc:
        if "does not exist" in str(exc):
            return None
        raise
    if not stacks:
        return None
    stack = stacks[0]
    return StackInfo(stack_name, stack.get("StackStatus", "?"), runtime_arns_from_outputs(stack.get("Outputs")))


def stack_resource_counts(region: str, stack_name: str = STACK_NAME, client: Any = None) -> Counter:
    """Read-only ListStackResources: {resource type: count} of what deleting the stack removes."""
    client = client or cfn_client(region)
    counts: Counter = Counter()
    for page in client.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        for res in page.get("StackResourceSummaries", []):
            if res.get("ResourceStatus") != "DELETE_COMPLETE":
                counts[res.get("ResourceType", "?")] += 1
    return counts


def deployed_state_stages(path: Path = DEPLOYED_STATE_JSON, target: str = TARGET_NAME) -> list[str] | None:
    """Runtime names in the CLI's deployed-state.json; None when the file is missing (fresh clone, git clean -X)."""
    if not path.is_file():
        return None
    state = json.loads(path.read_text())
    runtimes = (state.get("targets", {}).get(target, {}).get("resources", {}) or {}).get("runtimes", {}) or {}
    return list(runtimes)


def current_deployment(region: str, *, state_path: Path = DEPLOYED_STATE_JSON, project_path: Path = AGENTCORE_JSON,
                       lookup=stack_info) -> Deployment:
    """The CloudFormation stack is the truth; the CLI deployed state is the fallback when it cannot be read."""
    try:
        stack = lookup(region)
    except Exception as exc:  # noqa: BLE001 - no permission, throttling, network: fall back to local state
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    else:
        if stack is None:
            return Deployment([], True, f"CloudFormation (stack {STACK_NAME} does not exist in {region})")
        return Deployment(list(stack.runtimes), True, f"CloudFormation stack {STACK_NAME} ({stack.status})", stack)
    state = deployed_state_stages(state_path)
    if state is None:
        return Deployment([], False, f"the CloudFormation lookup failed ({error}) and there is no {rel(state_path)}")
    warn(f"CloudFormation lookup failed ({error}); using the local CLI state instead")
    stages = list(dict.fromkeys([*state, *configured_stages(project_path)]))
    return Deployment(stages, True, f"the local CLI state {rel(state_path)}")


def refuse_subset_message(requested: list[str], deployment: Deployment) -> str:
    names = " ".join(requested)
    return (
        f"refusing a subset deploy: cannot tell which agents are already deployed ({deployment.source}).\n"
        f"  Deploying only '{names}' would make CloudFormation DELETE every other runtime in stack {STACK_NAME}.\n"
        "  Choose one:\n"
        "    deploy/deploy.sh --all                       deploy every agent (same as no arguments)\n"
        f"    FORCE_SUBSET=1 deploy/deploy.sh {names:<12} deploy only these; other runtimes in the stack are deleted\n"
        "  Or fix the lookup (aws sso login, AWS_REGION) and retry."
    )


# --------------------------------------------------------------------------------------------- ARN file


def runtime_arns_from_state(state: dict[str, Any], target: str = TARGET_NAME) -> dict[str, str]:
    runtimes = (state.get("targets", {}).get(target, {}).get("resources", {}) or {}).get("runtimes", {}) or {}
    return {name: info["runtimeArn"] for name, info in runtimes.items() if info.get("runtimeArn")}


def order_arns(arns: dict[str, str], known: list[str]) -> dict[str, str]:
    ordered = {s: arns[s] for s in known if s in arns}
    ordered.update({k: v for k, v in arns.items() if k not in ordered})
    return ordered


def write_arns(arns: dict[str, str], path: Path = ARNS_FILE) -> None:
    path.write_text(json.dumps(arns, indent=2) + "\n")


def read_arns(path: Path = ARNS_FILE) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"{path} must map stage -> runtime ARN")
    return data


def region_from_arn(arn: str) -> str:
    parts = arn.split(":")
    if len(parts) < 6 or not parts[0] == "arn" or parts[2] != "bedrock-agentcore":
        raise ValueError(f"not an AgentCore runtime ARN: {arn}")
    return parts[3]


# --------------------------------------------------------------------------------------------- invoke


def parse_payload(arg: str) -> dict[str, Any]:
    """'<json>' | '@file.json' | '-' (stdin) | plain text (sent as the instruction)."""
    if arg == "-":
        arg = sys.stdin.read()
    elif arg.startswith("@"):
        arg = Path(arg[1:]).read_text()
    try:
        value = json.loads(arg)
    except json.JSONDecodeError:
        return {"instruction": arg}
    if not isinstance(value, dict):
        raise TypeError("payload must be a JSON object")
    return value


def session_id(stage: str) -> str:
    return f"cli-{stage}-{uuid.uuid4()}"  # InvokeAgentRuntime requires 33-256 characters


def iter_sse_events(lines):
    """Yield decoded JSON events from SSE `data:` lines (bytes or str)."""
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if line.startswith("{"):  # a plain JSON body (e.g. an error returned without SSE framing)
            body = line
        elif line.startswith("data:"):
            body = line[5:].strip()
        else:
            continue
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            yield {"type": "raw", "data": body}
            continue
        if isinstance(event, str):  # the SDK JSON-encodes; a str here is a doubly-encoded event
            try:
                event = json.loads(event)
            except json.JSONDecodeError:
                event = {"type": "text", "delta": event}
        yield event if isinstance(event, dict) else {"type": "raw", "data": event}


COLORS = {"status": "36", "tool_start": "35", "tool_end": "35", "artifact": "32", "result": "1;32", "error": "1;31"}


def format_event(event: dict[str, Any], color: bool = False, max_chars: int = 600) -> str:
    """Human-readable rendering of one contract event: text deltas verbatim (streamed), anything else as lines."""
    kind = event.get("type")

    def paint(text: str) -> str:
        return f"\033[{COLORS[kind]}m{text}\033[0m" if color and kind in COLORS else text

    def cut(text: str) -> str:
        text = " ".join(text.split()) if kind in ("tool_start", "tool_end") else text
        return text if len(text) <= max_chars else text[:max_chars] + f"... (+{len(text) - max_chars} chars)"

    if kind == "text":
        return str(event.get("delta", ""))
    if kind == "status":
        model = f" [{event['model']}]" if event.get("model") else ""
        return paint(f"[{event.get('stage', '?')}] {event.get('message', '')}{model}")
    if kind == "tool_start":
        return paint(f"-> {event.get('name')}({cut(str(event.get('input', '')))})")
    if kind == "tool_end":
        return paint(f"<- {event.get('name')} [{event.get('status')}] {cut(str(event.get('output', '')))}")
    if kind == "artifact":
        return paint(f"+ artifact {event.get('path')} ({event.get('bytes')} bytes)")
    if kind == "result":
        lines = [paint(f"== result: {event.get('stage')} =="), f"summary: {event.get('summary', '')}",
                 f"metrics: {json.dumps(event.get('metrics', {}))}",
                 f"data: {cut(json.dumps(event.get('data', {}), indent=2))}"]
        lines += [f"artifact: {p} ({len(c)} chars)" for p, c in (event.get("artifacts") or {}).items()]
        return "\n".join(lines)
    if kind == "error":
        return paint(f"ERROR [{event.get('stage', '?')}]: {event.get('message', '')}")
    return json.dumps(event)


class EventPrinter:
    """Streams text deltas inline and puts every other event on its own line."""

    def __init__(self, out=None, color: bool = False):
        self.out = out or sys.stdout
        self.color = color
        self.at_line_start = True

    def __call__(self, event: dict[str, Any]) -> None:
        text = format_event(event, color=self.color)
        if event.get("type") == "text":
            if text:
                self.out.write(text)
                self.at_line_start = text.endswith("\n")
        else:
            self.out.write(("" if self.at_line_start else "\n") + text + "\n")
            self.at_line_start = True
        self.out.flush()


def invoke(stage: str, payload: dict[str, Any], *, local: str | None = None, raw: bool = False,
           out: Path | None = None, arns_path: Path = ARNS_FILE) -> int:
    started = time.time()
    if local:
        req = urllib.request.Request(local.rstrip("/") + "/invocations", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        resp = urllib.request.urlopen(req, timeout=900)  # explicit local URL from --local
        lines, where = resp, local
    else:
        arns = read_arns(arns_path)
        if stage not in arns:
            print(f"no runtime ARN for {stage!r} in {arns_path} (deployed: {', '.join(arns) or 'none'}). "
                  "Run deploy/deploy.sh first.", file=sys.stderr)
            return 2
        import boto3
        from botocore.config import Config

        arn = arns[stage]
        client = boto3.client("bedrock-agentcore", region_name=region_from_arn(arn),
                              config=Config(read_timeout=900, connect_timeout=10, retries={"max_attempts": 2}))
        sid = session_id(stage)
        resp = client.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=sid, qualifier="DEFAULT",
                                           contentType="application/json", accept="text/event-stream",
                                           payload=json.dumps(payload).encode())
        lines, where = resp["response"].iter_lines(), f"{arn} (session {sid})"
    print(f"invoking {stage} at {where}", file=sys.stderr)
    printer = EventPrinter(color=sys.stdout.isatty())
    status, sink = 1, out.open("w") if out else None
    try:
        for event in iter_sse_events(lines):
            if sink:
                sink.write(json.dumps(event) + "\n")
            if raw:
                print(json.dumps(event), flush=True)
            else:
                printer(event)
            if event.get("type") == "result":
                status = 0
            elif event.get("type") == "error":
                status = 1
    finally:
        if sink:
            sink.close()
    print(f"\n({time.time() - started:.1f}s)", file=sys.stderr)
    return status


# --------------------------------------------------------------------------------------------- status


def status(arns_path: Path = ARNS_FILE) -> int:
    arns = read_arns(arns_path)
    if not arns:
        print(f"no agents recorded in {arns_path} - run deploy/deploy.sh")
        return 0
    import boto3

    clients: dict[str, Any] = {}
    print(f"{'stage':<16}{'status':<12}{'version':<9}{'updated':<27}runtime ARN")
    for stage, arn in arns.items():
        region = region_from_arn(arn)
        client = clients.setdefault(region, boto3.client("bedrock-agentcore-control", region_name=region))
        try:
            rt = client.get_agent_runtime(agentRuntimeId=arn.rsplit("/", 1)[-1])
            print(f"{stage:<16}{rt.get('status', '?'):<12}{rt.get('agentRuntimeVersion', '?'):<9}"
                  f"{str(rt.get('lastUpdatedAt', ''))[:25]:<27}{arn}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"{stage:<16}{'ERROR':<12}{'':<9}{'':<27}{arn}  ({type(exc).__name__}: {exc})")
    return 0


# --------------------------------------------------------------------------------------------- CLI


def plan_deploy_set(requested: list[str] | None, available: list[str], known: list[str], *, exact: bool = False,
                    force_subset: bool = False, deployment: Deployment | None = None) -> list[str] | None:
    """The stages agentcore.json must list. None means: refuse (a subset deploy while the deployed set is unknown)."""
    if not requested:
        return list(known)
    if exact:
        warn(f"--exact: deploying only {', '.join(available)}; any other runtime in the stack is deleted")
        return list(available)
    assert deployment is not None
    if not deployment.known and not force_subset:
        return None
    if not deployment.known:
        warn(f"FORCE_SUBSET=1: deploying {', '.join(available)} without knowing what is deployed; any other runtime "
             f"in stack {STACK_NAME} is deleted by CloudFormation")
    existing = [s for s in deployment.stages if s in known]
    foreign = [s for s in deployment.stages if s not in known]
    if foreign:
        warn(f"runtime(s) {', '.join(foreign)} in the stack are not workshop stages any more: this deploy deletes them")
    kept = [s for s in existing if s not in available]
    if kept:
        print(f"keeping the already-deployed runtimes {', '.join(kept)} (from {deployment.source})")
    return resolve_deploy_set(available, existing, known)


def cmd_prepare(args: argparse.Namespace) -> int:
    known = all_stages()
    requested = None if args.all else (args.stages or None)
    available, _ = discover(requested)
    if requested and not available:
        print("nothing to deploy: none of the requested stages has a runtime.py yet", file=sys.stderr)
        return 3
    region = args.region
    account = args.account or aws_account(region)
    deployment = current_deployment(region) if requested and not args.exact else None
    deploy_set = plan_deploy_set(requested, available, known, exact=args.exact, force_subset=args.force_subset,
                                 deployment=deployment)
    if deploy_set is None:
        print(f"error: {refuse_subset_message(available, deployment)}", file=sys.stderr)
        return 4
    deploy_set, dropped = discover(deploy_set)
    if dropped:
        warn(f"not deploying (runtime.py missing): {', '.join(dropped)}")
    if not deploy_set:
        print("nothing to deploy: no stage has a runtime.py yet", file=sys.stderr)
        return 3
    requirements = locked_requirements()
    envs = {}
    for stage in deploy_set:
        manifest = build_stage_dir(stage, region=region, requirements=requirements,
                                   with_binaries=not args.no_binaries, with_opt_in_binaries=args.with_terraform)
        envs[stage] = stage_env(stage)
        print(f"prepared app/{stage}/ ({manifest['requirements']} pinned deps, "
              f"binaries: {', '.join(manifest['binaries']) or 'none'}, env: {', '.join(manifest['env']) or 'none'})")
    for stale in sorted(p.name for p in APP_DIR.iterdir() if p.is_dir() and p.name not in deploy_set):
        shutil.rmtree(APP_DIR / stale)
    write_project(deploy_set, envs)
    write_targets(account, region)
    print(f"agentcore.json runtimes: {', '.join(deploy_set)} (account {account}, region {region})")
    return 0


def cmd_scratch(args: argparse.Namespace) -> int:
    """A throwaway one-runtime AgentCore project (for `agentcore package` smoke tests) that leaves deploy/ alone."""
    available, _ = discover([args.stage])
    if not available:
        return 3
    root = Path(args.dir).resolve()
    build_stage_dir(args.stage, region=args.region, requirements=locked_requirements(),
                    with_binaries=not args.no_binaries, with_opt_in_binaries=args.with_terraform,
                    app_dir=root / "app")
    write_project([args.stage], {args.stage: stage_env(args.stage)}, path=root / "agentcore" / "agentcore.json")
    (root / "agentcore" / "aws-targets.json").write_text("[]\n")
    print(f"scratch project for {args.stage} in {root}")
    return 0


IN_PROGRESS_SUFFIX = "_IN_PROGRESS"


def plan_destroy(requested: list[str], stack: StackInfo | None, known: list[str],
                 resources: Counter | None = None, where: str = "") -> tuple[int, list[str]]:
    """Explain on stderr what destroy.sh would delete. Returns (exit code, remaining stages).

    0 = go ahead (remaining == [] means delete the whole stack), 3 = nothing to do, 2 = refuse.
    """
    def say(line: str = "") -> None:
        print(line, file=sys.stderr)

    if stack is None:
        say(f"nothing to destroy: stack {STACK_NAME} does not exist{where}.")
        return 3, []
    if stack.status.endswith(IN_PROGRESS_SUFFIX):
        say(f"stack {STACK_NAME} is {stack.status}: wait for it to finish, then retry.")
        return 2, []
    deployed = order_arns(stack.runtimes, known)
    if requested:
        not_deployed = [s for s in requested if s not in deployed]
        if not_deployed:
            warn(f"not deployed, nothing to remove: {', '.join(not_deployed)}")
        doomed = [s for s in deployed if s in requested]
        if not doomed:
            say(f"nothing to remove: none of {', '.join(requested)} is deployed in {STACK_NAME}{where}.")
            return 3, []
        remaining = [s for s in deployed if s not in requested]
        if remaining:
            say(f"Stack {STACK_NAME} ({stack.status}){where}:")
            say(f"  DELETE runtime(s) and their IAM roles: {', '.join(doomed)}")
            say(f"  keep and redeploy:                     {', '.join(remaining)}")
            return 0, remaining
        say("that is every deployed runtime, so the whole stack goes:")
    say(f"DELETE CloudFormation stack {STACK_NAME} ({stack.status}){where}:")
    say(f"  runtimes: {', '.join(deployed) or '(none in the stack outputs)'}")
    for rtype, count in sorted((resources or {}).items()):
        say(f"  {count:>3} x {rtype}")
    say("  Kept: the CDKToolkit bootstrap stack and its asset bucket, the runtime log groups, Transaction Search.")
    return 0, []


def cmd_plan_destroy(args: argparse.Namespace) -> int:
    """Read-only: print the plan on stderr, then the stack name and the stages to keep on stdout (two lines)."""
    known = all_stages()
    unknown = [s for s in args.stages if s not in known]
    if unknown:
        raise ValueError(f"unknown stage(s): {', '.join(unknown)} (known: {', '.join(known)})")
    region = args.region
    account = aws_account(region)
    where = f" in account {account}, region {region}"
    last = read_targets()
    mismatch = bool(last) and (last.get("region"), last.get("account")) != (region, account)
    if mismatch:
        warn(f"the last prepare targeted account {last.get('account')}, region {last.get('region')} "
             f"(deploy/agentcore/aws-targets.json); you are looking{where}. Set AWS_PROFILE / AWS_REGION to match.")
    stack = stack_info(region)
    resources = None
    whole_stack = stack is not None and (not args.stages or set(stack.runtimes) <= set(args.stages))
    if whole_stack:
        try:
            resources = stack_resource_counts(region)
        except Exception as exc:  # noqa: BLE001 - the listing is informational only
            warn(f"could not list the stack resources ({type(exc).__name__})")
    code, remaining = plan_destroy(args.stages, stack, known, resources, where)
    if stack is None and mismatch:
        code = 2  # probably the wrong account or region: do not let destroy.sh forget the local state
    if code == 0:
        print(STACK_NAME)
        print(" ".join(remaining))
    return code


def cmd_forget(_args: argparse.Namespace) -> int:
    """After the stack is gone: drop the local record of it, so the next deploy starts clean."""
    if DEPLOYED_STATE_JSON.is_file():
        state = json.loads(DEPLOYED_STATE_JSON.read_text())
        if state.get("targets", {}).pop(TARGET_NAME, None) is not None:
            DEPLOYED_STATE_JSON.write_text(json.dumps(state, indent=2) + "\n")
    if AGENTCORE_JSON.is_file():
        project = read_project(AGENTCORE_JSON)
        project["runtimes"] = []
        AGENTCORE_JSON.write_text(json.dumps(project, indent=2) + "\n")
    shutil.rmtree(APP_DIR, ignore_errors=True)
    ARNS_FILE.unlink(missing_ok=True)
    print(f"cleared the local deploy state ({rel(DEPLOYED_STATE_JSON)} target, agentcore.json runtimes, "
          f"{rel(APP_DIR)}/, {rel(ARNS_FILE)})")
    return 0


def sync_arns(state_path: Path, arns_path: Path, known: list[str]) -> dict[str, str] | None:
    """Write {stage: runtimeArn} from the CLI deployed state. No state file: leave arns_path untouched, return None."""
    if not state_path.is_file():
        return None
    arns = order_arns(runtime_arns_from_state(json.loads(state_path.read_text())), known)
    write_arns(arns, arns_path)
    return arns


def cmd_arns(args: argparse.Namespace) -> int:
    known = all_stages()
    arns, source = sync_arns(DEPLOYED_STATE_JSON, ARNS_FILE, known), rel(DEPLOYED_STATE_JSON)
    if arns is None:  # fresh clone: rebuild the file from the stack outputs (read-only) if the stack exists
        try:
            stack = stack_info(args.region)
        except Exception as exc:  # noqa: BLE001
            stack = None
            warn(f"CloudFormation lookup failed ({type(exc).__name__}: {str(exc)[:200]})")
        if stack is None:
            print(f"no deployed state at {rel(DEPLOYED_STATE_JSON)} and no stack {STACK_NAME} in {args.region}: "
                  f"{rel(ARNS_FILE)} left unchanged", file=sys.stderr)
            return 1
        arns, source = order_arns(stack.runtimes, known), f"the outputs of stack {STACK_NAME}"
        write_arns(arns, ARNS_FILE)
    print(f"wrote {rel(ARNS_FILE)} from {source}:")
    for stage, arn in arns.items():
        print(f"  {stage:<16}{arn}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stages", help="list deployable stages")
    p.add_argument("stages", nargs="*")
    default_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_REGION
    p = sub.add_parser("prepare", help="build app/<stage>/ dirs and write agentcore.json + aws-targets.json")
    p.add_argument("stages", nargs="*")
    p.add_argument("--all", action="store_true", help="every stage (the default when no stage is named)")
    p.add_argument("--force-subset", action="store_true",
                   help="deploy a subset even when what is deployed cannot be determined (FORCE_SUBSET=1)")
    p.add_argument("--exact", action="store_true",
                   help="deploy exactly the named stages: CloudFormation deletes every other runtime (destroy.sh)")
    p.add_argument("--region", default=default_region)
    p.add_argument("--account", help="AWS account id (default: from STS)")
    p.add_argument("--no-binaries", action="store_true", help="do not vendor any tool binary")
    p.add_argument("--with-terraform", action="store_true", help="also vendor terraform (cold init in the cloud)")
    p = sub.add_parser("scratch", help="one-runtime AgentCore project in DIR, for local packaging smoke tests")
    p.add_argument("stage")
    p.add_argument("dir")
    p.add_argument("--region", default=os.getenv("AWS_REGION") or DEFAULT_REGION)
    p.add_argument("--no-binaries", action="store_true")
    p.add_argument("--with-terraform", action="store_true")
    p = sub.add_parser("plan-destroy", help="read-only: show what destroy.sh would delete (all, or these stages)")
    p.add_argument("stages", nargs="*")
    p.add_argument("--region", default=default_region)
    sub.add_parser("forget", help="clear the local deploy state after the stack was deleted")
    p = sub.add_parser("arns", help="write deploy/agent-arns.json from the CLI deployed state (or the stack outputs)")
    p.add_argument("--region", default=default_region)
    p = sub.add_parser("invoke", help="invoke a deployed (or local) agent and print its SSE events")
    p.add_argument("stage")
    p.add_argument("payload", nargs="?", default="{}")
    p.add_argument("--local", help="POST to a local server instead, e.g. http://localhost:8080")
    p.add_argument("--raw", action="store_true", help="print one JSON event per line")
    p.add_argument("--out", type=Path, help="also save events as JSON lines")
    sub.add_parser("status", help="show runtime status for every ARN in agent-arns.json")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "stages":
            available, _ = discover(args.stages or None)
            print(" ".join(available))
            return 0
        if args.cmd == "prepare":
            return cmd_prepare(args)
        if args.cmd == "scratch":
            return cmd_scratch(args)
        if args.cmd == "plan-destroy":
            return cmd_plan_destroy(args)
        if args.cmd == "forget":
            return cmd_forget(args)
        if args.cmd == "arns":
            return cmd_arns(args)
        if args.cmd == "invoke":
            return invoke(args.stage, parse_payload(args.payload), local=args.local, raw=args.raw, out=args.out)
        if args.cmd == "status":
            return status()
    except (ValueError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if type(exc).__module__.startswith("botocore"):  # expired SSO token, AccessDenied, throttling...
            print(f"error: AWS {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        raise
    return 1


if __name__ == "__main__":
    sys.exit(main())
