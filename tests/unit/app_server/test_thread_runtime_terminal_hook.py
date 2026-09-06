import asyncio

from athena.app_server.submissions import StartTurn, Submission
from athena.app_server.thread_runtime import ThreadRuntime


class _DoneRunner:
    """run_with_context 立即返回终态。"""

    async def run_with_context(self, thread, turn, emit, memory, cancel):
        return _Outcome()


class _Outcome:
    result_ref = "art:result"
    next_context_ref = "art:ctx"


async def test_terminal_hook_fires_on_completion():
    calls: list[tuple[str, TurnTerminalState]] = []
    rt = ThreadRuntime(
        thread_id="t1",
        session_id="s1",
        context_ref="c1",
        runner=_DoneRunner(),
        on_turn_terminal=lambda tid, ts: calls.append((tid, ts)),
    )
    await rt.start()
    try:
        turn_id = "turn1"
        await rt.submission_queue.put(
            Submission(id=turn_id, op=StartTurn(turn_id=turn_id, request_ref="req1"))
        )
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "on_turn_terminal never fired"
        tid, terminal = calls[0]
        assert tid == turn_id
        assert terminal.result_ref == "art:result"
    finally:
        await rt.force_close()
