"""Autonomous SEARCH PlanAgent registration."""

from collections.abc import Callable
from pathlib import Path

from athena.agents.prompt_agent import register_prompt_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
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
    extra_tools: ToolRegistry | Callable[[], ToolRegistry | None] | None = None,
) -> None:
    """Register fresh PlanAgent factories bound to each Hypothesis workspace.

    ``extra_tools`` 可为 callable：plan agent 在 runtime 构造期即注册，早于
    Supervisor 的任务理解，故 Kaggle 工具用惰性 callable 到建实例时才求值。
    """
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


__all__ = ["PLAN_AGENT_TYPE", "register_plan_agent"]
