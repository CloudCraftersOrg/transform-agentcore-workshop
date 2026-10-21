"""The request/event contract every agent speaks, locally and on AgentCore Runtime.

Request payload (JSON):
    {"run_id", "instruction", "feedback", "inputs": {...}, "files": {path: text}, "options": {...}}

Streamed events (one JSON object per SSE `data:` line):
    status      {"type": "status", "stage", "message", "model"?}
    text        {"type": "text", "delta"}
    tool_start  {"type": "tool_start", "id", "name", "input"}
    tool_end    {"type": "tool_end", "id", "name", "status", "output"}
    artifact    {"type": "artifact", "path", "bytes"}
    result      {"type": "result", "stage", "summary", "data", "artifacts": {path: text}, "metrics"}
    error       {"type": "error", "stage", "message"}
"""

from dataclasses import asdict, dataclass, field
from typing import Any

PREVIEW_CHARS = 400


@dataclass
class AgentRequest:
    run_id: str = "adhoc"
    instruction: str = ""
    feedback: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AgentRequest":
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        # `agentcore invoke "..."` sends {"prompt": "..."}; treat it as the instruction.
        instruction = payload.get("instruction") or payload.get("prompt") or ""
        if not isinstance(instruction, str):
            raise ValueError("instruction/prompt must be a string")
        files = payload.get("files") or {}
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ValueError("files must map path -> text")
        return cls(
            run_id=str(payload.get("run_id") or "adhoc"),
            instruction=instruction,
            feedback=str(payload.get("feedback") or ""),
            inputs=dict(payload.get("inputs") or {}),
            files=dict(files),
            options=dict(payload.get("options") or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def preview(value: Any, limit: int = PREVIEW_CHARS) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit} chars)"


def status(stage: str, message: str, **extra: Any) -> dict:
    return {"type": "status", "stage": stage, "message": message, **extra}


def error(stage: str, message: str) -> dict:
    return {"type": "error", "stage": stage, "message": message}
