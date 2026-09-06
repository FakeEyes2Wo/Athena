"""Kaggle Handoff Agent registration and prompt contract tests."""

from pathlib import Path

from athena.agents.kaggle_handoff_agent import (
    KAGGLE_HANDOFF_AGENT_TYPE,
    KaggleHandoffResult,
    register_kaggle_handoff_agent,
)
from athena.core.agent.registry import AgentTypeRegistry


def test_register_kaggle_handoff_agent_registers_type() -> None:
    registry = AgentTypeRegistry()
    register_kaggle_handoff_agent(
        registry,
        provider=object(),
        artifacts=object(),
        workspace=Path("."),
        runtime=None,
    )
    assert registry.contains(KAGGLE_HANDOFF_AGENT_TYPE)
    spec = registry.require_spec(KAGGLE_HANDOFF_AGENT_TYPE, agent_id="kh1")
    assert spec.codec is not None


def test_kaggle_handoff_result_requires_summary() -> None:
    result = KaggleHandoffResult(summary="collected discussions and notebooks")
    assert result.handoff_file == "KAGGLE_HANDOFF.md"
