"""Behavioral tests for the deterministic hypothesis Selector and dedup."""

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.supervisor.scheduling import EloPolicy
from athena.research.supervisor.scheduling import (
    RankConfig,
    Selector,
    deduplicate,
    jaccard,
    novelty,
    rubric_prior,
    tokenize,
)


def _hypothesis(
    *,
    statement: str,
    intervention: str,
    priority: float = 1000.0,
    order: int | None = None,
    sources: list[str] | None = None,
    cost: float = 0.0,
) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        intervention=intervention,
        expected_effect="improve metric",
        priority=priority,
        order=order,
        sources=sources or [],
        cost=cost,
    )


def test_tokenize_and_jaccard_are_symmetric() -> None:
    assert tokenize("Add L2 weight decay") == {"add", "l2", "weight", "decay"}
    left = tokenize("scale numeric features")
    right = tokenize("scale the numeric features")
    assert jaccard(left, right) > 0.5


def test_rubric_prior_rewards_evidence_and_specificity() -> None:
    tree = ResearchTree()
    weak = _hypothesis(statement="try something", intervention="change")
    strong = _hypothesis(
        statement="scale features",
        intervention="standardize numeric inputs",
        sources=["paper://x"],
    )

    assert rubric_prior(weak, tree) < rubric_prior(strong, tree)


def test_novelty_is_one_without_settled_hypotheses() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(
        _hypothesis(statement="a", intervention="change the model", priority=1000.0)
    )

    assert novelty(tree.pending_hypotheses()[0], tree) == 1.0


def test_novelty_drops_for_a_settled_near_duplicate() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="settled",
            statement="scale numeric features",
            intervention="standardize all inputs",
            expected_effect="improve metric",
            status="REFUTED",
            order=0,
        )
    )
    similar = _hypothesis(
        statement="scale numeric features",
        intervention="standardize all inputs",
    )

    assert novelty(similar, tree) < 0.5


def test_deduplicate_removes_existing_and_in_batch_duplicates() -> None:
    existing = [
        _hypothesis(statement="add interaction", intervention="multiply age income")
    ]
    candidates = [
        _hypothesis(statement="add interaction", intervention="multiply age income"),
        _hypothesis(statement="use gradient boosting", intervention="swap to xgboost"),
        _hypothesis(statement="use gradient boosting", intervention="swap to xgboost"),
    ]

    kept = deduplicate(candidates, existing, threshold=0.8)

    assert [item.statement for item in kept] == ["use gradient boosting"]


def test_selector_ranks_evidence_higher_then_stable_tiebreak() -> None:
    tree = ResearchTree()
    plain = _hypothesis(
        statement="tune learning rate", intervention="lower the learning rate", order=1
    )
    evidenced = _hypothesis(
        statement="add feature scaling",
        intervention="standardize numeric inputs",
        sources=["paper://scaling"],
        order=0,
    )
    tree.add_hypothesis(plain)
    tree.add_hypothesis(evidenced)

    ranked = Selector(EloPolicy()).rank(tree, tree.pending_hypotheses())

    assert [item.statement for item in ranked] == [
        "add feature scaling",
        "tune learning rate",
    ]


def test_selector_applies_cost_penalty() -> None:
    tree = ResearchTree()
    cheap = _hypothesis(
        statement="cheap change", intervention="tune one hyperparameter", cost=0.0
    )
    expensive = _hypothesis(
        statement="expensive change", intervention="train a giant model", cost=1.0
    )
    tree.add_hypothesis(cheap)
    tree.add_hypothesis(expensive)

    ranked = Selector(EloPolicy()).rank(tree, tree.pending_hypotheses())

    assert ranked[0].statement == "cheap change"


def test_selector_config_is_reusable_and_deterministic() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(
        _hypothesis(
            statement="a", intervention="standardize numeric inputs", priority=1016.0
        )
    )
    tree.add_hypothesis(
        _hypothesis(
            statement="b", intervention="standardize numeric inputs", priority=1000.0
        )
    )
    selector = Selector(EloPolicy(), RankConfig(strength_weight=1.0))

    first = selector.rank(tree, tree.pending_hypotheses())
    second = selector.rank(tree, tree.pending_hypotheses())

    assert first[0].statement == "a"
    assert [h.id for h in first] == [h.id for h in second]
