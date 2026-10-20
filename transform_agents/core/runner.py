"""Runs a Strands agent and translates its stream into the workshop event contract."""

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from strands import Agent, tool
from strands.tools.executors import SequentialToolExecutor

from .config import build_model, model_id_for
from .contract import error, preview, status
from .workspace import Workspace


class ResultBox:
    """Holds the structured result an agent submits through its `submit_result` tool."""

    def __init__(self):
        self.summary: str | None = None
        self.data: dict[str, Any] | None = None

    def set(self, summary: str, data: dict[str, Any]) -> str:
        self.summary, self.data = summary, data
        return "result recorded; reply with a one-line confirmation and stop"


def json_submit_tool(box: ResultBox, description: str):
    """Generic submit tool for agents whose result is a free-form JSON object."""

    @tool(name="submit_result", description=description)
    def submit_result(summary: str, result_json: str) -> str:
        """Submit the final result. Call exactly once, at the very end.

        Args:
            summary: 2-4 sentence human-readable summary for the reviewer.
            result_json: The structured result as a JSON object string.
        """
        try:
            data = json.loads(result_json)
        except json.JSONDecodeError as exc:
            return f"error: result_json is not valid JSON ({exc}); fix it and call submit_result again"
        if not isinstance(data, dict):
            return "error: result_json must be a JSON object"
        return box.set(summary, data)

    return submit_result


def extract_json(text: str) -> dict[str, Any] | None:
    """Fallback when a model answers in prose instead of calling submit_result."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.findall(r"(\{.*\})", text, re.S)
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


async def run_agent(
    *,
    stage: str,
    system_prompt: str,
    prompt: str,
    tools: list,
    workspace: Workspace,
    box: ResultBox,
    max_tokens: int = 8192,
) -> AsyncIterator[dict]:
    """Stream one agent run as contract events, ending with exactly one `result` or `error`."""
    model_id = model_id_for(stage)
    yield status(stage, "agent started", model=model_id)
    started = time.time()
    agent = Agent(
        model=build_model(stage, max_tokens=max_tokens),
        tools=tools,
        system_prompt=system_prompt,
        callback_handler=None,
        # One tool at a time: parallel edit_file calls on the same file could otherwise lose an edit.
        tool_executor=SequentialToolExecutor(),
    )
    tool_names: dict[str, str] = {}
    final_text, usage, cycles, stop_reason = "", {}, 0, None
    try:
        async for ev in agent.stream_async(prompt):
            if "data" in ev and isinstance(ev["data"], str):
                yield {"type": "text", "delta": ev["data"]}
            elif "message" in ev:
                msg = ev["message"]
                for block in msg.get("content", []):
                    if "toolUse" in block:
                        use = block["toolUse"]
                        tool_names[use["toolUseId"]] = use["name"]
                        yield {"type": "tool_start", "id": use["toolUseId"], "name": use["name"],
                               "input": preview(_tool_input_preview(use.get("input")))}
                    elif "toolResult" in block:
                        res = block["toolResult"]
                        text = " ".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
                        yield {"type": "tool_end", "id": res["toolUseId"], "name": tool_names.get(res["toolUseId"], "?"),
                               "status": res.get("status", "success"), "output": preview(text)}
                    elif "text" in block and msg.get("role") == "assistant":
                        final_text = block["text"]
            elif "result" in ev:
                result = ev["result"]
                usage = dict(result.metrics.accumulated_usage or {})
                cycles = result.metrics.cycle_count
                stop_reason = result.stop_reason
    except Exception as exc:  # surfaced to the UI instead of killing the stream
        yield error(stage, f"{type(exc).__name__}: {exc}")
        return

    data = box.data if box.data is not None else (extract_json(final_text) or {})
    summary = box.summary or final_text.strip() or "(no summary)"
    for path, content in workspace.artifacts().items():
        yield {"type": "artifact", "path": path, "bytes": len(content)}
    yield {
        "type": "result",
        "stage": stage,
        "summary": summary,
        "data": data,
        "artifacts": workspace.artifacts(),
        "metrics": {
            "model": model_id,
            "latency_s": round(time.time() - started, 1),
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "cycles": cycles,
            "stop_reason": stop_reason,
            "submitted": box.data is not None,
        },
    }


def _tool_input_preview(value: Any) -> str:
    if isinstance(value, dict):
        # Show paths/short args in full but never dump whole file bodies into the timeline.
        return json.dumps({k: (preview(v, 80) if isinstance(v, str) else v) for k, v in value.items()})
    return str(value)
