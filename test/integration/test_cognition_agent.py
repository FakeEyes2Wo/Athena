"""Integration test: full agent loop exercising cognition tools."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.cognition import register_cognition_tools
from athena.cognition.manager import CognitionManager


async def _noop_emit(_k, _r, _d=None): pass


@pytest.fixture
async def ctx():
    return ToolContext("test", "call-1", _noop_emit, asyncio.Event())


@pytest.mark.asyncio
async def test_register_cognition_tools_registers_five_tools():
    """register_cognition_tools adds exactly 5 tools to an empty registry."""
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as td:
        mgr = await register_cognition_tools(registry, work_root=td)
        assert len(registry) == 5
        assert "agent_plan" in registry
        assert "agent_checkpoint" in registry
        assert "agent_track" in registry
        assert "agent_reflect" in registry
        assert "agent_guard" in registry
        assert isinstance(mgr, CognitionManager)


@pytest.mark.asyncio
async def test_full_plan_checkpoint_track_cycle(ctx):
    """Simulate a complete agent workflow: plan -> track -> checkpoint -> track."""
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as td:
        mgr = await register_cognition_tools(registry, work_root=td)

        # Step 1: Create plan
        plan_result = await registry.resolve("agent_plan").ainvoke(
            ctx,
            goal="Prepare Titanic dataset",
            steps=[
                {"title": "Search competition info"},
                {"title": "Download data"},
                {"title": "Analyze dataset structure", "relation": "alternative",
                 "children": [
                     {"title": "ls+cat approach"},
                     {"title": "pandas approach"},
                 ]},
                {"title": "Clean dataset"},
            ],
        )
        assert plan_result.success
        assert plan_result.data["goal"] == "Prepare Titanic dataset"

        # Step 2: Check track summary
        track_result = await registry.resolve("agent_track").ainvoke(
            ctx, query="summary"
        )
        assert track_result.success
        assert track_result.data["pending"] >= 1
        assert "plan.mermaid" in track_result.data["mermaid_file"]

        # Step 3: Get plan to find node IDs
        plan = await mgr.get_plan()
        node1 = plan.root.children[0]
        assert node1.status.value == "in_progress"  # auto-advanced on create

        # Step 4: Complete first step via checkpoint
        cp_result = await registry.resolve("agent_checkpoint").ainvoke(
            ctx,
            node_id=node1.id,
            status="completed",
            summary="Found Titanic competition on Kaggle",
        )
        assert cp_result.success
        assert cp_result.data["status"] == "completed"
        assert cp_result.data["progress"]["completed"] == 1

        # Step 5: Verify plan.json and plan.mermaid exist on disk
        cog_dir = Path(td) / "cognition"
        assert cog_dir.exists()
        plan_json = cog_dir / "plan.json"
        assert plan_json.exists()
        plan_mermaid = cog_dir / "plan.mermaid"
        assert plan_mermaid.exists()
        mermaid_content = plan_mermaid.read_text(encoding="utf-8")
        assert "graph TD" in mermaid_content
        assert "Titanic" in mermaid_content
        assert ":::completed" in mermaid_content

        # Step 6: Reflect
        refl_result = await registry.resolve("agent_reflect").ainvoke(
            ctx,
            trigger="ls+cat doesn't show types",
            observations="Raw file inspection insufficient",
            adjustment="Prefer pandas approach",
        )
        assert refl_result.success


@pytest.mark.asyncio
async def test_checkpoint_nonexistent_node_returns_error(ctx):
    """agent_checkpoint with bad node_id returns error, not exception."""
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as td:
        await register_cognition_tools(registry, work_root=td)
        # create plan first
        await registry.resolve("agent_plan").ainvoke(
            ctx, goal="Test", steps=[{"title": "S1"}]
        )
        result = await registry.resolve("agent_checkpoint").ainvoke(
            ctx,
            node_id="nonexistent-node-id",
            status="completed",
            summary="Should fail",
        )
        assert not result.success
        assert "nonexistent-node-id" in result.error


@pytest.mark.asyncio
async def test_guard_tool_integration(ctx):
    """agent_guard with registered rules correctly identifies violations."""
    registry = ToolRegistry()
    with tempfile.TemporaryDirectory() as td:
        mgr = await register_cognition_tools(registry, work_root=td)
        from athena.cognition.schemas import GuardRule
        await mgr.register_guard_rules([
            GuardRule(
                rule_id="no-eval-modify",
                description="Do not modify evaluation scripts",
                pattern="eval.py",
                severity="error",
            ),
        ])
        result = await registry.resolve("agent_guard").ainvoke(
            ctx,
            action="I will edit eval.py to add logging",
        )
        assert result.success
        assert not result.data["allowed"]
        assert len(result.data["violations"]) == 1
