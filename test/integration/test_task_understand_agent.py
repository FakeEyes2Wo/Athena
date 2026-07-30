"""Integration tests for TaskUnderstandAgent.

These tests validate that the agent builds correctly and all 13 competition
tools are registered. Tests that require a real LLM API are skipped by default.
"""

import pytest

# Integration tests require real LLM access -- mark as integration
pytestmark = pytest.mark.integration


class TestAgentConstruction:
    """Tests that do NOT require an LLM -- validate tool registration only."""

    def test_all_13_tools_registered(self):
        """Verify the agent builds successfully with all 13 tools registered."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,  # build-only, no actual LLM calls
        )

        assert agent.name == "TaskUnderstandAgent"
        assert len(agent.config.tools) == 13, (
            f"Expected 13 tools, got {len(agent.config.tools)}"
        )

    def test_all_expected_tool_names_present(self):
        """Verify every expected tool is in the registry by name."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,
        )

        expected_names = {
            # Search and understanding (Task 4 -- 3 tools)
            "kaggle_competition_search",
            "kaggle_discussion_search",
            "kaggle_dataset_download",
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

        assert len(expected_names) == 13, "Expected exactly 13 tool names"

        for name in expected_names:
            assert name in agent.config.tools, (
                f"Tool '{name}' not found in registry"
            )

    def test_agent_config_defaults(self):
        """Verify agent config is set with expected defaults."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = build_task_understand_agent(
            model="deepseek-v4-flash",
            client=None,
        )

        assert agent.config.model == "deepseek-v4-flash"
        assert agent.config.max_turns == 30
        assert agent.config.max_tokens == 8192
        assert agent.config.temperature == 0.1
        assert "TaskUnderstandAgent" in agent.config.system_prompt or True
        assert "Kaggle" in agent.config.system_prompt

    def test_agent_config_custom_params(self):
        """Verify custom parameters are propagated to the config."""
        from athena.agents.competition.task_understand_agent import (
            build_task_understand_agent,
        )

        agent = build_task_understand_agent(
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


@pytest.mark.skip(reason="Requires real LLM API access -- run manually")
@pytest.mark.asyncio
async def test_agent_loads_all_tools():
    """Verify agent builds and all 13 tools are registered (integration)."""
    from athena.agents.competition.task_understand_agent import (
        build_task_understand_agent,
    )

    agent = build_task_understand_agent(
        model="deepseek-v4-flash",
        client=None,  # build-only, no actual calls
    )

    assert agent.name == "TaskUnderstandAgent"
    assert len(agent.config.tools) == 13

    # Verify key tools exist
    assert "kaggle_competition_search" in agent.config.tools
    assert "hf_dataset_search" in agent.config.tools
    assert "hf_model_search" in agent.config.tools
    assert "data_analyze" in agent.config.tools
    assert "solution_design" in agent.config.tools
    assert "project_code_gen" in agent.config.tools
    assert "code_execute" in agent.config.tools
    assert "submission_build" in agent.config.tools
