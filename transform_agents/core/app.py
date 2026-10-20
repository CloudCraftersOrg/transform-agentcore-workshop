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
        async for event in handle(request):
            yield event

    return app
