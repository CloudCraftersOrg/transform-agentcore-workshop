"""ui/views.py: the pure helpers behind the stage cards (scrubbing, facts, KPIs, phases, finale) and render robustness."""

import json
import re
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from transform_agents.core import STAGES
from transform_agents.orchestrator import FakeClient, Pipeline
from ui import views as v

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / "samples" / "recorded-run"
FIXTURES = ROOT / "tests" / "fixtures"


def recorded(stage: str) -> dict:
    return json.loads((RECORDED / stage / "result.json").read_text())


def fixture(stage: str) -> dict:
    return json.loads((FIXTURES / f"{stage}_result.json").read_text())


# ----------------------------------------------------------------------------- text


# DeepSeek's leaked tool-call markers, split across deltas exactly as an old recording streamed them.
LEAKY_DELTAS = ["I'll start by scaffolding the microservice pattern", " and then adapt it.\n\n<｜DSML｜", "function_calls",
                "", "Now I'll write the test file:\n\n<｜DSML｜function_calls", "Let me read the report file first:\n\n<｜DSML｜function",
                "_calls", "Great! Both tests passed.", "\n\n</｜DSML｜function_calls>", "Done."]


def test_scrub_removes_markers_split_across_deltas():
    text = v.scrub_deltas(LEAKY_DELTAS)
    assert "DSML" not in text and "function_calls" not in text and "<" not in text
    assert "adapt it." in text and "Both tests passed." in text and text.endswith("Done.")
    assert v.scrub_text("a<｜DSML｜function_callsb") == "ab"
    assert v.plain("⚠️ Kubernetes 1.33 is in EKS extended support ✅") == "Kubernetes 1.33 is in EKS extended support"
    assert v.plain("✓ Pass") == "✓ Pass"  # the table check marks survive


def test_latest_sentence_is_one_complete_sentence_not_a_wall_of_text():
    text = v.scrub_deltas(LEAKY_DELTAS)
    line = v.latest_sentence(text)
    assert line == "Done." and "DSML" not in line
    long = "First sentence. " + "word " * 80 + "end."
    cut = v.latest_sentence(long)
    assert cut.startswith("…") and cut.endswith("end.") and len(cut) <= 200
    assert v.latest_sentence("") == "" and v.latest_sentence(None) == ""
    # one sentence only, never two run together; a sentence still streaming waits until it is complete
    two = "Now I'll write the adapted `api.py` file based on the worked example:\n\nNow I'll write the test file:\n\n"
    assert v.latest_sentence(two) == "Now I'll write the test file…"
    assert v.latest_sentence("Both tests passed. Now I should submit the result with a summ") == "Both tests passed."
    assert v.latest_sentence("still streaming without an end") == ""


def _render_leaky_job():
    import json as _json
    from pathlib import Path as _Path

    from ui.bridge import StageJob
    from ui.views import StageView

    events = [{"type": "status", "message": "agent started", "model": "deepseek.v3.2"}]
    events += [{"type": "text", "delta": d} for d in
               ["Porting the routes.\n\n<｜DSML｜", "function_calls", "All tests pass.\n\n<｜DSML｜function", "_calls"]]
    result = _json.loads(_Path("tests/fixtures/modernization_result.json").read_text())
    StageView(StageJob("modernization", 1, events=[*events, {**result, "type": "result"}])).render_final()


def _render_live_job():
    from ui.bridge import StageJob
    from ui.views import StageView

    events = [{"type": "status", "message": "agent started", "model": "deepseek.v3.2"},
              {"type": "text", "delta": "Porting the routes:\n\n"},
              {"type": "tool_start", "id": "1", "name": "scaffold_from_pattern", "input": "{}"},
              {"type": "tool_end", "id": "1", "name": "scaffold_from_pattern", "output": "Scaffolded 31 files"},
              {"type": "text", "delta": "Great! Both tests passed. Now I should submit the result with a summary of the"},
              {"type": "text", "delta": " chan"},
              {"type": "tool_start", "id": "2", "name": "submit_result", "input": "{}"},
              {"type": "text", "delta": "Migration completed"}]
    job = StageJob("modernization", 1, events=events)
    job._done.clear()  # still working: the live card
    view = StageView(job)
    view.consume()
    view.render_live()
    import streamlit as st

    st.session_state["narration"] = view.narration()


def test_live_card_shows_the_checklist_with_the_timeline_collapsed_and_one_sentence():
    at = AppTest.from_function(_render_live_job, default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    [timeline] = at.status
    assert timeline.label == "Timeline · 2 steps so far" and not timeline.proto.expanded
    markup = [m.value for m in at.markdown]
    assert any("Render the tested pattern" in m and "tx2-chip" in m for m in markup)  # the phase checklist
    [narration] = [m for m in markup if "tx2-narration" in m]
    # turns are kept apart, and the half-finished sentence before submit_result is skipped
    assert "Both tests passed." in narration and "chan" not in narration and "Migration" not in narration
    assert "chan\n\nMigration completed" in at.session_state["narration"]


def test_stage_view_scrubs_narration_from_old_recordings():
    at = AppTest.from_function(_render_leaky_job, default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    texts = [m.value for m in at.markdown] + [e.label for e in at.expander]
    assert not any("DSML" in t or "function_calls" in t for t in texts)
    assert any("Porting the routes." in t and "All tests pass." in t for t in texts)


def test_formatting_has_no_raw_numbers():
    assert v.money(365.0) == "$365" and v.money(24.53, cents=True) == "$24.53" and v.money(None) == "–"
    assert v.fmt_tokens(24566) == "24.6k" and v.fmt_tokens(215_800) == "216k" and v.fmt_tokens(900) == "900"
    assert v.fmt_secs(7.9) == "7.9 s" and v.fmt_secs(120.4) == "2 min" and v.fmt_secs(130) == "2 min 10 s"
    assert v.friendly_model("us.amazon.nova-2-lite-v1:0") == "Nova 2 Lite"
    assert v.friendly_model("deepseek.v3.2") == "DeepSeek V3.2"
    assert v.telemetry_line({"model": "us.amazon.nova-2-lite-v1:0", "latency_s": 7.9, "input_tokens": 23875,
                             "output_tokens": 691}) == "Nova 2 Lite · 7.9 s · 24.6k tokens"
    assert v.humanize("db_multi_az") == "DB multi AZ" and v.humanize("node_instance_types") == "Node instance types"
    assert v.cell(365.0) == "365" and v.cell(["LF-01", "SEC5"]) == "LF-01, SEC5" and v.cell({"cpu": "100m"}) == "cpu: 100m"
    assert v.md("$72 → $623") == "\\$72 → \\$623" and "&#36;" in v.esc("$5")


def test_container_keys_follow_the_shell_contract():
    assert v.container_key("card", "discovery") == "card-discovery"
    assert v.container_key("kpis", "finops", 1) == "kpis-finops"
    assert v.container_key("card", "requirements", 2) == "card-requirements-2"


# ----------------------------------------------------------------------------- facts and KPIs per stage


def test_kpis_for_every_recorded_stage_are_three_and_consistent_with_the_data():
    for stage in STAGES:
        result = recorded(stage)
        facts = v.stage_facts(stage, result["data"], result.get("artifacts"), result.get("metrics"))
        kpis = v.stage_kpis(stage, facts)
        assert len(kpis) == 3, (stage, kpis)
        assert all(k.value and k.value != "–" for k in kpis), (stage, kpis)
        assert v.headline(stage, facts), stage
        found, decided = v.evidence(stage, facts)
        assert found and decided, stage
    d = recorded("discovery")["data"]
    disc = v.discovery_facts(d)
    assert disc["findings"] == len(d["findings"])
    assert disc["critical"] == sum(f["severity"] == "critical" for f in d["findings"])
    assert disc["readiness"] == d["readiness_score"] and disc["strategy"] == d["strategy"]["recommended"]


def test_discovery_and_requirements_kpis():
    kpis = v.stage_kpis("discovery", v.discovery_facts(fixture("discovery")["data"]))
    assert [k.label for k in kpis] == ["Findings", "Readiness", "Strategy"]
    findings = fixture("discovery")["data"]["findings"]
    critical = sum(f["severity"] == "critical" for f in findings)
    assert kpis[0].value == str(len(findings)) and kpis[0].delta == f"{critical} critical" and kpis[0].color == "red"
    assert kpis[1].value.endswith("/100") and kpis[2].value == "Replatform"
    req = v.stage_kpis("requirements", v.requirements_facts(fixture("requirements")["data"]))
    assert [k.label for k in req] == ["Requirements", "Open questions", "Budget"] and req[2].value == "$450/mo"


def test_open_questions_count_only_unresolved_ones():
    data = {"requirements": [{"id": "G1", "priority": "must", "traces_to": ["LF-01"]}],
            "open_questions": [{"id": "Q-01", "question": "3x?", "status": "resolved", "resolution": "Size for today"},
                               {"id": "Q-02", "question": "Dev/test?", "assumption": "Spot", "status": "open"},
                               {"id": "Q-03", "question": "Window?", "assumption": "Weekend"}]}
    facts = v.requirements_facts(data)
    assert facts["questions"] == 2 and facts["questions_resolved"] == 1
    kpi = next(k for k in v.stage_kpis("requirements", facts) if k.label == "Open questions")
    assert kpi.value == "2" and kpi.delta == "1 resolved by you"
    markup = v.questions_html(data["open_questions"])
    assert markup.count("Resolved") == 1 and "Size for today" in markup and markup.count(">Open<") == 2
    assert "Assumption: Spot" in markup
    assert v.requirements_facts({"open_questions": [{"question": "?"}, "odd"]})["questions"] == 1


def test_modernization_counts_come_from_the_pytest_summary():
    assert v.test_counts({"summary": "25 passed", "output": "...\n25 passed in 0.14s"}) == (25, 25)
    assert v.test_counts({"output": "2 failed, 23 passed in 0.4s"}) == (23, 25)
    assert v.test_counts({"passed": True}) == (None, None)
    data = fixture("modernization")["data"]
    facts = v.modernization_facts(data, fixture("modernization")["artifacts"])
    passed, total = v.test_counts(data["tests"])
    kpis = v.stage_kpis("modernization", facts)
    assert kpis[0].label == "Tests" and kpis[0].value == f"{passed}/{total}" and kpis[0].delta == "passing"
    legacy = data["api_parity"]["legacy"]
    assert kpis[1].value == f"{len(legacy) - len(data['api_parity']['missing'])} ported"
    assert kpis[2].value == str(len(fixture("modernization")["artifacts"]))
    only_bool = v.stage_kpis("modernization", v.modernization_facts({"tests": {"passed": False}, "changes": []}))
    assert only_bool[0].value == "Failed"


def test_terraform_prefers_tool_verified_checks_and_flags_contradicting_coverage():
    data = recorded("terraform")["data"]
    facts = v.terraform_facts(data)
    checks = data["spec_checks"]
    kpis = v.stage_kpis("terraform", facts)
    assert [k.label for k in kpis] == ["Validate", "Spec checks", "Kubernetes"]
    assert kpis[1].value == f"{sum(c['status'] == 'pass' for c in checks)}/{len(checks)}"
    assert kpis[2].value == data["kubernetes_version"]  # the FinOps reveal's setup must be on screen
    odd = {"network": {"azs": 2, "nat_gateways": 1}, "database": {"multi_az": True},
           "spec_coverage": [{"requirement": "G2", "how": "RDS Multi-AZ, 3 AZ VPC, NAT per AZ"},
                             {"requirement": "SEC3", "how": "private subnets"}]}
    conflicts = v.coverage_conflicts(odd)
    assert set(conflicts) == {"G2"} and "2 AZs" in conflicts["G2"] and "1 NAT gateway" in conflicts["G2"]


def test_violations_flow_found_fixed_left():
    assert v.violations_flow(recorded("k8s_validation")["data"]) == (4, 4, 0)
    assert v.violations_flow({"policy_before": ["A", "B"], "policy_after": ["B"]}) == (2, 1, 1)
    assert v.violations_flow({}, {"failing_before": 4, "failing_after": 0}) == (4, 4, 0)
    drift_only = {"drift_injected": [{"file": "a"}, {"file": "b"}, {"file": "c"}],
                  "checks": [{"status": "pass"}, {"status": "warn"}]}
    assert v.violations_flow(drift_only) == (3, 3, 0)
    assert v.violations_flow({"checks": [{"status": "fail"}]}) == (None, None, 1)
    kpis = v.stage_kpis("k8s_validation", v.k8s_facts(recorded("k8s_validation")["data"]))
    assert kpis[0].label == "Violations" and kpis[0].value == "4 → 0" and kpis[0].delta == "4 fixed"


def test_smoke_tests_read_409_on_oversell_as_an_expected_pass():
    rows = v.smoke_rows([{"request": "GET /healthz", "status": 200},
                         {"request": "POST /api/orders (qty 1)", "status": 201},
                         {"request": "POST /api/orders (oversell)", "status": 409},
                         {"request": "GET /api/items", "status": 500},
                         {"request": "GET /readyz", "status": 503, "expected": 200, "passed": False},
                         {"request": "custom", "status": 204}, "not a row"])
    assert [(r["expected"], r["actual"], r["ok"]) for r in rows] == [
        (200, 200, True), (201, 201, True), (409, 409, True), (200, 500, False), (200, 503, False), (None, 204, True)]
    facts = v.k8s_facts(recorded("k8s_validation")["data"])
    smoke = recorded("k8s_validation")["data"]["live_deploy"]["smoke_tests"]
    assert facts["smoke_total"] == len(smoke) and facts["smoke_passed"] == len(smoke) and facts["oversell_blocked"]


def test_finops_alert_chart_and_kpis_from_the_data():
    data = recorded("finops")["data"]
    facts = v.finops_facts(data)
    surcharge = sum(i["monthly_usd"] for i in data["target_as_designed"]["items"] if "extended support" in i["item"])
    assert facts["surcharge"] == surcharge and facts["extended"] and facts["eks_rec_id"] == "FO-01"
    title, text = v.finops_alert(facts)
    assert title == f"Kubernetes {facts['version']} is in EKS extended support: +{v.money(surcharge)}/month"
    assert f"Upgrade to {facts['target_version']} (FO-01)" in text
    kpis = v.stage_kpis("finops", facts)
    assert [k.label for k in kpis] == ["Today", "As designed", "Optimized"]
    assert kpis[1].value == v.money(data["target_as_designed"]["monthly_usd"]) and kpis[1].color == "red"
    assert kpis[2].delta.endswith("under budget") and kpis[2].color == "green"
    chart = v.cost_chart(facts["today"], facts["designed"], facts["optimized"], facts["budget"], facts["surcharge"],
                         facts["over_designed"]).to_dict()
    colors = {row["color"] for layer in chart["layer"] for row in chart["datasets"].get(
        layer.get("data", {}).get("name", ""), []) if isinstance(row, dict) and "color" in row}
    assert {v.T["chart_today"], v.T["chart_over"], v.T["chart_optimized"]} <= colors
    assert v.finops_alert({"over_designed": True, "designed": 623, "budget": 450})[0].startswith("As designed")
    assert v.finops_alert({}) is None
    pairs = dict(v.rightsizing_pairs(data["rightsizing"]))
    assert pairs["Pod requests"] == "200m CPU · 128Mi memory" and pairs["Nodes"].startswith("t4g.small × ")


# ----------------------------------------------------------------------------- tools and phases


def test_tool_outcomes_are_friendly_and_never_raw_json():
    def out(name, output, status="success", raw_input="{}"):
        return v.tool_outcome({"name": name, "output": output, "status": status, "input": raw_input})

    assert out("policy_check", "policy_check: FAIL - 4 failing findings, 12/16 rules pass (targets: base)") == \
        "4 violations · 12/16 rules pass"
    assert out("policy_check", "policy_check: PASS - 0 failing findings, 16/16 rules pass") == \
        "No violations · 16/16 rules pass"
    assert out("terraform_validate", "terraform_validate attempt 1/4: VALID (level: terraform, 3.9s)\nSpec mismatches "
                                     "(fix them):\n  - az_count: expected 2\n  - nat_gateways: expected 1\n") == \
        "Valid (attempt 1) · 2 spec mismatches to fix"
    assert out("compare_scenarios", "current 71.89 | as designed 622.88 (8.7x today) | optimized 207.57 USD/month") == \
        "$72 today · $623 as designed · $208 optimized"
    assert out("run_service_tests", "PASS: 25 passed (0.8s)\nAPI parity: 6/6 legacy /api routes ported.") == \
        "25 passed · API parity 6/6"
    assert out("scan_codebase", '{"application": {"framework": "flask 2.0.3", "loc": 113, "files": 8}') == \
        "Flask 2.0.3 · 8 files · 113 lines of code"
    assert out("mystery_tool", '{"raw": "json"}') == "" and out("mystery_tool", "did a thing") == "did a thing"
    assert out("write_file", "boom", status="error").startswith("Failed:")
    assert v.tool_label("write_file", '{"path": "microservice/app/api.py", "content": "..."}') == "Write api.py"
    assert v.tool_label("mystery_tool") == "Run mystery tool" and v.tool_info("set_variables").kind == "model"


def _tools(*calls: tuple[str, str]) -> list[dict]:
    return [{"name": n, "input": json.dumps({"path": p}) if p else "{}", "status": "success", "output": ""}
            for n, p in calls]


def test_phase_checklist_follows_the_tools():
    tools = _tools(("scaffold_from_pattern", ""), ("write_file", "microservice/app/api.py"),
                   ("write_file", "microservice/tests/test_api.py"))
    views = v.phase_views("modernization", tools, running=True)
    assert [p.label for p in views] == ["Render the tested pattern", "Port the routes", "Write the tests",
                                        "Run the tests", "Validate the manifests"]
    assert [p.kind for p in views] == ["tool", "model", "model", "tool", "tool"]
    assert [p.state for p in views] == ["done", "done", "done", "current", "next"]
    running = _tools(("scaffold_from_pattern", ""))
    running[0]["status"] = "running"
    assert [p.state for p in v.phase_views("modernization", running)][:2] == ["current", "next"]
    # terraform: set_variables again after validate goes back to "Map the spec onto it"
    tf = _tools(("get_blueprint", ""), ("set_variables", ""), ("terraform_validate", ""), ("set_variables", ""))
    assert v.assign_phases("terraform", tf) == [0, 1, 2, 1]
    assert [p.state for p in v.phase_views("terraform", tf)] == ["done", "done", "current"]
    # k8s without drift: the fix and re-check phases were not needed
    k8s = _tools(("kubeconform_check", ""), ("policy_check", ""), ("deploy_to_kind", ""))
    assert [p.state for p in v.phase_views("k8s_validation", k8s, running=False)] == [
        "done", "done", "skipped", "skipped", "done"]
    assert v.phase_views("unknown-stage", k8s) == []


def test_recorded_runs_map_onto_every_stage_checklist():
    for stage in STAGES:
        events = [json.loads(line) for line in (RECORDED / stage / "events.jsonl").read_text().splitlines() if line]
        tools = [{"name": e["name"], "input": e.get("input", ""), "status": "success", "output": ""}
                 for e in events if e.get("type") == "tool_start"]
        views = v.phase_views(stage, tools, running=False)
        assert views and views[0].state == "done", stage
        assert v.timeline_html([{**t, "output": "{}"} for t in tools]).count("tx2-row") == len(tools)


# ----------------------------------------------------------------------------- finale


def test_finale_numbers_come_from_the_pipeline(tmp_path):
    state = json.loads((RECORDED / "state.json").read_text())
    shutil.copytree(RECORDED, tmp_path / state["run_id"])
    p = Pipeline.load(state["run_id"], FakeClient(), root=tmp_path)
    facts = v.finale_facts(p)
    seconds = sum(s["metrics"]["latency_s"] for s in state["stages"].values())
    tokens = sum(s["metrics"]["input_tokens"] + s["metrics"]["output_tokens"] for s in state["stages"].values())
    assert facts["seconds"] == pytest.approx(seconds) and facts["tokens"] == tokens and facts["approved_all"]
    assert v.finale_title(facts) == f"One server. Six agents. {v.minutes_phrase(seconds)}. You approved every step."
    tiles = {t.label: t for t in v.finale_tiles(facts)}
    findings = p.data("discovery")["findings"]
    assert tiles["Findings"].value == str(len(findings))
    assert tiles["Requirements"].value == str(len(p.data("requirements")["requirements"]))
    assert tiles["Tests"].value == "{}/{}".format(*v.test_counts(p.data("modernization")["tests"]))
    assert tiles["Infrastructure"].value == "Valid"
    assert tiles["Smoke tests"].value == "5/5"
    fin = p.data("finops")
    assert tiles["Monthly cost"].value == " → ".join(v.money(fin[k]["monthly_usd"]) for k in
                                                     ("current", "target_as_designed", "target_optimized"))
    assert tiles["Caught before go-live"].value == f"{v.money(v.finops_facts(fin)['surcharge'])}/mo"
    assert v.minutes_phrase(120.4) == "About 2 minutes" and v.minutes_phrase(30) == "Under a minute"
    assert v.minutes_phrase(None) == ""


class BrokenPipeline:
    run_id = "broken"

    def result(self, stage):
        raise RuntimeError("no results")

    def data(self, stage):
        return "not a dict"

    def status(self, stage):
        return "approved"


def test_finale_tolerates_a_broken_pipeline():
    facts = v.finale_facts(BrokenPipeline())
    assert facts["seconds"] is None and v.finale_tiles(facts) == []
    assert v.finale_title(facts) == "One server. Six agents. You approved every step."


# ----------------------------------------------------------------------------- robustness


ODD_VALUES = [{}, None, [], "text", 42, {"findings": "not a list", "readiness_score": "high", "strategy": "x"},
              {"requirements": [None, 3, {"priority": None}], "open_questions": "?"},
              {"tests": "25 passed", "api_parity": [], "changes": [{"file": None}]},
              {"valid": "yes", "spec_checks": [{"status": None}], "network": "2 AZ"},
              {"checks": [{}], "policy_before": "A", "live_deploy": {"smoke_tests": [{"status": "teapot"}]}},
              {"recommendations": [{}], "current": {"monthly_usd": "abc"}, "target_as_designed": [],
               "budget_usd": "450"}]


def test_helpers_tolerate_missing_or_odd_fields():
    for stage in [*STAGES, "unknown"]:
        for value in ODD_VALUES:
            facts = v.stage_facts(stage, value, value, value)
            assert isinstance(facts, dict)
            assert isinstance(v.stage_kpis(stage, facts), list)
            assert isinstance(v.headline(stage, facts), str)
            v.evidence(stage, facts)
            v.phase_views(stage, [{"name": None}, {}])
    assert v.finops_alert(v.finops_facts({})) is None
    assert v.smoke_rows("nope") == [] and v.rightsizing_pairs(None) == []
    assert v.tool_outcome({}) == "" and v.timeline_html([]) and v.kv_html([("a", None)]) == ""


def _render_odd_results():
    import streamlit as st

    from ui.views import STAGES as stages
    from ui.views import render_result

    st.session_state.setdefault("presenter", False)
    odd = [{}, {"data": "nope", "metrics": [], "artifacts": "x"},
           {"summary": "Revised: changed things", "data": {"findings": [{"severity": None}], "valid": None,
                                                           "checks": "?", "recommendations": [None],
                                                           "requirements": [{}], "changes": [3]},
            "artifacts": {"a.md": "# hi", "b.json": {"k": 1}}, "metrics": {"latency_s": "fast"}}]
    for i, stage in enumerate([*stages, "mystery"]):
        for j, result in enumerate(odd):
            render_result(stage, result, key=f"odd-{i}-{j}", attempt=j + 1)


def test_render_result_never_raises_on_odd_results():
    at = AppTest.from_function(_render_odd_results, default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Revised after your feedback" in m.value for m in at.markdown)


def _render_recorded_cards():
    import json as _json
    from pathlib import Path as _Path

    import streamlit as st

    from ui.bridge import StageJob
    from ui.views import STAGES as stages
    from ui.views import StageView, render_finale

    for stage in stages:
        lines = (_Path("samples/recorded-run") / stage / "events.jsonl").read_text().splitlines()
        StageView(StageJob(stage, 1, events=[_json.loads(line) for line in lines if line])).render_final()
    render_finale(st.session_state.get("pipeline_stub"))


@pytest.mark.parametrize("presenter", [False, True])
def test_recorded_cards_render_and_presenter_mode_hides_json_and_telemetry(presenter):
    at = AppTest.from_function(_render_recorded_cards, default_timeout=60)
    at.session_state["presenter"] = presenter
    at.session_state["pipeline_stub"] = BrokenPipeline()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # presenter mode drops the FinOps KPI row: the cost chart above it carries the same numbers
    assert len(at.metric) == 3 * len(STAGES) - (3 if presenter else 0)
    assert {m.label for m in at.metric} >= {"Findings", "Tests", "Spec checks", "Violations", "Smoke tests"}
    assert ("As designed" in {m.label for m in at.metric}) is not presenter
    telemetry = [c.value for c in at.caption if "tokens" in c.value]
    if presenter:
        assert not at.json and not telemetry
    else:
        assert len(at.json) == len(STAGES) and len(telemetry) == len(STAGES)
    markup = [m.value for m in at.markdown]
    # the model's summary: one clamped line with the full text as its tooltip, none in presenter mode; the full
    # text always opens the Overview tab
    clamped = [m for m in markup if "tx2-summary" in m]
    if presenter:
        assert not clamped
    else:
        assert len(clamped) == len(STAGES) and all("-webkit-line-clamp:1" in m and 'title="' in m for m in clamped)
    assert sum("The model&#x27;s summary." in m or "The model's summary." in m for m in markup) == len(STAGES)
    assert any(m.value.startswith("**In AWS Transform**") for m in at.markdown)
    assert any("Simulated drift" in m.value for m in at.markdown)


def _render_requirements_revision():
    import json as _json
    from pathlib import Path as _Path

    from ui.views import render_result

    result = _json.loads((_Path("samples/recorded-run") / "requirements" / "result.json").read_text())
    render_result("requirements", result, key="req-rev", attempt=2)


@pytest.mark.parametrize("presenter", [False, True])
def test_requirements_revision_says_so_and_shows_resolved_and_open_pills(presenter):
    data = recorded("requirements")["data"]
    resolved = sum(v.question_resolved(q) for q in data["open_questions"])
    assert resolved and resolved < len(data["open_questions"])  # the recording is the revision (attempt 2)
    # the model's decision stays one line at presenter scale: what the revision applied
    assert v.evidence("requirements", v.requirements_facts(data))[1] == \
        f"{len(data['requirements'])} requirements · your {resolved} answers applied"
    at = AppTest.from_function(_render_requirements_revision, default_timeout=60)
    at.session_state["presenter"] = presenter
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    markup = [m.value for m in at.markdown]
    assert any("Revised after your feedback" in m and "tx2-headline" in m for m in markup)
    [questions] = [m for m in markup if "Your decision:" in m]
    assert questions.count(">Resolved<") == resolved
    assert questions.count(">Open<") == len(data["open_questions"]) - resolved
    assert any(m.label == "Open questions" and m.value == str(len(data["open_questions"]) - resolved)
               for m in at.metric)


def _rem_sizes(markup: str) -> list[float]:
    """Every font-size in px (at the shell's 18 px root)."""
    return [float(x) * 18 for x in re.findall(r"font-size:([\d.]+)rem", markup)]


def test_small_labels_are_at_least_14px_and_no_robot_or_sparkle_icons():
    assert min(_rem_sizes(v.chip("tool") + v.chip("model"))) >= 14
    assert min(_rem_sizes(v.evidence_html("17 findings", "Replatform"))) >= 14
    assert min(_rem_sizes(v.kv_html([("Instance", "t3.large")]))) >= 14
    assert min(_rem_sizes(v.phases_html(v.phase_views("discovery", _tools(("scan_codebase", "")))))) >= 14
    source = (ROOT / "ui" / "views.py").read_text()
    assert "smart_toy" not in source and "auto_awesome" not in source
    assert v.AGENT_AVATAR == ":material/hub:" and "lightbulb" in v.chip("model")


def test_headline_clamps_the_summary_to_one_line():
    markup = v.headline_html("17 findings.", 'Replatform: fix the "critical" flaws first.', revised=True)
    assert "-webkit-line-clamp:1" in markup and 'title="Replatform: fix the &quot;critical&quot; flaws first."' in markup
    assert "Revised after your feedback" in markup
    assert "tx2-summary" not in v.headline_html("17 findings.", "")
    assert v.headline_html("", "") == "" and "Revised" in v.headline_html("", "", revised=True)


def test_finale_tiles_fit_without_truncation():
    tiles = v.finale_tiles({"stages": {"finops": {"today": 72, "designed": 623, "optimized": 208, "extended": True,
                                                  "surcharge": 365}}})
    caught = next(t for t in tiles if t.label == "Caught before go-live")
    assert caught.value == "$365/mo" and caught.delta == "EKS extended support"
    assert ".st-key-finale [data-testid=\"stMetricDelta\"]" in v.CSS  # a long caption wraps instead of "supp…"
