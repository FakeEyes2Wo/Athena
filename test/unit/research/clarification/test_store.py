"""Persistence and Markdown tests for ClarificationStore."""

from pathlib import Path

import pytest

from athena.research.clarification.handoff import materialize_handoff, render_handoff
from athena.research.clarification.journal import ConfirmationJournalStore
from athena.research.clarification.models import ClarificationDraft, ConfirmationJournal
from athena.research.clarification.store import (
    ClarificationPersistenceError,
    ClarificationStore,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "clarification"


def _load_ready() -> ClarificationDraft:
    text = (FIXTURES / "draft_ready.json").read_text(encoding="utf-8")
    return ClarificationDraft.model_validate_json(text)


def test_round_trip_retains_answers_and_revision(tmp_path: Path) -> None:
    store = ClarificationStore(tmp_path)
    draft = _load_ready()
    store.save(draft)

    loaded = store.load("draft-1")
    assert loaded.revision == draft.revision
    assert loaded.status == draft.status
    assert [a.outcome for a in loaded.answers] == [a.outcome for a in draft.answers]

    assert not (tmp_path / "clarification.json.tmp").exists()


def test_missing_corrupt_json_raise_persistence_error(tmp_path: Path) -> None:
    store = ClarificationStore(tmp_path)
    with pytest.raises(ClarificationPersistenceError, match="draft_not_found"):
        store.load()

    store.save(_load_ready())
    store.draft_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ClarificationPersistenceError, match="draft_corrupt"):
        store.load()


def test_markdown_preserves_typed_outcomes_and_metadata(tmp_path: Path) -> None:
    draft = _load_ready()
    text = render_handoff(draft)
    assert "Draft ID: draft-1" in text
    assert "Revision: 7" in text
    assert "Outcome: skip" in text
    assert "Outcome: timeout" in text
    assert "Outcome: cancelled" in text
    assert "Outcome: choice" in text
    assert "## Unresolved items" in text
    assert "- [critical] target: not supplied" in text


def test_materialize_handoff_writes_exact_text_atomically(tmp_path: Path) -> None:
    store = ClarificationStore(tmp_path)
    draft = _load_ready()
    text = materialize_handoff(store.handoff_path, draft)
    assert store.handoff_path.read_text(encoding="utf-8") == text
    assert not (store.handoff_path.parent / "TASK_CLARIFICATION.md.tmp").exists()


def test_confirmation_journal_round_trip_and_recovery(tmp_path: Path) -> None:
    store = ClarificationStore(tmp_path)
    draft = _load_ready()
    store.save(draft)
    before = store.draft_path.read_text(encoding="utf-8")

    journals = ConfirmationJournalStore(tmp_path)
    journal = journals.load()
    assert journal is None

    store.draft_path.write_text('{"status":"CONFIRMED"}', encoding="utf-8")
    # A PREPARED journal restores the previous draft bytes.
    journal = ConfirmationJournal(
        transaction_id="tx-1",
        session_id="s-1",
        draft_id="draft-1",
        revision=7,
        phase="PREPARED",
        previous_state_json=None,
        previous_draft_json=before,
        previous_handoff_text=None,
    )
    journals.save(journal)
    assert journals.load().draft_id == "draft-1"
    assert journals.recover(store, store.state_path) is True
    assert store.draft_path.read_text(encoding="utf-8") == before
    assert journals.load() is None
