import pytest

from athena.core.research_models import TaskUnderstanding
from athena.research.rubrics.evaluation import (
    RubricGenerationError,
    assemble_evaluation_policy,
    generate_evaluation_policy,
)
from athena.research.rubrics.models import (
    EvaluationRubricDraft,
    ResearchEvaluationContext,
)


def _understanding() -> TaskUnderstanding:
    return TaskUnderstanding(
        title="diagnosis",
        dataset="medical.csv",
        target="diagnosis",
        task_type="classification",
        evaluation_plan="hold-out evaluation",
        readiness="READY",
        confidence=0.9,
    )


def _context(**updates) -> ResearchEvaluationContext:
    values = {
        "research_task": "predict diagnosis",
        "task_understanding": _understanding(),
    }
    values.update(updates)
    return ResearchEvaluationContext(**values)


def _draft(metric: str = "balanced_accuracy") -> EvaluationRubricDraft:
    return EvaluationRubricDraft(
        primary_metric=metric,
        direction="maximize",
        confidence=0.8,
        explanation="appropriate for this classification task",
    )


def test_human_metric_has_highest_precedence() -> None:
    context = _context(
        human_primary_metric="f1",
        human_direction="maximize",
        official_primary_metric="roc_auc",
        official_direction="maximize",
    )

    policy = assemble_evaluation_policy(context, _draft())

    assert policy.primary_metric == "f1"
    assert policy.metric_source == "human"
    assert policy.locked is True


def test_ai_selects_only_without_locked_source() -> None:
    policy = assemble_evaluation_policy(_context(), _draft())

    assert policy.primary_metric == "balanced_accuracy"
    assert policy.metric_source == "ai"
    assert policy.locked is False


def test_unsupported_metric_never_falls_back_to_accuracy() -> None:
    with pytest.raises(ValueError, match="unsupported primary metric"):
        assemble_evaluation_policy(_context(), _draft("imaginary_score"))


@pytest.mark.asyncio
async def test_locked_metric_survives_ai_failure() -> None:
    async def broken_provider(_context, _correction):
        raise RuntimeError("provider unavailable")

    policy = await generate_evaluation_policy(
        _context(official_primary_metric="roc_auc", official_direction="maximize"),
        broken_provider,
    )

    assert policy.primary_metric == "roc_auc"
    assert policy.metric_source == "official"
    assert policy.confidence == 0.0


@pytest.mark.asyncio
async def test_unresolved_metric_fails_closed_when_ai_fails() -> None:
    async def broken_provider(_context, _correction):
        raise RuntimeError("provider unavailable")

    with pytest.raises(RubricGenerationError):
        await generate_evaluation_policy(_context(), broken_provider)
