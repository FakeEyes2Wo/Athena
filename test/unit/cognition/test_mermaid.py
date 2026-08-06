"""Tests for Mermaid renderer — output format and structure."""

from athena.cognition.schemas import Plan, PlanNode, PlanNodeRelation, PlanNodeStatus
from athena.cognition.mermaid import render_mermaid, _sanitize_id


def test_sanitize_id_replaces_special_chars():
    """_sanitize_id replaces #, ., - with safe alternatives."""
    assert _sanitize_id("plan-1#step.2") == "plan_1nstep_2"


def test_render_flat_plan():
    """A plan with a single root node produces valid Mermaid."""
    root = PlanNode(id="root", title="Do everything", status=PlanNodeStatus.PENDING)
    plan = Plan(plan_id="p1", goal="Test", root=root)
    result = render_mermaid(plan)
    assert "graph TD" in result
    assert '("⬜ Do everything")' in result
    assert ":::pending" in result


def test_render_two_level_tree():
    """A plan with root and children produces nodes + edges."""
    root = PlanNode(
        id="root",
        title="Goal",
        status=PlanNodeStatus.IN_PROGRESS,
        children=[
            PlanNode(id="s1", title="Step 1", status=PlanNodeStatus.COMPLETED),
            PlanNode(id="s2", title="Step 2", status=PlanNodeStatus.PENDING),
        ],
    )
    plan = Plan(plan_id="p1", goal="Two steps", root=root)
    result = render_mermaid(plan)
    # node lines
    assert "root" in result
    assert "s1" in result
    assert "s2" in result
    # edges
    assert "root --> s1" in result
    assert "root --> s2" in result
    # class defs
    assert "classDef completed" in result
    assert "classDef in_progress" in result
    assert "classDef pending" in result


def test_render_alternative_branch():
    """Alternative relation appears as label on edges."""
    root = PlanNode(
        id="root",
        title="Analyze",
        relation=PlanNodeRelation.ALTERNATIVE,
        children=[
            PlanNode(id="a1", title="Approach 1"),
            PlanNode(id="a2", title="Approach 2"),
        ],
    )
    plan = Plan(plan_id="p1", goal="Analyze", root=root)
    result = render_mermaid(plan)
    assert "[alternative]" in result
    assert "root --> a1" in result
    assert "root --> a2" in result


def test_render_all_statuses():
    """All five statuses produce distinct class assignments."""
    root = PlanNode(
        id="root",
        title="All statuses",
        children=[
            PlanNode(id="c", title="Done", status=PlanNodeStatus.COMPLETED),
            PlanNode(id="ip", title="Working", status=PlanNodeStatus.IN_PROGRESS),
            PlanNode(id="p", title="Waiting", status=PlanNodeStatus.PENDING),
            PlanNode(id="f", title="Broken", status=PlanNodeStatus.FAILED),
            PlanNode(id="sk", title="Skipped", status=PlanNodeStatus.SKIPPED),
        ],
    )
    plan = Plan(plan_id="p1", goal="All statuses", root=root)
    result = render_mermaid(plan)
    assert ":::completed" in result
    assert ":::in_progress" in result
    assert ":::pending" in result
    assert ":::failed" in result
    assert ":::skipped" in result


def test_render_deeply_nested():
    """Three-level nesting produces correct edges at each level."""
    root = PlanNode(
        id="root",
        title="Top",
        children=[
            PlanNode(
                id="mid",
                title="Middle",
                children=[
                    PlanNode(id="leaf1", title="Leaf A"),
                    PlanNode(id="leaf2", title="Leaf B"),
                ],
            ),
        ],
    )
    plan = Plan(plan_id="p1", goal="Deep", root=root)
    result = render_mermaid(plan)
    assert "root --> mid" in result
    assert "mid --> leaf1" in result
    assert "mid --> leaf2" in result
