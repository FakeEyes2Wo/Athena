import asyncio
import unittest
from pathlib import Path

import pytest

from athena.app_server.thread_manager import RuntimeThreadManager
from athena.core.agent.types import AgentSpec, JsonCodec

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

    async def test_duplicate_explicit_thread_id_keeps_original_runtime(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        try:
            first = await manager.start(
                "session:1", "artifact:context", thread_id="thread:fixed"
            )
            original = await manager.get(first.thread_id)
            with self.assertRaisesRegex(RuntimeError, "thread already exists"):
                await manager.start(
                    "session:2", "artifact:other", thread_id="thread:fixed"
                )
            self.assertIs(await manager.get(first.thread_id), original)
            await manager.aclose("test_cleanup")
            self.assertEqual(original.state, "closed")
        finally:
            await manager.aclose("test_cleanup")

    async def test_close_waits_for_start_to_register_runtime(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        runtime_ready = asyncio.Event()
        allow_start = asyncio.Event()
        original_make = manager._make_runtime
        runtimes = []

        def delayed_runtime(*args, **kwargs):
            runtime = original_make(*args, **kwargs)
            runtimes.append(runtime)
            original_start = runtime.start

            async def delayed_start():
                runtime_ready.set()
                await allow_start.wait()
                await original_start()

            runtime.start = delayed_start
            return runtime

        manager._make_runtime = delayed_runtime
        start_task = asyncio.create_task(manager.start("session:1", "artifact:context"))
        try:
            await asyncio.wait_for(runtime_ready.wait(), timeout=1)
            close_task = asyncio.create_task(manager.aclose("race"))
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())
            allow_start.set()
            await start_task
            await close_task
            self.assertEqual(runtimes[0].state, "closed")
        finally:
            allow_start.set()
            if not start_task.done():
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            await manager.aclose("test_cleanup")

    async def test_cancelled_start_closes_unregistered_runtime(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        runtime_started = asyncio.Event()
        hold_start = asyncio.Event()
        original_make = manager._make_runtime
        runtimes = []

        def held_runtime(*args, **kwargs):
            runtime = original_make(*args, **kwargs)
            runtimes.append(runtime)
            original_start = runtime.start

            async def held_start():
                await original_start()
                runtime_started.set()
                await hold_start.wait()

            runtime.start = held_start
            return runtime

        manager._make_runtime = held_runtime
        task = asyncio.create_task(manager.start("session:1", "artifact:context"))
        try:
            await asyncio.wait_for(runtime_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(runtimes[0].state, "closed")
            self.assertFalse(manager._handles)
        finally:
            hold_start.set()
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

    async def test_wait_turn_delegates_to_thread_handle(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        try:
            thread = await manager.start("session:1", "artifact:context")
            turn = await manager.submit(thread.thread_id, "artifact:request")

            self.assertEqual(
                await manager.wait_turn(thread.thread_id, turn.turn_id),
                "artifact:result",
            )
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


async def test_research_runtime_writes_agent_recovery_log(
    tmp_path: Path,
) -> None:
    from athena.research.runtime import ResearchRuntime

    class EchoRunner:
        async def run(self, request, *, session, emit):
            return {"echo": request}

    runtime = ResearchRuntime(project_root=tmp_path)
    try:
        runtime.registry.register(
            "echo",
            lambda _agent_id: AgentSpec(runner=EchoRunner(), codec=JsonCodec()),
        )
        _, run_id = await runtime.agents.create_root(
            "echo", {"content": "first"}, agent_id="hyp_vit"
        )
        await runtime.agents.wait_run(run_id, timeout=5)

        assert (tmp_path / ".athena" / "logs" / "agents" / "hyp_vit.jsonl").is_file()
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "agent_id",
    [
        "../x",
        "a/b",
        "a\\b",
        ".",
        "..",
        "a<b",
        "a>b",
        "a:b",
        'a"b',
        "a|b",
        "a?b",
        "a*b",
        "a\x01b",
        "name.",
        "name ",
        "CON",
        "con.txt",
        "PRN",
        "aux.csv",
        "NUL",
        "COM1",
        "com9.log",
        "LPT1",
        "lpt9.txt",
    ],
)
async def test_rollout_rejects_agent_id_that_is_not_a_safe_basename(
    tmp_path: Path, agent_id: str
) -> None:
    rollout_dir = tmp_path / "logs" / "agents"
    manager = RuntimeThreadManager(immediate_runner, rollout_dir=rollout_dir)
    try:
        with pytest.raises(ValueError, match="safe path basename"):
            await manager.start(agent_id, "artifact:context", thread_id=agent_id)

        assert not rollout_dir.exists()
        assert list(tmp_path.rglob("*.jsonl")) == []
    finally:
        await manager.aclose("test_cleanup")
