"""Shared registration for the two tool-free structured Rubric V2 agents."""

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.prompt_agent import load_prompt
from athena.core.agent.models import AgentConfig
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.research.rubrics.models import (
    EvaluationRubricDraft,
    HypothesisPriorityBatch,
)

EVALUATION_RUBRIC_AGENT_TYPE = "evaluation_rubric"
HYPOTHESIS_RUBRIC_AGENT_TYPE = "hypothesis_rubric"


def _register_structured_agent(
    registry,
    *,
    agent_type: str,
    provider: object,
    artifacts: ArtifactStore,
    output_type: type,
) -> None:
    """Register one no-tools agent through the only shared factory."""

    def _factory(agent_id: str, _config: str | None = None) -> AgentSpec:
        tools = ToolRegistry()
        agent = Agent(
            provider,
            tools,
            load_prompt(agent_type),
            AgentConfig(name=f"{agent_type}-agent:{agent_id}"),
            output_type=output_type,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(agent, tools=tools, agent_type=agent_type),
            codec=JsonCodec(),
        )

    registry.register(agent_type, _factory)


def register_rubric_agents(
    registry, *, provider: object, artifacts: ArtifactStore
) -> None:
    """Register both one-shot Rubric agents without duplicated boilerplate."""
    registrations = (
        (EVALUATION_RUBRIC_AGENT_TYPE, EvaluationRubricDraft),
        (HYPOTHESIS_RUBRIC_AGENT_TYPE, HypothesisPriorityBatch),
    )
    for agent_type, output_type in registrations:
        if not registry.contains(agent_type):
            _register_structured_agent(
                registry,
                agent_type=agent_type,
                provider=provider,
                artifacts=artifacts,
                output_type=output_type,
            )


__all__ = [
    "EVALUATION_RUBRIC_AGENT_TYPE",
    "HYPOTHESIS_RUBRIC_AGENT_TYPE",
    "register_rubric_agents",
]
