"""Provider routing contracts for the two Rubric V2 agents."""

from athena.agents.rubric_agent import (
    EVALUATION_RUBRIC_AGENT_TYPE,
    HYPOTHESIS_RUBRIC_AGENT_TYPE,
    register_rubric_agents,
)
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore


def test_rubrics_use_reasoning_provider_and_normal_repair_provider(tmp_path) -> None:
    registry = AgentTypeRegistry()
    reasoning_provider = object()
    normal_provider = object()
    register_rubric_agents(
        registry,
        provider=reasoning_provider,
        repair_provider=normal_provider,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )

    for agent_type in (
        EVALUATION_RUBRIC_AGENT_TYPE,
        HYPOTHESIS_RUBRIC_AGENT_TYPE,
    ):
        spec = registry.require_spec(agent_type, agent_id=f"{agent_type}-1")
        agent = spec.runner._agent
        assert agent.model is reasoning_provider
        assert agent._structured_repair_provider is normal_provider
        assert agent.tools.specs == []
