"""Unit tests for deploy/deploy_tool.py (no AWS, no network, no AgentCore CLI)."""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "deploy"))

import deploy_tool as d  # noqa: E402  (deploy/ is not a package)

# Constraints copied from the AgentCore CLI schema (deploy/agentcore/.llm-context/agentcore.ts).
RUNTIME_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,22}$")
ENTRYPOINT_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_/.-]*\.(py|ts|js)(:[a-zA-Z_][a-zA-Z0-9_]*)?$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/TransformWorkshop_hello-AbCdEf1234"


@pytest.fixture
def fake_package(tmp_path):
    """A transform_agents tree with core/config.py, hello + discovery runtimes, and junk that must not ship."""
    pkg = tmp_path / "transform_agents"
    (pkg / "core").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core" / "__init__.py").write_text("")
    (pkg / "core" / "config.py").write_text('STAGES = ["discovery", "requirements", "terraform"]\nOTHER = 1\n')
    for stage in ("hello", "discovery"):
        (pkg / stage).mkdir()
        (pkg / stage / "__init__.py").write_text("")
        (pkg / stage / "runtime.py").write_text("from x import make_app\n\napp = make_app()\n")
    (pkg / "requirements").mkdir()
    (pkg / "requirements" / "runtime.py").write_text("handler = None\n")  # no `app`
    (pkg / "terraform" / "knowledge" / ".terraform").mkdir(parents=True)  # no runtime.py yet
    (pkg / "terraform" / "knowledge" / "main.tf").write_text("# blueprint\n")
    (pkg / "terraform" / "knowledge" / ".terraform" / "provider.bin").write_text("x")
    (pkg / "hello" / "__pycache__").mkdir()
    (pkg / "hello" / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"x")
    return pkg


# --------------------------------------------------------------------------------------------- stages


def test_stages_come_from_config_with_hello_first(fake_package):
    config = fake_package / "core" / "config.py"
    assert d.read_pipeline_stages(config) == ["discovery", "requirements", "terraform"]
    assert d.all_stages(config) == ["hello", "discovery", "requirements", "terraform"]


def test_real_config_stages_are_valid_runtime_names():
    stages = d.all_stages()
    assert stages[0] == "hello" and "k8s_validation" in stages
    assert PROJECT_NAME_RE.match(d.PROJECT_NAME)
    for stage in stages:
        assert RUNTIME_NAME_RE.match(stage)
        assert len(f"{d.PROJECT_NAME}_{stage}") <= 48  # AWS runtime-name limit checked by `agentcore deploy`


def test_runtime_module_ok_detects_app(tmp_path):
    ok, annotated, missing, broken = (tmp_path / n for n in ("a.py", "b.py", "c.py", "d.py"))
    ok.write_text("app = object()\n")
    annotated.write_text("app: object = object()\n")
    missing.write_text("application = object()\n")
    broken.write_text("app = (\n")
    assert d.runtime_module_ok(ok) and d.runtime_module_ok(annotated)
    assert not d.runtime_module_ok(missing) and not d.runtime_module_ok(broken)
    assert not d.runtime_module_ok(tmp_path / "nope.py")


def test_discover_skips_unfinished_stages_and_keeps_pipeline_order(fake_package, capsys):
    config = fake_package / "core" / "config.py"
    available, skipped = d.discover(None, fake_package, config)
    assert available == ["hello", "discovery"]
    assert skipped == ["requirements", "terraform"]
    err = capsys.readouterr().err
    assert "skipping requirements: runtime.py does not define `app`" in err
    assert "skipping terraform: no runtime.py" in err

    available, skipped = d.discover(["discovery", "hello", "hello"], fake_package, config)
    assert available == ["hello", "discovery"] and skipped == []


def test_discover_rejects_unknown_stage(fake_package):
    with pytest.raises(ValueError, match="unknown stage"):
        d.discover(["hello", "typo"], fake_package, fake_package / "core" / "config.py")


def test_resolve_deploy_set_keeps_already_configured_stages():
    known = ["hello", "discovery", "requirements", "terraform"]
    assert d.resolve_deploy_set(None, [], known) == known
    assert d.resolve_deploy_set(["terraform"], ["hello"], known) == ["hello", "terraform"]
    assert d.resolve_deploy_set(["discovery", "hello"], [], known) == ["hello", "discovery"]


KNOWN = ["hello", "discovery", "requirements", "terraform"]


def test_plan_deploy_set_keeps_what_the_stack_already_runs(capsys):
    stack = d.Deployment(["hello", "terraform", "retired"], True, "CloudFormation stack X")
    assert d.plan_deploy_set(None, KNOWN, KNOWN) == KNOWN  # no names: everything, no lookup needed
    assert d.plan_deploy_set(["discovery"], ["discovery"], KNOWN, deployment=stack) == ["hello", "discovery",
                                                                                         "terraform"]
    captured = capsys.readouterr()
    assert "keeping the already-deployed runtimes hello, terraform" in captured.out
    assert "retired" in captured.err  # not a workshop stage any more: the deploy deletes it, say so
    nothing = d.Deployment([], True, "CloudFormation (stack does not exist)")
    assert d.plan_deploy_set(["hello"], ["hello"], KNOWN, deployment=nothing) == ["hello"]


def test_plan_deploy_set_refuses_a_subset_when_the_deployed_set_is_unknown(capsys):
    unknown = d.Deployment([], False, "the CloudFormation lookup failed (X) and there is no state")
    assert d.plan_deploy_set(["hello"], ["hello"], KNOWN, deployment=unknown) is None
    assert d.plan_deploy_set(["hello"], ["hello"], KNOWN, deployment=unknown, force_subset=True) == ["hello"]
    assert "FORCE_SUBSET=1" in capsys.readouterr().err
    # --exact (destroy.sh) deploys exactly the named stages and needs no lookup
    assert d.plan_deploy_set(["discovery"], ["discovery"], KNOWN, exact=True) == ["discovery"]
    message = d.refuse_subset_message(["hello"], unknown)
    assert "--all" in message and "FORCE_SUBSET=1 deploy/deploy.sh hello" in message and d.STACK_NAME in message


def test_prepare_refuses_a_subset_from_a_fresh_clone(monkeypatch, capsys):
    monkeypatch.setattr(d, "aws_account", lambda region: "123456789012")
    monkeypatch.setattr(d, "current_deployment", lambda region: d.Deployment([], False, "no lookup, no state"))
    monkeypatch.setattr(d, "locked_requirements", lambda: pytest.fail("must refuse before building anything"))
    assert d.main(["prepare", "hello", "--region", "us-west-2"]) == 4
    err = capsys.readouterr().err
    assert "refusing a subset deploy" in err and "delete" in err.lower()


class FakeCfn:
    """describe_stacks / list_stack_resources double."""

    def __init__(self, stacks=None, error=None, resources=()):
        self.stacks, self.error, self.resources, self.calls = stacks, error, list(resources), []

    def describe_stacks(self, StackName):
        self.calls.append(StackName)
        if self.error:
            raise self.error
        if self.stacks is None:
            raise RuntimeError(f"An error occurred (ValidationError): Stack with id {StackName} does not exist")
        return {"Stacks": self.stacks}

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        pages = [{"StackResourceSummaries": self.resources}]
        return type("P", (), {"paginate": lambda _self, StackName: iter(pages)})()


def stack_outputs(**arns):
    out = [{"OutputKey": "StackNameOutput", "OutputValue": d.STACK_NAME}]
    for name, arn in arns.items():
        out += [{"OutputKey": f"{name}RuntimeArnOutput", "OutputValue": arn, "Description": f"Runtime ARN for agent: {name}"},
                {"OutputKey": f"{name}RuntimeIdOutput", "OutputValue": arn.rsplit("/", 1)[-1],
                 "Description": f"Runtime ID for agent: {name}"},
                {"OutputKey": f"{name}RoleArnOutput", "OutputValue": "arn:aws:iam::1:role/r",
                 "Description": f"Execution Role ARN for agent: {name}"}]
    return out


K8S_ARN = ARN.replace("hello", "k8s_validation")


def test_stack_name_matches_the_cdk_app_and_the_shell_scripts():
    assert d.STACK_NAME == "AgentCore-TransformWorkshop-default"
    assert f'STACK_NAME="{d.STACK_NAME}"' in (d.DEPLOY_DIR / "_common.sh").read_text()
    cdk = (d.DEPLOY_DIR / "agentcore" / "cdk" / "bin" / "cdk.ts").read_text()
    assert "return `AgentCore-${sanitize(projectName)}-${sanitize(targetName)}`;" in cdk


def test_runtime_arns_from_stack_outputs():
    assert d.runtime_arns_from_outputs(stack_outputs(hello=ARN, k8s_validation=K8S_ARN)) == {
        "hello": ARN, "k8s_validation": K8S_ARN}
    # without the CDK description, the name comes from the runtime id (<project>_<name>-<suffix>)
    assert d.runtime_arns_from_outputs([{"OutputKey": "X", "OutputValue": K8S_ARN}]) == {"k8s_validation": K8S_ARN}
    assert d.runtime_arns_from_outputs(None) == {}


def test_stack_info_reads_outputs_and_treats_missing_stack_as_none():
    fake = FakeCfn([{"StackStatus": "UPDATE_COMPLETE", "Outputs": stack_outputs(hello=ARN)}])
    info = d.stack_info("us-west-2", client=fake)
    assert info == d.StackInfo(d.STACK_NAME, "UPDATE_COMPLETE", {"hello": ARN}) and fake.calls == [d.STACK_NAME]
    assert d.stack_info("us-west-2", client=FakeCfn(None)) is None
    with pytest.raises(PermissionError):
        d.stack_info("us-west-2", client=FakeCfn(error=PermissionError("AccessDenied")))
    counts = d.stack_resource_counts("us-west-2", client=FakeCfn(resources=[
        {"ResourceType": "AWS::BedrockAgentCore::Runtime", "ResourceStatus": "UPDATE_COMPLETE"},
        {"ResourceType": "AWS::IAM::Role", "ResourceStatus": "CREATE_COMPLETE"},
        {"ResourceType": "AWS::IAM::Role", "ResourceStatus": "DELETE_COMPLETE"}]))
    assert counts == {"AWS::BedrockAgentCore::Runtime": 1, "AWS::IAM::Role": 1}


def test_current_deployment_prefers_the_stack_then_the_cli_state(tmp_path, capsys):
    state, project = tmp_path / "deployed-state.json", tmp_path / "agentcore.json"
    up = d.StackInfo(d.STACK_NAME, "UPDATE_COMPLETE", {"hello": ARN, "discovery": ARN})

    def failing(region):
        raise PermissionError("AccessDenied")

    dep = d.current_deployment("us-west-2", state_path=state, project_path=project, lookup=lambda r: up)
    assert dep.known and dep.stages == ["hello", "discovery"] and dep.stack == up
    dep = d.current_deployment("us-west-2", state_path=state, project_path=project, lookup=lambda r: None)
    assert dep.known and dep.stages == []  # verified: nothing deployed, a subset deploy deletes nothing
    dep = d.current_deployment("us-west-2", state_path=state, project_path=project, lookup=failing)
    assert not dep.known and "AccessDenied" in dep.source  # fresh clone and no lookup: unknown
    state.write_text(json.dumps(deployed_state(finops=ARN)))
    d.write_project(["hello"], {}, project)
    dep = d.current_deployment("us-west-2", state_path=state, project_path=project, lookup=failing)
    assert dep.known and dep.stages == ["finops", "hello"]
    assert "using the local CLI state" in capsys.readouterr().err
    assert d.deployed_state_stages(tmp_path / "missing.json") is None


def test_plan_destroy(capsys):
    known = ["hello", "discovery", "finops"]
    up = d.StackInfo(d.STACK_NAME, "UPDATE_COMPLETE", {"finops": ARN, "hello": ARN})
    assert d.plan_destroy([], None, known) == (3, [])
    assert "does not exist" in capsys.readouterr().err
    assert d.plan_destroy([], d.StackInfo(d.STACK_NAME, "UPDATE_IN_PROGRESS", {}), known) == (2, [])
    assert d.plan_destroy([], up, known, {"AWS::IAM::Role": 2}) == (0, [])  # [] = delete the whole stack
    err = capsys.readouterr().err
    assert f"DELETE CloudFormation stack {d.STACK_NAME}" in err and "hello, finops" in err
    assert "2 x AWS::IAM::Role" in err and "CDKToolkit" in err
    assert d.plan_destroy(["finops"], up, known) == (0, ["hello"])
    assert "DELETE runtime(s) and their IAM roles: finops" in capsys.readouterr().err
    assert d.plan_destroy(["finops", "hello"], up, known) == (0, [])  # every runtime: the whole stack
    assert d.plan_destroy(["discovery"], up, known) == (3, [])  # not deployed: nothing to do
    assert d.plan_destroy([], d.StackInfo(d.STACK_NAME, "ROLLBACK_COMPLETE", {}), known) == (0, [])


# --------------------------------------------------------------------------------------------- packaging


def test_filter_requirements_drops_otel_pins_and_adds_adot():
    lines = ["# comment", "", "boto3==1.43.115", "    # via strands-agents", "-e .",
             "opentelemetry-api==1.45.1", "OpenTelemetry-SDK==1.45.1",
             "cffi==2.1.1 ; platform_python_implementation != 'PyPy'"]
    reqs = d.filter_requirements(lines)
    assert reqs == ["boto3==1.43.115", "cffi==2.1.1 ; platform_python_implementation != 'PyPy'",
                    *d.EXTRA_REQUIREMENTS]
    assert any(r.startswith("aws-opentelemetry-distro==") for r in reqs)


def test_render_pyproject_is_valid_toml_with_markers():
    reqs = ["boto3==1.43.115", "pywin32==312 ; sys_platform == 'win32'", "x==1 ; python_version >= \"3.12\""]
    data = tomllib.loads(d.render_pyproject("k8s_validation", reqs))
    assert data["project"]["name"] == "transform-agent-k8s-validation"
    assert data["project"]["dependencies"] == reqs


def test_render_main_compiles_and_targets_the_stage_runtime():
    src = d.render_main("terraform", "eu-west-1")
    compile(src, "main.py", "exec")
    assert "from transform_agents.terraform.runtime import app" in src
    assert 'os.environ.setdefault("AWS_REGION", "eu-west-1")' in src
    assert 'if __name__ == "__main__":\n    app.run()' in src
    assert "UV_CACHE_DIR" not in src


def test_render_main_sets_writable_uv_dirs_for_modernization():
    src = d.render_main("modernization", "us-west-2")
    compile(src, "main.py", "exec")
    assert 'os.environ.setdefault("UV_CACHE_DIR", "/tmp/uv-cache")' in src
    assert 'os.environ.setdefault("UV_PYTHON_INSTALL_DIR", "/tmp/uv-python")' in src
    assert src.index("UV_CACHE_DIR") < src.index("from transform_agents.modernization.runtime import app")
    assert all(v.startswith("/tmp/") for env in d.MAIN_ENV_DEFAULTS.values() for v in env.values())


def test_stage_env_passthrough_is_stage_scoped():
    environ = {"MODEL_TERRAFORM": "mistral.x", "MODEL_DISCOVERY": "nova.y", "MODEL_ID": "global",
               "MODEL_MAX_TOKENS": "4096", "HOME": "/root", "MODEL_FINOPS": ""}
    assert d.stage_env("discovery", environ) == {"MODEL_DISCOVERY": "nova.y", "MODEL_ID": "global",
                                                 "MODEL_MAX_TOKENS": "4096"}
    tf = d.stage_env("terraform", {**environ, "TERRAFORM_OFFLINE": "1"})
    assert tf["MODEL_TERRAFORM"] == "mistral.x" and "MODEL_DISCOVERY" not in tf
    assert tf["TF_WORKSHOP_CACHE"].startswith("/tmp/")  # the code dir and ~ are not writable on AgentCore
    assert tf["TERRAFORM_OFFLINE"] == "1" and "TERRAFORM_OFFLINE" not in d.stage_env("finops", environ)
    assert d.stage_env("finops", environ) == {"MODEL_ID": "global", "MODEL_MAX_TOKENS": "4096"}
    assert all(ENV_NAME_RE.match(k) for k in tf)


def test_build_stage_dir_layout_and_manifest(fake_package, tmp_path):
    reqs = ["boto3==1.43.115", *d.EXTRA_REQUIREMENTS]
    manifest = d.build_stage_dir("hello", region="us-west-2", requirements=reqs, app_dir=tmp_path / "app",
                                 package_dir=fake_package, environ={"MODEL_HELLO": "m"})
    out = tmp_path / "app" / "hello"
    assert (out / "main.py").is_file() and (out / "pyproject.toml").is_file()
    assert (out / "transform_agents" / "core" / "config.py").is_file()
    assert (out / "transform_agents" / "terraform" / "knowledge" / "main.tf").is_file()
    assert not list(out.rglob("__pycache__")) and not list(out.rglob("*.pyc"))
    assert not list(out.rglob(".terraform"))
    assert not (out / "vendor").exists()
    assert manifest == json.loads((out / "build-manifest.json").read_text())
    assert manifest["module"] == "transform_agents.hello.runtime:app"
    assert manifest["runtime_version"] == d.RUNTIME_VERSION and manifest["requirements"] == 2
    assert manifest["binaries"] == {} and manifest["env"] == ["MODEL_HELLO"]

    (out / "stale.txt").write_text("old")  # a rebuild starts from scratch
    d.build_stage_dir("hello", region="us-west-2", requirements=reqs, app_dir=tmp_path / "app",
                      package_dir=fake_package, environ={})
    assert not (out / "stale.txt").exists()


def test_build_stage_dir_vendors_stage_binaries(fake_package, tmp_path, monkeypatch):
    cached = tmp_path / "cache" / "kubeconform"
    cached.parent.mkdir()
    cached.write_bytes(b"\x7fELF fake")
    monkeypatch.setattr(d, "fetch_binary", lambda binary, cache_dir: cached)
    manifest = d.build_stage_dir("k8s_validation", region="us-west-2", requirements=[], app_dir=tmp_path / "app",
                                 package_dir=fake_package, environ={})
    for tool in ("kubeconform", "kubectl"):
        assert (tmp_path / "app" / "k8s_validation" / "vendor" / "bin" / tool).read_bytes() == b"\x7fELF fake"
    assert manifest["binaries"] == {t: d.BINARIES[t].version for t in ("kubeconform", "kubectl")}
    skipped = d.build_stage_dir("k8s_validation", region="us-west-2", requirements=[], app_dir=tmp_path / "app2",
                                package_dir=fake_package, environ={}, with_binaries=False)
    assert skipped["binaries"] == {}


def test_terraform_binary_is_opt_in():
    assert d.stage_binaries("modernization") == ["uv"]  # run_service_tests runs the service's pytest with uv
    assert d.stage_binaries("terraform") == []
    assert d.stage_binaries("terraform", opt_in=True) == ["terraform"]
    assert d.stage_binaries("k8s_validation") == ["kubeconform", "kubectl"]
    assert d.stage_binaries("k8s_validation", with_binaries=False, opt_in=True) == []
    assert d.stage_binaries("hello", opt_in=True) == []


def _fake_binary(tmp_path, archive: str, payload: bytes):
    buf = io.BytesIO()
    if "." not in archive:  # a plain executable download (kubectl)
        return d.Binary("tool", "1.0", f"https://example.invalid/{archive}", hashlib.sha256(payload).hexdigest(), ""), payload
    if archive.endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("tool", payload)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("tool")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    data = buf.getvalue()
    return d.Binary("tool", "1.0", f"https://example.invalid/{archive}", hashlib.sha256(data).hexdigest(), "tool"), data


@pytest.mark.parametrize("archive", ["tool_linux_arm64.zip", "tool-linux-arm64.tar.gz", "tool"])
def test_fetch_binary_verifies_checksum_extracts_and_caches(tmp_path, monkeypatch, archive):
    binary, data = _fake_binary(tmp_path, archive, b"#!/bin/sh\necho hi\n")
    calls = []

    def fake_urlopen(url, timeout):
        calls.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(d.urllib.request, "urlopen", fake_urlopen)
    path = d.fetch_binary(binary, tmp_path / "cache")
    assert path.read_bytes() == b"#!/bin/sh\necho hi\n"
    assert os.access(path, os.X_OK)
    assert d.fetch_binary(binary, tmp_path / "cache") == path and len(calls) == 1  # cached

    bad = d.Binary("tool", "2.0", binary.url, "0" * 64, "tool")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        d.fetch_binary(bad, tmp_path / "cache")


def test_pinned_binaries_are_linux_arm64():
    for binary in d.BINARIES.values():
        assert "linux" in binary.url and re.search(r"arm64|aarch64", binary.url) and len(binary.sha256) == 64
        assert binary.url.startswith("https://") and binary.version in binary.url
    assert set(d.STAGE_BINARIES) | set(d.OPT_IN_BINARIES) <= set(d.all_stages())
    assert {b for bins in [*d.STAGE_BINARIES.values(), *d.OPT_IN_BINARIES.values()] for b in bins} <= set(d.BINARIES)


def test_generated_main_exposes_vendored_binaries_and_imports_the_app(tmp_path):
    """Run a generated main.py (as AgentCore would import it) against a minimal fake package."""
    root = tmp_path / "pkg"
    (root / "transform_agents" / "demo").mkdir(parents=True)
    (root / "transform_agents" / "__init__.py").write_text("")
    (root / "transform_agents" / "demo" / "__init__.py").write_text("")
    (root / "transform_agents" / "demo" / "runtime.py").write_text("app = 'the-app'\n")
    tool = root / "vendor" / "bin" / "fake-tool"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\necho vendored-ok\n")
    tool.chmod(0o644)  # zip deploys drop the executable bit
    (root / "main.py").write_text(d.render_main("demo", "us-west-2"))
    bin_dir = tmp_path / "vendor-bin"
    env = {"PATH": os.environ["PATH"], "VENDOR_BIN_DIR": str(bin_dir)}
    script = "import main, shutil, subprocess; print(main.app); print(subprocess.check_output(['fake-tool']).decode().strip())"
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, '.'); {script}"],
                         cwd=root, env=env, capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ["the-app", "vendored-ok"]
    assert os.access(bin_dir / "fake-tool", os.X_OK)


# --------------------------------------------------------------------------------------------- project config


def test_runtime_spec_matches_cli_schema():
    spec = d.runtime_spec("k8s_validation", {"MODEL_ID": "x"})
    assert RUNTIME_NAME_RE.match(spec["name"]) and ENTRYPOINT_RE.match(spec["entrypoint"])
    assert spec["build"] == "CodeZip" and spec["runtimeVersion"] == "PYTHON_3_12"
    assert spec["codeLocation"] == "app/k8s_validation/" and spec["networkMode"] == "PUBLIC"
    assert spec["envVars"] == [{"name": "MODEL_ID", "value": "x"}]
    assert spec["instrumentation"] == {"enableOtel": True}
    assert len(spec["description"]) <= 200
    lc = spec["lifecycleConfiguration"]
    assert 60 <= lc["idleRuntimeSessionTimeout"] <= lc["maxLifetime"] <= 28800
    assert "envVars" not in d.runtime_spec("hello", {})
    # additionalPolicies resolve relative to codeLocation (deploy/app/<stage>/) -> deploy/policies/
    policy = (d.DEPLOY_DIR / "app" / "k8s_validation" / spec["additionalPolicies"][0]).resolve()
    assert policy == d.EXTRA_POLICY.resolve()


def policy_actions() -> set[str]:
    doc = json.loads(d.EXTRA_POLICY.read_text())
    return {a for st in doc["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])}


# boto3 call in transform_agents -> the IAM action it needs. A new AWS call fails the test below until it is mapped
# here and granted in deploy/policies/runtime-extras.json.
AGENT_AWS_CALLS = {
    "describe_instances": "ec2:DescribeInstances",  # discovery live_aws
    "describe_volumes": "ec2:DescribeVolumes",  # discovery live_aws (root volume)
    "describe_instance_types": "ec2:DescribeInstanceTypes",  # finops live pricing
    "describe_spot_price_history": "ec2:DescribeSpotPriceHistory",  # finops live pricing
    "get_metric_statistics": "cloudwatch:GetMetricStatistics",  # discovery utilization
    "get_metric_data": "cloudwatch:GetMetricData",
    "list_metrics": "cloudwatch:ListMetrics",  # discovery memory metric lookup
    "get_products": "pricing:GetProducts",  # finops live pricing (paginator)
    "describe_savings_plans_offering_rates": "savingsplans:DescribeSavingsPlansOfferingRates",  # finops
    "describe_cluster_versions": "eks:DescribeClusterVersions",  # finops live pricing
}
AWS_CALL_RE = re.compile(r'\.(describe_[a-z_]+|get_metric_[a-z_]+|list_metrics)\(|get_paginator\("([a-z_]+)"\)')


def test_extra_policy_grants_every_aws_call_the_agents_make():
    sources = [p for p in d.PACKAGE_DIR.rglob("*.py") if "knowledge" not in p.parts and "tests" not in p.parts]
    calls = {m.group(1) or m.group(2) for p in sources for m in AWS_CALL_RE.finditer(p.read_text())}
    assert {"describe_instances", "get_metric_statistics", "get_products"} <= calls  # the scan finds them
    unmapped = calls - set(AGENT_AWS_CALLS)
    assert not unmapped, f"map these boto3 calls to IAM actions in AGENT_AWS_CALLS: {sorted(unmapped)}"
    missing = {AGENT_AWS_CALLS[c] for c in calls} - policy_actions()
    assert not missing, f"grant in deploy/policies/runtime-extras.json: {sorted(missing)}"


def test_extra_policy_is_valid_and_read_only_for_agents():
    actions = policy_actions()
    doc = json.loads(d.EXTRA_POLICY.read_text())
    for needed in ("pricing:GetProducts", "pricing:DescribeServices", "ec2:DescribeInstances",
                   "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData", "xray:GetSamplingRules",
                   "cloudwatch:PutMetricData"):
        assert needed in actions
    assert not [a for a in actions if "*" in a]  # no wildcards: ec2:Describe* includes DescribeInstanceAttribute
    assert "ec2:DescribeInstanceAttribute" not in actions  # can read EC2 user data
    assert all(a.split(":")[1].startswith(("Describe", "Get", "List", "Put")) for a in actions)
    mutating = {a for a in actions if a.split(":")[1].startswith(("Create", "Delete", "Put", "Run", "Terminate"))}
    assert mutating == {"cloudwatch:PutMetricData"}  # telemetry only, namespace-restricted below
    put = next(st for st in doc["Statement"] if st["Action"] == "cloudwatch:PutMetricData")
    assert put["Condition"] == {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}


def test_write_project_roundtrip_preserves_other_settings(tmp_path):
    path = tmp_path / "agentcore" / "agentcore.json"
    assert d.configured_stages(path) == []
    d.write_project(["hello", "discovery"], {"discovery": {"MODEL_ID": "m"}}, path)
    project = json.loads(path.read_text())
    assert project["name"] == d.PROJECT_NAME and project["managedBy"] == "CDK"
    assert d.configured_stages(path) == ["hello", "discovery"]
    project["memories"] = [{"name": "keep-me"}]
    path.write_text(json.dumps(project))
    d.write_project(["hello"], {}, path)
    assert d.configured_stages(path) == ["hello"]
    assert json.loads(path.read_text())["memories"] == [{"name": "keep-me"}]


def test_write_targets(tmp_path):
    path = tmp_path / "aws-targets.json"
    d.write_targets("123456789012", "us-west-2", path)
    [target] = json.loads(path.read_text())
    assert target["name"] == "default" and target["account"] == "123456789012" and target["region"] == "us-west-2"


# --------------------------------------------------------------------------------------------- ARN file


def deployed_state(**runtimes):
    return {"targets": {"default": {"resources": {"runtimes": {
        name: {"runtimeId": arn.rsplit("/", 1)[-1], "runtimeArn": arn, "roleArn": "arn:aws:iam::1:role/r"}
        for name, arn in runtimes.items()}}}}}


def test_arns_from_deployed_state_in_pipeline_order(tmp_path):
    known = ["hello", "discovery", "finops"]
    state = deployed_state(finops=ARN.replace("hello", "finops"), hello=ARN, extra=ARN.replace("hello", "extra"))
    arns = d.order_arns(d.runtime_arns_from_state(state), known)
    assert list(arns) == ["hello", "finops", "extra"]
    assert d.runtime_arns_from_state({"targets": {}}) == {}
    assert d.runtime_arns_from_state({"targets": {"default": {"resources": {}}}}) == {}


def test_sync_and_read_arns(tmp_path):
    state_path, arns_path = tmp_path / "deployed-state.json", tmp_path / "agent-arns.json"
    assert d.sync_arns(state_path, arns_path, ["hello"]) is None
    assert not arns_path.exists()
    arns_path.write_text(json.dumps({"hello": ARN}))  # no state (fresh clone): the ARN file is left untouched
    assert d.sync_arns(state_path, arns_path, ["hello"]) is None
    assert d.read_arns(arns_path) == {"hello": ARN}
    arns_path.unlink()
    state_path.write_text(json.dumps(deployed_state(hello=ARN)))
    assert d.sync_arns(state_path, arns_path, ["hello"]) == {"hello": ARN}
    assert d.read_arns(arns_path) == {"hello": ARN}
    assert d.read_arns(tmp_path / "missing.json") == {}
    arns_path.write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError):
        d.read_arns(arns_path)


def test_region_from_arn():
    assert d.region_from_arn(ARN) == "us-west-2"
    with pytest.raises(ValueError):
        d.region_from_arn("arn:aws:lambda:us-west-2:1:function:f")


# --------------------------------------------------------------------------------------------- invoke


def test_parse_payload(tmp_path):
    assert d.parse_payload('{"instruction": "x", "files": {}}') == {"instruction": "x", "files": {}}
    assert d.parse_payload("List the files") == {"instruction": "List the files"}
    f = tmp_path / "p.json"
    f.write_text('{"run_id": "r1"}')
    assert d.parse_payload(f"@{f}") == {"run_id": "r1"}
    with pytest.raises(TypeError):
        d.parse_payload("[1, 2]")


def test_session_id_length_fits_invoke_agent_runtime():
    for stage in ("hello", "k8s_validation"):
        assert 33 <= len(d.session_id(stage)) <= 256
    assert d.session_id("hello") != d.session_id("hello")


def test_iter_sse_events_parses_contract_lines():
    lines = [b'data: {"type": "status", "stage": "hello", "message": "go"}', b"", b": keepalive",
             'data: "{\\"type\\": \\"text\\", \\"delta\\": \\"hi\\"}"', b"data: not-json", b'data: "plain words"',
             b'{"type": "error", "stage": "hello", "message": "bad request"}']
    events = list(d.iter_sse_events(lines))
    assert events == [{"type": "status", "stage": "hello", "message": "go"}, {"type": "text", "delta": "hi"},
                      {"type": "raw", "data": "not-json"}, {"type": "text", "delta": "plain words"},
                      {"type": "error", "stage": "hello", "message": "bad request"}]


def test_event_printer_streams_text_and_breaks_lines_between_events():
    buf = io.StringIO()
    printer = d.EventPrinter(out=buf)
    for event in [{"type": "status", "stage": "s", "message": "started", "model": "m"},
                  {"type": "text", "delta": "Hel"}, {"type": "text", "delta": "lo"},
                  {"type": "tool_start", "id": "1", "name": "read_file", "input": '{"path": "a"}'},
                  {"type": "tool_end", "id": "1", "name": "read_file", "status": "success", "output": "x\ny"},
                  {"type": "artifact", "path": "r.md", "bytes": 3},
                  {"type": "result", "stage": "s", "summary": "done", "data": {"k": 1}, "artifacts": {"r.md": "abc"},
                   "metrics": {}},
                  {"type": "error", "stage": "s", "message": "boom"}]:
        printer(event)
    lines = buf.getvalue().splitlines()
    assert lines[:4] == ["[s] started [m]", "Hello", '-> read_file({"path": "a"})', "<- read_file [success] x y"]
    assert "+ artifact r.md (3 bytes)" in lines and "== result: s ==" in lines
    assert "artifact: r.md (3 chars)" in lines and lines[-1] == "ERROR [s]: boom"


def test_status_without_arns(tmp_path, capsys):
    assert d.status(tmp_path / "agent-arns.json") == 0
    assert "run deploy/deploy.sh" in capsys.readouterr().out


def test_invoke_without_arn_explains_how_to_deploy(tmp_path, capsys):
    assert d.invoke("hello", {}, arns_path=tmp_path / "agent-arns.json") == 2
    assert "deploy/deploy.sh" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------- destroy


def test_forget_clears_only_the_local_record(tmp_path, monkeypatch):
    state, project = tmp_path / ".cli" / "deployed-state.json", tmp_path / "agentcore.json"
    app_dir, arns = tmp_path / "app", tmp_path / "agent-arns.json"
    state.parent.mkdir()
    state.write_text(json.dumps({**deployed_state(hello=ARN), "other": 1}))
    d.write_project(["hello"], {}, project)
    (app_dir / "hello").mkdir(parents=True)
    arns.write_text(json.dumps({"hello": ARN}))
    for name, value in {"DEPLOYED_STATE_JSON": state, "AGENTCORE_JSON": project, "APP_DIR": app_dir,
                        "ARNS_FILE": arns}.items():
        monkeypatch.setattr(d, name, value)
    assert d.main(["forget"]) == 0
    assert json.loads(state.read_text()) == {"targets": {}, "other": 1}
    assert d.configured_stages(project) == [] and json.loads(project.read_text())["name"] == d.PROJECT_NAME
    assert not app_dir.exists() and not arns.exists()


def test_destroy_script_confirms_and_never_trusts_agentcore_json():
    script = (d.DEPLOY_DIR / "destroy.sh").read_text()
    assert "plan-destroy" in script and "AUTO_APPROVE" in script and "confirm " in script
    assert "remove all" not in script  # `agentcore remove all` without agentcore.json targets a "Project" stack
    assert script.index("confirm ") < script.index("delete-stack")


def test_plan_destroy_command_prints_stack_and_remaining_and_guards_the_wrong_account(monkeypatch, capsys):
    up = d.StackInfo(d.STACK_NAME, "UPDATE_COMPLETE", {"hello": ARN, "finops": ARN})
    monkeypatch.setattr(d, "aws_account", lambda region: "123456789012")
    monkeypatch.setattr(d, "stack_info", lambda region: up)
    monkeypatch.setattr(d, "stack_resource_counts", lambda region: {"AWS::IAM::Role": 2})
    monkeypatch.setattr(d, "read_targets", lambda: {"account": "123456789012", "region": "us-west-2"})
    assert d.main(["plan-destroy", "finops", "--region", "us-west-2"]) == 0
    assert capsys.readouterr().out.splitlines() == [d.STACK_NAME, "hello"]
    assert d.main(["plan-destroy", "--region", "us-west-2"]) == 0
    assert capsys.readouterr().out.splitlines() == [d.STACK_NAME, ""]
    # nothing found, but the last deploy went to another account: refuse, so destroy.sh keeps the local state
    monkeypatch.setattr(d, "stack_info", lambda region: None)
    monkeypatch.setattr(d, "read_targets", lambda: {"account": "999999999999", "region": "us-west-2"})
    assert d.main(["plan-destroy", "--region", "us-west-2"]) == 2
    assert "999999999999" in capsys.readouterr().err
    monkeypatch.setattr(d, "read_targets", lambda: {})
    assert d.main(["plan-destroy", "--region", "us-west-2"]) == 3


def test_arns_command_falls_back_to_the_stack_outputs(tmp_path, monkeypatch):
    arns = tmp_path / "agent-arns.json"
    monkeypatch.setattr(d, "DEPLOYED_STATE_JSON", tmp_path / "missing-state.json")
    monkeypatch.setattr(d, "ARNS_FILE", arns)
    monkeypatch.setattr(d, "stack_info", lambda region: None)
    arns.write_text(json.dumps({"hello": ARN}))
    assert d.main(["arns", "--region", "us-west-2"]) == 1
    assert d.read_arns(arns) == {"hello": ARN}  # untouched
    monkeypatch.setattr(d, "stack_info", lambda region: d.StackInfo(d.STACK_NAME, "UPDATE_COMPLETE",
                                                                     {"finops": K8S_ARN, "hello": ARN}))
    assert d.main(["arns", "--region", "us-west-2"]) == 0
    assert list(d.read_arns(arns)) == ["hello", "finops"]  # pipeline order
