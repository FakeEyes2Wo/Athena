"""Atomic persistence for the canonical clarification draft."""

import json
from pathlib import Path

from athena.core.persistence import atomic_write_json
from athena.research.clarification.errors import (
    ClarificationError,
    ClarificationPersistenceError,
)
from athena.research.clarification.handoff import atomic_write_text, materialize_handoff
from athena.research.clarification.models import ClarificationDraft, ConfirmationJournal


class ClarificationStore:
    """Store one current clarification draft per session state root."""

    def __init__(self, state_root: str | Path) -> None:
        """Bind persistence to one session state directory."""
        self.root = Path(state_root).resolve()

    @property
    def draft_path(self) -> Path:
        """Return the canonical draft file path."""
        return self.root / "clarification.json"

    @property
    def handoff_path(self) -> Path:
        """Return the named handoff path derived from this session root."""
        return self.root / "handoffs" / "TASK_CLARIFICATION.md"

    @property
    def state_path(self) -> Path:
        """Return the research checkpoint path beside the draft."""
        return self.root / "state.json"

    @property
    def journal_path(self) -> Path:
        """Return the confirmation transaction journal path."""
        return self.root / "clarification-confirmation.json"

    def exists(self) -> bool:
        """Report whether this session has a persisted clarification draft."""
        return self.draft_path.is_file()

    def load(self, draft_id: str | None = None) -> ClarificationDraft:
        """Load and validate the current draft, optionally checking its ID."""
        if not self.exists():
            raise ClarificationPersistenceError(f"draft_not_found: {self.draft_path}")
        try:
            payload = json.loads(self.draft_path.read_text(encoding="utf-8"))
            draft = ClarificationDraft.model_validate(payload)
        except Exception as error:
            raise ClarificationPersistenceError(
                f"draft_corrupt: could not load {self.draft_path}: {error}"
            ) from error
        if draft_id is not None and draft.draft_id != draft_id:
            raise ClarificationPersistenceError(
                f"draft_not_found: requested {draft_id}, found {draft.draft_id}"
            )
        return draft

    def save(self, draft: ClarificationDraft) -> Path:
        """Atomically replace the canonical draft file."""
        try:
            return atomic_write_json(self.draft_path, draft.model_dump(mode="json"))
        except OSError as error:
            # The filesystem rejected the atomic replacement of clarification.json.
            raise ClarificationPersistenceError(
                f"draft_write_failed: {error}"
            ) from error

    def load_journal(self) -> ConfirmationJournal | None:
        """Load a pending journal or report that no transaction exists."""
        if not self.journal_path.is_file():
            return None
        try:
            return ConfirmationJournal.model_validate_json(
                self.journal_path.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise ClarificationError(
                "confirmation_recovery_failed",
                f"confirmation journal is corrupt: {error}",
            ) from error

    def save_journal(self, journal: ConfirmationJournal) -> None:
        """Atomically persist the latest transaction phase."""
        try:
            atomic_write_json(self.journal_path, journal.model_dump(mode="json"))
        except OSError as error:
            raise ClarificationError("confirmation_write_failed", str(error)) from error

    def delete_journal(self) -> None:
        """Remove the journal after commit or rollback completes."""
        self.journal_path.unlink(missing_ok=True)

    def recover(self) -> bool:
        """Rollback prepared data or finish committed derived files."""
        journal = self.load_journal()
        if journal is None:
            return False
        if journal.phase == "PREPARED":
            _restore(self.state_path, journal.previous_state_json)
            _restore(
                self.state_path.with_name("resume.json"), journal.previous_resume_json
            )
            atomic_write_text(self.draft_path, journal.previous_draft_json)
            _restore(self.handoff_path, journal.previous_handoff_text)
        else:
            draft = _confirmed_draft(self, journal)
            materialize_handoff(self.handoff_path, draft)
        self.delete_journal()
        return True


def _confirmed_draft(
    drafts: ClarificationStore, journal: ConfirmationJournal
) -> ClarificationDraft:
    try:
        draft = drafts.load(journal.draft_id)
    except ClarificationPersistenceError:
        draft = None
    if draft is not None and draft.status == "CONFIRMED":
        return draft
    draft = ClarificationDraft.model_validate_json(journal.previous_draft_json)
    confirmed = ClarificationDraft.model_validate(
        {**draft.model_dump(), "status": "CONFIRMED"}
    )
    drafts.save(confirmed)
    return confirmed


def _restore(path: Path, text: str | None) -> None:
    if text is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_text(path, text)


__all__ = ["ClarificationStore"]
