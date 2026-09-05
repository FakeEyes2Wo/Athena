"""Async orchestration for the persisted clarification workflow."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from athena.core.human_request import HumanOutcome, HumanRequest
from athena.research.clarification.errors import (
    ClarificationError,
    ClarificationPersistenceError,
)
from athena.research.clarification.generator import (
    ClarificationFinalStep,
    ClarificationGenerator,
    ClarificationQuestionStep,
    ClarificationTurnResult,
    PublicProgressSink,
    generate_turn,
    publish_public_progress,
)
from athena.research.clarification.models import ClarificationDraft
from athena.research.clarification.persistence import ClarificationStore
from athena.research.clarification.requirements import normalize_task
from athena.research.clarification.state import (
    MAX_QUESTIONS,
    best_final,
    fail,
    finalize,
    new_draft,
    set_pending,
    settle_answer,
)
from athena.research.clarification.state import (
    cancel as cancel_draft,
)
from athena.research.clarification.state import (
    retry as retry_draft,
)
from athena.research.clarification.state import revise as revise_draft


class HumanBroker(Protocol):
    """Supply and cancel typed human outcomes for one clarification scope."""

    async def ask(self, request: HumanRequest) -> HumanOutcome:
        """Wait for one settled typed outcome."""
        ...

    async def cancel_scope(self, session_id: str, scope_id: str) -> list[HumanOutcome]:
        """Cancel pending requests in one session scope."""
        ...


CLARIFICATION_FAILURE_NOTICE = "Task understanding failed. Retry to continue."


@dataclass(frozen=True)
class ClarificationOptions:
    """Configure controller identity, time, and public progress projection."""

    session_id: str = "default"
    id_factory: Callable[[str], str] = lambda prefix: (f"{prefix}_{uuid4().hex[:12]}")
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    progress_sink: PublicProgressSink | None = None


class ClarificationController:
    """Coordinate a generator, human broker, and durable draft store."""

    def __init__(
        self,
        store: ClarificationStore,
        broker: HumanBroker,
        generator: ClarificationGenerator,
        options: ClarificationOptions | None = None,
    ) -> None:
        """Bind the three workflow ports and stable identity policy."""
        self._store = store
        self._broker = broker
        self._generator = generator
        self._options = options or ClarificationOptions()

    async def start_or_resume(self, task: str) -> ClarificationDraft:
        """Create a fresh draft or continue the matching active draft."""
        normalized = normalize_task(task)
        if self._store.exists():
            current = self._store.load()
            if current.status not in {"CANCELLED", "FAILED"}:
                if normalize_task(current.original_task) != normalized:
                    raise ClarificationError(
                        "different_task",
                        "cancel the active draft before starting another task",
                    )
                return await self.run(current)
        draft = new_draft(
            task,
            self._options.session_id,
            self._options.id_factory("clarify"),
            self._options.clock(),
        )
        self._store.save(draft)
        return await self.run(draft)

    async def run(self, draft: ClarificationDraft) -> ClarificationDraft:
        """Drive a clarifying draft until it becomes ready or failed."""
        if draft.status != "CLARIFYING":
            return draft
        if draft.pending_request is not None:
            draft = await self._recover_pending(draft)
        while draft.status == "CLARIFYING":
            try:
                turn = await generate_turn(self._generator, draft)
            except Exception:  # noqa: BLE001
                # Injected generators cross the model/provider boundary; any
                # provider failure becomes a durable, retryable FAILED draft.
                failed = self._save(fail(draft, self._options.clock()))
                await publish_public_progress(
                    self._options.progress_sink,
                    summary=CLARIFICATION_FAILURE_NOTICE,
                    stage="failure",
                    session_id=failed.session_id,
                    scope_id=failed.draft_id,
                    source="agent",
                    persist=True,
                )
                return failed
            if isinstance(turn.step, ClarificationFinalStep):
                ready = self._save(finalize(draft, turn.step, self._options.clock()))
                await self._publish_update(ready, turn)
                return ready
            if draft.questions_asked >= MAX_QUESTIONS:
                ready = self._save(best_final(draft, self._options.clock()))
                await self._publish_update(ready, turn)
                return ready
            draft = await self._ask(draft, turn.step, turn)
        return draft

    def get(self, draft_id: str) -> ClarificationDraft:
        """Read the authoritative persisted draft."""
        return self._store.load(draft_id)

    def retry(self, draft_id: str, revision: int) -> ClarificationDraft:
        """Return a retryable failed draft to clarification."""
        draft = self._for_revision(draft_id, revision)
        if draft.status != "FAILED":
            raise ClarificationError("not_failed", "only FAILED drafts can be retried")
        if draft.failure is None or not draft.failure.retryable:
            raise ClarificationError("not_retryable", "draft failure is not retryable")
        return self._save(retry_draft(draft, self._options.clock()))

    def revise(
        self, draft_id: str, revision: int, instruction: str
    ) -> ClarificationDraft:
        """Record a revision instruction against the latest ready draft."""
        draft = self._for_revision(draft_id, revision)
        instruction = instruction.strip()
        if draft.status != "READY_FOR_CONFIRMATION":
            raise ClarificationError(
                "draft_not_ready", "only a ready draft can be revised"
            )
        if not instruction:
            raise ClarificationError(
                "invalid_instruction", "revision instruction is empty"
            )
        return self._save(revise_draft(draft, instruction, self._options.clock()))

    async def cancel(self, draft_id: str, revision: int) -> ClarificationDraft:
        """Cancel an unconfirmed draft and persist any pending outcome."""
        draft = self._for_revision(draft_id, revision)
        if draft.status not in {"CLARIFYING", "READY_FOR_CONFIRMATION", "FAILED"}:
            raise ClarificationError(
                "draft_not_cancellable", f"draft status is {draft.status}"
            )
        outcomes = await self._cancel_scope(draft)
        if draft.pending_request is not None:
            draft = settle_answer(
                draft,
                draft.pending_request,
                _matching_outcome(
                    draft.pending_request, outcomes, self._options.clock()
                ),
                self._options.clock(),
            )
        return self._save(cancel_draft(draft, self._options.clock()))

    async def _ask(
        self,
        draft: ClarificationDraft,
        step: ClarificationQuestionStep,
        turn: ClarificationTurnResult,
    ) -> ClarificationDraft:
        request = self._request(draft, step)
        draft = self._save(set_pending(draft, request, self._options.clock()))
        await self._publish_update(draft, turn)
        try:
            outcome = await self._broker.ask(request)
        except Exception:  # noqa: BLE001
            # Broker.ask crosses the human transport boundary. Preserve the
            # interrupted question as cancelled before exposing FAILED.
            outcomes = await self._cancel_scope(draft)
            draft = settle_answer(
                draft,
                request,
                _matching_outcome(request, outcomes, self._options.clock()),
                self._options.clock(),
                field=step.field,
            )
            return self._save(fail(draft, self._options.clock()))
        return self._save(
            settle_answer(
                draft, request, outcome, self._options.clock(), field=step.field
            )
        )

    async def _publish_update(
        self, draft: ClarificationDraft, turn: ClarificationTurnResult
    ) -> None:
        """Publish a model summary after its matching draft transition is saved."""
        update = turn.public_update
        if update is None:
            return
        await publish_public_progress(
            self._options.progress_sink,
            summary=update.summary,
            stage=update.stage,
            session_id=draft.session_id,
            scope_id=draft.draft_id,
            source="agent",
            persist=True,
        )

    async def _recover_pending(self, draft: ClarificationDraft) -> ClarificationDraft:
        request = draft.pending_request
        assert request is not None
        outcomes = await self._cancel_scope(draft)
        return self._save(
            settle_answer(
                draft,
                request,
                _matching_outcome(request, outcomes, self._options.clock()),
                self._options.clock(),
            )
        )

    async def _cancel_scope(self, draft: ClarificationDraft) -> list[HumanOutcome]:
        return await self._broker.cancel_scope(draft.session_id, draft.draft_id)

    def _request(
        self, draft: ClarificationDraft, step: ClarificationQuestionStep
    ) -> HumanRequest:
        now = self._options.clock()
        return HumanRequest(
            request_id=self._options.id_factory("req"),
            session_id=draft.session_id,
            scope_id=draft.draft_id,
            scope_kind="clarification",
            prompt=step.prompt,
            choices=step.choices,
            allow_custom=step.allow_custom,
            allow_skip=step.allow_skip,
            created_at=now,
            expires_at=now + timedelta(minutes=2),
        )

    def _for_revision(self, draft_id: str, revision: int) -> ClarificationDraft:
        try:
            draft = self._store.load(draft_id)
        except ClarificationPersistenceError as error:
            # The requested draft is absent or corrupt, so expose a stable RPC code.
            raise ClarificationError("draft_not_found", str(error)) from error
        if draft.revision != revision:
            raise ClarificationError(
                "stale_revision",
                f"draft revision is {draft.revision}, expected {revision}",
            )
        return draft

    def _save(self, draft: ClarificationDraft) -> ClarificationDraft:
        self._store.save(draft)
        return draft


def _matching_outcome(
    request: HumanRequest, outcomes: list[HumanOutcome], now: datetime
) -> HumanOutcome:
    return next(
        (outcome for outcome in outcomes if outcome.request_id == request.request_id),
        HumanOutcome(request_id=request.request_id, kind="cancelled", settled_at=now),
    )


__all__ = [
    "CLARIFICATION_FAILURE_NOTICE",
    "ClarificationController",
    "ClarificationOptions",
]
