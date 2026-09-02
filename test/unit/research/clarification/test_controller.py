"""Deterministic controller tests."""

from datetime import UTC, datetime

import pytest

from athena.core.human_request import HumanChoice, HumanOutcome, HumanRequest
from athena.research.clarification.controller import (
    ClarificationController,
    ClarificationControllerError,
    ClarificationFinalStep,
    ClarificationQuestionStep,
)
from athena.research.clarification.generator import (
    DeterministicClarificationGenerator,
)
from athena.research.clarification.models import (
    ClarificationAnswer,
    ClarificationDraft,
    DraftUnderstanding,
    UnresolvedItem,
)
from athena.research.clarification.state import new_draft, set_pending
from athena.research.clarification.store import ClarificationStore


class AutoBroker:
    def __init__(self) -> None:
        self.requests: list[HumanRequest] = []
        self.cancelled: list[tuple[str, str]] = []

    async def ask(self, request: HumanRequest) -> HumanOutcome:
        self.requests.append(request)
        return HumanOutcome(
            request_id=request.request_id,
            kind="choice",
            value=request.choices[0].value,
            choice_label=request.choices[0].label,
            settled_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        )

    async def cancel_scope(self, session_id: str, scope_id: str) -> list[HumanOutcome]:
        self.cancelled.append((session_id, scope_id))
        return []


class AlwaysQuestionGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, draft: ClarificationDraft):
        self.calls += 1
        if draft.questions_asked >= 8:
            return ClarificationFinalStep(
                kind="final",
                understanding=DraftUnderstanding(
                    title="Predict churn",
                    task_type="classification",
                    primary_metric="f1",
                    direction="maximize",
                ),
                unresolved=[
                    UnresolvedItem(field="target", reason="not supplied", critical=True)
                ],
            )
        return ClarificationQuestionStep(
            kind="question",
            field="dataset",
            prompt="Which dataset?",
            choices=[
                HumanChoice(label="A", value="a"),
                HumanChoice(label="B", value="b"),
            ],
            allow_custom=True,
            allow_skip=True,
        )


def _controller(tmp_path, generator=None, *, session="s-1"):
    store = ClarificationStore(tmp_path)
    broker = AutoBroker()
    controller = ClarificationController(
        store,
        broker,
        generator or AlwaysQuestionGenerator(),
        session_id=session,
        clock=lambda: datetime(2026, 9, 1, 10, tzinfo=UTC),
        id_factory=lambda prefix: f"{prefix}-1",
    )
    return store, broker, controller


@pytest.mark.asyncio
async def test_controller_stops_at_eight_and_never_starts_runtime(tmp_path) -> None:
    store, _broker, controller = _controller(tmp_path)
    started = False

    draft = await controller.start_or_resume("predict churn")
    loaded = store.load(draft.draft_id)
    assert loaded.questions_asked == 8
    assert loaded.status == "READY_FOR_CONFIRMATION"
    assert loaded.understanding.dataset == "a"
    assert started is False


@pytest.mark.asyncio
async def test_controller_persists_before_await_and_records_pending(tmp_path) -> None:
    store, broker, controller = _controller(tmp_path)

    ori = await controller.start_or_resume("predict churn")
    assert len(broker.requests) == 8
    for request in broker.requests:
        assert request.scope_id == ori.draft_id
        assert request.scope_kind == "clarification"
        assert request.session_id == "s-1"
    assert store.load().pending_request is None


@pytest.mark.asyncio
async def test_identical_task_resumes_and_different_task_conflicts(tmp_path) -> None:
    _store, _broker, controller = _controller(tmp_path)
    first = await controller.start_or_resume("predict churn")
    second = await controller.start_or_resume("  PREDICT   churn ")
    assert second.draft_id == first.draft_id
    assert second.questions_asked == 8

    with pytest.raises(ClarificationControllerError, match="different_task"):
        await controller.start_or_resume("another task")


@pytest.mark.asyncio
async def test_revise_preserves_answers_and_returns_to_clarifying(tmp_path) -> None:
    _store, _broker, controller = _controller(tmp_path)
    ready = await controller.start_or_resume("predict churn")
    before_answers = len(ready.answers)
    revised = await controller.revise(ready.draft_id, ready.revision, "add recall")
    assert revised.status == "CLARIFYING"
    assert len(revised.answers) == before_answers
    assert len(revised.revisions) == 1
    assert len(revised.answers) == 8

    with pytest.raises(ClarificationControllerError, match="stale_revision"):
        await controller.revise(ready.draft_id, ready.revision, "old")

    resumed = await controller.run(revised)
    assert resumed.status == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_revision_instruction_changes_final_understanding(tmp_path) -> None:
    _store, _broker, controller = _controller(
        tmp_path, generator=DeterministicClarificationGenerator()
    )
    ready = await controller.start_or_resume("predict churn")
    assert ready.understanding.primary_metric == "accuracy"

    revised = await controller.revise(
        ready.draft_id,
        ready.revision,
        "Use recall as the primary metric",
    )
    updated = await controller.run(revised)

    assert updated.status == "READY_FOR_CONFIRMATION"
    assert updated.understanding.primary_metric == "recall"


@pytest.mark.asyncio
async def test_revision_negation_does_not_override_primary_metric(tmp_path) -> None:
    _store, _broker, controller = _controller(
        tmp_path, generator=DeterministicClarificationGenerator()
    )
    ready = await controller.start_or_resume("predict churn")
    assert ready.understanding.primary_metric == "accuracy"

    revised = await controller.revise(
        ready.draft_id,
        ready.revision,
        "Keep accuracy, not recall",
    )
    updated = await controller.run(revised)

    assert updated.understanding.primary_metric == "accuracy"


@pytest.mark.asyncio
async def test_revision_explicit_plan_beats_token_order(tmp_path) -> None:
    _store, _broker, controller = _controller(
        tmp_path, generator=DeterministicClarificationGenerator()
    )
    ready = await controller.start_or_resume("predict churn")
    assert ready.understanding.evaluation_plan == "holdout"

    revised = await controller.revise(
        ready.draft_id,
        ready.revision,
        "Use holdout instead of cross-validation",
    )
    updated = await controller.run(revised)

    assert updated.understanding.evaluation_plan == "holdout"


@pytest.mark.asyncio
async def test_cancel_sets_cancelled_and_settles_scope(tmp_path) -> None:
    _store, _broker, controller = _controller(tmp_path)
    ready = await controller.start_or_resume("predict churn")
    cancelled = await controller.cancel(ready.draft_id, ready.revision)
    assert cancelled.status == "CANCELLED"
    # After cancel, a new identical task creates a fresh draft instead of
    # returning the terminal CANCELLED one.
    again = await controller.start_or_resume("predict churn")
    assert again.status != "CANCELLED"
    assert again.status == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_cancel_scope_failure_is_not_silently_flattened(tmp_path) -> None:
    store, broker, controller = _controller(tmp_path)
    ready = await controller.start_or_resume("predict churn")

    async def failing_cancel_scope(session_id: str, scope_id: str):
        raise RuntimeError("broker unavailable")

    broker.cancel_scope = failing_cancel_scope

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await controller.cancel(ready.draft_id, ready.revision)

    assert store.load().status == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_retry_clears_failure_and_returns_to_clarifying(tmp_path) -> None:
    async def failing_generator(draft):
        raise RuntimeError("model down")

    _store, _broker, controller = _controller(tmp_path, generator=failing_generator)
    failed = await controller.start_or_resume("predict churn")

    async def working_generator(draft):
        return ClarificationFinalStep(
            kind="final",
            understanding=DraftUnderstanding(
                title="Predict churn",
                task_type="other",
            ),
            unresolved=[],
        )

    controller._generator = working_generator  # type: ignore[method-assign]
    retried = await controller.retry(failed.draft_id, failed.revision)
    assert retried.status == "CLARIFYING"
    assert retried.failure is None
    assert (await controller.run(retried)).status == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_provider_failure_produces_retryable_failed_draft(tmp_path) -> None:
    async def failing_generator(draft):
        raise RuntimeError("model down")

    _store, _broker, controller = _controller(tmp_path, generator=failing_generator)
    failed = await controller.start_or_resume("predict churn")
    assert failed.status == "FAILED"
    assert failed.failure is not None
    assert failed.failure.retryable is True
    assert failed.understanding.title
    assert any(
        item.critical and item.field == "task_type" for item in failed.unresolved
    )

    restarted = await controller.start_or_resume("forecast demand")
    assert restarted.original_task == "forecast demand"
    assert restarted.status == "FAILED"


@pytest.mark.asyncio
async def test_eighth_pending_question_is_audited_before_finalization(tmp_path) -> None:
    store, _broker, controller = _controller(tmp_path)
    now = datetime(2026, 9, 1, 10, tzinfo=UTC)
    draft = new_draft("predict churn", "s-1", "draft-8", now)
    request = HumanRequest(
        request_id="req-8",
        session_id="s-1",
        scope_id="draft-8",
        scope_kind="clarification",
        prompt="Final question?",
        choices=[HumanChoice(label="A", value="a"), HumanChoice(label="B", value="b")],
        allow_custom=True,
        allow_skip=True,
        created_at=now,
        expires_at=datetime(2026, 9, 1, 10, 2, tzinfo=UTC),
    )
    payload = draft.model_dump()
    payload["questions_asked"] = 7
    payload["answers"] = [
        ClarificationAnswer(
            request_id=f"old-{index}",
            question="Old question",
            outcome="skip",
            answered_at=now,
        ).model_dump()
        for index in range(7)
    ]
    draft = ClarificationDraft.model_validate(payload)
    pending_payload = draft.model_dump()
    pending_payload["pending_request"] = request.model_dump()
    draft = ClarificationDraft.model_validate(pending_payload)
    store.save(draft)

    recovered = await controller.run(draft)

    assert recovered.status == "READY_FOR_CONFIRMATION"
    assert recovered.questions_asked == 8
    assert recovered.answers[-1].outcome == "cancelled"
    assert recovered.pending_request is None


@pytest.mark.asyncio
async def test_cancel_persists_pending_request_as_cancelled(tmp_path) -> None:
    store, _broker, controller = _controller(tmp_path)
    now = datetime(2026, 9, 1, 10, tzinfo=UTC)
    draft = new_draft("predict churn", "s-1", "pending", now)
    request = HumanRequest(
        request_id="req-pending",
        session_id="s-1",
        scope_id="pending",
        scope_kind="clarification",
        prompt="Which dataset?",
        choices=[HumanChoice(label="A", value="a"), HumanChoice(label="B", value="b")],
        allow_custom=True,
        allow_skip=True,
        created_at=now,
        expires_at=datetime(2026, 9, 1, 10, 2, tzinfo=UTC),
    )
    draft = set_pending(draft, request, now)
    store.save(draft)

    cancelled = await controller.cancel(draft.draft_id, draft.revision)

    assert cancelled.status == "CANCELLED"
    assert cancelled.answers[-1].outcome == "cancelled"


@pytest.mark.asyncio
async def test_final_records_missing_required_critical_fields(tmp_path) -> None:
    async def final_generator(draft):
        return ClarificationFinalStep(
            kind="final",
            understanding=DraftUnderstanding(
                title="Predict churn",
                task_type="classification",
            ),
            unresolved=[],
        )

    _store, _broker, controller = _controller(tmp_path, generator=final_generator)
    draft = await controller.start_or_resume("predict churn")

    critical_fields = {item.field for item in draft.unresolved if item.critical}
    assert "task_type" not in critical_fields
    assert {"dataset", "target", "primary_metric", "direction", "evaluation_plan"} <= (
        critical_fields
    )
