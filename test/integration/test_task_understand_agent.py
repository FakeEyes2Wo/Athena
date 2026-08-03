"""Integration tests for TaskUnderstandAgent.

These tests validate that the agent builds correctly and all 10 competition
tools are registered. Tests that require a real LLM API are skipped by default.
"""

import pytest

# Integration tests require real LLM access -- mark as integration
pytestmark = pytest.mark.integration


class TestAgentConstruction:
    """Tests that do NOT require an LLM -- validate tool registration only."""

    async def test_all_10_tools_registered(self):
        """Verify the agent builds successfully with all 10 tools registered."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = await build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,  # build-only, no actual LLM calls
        )

        assert agent.name == "TaskUnderstandAgent"
        assert len(agent.config.tools) == 10, (
            f"Expected 10 tools, got {len(agent.config.tools)}"
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
            # Modeling and submission (Task 8 -- 4 tools)
            "solution_design",
            "project_code_gen",
            "code_execute",
            "submission_build",
        }

        assert len(expected_names) == 10, "Expected exactly 10 tool names"

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
        assert len(agent.config.tools) == 11  # 10 原生 + 1 搜索工具


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
    )

    assert agent.name == "TaskUnderstandAgent"
    assert len(agent.config.tools) == 10

    # Verify key tools exist
    assert "hf_dataset_search" in agent.config.tools
    assert "hf_model_search" in agent.config.tools
    assert "data_analyze" in agent.config.tools
    assert "solution_design" in agent.config.tools
    assert "project_code_gen" in agent.config.tools
    assert "code_execute" in agent.config.tools
    assert "submission_build" in agent.config.tools
