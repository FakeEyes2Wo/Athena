"""Stable validation repair Agent registration."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.models import AgentConfig
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec

VALIDATE_AGENT_ID = "validate"
VALIDATE_AGENT_TYPE = "validate"


class ValidationRepair(BaseModel):
    """Agent explanation submitted with one proposed validation repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    explanation: str = Field(min_length=1)


def register_validate_agent(
    registry,
    *,
    provider: Any,
    artifacts,
    workspace: Path,
    runtime,
) -> None:
    """Register fresh validate Agent factories bound to one isolated workspace."""

    def factory(_agent_id: str, _config: str | None = None) -> AgentSpec:
        tools = generic_tool_registry(workspace, runtime=runtime)
        agent = Agent(
            provider,
            tools,
            load_prompt(VALIDATE_AGENT_TYPE),
            AgentConfig(name="validate-agent"),
            output_type=ValidationRepair,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(agent, tools=tools, agent_type=VALIDATE_AGENT_TYPE),
            codec=JsonCodec(),
        )

    registry.register(VALIDATE_AGENT_TYPE, factory)


__all__ = [
    "VALIDATE_AGENT_ID",
    "VALIDATE_AGENT_TYPE",
    "ValidationRepair",
    "register_validate_agent",
]
