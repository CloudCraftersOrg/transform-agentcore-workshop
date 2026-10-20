"""Plan B replay: real-time pacing, REPLAY_SPEED, a live-looking stream, scripted revisions and the recording."""

import json
import re

import pytest

from transform_agents.core import STAGES, AgentRequest
from transform_agents.orchestrator import AWAITING, FakeClient, Pipeline, ReplayClient, make_client
from transform_agents.orchestrator import paths as orch_paths
from transform_agents.orchestrator.__main__ import main as cli_main
from transform_agents.orchestrator.clients import (
    env_replay_speed,
    pace,
    parse_speed,
    read_jsonl,
    stream_pieces,
    successful_attempts,
)
from transform_agents.orchestrator.record import attempts_summary, record

REQS = "# Requirements\n- FR1 keep the API\n"
FEEDBACK = "Assume dev/test is out of scope for now; size for today's traffic but keep headroom for 3x growth."


async def drain(agen) -> list[dict]:
    return [event async for event in agen]


def recorded_attempt(latency: float = 20.0) -> list[dict]:
    """The shape of a real attempt: status, a parallel tool turn, narration, a big write_file, artifacts, result."""
    return [
        {"type": "status", "stage": "modernization", "message": "agent started", "model": "deepseek.v3.2"},
        {"type": "tool_start", "id": "a", "name": "scan", "input": "{}"},
        {"type": "tool_start", "id": "b", "name": "parse", "input": "{}"},
        {"type": "tool_end", "id": "a", "name": "scan", "status": "success", "output": "ok"},
        {"type": "tool_end", "id": "b", "name": "parse", "status": "success", "output": "ok"},
        {"type": "text", "delta": ""},
        {"type": "text", "delta": "Now I'll write the service, keeping every legacy route.\n\n"},
        {"type": "tool_start", "id": "c", "name": "write_file",
         "input": '{"path": "app/api.py", "content": "\\"\\"\\"API... (+3716 chars)"}'},
        {"type": "tool_end", "id": "c", "name": "write_file", "status": "success", "output": "wrote app/api.py"},
        {"type": "artifact", "path": "app/api.py", "bytes": 3796},
        {"type": "result", "stage": "modernization", "summary": "done", "data": {}, "artifacts": {"app/api.py": "x"},
         "metrics": {"latency_s": latency}},
    ]


def timeline(pairs: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    out, t = [], 0.0
    for delay, event in pairs:
        t += delay
        out.append((t, event))
    return out


# ----------------------------------------------------------------------------- 1. pacing


def test_a_stage_replays_over_its_recorded_duration_divided_by_the_speed():
    events = recorded_attempt(latency=20.0)
    assert sum(d for d, _ in pace(events)) == pytest.approx(20.0)
    assert sum(d for d, _ in pace(events, speed=4)) == pytest.approx(5.0)
    assert all(d >= 0 for d, _ in pace(events))


def test_tools_start_early_and_the_model_time_goes_where_it_wrote_the_big_file():
    at = timeline(pace(recorded_attempt(latency=20.0)))
    assert at[0][0] == 0  # "agent started" right away
    first_tool = next(t for t, e in at if e["type"] == "tool_start")
    assert first_tool < 0.15 * 20  # front-loaded: the first turn gets half a share
    starts = {e["id"]: t for t, e in at if e["type"] == "tool_start"}
    ends = {e["id"]: t for t, e in at if e["type"] == "tool_end"}
    assert starts["b"] - starts["a"] < 0.2 and ends["a"] - starts["b"] < 1  # one turn, tools run quickly
    narration = [t for t, e in at if e["type"] == "text"]
    assert narration[-1] - narration[0] < 3  # streamed word by word, not paced like a turn
    assert starts["c"] - narration[-1] > 5  # writing a 3.7 KB file is most of the stage
    assert at[-1][0] == pytest.approx(20.0) and at[-1][1]["type"] == "result"


def test_narration_streams_in_words_and_keeps_its_text():
    events = [{"type": "text", "delta": ""}, {"type": "text", "delta": "Report written\n\nand  merged."},
              {"type": "text", "delta": "\n"}]
    pieces = stream_pieces(events)
    assert [p["delta"] for p in pieces] == ["Report ", "written\n\n", "and  ", "merged.", "\n"]
    assert "".join(p["delta"] for p in pieces) == "Report written\n\nand  merged.\n"


def test_recorded_timestamps_win_over_the_heuristic():
    events = [{"type": "status", "message": "agent started", "t": 0.0}, {"type": "tool_start", "t": 1.5},
              {"type": "text", "delta": "a long sentence", "t": 4.0}, {"type": "result", "metrics": {"latency_s": 99},
                                                                       "t": 6.0}]
    assert [d for d, _ in pace(events, speed=2)] == [0.0, 0.75, 1.25, 1.0]


def test_a_short_stage_with_lots_of_narration_is_compressed_to_its_duration():
    events = [{"type": "status", "message": "agent started", "model": "m"},
              *({"type": "text", "delta": "word " * 200} for _ in range(3)),
              {"type": "result", "metrics": {"latency_s": 2.0}}]
    pairs = pace(events)
    assert sum(d for d, _ in pairs) == pytest.approx(2.0) and min(d for d, _ in pairs) >= 0


def test_without_a_recorded_latency_the_pace_is_still_plausible():
    events = recorded_attempt()
    events[-1] = {**events[-1], "metrics": {}}
    assert 2 < sum(d for d, _ in pace(events)) < 15


def test_steps_before_the_agent_starts_are_not_model_turns():
    """k8s_validation's preflight runs tools before "agent started": they take tool time, not thinking time."""
    events = [{"type": "status", "message": "preflight"},
              {"type": "tool_start", "id": "p", "name": "policy_check", "input": "{}"},
              {"type": "tool_end", "id": "p", "name": "policy_check", "output": "FAIL"},
              {"type": "status", "message": "agent started", "model": "m"},
              {"type": "tool_start", "id": "x", "name": "edit_file", "input": "{}"},
              {"type": "tool_end", "id": "x", "name": "edit_file", "output": "ok"},
              {"type": "result", "metrics": {"latency_s": 10.0}}]
    at = timeline(pace(events))
    assert at[2][0] < 1.0 and at[4][0] - at[3][0] > 3


# ----------------------------------------------------------------------------- 1. speed: env, CLI and client


def test_replay_speed_parsing(monkeypatch):
    assert parse_speed("4") == 4.0 and parse_speed(0.5) == 0.5
    for bad in ("0", "-1", "fast", "nan", "inf", None):
        with pytest.raises(ValueError, match="replay speed"):
            parse_speed(bad)
    monkeypatch.delenv("REPLAY_SPEED", raising=False)
    assert env_replay_speed() == 1.0  # real time by default
    monkeypatch.setenv("REPLAY_SPEED", "4")
    assert env_replay_speed() == 4.0 and ReplayClient().speed == 4.0
    assert ReplayClient(speed=2).speed == 2.0 and make_client("replay", replay_speed=8).speed == 8.0
    monkeypatch.setenv("REPLAY_SPEED", "typo")
    assert env_replay_speed() == 1.0  # never breaks the stage fallback


def test_cli_rejects_a_bad_replay_speed(capsys):
    with pytest.raises(SystemExit):
        cli_main(["--mode", "replay", "--replay-speed", "0"])
    assert "replay speed must be a number > 0" in capsys.readouterr().err


async def test_replay_client_sleeps_the_paced_delays_and_streams_like_a_live_run(tmp_path):
    rec = Pipeline.create(FakeClient(), root=tmp_path)
    await drain(rec.run_stage("discovery"))
    slept: list[float] = []

    async def sleep(seconds: float) -> None:  # injected: the test never waits
        slept.append(seconds)

    replay = ReplayClient(rec.dir, speed=4, sleep=sleep)
    events = await drain(replay.stream("discovery", AgentRequest(), attempt=1))
    latency = rec.result("discovery")["metrics"]["latency_s"]
    assert sum(slept) == pytest.approx(latency / 4) and len(slept) == len(events)
    assert not [e for e in events if e["type"] == "status" and "replay" in e["message"]]
    assert events[0] == {"type": "status", "stage": "discovery", "message": "agent started",
                         "model": rec.result("discovery")["metrics"]["model"]}
    recorded = read_jsonl(rec.dir / "discovery" / "events.jsonl")
    text = "".join(e.get("delta", "") for e in recorded if e["type"] == "text")
    assert "".join(e.get("delta", "") for e in events if e["type"] == "text") == text
    assert [e for e in events if e["type"] != "text"] == [e for e in recorded if e["type"] != "text"]


# ----------------------------------------------------------------------------- 3. scripted revisions


def test_revise_option_is_validated(capsys):
    for bad in ("nosuchstage=hi", "requirements=", "requirements"):
        with pytest.raises(SystemExit):
            cli_main(["--mode", "fake", "--revise", bad])
        assert "expected STAGE=FEEDBACK" in capsys.readouterr().err


def test_rehearsal_records_a_scripted_revision_and_replay_re_enacts_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("FAKE_DELAY", "0")
    reqs = tmp_path / "reqs.md"
    reqs.write_text(REQS)
    assert cli_main(["--requirements", str(reqs), "--mode", "fake", "--auto-approve", "--until", "modernization",
                     "--revise", f"requirements={FEEDBACK}", "--revise", "discovery=add a backup finding",
                     "--revise", "discovery=and mention the AZ"]) == 0
    out = capsys.readouterr().out
    assert out.index("revising discovery: add a backup finding") < out.index("revising discovery: and mention the AZ")
    assert out.index("revising discovery: and mention the AZ") < out.index("✅ approved discovery")
    assert f"revising requirements: {FEEDBACK}" in out and "✅ approved requirements" in out
    run_id = Pipeline.list_runs(tmp_path / "runs")[0]
    rec = Pipeline.load(run_id, FakeClient(), root=tmp_path / "runs")
    st = rec.state.stages["requirements"]
    assert st.attempts == 2 and st.feedback[0]["attempt"] == 2 and st.feedback[0]["text"] == FEEDBACK
    assert rec.state.stages["discovery"].attempts == 3 and rec.state.stages["modernization"].attempts == 1
    assert ReplayClient(rec.dir).revisions() == {"discovery": ["add a backup finding", "and mention the AZ"],
                                                 "requirements": [FEEDBACK]}

    # replay mode re-enacts the recording's revisions by default, in the recorded order
    assert cli_main(["--requirements", str(reqs), "--mode", "replay", "--replay-from", str(rec.dir),
                     "--replay-speed", "1000", "--auto-approve", "--until", "requirements"]) == 0
    out = capsys.readouterr().out
    assert f"revising requirements: {FEEDBACK}" in out and "replaying" not in out.split("\n", 1)[1]
    [replayed_id] = [r for r in Pipeline.list_runs(tmp_path / "runs") if r != run_id]  # ids may share a second
    replayed = Pipeline.load(replayed_id, FakeClient(), root=tmp_path / "runs")
    assert replayed.state.client == "replay" and replayed.state.stages["requirements"].attempts == 2
    assert replayed.result("requirements")["summary"] == rec.result("requirements")["summary"]
    assert "Revised per feedback" in replayed.result("requirements")["summary"]


async def test_ask_for_changes_replays_the_recorded_revision(tmp_path):
    rec = Pipeline.create(FakeClient(), root=tmp_path / "rec")
    rec.set_requirements("reqs.md", REQS)
    await drain(rec.run_stage("discovery"))
    rec.approve("discovery")
    await drain(rec.run_stage("requirements"))
    await drain(rec.revise("requirements", FEEDBACK))

    p = Pipeline.create(ReplayClient(rec.dir, speed=1000), root=tmp_path / "runs")  # the UI on stage
    p.set_requirements("reqs.md", REQS)
    await drain(p.run_stage("discovery"))
    p.approve("discovery")
    await drain(p.run_stage("requirements"))
    first = p.result("requirements")
    assert p.client.played["requirements"].endswith("attempt 1/2") and "Revised" not in first["summary"]
    await drain(p.revise("requirements", "whatever the presenter types"))
    assert p.client.played["requirements"].endswith("attempt 2/2") and p.status("requirements") == AWAITING
    assert p.result("requirements")["summary"] != first["summary"]
    assert p.result("requirements")["summary"] == rec.result("requirements")["summary"]


async def test_record_keeps_every_attempt_even_large_ones(tmp_path, monkeypatch):
    monkeypatch.setattr("transform_agents.orchestrator.record.MAX_FILE_BYTES", 1_000)
    src = Pipeline.create(FakeClient(), root=tmp_path / "runs")
    src.set_requirements("reqs.md", REQS)
    for stage in STAGES:
        await drain(src.run_stage(stage))
        if stage == "requirements":
            await drain(src.revise(stage, FEEDBACK))
        src.approve(stage)
    (src.dir / "notes.bin").write_bytes(b"x" * 2_000)  # anything else over the limit is still dropped
    copied = record(src.dir, tmp_path / "recorded-run", force=True)  # fake agents: not a "complete" real run
    assert {"requirements/history/attempt-1.events.jsonl", "requirements/history/attempt-1.result.json",
            "requirements/events.jsonl", "requirements/result.json", "modernization/events.jsonl"} <= set(copied)
    assert "notes.bin" not in copied
    dest = tmp_path / "recorded-run"
    assert len(successful_attempts(dest / "requirements")) == 2
    assert attempts_summary(dest) == ("discovery 1, requirements 2 (1 revision), modernization 1, terraform 1, "
                                      "k8s_validation 1, finops 1")


# ----------------------------------------------------------------------------- the shipped recording

RECORDING = orch_paths.RECORDED_RUN


@pytest.mark.skipif(not (RECORDING / "state.json").exists(), reason="no samples/recorded-run yet")
def test_the_shipped_recording_has_six_stages_and_the_scripted_revision():
    state = json.loads((RECORDING / "state.json").read_text())
    assert all(state["stages"][s]["status"] == "approved" for s in STAGES)
    replay = ReplayClient(RECORDING)
    assert [f.lower() for f in replay.revisions().get("requirements", [])] == [FEEDBACK.lower()]
    assert len(successful_attempts(RECORDING / "requirements")) == 2
    for stage in STAGES:
        for path in successful_attempts(RECORDING / stage):
            events = read_jsonl(path)
            latency = events[-1]["metrics"]["latency_s"]
            assert sum(d for d, _ in pace(events)) == pytest.approx(latency)
            assert not [e for e in events if e["type"] == "status" and "replaying" in e.get("message", "")]


@pytest.mark.skipif(not RECORDING.is_dir(), reason="no samples/recorded-run yet")
def test_the_shipped_recording_has_no_local_paths_or_aws_identities():
    patterns = [r"/Users/[^/\s\"']+", r"/home/[^/\s\"']+", r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:",
                r"\b\d{12}\.dkr\.ecr\.", r"assumed-role/", r"AWSReservedSSO"]
    for path in RECORDING.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            for pattern in patterns:
                assert not re.search(pattern, text), f"{path.relative_to(RECORDING)} matches {pattern}"
