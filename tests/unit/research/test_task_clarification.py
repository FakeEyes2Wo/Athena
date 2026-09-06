"""Hermetic tests for Supervisor-owned task clarification handoff writing.

Task 1/2 also cover the typed human-request broker contract used by the
clarification controller.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from athena.core.human_request import (
    ChoiceReply,
    HumanChoice,
    HumanContractError,
    HumanRequest,
)
from gui_gateway.human import HumanRequestBroker


def _request(
    broker: HumanRequestBroker,
    *,
    request_id: str = "req-1",
    session_id: str = "s1",
) -> HumanRequest:
    now = datetime(2026, 9, 1, 10, tzinfo=UTC)
    return HumanRequest(
        request_id=request_id,
        session_id=session_id,
        scope_id="clarify-1",
        scope_kind="clarification",
        prompt="Choose metric",
        choices=[
            HumanChoice(label="F1", value="f1"),
            HumanChoice(label="AUC", value="auc"),
        ],
        allow_custom=True,
        allow_skip=True,
        created_at=now,
        expires_at=now.replace(minute=2),
    )


# ── Task 2: session-scoped broker ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_session_duplicate_and_cancel_are_typed() -> None:
    broker = HumanRequestBroker(clock=lambda: datetime(2026, 9, 1, 9, 59, tzinfo=UTC))
    request = _request(broker)
    waiter = asyncio.create_task(broker.ask(request))
    await asyncio.sleep(0)

    with pytest.raises(HumanContractError, match="cross_session"):
        await broker.reply("s2", "req-1", ChoiceReply(kind="choice", value="f1"))

    outcomes = await broker.cancel_scope("s1", "clarify-1")
    assert outcomes[0].kind == "cancelled"
    assert (await waiter).kind == "cancelled"


@pytest.mark.asyncio
async def test_invalid_choice_expired_duplicate_and_pending_isolation() -> None:
    current = {"now": datetime(2026, 9, 1, 10, tzinfo=UTC)}
    broker = HumanRequestBroker(clock=lambda: current["now"])
    request = _request(broker, request_id="req-1")
    task = asyncio.create_task(broker.ask(request))
    await asyncio.sleep(0)

    with pytest.raises(HumanContractError, match="invalid_choice"):
        await broker.reply("s1", "req-1", ChoiceReply(kind="choice", value="nope"))
    # First reply succeeds; duplicate is typed.
    outcome = await broker.reply("s1", "req-1", ChoiceReply(kind="choice", value="f1"))
    assert outcome.kind == "choice"
    assert outcome.choice_label == "F1"
    with pytest.raises(HumanContractError, match="duplicate_reply"):
        await broker.reply("s1", "req-1", ChoiceReply(kind="choice", value="f1"))
    assert (await task).kind == "choice"

    expired = request.model_copy(
        update={
            "request_id": "req-expired",
            "expires_at": datetime(2026, 9, 1, 10, 2, tzinfo=UTC),
        }
    )
    task = asyncio.create_task(broker.ask(expired))
    await asyncio.sleep(0)
    current["now"] = datetime(2026, 9, 1, 10, 3, tzinfo=UTC)
    with pytest.raises(HumanContractError, match="expired_request"):
        await broker.reply("s1", "req-expired", ChoiceReply(kind="choice", value="f1"))
    await broker.cancel_session("s1")
    assert (await task).kind == "cancelled"

    # pending() is session-scoped.
    current["now"] = datetime(2026, 9, 1, 10, tzinfo=UTC)
    other = _request(broker, request_id="req-2", session_id="s2")
    other_task = asyncio.create_task(broker.ask(other))
    await asyncio.sleep(0)
    assert [item.request_id for item in await broker.pending("s1")] == []
    assert [item.request_id for item in await broker.pending("s2")] == ["req-2"]
    await broker.cancel_session("s2")
    assert (await other_task).kind == "cancelled"


@pytest.mark.asyncio
async def test_timeout_is_server_generated_outcome() -> None:
    broker = HumanRequestBroker(
        clock=lambda: datetime(2026, 9, 1, 10, 0, 2, tzinfo=UTC)
    )
    request = _request(broker, request_id="req-1")
    request = request.model_copy(
        update={
            "expires_at": datetime(2026, 9, 1, 10, 0, 1, tzinfo=UTC),
            "created_at": datetime(2026, 9, 1, 10, tzinfo=UTC),
        }
    )

    outcome = await broker.ask(request)
    assert outcome.kind == "timeout"
