"""Unit tests for the ThreadManager submission loop."""

import asyncio
import unittest

from athena.execution.handlers import Submission, SubmissionOp, submission_loop


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_submit = False

    async def start(self, *args: object) -> object:
        return self._result("start", args)

    async def submit(self, *args: object) -> object:
        if self.fail_submit:
            raise RuntimeError("submit failed")
        return self._result("submit", args)

    async def fork(self, *args: object) -> object:
        return self._result("fork", args)

    async def interrupt(self, *args: object) -> None:
        self.calls.append(("interrupt", args))

    def events(self, *args: object):
        self.calls.append(("events", args))

        async def stream():
            yield "event://one"

        return stream()

    def _result(self, operation: str, args: tuple[object, ...]) -> object:
        self.calls.append((operation, args))
        return (operation, args)


class SubmissionLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_routes_operations_and_stops_on_shutdown(self) -> None:
        manager = FakeManager()
        queue: asyncio.Queue[Submission | None] = asyncio.Queue()
        loop_task = asyncio.create_task(submission_loop(manager, queue))

        start = self._submission("start", "session", "context")
        submit = self._submission("submit", "thread", "request")
        fork = self._submission("fork", "thread")
        interrupt = self._submission("interrupt", "thread", "turn", "stop")
        events = self._submission("events", "thread")
        shutdown = self._submission("shutdown")
        for submission in (start, submit, fork, interrupt, events, shutdown):
            queue.put_nowait(submission)

        self.assertEqual(("start", ("session", "context")), await start.future)
        self.assertEqual(("submit", ("thread", "request")), await submit.future)
        self.assertEqual(("fork", ("thread",)), await fork.future)
        self.assertIsNone(await interrupt.future)
        self.assertEqual("event://one", await anext(await events.future))
        self.assertIsNone(await shutdown.future)
        await loop_task
        await queue.join()

        self.assertEqual(
            ["start", "submit", "fork", "interrupt", "events"],
            [operation for operation, _ in manager.calls],
        )

    async def test_forwards_error_and_keeps_processing(self) -> None:
        manager = FakeManager()
        manager.fail_submit = True
        queue: asyncio.Queue[Submission | None] = asyncio.Queue()
        loop_task = asyncio.create_task(submission_loop(manager, queue))
        failed = self._submission("submit", "thread", "request")
        started = self._submission("start", "session", "context")
        queue.put_nowait(failed)
        queue.put_nowait(started)
        queue.put_nowait(None)

        with self.assertRaisesRegex(RuntimeError, "submit failed"):
            await failed.future
        self.assertEqual(("start", ("session", "context")), await started.future)
        await loop_task
        await queue.join()

    def _submission(self, op: SubmissionOp, *args: object) -> Submission:
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        return Submission(op=op, args=args, future=future)
