import unittest
from types import SimpleNamespace

from athena.app_server.execution import ExecutionAdapter
from athena.app_server.protocol import Method
from athena.app_server.submissions import InterruptTurn, StartTurn
from athena.core.schemas import AthenaThread, AthenaTurn


class FakeHandle:
    def __init__(self, thread_id: str = "thread:1") -> None:
        self.thread_id = thread_id
        self.ops = []

    async def submit(self, op):
        self.ops.append(op)
        if isinstance(op, StartTurn):
            return AthenaTurn(
                turn_id=op.turn_id,
                thread_id=self.thread_id,
                request_ref=op.request_ref,
                status="running",
            )
        return None


class FakeManager:
    def __init__(self) -> None:
        self.handle = FakeHandle()
        self.started = []
        self.forked = []

    async def start(self, session_id: str, context_ref: str) -> AthenaThread:
        self.started.append((session_id, context_ref))
        return AthenaThread(
            thread_id=self.handle.thread_id,
            session_id=session_id,
            context_ref=context_ref,
            status="idle",
        )

    async def get(self, thread_id: str) -> FakeHandle:
        if thread_id != self.handle.thread_id:
            raise KeyError(thread_id)
        return self.handle

    async def fork(
        self, thread_id: str, after_turn_id: str | None = None
    ) -> AthenaThread:
        self.forked.append((thread_id, after_turn_id))
        return AthenaThread(
            thread_id="thread:child",
            session_id="session:1",
            context_ref="artifact:context",
            status="idle",
        )


class FakeHandlers:
    def __init__(self) -> None:
        self.attached = []

    async def attach(self, thread_id: str) -> None:
        self.attached.append(thread_id)


class FakeSubscriptions:
    def __init__(self) -> None:
        self.created = []
        self.removed = []

    async def create(self, thread_id: str, cursor: int):
        self.created.append((thread_id, cursor))
        return SimpleNamespace(subscription_id="sub:1")

    async def remove(self, subscription_id: str) -> None:
        self.removed.append(subscription_id)


class ExecutionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = FakeManager()
        self.handlers = FakeHandlers()
        self.subscriptions = FakeSubscriptions()
        self.adapter = ExecutionAdapter(self.manager, self.handlers, self.subscriptions)

    async def test_thread_start_calls_manager_and_attaches(self) -> None:
        result = await self.adapter.execute(
            Method.THREAD_START,
            {"session_id": "session:1", "context_ref": "artifact:context"},
        )
        self.assertEqual(result, {"thread_id": "thread:1"})
        self.assertEqual(self.manager.started, [("session:1", "artifact:context")])
        self.assertEqual(self.handlers.attached, ["thread:1"])

    async def test_turn_start_submits_current_start_turn_op(self) -> None:
        result = await self.adapter.execute(
            Method.TURN_START,
            {"thread_id": "thread:1", "request_ref": "artifact:request"},
        )
        self.assertEqual(result["turn_id"], self.manager.handle.ops[0].turn_id)
        self.assertIsInstance(self.manager.handle.ops[0], StartTurn)

    async def test_turn_interrupt_submits_current_interrupt_op(self) -> None:
        result = await self.adapter.execute(
            Method.TURN_INTERRUPT,
            {"thread_id": "thread:1", "turn_id": "turn:1", "reason": "test"},
        )
        operation = self.manager.handle.ops[0]
        self.assertIsInstance(operation, InterruptTurn)
        self.assertEqual(operation.reason, "test")
        self.assertEqual(result, {"turn_id": "turn:1", "status": "interrupted"})

    async def test_thread_fork_attaches_child(self) -> None:
        result = await self.adapter.execute(
            Method.THREAD_FORK,
            {"thread_id": "thread:1", "after_turn_id": "turn:1"},
        )
        self.assertEqual(result, {"thread_id": "thread:child"})
        self.assertEqual(self.manager.forked, [("thread:1", "turn:1")])
        self.assertEqual(self.handlers.attached, ["thread:child"])

    async def test_subscribe_and_unsubscribe_use_registry(self) -> None:
        subscribed = await self.adapter.execute(
            Method.THREAD_SUBSCRIBE,
            {"thread_id": "thread:1", "after_sequence": 3},
        )
        unsubscribed = await self.adapter.execute(
            Method.THREAD_UNSUBSCRIBE, {"subscription_id": "sub:1"}
        )
        self.assertEqual(subscribed, {"subscription_id": "sub:1"})
        self.assertEqual(unsubscribed, {"status": "unsubscribed"})
        self.assertEqual(self.subscriptions.created, [("thread:1", 3)])
        self.assertEqual(self.subscriptions.removed, ["sub:1"])

    async def test_unknown_method_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown method"):
            await self.adapter.execute("unknown/method", {})
