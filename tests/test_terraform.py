"""Unit tests for the Terraform agent's deterministic parts (no network, no LLM)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from transform_agents.core import AgentRequest, ResultBox, Workspace
from transform_agents.terraform import agent as tf_agent
from transform_agents.terraform.hcl import TfConfig, coerce, parse, set_defaults, tfvars_example
from transform_agents.terraform.summary import expected_values, facts, spec_checks
from transform_agents.terraform.tools import TerraformRun, describe_schema, normalize_coverage, terraform_tools
from transform_agents.terraform.validate import (
    blueprint_files,
    cache_dir,
    knowledge,
    signature,
    terraform_bin,
    validate,
    version_matches,
)

FIXTURES = Path(__file__).parent / "fixtures" / "terraform_inputs"
SPEC = json.loads((FIXTURES / "spec.json").read_text())
MODERNIZATION = json.loads((FIXTURES / "modernization.json").read_text())
# The values the mapping rules give for the fixture spec (what a correct agent sends to set_variables).
SPEC_VALUES = {
    "name": "inventory-service", "environment": "prod", "region": "us-west-2",
    "tags": {"app": "inventory-service", "environment": "prod", "managed-by": "terraform",
             "migrated-by": "transform-agentcore"},
    "kubernetes_version": "1.33", "node_architecture": "arm64", "node_instance_types": ["t4g.medium"],
    "node_capacity_type": "ON_DEMAND", "node_min": 2, "node_max": 4, "az_count": 2, "single_nat_gateway": True,
    "ecr_repository_name": "inventory-api", "db_engine_version": "16", "db_instance_class": "db.t4g.small",
    "db_multi_az": True, "db_backup_retention_days": 7, "db_deletion_protection": True,
}
CTX = SimpleNamespace  # stands in for strands.ToolContext


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("TERRAFORM_OFFLINE", "1")


def broken(replacements: dict[str, str], file: str = "main.tf") -> dict[str, str]:
    files = blueprint_files()
    for old, new in replacements.items():
        assert old in files[file], old
        files[file] = files[file].replace(old, new)
    return files


def errors_of(files: dict[str, str]) -> list[str]:
    return [str(e) for e in validate(files).errors]


# --- blueprint and knowledge ----------------------------------------------------------------------------------------


def test_blueprint_is_valid_offline():
    report = validate(blueprint_files())
    assert report.valid and report.level == "offline", report.text()
    assert not report.warnings


def test_knowledge_matches_pinned_module_versions():
    mods = knowledge()["modules"]
    eks, vpc = mods["terraform-aws-modules/eks/aws"], mods["terraform-aws-modules/vpc/aws"]
    assert version_matches("~> 21.0", eks["version"]) and version_matches("~> 6.0", vpc["version"])
    assert {"name", "kubernetes_version", "eks_managed_node_groups", "addons"} <= set(eks["inputs"])
    assert "cluster_name" not in eks["inputs"]
    assert "desired_size" in eks["inputs"]["eks_managed_node_groups"]["attributes"]
    assert "node_security_group_id" in eks["outputs"] and "private_subnets" in vpc["outputs"]
    assert "aws_db_instance" in knowledge()["resources"]


@pytest.mark.parametrize("constraint,version,ok", [
    ("~> 21.0", "21.29.0", True), ("~> 20.0", "21.29.0", False), ("~> 6.0", "6.7.3", True),
    (">= 5, < 7", "6.7.3", True), ("21.29.0", "21.29.0", True), ("~> 21.29.1", "21.29.0", False),
])
def test_version_matches(constraint, version, ok):
    assert version_matches(constraint, version) is ok


# --- layered validation ---------------------------------------------------------------------------------------------


def test_pre_v21_eks_arguments_get_did_you_mean_hints():
    errs = errors_of(broken({'  name                                     = "${var.name}-eks"': '  cluster_name = "x"',
                             "  kubernetes_version                       = var.kubernetes_version":
                             "  cluster_version = var.kubernetes_version"}))
    assert any('unknown argument "cluster_name"' in e and '"name"' in e and e.startswith("main.tf:") for e in errs)
    assert any('"cluster_version"' in e and "kubernetes_version" in e for e in errs)


def test_unknown_node_group_attribute_is_caught():
    # terraform validate silently drops unknown object attributes; the offline layer does not.
    errs = errors_of(broken({"desired_size   = var.node_min": "desired_capacity = var.node_min"}))
    assert any("desired_capacity" in e and "desired_size" in e for e in errs)


def test_reference_errors():
    errs = errors_of(broken({"subnet_ids                               = module.vpc.private_subnets":
                             "subnet_ids = module.vpc.private_subnet_ids",
                             "[aws_security_group.db.id]": "[aws_security_group.database.id]",
                             "instance_class = var.db_instance_class": "instance_class = var.db_class"}))
    assert any('no output "private_subnet_ids"' in e and "private_subnets" in e for e in errs)
    assert any("aws_security_group.database is not declared" in e for e in errs)
    assert any("var.db_class is not declared" in e for e in errs)


def test_policy_guardrails():
    errs = errors_of(broken({"storage_encrypted     = true": "storage_encrypted     = false",
                             'image_tag_mutability = "IMMUTABLE"': 'image_tag_mutability = "MUTABLE"'}))
    assert any("storage_encrypted must be true" in e for e in errs)
    assert any("IMMUTABLE" in e for e in errs)
    errs = errors_of(broken({'default     = ["t3.medium"]': 'default     = ["t4g.medium"]'}, "variables.tf"))
    assert any("do not match architecture x86_64" in e for e in errs)


def test_resource_arguments_and_versions():
    errs = errors_of(broken({"  multi_az       = var.db_multi_az": "  multi_azs = var.db_multi_az",
                             'version = "~> 21.0"': 'version = "~> 20.0"'}))
    assert any('unknown argument "multi_azs"' in e and "multi_az" in e for e in errs)
    assert any('version "~> 20.0"' in e for e in errs)


def test_duplicates_and_parse_errors_have_line_numbers():
    files = blueprint_files()
    files["extra.tf"] = 'variable "name" {\n  type = string\n}\n'
    assert any('duplicate variable "name"' in e for e in errors_of(files))
    files = blueprint_files()
    files["main.tf"] += '\nresource "aws_s3_bucket" "x" {\n  bucket =\n}\n'
    report = validate(files)
    assert not report.valid and report.errors[0].check == "parse" and report.errors[0].line


def test_terraform_layer_uses_warm_cache_when_present(monkeypatch):
    monkeypatch.delenv("TERRAFORM_OFFLINE")
    files = blueprint_files()
    cfg = TfConfig({n: parse(t) for n, t in files.items()})
    if not terraform_bin() or not (cache_dir() / "warm" / signature(cfg) / ".terraform").is_dir():
        pytest.skip("terraform binary or warm cache missing (run: python -m transform_agents.terraform.tools --warmup)")
    report = validate(files)
    assert report.valid and report.level == "terraform", report.text()
    assert any("warm cache" in n for n in report.notes)


# --- variable editing -----------------------------------------------------------------------------------------------


def test_set_defaults_edits_in_place_and_reports():
    text = blueprint_files()["variables.tf"]
    new, changes, errs = set_defaults(text, {**SPEC_VALUES, "node_minimum": 3})
    assert len(changes) == 14 and errs == ["unknown variable 'node_minimum'; did you mean 'node_min' or 'node_max'?"]
    variables = TfConfig({"v.tf": parse(new)}).variables()
    for name, value in SPEC_VALUES.items():
        assert variables[name]["default"] == value, name
    assert '  default     = "inventory-service"' in new  # aligned `=` kept
    assert set_defaults(new, SPEC_VALUES)[1] == []  # idempotent


def test_set_defaults_type_errors_and_coercion():
    text = blueprint_files()["variables.tf"]
    values = {"node_min": "two", "db_multi_az": "true", "node_instance_types": "t4g.large"}
    _, changes, errs = set_defaults(text, values)
    assert any(e.startswith("node_min:") for e in errs)
    assert "db_multi_az: false -> true" in changes and 'node_instance_types: ["t3.medium"] -> ["t4g.large"]' in changes
    assert coerce(1.33, "string") == "1.33" and coerce("3", "number") == 3


def test_tfvars_example_matches_defaults():
    text = blueprint_files()["variables.tf"]
    parsed = parse(tfvars_example(text))
    defaults = {n: v["default"] for n, v in TfConfig({"v.tf": parse(text)}).variables().items()}
    assert {k: v for k, v in parsed.items() if not k.startswith("__")} == defaults


# --- spec checks and facts ------------------------------------------------------------------------------------------


def test_expected_values_follow_mapping_rules():
    exp = {k: v for k, (v, _) in expected_values(SPEC, MODERNIZATION).items()}
    assert exp["node_min"] == 2 and exp["node_max"] == 4 and exp["az_count"] == 2 and exp["nat_gateways"] == 1
    assert exp["node_instance_types"] == ["t4g.medium"] and exp["db_instance_class"] == "db.t4g.small"
    assert exp["ecr_repository"] == "inventory-api" and exp["db_deletion_protection"] is True
    assert expected_values({}, {}) == {}


def test_spec_checks_flag_blueprint_defaults_and_pass_after_mapping():
    files = blueprint_files()
    before = spec_checks(facts(TfConfig({n: parse(t) for n, t in files.items()})), SPEC, MODERNIZATION)
    assert sum(c["status"] == "mismatch" for c in before) >= 12
    files["variables.tf"] = set_defaults(files["variables.tf"], SPEC_VALUES)[0]
    f = facts(TfConfig({n: parse(t) for n, t in files.items()}))
    assert all(c["status"] == "pass" for c in spec_checks(f, SPEC, MODERNIZATION))
    assert f["cluster_name"] == "inventory-service-eks" and f["network"] == {"vpc_cidr": "10.42.0.0/16", "azs": 2,
                                                                             "nat_gateways": 1}
    assert f["database"]["storage_encrypted"] is True and f["database"]["publicly_accessible"] is False


# --- tools ----------------------------------------------------------------------------------------------------------


def make_tools(spec=SPEC, files=None, feedback=""):
    ws, box = Workspace(files), ResultBox()
    run = TerraformRun(ws, spec=spec, modernization=MODERNIZATION, feedback=feedback)
    return ws, box, run, {t.tool_name: t for t in terraform_tools(run, box)}


def test_get_blueprint_copies_files_and_keeps_existing_ones():
    ws, _, _, tools = make_tools()
    out = tools["get_blueprint"]()
    assert "--- terraform/variables.tf ---" in out
    assert set(ws.listing("terraform/")) == {f"terraform/{n}" for n in
                                             ["versions.tf", "variables.tf", "main.tf", "outputs.tf",
                                              "terraform.tfvars.example"]}
    ws2, _, _, tools2 = make_tools(files={"terraform/main.tf": "# previous run\n"})
    assert "kept" in tools2["get_blueprint"]() and ws2.read("terraform/main.tf") == "# previous run\n"


def test_describe_schema():
    out = describe_schema("terraform-aws-modules/eks/aws", "node_groups")
    assert "eks_managed_node_groups" in out and "desired_size" in out and "21." in out
    assert "multi_az" in describe_schema("aws_db_instance", "multi")
    assert "did you mean" in describe_schema("terraform-aws-modules/eks/aw")


def test_validate_and_submit_flow():
    ws, box, run, tools = make_tools()
    ctx = CTX(invocation_state={})
    tools["get_blueprint"]()
    first = tools["terraform_validate"](tool_context=ctx)
    assert "VALID (level: offline" in first and "Spec mismatches" in first and "region" in first
    rejected = tools["submit_result"](summary="s", spec_coverage=[], tool_context=ctx)
    assert rejected.startswith("error: not submitted") and box.data is None
    tools["set_variables"](values=SPEC_VALUES)
    assert "Next: call submit_result" in tools["terraform_validate"](tool_context=ctx)
    cov = [{"requirement": "SEC3", "how": "private subnets"}]
    assert tools["submit_result"](summary="done", spec_coverage=cov, tool_context=ctx) == "result recorded"
    assert ctx.invocation_state["request_state"]["stop_event_loop"] is True
    data = run.finalize(box.data)
    assert data["valid"] and data["validate_attempts"] == 2 and data["spec_coverage"] == cov
    assert data["registry"]["repositories"] == ["inventory-api"] and data["compute"]["node_min"] == 2
    assert {"modules", "compute", "database", "network", "registry", "outputs", "spec_coverage"} <= set(data)
    assert "terraform/terraform.tfvars.example" in ws.artifacts()


def test_revision_flags_only_values_moved_away_from_the_spec():
    files = {f"terraform/{n}": text for n, text in blueprint_files().items()}
    files["terraform/variables.tf"] = set_defaults(files["terraform/variables.tf"], SPEC_VALUES)[0]
    *_, tools = make_tools(files=files, feedback="use 3 AZs")
    ctx = CTX(invocation_state={})
    assert "kept" in tools["get_blueprint"]()
    tools["set_variables"](values={"az_count": 3, "node_max": 6})
    out = tools["terraform_validate"](tool_context=ctx)
    assert "Changed away from the spec rules" in out and "az_count" in out and "node_max" in out
    first = tools["submit_result"](summary="s", spec_coverage=[], tool_context=ctx)
    assert first.startswith("error: not submitted") and "feedback asked" in first
    assert tools["submit_result"](summary="s", spec_coverage=[], tool_context=ctx) == "result recorded"


def test_validation_budget_stops_the_loop():
    _, _, run, tools = make_tools()
    ctx = CTX(invocation_state={})
    tools["get_blueprint"]()
    for _ in range(run.max_validations + 2):
        tools["terraform_validate"](tool_context=ctx)
    assert "budget exhausted" in tools["terraform_validate"](tool_context=ctx)
    assert ctx.invocation_state["request_state"]["stop_event_loop"] is True


def test_normalize_coverage_accepts_model_variants():
    assert normalize_coverage('[{"id": "SEC1", "description": "x"}]') == [{"requirement": "SEC1", "how": "x"}]
    assert normalize_coverage("not json") == [] and normalize_coverage(None) == []


# --- handle() with a scripted model ---------------------------------------------------------------------------------


def scripted_run_agent(script):
    """Replaces core.run_agent: runs `script(tools)` instead of a model, then emits run_agent's events."""

    async def fake(*, stage, system_prompt, prompt, tools, workspace, box, **_):
        yield {"type": "status", "stage": stage, "message": "agent started", "model": "scripted"}
        script({t.tool_name: t for t in tools}, prompt)
        for path, content in workspace.artifacts().items():
            yield {"type": "artifact", "path": path, "bytes": len(content)}
        yield {"type": "result", "stage": stage, "summary": box.summary or "(no summary)", "data": box.data or {},
               "artifacts": workspace.artifacts(), "metrics": {"model": "scripted", "submitted": box.data is not None}}

    return fake


async def collect(request):
    return [e async for e in tf_agent.handle(request)]


async def test_handle_end_to_end_with_scripted_model(monkeypatch):
    def script(tools, prompt):
        assert "inventory-api" in prompt and "SEC3" in prompt
        ctx = CTX(invocation_state={})
        tools["get_blueprint"]()
        tools["set_variables"](values=SPEC_VALUES)
        tools["terraform_validate"](tool_context=ctx)
        tools["submit_result"](summary="ok", spec_coverage=[{"requirement": "TC2", "how": "t4g"}], tool_context=ctx)

    monkeypatch.setattr(tf_agent, "run_agent", scripted_run_agent(script))
    events = await collect(AgentRequest(inputs={"spec": SPEC, "modernization": MODERNIZATION}))
    terminal = [e for e in events if e["type"] in ("result", "error")]
    assert len(terminal) == 1 and terminal[0]["type"] == "result"
    result = terminal[0]
    assert result["data"]["valid"] and result["metrics"]["validate_attempts"] == 1
    assert {e["path"] for e in events if e["type"] == "artifact"} == set(result["artifacts"])
    assert 'default     = "inventory-api"' in result["artifacts"]["terraform/variables.tf"]


async def test_handle_minimal_request_without_submit(monkeypatch):
    monkeypatch.setattr(tf_agent, "run_agent", scripted_run_agent(lambda tools, prompt: tools["get_blueprint"]()))
    events = await collect(AgentRequest())
    result = events[-1]
    assert result["type"] == "result" and result["data"]["valid"] and result["summary"].startswith("Terraform is valid")
    assert result["data"]["spec_checks"] == []


async def test_handle_reports_error_when_no_files(monkeypatch):
    monkeypatch.setattr(tf_agent, "run_agent", scripted_run_agent(lambda tools, prompt: None))
    events = await collect(AgentRequest())
    assert events[-1]["type"] == "error" and sum(e["type"] in ("result", "error") for e in events) == 1


def test_build_prompt_minimal_and_revision():
    assert "No migration spec" in tf_agent.build_prompt(AgentRequest())
    prompt = tf_agent.build_prompt(AgentRequest(inputs={"spec": SPEC}, feedback="use m7g.large"))
    assert "Reviewer feedback" in prompt and "prefer_graviton" in prompt and "- SEC3:" in prompt


async def test_handle_continues_once_when_the_model_stops_early(monkeypatch):
    prompts = []

    def script(tools, prompt):
        prompts.append(prompt)
        tools["get_blueprint"]()
        if len(prompts) == 2:  # the follow-up run finishes the job
            assert "stopped early" in prompt and "region" in prompt
            ctx = CTX(invocation_state={})
            tools["set_variables"](values=SPEC_VALUES)
            tools["terraform_validate"](tool_context=ctx)
            tools["submit_result"](summary="ok", spec_coverage=[{"requirement": "G1", "how": "EKS"}], tool_context=ctx)

    monkeypatch.setattr(tf_agent, "run_agent", scripted_run_agent(script))
    events = await collect(AgentRequest(inputs={"spec": SPEC, "modernization": MODERNIZATION}))
    assert len(prompts) == 2 and any(e["type"] == "status" and "continuing" in e["message"] for e in events)
    result = events[-1]
    assert result["type"] == "result" and result["metrics"]["runs"] == 2 and result["summary"] == "ok"
    assert all(c["status"] == "pass" for c in result["data"]["spec_checks"])


async def test_handle_recovers_from_a_failed_model_call(monkeypatch):
    calls = []

    async def flaky(*, stage, workspace, box, tools, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield {"type": "error", "stage": stage, "message": "ThrottlingException"}
            return
        async for event in scripted_run_agent(lambda t, p: t["get_blueprint"]())(
                stage=stage, workspace=workspace, box=box, tools=tools, **kwargs):
            yield event

    monkeypatch.setattr(tf_agent, "run_agent", flaky)
    events = await collect(AgentRequest())
    assert [e["type"] for e in events if e["type"] in ("result", "error")] == ["result"]


def test_terraform_init_failure_degrades_to_offline(monkeypatch, tmp_path):
    from transform_agents.terraform import validate as v

    monkeypatch.delenv("TERRAFORM_OFFLINE")
    monkeypatch.setattr(v, "terraform_bin", lambda: "terraform")
    monkeypatch.setattr(v, "cache_dir", lambda: tmp_path)

    def fake_tf(args, cwd, timeout):
        code = 1 if args[0] == "init" else 0
        return SimpleNamespace(returncode=code, stdout="", stderr="Error: Failed to query available provider packages")

    monkeypatch.setattr(v, "run_tf", fake_tf)
    report = v.validate(blueprint_files())
    assert report.valid and report.level == "offline" and "terraform init failed" in report.notes[0]


def test_leaked_call_is_parsed_from_text():
    names = ["get_blueprint", "set_variables", "submit_result"]
    text = 'יׇsubmit_result{"summary": "done", "spec_coverage": [{"requirement": "G1", "how": "EKS"}]}'
    assert tf_agent.leaked_call(text, names) == ("submit_result", {"summary": "done", "spec_coverage": [
        {"requirement": "G1", "how": "EKS"}]})
    assert tf_agent.leaked_call("NIEFFget_blueprint{}", names) == ("get_blueprint", {})
    assert tf_agent.leaked_call("I will call get_blueprint first.", names) is None


async def test_handle_replays_tool_calls_written_as_text(monkeypatch):
    runs = []

    async def fake(*, stage, workspace, box, tools, prompt, **_):
        runs.append(prompt)
        t = {x.tool_name: x for x in tools}
        if len(runs) == 1:  # first run: the very first call leaks as text
            yield {"type": "text", "delta": "NIEFFget_blueprint{}"}
        else:  # second run: work, then the final submit leaks as text
            assert "executed for you: get_blueprint" in prompt
            yield {"type": "tool_start", "id": "1", "name": "set_variables", "input": "{}"}
            t["set_variables"](values=SPEC_VALUES)
            yield {"type": "text", "delta": 'xxsubmit_result{"summary": "ok", "spec_coverage": '}
            yield {"type": "text", "delta": '[{"requirement": "SEC3", "how": "private"}]}'}
        yield {"type": "result", "stage": stage, "summary": "garbage", "data": {}, "artifacts": workspace.artifacts(),
               "metrics": {"submitted": False}}

    monkeypatch.setattr(tf_agent, "run_agent", fake)
    events = await collect(AgentRequest(inputs={"spec": SPEC, "modernization": MODERNIZATION}))
    replayed = [e["name"] for e in events if e["type"] == "tool_end" and e["id"].startswith("text-call")]
    assert replayed == ["get_blueprint", "submit_result"]
    result = events[-1]
    assert result["summary"] == "ok" and result["metrics"]["runs"] == 2 and result["metrics"]["submitted"]
    assert result["data"]["spec_coverage"] == [{"requirement": "SEC3", "how": "private"}]


async def test_handle_stops_a_rambling_model_and_continues(monkeypatch):
    runs = []

    async def fake(*, stage, workspace, box, tools, **_):
        runs.append(1)
        if len(runs) == 1:
            while True:  # degenerate generation: endless prose, no tool calls
                yield {"type": "text", "delta": "blah " * 50}
        async for event in scripted_run_agent(lambda t, p: t["get_blueprint"]())(
                stage=stage, workspace=workspace, box=box, tools=tools, prompt="", system_prompt=""):
            yield event

    monkeypatch.setattr(tf_agent, "run_agent", fake)
    events = await collect(AgentRequest())
    assert any(e["type"] == "status" and "prose instead of calling tools" in e["message"] for e in events)
    assert events[-1]["type"] == "result" and len(runs) >= 2


async def test_handle_deadline_still_reports_valid_terraform(monkeypatch):
    import asyncio

    async def hangs(*, stage, workspace, box, tools, **_):
        t = {x.tool_name: x for x in tools}
        t["get_blueprint"]()
        t["set_variables"](values=SPEC_VALUES)
        yield {"type": "status", "stage": stage, "message": "agent started"}
        await asyncio.sleep(30)  # the model never answers

    monkeypatch.setattr(tf_agent, "run_agent", hangs)
    monkeypatch.setattr(tf_agent, "DEADLINE_S", 0.5)
    events = await collect(AgentRequest(inputs={"spec": SPEC, "modernization": MODERNIZATION}))
    result = events[-1]
    assert result["type"] == "result" and result["data"]["valid"] and "deadline" in result["summary"]
    assert sum(e["type"] in ("result", "error") for e in events) == 1


async def test_handle_stops_a_silent_run(monkeypatch):
    import asyncio

    async def silent(*, stage, **_):
        yield {"type": "status", "stage": stage, "message": "agent started"}
        await asyncio.sleep(30)

    monkeypatch.setattr(tf_agent, "run_agent", silent)
    monkeypatch.setattr(tf_agent, "SILENCE_S", 0.2)
    events = await collect(AgentRequest())
    assert events[-1]["type"] == "error" and "no progress" in events[-1]["message"]
    assert sum(e["type"] == "status" and "continuing" in e["message"] for e in events) == tf_agent.MAX_RUNS - 1


# --- module / provider source allow-list ----------------------------------------------------------------------------


def online(monkeypatch, tmp_path) -> list[list[str]]:
    """Pretend the terraform binary exists and record every command (none may run for blocked sources)."""
    from transform_agents.terraform import validate as v

    monkeypatch.delenv("TERRAFORM_OFFLINE")
    monkeypatch.setattr(v, "terraform_bin", lambda: "terraform")
    monkeypatch.setattr(v, "cache_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    def fake_tf(args, cwd, timeout):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"valid": true, "diagnostics": []}', stderr="")

    monkeypatch.setattr(v, "run_tf", fake_tf)
    return calls


@pytest.mark.parametrize("source", ["git::https://github.com/evil/mod.git", "evil-corp/eks/aws",
                                    "./modules/vpc", "../../../../tmp/x", "https://example.com/mod.zip",
                                    "terraform-aws-modules/vpc/aws/../../evil"])
def test_blocked_module_source_skips_terraform_init(monkeypatch, tmp_path, source):
    calls = online(monkeypatch, tmp_path)
    files = broken({'source  = "terraform-aws-modules/vpc/aws"': f'source  = "{source}"'})
    report = validate(files)
    assert not report.valid and report.level == "offline"
    assert not any(c[0] in ("init", "validate") for c in calls), calls
    blocked = [e for e in report.errors if e.check == "source"]
    assert len(blocked) == 1 and f'module "vpc": blocked module source "{source}"' in str(blocked[0])
    assert blocked[0].file == "main.tf" and blocked[0].line
    assert any("blocked module source" in n for n in report.notes)
    assert "blocked module source" in report.text()


def test_allowed_sources_reach_the_online_layer(monkeypatch, tmp_path):
    calls = online(monkeypatch, tmp_path)
    files = broken({'source  = "terraform-aws-modules/vpc/aws"':
                    'source  = "registry.terraform.io/terraform-aws-modules/vpc/aws"'})
    report = validate(files)
    assert report.level == "terraform" and not [e for e in report.errors if e.check == "source"]
    assert [c[0] for c in calls if c[0] in ("init", "validate")] == ["init", "validate"]


def test_blocked_provider_source(monkeypatch, tmp_path):
    calls = online(monkeypatch, tmp_path)
    files = broken({'source  = "hashicorp/aws"': 'source  = "evilcorp/aws"'}, file="versions.tf")
    report = validate(files)
    assert report.level == "offline" and not any(c[0] == "init" for c in calls)
    assert any('blocked provider source "evilcorp/aws"' in str(e) for e in report.errors)


def test_blocked_source_in_a_file_the_parser_cannot_read(monkeypatch, tmp_path):
    calls = online(monkeypatch, tmp_path)
    files = blueprint_files()
    files["extra.tf"] = 'module "x" {\n  source = "git::https://evil.example/x.git"\n  broken = \n}\n'
    report = validate(files)
    assert report.level == "offline" and not any(c[0] in ("init", "validate") for c in calls)
    assert any(e.check == "source" and e.file == "extra.tf" and e.line == 2 for e in report.errors)


def test_blocked_source_is_an_open_problem_for_the_agent():
    ws = Workspace({f"terraform/{n}": t for n, t in broken(
        {'source  = "terraform-aws-modules/vpc/aws"': 'source  = "git::https://evil.example/vpc.git"'}).items()})
    run = TerraformRun(ws, spec=SPEC, modernization=MODERNIZATION)
    assert any("blocked module source" in p for p in run.open_problems())
    data = run.finalize(None)
    assert data["valid"] is False and data["validation_level"] == "offline"
    assert "blocked module source" in data["validate_output"]
