"""Production ResearchRuntime clarification RPC wiring tests.

These tests use a real ``ResearchRuntime`` (not a fake) with a stub broker to
ensure the GUI-facing RPC methods are actually present and work end-to-end at
the runtime boundary.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from athena.core.human_request import HumanOutcome
from athena.research import ResearchRuntime


class StubBroker:
    def __init__(self) -> None:
        self.asked: list[object] = []

    async def ask(self, request):
        self.asked.append(request)
        return HumanOutcome(
            request_id=request.request_id,
            kind="text",
            value="answer",
            settled_at=datetime.now(UTC),
        )

    async def cancel_scope(self, session_id: str, scope_id: str):
        return []


@pytest.mark.asyncio
async def test_real_runtime_exposes_clarification_rpc(tmp_path: Path) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        session_id="s1",
        broker=StubBroker(),
        task_confirmation_gate=True,
        auto_confirm=False,
    )
    try:
        draft = await runtime.task_clarification_start("predict churn")
        assert draft.status == "READY_FOR_CONFIRMATION"
        assert draft.questions_asked == 4

        got = await runtime.task_clarification_get(draft.draft_id)
        assert got.draft_id == draft.draft_id
        assert got.revision == draft.revision

        cancelled = await runtime.task_clarification_cancel(
            draft.draft_id, draft.revision
        )
        assert cancelled.status == "CANCELLED"

        # Cancellation clears the previous draft so a different task can start.
        again = await runtime.task_clarification_start("another task")
        assert again.status == "READY_FOR_CONFIRMATION"
    finally:
        await runtime.aclose()
