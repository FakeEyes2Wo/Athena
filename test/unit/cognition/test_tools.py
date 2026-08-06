"""Tests for five cognitive tools — verify delegate paths to Manager."""

import asyncio
import tempfile

import pytest

from athena.cognition.manager import CognitionManager
from athena.cognition.schemas import GuardRule
from athena.cognition.tools.checkpoint import AgentCheckpointTool
from athena.cognition.tools.guard import AgentGuardTool
from athena.cognition.tools.plan import AgentPlanTool
from athena.cognition.tools.reflect import AgentReflectTool
from athena.cognition.tools.track import AgentTrackTool
from athena.core.tool_types import ToolContext


async def _noop_emit(_k: str, _r: str, _d: dict | None = None) -> None:
    """空事件发射器 —— 测试中忽略工具生命周期事件。"""
    pass


@pytest.fixture
def ctx():
    """Create a minimal ToolContext for testing tool execute()."""
    return ToolContext("test_tool", "call-1", _noop_emit, asyncio.Event())


@pytest.fixture
def manager():
    """Create a CognitionManager with a temp work_root."""
    with tempfile.TemporaryDirectory() as td:
        yield CognitionManager(work_root=td)


async def test_agent_plan_creates_plan(ctx, manager):
    """agent_plan creates a plan and returns mermaid path."""
    tool = AgentPlanTool(manager)
    result = await tool.ainvoke(
        ctx,
        goal="Do task",
        steps=[
            {"title": "Step 1"},
            {"title": "Step 2", "relation": "parallel"},
        ],
    )
    assert result.success
    assert result.data["goal"] == "Do task"
    assert "plan.mermaid" in result.data["mermaid_file"]
    assert len(result.data["root_steps"]) == 2


async def test_agent_checkpoint_updates_status(ctx, manager):
    """agent_checkpoint completes a node and records checkpoint."""
    tool_plan = AgentPlanTool(manager)
    await tool_plan.ainvoke(
        ctx,
        goal="Test",
        steps=[{"title": "Step 1"}, {"title": "Step 2"}],
    )
    # get the first child node ID
    plan = await manager.get_plan()
    node_id = plan.root.children[0].id

    tool_cp = AgentCheckpointTool(manager)
    result = await tool_cp.ainvoke(
        ctx,
        node_id=node_id,
        status="completed",
        summary="Done!",
    )
    assert result.success
    assert result.data["status"] == "completed"
    assert result.data["progress"]["completed"] == 1


async def test_agent_track_returns_summary(ctx, manager):
    """agent_track with query='summary' returns progress stats."""
    await manager.create_plan(
        "Test",
        [
            {"title": "S1"},
            {"title": "S2"},
            {"title": "S3"},
        ],
    )
    tool = AgentTrackTool(manager)
    result = await tool.ainvoke(ctx, query="summary")
    assert result.success
    assert "completed" in result.data
    assert "pending" in result.data
    assert "mermaid_file" in result.data


async def test_agent_track_returns_next(ctx, manager):
    """agent_track with query='next' returns pending nodes."""
    await manager.create_plan(
        "Test",
        [
            {"title": "S1"},
            {"title": "S2"},
        ],
    )
    tool = AgentTrackTool(manager)
    result = await tool.ainvoke(ctx, query="next")
    assert result.success
    assert "next_steps" in result.data


async def test_agent_track_returns_position(ctx, manager):
    """agent_track with query='position' returns current location."""
    await manager.create_plan(
        "Test",
        [
            {"title": "S1"},
            {"title": "S2"},
        ],
    )
    tool = AgentTrackTool(manager)
    result = await tool.ainvoke(ctx, query="position")
    assert result.success
    assert "current_node_id" in result.data
    assert "path_to_root" in result.data


async def test_agent_reflect_records_reflection(ctx, manager):
    """agent_reflect creates a reflection entry."""
    await manager.create_plan("Test", [{"title": "S1"}])
    tool = AgentReflectTool(manager)
    result = await tool.ainvoke(
        ctx,
        trigger="S1 slow",
        observations="Takes 5 min",
        adjustment="Try caching",
    )
    assert result.success
    assert result.data["trigger"] == "S1 slow"


async def test_agent_reflect_adds_branches(ctx, manager):
    """agent_reflect with new_branches adds alternative branches."""
    await manager.create_plan("Test", [{"title": "Analyze"}])
    plan = await manager.get_plan()
    parent_id = plan.root.children[0].id

    tool = AgentReflectTool(manager)
    result = await tool.ainvoke(
        ctx,
        trigger="Need alternative",
        observations="Current approach slow",
        new_branches=[
            {
                "parent_id": parent_id,
                "title": "Faster method",
                "description": "Use numpy",
            },
        ],
    )
    assert result.success
    assert result.data["plan_changes_made"] == 1


async def test_agent_guard_checks_action(ctx, manager):
    """agent_guard checks an action against registered rules."""
    await manager.register_guard_rules(
        [
            GuardRule(
                rule_id="r1",
                description="No delete data",
                pattern="rm ",
                severity="error",
            ),
        ]
    )
    tool = AgentGuardTool(manager)
    result = await tool.ainvoke(ctx, action="I will rm -rf the data dir")
    assert result.success
    assert not result.data["allowed"]
    assert len(result.data["violations"]) == 1


async def test_agent_guard_allows_safe_action(ctx, manager):
    """agent_guard returns allowed=True for safe actions."""
    await manager.register_guard_rules(
        [
            GuardRule(
                rule_id="r1",
                description="No delete data",
                pattern="rm ",
                severity="error",
            ),
        ]
    )
    tool = AgentGuardTool(manager)
    result = await tool.ainvoke(ctx, action="I will read data.csv")
    assert result.success
    assert result.data["allowed"]
