import asyncio
import unittest
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.app_server.submissions import GetForkSnapshot, InterruptTurn, StartTurn
from athena.app_server.thread_runtime import ThreadHandle, ThreadRuntime
from athena.core.agent import AgentContext, AgentOutcome, BaseAgent, agent_runner
from athena.core.tool import ToolRegistry
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
            self.assertEqual(
                [event.kind for event in runtime.journal._records],
                ["turn_started", "item", "turn_completed"],
            )
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
        finally:
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
