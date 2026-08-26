"""Thin prompt-driven task agent registrations.

These agents share the same ``register_prompt_agent`` plumbing and differ only
in their prompt file, output contract, and workspace binding. Keeping them in
one module reduces repetitive registration boilerplate.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.research_models import EdaResult
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.plans import PlanDecision

DATA_AGENT_ID = "data"
DATA_AGENT_TYPE = "data"
GENERAL_AGENT_TYPE = "general"
VALIDATE_AGENT_ID = "validate"
VALIDATE_AGENT_TYPE = "validate"
PLAN_AGENT_TYPE = "plan"


class GeneralResult(BaseModel):
    """General Agent 的通用杂活结果."""

    model_config = ConfigDict(extra="forbid", strict=True)

    result: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)


class ValidationRepair(BaseModel):
    """Agent explanation submitted with one proposed validation repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    explanation: str = Field(min_length=1)


def register_data_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh Data Agent factory bound to the EDA workspace."""
    register_prompt_agent(
        registry,
        agent_type=DATA_AGENT_TYPE,
        output_type=EdaResult,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


def register_general_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    project_root: Path,
    runtime: ExecutionRuntime,
    extra_tools: ToolRegistry | None = None,
) -> None:
    """Register a fresh General Agent factory rooted at ``project_root``."""
    register_prompt_agent(
        registry,
        agent_type=GENERAL_AGENT_TYPE,
        output_type=GeneralResult,
        workspace=project_root,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
        extra_tools=extra_tools,
    )


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


def register_plan_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace_for: Callable[[str], Path],
    execution: ExecutionRuntime,
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
) -> None:
    """Register fresh PlanAgent factories bound to each Hypothesis workspace."""
    register_prompt_agent(
        registry,
        agent_type=PLAN_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace_for,
        runtime=execution,
        provider=provider,
        artifacts=artifacts,
        name="plan-agent-{agent_id}",
        extra_tools=extra_tools,
    )


__all__ = [
    "DATA_AGENT_ID",
    "DATA_AGENT_TYPE",
    "GENERAL_AGENT_TYPE",
    "PLAN_AGENT_TYPE",
    "VALIDATE_AGENT_ID",
    "VALIDATE_AGENT_TYPE",
    "GeneralResult",
    "ValidationRepair",
    "register_data_agent",
    "register_general_agent",
    "register_plan_agent",
    "register_validate_agent",
]
