"""IdeatorProfile 注册与输出契约的聚焦测试。"""

from pathlib import Path
from types import SimpleNamespace

from athena.agents.ideator_agent import (
    BASELINE_IDEATOR_PROFILE,
    IDEATOR_MAX_TOKENS,
    MOONSHOT_IDEATOR_PROFILE,
    SEARCH_IDEATOR_PROFILES,
    HandoffResult,
    register_ideator_agent,
)
from athena.research.idea_generation.idea_schemas import (
    IdeatorHypothesisBatch,
)


class _Registry:
    def __init__(self) -> None:
        self.factories: dict[str, object] = {}

    def register(self, agent_type: str, factory) -> None:
        self.factories[agent_type] = factory

    def contains(self, agent_type: str) -> bool:
        return agent_type in self.factories


def _noop_tool():
    from athena.core.tool import tool

    @tool
    async def shell_command(command: str) -> dict:
        return {"stdout": ""}

    return shell_command


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(shell_command_tool=lambda _root: _noop_tool())


def _register(profile):
    registry = _Registry()
    register_ideator_agent(
        registry,
        provider=object(),
        artifacts=SimpleNamespace(),
        workspace=Path("."),
        runtime=_runtime(),
        profile=profile,
    )
    return registry.factories[profile.agent_type]("agent-1")


def test_search_profiles_cover_exploit_bold_moonshot() -> None:
    assert [p.agent_type for p in SEARCH_IDEATOR_PROFILES] == [
        "ideator_exploit",
        "ideator_bold",
        "ideator_moonshot",
    ]


def test_moonshot_profile_binds_gated_output_contract() -> None:
    spec = _register(MOONSHOT_IDEATOR_PROFILE)
    assert spec.runner._agent._output_type is IdeatorHypothesisBatch
    assert spec.runner._agent.config.max_tokens == IDEATOR_MAX_TOKENS


def test_baseline_profile_binds_handoff_output_contract() -> None:
    spec = _register(BASELINE_IDEATOR_PROFILE)
    assert spec.runner._agent._output_type is HandoffResult
