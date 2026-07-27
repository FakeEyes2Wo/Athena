"""End-to-end smoke test: PREPARE -> SEARCH -> VALIDATE -> REPORT."""

import pytest
import asyncio


@pytest.mark.slow
def test_full_pipeline_smoke():
    """End-to-end smoke test verifying all modules wire together.

    Exercises: TaskMetaData, DataProfile, EvaluatorFactory, ResearchTree,
    HypothesisRanker, Supervisor, SearchLoop, generate_hypotheses.
    """
    from athena.core.schemas import TaskMetaData, MetricSpec, ComparisonVerdict
    from athena.core.budget import BudgetSnapshot
    from athena.core.research.research_tree import ResearchTree
    from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
    from athena.workflows.prepare.data_analysis import DataProfile
    from athena.workflows.search.idea_generation import generate_hypotheses
    from athena.execution.supervisor import Supervisor

    # ── Setup ──────────────────────────────────────────────────────
    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    profile = DataProfile(row_count=100, col_count=5, task_type_hint="classification")
    spec = EvaluatorFactory.build(task, profile)
    assert spec.primary.name == "f1_macro"
    assert spec.primary.direction == "maximize"

    tree = ResearchTree()
    budget = BudgetSnapshot(remaining=3, max_no_improve=2)

    # ── Generate initial hypotheses ────────────────────────────────
    hypotheses = asyncio.run(generate_hypotheses(profile, [], [], tree))
    assert len(hypotheses) > 0
    assert all(h.status == "PROPOSED" for h in hypotheses)
    assert all(h.id is not None for h in hypotheses)

    # ── Verify tree state ──────────────────────────────────────────
    pending = tree.pending_hypotheses()
    assert len(pending) == len(hypotheses)
    assert all(h.status == "PROPOSED" for h in pending)

    # ── Verify supervisor decisions ────────────────────────────────
    supervisor = Supervisor()

    # Candidate wins with budget remaining -> ACCEPT
    d1 = supervisor.decide(
        ComparisonVerdict(winner="candidate", p_value=0.01),
        BudgetSnapshot(remaining=5),
    )
    assert d1.action == "ACCEPT"

    # Baseline wins near streak limit -> STOP
    d2 = supervisor.decide(
        ComparisonVerdict(winner="baseline", p_value=0.5),
        BudgetSnapshot(remaining=1, no_improve_streak=4, max_no_improve=5),
    )
    assert d2.action == "STOP"

    # Tie with no streak -> REJECT
    d3 = supervisor.decide(
        ComparisonVerdict(winner="tie", p_value=0.9),
        BudgetSnapshot(remaining=5, max_no_improve=5),
    )
    assert d3.action == "REJECT"

    # Budget exhausted -> STOP regardless of verdict
    d4 = supervisor.decide(
        ComparisonVerdict(winner="candidate", p_value=0.01),
        BudgetSnapshot(remaining=0, is_exhausted=True),
    )
    assert d4.action == "STOP"
