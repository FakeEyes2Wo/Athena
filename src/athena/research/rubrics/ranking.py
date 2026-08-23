"""Four-dimension aggregation and exact-ID validation for Layer 2."""

import json

from athena.research.rubrics.models import (
    HypothesisPriorityBatch,
    HypothesisPriorityContext,
    HypothesisPriorityReview,
    ResourceEstimate,
)

HYPOTHESIS_PRIORITY_WEIGHTS: dict[str, float] = {
    "evidence_testability": 0.30,
    "scientific_value": 0.30,
    "execution_efficiency": 0.20,
    "validity_risk_control": 0.20,
}

RESOURCE_PENALTY_WEIGHTS: dict[str, float] = {
    "latency_penalty": 0.30,
    "compute_penalty": 0.25,
    "memory_penalty": 0.15,
    "api_cost_penalty": 0.15,
    "implementation_penalty": 0.15,
}


def execution_efficiency(resources: ResourceEstimate) -> float:
    """Convert resource penalties into one positive efficiency score."""
    penalty = sum(
        RESOURCE_PENALTY_WEIGHTS[name] * getattr(resources, name)
        for name in RESOURCE_PENALTY_WEIGHTS
    )
    return 1.0 - min(max(penalty, 0.0), 1.0)


def aggregate_hypothesis_priority(review: HypothesisPriorityReview) -> float:
    """Aggregate exactly four top-level dimensions into a score in [0, 1]."""
    values = {
        "evidence_testability": review.evidence_testability,
        "scientific_value": review.scientific_value,
        "execution_efficiency": execution_efficiency(review.resources),
        "validity_risk_control": review.validity_risk_control,
    }
    score = sum(HYPOTHESIS_PRIORITY_WEIGHTS[name] * values[name] for name in values)
    return min(max(score, 0.0), 1.0)


def validate_hypothesis_priority_batch(
    batch: HypothesisPriorityBatch,
    expected_ids: list[str],
    *,
    allowed_evidence_refs: set[str] | None = None,
) -> dict[str, HypothesisPriorityReview]:
    """Require one validated review for every requested hypothesis ID."""
    if len(expected_ids) != len(set(expected_ids)):
        raise ValueError("requested hypothesis IDs must be unique")
    reviews = {review.hypothesis_id: review for review in batch.reviews}
    expected, actual = set(expected_ids), set(reviews)
    missing, unexpected = sorted(expected - actual), sorted(actual - expected)
    if missing or unexpected:
        raise ValueError(
            f"hypothesis priority ID mismatch: missing={missing}, "
            f"unexpected={unexpected}"
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
                f"hypothesis priority contains unknown evidence refs: {unknown_refs}"
            )
    return reviews


def build_hypothesis_priority_prompt(context: HypothesisPriorityContext) -> str:
    """Serialize one bounded post-Gate batch review request."""
    payload = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return (
        "Review every post-Gate hypothesis in one batch. This is prioritization, "
        "not a second eligibility gate and not a prediction of final performance. "
        "Score Evidence & Testability, Scientific Value, and Validity & Risk "
        "Control in [0,1]. Provide normalized resource penalties for latency, "
        "compute, memory, API cost, and implementation effort, where 1 means "
        "most expensive. Reuse supplied Gate evidence instead of re-running its "
        "reasoning. Return exactly one review for every input hypothesis_id and "
        "no other IDs. Never invent evidence refs.\n\nResearch context:\n"
        f"{payload}"
    )


__all__ = [
    "HYPOTHESIS_PRIORITY_WEIGHTS",
    "RESOURCE_PENALTY_WEIGHTS",
    "aggregate_hypothesis_priority",
    "build_hypothesis_priority_prompt",
    "execution_efficiency",
    "validate_hypothesis_priority_batch",
]
