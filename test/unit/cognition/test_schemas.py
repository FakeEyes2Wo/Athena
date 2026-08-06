"""Tests for cognition schemas — serialization, defaults, nested structures."""

from athena.cognition.schemas import (
    Checkpoint,
    GuardResult,
    GuardRule,
    Plan,
    PlanNode,
    PlanNodeRelation,
    PlanNodeStatus,
    Reflection,
    ToolCategory,
    TrackState,
)


def test_plan_node_defaults():
    """PlanNode has sensible defaults for all optional fields."""
    node = PlanNode(id="n1", title="Test step")
    assert node.parent_id is None
    assert node.description == ""
    assert node.status == PlanNodeStatus.PENDING
    assert node.relation == PlanNodeRelation.SEQUENTIAL
    assert node.children == []
    assert node.checkpoint_ref is None
    assert node.evidence == ""
    assert node.created_at is not None
    assert node.updated_at is not None


def test_plan_node_serialization_roundtrip():
    """PlanNode with children survives JSON roundtrip."""
    node = PlanNode(
        id="root",
        title="Root goal",
        relation=PlanNodeRelation.SEQUENTIAL,
        children=[
            PlanNode(id="child1", title="Step 1"),
            PlanNode(
                id="child2",
                title="Step 2",
                relation=PlanNodeRelation.ALTERNATIVE,
                children=[
                    PlanNode(id="child2a", title="Approach A"),
                    PlanNode(id="child2b", title="Approach B"),
                ],
            ),
        ],
    )
    dumped = node.model_dump_json()
    loaded = PlanNode.model_validate_json(dumped)
    assert loaded.id == "root"
    assert loaded.relation == PlanNodeRelation.SEQUENTIAL
    assert len(loaded.children) == 2
    assert loaded.children[1].relation == PlanNodeRelation.ALTERNATIVE
    assert len(loaded.children[1].children) == 2


def test_plan_with_root():
    """Plan wraps a root PlanNode with metadata."""
    root = PlanNode(id="root", title="Complete task")
    plan = Plan(plan_id="p1", session_id="s1", goal="Finish the task", root=root)
    dumped = plan.model_dump_json()
    loaded = Plan.model_validate_json(dumped)
    assert loaded.plan_id == "p1"
    assert loaded.session_id == "s1"
    assert loaded.goal == "Finish the task"
    assert loaded.root.id == "root"


def test_checkpoint_creation():
    """Checkpoint stores milestone summary with artifact refs."""
    cp = Checkpoint(
        checkpoint_id="cp1",
        plan_id="p1",
        node_id="n1",
        summary="Found train.csv has 891 rows",
        artifact_refs=["data/train.csv"],
    )
    assert cp.checkpoint_id == "cp1"
    assert cp.node_id == "n1"
    assert "891 rows" in cp.summary
    assert cp.artifact_refs == ["data/train.csv"]
    assert cp.created_at is not None


def test_reflection_with_changes():
    """Reflection records trigger, observations, and optional plan changes."""
    r = Reflection(
        reflection_id="r1",
        plan_id="p1",
        trigger="Step 3a failed twice",
        observations="ls+cat cannot parse CSV properly",
        adjustment="Switch to pandas approach",
        plan_changes=[{"action": "add_alternative", "node_id": "#3", "branch": "#3b"}],
    )
    assert r.trigger == "Step 3a failed twice"
    assert len(r.plan_changes) == 1
    assert r.adjustment != ""


def test_guard_rule_and_result():
    """GuardRule defines pattern; GuardResult reports violations."""
    rule = GuardRule(
        rule_id="no-eval-modify",
        description="Do not modify evaluation scripts",
        pattern="eval\\.py",
        severity="error",
    )
    result = GuardResult(
        allowed=False,
        violations=["Attempted to modify eval.py"],
        warnings=[],
    )
    assert not result.allowed
    assert len(result.violations) == 1
    assert rule.severity == "error"


def test_track_state_counts():
    """TrackState provides progress statistics."""
    ts = TrackState(
        plan_id="p1",
        current_node_id="n3b",
        active_nodes=["n3b"],
        completed_count=3,
        failed_count=0,
        pending_count=5,
        path_to_root=["n3b", "n3", "root"],
    )
    assert ts.completed_count == 3
    assert ts.pending_count == 5
    assert len(ts.path_to_root) == 3


def test_tool_category_enum_values():
    """ToolCategory enum has the expected five working labels plus cognition."""
    assert ToolCategory.SEARCH.value == "search"
    assert ToolCategory.DOWNLOAD.value == "download"
    assert ToolCategory.INSPECT.value == "inspect"
    assert ToolCategory.COMPUTE.value == "compute"
    assert ToolCategory.ARTIFACT.value == "artifact"
    assert ToolCategory.COGNITION.value == "cognition"
