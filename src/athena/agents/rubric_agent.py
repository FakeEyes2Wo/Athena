"""Tool-free structured LLM agents for the two Rubric V2 layers."""

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.prompt_agent import load_prompt
from athena.core.agent.models import AgentConfig
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.contracts import ArtifactStore
from athena.core.tool import ToolRegistry
from athena.research.rubrics.models import (
    EvaluationRubricDraft,
    HypothesisRankingBatch,
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
    """Register a no-tools agent whose only output is one validated model."""

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
    """Register evaluation-policy and hypothesis-ranking structured agents."""
    if not registry.contains(EVALUATION_RUBRIC_AGENT_TYPE):
        _register_structured_agent(
            registry,
            agent_type=EVALUATION_RUBRIC_AGENT_TYPE,
            provider=provider,
            artifacts=artifacts,
            output_type=EvaluationRubricDraft,
        )
    if not registry.contains(HYPOTHESIS_RUBRIC_AGENT_TYPE):
        _register_structured_agent(
            registry,
            agent_type=HYPOTHESIS_RUBRIC_AGENT_TYPE,
            provider=provider,
            artifacts=artifacts,
            output_type=HypothesisRankingBatch,
        )


__all__ = [
    "EVALUATION_RUBRIC_AGENT_TYPE",
    "HYPOTHESIS_RUBRIC_AGENT_TYPE",
    "register_rubric_agents",
]
