"""Pure clarification transition tests."""

from datetime import UTC, datetime, timedelta

from athena.core.human_request import HumanOutcome, HumanRequest
from athena.research.clarification.state import new_draft, set_pending, settle_answer


def test_settled_answer_updates_understanding_without_mutating_input() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    draft = new_draft("predict churn", "s-1", "draft-1", now)
    request = HumanRequest(
        request_id="req-1",
        session_id="s-1",
        scope_id="draft-1",
        scope_kind="clarification",
        prompt="Which dataset?",
        choices=[],
        allow_custom=True,
        allow_skip=True,
        created_at=now,
        expires_at=now + timedelta(minutes=2),
    )
    pending = set_pending(draft, request, now)
    settled = settle_answer(
        pending,
        request,
        HumanOutcome(
            request_id="req-1", kind="text", value="churn.csv", settled_at=now
        ),
        now,
        field="dataset",
    )

    assert draft.understanding.dataset is None
    assert settled.understanding.dataset == "churn.csv"
    assert settled.answers[0].outcome == "text"
