import asyncio
import unittest

from athena.app_server.thread_manager import RuntimeThreadManager

from ._support import BlockingRunner, eventually, immediate_runner


class RuntimeThreadManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_runner_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            RuntimeThreadManager(None)

    async def test_start_registers_thread_and_handle(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        try:
            thread = await manager.start("session:1", "artifact:context")
            handle = await manager.get(thread.thread_id)
            current = await manager.get_thread(thread.thread_id)
            self.assertEqual(handle.thread_id, thread.thread_id)
            self.assertEqual(current.session_id, "session:1")
            self.assertEqual(current.context_ref, "artifact:context")
        finally:
            await manager.aclose("test_cleanup")

    async def test_submit_returns_turn_and_events(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        try:
            thread = await manager.start("session:1", "artifact:context")
            turn = await manager.submit(thread.thread_id, "artifact:request")
            self.assertEqual(turn.thread_id, thread.thread_id)
            handle = await manager.get(thread.thread_id)
            await eventually(lambda: handle.state == "idle")
            refs = manager.events(thread.thread_id)
            first = await asyncio.wait_for(anext(refs), timeout=0.1)
            self.assertTrue(first.startswith("athena-event:"))
        finally:
            await manager.aclose("test_cleanup")

    async def test_interrupt_routes_to_thread_handle(self) -> None:
        runner = BlockingRunner()
        manager = RuntimeThreadManager(runner)
        try:
            thread = await manager.start("session:1", "artifact:context")
            turn = await manager.submit(thread.thread_id, "artifact:request")
            await asyncio.wait_for(runner.started.wait(), timeout=0.1)
            await manager.interrupt(thread.thread_id, turn.turn_id, "test")
            handle = await manager.get(thread.thread_id)
            self.assertEqual(handle.state, "idle")
        finally:
            runner.release.set()
            await manager.aclose("test_cleanup")

    async def test_fork_uses_completed_turn_context(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        try:
            parent = await manager.start("session:1", "artifact:context")
            turn = await manager.submit(parent.thread_id, "artifact:request")
            parent_handle = await manager.get(parent.thread_id)
            await eventually(lambda: parent_handle.state == "idle")
            child = await manager.fork(parent.thread_id, after_turn_id=turn.turn_id)
            self.assertEqual(child.session_id, parent.session_id)
            self.assertEqual(child.context_ref, "artifact:next-context")
            self.assertNotEqual(child.thread_id, parent.thread_id)
        finally:
            await manager.aclose("test_cleanup")

    async def test_invalid_references_are_rejected(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        try:
            with self.assertRaises(ValueError):
                await manager.start("", "artifact:context")
            with self.assertRaises(ValueError):
                await manager.start("session:1", "")
            with self.assertRaises(KeyError):
                await manager.get("thread:missing")
        finally:
            await manager.aclose("test_cleanup")

    async def test_close_is_sequentially_idempotent(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        await manager.start("session:1", "artifact:context")
        await manager.aclose("first")
        await manager.aclose("second")
        self.assertEqual(manager.state, "closed")
