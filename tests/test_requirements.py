"""Requirements agent: document parsing and spec validation (offline), plus one live Bedrock run.

Uses tests/fixtures/discovery_result.json as the upstream assessment.
Live: AWS_PROFILE=personal-dev uv run pytest -q -m live tests/test_requirements.py
      (SAVE_FIXTURES=1 also writes tests/fixtures/requirements_result.json)
"""

import asyncio
import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from transform_agents.core import AgentRequest, ResultBox, Workspace
from transform_agents.requirements import agent as requirements_agent
from transform_agents.requirements import tools as requirements_tools
from transform_agents.requirements.tools import (
    APPENDIX_MARKER,
    REPORT,
    RequirementsContext,
    check_spec,
    complete_requirements,
    load_sample_requirements,
    make_tools,
    mentioned_growth,
    normalize_spec,
    parse_requirements,
    render_appendix,
    spec_template,
)

FIXTURES = Path(__file__).parent / "fixtures"
ASSESSMENT = json.loads((FIXTURES / "discovery_result.json").read_text())["data"]
DOCS = load_sample_requirements()
SAMPLE = DOCS["requirements/migration-requirements.md"]
TRACES = {"G2": ["LF-08", "LF-09"], "G3": ["LF-17"], "FR3": ["LF-10", "LF-15"], "NFR3": ["LF-08"],
          "NFR4": ["LF-12"], "NFR5": ["LF-14"], "NFR6": ["LF-16"], "SEC1": ["LF-02", "LF-13"], "SEC2": ["LF-06", "LF-07"],
          "SEC4": ["LF-04"], "SEC5": ["LF-01"], "TC3": ["LF-09"]}


def valid_spec() -> dict:
    """The spec a careful analyst would write for the sample document (the live test checks the same values)."""
    spec = spec_template(ASSESSMENT)
    spec["service"]["description"] = "Inventory and orders API for warehouse scanners"
    spec["target"] |= {"platform": "eks", "region": "us-west-2", "cpu_architecture": "arm64", "iac": "terraform",
                       "registry": "ecr"}
    spec["workload"] |= {"replicas_min": 2, "replicas_max": 6, "autoscaling_metric": "cpu", "peak_rps": 40,
                         "p95_latency_ms": 300}
    spec["data"] |= {"engine": "postgres", "managed_service": "rds", "multi_az": True}
    spec["nfr"] |= {"availability_slo_pct": 99.9, "rpo_minutes": 15, "rto_minutes": 60, "logging": "json-stdout"}
    spec["security"] |= {"secrets": "aws-secrets-manager", "encryption_at_rest": True, "private_database": True,
                         "non_root": True, "read_only_root_fs": True}
    spec["finops"] |= {"monthly_budget_usd": 450, "prefer_graviton": True, "growth_factor_next_year": 3}
    spec["requirements"] = [{"id": r["id"], "priority": "could" if r["id"] == "FR2" else "must",
                             "acceptance": f"Verified by a test for {r['id']}", "traces_to": TRACES.get(r["id"], [])}
                            for r in parse_requirements(SAMPLE)["requirements"]]
    spec["gaps"] = [{"finding_id": f, "handling": "fixed in the container image"} for f in ("LF-03", "LF-05", "LF-11")]
    spec["conflicts"] = [{"description": "G3 cost cap vs. G2 Multi-AZ", "resolution": "small Graviton nodes"}]
    spec["open_questions"] = [{"id": f"Q-0{i}", "question": p, "assumption": "a", "impact": "b"}
                              for i, p in enumerate(parse_requirements(SAMPLE)["open_points"], 1)]
    return spec


def problems(spec: dict) -> tuple[list[str], list[str]]:
    doc = parse_requirements(SAMPLE)
    spec = normalize_spec(spec)
    complete_requirements(spec, doc["requirements"])
    return check_spec(spec, doc["requirements"], doc["open_points"], ASSESSMENT)


# ---- parsing and template ----

def test_parse_sample_requirements():
    doc = parse_requirements(SAMPLE)
    ids = [r["id"] for r in doc["requirements"]]
    assert len(ids) == 24 and ids[:2] == ["G1", "G2"] and ids[-1] == "TC5"
    cats = {r["id"]: r["category"] for r in doc["requirements"]}
    assert (cats["G3"], cats["FR1"], cats["NFR2"], cats["SEC5"], cats["TC2"]) == (
        "goal", "functional", "nfr", "security", "constraint")
    assert "USD 450" in doc["requirements"][2]["text"] and "**" not in doc["requirements"][2]["text"]
    assert len(doc["open_points"]) == 3 and "3x" in doc["open_points"][0]


def test_parse_numbered_and_bold_ids():
    doc = parse_requirements("## Needs\n1. **FR1.** Keep the API\n* NFR2: p95 < 200 ms\n## Open questions\n- Region?\n")
    assert [(r["id"], r["text"]) for r in doc["requirements"]] == [("FR1", "Keep the API"), ("NFR2", "p95 < 200 ms")]
    assert doc["open_points"] == ["Region?"]


def test_spec_template_has_defaults_and_nulls():
    t = spec_template(ASSESSMENT)
    assert t["service"]["name"] == "inventory-service" and t["data"]["migrate_from"] == "sqlite"
    assert t["workload"]["container_port"] == 8080 and t["data"]["version"] == "16"
    assert t["workload"]["peak_rps"] is None and t["finops"]["monthly_budget_usd"] is None
    assert t["requirements"] == [] and "gaps" in t and t["finops"]["growth_factor_next_year"] == 1
    assert RequirementsContext(Workspace(DOCS), ASSESSMENT).draft["finops"]["growth_factor_next_year"] == 3


def test_normalize_forgives_type_slips_and_nesting():
    spec = normalize_spec({"service": {"name": "x", "target": {"cpu_architecture": "Graviton", "kubernetes_version": 1.33}},
                           "data": {"engine": "PostgreSQL", "version": 16, "multi_az": "yes"},
                           "finops": {"monthly_budget_usd": "USD 450"}, "nfr": {"availability_slo_pct": "99.9%"},
                           "requirements": [{"id": "fr1.", "priority": "MUST", "traces_to": "LF-01, lf-02"}]})
    assert spec["target"] == {"kubernetes_version": "1.33", "cpu_architecture": "arm64"}
    assert spec["data"] == {"engine": "postgres", "version": "16", "multi_az": True}
    assert spec["finops"]["monthly_budget_usd"] == 450 and spec["nfr"]["availability_slo_pct"] == 99.9
    assert spec["requirements"][0] == {"id": "FR1", "priority": "must", "traces_to": ["LF-01", "LF-02"]}


# ---- validation ----

def test_valid_spec_passes():
    errors, warnings = problems(valid_spec())
    assert errors == [] and warnings == []


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda s: s["workload"].update(replicas_min=1), "replicas_min must be >= 2"),
    (lambda s: s["workload"].update(replicas_max=1), "replicas_max must be >= workload.replicas_min"),
    (lambda s: s["finops"].update(monthly_budget_usd=0), "monthly_budget_usd must be greater than 0"),
    (lambda s: s["target"].update(region=None), "target.region is missing"),
    (lambda s: s["target"].update(cpu_architecture="sparc"), "cpu_architecture must be one of"),
    (lambda s: s["requirements"].append(dict(s["requirements"][0])), "G1 appears more than once"),
    (lambda s: s["requirements"].pop(), "requirements is missing TC5"),
    (lambda s: s["requirements"][0].update(traces_to=["LF-99"]), "unknown finding ids ['LF-99']"),
    (lambda s: s["requirements"][0].update(priority="urgent"), "G1: priority must be one of"),
    (lambda s: s["requirements"][-6].update(traces_to=[]), "LF-01 (critical"),
    (lambda s: s["gaps"].pop(0), "LF-03 ("),
    (lambda s: s["open_questions"].pop(), "3 open points but open_questions has 2"),
    (lambda s: s["open_questions"][0].update(assumption=""), "open_questions[0] needs a non-empty assumption"),
    (lambda s: s["conflicts"].append({"description": "x"}), "conflicts[1] needs a description and a resolution"),
    (lambda s: s["workload"].update(replicas_max=4), "leaves no headroom for the x3 growth"),
    (lambda s: s["finops"].update(growth_factor_next_year=1), "set finops.growth_factor_next_year to 3"),
    (lambda s: s["service"].update(name="legacy-inventory"), "must name the new microservice"),
])
def test_check_spec_catches_inconsistencies(mutate, expected):
    spec = valid_spec()
    mutate(spec)
    errors, _ = problems(spec)
    assert any(expected in e for e in errors), errors


def test_check_spec_warnings_do_not_block():
    spec = valid_spec()
    spec["data"]["multi_az"] = False
    spec["conflicts"] = []
    errors, warnings = problems(spec)
    assert errors == [] and len(warnings) == 2 and "multi_az" in warnings[0]


def test_mentioned_growth():
    assert mentioned_growth(['The team said "maybe 3x more traffic next year".']) == 3.0
    assert mentioned_growth(["Use 2 replicas", "no growth expected"]) is None


def fill_with_tools(tools: dict, spec: dict) -> None:
    """Fill a draft the way the model does: flat scalar fields, then list entries."""
    sections = ("service", "target", "workload", "data", "nfr", "security", "finops")
    tools["set_fields"](fields={f"{sec}.{k}": v for sec in sections for k, v in spec[sec].items()})
    tools["set_lists"](requirements=spec["requirements"], conflicts=spec["conflicts"],
                       open_questions=spec["open_questions"], gaps=spec["gaps"])


def test_set_fields_and_set_lists_build_the_draft():
    ctx = RequirementsContext(Workspace(DOCS), ASSESSMENT)
    tools = {t.tool_name: t for t in make_tools(ctx, ResultBox())}
    out = tools["set_fields"](fields={"target.region": "us-west-2", "workload": {"peak_rps": 40}, "bogus.x": 1})
    assert out.startswith("set 2 fields") and "bogus.x" in out and "target.platform" in out
    assert ctx.draft["target"]["region"] == "us-west-2" and ctx.draft["workload"]["peak_rps"] == 40
    tools["set_lists"](requirements=[{"id": "G1", "priority": "must"}, {"id": "G2", "priority": "must"}])
    tools["set_lists"](requirements=[{"id": "G1", "acceptance": "Runs on EKS"}, {"id": "G2", "remove": True}])
    assert ctx.draft["requirements"] == [{"id": "G1", "priority": "must", "acceptance": "Runs on EKS"}]


def test_check_then_submit_validates_the_draft_and_stops_the_loop():
    ws = Workspace(DOCS)
    ctx, box = RequirementsContext(ws, ASSESSMENT), ResultBox()
    tools = {t.tool_name: t for t in make_tools(ctx, box)}
    read = json.loads(tools["read_requirements"]())
    assert len(read["requirement_ids"]) == 24 and len(read["open_points"]) == 3
    assert json.loads(tools["get_spec_template"]())["draft"]["workload"]["container_port"] == 8080
    spec = valid_spec()
    spec["workload"]["replicas_min"] = 1
    fill_with_tools(tools, spec)
    assert "replicas_min must be >= 2" in tools["check_spec"]()
    tools["set_fields"](fields={"workload.replicas_min": 2})
    assert tools["check_spec"]().startswith("OK: 24 requirements")
    tool_context = SimpleNamespace(invocation_state={})
    assert "write reports/migration-spec.md" in tools["submit_result"](summary="s", tool_context=tool_context)
    ws.write(REPORT, "# Migration spec: inventory-service\n")
    assert tools["submit_result"](summary="s", tool_context=tool_context).startswith("result recorded")
    assert box.data["requirements"][0]["text"].startswith("Move the application")
    assert list(box.data)[:3] == ["service", "target", "workload"]
    assert tool_context.invocation_state["request_state"]["stop_event_loop"] is True


def test_replicas_max_is_derived_from_growth_when_left_null():
    ctx = RequirementsContext(Workspace(DOCS), ASSESSMENT)
    ctx.draft = valid_spec()
    ctx.draft["workload"]["replicas_max"] = None
    ctx.validate()
    assert ctx.draft["workload"]["replicas_max"] == 6


def test_revision_starts_from_the_previous_spec():
    ctx = RequirementsContext(Workspace(DOCS), ASSESSMENT, previous=valid_spec())
    assert ctx.draft["finops"]["monthly_budget_usd"] == 450 and len(ctx.draft["requirements"]) == 24


def test_report_appendix_has_traceability_and_coverage():
    spec = normalize_spec(valid_spec())
    complete_requirements(spec, parse_requirements(SAMPLE)["requirements"])
    md = render_appendix(spec, ASSESSMENT)
    assert md.startswith(APPENDIX_MARKER) and "## Traceability matrix" in md
    assert "| SEC5 | security | must |" in md and "| LF-01 | critical |" in md
    assert "gap: fixed in the container image" in md and "## Open questions" in md and "NOT COVERED" not in md


def test_report_appendix_tolerates_a_malformed_last_chance_draft():
    spec = normalize_spec(valid_spec())
    complete_requirements(spec, parse_requirements(SAMPLE)["requirements"])
    spec["conflicts"] = [{"description": "cost vs HA"}, {"resolution": "only a resolution"}, "a bare string", None, 3]
    spec["open_questions"] = [{"id": "Q-01"}, {"question": "no id", "assumption": None}, "free text"]
    spec["gaps"] = [{"finding_id": "LF-03"}]
    spec["requirements"].append({"id": "X1"})
    del spec["security"], spec["workload"]["replicas_max"]
    md = render_appendix(spec, {"findings": [{"id": "LF-03", "severity": "high"}, "junk"]})
    assert "| cost vs HA | - |" in md and "| a bare string | - |" in md and "| only a resolution |" in md
    assert "| Q-01 |  | Open | - | - |" in md and "| - | no id | Open | - | - |" in md and "| - | free text |" in md
    assert "secrets ?" in md and "replicas 2-? " in md and "| X1 | - | - |" in md and "| LF-03 | high |" in md


def test_draft_updates_from_parallel_tool_threads_are_not_lost():
    """Defence in depth: set_fields (derive() swaps the draft for a normalized copy) racing set_lists (upsert)."""
    ctx = RequirementsContext(Workspace(DOCS), ASSESSMENT)
    tools = {t.tool_name: t for t in make_tools(ctx, ResultBox())}
    ids = [r["id"] for r in ctx.doc_reqs]

    def work(i: int) -> None:
        tools["set_lists"](requirements=[{"id": ids[i], "priority": "must"}],
                           open_questions=[{"id": f"Q-{i:02d}", "question": "q", "assumption": "a", "impact": "i"}])
        tools["set_fields"](fields={"workload.peak_rps": i + 1, "target.region": "us-west-2"})
        tools["check_spec"]()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(len(ids))))
    assert sorted(r["id"] for r in ctx.draft["requirements"]) == sorted(ids)
    assert len(ctx.draft["open_questions"]) == len(ids) and ctx.draft["target"]["region"] == "us-west-2"


# ---- handle() ----

async def collect(gen) -> list[dict]:
    return [event async for event in gen]


async def test_handle_uses_valid_draft_when_model_skips_submit(monkeypatch):
    async def fake_run_agent(**kw):
        fill_with_tools({t.tool_name: t for t in kw["tools"]}, valid_spec())
        yield {"type": "result", "stage": "requirements", "summary": "done", "data": {}, "artifacts": {}, "metrics": {}}

    monkeypatch.setattr(requirements_agent, "run_agent", fake_run_agent)
    events = await collect(requirements_agent.handle(AgentRequest(inputs={"assessment": ASSESSMENT})))
    assert [e["type"] for e in events] == ["artifact", "result"]
    assert events[-1]["data"]["finops"]["monthly_budget_usd"] == 450 and APPENDIX_MARKER in events[-1]["artifacts"][REPORT]


async def test_handle_errors_without_a_valid_spec(monkeypatch):
    async def fake_run_agent(**kw):
        yield {"type": "result", "stage": "requirements", "summary": "gave up", "data": {}, "artifacts": {}, "metrics": {}}

    monkeypatch.setattr(requirements_agent, "run_agent", fake_run_agent)
    events = await collect(requirements_agent.handle(AgentRequest(files=DOCS)))
    assert [e["type"] for e in events] == ["status", "error"]  # one resume, then a clear error


async def test_handle_renders_a_last_chance_submit_with_malformed_lists(monkeypatch):
    async def sloppy_run_agent(**kw):  # submits three times with half-filled conflicts/open questions
        tools = {t.tool_name: t for t in kw["tools"]}
        spec = valid_spec()
        spec["conflicts"] = [{"resolution": "no description"}]
        spec["open_questions"] = [{"id": "Q-01", "question": "growth?"}]
        fill_with_tools(tools, spec)
        for _ in range(3):
            tools["submit_result"](summary="best effort", tool_context=SimpleNamespace(invocation_state={}))
        yield {"type": "result", "stage": "requirements", "summary": "best effort", "data": {}, "artifacts": {},
               "metrics": {}}

    monkeypatch.setattr(requirements_agent, "run_agent", sloppy_run_agent)
    events = await collect(requirements_agent.handle(AgentRequest(files=dict(DOCS), inputs={"assessment": ASSESSMENT})))
    assert [e["type"] for e in events] == ["artifact", "result"]
    assert events[-1]["data"]["validation_problems"] and "| Q-01 | growth? | Open | - | - |" in events[-1]["artifacts"][REPORT]


async def test_handle_falls_back_to_the_narrative_when_the_tables_fail(monkeypatch):
    async def fake_run_agent(**kw):
        tools = {t.tool_name: t for t in kw["tools"]}
        fill_with_tools(tools, valid_spec())
        tools["write_file"](path=REPORT, content="# Migration spec: inventory-service\n\n## Summary\n- narrative\n")
        yield {"type": "result", "stage": "requirements", "summary": "done", "data": {}, "artifacts": {}, "metrics": {}}

    def broken_appendix(*args, **kwargs):
        raise KeyError("description")

    monkeypatch.setattr(requirements_agent, "run_agent", fake_run_agent)
    monkeypatch.setattr(requirements_tools, "render_appendix", broken_appendix)
    events = await collect(requirements_agent.handle(AgentRequest(files=dict(DOCS), inputs={"assessment": ASSESSMENT})))
    assert [e["type"] for e in events] == ["status", "artifact", "result"] and "narrative" in events[0]["message"]
    report = events[-1]["artifacts"][REPORT]
    assert report.startswith("# Migration spec: inventory-service") and "- narrative" in report
    assert APPENDIX_MARKER not in report and events[-1]["data"]["finops"]["monthly_budget_usd"] == 450


def test_revision_prompt_includes_previous_spec():
    ctx = RequirementsContext(Workspace(DOCS), ASSESSMENT)
    prompt = requirements_agent.build_prompt(AgentRequest(feedback="budget is 500", inputs={"previous": {"x": 1}}), ctx)
    assert "budget is 500" in prompt and "previous spec" in prompt and "LF-01 critical" in prompt
    assert "POST /api/orders" in prompt


# ---- live ----

@pytest.mark.live
async def test_requirements_live_end_to_end():
    request = AgentRequest(run_id="test-requirements", files=dict(DOCS), inputs={"assessment": copy.deepcopy(ASSESSMENT)})
    events = await collect(requirements_agent.handle(request))
    assert events[-1]["type"] == "result", events[-1]
    assert sum(e["type"] in ("result", "error") for e in events) == 1
    result = events[-1]
    assert result["metrics"]["submitted"] is True, result["summary"]
    spec = result["data"]
    errors, _ = problems(spec)
    assert errors == [], errors
    assert spec["finops"]["monthly_budget_usd"] == 450 and spec["workload"]["peak_rps"] == 40
    assert spec["workload"]["p95_latency_ms"] == 300 and spec["nfr"]["availability_slo_pct"] == 99.9
    assert spec["workload"]["replicas_min"] == 2 and spec["workload"]["replicas_max"] >= 6
    assert spec["service"]["name"] == "inventory-service" and spec["finops"]["growth_factor_next_year"] == 3
    assert spec["target"]["region"] == "us-west-2" and spec["target"]["cpu_architecture"] == "arm64"
    assert (spec["data"]["engine"], spec["data"]["version"], spec["data"]["managed_service"]) == ("postgres", "16", "rds")
    assert spec["data"]["multi_az"] is True and spec["security"]["secrets"] == "aws-secrets-manager"
    assert {r["id"] for r in spec["requirements"]} >= {r["id"] for r in parse_requirements(SAMPLE)["requirements"]}
    assert len(spec["open_questions"]) >= 3
    report = result["artifacts"][REPORT]
    assert report.startswith("# Migration spec") and "## Traceability matrix" in report
    assert result["metrics"]["latency_s"] < 90
    if os.getenv("SAVE_FIXTURES"):
        (FIXTURES / "requirements_result.json").write_text(json.dumps(result, indent=2) + "\n")


@pytest.mark.parametrize("fill", [True, False])
async def test_handle_times_out_with_or_without_a_valid_draft(monkeypatch, fill):
    async def stalled_run_agent(**kw):
        if fill:
            fill_with_tools({t.tool_name: t for t in kw["tools"]}, valid_spec())
        await asyncio.sleep(30)
        yield {"type": "result"}

    monkeypatch.setattr(requirements_agent, "run_agent", stalled_run_agent)
    request = AgentRequest(files=dict(DOCS), inputs={"assessment": ASSESSMENT}, options={"timeout_s": 0.2})
    events = await collect(requirements_agent.handle(request))
    if fill:
        assert [e["type"] for e in events] == ["artifact", "result"] and events[-1]["metrics"]["timed_out"] is True
    else:
        assert [e["type"] for e in events] == ["error"]


async def test_handle_resumes_once_from_the_saved_draft(monkeypatch):
    prompts = []

    async def flaky_run_agent(**kw):  # 1st run: malformed tool call after set_fields; 2nd run finishes the draft
        prompts.append(kw["prompt"])
        tools = {t.tool_name: t for t in kw["tools"]}
        spec = valid_spec()
        if len(prompts) == 1:
            tools["set_fields"](fields={f"finops.{k}": v for k, v in spec["finops"].items()})
        else:
            fill_with_tools(tools, spec)
        yield {"type": "result", "stage": "requirements", "summary": "", "data": {}, "artifacts": {},
               "metrics": {"input_tokens": 10, "output_tokens": 1, "cycles": 2}}

    monkeypatch.setattr(requirements_agent, "run_agent", flaky_run_agent)
    events = await collect(requirements_agent.handle(AgentRequest(files=dict(DOCS), inputs={"assessment": ASSESSMENT})))
    assert [e["type"] for e in events] == ["status", "artifact", "result"] and "resuming" in events[0]["message"]
    assert prompts[1] == requirements_agent.RESUME_PROMPT
    assert events[-1]["metrics"]["input_tokens"] == 20 and events[-1]["metrics"]["resumed"] is True


def test_canonical_ids_merge_duplicates():
    ctx = RequirementsContext(Workspace(DOCS), ASSESSMENT)
    ctx.upsert("requirements", [{"id": "G1 (goal)", "priority": "must"}, {"id": "g1.", "acceptance": "ok"}], "id")
    assert ctx.draft["requirements"] == [{"id": "G1", "priority": "must", "acceptance": "ok"}]


def test_runtime_serves_the_contract_over_sse(monkeypatch):
    from starlette.testclient import TestClient

    from transform_agents.requirements.runtime import app

    async def filling_run_agent(**kw):  # no Bedrock: fill the draft the way the model would
        fill_with_tools({t.tool_name: t for t in kw["tools"]}, valid_spec())
        yield {"type": "result", "stage": "requirements", "summary": "", "data": {}, "artifacts": {}, "metrics": {}}

    monkeypatch.setattr(requirements_agent, "run_agent", filling_run_agent)
    response = TestClient(app).post("/invocations", json={"files": DOCS, "inputs": {"assessment": ASSESSMENT}})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert response.status_code == 200 and [e["type"] for e in events] == ["artifact", "result"]
    assert events[-1]["data"]["workload"]["replicas_max"] == 6


def test_resolve_question_keeps_the_entry_and_renders_the_decision():
    from transform_agents.core import ResultBox, Workspace
    from transform_agents.requirements.tools import RequirementsContext, make_tools, render_appendix

    ws = Workspace({"requirements/req.md": "## 7. Open points\n- Dev/test environments: no budget yet.\n"})
    ctx = RequirementsContext(ws)
    ctx.set_lists([], None, [{"id": "Q-01", "question": "Dev/test?", "assumption": "Prod only", "impact": "Cost"}], [])
    tools = {t.tool_name: t for t in make_tools(ctx, ResultBox())}
    assert "resolved" in tools["resolve_question"](question_id="Q-01", decision="Dev/test is out of scope for now")
    assert "error" in tools["resolve_question"](question_id="Q-09", decision="x")
    (q,) = ctx.snapshot()["open_questions"]
    assert q["status"] == "resolved" and q["resolution"] == "Dev/test is out of scope for now"
    appendix = render_appendix(ctx.snapshot())
    assert "Resolved by reviewer" in appendix and "Decided: Dev/test is out of scope for now" in appendix
