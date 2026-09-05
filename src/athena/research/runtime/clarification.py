"""Clarification and confirmation behavior at the runtime boundary."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from athena.research.clarification.confirmation import (
    confirm_and_start,
    recover_confirmation_transaction,
)
from athena.research.clarification.errors import ClarificationError
from athena.research.clarification.handoff import atomic_write_text
from athena.research.clarification.models import ClarificationDraft
from athena.research.clarification.persistence import ClarificationStore
from athena.research.clarification.requirements import (
    initial_task_understanding,
    normalize_task,
)
from athena.research.supervisor.state import ResearchState


def clarification_store(runtime: Any) -> ClarificationStore:
    """Return the clarification store rooted in the runtime state directory."""
    config = getattr(runtime, "config", None)
    state_root = config.paths.athena if config is not None else None
    if state_root is None:
        raise ClarificationError(
            "confirmation_recovery_failed",
            "runtime has no clarification state root",
        )
    return ClarificationStore(state_root)


async def auto_confirm(runtime: Any, task: str) -> ClarificationDraft:
    """Create or reuse a deterministic draft and confirm it without starting."""
    store = clarification_store(runtime)
    draft = store.load() if store.exists() else None
    if draft is not None and normalize_task(draft.original_task) != normalize_task(
        task
    ):
        draft = None
    if draft is None or draft.status not in {"READY_FOR_CONFIRMATION", "CONFIRMED"}:
        understanding, unresolved = initial_task_understanding(task)
        now = datetime.now(UTC)
        draft = ClarificationDraft(
            draft_id=f"auto_{uuid4().hex}",
            revision=0,
            session_id=runtime.session_id,
            original_task=task,
            status="READY_FOR_CONFIRMATION",
            understanding=understanding,
            unresolved=unresolved,
            questions_asked=0,
            created_at=now,
            updated_at=now,
        )
        store.save(draft)
    return await confirm_and_start(
        runtime,
        draft.draft_id,
        draft.revision,
        acknowledge_unresolved=True,
        start_after=False,
    )


async def recover_confirmation(runtime: Any) -> None:
    """Recover confirmation files and synchronize projected memory fields."""
    journal_path = clarification_store(runtime).root / "clarification-confirmation.json"
    interrupted = journal_path.is_file()
    await recover_confirmation_transaction(runtime)
    if interrupted:
        if runtime.state_path.is_file():
            durable = ResearchState.load(runtime.state_path)
            runtime.state.task_understanding = durable.task_understanding
            runtime.state.task_text = durable.task_text
            runtime.state.handoff_refs = dict(durable.handoff_refs)
        else:
            runtime.state.task_understanding = None
            runtime.state.task_text = None
            runtime.state.handoff_refs = {}
    await _restore_legacy_named_handoff(runtime)


async def _restore_legacy_named_handoff(runtime: Any) -> None:
    """Materialize the named file omitted by legacy confirmed checkpoints."""
    store = clarification_store(runtime)
    if store.handoff_path.is_file() or not runtime.state.task_understanding:
        return
    handoff_ref = (runtime.state.handoff_refs or {}).get("task_clarification")
    if not handoff_ref:
        return
    try:
        handoff = await runtime.store.get_text(handoff_ref)
        if not handoff.strip():
            raise ValueError("task clarification handoff artifact is empty")
        atomic_write_text(store.handoff_path, handoff)
    except Exception as error:
        raise ClarificationError(
            "confirmation_recovery_failed",
            f"could not restore legacy task clarification handoff: {error}",
        ) from error


async def confirm_pending_task(runtime: Any) -> None:
    """Require or auto-confirm raw task text before lifecycle start."""
    if not runtime.task_text.strip() or runtime.state.task_understanding is not None:
        return
    if runtime.config.auto_confirm:
        await auto_confirm(runtime, runtime.task_text)
        return
    code = (
        "confirmation_required"
        if runtime.config.task_confirmation_gate
        else "confirmation_policy_required"
    )
    raise ClarificationError(
        code,
        "raw task start requires explicit confirmation or auto_confirm=True",
    )


async def seed_unconfirmed_task(runtime: Any, task: str) -> None:
    """Seed raw task text only through the configured confirmation policy."""
    if runtime.config.task_confirmation_gate:
        raise ClarificationError(
            "confirmation_required",
            "raw task start requires an explicitly confirmed clarification draft",
        )
    if not runtime.config.auto_confirm:
        raise ClarificationError(
            "confirmation_policy_required",
            "raw task start requires auto_confirm=True",
        )
    effective_task = runtime.state.task_text or task
    if runtime.state.task_text is None:
        runtime.state.task_text = effective_task
        runtime.state.save(runtime.state_path)
    runtime.session.lifecycle.task_text = effective_task
    await auto_confirm(runtime, effective_task)


__all__ = [
    "auto_confirm",
    "clarification_store",
    "confirm_pending_task",
    "recover_confirmation",
    "seed_unconfirmed_task",
]
