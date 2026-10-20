"""AgentCore Runtime entrypoint: `python -m transform_agents.terraform.runtime` serves POST /invocations on :8080."""

from transform_agents.core.app import make_app
from transform_agents.terraform.agent import STAGE, handle

app = make_app(STAGE, handle)

if __name__ == "__main__":
    app.run()
