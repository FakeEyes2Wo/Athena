"""Integration tests for TaskUnderstandAgent.

These tests validate that the agent builds correctly and all 6 competition
tools are registered. Tests that require a real LLM API are skipped by default.
"""

import pytest

# Integration tests require real LLM access -- mark as integration
pytestmark = pytest.mark.integration


class TestAgentConstruction:
    """Tests that do NOT require an LLM -- validate tool registration only."""

    async def test_all_11_native_tools_registered(self):
        """Verify the agent builds successfully with all 11 native tools (6 core + 5 cognition)."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,  # build-only, no actual LLM calls
            mcp_servers=[],
        )

        assert agent.name == "TaskUnderstandAgent"
        assert len(agent.config.tools) == 11, (
            f"Expected 11 native tools (6 core + 5 cognition), got {len(agent.config.tools)}"
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
            # HF datasets (Task 5 -- 2 tools)
            "hf_dataset_search",
            "hf_dataset_download",
            # HF models (Task 6 -- 2 tools)
            "hf_model_search",
            "hf_model_download",
            # Data preparation (Task 7 -- 2 tools)
            "data_analyze",
            "data_clean_code_gen",
            # Agent 认知子系统 —— 5 个认知工具
            "agent_plan",
            "agent_checkpoint",
            "agent_track",
            "agent_reflect",
            "agent_guard",
        }

        assert len(expected_names) == 11, "Expected exactly 11 native tool names (6 core + 5 cognition)"

        for name in expected_names:
            assert name in agent.config.tools, (
                f"Tool '{name}' not found in registry"
            )

    async def test_agent_config_defaults(self):
        """Verify agent config is set with expected defaults."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,
        )

        assert agent.config.model == "deepseek-v4-flash"
        assert agent.config.max_turns == 30
        assert agent.config.max_tokens == 8192
        assert agent.config.temperature == 0.1
        assert "TaskUnderstandAgent" in agent.config.system_prompt
        assert "mcp_search_tools" in agent.config.system_prompt

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

        assert agent.config.model == "custom-model"
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
        assert "mcp_search_tools" in agent.config.tools
        assert len(agent.config.tools) == 12  # 11 原生 + 1 搜索工具


@pytest.mark.skip(reason="Requires real LLM API access -- run manually")
@pytest.mark.asyncio
async def test_agent_loads_all_tools():
    """Verify agent builds and all 10 tools are registered (integration)."""
    from athena.agents.competition.task_understand_agent import (
        build_task_understand_agent,
    )

    agent = await build_task_understand_agent(
        model="deepseek-v4-flash",
        client=None,  # build-only, no actual calls
        mcp_servers=[],
    )

    assert agent.name == "TaskUnderstandAgent"
    assert len(agent.config.tools) == 11

    # Verify key tools exist
    assert "hf_dataset_search" in agent.config.tools
    assert "hf_model_search" in agent.config.tools
    assert "data_analyze" in agent.config.tools
