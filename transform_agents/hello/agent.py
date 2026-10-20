"""Reference agent: the smallest complete example of the workshop agent pattern.

Every stage agent follows this shape: a system prompt, workspace tools plus its own
domain tools, a `submit_result` tool, and `handle(request)` streaming contract events.
"""

from collections.abc import AsyncIterator

from transform_agents.core import AgentRequest, ResultBox, Workspace, json_submit_tool, run_agent, workspace_tools

STAGE = "hello"

SYSTEM_PROMPT = """You are a smoke-test agent for the migration workshop.
List the workspace files, write `hello/report.md` describing what you found in one short paragraph,
then call submit_result with result_json {"file_count": <int>, "files": [<paths>]}."""


async def handle(request: AgentRequest) -> AsyncIterator[dict]:
    ws = Workspace(request.files)
    box = ResultBox()
    tools = [*workspace_tools(ws), json_submit_tool(box, "Submit the smoke-test result.")]
    prompt = request.instruction or "Inspect the workspace and report."
    async for event in run_agent(stage=STAGE, system_prompt=SYSTEM_PROMPT, prompt=prompt, tools=tools,
                                 workspace=ws, box=box):
        yield event
