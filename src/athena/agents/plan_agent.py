"""Autonomous SEARCH PlanAgent registration."""

from collections.abc import Callable
from pathlib import Path

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.models import AgentConfig
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
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

    def factory(agent_id: str, _config: str | None = None) -> AgentSpec:
        workspace = workspace_for(agent_id)
        tools = generic_tool_registry(workspace, runtime=execution)
        agent = Agent(
            provider,
            tools,
            load_prompt(PLAN_AGENT_TYPE),
            # SEARCH Plan 的 ReAct 环要实现+运行+评分再决策，turn 放宽到 200。
            AgentConfig(name=f"plan-agent-{agent_id}", max_turns=200),
            output_type=PlanDecision,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(agent, tools=tools, agent_type=PLAN_AGENT_TYPE),
            codec=JsonCodec(),
        )

    registry.register(PLAN_AGENT_TYPE, factory)


__all__ = ["PLAN_AGENT_TYPE", "register_plan_agent"]
