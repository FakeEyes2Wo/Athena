"""Stable validation repair Agent registration."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.execution.runtime import ExecutionRuntime

VALIDATE_AGENT_ID = "validate"
VALIDATE_AGENT_TYPE = "validate"


class ValidationRepair(BaseModel):
    """Agent explanation submitted with one proposed validation repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    explanation: str = Field(min_length=1)


def register_validate_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register fresh validate Agent factories bound to one isolated workspace."""
    register_prompt_agent(
        registry,
        agent_type=VALIDATE_AGENT_TYPE,
        output_type=ValidationRepair,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
    )


__all__ = [
    "VALIDATE_AGENT_ID",
    "VALIDATE_AGENT_TYPE",
    "ValidationRepair",
    "register_validate_agent",
]
