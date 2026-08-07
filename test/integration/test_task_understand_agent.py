"""Integration tests for TaskUnderstandAgent.

These tests validate that the agent builds correctly and all 9 competition
tools are registered. Tests that require a real LLM API are skipped by default.

Note: 旧 DataAnalyzeTool/DataCleanCodeGenTool 已移除，替换为 Sandbox MCP 工具
（通过 mcp_servers.json 自动发现）。原生工具现为 9 个（4 core + 5 cognition）。
"""

import pytest

# Integration tests require real LLM access -- mark as integration
pytestmark = pytest.mark.integration


class TestAgentConstruction:
    """Tests that do NOT require an LLM -- validate tool registration only."""

    async def test_all_9_native_tools_registered(self):
        """Verify the agent builds successfully with all 9 native tools (4 core + 5 cognition)."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,  # build-only, no actual LLM calls
            mcp_servers=[],
        )

        assert agent.name == "TaskUnderstandAgent"
        assert len(agent.tools.specs) == 9, (
            f"Expected 9 native tools (4 core + 5 cognition), got {len(agent.tools.specs)}"
        )

    async def test_all_expected_tool_names_present(self):
        """Verify every expected tool is in the registry by name."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,
        )

        expected_names = {
            # HF datasets (2 tools)
            "hf_dataset_search",
            "hf_dataset_download",
            # HF models (2 tools)
            "hf_model_search",
            "hf_model_download",
            # Agent 认知子系统 (5 tools)
            "agent_plan",
            "agent_checkpoint",
            "agent_track",
            "agent_reflect",
            "agent_guard",
        }

        assert len(expected_names) == 9, "Expected exactly 9 native tool names (4 core + 5 cognition)"

        tool_names = {s.name for s in agent.tools.specs}
        for name in expected_names:
            assert name in tool_names, f"Tool '{name}' not found in registry"

    async def test_agent_config_defaults(self):
        """Verify agent config is set with expected defaults."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,
        )

        assert agent.model.model_name == "deepseek-v4-flash"
        assert agent.config.max_turns == 30
        assert agent.config.max_tokens == 8192
        assert agent.config.temperature == 0.1
        assert agent.config.name == "TaskUnderstandAgent"
        assert "TaskUnderstandAgent" in agent.system_prompt
        assert "mcp_search_tools" in agent.system_prompt

    async def test_agent_config_custom_params(self):
        """Verify custom parameters are propagated to the config."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="custom-model",
            client=None,
            max_turns=5,
            max_tokens=1024,
            temperature=0.7,
            mcp_servers=[],
        )

        assert agent.model.model_name == "custom-model"
        assert agent.config.max_turns == 5
        assert agent.config.max_tokens == 1024
        assert agent.config.temperature == 0.7

    async def test_mcp_servers_registers_search_tool(self):
        """配置 mcp_servers 时注册 mcp_search_tools，且构建期不联网。"""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )
        from athena.tools.mcp.config import McpServerConfig

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,
            mcp_servers=[McpServerConfig(name="kaggle")],
        )
        tool_names = {s.name for s in agent.tools.specs}
        assert "mcp_search_tools" in tool_names
        assert len(agent.tools.specs) == 10  # 9 原生 + 1 搜索工具


@pytest.mark.skip(reason="Requires real LLM API access -- run manually")
@pytest.mark.asyncio
async def test_agent_loads_all_tools():
    """Verify agent builds and all 9 tools are registered (integration)."""
    from athena.agents.competition.task_understand_agent import (
        build_task_understand_agent,
    )

    agent = await build_task_understand_agent(
        model="deepseek-v4-flash",
        client=None,  # build-only, no actual calls
        mcp_servers=[],
    )

    assert agent.name == "TaskUnderstandAgent"
    assert len(agent.tools.specs) == 9

    tool_names = {s.name for s in agent.tools.specs}
    assert "hf_dataset_search" in tool_names
    assert "hf_model_search" in tool_names
    assert "agent_plan" in tool_names
