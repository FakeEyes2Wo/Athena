"""Autonomous SEARCH PlanAgent registration."""

from collections.abc import Callable
from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.plans import PlanDecision

PLAN_AGENT_TYPE = "plan"


def register_plan_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace_for: Callable[[str], Path],
    execution: ExecutionRuntime,
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
    )


__all__ = ["PLAN_AGENT_TYPE", "register_plan_agent"]
