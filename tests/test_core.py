import pytest

from transform_agents.core import AgentRequest, Workspace, extract_json, workspace_tools


def test_workspace_rejects_escaping_paths():
    ws = Workspace()
    for bad in ["/etc/passwd", "../x", "a/../../x", ""]:
        with pytest.raises(ValueError):
            ws.write(bad, "x")


def test_workspace_tracks_only_changed_files_as_artifacts():
    ws = Workspace({"legacy/app.py": "print(1)"})
    ws.write("./out/a.txt", "a")
    assert ws.artifacts() == {"out/a.txt": "a"}
    assert ws.listing("legacy/") == ["legacy/app.py"]


def test_edit_file_requires_unique_snippet():
    ws = Workspace({"f.txt": "a a b"})
    tools = {t.tool_name: t for t in workspace_tools(ws)}
    assert "found 2 times" in tools["edit_file"](path="f.txt", old="a", new="z")
    assert tools["edit_file"](path="f.txt", old="b", new="c") == "edited f.txt"
    assert ws.read("f.txt") == "a a c"


def test_materialize_strips_prefix(tmp_path):
    ws = Workspace({"terraform/main.tf": "x", "k8s/d.yaml": "y"})
    root = ws.materialize("terraform")
    assert (root / "main.tf").read_text() == "x"
    assert not (root / "k8s").exists()


def test_request_accepts_agentcore_cli_prompt():
    req = AgentRequest.from_payload({"prompt": "hi"})
    assert req.instruction == "hi" and req.run_id == "adhoc"
    with pytest.raises(ValueError):
        AgentRequest.from_payload({"prompt": {"not": "a string"}})


def test_extract_json_prefers_last_fenced_block():
    text = 'x ```json\n{"a": 1}\n``` y ```json\n{"b": 2}\n```'
    assert extract_json(text) == {"b": 2}
