"""Clarification generator policy tests."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from athena.research.clarification.generator import (
    ClarificationFinalStep,
    ClarificationModelOutput,
    ClarificationQuestionStep,
    DeterministicClarificationGenerator,
    PublicProgress,
    generate_turn,
    publish_public_progress,
)
from athena.research.clarification.state import new_draft


@pytest.mark.asyncio
async def test_deterministic_generator_asks_first_missing_requirement() -> None:
    draft = new_draft(
        "predict churn", "s-1", "draft-1", datetime(2026, 9, 1, tzinfo=UTC)
    )

    step = DeterministicClarificationGenerator().next_step(draft)

    assert isinstance(step, ClarificationQuestionStep)
    assert step.field == "dataset"


def test_public_progress_normalizes_only_public_text() -> None:
    value = PublicProgress(
        stage="analysis", summary="  A\u200b\x00   metric\nselected  "
    )
    assert value.summary == "A metric selected"


@pytest.mark.parametrize("summary", ["\u200b\x00", "x" * 601])
def test_public_progress_rejects_empty_or_over_limit(summary: str) -> None:
    with pytest.raises(ValidationError):
        PublicProgress(stage="analysis", summary=summary)


def test_public_progress_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PublicProgress.model_validate(
            {"stage": "analysis", "summary": "safe", "reasoning": "private"}
        )


def test_public_progress_reports_non_string_input_as_validation_error() -> None:
    with pytest.raises(ValidationError):
        PublicProgress.model_validate({"stage": "analysis", "summary": 1})


def test_clarification_model_output_accepts_both_step_variants_and_is_strict() -> None:
    question = {
        "kind": "question",
        "field": "dataset",
        "prompt": "Which dataset?",
        "choices": [
            {"label": "Provided", "value": "provided"},
            {"label": "Later", "value": "later"},
        ],
        "allow_custom": True,
        "allow_skip": True,
    }
    final = {
        "kind": "final",
        "understanding": {
            "title": "Task",
            "task_type": "other",
        },
        "unresolved": [],
    }
    update = {"stage": "analysis", "summary": "safe"}
    assert isinstance(
        ClarificationModelOutput.model_validate(
            {"public_update": update, "step": question}
        ).step,
        ClarificationQuestionStep,
    )
    assert isinstance(
        ClarificationModelOutput.model_validate(
            {"public_update": update, "step": final}
        ).step,
        ClarificationFinalStep,
    )
    with pytest.raises(ValidationError):
        ClarificationModelOutput.model_validate(
            {"public_update": update, "step": {"kind": "unknown"}}
        )


@pytest.mark.asyncio
async def test_generate_turn_normalizes_generator_outputs() -> None:
    draft = new_draft(
        "predict churn", "s-1", "draft-1", datetime(2026, 9, 1, tzinfo=UTC)
    )
    deterministic = await generate_turn(DeterministicClarificationGenerator(), draft)
    assert deterministic.public_update is None
    assert isinstance(deterministic.step, ClarificationQuestionStep)

    output = ClarificationModelOutput(
        public_update=PublicProgress(stage="question", summary="Need dataset"),
        step=deterministic.step,
    )

    class ModelGenerator:
        def next_step(self, _draft):
            return output

    wrapped = await generate_turn(ModelGenerator(), draft)
    assert wrapped.public_update == output.public_update
    assert wrapped.step == output.step


@pytest.mark.asyncio
async def test_publish_public_progress_isolates_ordinary_sink_failure() -> None:
    calls: list[str] = []

    async def failing_sink(**_kwargs: object) -> None:
        raise RuntimeError("display down")

    async def recording_sink(**kwargs: object) -> None:
        calls.append(str(kwargs["summary"]))

    await publish_public_progress(
        failing_sink,
        summary="safe",
        stage="analysis",
        session_id="s-1",
        scope_id="d-1",
        source="agent",
        persist=True,
    )
    await publish_public_progress(
        recording_sink,
        summary="still safe",
        stage="analysis",
        session_id="s-1",
        scope_id="d-1",
        source="agent",
        persist=True,
    )
    assert calls == ["still safe"]


@pytest.mark.asyncio
async def test_publish_public_progress_propagates_cancellation() -> None:
    async def cancelled_sink(**_kwargs: object) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await publish_public_progress(
            cancelled_sink,
            summary="safe",
            stage="analysis",
            session_id="s-1",
            scope_id="d-1",
            source="agent",
            persist=False,
        )
