"""Queue handlers for ``ThreadManager`` operations."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


class ThreadManager(ABC):
    """ThreadManager 抽象基类 — 定义 start/submit/fork/interrupt/events。"""

    @abstractmethod
    async def start(self, *args: object) -> object: ...
    @abstractmethod
    async def submit(self, *args: object) -> object: ...
    @abstractmethod
    async def fork(self, *args: object) -> object: ...
    @abstractmethod
    async def interrupt(self, *args: object) -> object: ...
    @abstractmethod
    def events(self, *args: object) -> object: ...


SubmissionOp = Literal["start", "submit", "fork", "interrupt", "events", "shutdown"]


@dataclass(slots=True)
class Submission:
    """One manager operation and the Future that receives its outcome."""

    op: SubmissionOp
    args: tuple[object, ...]
    future: asyncio.Future[object]


async def submission_loop(
    manager: ThreadManager,
    submissions: asyncio.Queue[Submission | None],
) -> None:
    """Serially dispatch submissions until shutdown or a ``None`` sentinel."""
    while True:
        submission = await submissions.get()
        try:
            if submission is None:
                return

            try:
                match submission.op:
                    case "start":
                        result = await manager.start(*submission.args)
                    case "submit":
                        result = await manager.submit(*submission.args)
                    case "fork":
                        result = await manager.fork(*submission.args)
                    case "interrupt":
                        result = await manager.interrupt(*submission.args)
                    case "events":
                        result = manager.events(*submission.args)
                    case "shutdown":
                        result = None
                    case _:
                        raise ValueError(f"unsupported submission op: {submission.op}")
            except Exception as exc:
                if not submission.future.done():
                    submission.future.set_exception(exc)
            else:
                if not submission.future.done():
                    submission.future.set_result(result)

            if submission.op == "shutdown":
                return
        finally:
            submissions.task_done()
