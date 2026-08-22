"""dataclean agent 注册与数据无关 prompt 的单元测试。"""

from pathlib import Path

import pytest

from athena.agents.prepare_dataclean_agent import (
    DATACLEAN_AGENT_ID,
    DATACLEAN_AGENT_TYPE,
    register_dataclean_agent,
)
from athena.agents.prompt_agent import load_prompt
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore


def test_dataclean_registration_and_data_agnostic_prompt(tmp_path: Path) -> None:
    registry = AgentTypeRegistry()
    register_dataclean_agent(
        registry,
        provider=object(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        workspace=tmp_path / "dataclean",
        runtime=None,
    )
    assert registry.contains(DATACLEAN_AGENT_TYPE)

    prompt = load_prompt(DATACLEAN_AGENT_TYPE)
    for marker in ("INSPECT", "DECIDE", "IMPLEMENT", "DOCUMENT", "DATACLEAN_HANDOFF.md", "PlanDecision"):
        assert marker in prompt, f"prompt missing marker {marker!r}"
    # 数据无关：prompt 不得写死任何数据集布局
    assert "filament" not in prompt.lower()
    assert "coco" not in prompt.lower()


def test_dataclean_constants() -> None:
    assert DATACLEAN_AGENT_ID == "dataclean"
    assert DATACLEAN_AGENT_TYPE == "dataclean"
