"""Kaggle 组合根与 Agent 装配的单元测试。"""

from athena.agents.general_agent import register_general_agent
from athena.agents.plan_agent import register_plan_agent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.kaggle.auth import KaggleCredentials
from athena.kaggle.wiring import AGENT_KAGGLE_TOOLS, build_kaggle_stack, build_kaggle_tools

EXPECTED_TOOLS = {
    "kaggle_list_competitions",
    "kaggle_get_competition",
    "kaggle_list_notebooks",
    "kaggle_download_data",
    "kaggle_run",
    "kaggle_submit",
}


def test_agent_kaggle_tool_mapping_is_minimal() -> None:
    assert AGENT_KAGGLE_TOOLS["prepare"] == ("kaggle_run",)
    assert AGENT_KAGGLE_TOOLS["ideator"] == ("kaggle_list_notebooks",)
    assert AGENT_KAGGLE_TOOLS["plan"] == ("kaggle_list_notebooks",)
    assert set(AGENT_KAGGLE_TOOLS["evaluator"]) == {
        "kaggle_get_competition",
        "kaggle_download_data",
    }
    assert set(AGENT_KAGGLE_TOOLS["general"]) == EXPECTED_TOOLS
    assert "data" not in AGENT_KAGGLE_TOOLS


def test_build_kaggle_tools_registers_all_tools(tmp_path) -> None:
    stack = build_kaggle_stack(
        download_root=tmp_path / "download",
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        credentials=KaggleCredentials(bearer_token="KGAT_t"),
    )
    tools = build_kaggle_tools(stack)
    assert {item.name for item in tools.specs} == EXPECTED_TOOLS


def test_register_general_agent_includes_extra_tools(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    stack = build_kaggle_stack(
        download_root=tmp_path / "download",
        artifacts=store,
        credentials=KaggleCredentials(bearer_token="KGAT_t"),
    )
    registry = AgentTypeRegistry()
    register_general_agent(
        registry,
        provider=object(),
        artifacts=store,
        project_root=tmp_path,
        runtime=None,
        extra_tools=build_kaggle_tools(stack),
    )
    spec = registry.require_spec("general", agent_id="g")
    names = {item.name for item in spec.runner._base_tools.specs}
    assert "kaggle_run" in names
    assert "read_file" in names


def test_register_plan_agent_resolves_callable_extra_tools(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    stack = build_kaggle_stack(
        download_root=tmp_path / "download",
        artifacts=store,
        credentials=KaggleCredentials(bearer_token="KGAT_t"),
    )
    tools = build_kaggle_tools(stack)
    registry = AgentTypeRegistry()
    register_plan_agent(
        registry,
        provider=object(),
        artifacts=store,
        workspace_for=lambda _agent_id: tmp_path / "workspace",
        execution=None,
        extra_tools=lambda: tools,
    )
    spec = registry.require_spec("plan", agent_id="h1")
    names = {item.name for item in spec.runner._base_tools.specs}
    assert "kaggle_run" in names
    assert "read_file" in names
