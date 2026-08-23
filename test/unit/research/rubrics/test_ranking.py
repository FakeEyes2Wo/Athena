import pytest

from athena.research.rubrics.models import (
    HypothesisPriorityBatch,
    HypothesisPriorityReview,
    ResourceEstimate,
)
from athena.research.rubrics.ranking import (
    aggregate_hypothesis_priority,
    execution_efficiency,
    validate_hypothesis_priority_batch,
)


def _review(identifier: str, *, penalties: float = 0.0) -> HypothesisPriorityReview:
    return HypothesisPriorityReview(
        hypothesis_id=identifier,
        evidence_testability=0.8,
        scientific_value=0.6,
        resources=ResourceEstimate(
            latency_penalty=penalties,
            compute_penalty=penalties,
            memory_penalty=penalties,
            api_cost_penalty=penalties,
            implementation_penalty=penalties,
            explanation="bounded estimate",
        ),
        validity_risk_control=0.9,
        confidence=0.7,
        explanation="reviewed",
    )


def test_resource_penalties_reduce_execution_efficiency_and_priority() -> None:
    cheap = _review("h1", penalties=0.0)
    expensive = _review("h2", penalties=1.0)

    assert execution_efficiency(cheap.resources) == 1.0
    assert execution_efficiency(expensive.resources) == 0.0
    assert aggregate_hypothesis_priority(cheap) > aggregate_hypothesis_priority(
        expensive
    )


def test_batch_requires_exact_identifiers() -> None:
    batch = HypothesisPriorityBatch(reviews=[_review("h1")])

    with pytest.raises(ValueError, match=r"missing=\['h2'\]"):
        validate_hypothesis_priority_batch(batch, ["h1", "h2"])


def test_batch_rejects_unknown_evidence_refs() -> None:
    ref = "sha256:" + "a" * 64
    batch = HypothesisPriorityBatch(
        reviews=[_review("h1").model_copy(update={"evidence_refs": [ref]})]
    )

    with pytest.raises(ValueError, match="unknown evidence refs"):
        validate_hypothesis_priority_batch(batch, ["h1"], allowed_evidence_refs=set())


def test_batch_rejects_duplicate_identifiers() -> None:
    with pytest.raises(ValueError, match="duplicate IDs"):
        HypothesisPriorityBatch(reviews=[_review("h1"), _review("h1")])
