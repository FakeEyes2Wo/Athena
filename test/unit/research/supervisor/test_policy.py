"""Behavioral contract for hypothesis scheduling policy."""

import math

import pytest

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.scheduling import (
    EloPolicy,
    HypothesisPolicy,
    Outcome,
    queue_order,
)


def _hypothesis(*, priority: float, order: int = 0) -> Hypothesis:
    return Hypothesis(
        statement="claim",
        intervention="change",
        expected_effect="improve",
        priority=priority,
        order=order,
    )


def test_single_sided_elo_settlement_uses_fixed_reference_expectation() -> None:
    policy = EloPolicy(k=32)

    assert policy.settle(1048.0, Outcome.WIN) == 1064.0
    assert policy.settle(1048.0, Outcome.DRAW) == 1048.0
    assert policy.settle(1048.0, Outcome.LOSS) == 1032.0


def test_root_and_child_seed_use_the_settled_parent_priority() -> None:
    policy = EloPolicy()

    assert policy.seed(None) == 1000.0
    assert policy.seed(_hypothesis(priority=1016.0)) == 1016.0


def test_root_priority_is_fixed_and_cannot_be_overridden() -> None:
    with pytest.raises(TypeError):
        EloPolicy(root_priority=900.0)


def test_policy_priority_reads_hypothesis_priority() -> None:
    policy: HypothesisPolicy = EloPolicy()
    tree = ResearchTree()
    tree.add_hypothesis(_hypothesis(priority=1032.0))

    assert policy.priority(tree.pending_hypotheses()[0], tree=tree) == 1032.0


@pytest.mark.parametrize("k", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_elo_k_must_be_positive_and_finite(k: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        EloPolicy(k=k)


@pytest.mark.parametrize("reference", [math.nan, math.inf, -math.inf])
def test_elo_reference_priority_must_be_finite(reference: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        EloPolicy().settle(reference, Outcome.WIN)


@pytest.mark.parametrize("priority", [math.nan, math.inf, -math.inf])
def test_queue_order_rejects_nonfinite_priority(priority: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        queue_order(priority, 0)


def test_queue_order_is_priority_descending_then_fifo_order_ascending() -> None:
    hypotheses = [
        _hypothesis(priority=1000.0, order=2),
        _hypothesis(priority=1016.0, order=3),
        _hypothesis(priority=1000.0, order=1),
    ]

    ordered = sorted(
        hypotheses,
        key=lambda hypothesis: queue_order(hypothesis.priority, hypothesis.order),
    )

    assert [(item.priority, item.order) for item in ordered] == [
        (1016.0, 3),
        (1000.0, 1),
        (1000.0, 2),
    ]
