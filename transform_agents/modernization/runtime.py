"""AgentCore Runtime entrypoint: `python -m transform_agents.modernization.runtime` serves /invocations on :8080."""

from transform_agents.core.app import make_app
from transform_agents.modernization.agent import STAGE, handle

app = make_app(STAGE, handle)

if __name__ == "__main__":
    app.run()
