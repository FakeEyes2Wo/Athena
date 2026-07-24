"""app_server 自检 — ``python -m athena.app_server``。"""

import asyncio
from athena.app_server.submissions import InterruptTurn, StartTurn, Submission
from athena.app_server.thread_runtime import ThreadRuntime


async def _example_runner(thread, turn, emit):
    await emit("message", f"artifact://events/{turn.turn_id}/started")
    if turn.request_ref == "request://fail":
        raise LookupError("simulated failure")
    if turn.request_ref == "request://slow":
        await asyncio.sleep(10)
        return ("result://slow", "context://slow")
    await emit("message", f"artifact://events/{turn.turn_id}/done")
    return (f"result://{turn.turn_id}", f"context://{turn.turn_id}")


async def _test_basic_submit_complete():
    print("  P2-1: basic submit & complete...", end=" ")
    rt = ThreadRuntime("t1", "s1", "ctx://init", _example_runner)
    await rt.start()
    rt.submission_queue.put_nowait(
        Submission(
            id="turn-1", op=StartTurn(turn_id="turn-1", request_ref="request://ok")
        )
    )
    events = []
    async for evt in rt.journal.read_from(0):
        events.append(evt.kind)
        if evt.kind == "turn_completed":
            break
    assert "turn_started" in events and "turn_completed" in events
    assert rt.state == "idle" and rt.active_turn is None
    await rt.force_close()
    print("PASS")
    return True


async def _test_runner_failure():
    print("  P2-2: runner failure...", end=" ")
    rt = ThreadRuntime("t2", "s1", "ctx://init", _example_runner)
    await rt.start()
    rt.submission_queue.put_nowait(
        Submission(
            id="turn-2", op=StartTurn(turn_id="turn-2", request_ref="request://fail")
        )
    )
    events = []
    async for evt in rt.journal.read_from(0):
        events.append(evt.kind)
        if evt.kind == "turn_failed":
            break
    assert "turn_failed" in events and rt.state == "idle"
    assert rt.context_ref == "ctx://init"
    await rt.force_close()
    print("PASS")
    return True


async def _test_interrupt():
    print("  P2-3: interrupt running turn...", end=" ")
    rt = ThreadRuntime("t3", "s1", "ctx://init", _example_runner)
    await rt.start()
    rt.submission_queue.put_nowait(
        Submission(
            id="turn-3", op=StartTurn(turn_id="turn-3", request_ref="request://slow")
        )
    )
    await asyncio.sleep(0.1)
    rt.submission_queue.put_nowait(
        Submission(id="int-1", op=InterruptTurn(turn_id="turn-3", reason="test"))
    )
    events = []
    async for evt in rt.journal.read_from(0):
        events.append(evt.kind)
        if evt.kind == "turn_interrupted":
            break
    assert "turn_interrupted" in events and rt.state == "idle"
    await rt.force_close()
    print("PASS")
    return True


async def _test_emit_rejected_after_terminal():
    print("  P2-4: emit rejected after terminal...", end=" ")
    rt = ThreadRuntime("t4", "s1", "ctx://init", _example_runner)
    await rt.start()
    rt.submission_queue.put_nowait(
        Submission(
            id="turn-4", op=StartTurn(turn_id="turn-4", request_ref="request://fail")
        )
    )
    async for evt in rt.journal.read_from(0):
        if evt.kind == "turn_failed":
            break
    try:
        async with rt.journal.condition:
            if rt.active_turn is None:
                raise RuntimeError("no active turn")
        assert False, "should have raised"
    except RuntimeError as e:
        assert "no active turn" in str(e)
    await rt.force_close()
    print("PASS")
    return True


async def _test_concurrent_threads():
    print("  P2-5: concurrent threads...", end=" ")

    async def _slow_runner(thread, turn, emit):
        await emit("message", f"artifact://{turn.turn_id}/start")
        await asyncio.sleep(0.2)
        await emit("message", f"artifact://{turn.turn_id}/end")
        return (f"result://{turn.turn_id}", f"context://{turn.turn_id}")

    rt1 = ThreadRuntime("tA", "s1", "ctx://a", _slow_runner)
    rt2 = ThreadRuntime("tB", "s1", "ctx://b", _slow_runner)
    await rt1.start()
    await rt2.start()
    rt1.submission_queue.put_nowait(
        Submission(id="ta-1", op=StartTurn(turn_id="ta-1", request_ref="req://a"))
    )
    rt2.submission_queue.put_nowait(
        Submission(id="tb-1", op=StartTurn(turn_id="tb-1", request_ref="req://b"))
    )

    async def _wait_completed(rt):
        async for evt in rt.journal.read_from(0):
            if evt.kind == "turn_completed":
                return True

    await asyncio.wait_for(
        asyncio.gather(_wait_completed(rt1), _wait_completed(rt2)), timeout=2.0
    )
    await rt1.force_close()
    await rt2.force_close()
    print("PASS")
    return True


async def _test_shutdown():
    print("  P2-6: graceful shutdown...", end=" ")
    rt = ThreadRuntime("t6", "s1", "ctx://init", _example_runner)
    await rt.start()
    rt.submission_queue.put_nowait(
        Submission(
            id="turn-6", op=StartTurn(turn_id="turn-6", request_ref="request://slow")
        )
    )
    await asyncio.sleep(0.05)
    await rt.shutdown("test_shutdown")
    assert rt.state in ("closing", "closed")
    async for evt in rt.journal.read_from(0):
        if evt.kind == "turn_interrupted":
            break
    print("PASS")
    return True


async def _test_event_journal_multi_subscriber():
    print("  P4-1: multi-subscriber replay...", end=" ")
    from athena.app_server.events import Event, EventJournal

    journal = EventJournal("test-thread")
    for i in range(5):
        async with journal.condition:
            journal.append(
                Event(
                    thread_id="test-thread",
                    turn_id="turn-1",
                    sequence=journal.next_sequence(),
                    kind=f"event_{i}",
                    event_ref=f"artifact://{i}",
                )
            )
    sub1 = []
    sub2 = []
    async for evt in journal.read_from(0):
        sub1.append(evt.sequence)
        if len(sub1) >= 5:
            break
    async for evt in journal.read_from(2):
        sub2.append(evt.sequence)
        if len(sub2) >= 3:
            break
    assert sub1 == [1, 2, 3, 4, 5]
    assert sub2 == [3, 4, 5]
    print("PASS")
    return True


async def main():
    print("=== Athena App Server Self-Test ===\n")
    tests = [
        (
            "ThreadRuntime",
            [
                _test_basic_submit_complete,
                _test_runner_failure,
                _test_interrupt,
                _test_emit_rejected_after_terminal,
                _test_concurrent_threads,
                _test_shutdown,
            ],
        ),
        ("EventJournal", [_test_event_journal_multi_subscriber]),
    ]
    passed = failed = 0
    for group_name, group_tests in tests:
        print(f"[{group_name}]")
        for test in group_tests:
            try:
                await test()
                passed += 1
            except Exception as e:
                failed += 1
                print(f"FAIL: {e}")
                import traceback

                traceback.print_exc()
        print()
    print(f"Results: {passed} passed, {failed} failed")
    if failed > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
