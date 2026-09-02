"""Atomic persistence for the canonical clarification draft."""

import json
from pathlib import Path

from athena.core.persistence import atomic_write_json
from athena.research.clarification.errors import (
    ClarificationConfirmationError,
    ClarificationPersistenceError,
)
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


__all__ = [
    "ClarificationConfirmationError",
    "ClarificationPersistenceError",
    "ClarificationStore",
    "ConfirmationJournal",
]
