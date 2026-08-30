"""Mutable, non-durable runtime state for one Supervisor process."""

import asyncio
from collections.abc import Iterable

from athena.core.workspace import GitWorkBranch


class SupervisorRunState:
    """Transient state that does not belong in ResearchState or ResearchTree.

    This includes in-flight Plan tasks, created worktrees, guidance that has not
    yet been frozen into a Plan, the SEARCH wake event, and the local stop flag.
    """

    def __init__(self, kaggle_download: bool | None) -> None:
        self._branches: dict[str, GitWorkBranch] = {}
        self._next_guidance: str | None = None
        self._persistent_guidance: list[str] = []
        self._running: dict[str, asyncio.Task] = {}
        self._next_hypothesis_id: str | None = None
        self._stopped = False
        # None=关闭, True=接入且下载, False=接入但不下载（任务理解阶段由 SupervisorAgent 决定）。
        self._kaggle_download: bool | None = kaggle_download
        # 手动模式下等待人工选定假设时，唤醒 run_search 循环的信号。
        self._wake = asyncio.Event()
        # SEARCH 调度循环的后台任务（供 WAITING→RUNNING 重入）；首轮由 start() 直接 await。
        self._search_task: asyncio.Task | None = None

    @property
    def running_plan_ids(self) -> tuple[str, ...]:
        """Return currently dispatched Plan IDs."""
        return tuple(self._running)

    @property
    def next_hypothesis_id(self) -> str | None:
        """Return the pending one-shot Human selection, if any."""
        return self._next_hypothesis_id

    @property
    def kaggle_enabled(self) -> bool:
        return self._kaggle_download is not None

    @property
    def kaggle_download(self) -> bool:
        return self._kaggle_download is not False

    def is_stopped(self) -> bool:
        return self._stopped

    def set_stopped(self, stopped: bool = True) -> None:
        self._stopped = stopped
        if stopped:
            self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def clear_wake(self) -> None:
        self._wake.clear()

    async def wait_wake(self) -> None:
        await self._wake.wait()

    def set_kaggle_download(self, value: bool | None) -> None:
        self._kaggle_download = value

    def add_branch(self, plan_id: str, branch: GitWorkBranch) -> None:
        self._branches[plan_id] = branch

    def get_branch(self, plan_id: str) -> GitWorkBranch:
        return self._branches[plan_id]

    def record_next_guidance(self, text: str) -> None:
        self._next_guidance = text

    def record_persistent_guidance(self, text: str) -> None:
        self._persistent_guidance.append(text)

    async def record_guidance(self, text: str, scope: str) -> dict[str, object]:
        """Record guidance that will be frozen only into later Plan inputs."""
        if not text.strip():
            raise ValueError("guidance text must be nonblank")
        if scope == "next":
            self.record_next_guidance(text)
        elif scope == "persistent":
            self.record_persistent_guidance(text)
        else:
            raise ValueError(f"unsupported guidance scope: {scope}")
        return {"text": text, "scope": scope}

    def take_guidance(self) -> list[str]:
        guidance = [*self._persistent_guidance]
        if self._next_guidance is not None:
            guidance.append(self._next_guidance)
            self._next_guidance = None
        return guidance

    def add_running(self, plan_id: str, task: asyncio.Task) -> None:
        self._running[plan_id] = task

    def pop_running(self, plan_id: str) -> asyncio.Task | None:
        return self._running.pop(plan_id, None)

    def running_ids(self) -> Iterable[str]:
        return tuple(self._running)

    @property
    def running_tasks(self) -> tuple[asyncio.Task, ...]:
        return tuple(self._running.values())

    def running_items(self) -> tuple[tuple[str, asyncio.Task], ...]:
        """Return ``(plan_id, task)`` pairs for every in-flight Plan.

        ``running_tasks`` 只给 task，用来喂 ``asyncio.wait``；想从 task 反查
        plan_id 必须用这个。别拿 ``running_tasks`` 解包成二元组——那是在迭代
        ``asyncio.Task`` 本身（Future 的 ``__iter__`` 即 ``__await__``），已完成的
        task 迭代结果为空，正好是 ``asyncio.wait`` 交回来的那些。
        """
        return tuple(self._running.items())

    def clear_running(self) -> None:
        self._running.clear()

    def set_next_hypothesis_id(self, hypothesis_id: str | None) -> None:
        self._next_hypothesis_id = hypothesis_id

    def set_search_task(self, task: asyncio.Task | None) -> None:
        self._search_task = task

    @property
    def search_task(self) -> asyncio.Task | None:
        return self._search_task


__all__ = ["SupervisorRunState"]
