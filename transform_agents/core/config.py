"""Model and region configuration shared by every agent.

Defaults come from a live tool-calling benchmark run on 2026-10-19 (see docs/PLAN.md).
Override per agent with MODEL_<AGENT> (e.g. MODEL_TERRAFORM) or globally with MODEL_ID.
"""

import os

from botocore.config import Config
from strands.models import BedrockModel

STAGES = ["discovery", "requirements", "modernization", "terraform", "k8s_validation", "finops"]

DEFAULT_MODELS = {
    "discovery": "us.amazon.nova-2-lite-v1:0",
    "requirements": "us.amazon.nova-2-lite-v1:0",
    "modernization": "deepseek.v3.2",
    "terraform": "us.amazon.nova-2-lite-v1:0",  # mistral.mistral-large-3-675b-instruct also works; see docs/PLAN.md
    "k8s_validation": "us.amazon.nova-2-lite-v1:0",
    "finops": "us.amazon.nova-2-lite-v1:0",
    "hello": "us.amazon.nova-2-lite-v1:0",
}


def region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-west-2"


def model_id_for(agent: str) -> str:
    return os.getenv(f"MODEL_{agent.upper()}") or os.getenv("MODEL_ID") or DEFAULT_MODELS[agent]


def build_model(agent: str, max_tokens: int = 8192) -> BedrockModel:
    return BedrockModel(
        model_id=model_id_for(agent),
        region_name=region(),
        max_tokens=int(os.getenv("MODEL_MAX_TOKENS", max_tokens)),
        streaming=True,
        # Short read timeout so a stalled stream fails fast on stage instead of hanging for 10 minutes.
        boto_client_config=Config(read_timeout=int(os.getenv("MODEL_READ_TIMEOUT", 120)), connect_timeout=10,
                                  retries={"max_attempts": 4, "mode": "adaptive"}),
    )
