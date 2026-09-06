"""Schema tests for durable clarification drafts."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.research.clarification.models import (
    ClarificationAnswer,
    ClarificationDraft,
    DraftUnderstanding,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "clarification"


def _ready_payload() -> dict:
    return json.loads((FIXTURES / "draft_ready.json").read_text(encoding="utf-8"))


def test_draft_fixture_loads_and_retains_answers() -> None:
    draft = ClarificationDraft.model_validate(_ready_payload())
    assert draft.draft_id == "draft-1"
    assert draft.revision == 7
    assert draft.status == "READY_FOR_CONFIRMATION"
    assert [answer.outcome for answer in draft.answers] == [
        "choice",
        "skip",
        "timeout",
        "cancelled",
    ]
    assert draft.unresolved[0].critical is True


def test_draft_rejects_invalid_pending_scope_and_status_failure_consistency() -> None:
    payload = _ready_payload()
    payload["pending_request"] = {
        "request_id": "req-5",
        "session_id": "s-2",
        "scope_id": "other",
        "scope_kind": "clarification",
        "prompt": "q",
        "choices": [],
        "allow_custom": True,
        "allow_skip": True,
        "created_at": "2026-09-01T10:00:00Z",
        "expires_at": "2026-09-01T10:02:00Z",
    }
    with pytest.raises(ValidationError):
        ClarificationDraft.model_validate(payload)

    payload = _ready_payload()
    payload["status"] = "FAILED"
    with pytest.raises(ValidationError):
        ClarificationDraft.model_validate(payload)


def test_draft_rejects_question_count_above_eight() -> None:
    payload = _ready_payload()
    payload["questions_asked"] = 9
    with pytest.raises(ValidationError):
        ClarificationDraft.model_validate(payload)


def test_unknown_metric_and_direction_remain_none() -> None:
    understanding = DraftUnderstanding(
        title="t",
        dataset=None,
        target=None,
        task_type="classification",
        primary_metric=None,
        direction=None,
        evaluation_plan=None,
    )
    assert understanding.primary_metric is None
    assert understanding.direction is None


def test_answers_accept_server_generated_outcomes() -> None:
    for outcome in ("choice", "text", "skip", "timeout", "cancelled"):
        answer = ClarificationAnswer(
            request_id="r",
            question="q",
            outcome=outcome,
            value="v" if outcome in ("choice", "text") else None,
        )
        assert answer.outcome == outcome
