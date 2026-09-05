"""Deterministic and transactional controller tests."""

import asyncio
from datetime import UTC, datetime

import pytest

from athena.core.human_request import HumanChoice, HumanOutcome, HumanRequest
from athena.research.clarification.controller import (
    CLARIFICATION_FAILURE_NOTICE,
    ClarificationController,
    ClarificationOptions,
)
from athena.research.clarification.errors import ClarificationError
from athena.research.clarification.generator import (
    ClarificationFinalStep,
    ClarificationModelOutput,
    ClarificationQuestionStep,
    DeterministicClarificationGenerator,
    PublicProgress,
)
from athena.research.clarification.models import (
    ClarificationAnswer,
    ClarificationDraft,
    DraftUnderstanding,
    UnresolvedItem,
)
from athena.research.clarification.persistence import ClarificationStore
from athena.research.clarification.state import MAX_QUESTIONS, new_draft, set_pending


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


class RecordingSink:
    def __init__(self, store: ClarificationStore) -> None:
        self.store = store
        self.events: list[dict[str, object]] = []

    async def __call__(self, **event: object) -> None:
        draft = self.store.load()
        self.events.append(
            {
                **event,
                "status": draft.status,
                "pending_request": draft.pending_request,
            }
        )


class CancelSink:
    async def __call__(self, **event: object) -> None:
        raise asyncio.CancelledError


def _public_output(step, summary: str = "safe update") -> ClarificationModelOutput:
    return ClarificationModelOutput(
        public_update=PublicProgress(stage="analysis", summary=summary),
        step=step,
    )


def _final_step() -> ClarificationFinalStep:
    return ClarificationFinalStep(
        kind="final",
        understanding=DraftUnderstanding(title="Predict churn", task_type="other"),
        unresolved=[],
    )


def _question_step() -> ClarificationQuestionStep:
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


def _controller(tmp_path, generator=None, *, session="s-1", progress_sink=None):
    store = ClarificationStore(tmp_path)
    broker = AutoBroker()
    controller = ClarificationController(
        store,
        broker,
        generator or AlwaysQuestionGenerator(),
        ClarificationOptions(
            session_id=session,
            clock=lambda: datetime(2026, 9, 1, 10, tzinfo=UTC),
            id_factory=lambda prefix: f"{prefix}-1",
            progress_sink=progress_sink,
        ),
    )
    return store, broker, controller


def _save_capped_draft(
    store: ClarificationStore, draft_id: str = "draft-cap"
) -> ClarificationDraft:
    draft = new_draft(
        "predict churn", "s-1", draft_id, datetime(2026, 9, 1, 10, tzinfo=UTC)
    )
    draft = ClarificationDraft.model_validate(
        {**draft.model_dump(), "questions_asked": 8}
    )
    store.save(draft)
    return draft


@pytest.mark.asyncio
async def test_controller_stops_at_eight_and_accepts_final_generator_output(
    tmp_path,
) -> None:
    store, _broker, controller = _controller(tmp_path)
    started = False

    draft = await controller.start_or_resume("predict churn")
    loaded = store.load(draft.draft_id)
    assert loaded.questions_asked == 8
    assert loaded.status == "READY_FOR_CONFIRMATION"
    assert loaded.understanding.primary_metric == "f1"
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

    with pytest.raises(ClarificationError, match="different_task"):
        await controller.start_or_resume("another task")


@pytest.mark.asyncio
async def test_revise_preserves_answers_and_returns_to_clarifying(tmp_path) -> None:
    _store, _broker, controller = _controller(tmp_path)
    ready = await controller.start_or_resume("predict churn")
    before_answers = len(ready.answers)
    revised = controller.revise(ready.draft_id, ready.revision, "add recall")
    assert revised.status == "CLARIFYING"
    assert len(revised.answers) == before_answers
    assert len(revised.revisions) == 1
    assert len(revised.answers) == 8

    with pytest.raises(ClarificationError, match="stale_revision"):
        controller.revise(ready.draft_id, ready.revision, "old")

    resumed = await controller.run(revised)
    assert resumed.status == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_revision_instruction_changes_final_understanding(tmp_path) -> None:
    _store, _broker, controller = _controller(
        tmp_path, generator=DeterministicClarificationGenerator()
    )
    ready = await controller.start_or_resume("predict churn")
    assert ready.understanding.primary_metric == "accuracy"

    revised = controller.revise(
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

    revised = controller.revise(
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

    revised = controller.revise(
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
    retried = controller.retry(failed.draft_id, failed.revision)
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


@pytest.mark.asyncio
async def test_question_and_final_updates_publish_after_each_save(tmp_path) -> None:
    sink = RecordingSink(ClarificationStore(tmp_path))
    results = [_public_output(_question_step()), _public_output(_final_step())]

    async def generator(_draft):
        return results.pop(0)

    _store, _broker, controller = _controller(
        tmp_path, generator=generator, progress_sink=sink
    )
    draft = await controller.start_or_resume("predict churn")

    assert draft.status == "READY_FOR_CONFIRMATION"
    assert len(sink.events) == 2
    assert sink.events[0]["status"] == "CLARIFYING"
    assert sink.events[0]["pending_request"] is not None
    assert sink.events[1]["status"] == "READY_FOR_CONFIRMATION"
    assert all(
        {
            "source": event["source"],
            "persist": event["persist"],
            "session_id": event["session_id"],
            "scope_id": event["scope_id"],
        }
        == {
            "source": "agent",
            "persist": True,
            "session_id": draft.session_id,
            "scope_id": draft.draft_id,
        }
        for event in sink.events
    )


@pytest.mark.asyncio
async def test_missing_public_update_does_not_publish(tmp_path) -> None:
    sink = RecordingSink(ClarificationStore(tmp_path))

    async def generator(_draft):
        return _final_step()

    store, _broker, controller = _controller(
        tmp_path, generator=generator, progress_sink=sink
    )
    draft = new_draft("predict churn", "s-1", "draft-no-update", datetime.now(UTC))
    store.save(draft)

    result = await controller.run(draft)

    assert result.status == "READY_FOR_CONFIRMATION"
    assert sink.events == []


@pytest.mark.asyncio
async def test_cap_invokes_generator_once_and_accepts_final_without_broker(
    tmp_path,
) -> None:
    sink = RecordingSink(ClarificationStore(tmp_path))
    calls = 0

    async def generator(_draft):
        nonlocal calls
        calls += 1
        return _public_output(
            ClarificationFinalStep(
                kind="final",
                understanding=DraftUnderstanding(
                    title="LLM final", task_type="regression"
                ),
                unresolved=[],
            ),
            "final update",
        )

    store, broker, controller = _controller(
        tmp_path, generator=generator, progress_sink=sink
    )
    draft = _save_capped_draft(store)

    result = await controller.run(draft)

    assert calls == 1
    assert broker.requests == []
    assert result.status == "READY_FOR_CONFIRMATION"
    assert result.understanding.title == "LLM final"
    assert sink.events[0]["status"] == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_cap_defensive_question_uses_best_final_without_broker(tmp_path) -> None:
    sink = RecordingSink(ClarificationStore(tmp_path))

    async def generator(_draft):
        return _public_output(_question_step(), "defensive final")

    store, broker, controller = _controller(
        tmp_path, generator=generator, progress_sink=sink
    )
    draft = _save_capped_draft(store)

    result = await controller.run(draft)

    assert broker.requests == []
    assert result.status == "READY_FOR_CONFIRMATION"
    assert result.understanding == draft.understanding
    assert result.questions_asked == MAX_QUESTIONS
    assert len(sink.events) == 1
    assert sink.events[0]["status"] == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_cap_provider_failure_saves_failed_without_deterministic_fallback(
    tmp_path,
) -> None:
    sink = RecordingSink(ClarificationStore(tmp_path))
    calls = 0

    async def generator(_draft):
        nonlocal calls
        calls += 1
        raise RuntimeError("model down; sk-secret-value")

    store, _broker, controller = _controller(
        tmp_path, generator=generator, progress_sink=sink
    )
    draft = _save_capped_draft(store)

    result = await controller.run(draft)

    assert calls == 1
    assert result.status == "FAILED"
    assert sink.events[0]["summary"] == CLARIFICATION_FAILURE_NOTICE
    assert "model down" not in repr(sink.events)
    assert "sk-secret-value" not in repr(sink.events)


@pytest.mark.asyncio
async def test_provider_failure_publishes_one_fixed_notice_after_failed_save(
    tmp_path,
) -> None:
    sink = RecordingSink(ClarificationStore(tmp_path))

    async def generator(_draft):
        raise RuntimeError("model down; sk-secret-value")

    _store, _broker, controller = _controller(
        tmp_path, generator=generator, progress_sink=sink
    )
    failed = await controller.start_or_resume("predict churn")

    assert failed.status == "FAILED"
    assert sink.events == [
        {
            "summary": CLARIFICATION_FAILURE_NOTICE,
            "stage": "failure",
            "source": "agent",
            "persist": True,
            "session_id": "s-1",
            "scope_id": failed.draft_id,
            "status": "FAILED",
            "pending_request": None,
        }
    ]


@pytest.mark.asyncio
async def test_sink_error_does_not_change_ready_or_failed_state(tmp_path) -> None:
    async def sink(**_event):
        raise RuntimeError("display down")

    async def final_generator(_draft):
        return _public_output(_final_step())

    _store, _broker, controller = _controller(
        tmp_path, generator=final_generator, progress_sink=sink
    )
    ready = await controller.start_or_resume("predict churn")
    assert ready.status == "READY_FOR_CONFIRMATION"

    async def failing_generator(_draft):
        raise RuntimeError("provider down")

    controller._generator = failing_generator  # type: ignore[method-assign]
    revised = controller.revise(ready.draft_id, ready.revision, "retry")
    failed = await controller.run(revised)
    assert failed.status == "FAILED"


@pytest.mark.asyncio
async def test_save_failure_never_publishes(tmp_path) -> None:
    async def final_generator(_draft):
        return _public_output(_final_step())

    sink = RecordingSink(ClarificationStore(tmp_path))
    store, _broker, controller = _controller(
        tmp_path, generator=final_generator, progress_sink=sink
    )
    draft = new_draft("predict churn", "s-1", "draft-save", datetime.now(UTC))
    store.save(draft)

    def failing_save(_draft):
        raise RuntimeError("disk full")

    store.save = failing_save  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="disk full"):
        await controller.run(draft)
    assert sink.events == []


@pytest.mark.asyncio
async def test_generation_cancellation_keeps_clarifying_draft(tmp_path) -> None:
    async def generator(_draft):
        raise asyncio.CancelledError

    store, _broker, controller = _controller(tmp_path, generator=generator)
    draft = new_draft(
        "predict churn", "s-1", "draft-generation-cancel", datetime.now(UTC)
    )
    store.save(draft)
    with pytest.raises(asyncio.CancelledError):
        await controller.run(draft)
    assert store.load().status == "CLARIFYING"


@pytest.mark.asyncio
async def test_question_publication_cancellation_keeps_pending_request(
    tmp_path,
) -> None:
    store, _broker, controller = _controller(
        tmp_path,
        generator=lambda _draft: _public_output(_question_step()),
        progress_sink=CancelSink(),
    )
    draft = new_draft(
        "predict churn", "s-1", "draft-question-cancel", datetime.now(UTC)
    )
    store.save(draft)

    with pytest.raises(asyncio.CancelledError):
        await controller.run(draft)
    saved = store.load()
    assert saved.status == "CLARIFYING"
    assert saved.pending_request is not None


@pytest.mark.asyncio
async def test_final_publication_cancellation_keeps_ready_draft(tmp_path) -> None:
    store, _broker, controller = _controller(
        tmp_path,
        generator=lambda _draft: _public_output(_final_step()),
        progress_sink=CancelSink(),
    )
    draft = new_draft("predict churn", "s-1", "draft-final-cancel", datetime.now(UTC))
    store.save(draft)

    with pytest.raises(asyncio.CancelledError):
        await controller.run(draft)
    assert store.load().status == "READY_FOR_CONFIRMATION"


@pytest.mark.asyncio
async def test_failure_publication_cancellation_keeps_failed_draft(tmp_path) -> None:
    async def failing_generator(_draft):
        raise RuntimeError("provider down")

    store, _broker, controller = _controller(
        tmp_path,
        generator=failing_generator,
        progress_sink=CancelSink(),
    )
    draft = new_draft("predict churn", "s-1", "draft-failure-cancel", datetime.now(UTC))
    store.save(draft)

    with pytest.raises(asyncio.CancelledError):
        await controller.run(draft)
    assert store.load().status == "FAILED"
