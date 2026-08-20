"""Transparent aggregation and exact-ID validation for ranking rubrics."""

import json

from athena.research.rubrics.models import (
    HypothesisRankingBatch,
    HypothesisRankingContext,
    HypothesisRubric,
)

HYPOTHESIS_RUBRIC_BASE = 0.20
HYPOTHESIS_RUBRIC_WEIGHTS: dict[str, float] = {
    "verifiability": 0.25,
    "historical_difference": 0.20,
    "eda_evidence": 0.20,
    "feasibility": 0.15,
    "cost_penalty": -0.10,
    "leakage_risk": -0.10,
}

NLP_RISK_CHECKLIST: tuple[str, ...] = (
    "class imbalance",
    "exact duplicates",
    "near duplicates",
    "train/test text overlap",
    "target or label leakage",
    "prompt-response leakage",
    "benchmark or pretraining contamination",
    "tokenization mismatch",
    "sequence truncation or long-context information loss",
    "LLM-as-a-Judge reproducibility",
    "judge prompt stability and stochasticity",
    "evaluation leakage",
)


def aggregate_hypothesis_rubric(review: HypothesisRubric) -> float:
    """Aggregate one validated review and clamp the transparent score to [0,1]."""
    score = HYPOTHESIS_RUBRIC_BASE + sum(
        HYPOTHESIS_RUBRIC_WEIGHTS[name] * getattr(review, name)
        for name in HYPOTHESIS_RUBRIC_WEIGHTS
    )
    return min(max(score, 0.0), 1.0)


def validate_hypothesis_rubric_batch(
    batch: HypothesisRankingBatch,
    expected_ids: list[str],
    *,
    allowed_evidence_refs: set[str] | None = None,
) -> dict[str, HypothesisRubric]:
    """Require one and only one review for every requested hypothesis ID."""
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("requested hypothesis IDs must be unique")
    reviews = {review.hypothesis_id: review for review in batch.reviews}
    expected = set(expected_ids)
    actual = set(reviews)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise ValueError(
            f"hypothesis rubric ID mismatch: missing={missing}, unexpected={unexpected}"
        )
    if allowed_evidence_refs is not None:
        unknown_refs = sorted(
            {
                ref
                for review in batch.reviews
                for ref in review.evidence_refs
                if ref not in allowed_evidence_refs
            }
        )
        if unknown_refs:
            raise ValueError(
                f"hypothesis rubric contains unknown evidence refs: {unknown_refs}"
            )
    return reviews


def build_hypothesis_ranking_prompt(context: HypothesisRankingContext) -> str:
    """Serialize bounded context plus an applicability-aware NLP risk checklist."""
    risks = "\n".join(f"- {item}" for item in NLP_RISK_CHECKLIST)
    payload = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return (
        "Review every eligible hypothesis in the supplied batch. This is post-Gate "
        "budget prioritization, not a second eligibility gate and not a prediction "
        "of experimental performance. Use the frozen evaluation policy, EDA, "
        "baseline/SOTA, history, evidence, cost, and environment context. Return one "
        "review for every input hypothesis_id and no other IDs.\n\n"
        "For NLP/LLM tasks, consider only risks that are relevant and evidenced; do "
        "not penalize a candidate merely because a checklist item exists:\n"
        f"{risks}\n\nResearch context:\n{payload}"
    )


__all__ = [
    "HYPOTHESIS_RUBRIC_BASE",
    "HYPOTHESIS_RUBRIC_WEIGHTS",
    "NLP_RISK_CHECKLIST",
    "aggregate_hypothesis_rubric",
    "build_hypothesis_ranking_prompt",
    "validate_hypothesis_rubric_batch",
]
