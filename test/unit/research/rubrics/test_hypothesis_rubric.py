"""Hypothesis Ranking Rubric schema, aggregation, ID, prompt, and fallback tests."""

import pytest
from pydantic import ValidationError

from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.rubrics.models import (
    EvaluationPolicy,
    HypothesisRankingBatch,
    HypothesisRankingCandidate,
    HypothesisRankingContext,
    HypothesisRubric,
)
from athena.research.rubrics.ranking import (
    aggregate_hypothesis_rubric,
    build_hypothesis_ranking_prompt,
    validate_hypothesis_rubric_batch,
)
from athena.research.supervisor.policy import EloPolicy
from athena.research.supervisor.ranker import Selector, rubric_prior


def _review(**updates) -> HypothesisRubric:
    payload = {
        "hypothesis_id": "hyp_1",
        "verifiability": 0.8,
        "historical_difference": 0.7,
        "eda_evidence": 0.6,
        "feasibility": 0.9,
        "cost_penalty": 0.2,
        "leakage_risk": 0.1,
        "confidence": 0.75,
        "explanation": "The intervention is testable and supported by EDA.",
        "dimension_reasons": {"eda_evidence": "EDA shows a relevant failure mode."},
    }
    payload.update(updates)
    return HypothesisRubric(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verifiability", -0.01),
        ("historical_difference", 1.01),
        ("leakage_risk", 1.2),
        ("confidence", -0.1),
    ],
)
def test_dimension_and_confidence_ranges_are_strict(field, value) -> None:
    with pytest.raises(ValidationError):
        _review(**{field: value})


def test_explanation_is_required() -> None:
    with pytest.raises(ValidationError, match="explanation"):
        _review(explanation="")


def test_aggregation_clamps_upper_and_lower_bounds() -> None:
    upper = _review(
        verifiability=1.0,
        historical_difference=1.0,
        eda_evidence=1.0,
        feasibility=1.0,
        cost_penalty=0.0,
        leakage_risk=0.0,
    )
    lower = _review(
        verifiability=0.0,
        historical_difference=0.0,
        eda_evidence=0.0,
        feasibility=0.0,
        cost_penalty=1.0,
        leakage_risk=1.0,
    )

    assert aggregate_hypothesis_rubric(upper) == 1.0
    assert aggregate_hypothesis_rubric(lower) == 0.0


def test_cost_and_leakage_penalties_reduce_score() -> None:
    safe = _review(cost_penalty=0.0, leakage_risk=0.0)
    costly = _review(cost_penalty=1.0, leakage_risk=0.0)
    leaky = _review(cost_penalty=0.0, leakage_risk=1.0)

    assert aggregate_hypothesis_rubric(costly) < aggregate_hypothesis_rubric(safe)
    assert aggregate_hypothesis_rubric(leaky) < aggregate_hypothesis_rubric(safe)


def test_batch_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate IDs"):
        HypothesisRankingBatch(reviews=[_review(), _review()])


@pytest.mark.parametrize(
    ("reviews", "match"),
    [
        ([_review()], "missing"),
        ([_review(), _review(hypothesis_id="unknown")], "unexpected"),
    ],
)
def test_batch_requires_exact_requested_ids(reviews, match) -> None:
    batch = HypothesisRankingBatch(reviews=reviews)
    with pytest.raises(ValueError, match=match):
        validate_hypothesis_rubric_batch(batch, ["hyp_1", "hyp_2"])


def test_batch_rejects_unknown_evidence_refs() -> None:
    batch = HypothesisRankingBatch(
        reviews=[_review(evidence_refs=["artifact:invented"])]
    )

    with pytest.raises(ValueError, match="unknown evidence refs"):
        validate_hypothesis_rubric_batch(
            batch,
            ["hyp_1"],
            allowed_evidence_refs={"artifact:known"},
        )


def test_prompt_contains_layer_one_policy_and_nlp_risk_context() -> None:
    context = HypothesisRankingContext(
        research_task="Classify support messages into intent labels",
        evaluation_policy=EvaluationPolicy(
            primary_metric="macro_f1",
            direction="maximize",
            metric_source="ai",
            locked=False,
            confidence=0.8,
            explanation="Treat intents evenly.",
        ),
        hypotheses=[
            HypothesisRankingCandidate(
                hypothesis_id="hyp_1",
                statement="Truncation loses intent cues",
                intervention="Increase sequence length after measuring truncation",
                expected_effect="Improve macro F1 on long messages",
            )
        ],
    )

    prompt = build_hypothesis_ranking_prompt(context)

    assert '"primary_metric": "macro_f1"' in prompt
    assert "train/test text overlap" in prompt
    assert "prompt-response leakage" in prompt
    assert "LLM-as-a-Judge reproducibility" in prompt
    assert "do not penalize" in prompt


def test_selector_prefers_ai_rubric_score_when_available() -> None:
    tree = ResearchTree()
    high_fallback = Hypothesis(
        statement="evidenced",
        intervention="standardize numeric inputs",
        expected_effect="improve metric",
        sources=["paper://one"],
        rubric_score=0.1,
    )
    low_fallback = Hypothesis(
        statement="plain",
        intervention="change",
        expected_effect="improve metric",
        rubric_score=0.9,
    )
    tree.add_hypothesis(high_fallback)
    tree.add_hypothesis(low_fallback)

    ranked = Selector(EloPolicy()).rank(tree, tree.pending_hypotheses())

    assert ranked[0].statement == "plain"


def test_selector_falls_back_to_existing_deterministic_prior() -> None:
    tree = ResearchTree()
    weak = Hypothesis(
        statement="weak",
        intervention="change",
        expected_effect="improve metric",
    )
    strong = Hypothesis(
        statement="strong",
        intervention="standardize numeric inputs",
        expected_effect="improve metric",
        sources=["paper://one"],
    )
    tree.add_hypothesis(weak)
    tree.add_hypothesis(strong)

    assert rubric_prior(weak, tree) < rubric_prior(strong, tree)
    ranked = Selector(EloPolicy()).rank(tree, tree.pending_hypotheses())
    assert ranked[0].statement == "strong"
