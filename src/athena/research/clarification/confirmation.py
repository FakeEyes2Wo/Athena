"""Atomic clarification confirmation followed by retryable lifecycle launch."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from athena.research.clarification.errors import (
    ClarificationConfirmationError,
    ClarificationPersistenceError,
)
from athena.research.clarification.handoff import materialize_handoff
from athena.research.clarification.models import ClarificationDraft, ConfirmationJournal
from athena.research.clarification.persistence import (
    ClarificationStore,
    ConfirmationJournalStore,
)


class ArtifactWriter(Protocol):
    """Persist immutable text and return its content-addressed reference."""

    async def put_text(self, text: str) -> str:
        """Store text for later integrity-checked context loading."""
        ...


@dataclass(frozen=True)
class ConfirmationDependencies:
    """Focused ports used by the transaction, independent from ResearchRuntime."""

    drafts: ClarificationStore
    journals: ConfirmationJournalStore
    state: Any
    state_path: Path
    artifacts: ArtifactWriter
    session_id: str
    lock: asyncio.Lock
    start: Callable[[], Awaitable[None]]


async def commit_confirmation(
    deps: ConfirmationDependencies,
    draft_id: str,
    revision: int,
    acknowledge_unresolved: bool,
) -> ClarificationDraft:
    """Commit all confirmed projections without starting the lifecycle."""
    async with deps.lock:
        # Phase 1: settle any interrupted transaction, then validate the exact
        # session, revision, readiness, and unresolved acknowledgement.
        deps.journals.recover(deps.drafts, deps.state_path)
        draft = _validated_draft(deps, draft_id, revision, acknowledge_unresolved)
        if draft.status == "CONFIRMED":
            return draft

        # Phase 2: snapshot memory and all durable projections before mutation.
        previous = _StateSnapshot.capture(deps.state)
        journal = _prepare_journal(deps, draft)
        deps.journals.save(journal)
        try:
            # Phase 3: derive one confirmed handoff, store its artifact, then
            # persist state and draft projections before marking the commit.
            confirmed = ClarificationDraft.model_validate(
                {**draft.model_dump(), "status": "CONFIRMED"}
            )
            handoff = materialize_handoff(deps.drafts.handoff_path, confirmed)
            ref = await deps.artifacts.put_text(handoff)
            if not ref:
                raise ClarificationConfirmationError(
                    "artifact_write_failed", "artifact store returned an empty ref"
                )
            _project_state(deps.state, confirmed, ref)
            deps.state.save(deps.state_path)
            deps.drafts.save(confirmed)
            deps.journals.save(
                journal.model_copy(update={"phase": "COMMITTED", "handoff_ref": ref})
            )
        except Exception as error:
            # This is the transaction boundary: every pre-commit failure must
            # restore memory, state.json, resume.json, draft, and handoff.
            previous.restore(deps.state)
            deps.journals.recover(deps.drafts, deps.state_path)
            if isinstance(error, ClarificationConfirmationError):
                raise
            code = (
                "state_save_failed"
                if isinstance(error, OSError)
                else "confirmation_write_failed"
            )
            raise ClarificationConfirmationError(code, str(error)) from error
        # Phase 4: COMMITTED is durable; launch happens separately and may retry.
        deps.journals.delete()
        return confirmed


async def launch_confirmed(
    deps: ConfirmationDependencies, draft: ClarificationDraft
) -> ClarificationDraft:
    """Start PREPARE after commit; a failure leaves confirmation retryable."""
    try:
        await deps.start()
    except Exception as error:
        raise ClarificationConfirmationError(
            "confirmed_start_failed", f"confirmed start failed; retry is safe: {error}"
        ) from error
    return draft


async def confirm_and_start(
    runtime: Any,
    draft_id: str,
    revision: int,
    acknowledge_unresolved: bool,
    *,
    start_after: bool = True,
) -> ClarificationDraft:
    """Compatibility facade for the stable ResearchRuntime public method."""
    deps = dependencies_from_runtime(runtime)
    draft = await commit_confirmation(deps, draft_id, revision, acknowledge_unresolved)
    return await launch_confirmed(deps, draft) if start_after else draft


async def recover_confirmation_transaction(runtime: Any) -> None:
    """Recover an interrupted confirmation before lifecycle work resumes."""
    deps = dependencies_from_runtime(runtime)
    async with deps.lock:
        deps.journals.recover(deps.drafts, deps.state_path)


def dependencies_from_runtime(runtime: Any) -> ConfirmationDependencies:
    """Build focused confirmation ports at the legacy runtime boundary."""
    root = runtime.config.paths.athena
    return ConfirmationDependencies(
        drafts=ClarificationStore(root),
        journals=ConfirmationJournalStore(root),
        state=runtime.state,
        state_path=runtime.state_path,
        artifacts=runtime.store,
        session_id=runtime.session_id,
        lock=runtime.session.lifecycle.confirmation_lock,
        start=runtime.start,
    )


def _validated_draft(
    deps: ConfirmationDependencies,
    draft_id: str,
    revision: int,
    acknowledge_unresolved: bool,
) -> ClarificationDraft:
    try:
        draft = deps.drafts.load(draft_id)
    except ClarificationPersistenceError as error:
        # The requested canonical draft is absent or corrupt at confirmation time.
        raise ClarificationConfirmationError("draft_not_found", str(error)) from error
    if draft.session_id != deps.session_id:
        raise ClarificationConfirmationError(
            "wrong_session", "draft belongs to a different session"
        )
    if draft.revision != revision:
        raise ClarificationConfirmationError(
            "stale_revision", f"draft revision is {draft.revision}, expected {revision}"
        )
    if draft.status == "CONFIRMED":
        return draft
    if draft.status != "READY_FOR_CONFIRMATION":
        raise ClarificationConfirmationError(
            "draft_not_ready", f"draft status is {draft.status}"
        )
    if any(item.critical for item in draft.unresolved) and not acknowledge_unresolved:
        raise ClarificationConfirmationError(
            "unresolved_ack_required",
            "critical unresolved items require acknowledgement",
        )
    return draft


def _prepare_journal(
    deps: ConfirmationDependencies, draft: ClarificationDraft
) -> ConfirmationJournal:
    return ConfirmationJournal(
        transaction_id=f"confirm_{uuid4().hex}",
        session_id=draft.session_id,
        draft_id=draft.draft_id,
        revision=draft.revision,
        phase="PREPARED",
        previous_state_json=_read_optional(deps.state_path),
        previous_resume_json=_read_optional(deps.state_path.with_name("resume.json")),
        previous_draft_json=deps.drafts.draft_path.read_text(encoding="utf-8"),
        previous_handoff_text=_read_optional(deps.drafts.handoff_path),
    )


def _project_state(state: Any, draft: ClarificationDraft, handoff_ref: str) -> None:
    state.task_understanding = draft.understanding.model_dump(mode="json")
    if state.task_text is None:
        state.task_text = draft.original_task
    state.handoff_refs = {
        **(state.handoff_refs or {}),
        "task_clarification": handoff_ref,
    }


def _read_optional(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


@dataclass(frozen=True)
class _StateSnapshot:
    understanding: object
    task_text: object
    handoff_refs: dict

    @classmethod
    def capture(cls, state: Any) -> "_StateSnapshot":
        """Copy the mutable fields changed by confirmation."""
        return cls(
            state.task_understanding,
            state.task_text,
            dict(state.handoff_refs or {}),
        )

    def restore(self, state: Any) -> None:
        """Restore in-memory state after a pre-commit failure."""
        state.task_understanding = self.understanding
        state.task_text = self.task_text
        state.handoff_refs = self.handoff_refs


__all__ = [
    "ConfirmationDependencies",
    "commit_confirmation",
    "confirm_and_start",
    "dependencies_from_runtime",
    "launch_confirmed",
    "recover_confirmation_transaction",
]
