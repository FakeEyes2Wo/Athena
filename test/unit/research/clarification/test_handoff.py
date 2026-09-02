"""Confirmed handoff rendering tests."""

import json
from pathlib import Path

from athena.research.clarification.handoff import materialize_handoff
from athena.research.clarification.models import ClarificationDraft

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "clarification"


def test_materialized_handoff_matches_returned_text(tmp_path: Path) -> None:
    payload = json.loads((FIXTURES / "draft_ready.json").read_text(encoding="utf-8"))
    draft = ClarificationDraft.model_validate(payload)
    path = tmp_path / "handoffs" / "TASK_CLARIFICATION.md"

    text = materialize_handoff(path, draft)

    assert path.read_text(encoding="utf-8") == text
    assert "Outcome: cancelled" in text
