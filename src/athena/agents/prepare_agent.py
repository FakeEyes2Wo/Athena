"""Autonomous PREPARE Agent registration."""

from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.plans import PlanDecision

PREPARE_AGENT_ID = "prepare"
PREPARE_AGENT_TYPE = "prepare"
EVALUATOR_AGENT_ID = "evaluator"
EVALUATOR_AGENT_TYPE = "evaluator"


def register_prepare_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register a fresh PREPARE Agent factory with real workspace tools."""
    register_prompt_agent(
        registry,
        agent_type=PREPARE_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
    )


def register_evaluator_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register a fresh evaluator Agent factory writing the eval script draft."""
    register_prompt_agent(
        registry,
        agent_type=EVALUATOR_AGENT_TYPE,
        output_type=PlanDecision,
        workspace=workspace,
        runtime=runtime,
        provider=provider,
        artifacts=artifacts,
    )


__all__ = [
    "PREPARE_AGENT_ID",
    "PREPARE_AGENT_TYPE",
    "EVALUATOR_AGENT_ID",
    "EVALUATOR_AGENT_TYPE",
    "register_prepare_agent",
    "register_evaluator_agent",
]
