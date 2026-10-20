"""Wraps an agent handler as an Amazon Bedrock AgentCore Runtime app.

The same `handle(request)` async generator is used in-process by the orchestrator and,
through this wrapper, behind POST /invocations (SSE) on AgentCore Runtime.
"""

from collections.abc import AsyncIterator, Callable

from bedrock_agentcore import BedrockAgentCoreApp

from .contract import AgentRequest, error

Handler = Callable[[AgentRequest], AsyncIterator[dict]]


def make_app(stage: str, handle: Handler) -> BedrockAgentCoreApp:
    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invoke(payload, context=None):
        try:
            request = AgentRequest.from_payload(payload)
        except ValueError as exc:
            yield error(stage, f"bad request: {exc}")
            return
        finished = False
        try:
            async for event in handle(request):
                finished = finished or event.get("type") in ("result", "error")
                yield event
        except Exception as exc:  # a crash must still end the SSE stream with one terminal event
            if not finished:
                yield error(stage, f"{type(exc).__name__}: {exc}")

    return app
