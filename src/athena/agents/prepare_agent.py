"""Autonomous PREPARE Agent registration."""

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

PREPARE_AGENT_ID = "prepare"
PREPARE_AGENT_TYPE = "prepare"


def register_prepare_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace: Path,
    runtime: ExecutionRuntime,
) -> None:
    """Register a fresh PREPARE Agent factory with real workspace tools."""

    def factory(_agent_id: str, _config: str | None = None) -> AgentSpec:
        tools = generic_tool_registry(workspace, runtime=runtime)
        agent = Agent(
            provider,
            tools,
            load_prompt(PREPARE_AGENT_TYPE),
            # PREPARE 的 ReAct 环要跑完整个 baseline 管线（检查→训练→评估→决策），
            # turn 放宽到 200，避免研究途中耗尽。
            AgentConfig(name="prepare-agent", max_turns=200),
            output_type=PlanDecision,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(
                agent,
                tools=tools,
                agent_type=PREPARE_AGENT_TYPE,
            ),
            codec=JsonCodec(),
        )

    registry.register(PREPARE_AGENT_TYPE, factory)


__all__ = [
    "PREPARE_AGENT_ID",
    "PREPARE_AGENT_TYPE",
    "register_prepare_agent",
]
