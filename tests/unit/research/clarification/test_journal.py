"""Confirmation journal persistence tests."""

import json
from pathlib import Path

from athena.research.clarification.models import (
    ClarificationDraft,
    ConfirmationJournal,
)
from athena.research.clarification.persistence import ClarificationStore

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "clarification"


def test_journal_round_trip(tmp_path) -> None:
    store = ClarificationStore(tmp_path)
    journal = ConfirmationJournal(
        transaction_id="tx-1",
        session_id="s-1",
        draft_id="draft-1",
        revision=2,
        phase="PREPARED",
        previous_state_json=None,
        previous_draft_json="{}",
        previous_handoff_text=None,
    )

    store.save_journal(journal)

    assert store.load_journal() == journal


def test_prepared_recovery_restores_state_and_resume_together(tmp_path) -> None:
    drafts = ClarificationStore(tmp_path)
    payload = json.loads((FIXTURES / "draft_ready.json").read_text(encoding="utf-8"))
    draft = ClarificationDraft.model_validate(payload)
    drafts.save(draft)
    previous_draft = drafts.draft_path.read_text(encoding="utf-8")
    state_path = tmp_path / "state.json"
    resume_path = tmp_path / "resume.json"
    state_path.write_text('{"status":"IDLE"}', encoding="utf-8")
    resume_path.write_text('{"task_text":"before"}', encoding="utf-8")
    drafts.handoff_path.parent.mkdir(parents=True)
    drafts.handoff_path.write_text("before handoff", encoding="utf-8")
    drafts.save_journal(
        ConfirmationJournal(
            transaction_id="tx-restore",
            session_id=draft.session_id,
            draft_id=draft.draft_id,
            revision=draft.revision,
            phase="PREPARED",
            previous_state_json=state_path.read_text(encoding="utf-8"),
            previous_resume_json=resume_path.read_text(encoding="utf-8"),
            previous_draft_json=previous_draft,
            previous_handoff_text="before handoff",
        )
    )
    state_path.write_text('{"status":"RUNNING"}', encoding="utf-8")
    resume_path.write_text('{"task_text":"after"}', encoding="utf-8")
    drafts.draft_path.write_text("{}", encoding="utf-8")
    drafts.handoff_path.write_text("after handoff", encoding="utf-8")

    recovered = drafts.recover()

    assert recovered is True
    assert state_path.read_text(encoding="utf-8") == '{"status":"IDLE"}'
    assert resume_path.read_text(encoding="utf-8") == '{"task_text":"before"}'
    assert drafts.draft_path.read_text(encoding="utf-8") == previous_draft
    assert drafts.handoff_path.read_text(encoding="utf-8") == "before handoff"
    assert drafts.load_journal() is None
