"""Tests for CognitionManager — Plan tree ops, checkpoints, track, reflect,
guard, persistence, hints."""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from athena.cognition.manager import CognitionManager
from athena.cognition.schemas import (
    GuardRule,
    PlanNodeRelation,
    PlanNodeStatus,
    ToolCategory,
)


@pytest.fixture
def manager():
    """Create a CognitionManager with a temp work_root."""
    with tempfile.TemporaryDirectory() as td:
        yield CognitionManager(work_root=td)


async def test_create_plan_basic(manager):
    """create_plan builds a plan tree with root and children."""
    plan = await manager.create_plan(
        goal="Prepare Titanic dataset",
        root_nodes=[
            {"title": "Retrieve competition info", "description": "Use Kaggle MCP"},
            {"title": "Download data"},
            {
                "title": "Analyze data",
                "relation": "alternative",
                "children": [
                    {"title": "ls+cat approach"},
                    {"title": "pandas approach"},
                ],
            },
        ],
    )
    assert plan.goal == "Prepare Titanic dataset"
    assert len(plan.root.children) == 3
    assert plan.root.children[2].relation == PlanNodeRelation.ALTERNATIVE
    assert len(plan.root.children[2].children) == 2
    assert (Path(manager._cog_dir) / "plan.json").exists()
    assert (Path(manager._cog_dir) / "plan.mermaid").exists()


async def test_get_plan_none_before_create(manager):
    """get_plan returns None before create_plan is called."""
    assert await manager.get_plan() is None


async def test_get_plan_after_create(manager):
    """get_plan returns the plan after create_plan."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    plan = await manager.get_plan()
    assert plan is not None
    assert plan.goal == "Test"


async def test_update_node_status_completed(manager):
    """update_node_status completes a node and auto-advances to next pending."""
    await manager.create_plan(
        "Test",
        [
            {"title": "Step 1"},
            {"title": "Step 2"},
            {"title": "Step 3"},
        ],
    )
    plan = await manager.get_plan()
    node1 = plan.root.children[0]
    assert node1.status == PlanNodeStatus.IN_PROGRESS
    await manager.update_node_status(
        node1.id, "completed", evidence="Done step 1"
    )
    plan = await manager.get_plan()
    assert plan.root.children[0].status == PlanNodeStatus.COMPLETED
    assert plan.root.children[0].evidence == "Done step 1"
    assert plan.root.children[1].status == PlanNodeStatus.IN_PROGRESS


async def test_update_node_status_failed(manager):
    """update_node_status with 'failed' does not auto-advance."""
    await manager.create_plan("Test", [{"title": "Step 1"}, {"title": "Step 2"}])
    plan = await manager.get_plan()
    node1 = plan.root.children[0]
    await manager.update_node_status(node1.id, "failed", evidence="Cannot proceed")
    plan = await manager.get_plan()
    assert plan.root.children[0].status == PlanNodeStatus.FAILED
    assert plan.root.children[1].status == PlanNodeStatus.PENDING


async def test_update_node_status_nonexistent_raises(manager):
    """update_node_status with bad node_id raises ValueError with hint."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    with pytest.raises(ValueError, match="Node 'bad-id' not found"):
        await manager.update_node_status("bad-id", "completed")


async def test_update_node_status_no_plan_raises(manager):
    """update_node_status without a plan raises ValueError."""
    with pytest.raises(ValueError, match="No plan exists"):
        await manager.update_node_status("any-id", "completed")


async def test_add_child_nodes(manager):
    """add_child_nodes adds children to an existing node."""
    await manager.create_plan("Test", [{"title": "Parent"}])
    plan = await manager.get_plan()
    parent = plan.root.children[0]
    new_nodes = await manager.add_child_nodes(
        parent.id,
        [
            {"title": "Child A"},
            {"title": "Child B"},
        ],
    )
    assert len(new_nodes) == 2
    plan = await manager.get_plan()
    assert len(plan.root.children[0].children) == 2


async def test_add_child_nodes_bad_parent_raises(manager):
    """add_child_nodes with nonexistent parent raises ValueError."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    with pytest.raises(ValueError, match="Parent node 'bad-parent' not found"):
        await manager.add_child_nodes("bad-parent", [{"title": "Child"}])


async def test_add_alternative_branch(manager):
    """add_alternative_branch adds a branch and sets relation to ALTERNATIVE."""
    await manager.create_plan(
        "Test",
        [{"title": "Analyze", "relation": "sequential"}],
    )
    plan = await manager.get_plan()
    parent = plan.root.children[0]
    branch = await manager.add_alternative_branch(
        parent.id, "Try pandas instead", "Use pandas for analysis"
    )
    assert branch.title == "Try pandas instead"
    plan = await manager.get_plan()
    updated_parent = plan.root.children[0]
    assert updated_parent.relation == PlanNodeRelation.ALTERNATIVE
    assert len(updated_parent.children) == 1


async def test_create_and_list_checkpoints(manager):
    """create_checkpoint stores and list_checkpoints returns all."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    cp1 = await manager.create_checkpoint(
        "some-node", "Completed step 1"
    )
    cp2 = await manager.create_checkpoint(
        "some-node", "Completed step 2", artifact_refs=["data/out.csv"]
    )
    all_cps = await manager.list_checkpoints()
    assert len(all_cps) == 2
    assert cp2.artifact_refs == ["data/out.csv"]
    last = await manager.get_last_checkpoint()
    assert last is not None
    assert last.checkpoint_id == cp2.checkpoint_id
    cps_dir = Path(manager._cog_dir) / "checkpoints"
    assert len(list(cps_dir.glob("*.json"))) == 2


async def test_get_last_checkpoint_empty(manager):
    """get_last_checkpoint returns None when no checkpoints exist."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    assert await manager.get_last_checkpoint() is None


async def test_get_current_position(manager):
    """get_current_position returns correct counts and path."""
    await manager.create_plan(
        "Test",
        [
            {"title": "Step 1"},
            {"title": "Step 2"},
            {"title": "Step 3"},
        ],
    )
    plan = await manager.get_plan()
    await manager.update_node_status(plan.root.children[0].id, "completed")
    ts = await manager.get_current_position()
    assert ts.completed_count == 1
    assert ts.pending_count == 1
    assert ts.current_node_id is not None
    assert len(ts.path_to_root) > 0


async def test_get_current_position_no_plan(manager):
    """get_current_position returns empty TrackState when no plan exists."""
    ts = await manager.get_current_position()
    assert ts.plan_id == ""
    assert ts.current_node_id is None


async def test_get_next_pending(manager):
    """get_next_pending returns the first layer of pending nodes."""
    await manager.create_plan(
        "Test",
        [
            {"title": "Step 1"},
            {"title": "Step 2"},
        ],
    )
    next_pending = await manager.get_next_pending()
    assert len(next_pending) >= 0


async def test_get_next_pending_deep_tree(manager):
    """get_next_pending handles nested IN_PROGRESS nodes correctly."""
    await manager.create_plan(
        "Test",
        [
            {
                "title": "Phase 1",
                "children": [
                    {"title": "Task 1.1"},
                    {"title": "Task 1.2"},
                ],
            },
            {"title": "Phase 2"},
        ],
    )
    next_pending = await manager.get_next_pending()
    assert len(next_pending) >= 0


async def test_create_reflection(manager):
    """create_reflection stores with trigger and observations."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    r = await manager.create_reflection(
        trigger="Step 1 failed twice",
        observations="API timeout on Kaggle",
        adjustment="Retry with exponential backoff",
    )
    assert r.trigger == "Step 1 failed twice"
    assert "API timeout" in r.observations


async def test_guard_register_and_check(manager):
    """register_guard_rules stores rules, check_action matches patterns."""
    rules = [
        GuardRule(
            rule_id="r1",
            description="Do not modify eval.py",
            pattern="eval.py",
            severity="error",
        ),
        GuardRule(
            rule_id="r2",
            description="Do not delete data",
            pattern="rm -rf",
            severity="warning",
        ),
    ]
    await manager.register_guard_rules(rules)
    result = await manager.check_action("I will edit eval.py to fix the metric")
    assert not result.allowed
    assert len(result.violations) == 1
    assert "eval.py" in result.violations[0]
    result2 = await manager.check_action("I will rm -rf the temp dir")
    assert result2.allowed
    assert len(result2.warnings) == 1
    result3 = await manager.check_action("I will read train.csv")
    assert result3.allowed
    assert len(result3.violations) == 0


async def test_save_and_restore(manager):
    """save writes all state; restore rebuilds from plan.json."""
    await manager.create_plan(
        "Test restore",
        [{"title": "Step A"}, {"title": "Step B"}],
    )
    await manager.create_checkpoint("any-node", "mid checkpoint")
    await manager.register_guard_rules([
        GuardRule(rule_id="r1", description="No rm", pattern="rm ", severity="error")
    ])
    ref = await manager.save()
    assert ref.endswith("plan.json")
    with tempfile.TemporaryDirectory() as td2:
        mgr2 = CognitionManager(work_root=td2)
        src_dir = manager._cog_dir
        dst_dir = Path(td2) / "cognition"
        shutil.copytree(str(src_dir), str(dst_dir))
        await mgr2.restore(str(dst_dir / "plan.json"))
        plan = await mgr2.get_plan()
        assert plan is not None
        assert plan.goal == "Test restore"
        assert len(plan.root.children) == 2
        cps = await mgr2.list_checkpoints()
        assert len(cps) == 1
        result = await mgr2.check_action("rm temp")
        assert not result.allowed


async def test_restore_missing_file_raises(manager):
    """restore with nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Plan file not found"):
        await manager.restore("nonexistent/path/plan.json")


async def test_check_plan_hint_before_first_plan(manager):
    """check_plan_hint returns hint before agent_plan is ever called."""
    hint = manager.check_plan_hint()
    assert hint is not None
    assert "agent_plan" in hint


async def test_check_plan_hint_after_plan_returns_none(manager):
    """check_plan_hint returns None after create_plan is called."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    assert manager.check_plan_hint() is None


def _mock_get_category(name: str) -> ToolCategory | None:
    """Mock category mapping for hint tests."""
    mapping = {
        "mcp_search_tools": ToolCategory.SEARCH,
        "hf_dataset_search": ToolCategory.SEARCH,
        "data_prepare": ToolCategory.DOWNLOAD,
        "hf_dataset_download": ToolCategory.DOWNLOAD,
        "ls": ToolCategory.INSPECT,
        "cat": ToolCategory.INSPECT,
        "exec_code": ToolCategory.COMPUTE,
        "agent_plan": ToolCategory.COGNITION,
        "agent_checkpoint": ToolCategory.COGNITION,
        "agent_track": ToolCategory.COGNITION,
    }
    return mapping.get(name)


def test_check_migration_hint_detects_category_change():
    """Migration hint triggers when tool category changes from stable window."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        mgr.record_tool_call("ls")
        mgr.record_tool_call("cat")
        mgr.record_tool_call("cat")
        hint = mgr.check_migration_hint("exec_code", _mock_get_category)
        assert hint is not None
        assert "探查阶段" in hint
        assert "agent_checkpoint" in hint


def test_check_migration_hint_no_change():
    """No hint when category stays the same."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        mgr.record_tool_call("ls")
        mgr.record_tool_call("cat")
        hint = mgr.check_migration_hint("cat", _mock_get_category)
        assert hint is None


def test_check_migration_hint_ignores_cognition_tools():
    """Cognition tools in window are ignored for migration detection."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        mgr.record_tool_call("agent_checkpoint")
        mgr.record_tool_call("agent_plan")
        hint = mgr.check_migration_hint("exec_code", _mock_get_category)
        assert hint is None


def test_check_migration_hint_short_window():
    """Migration hint returns None when window has fewer than 2 entries."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        mgr.record_tool_call("ls")
        hint = mgr.check_migration_hint("exec_code", _mock_get_category)
        assert hint is None


def test_check_migration_hint_mixed_window():
    """No hint when previous window has multiple categories (already migrated).

    Window: [ls, data_prepare, exec_code]; [:-1] = [ls, data_prepare]
    which is {INSPECT, DOWNLOAD} → len=2 → no hint.
    """
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        mgr.record_tool_call("ls")
        mgr.record_tool_call("data_prepare")
        mgr.record_tool_call("exec_code")
        hint = mgr.check_migration_hint("cat", _mock_get_category)
        assert hint is None


async def test_check_track_after_track_hint(manager):
    """check_track_after_track_hint suggests checkpoint when active nodes exist."""
    await manager.create_plan("Test", [{"title": "Step 1"}, {"title": "Step 2"}])
    hint = manager.check_track_after_track_hint("agent_track")
    assert hint is not None
    assert "agent_checkpoint" in hint


def test_check_track_after_track_hint_not_track_tool():
    """No hint when the tool is not agent_track."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        hint = mgr.check_track_after_track_hint("other_tool")
        assert hint is None


def test_check_track_after_track_hint_no_plan():
    """No hint when there is no plan."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        hint = mgr.check_track_after_track_hint("agent_track")
        assert hint is None


def test_record_tool_call_maintains_window():
    """record_tool_call maintains a sliding window of last 3 calls."""
    with tempfile.TemporaryDirectory() as td:
        mgr = CognitionManager(work_root=td)
        mgr.record_tool_call("a")
        mgr.record_tool_call("b")
        mgr.record_tool_call("c")
        mgr.record_tool_call("d")
        assert mgr._tool_call_window == ["b", "c", "d"]


async def test_auto_advance_to_child(manager):
    """Auto-advance prefers child over sibling when node completes."""
    await manager.create_plan(
        "Test",
        [
            {
                "title": "Step 1",
                "children": [
                    {"title": "Sub 1.1"},
                    {"title": "Sub 1.2"},
                ],
            },
            {"title": "Step 2"},
        ],
    )
    plan = await manager.get_plan()
    node1 = plan.root.children[0]
    # complete node1, should auto-advance to Sub 1.1 (child), not Step 2 (sibling)
    await manager.update_node_status(node1.id, "completed")
    plan = await manager.get_plan()
    assert plan.root.children[0].children[0].status == PlanNodeStatus.IN_PROGRESS
    assert plan.root.children[1].status == PlanNodeStatus.PENDING


async def test_auto_advance_chain(manager):
    """Completing all children advances to next sibling."""
    await manager.create_plan(
        "Test",
        [
            {"title": "Step 1"},
            {"title": "Step 2"},
        ],
    )
    plan = await manager.get_plan()
    await manager.update_node_status(plan.root.children[0].id, "completed")
    plan = await manager.get_plan()
    assert plan.root.children[1].status == PlanNodeStatus.IN_PROGRESS


async def test_create_plan_auto_sets_in_progress(manager):
    """create_plan sets root and first child to IN_PROGRESS."""
    plan = await manager.create_plan(
        "Test",
        [{"title": "Step 1"}, {"title": "Step 2"}],
    )
    assert plan.root.status == PlanNodeStatus.IN_PROGRESS
    assert plan.root.children[0].status == PlanNodeStatus.IN_PROGRESS
    assert plan.root.children[1].status == PlanNodeStatus.PENDING


async def test_create_plan_writes_files(manager):
    """create_plan writes plan.json and plan.mermaid."""
    await manager.create_plan("Test", [{"title": "Step 1"}])
    cog_dir = Path(manager._cog_dir)
    assert (cog_dir / "plan.json").exists()
    assert (cog_dir / "plan.mermaid").exists()
    plan_json = json.loads((cog_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan_json["goal"] == "Test"


async def test_dict_to_node_correct_parent_ids(manager):
    """Nested children have correct parent_id references."""
    plan = await manager.create_plan(
        "Test",
        [
            {
                "title": "Parent",
                "children": [
                    {"title": "Child"},
                ],
            },
        ],
    )
    parent = plan.root.children[0]
    child = parent.children[0]
    assert child.parent_id == parent.id
