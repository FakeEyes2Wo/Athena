"""Clarification generator policy tests."""

from datetime import UTC, datetime

import pytest

from athena.research.clarification.generator import (
    ClarificationQuestionStep,
    DeterministicClarificationGenerator,
    generate_step,
)
from athena.research.clarification.state import new_draft


@pytest.mark.asyncio
async def test_deterministic_generator_asks_first_missing_requirement() -> None:
    draft = new_draft(
        "predict churn", "s-1", "draft-1", datetime(2026, 9, 1, tzinfo=UTC)
    )

    step = await generate_step(DeterministicClarificationGenerator(), draft)

    assert isinstance(step, ClarificationQuestionStep)
    assert step.field == "dataset"
