import asyncio
import unittest
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.app_server.submissions import GetForkSnapshot, InterruptTurn, StartTurn
from athena.app_server.thread_runtime import ThreadHandle, ThreadRuntime
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent, agent_runner
from athena.core.tool import ToolRegistry
from athena.execution import MonitorLimits
from athena.memory import Compactor, ContextManager

from ._support import BlockingRunner, eventually, immediate_runner


class ThreadRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_runner_uses_runtime_memory_after_compaction(self) -> None:
        class MemoryAgent(BaseAgent):
            name = "memory"
            description = "memory"

            def __init__(self) -> None:
                self.memory = None

            async def run(self, ctx: AgentContext) -> AgentOutcome:
                self.memory = ctx.memory
                ctx.memory.append(
                    ModelRequest(parts=[UserPromptPart(content="new turn item")])
                )
                return AgentOutcome("artifact:result", "artifact:next-context")

        class AlwaysCompactor(Compactor):
            def should_compact(self, ctx, at_tokens=170_000):
                return True

        class FakeRollout:
            def __init__(self) -> None:
                self.records = []

            async def open(self, _thread_id):
                return None

            async def close(self):
                return None

            def record_compaction(self, _version, _summary):
                return None

            def record(self, message):
                self.records.append(message)

        class FakeMessages:
            async def create(self, **_kwargs):
                return SimpleNamespace(
                    content=[SimpleNamespace(text="compact summary")]
                )

        ctx = ContextManager()
        ctx.append(ModelRequest(parts=[UserPromptPart(content="old item")]))
        ctx.append(ModelRequest(parts=[UserPromptPart(content="recent item")]))
        agent = MemoryAgent()
        rollout = FakeRollout()
        runtime = ThreadRuntime(
            "thread:1",
            "session:1",
            "artifact:context",
            agent_runner(agent, ToolRegistry()),
            ctx=ctx,
            compactor=AlwaysCompactor(keep_recent=1),
            rollout=rollout,
            llm=SimpleNamespace(messages=FakeMessages()),
        )
        await runtime.start()
        try:
            await ThreadHandle(runtime).submit(StartTurn("turn:1", "artifact:request"))
            await eventually(lambda: runtime.state == "idle")

            self.assertIs(agent.memory, ctx)
            self.assertEqual(runtime.last_terminal_kind, "turn_completed")
            self.assertEqual(rollout.records[-1].parts[0].content, "new turn item")
        finally:
            await runtime.force_close()

    async def test_completed_turn_updates_context_and_journal(self) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            turn = await handle.submit(StartTurn("turn:1", "artifact:request"))
            self.assertEqual(turn.turn_id, "turn:1")
            await eventually(lambda: runtime.state == "idle")
            self.assertEqual(runtime.context_ref, "artifact:next-context")
            native_kinds = [
                event.kind
                for event in runtime.journal._records
                if event.kind != "execution/health"
            ]
            health_states = [
                event.data["state"]
                for event in runtime.journal._records
                if event.kind == "execution/health"
            ]
            self.assertEqual(native_kinds, ["turn_started", "item", "turn_completed"])
            self.assertEqual(health_states, ["RUNNING", "COMPLETED"])
        finally:
            await runtime.force_close()

    async def test_wait_turn_returns_result_after_fast_turn_returns_to_idle(
        self,
    ) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))
            await eventually(lambda: runtime.state == "idle")

            self.assertEqual(await handle.wait_turn("turn:1"), "artifact:result")
        finally:
            await runtime.force_close()

    async def test_wait_turn_raises_for_failed_runner(self) -> None:
        async def failing_runner(thread, turn, emit):
            del thread, turn, emit
            raise RuntimeError("analysis.py failed: TypeError: labels")

        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", failing_runner
        )
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))

            with self.assertRaisesRegex(
                RuntimeError, "RuntimeError: analysis.py failed: TypeError: labels"
            ):
                await handle.wait_turn("turn:1")
        finally:
            await runtime.force_close()

    async def test_wait_turn_raises_cancelled_for_interrupted_runner(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", runner)
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))
            await asyncio.wait_for(runner.started.wait(), timeout=0.1)
            await handle.submit(InterruptTurn("turn:1", "test"))

            with self.assertRaises(asyncio.CancelledError):
                await handle.wait_turn("turn:1")
        finally:
            runner.release.set()
            await runtime.force_close()

    async def test_wait_turn_can_be_awaited_again_after_completion(self) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))

            self.assertEqual(await handle.wait_turn("turn:1"), "artifact:result")
            self.assertEqual(await handle.wait_turn("turn:1"), "artifact:result")
        finally:
            await runtime.force_close()

    async def test_runner_failure_preserves_context(self) -> None:
        async def failing_runner(thread, turn, emit):
            del thread, turn, emit
            raise LookupError("failed")

        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", failing_runner
        )
        await runtime.start()
        try:
            await ThreadHandle(runtime).submit(StartTurn("turn:1", "artifact:request"))
            await eventually(lambda: runtime.state == "idle")
            self.assertEqual(runtime.context_ref, "artifact:context")
            self.assertEqual(runtime.last_terminal_kind, "turn_failed")
            self.assertEqual(
                [
                    event.data["state"]
                    for event in runtime.journal._records
                    if event.kind == "execution/health"
                ],
                ["RUNNING", "FAILED"],
            )
        finally:
            await runtime.force_close()

    async def test_runner_failure_keeps_rollout_in_sync_with_context(self) -> None:
        class FailingMemoryRunner:
            async def run_with_context(self, thread, turn, emit, ctx, cancel):
                del thread, turn, emit, cancel
                ctx.append(
                    ModelRequest(parts=[UserPromptPart(content="partial turn item")])
                )
                raise LookupError("failed")

        class FakeRollout:
            def __init__(self) -> None:
                self.records = []

            async def open(self, _thread_id):
                return None

            async def close(self):
                return None

            def record(self, message):
                self.records.append(message)

        ctx = ContextManager()
        rollout = FakeRollout()
        runtime = ThreadRuntime(
            "thread:1",
            "session:1",
            "artifact:context",
            FailingMemoryRunner(),
            ctx=ctx,
            rollout=rollout,
        )
        await runtime.start()
        try:
            await ThreadHandle(runtime).submit(StartTurn("turn:1", "artifact:request"))
            await eventually(lambda: runtime.state == "idle")

            self.assertEqual(runtime.last_terminal_kind, "turn_failed")
            self.assertEqual(rollout.records, [])
            self.assertEqual(ctx.snapshot()[0], 0)
        finally:
            await runtime.force_close()

    async def test_stalled_health_does_not_cancel_active_runner(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime(
            "thread:1",
            "session:1",
            "artifact:context",
            runner,
            monitor_limits=MonitorLimits(stalled_after=0.01, timeout_after=1),
            monitor_scan_interval=0.001,
        )
        await runtime.start()
        try:
            await ThreadHandle(runtime).submit(StartTurn("turn:1", "artifact:request"))
            await asyncio.wait_for(runner.started.wait(), timeout=0.1)
            await eventually(
                lambda: "STALLED"
                in [
                    event.data["state"]
                    for event in runtime.journal._records
                    if event.kind == "execution/health"
                ]
            )

            self.assertEqual(runtime.state, "running")
            self.assertFalse(runtime.active_turn.runner_task.done())

            runner.release.set()
            await eventually(lambda: runtime.state == "idle")
            self.assertEqual(
                [
                    event.data["state"]
                    for event in runtime.journal._records
                    if event.kind == "execution/health"
                ],
                ["RUNNING", "STALLED", "COMPLETED"],
            )
        finally:
            runner.release.set()
            await runtime.force_close()

    async def test_timeout_health_allows_late_runner_completion(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime(
            "thread:1",
            "session:1",
            "artifact:context",
            runner,
            monitor_limits=MonitorLimits(stalled_after=1, timeout_after=0.01),
            monitor_scan_interval=0.001,
        )
        await runtime.start()
        try:
            await ThreadHandle(runtime).submit(StartTurn("turn:1", "artifact:request"))
            await asyncio.wait_for(runner.started.wait(), timeout=0.1)
            await eventually(
                lambda: "TIMEOUT"
                in [
                    event.data["state"]
                    for event in runtime.journal._records
                    if event.kind == "execution/health"
                ]
            )

            self.assertEqual(runtime.state, "running")
            self.assertFalse(runtime.active_turn.runner_task.done())

            runner.release.set()
            await eventually(lambda: runtime.state == "idle")
            self.assertEqual(
                [
                    event.data["state"]
                    for event in runtime.journal._records
                    if event.kind == "execution/health"
                ],
                ["RUNNING", "TIMEOUT", "COMPLETED"],
            )
        finally:
            runner.release.set()
            await runtime.force_close()

    async def test_interrupt_is_committed_before_submit_returns(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", runner)
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))
            await asyncio.wait_for(runner.started.wait(), timeout=0.1)
            await handle.submit(InterruptTurn("turn:1", "test"))
            self.assertEqual(runtime.journal._records[-1].kind, "turn_interrupted")
            self.assertEqual(runtime.state, "idle")
        finally:
            runner.release.set()
            await runtime.force_close()

    async def test_handle_events_exposes_journal_stream(self) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))
            events = handle.events(after_sequence=0)
            first = await asyncio.wait_for(anext(events), timeout=0.1)
            self.assertEqual(first.kind, "turn_started")
            self.assertFalse(hasattr(handle, "journal"))
        finally:
            await runtime.force_close()

    async def test_completed_snapshot_uses_requested_turn(self) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        await runtime.start()
        handle = ThreadHandle(runtime)
        try:
            await handle.submit(StartTurn("turn:1", "artifact:request"))
            await eventually(lambda: runtime.state == "idle")
            snapshot = await handle.submit(GetForkSnapshot(after_turn_id="turn:1"))
            self.assertEqual(snapshot, "artifact:next-context")
            with self.assertRaises(KeyError):
                runtime.completed_snapshot("turn:missing")
        finally:
            await runtime.force_close()

    async def test_shutdown_interrupts_active_turn_and_stays_closing(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", runner)
        await runtime.start()
        await ThreadHandle(runtime).submit(StartTurn("turn:1", "artifact:request"))
        await asyncio.wait_for(runner.started.wait(), timeout=0.1)
        await asyncio.wait_for(runtime.shutdown("test_shutdown"), timeout=0.1)
        self.assertEqual(runtime.state, "closed")
        self.assertEqual(runtime.journal._records[-1].kind, "turn_interrupted")

    async def test_force_close_sets_closed(self) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        await runtime.start()
        await runtime.force_close()
        self.assertEqual(runtime.state, "closed")

    async def test_force_close_cancels_existing_wait_turn_waiter(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", runner)
        await runtime.start()
        handle = ThreadHandle(runtime)
        await handle.submit(StartTurn("turn:1", "artifact:request"))
        await asyncio.wait_for(runner.started.wait(), timeout=0.1)
        waiter = asyncio.create_task(handle.wait_turn("turn:1"))
        await asyncio.sleep(0)

        await runtime.force_close()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=0.1)
        self.assertTrue(runtime._turn_done["turn:1"].done())
        self.assertIsNone(runtime.active_turn)

    async def test_handle_properties_reflect_runtime(self) -> None:
        runtime = ThreadRuntime(
            "thread:1", "session:1", "artifact:context", immediate_runner
        )
        handle = ThreadHandle(runtime)
        self.assertEqual(handle.state, "idle")
        self.assertEqual(handle.context_ref, "artifact:context")

    async def test_second_active_turn_is_not_started_concurrently(self) -> None:
        runner = BlockingRunner()
        runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", runner)
        await runtime.start()
        handle = ThreadHandle(runtime)
        first = await handle.submit(StartTurn("turn:1", "artifact:request:1"))
        await asyncio.wait_for(runner.started.wait(), timeout=0.1)
        error: BaseException | None = None
        try:
            try:
                await handle.submit(StartTurn("turn:2", "artifact:request:2"))
            except BaseException as exc:
                error = exc
            self.assertEqual(first.turn_id, "turn:1")
            self.assertIsNotNone(error)
            self.assertEqual(runner.entries, 1)
        finally:
            runner.release.set()
            await eventually(lambda: runtime.state == "idle")
            await runtime.force_close()
