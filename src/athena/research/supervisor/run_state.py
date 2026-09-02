"""Mutable, non-durable runtime state for one Supervisor process."""

import asyncio
from collections.abc import Iterable

from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.state import ResearchState


class SupervisorRunState:
    """Transient state that does not belong in ResearchState or ResearchTree.

    This includes in-flight Plan tasks, created worktrees, guidance that has not
    yet been frozen into a Plan, and the SEARCH wake event. Durable run status
    and settings stay in ``ResearchState``.
    """

    def __init__(self, state: ResearchState) -> None:
        self._state = state
        self._branches: dict[str, GitWorkBranch] = {}
        self._next_guidance: str | None = None
        self._persistent_guidance: list[str] = []
        self._running: dict[str, asyncio.Task] = {}
        self._next_hypothesis_id: str | None = None
        # 手动模式下等待人工选定假设时，唤醒 run_search 循环的信号。
        self._wake = asyncio.Event()

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
        """Report whether the durable run enables Kaggle integration."""
        return self._state.kaggle_download is not None

    @property
    def kaggle_download(self) -> bool:
        """Report whether enabled Kaggle integration may download data."""
        return self._state.kaggle_download is not False

    def is_stopped(self) -> bool:
        """Read the terminal stop decision from durable research state."""
        return self._state.status == "STOPPED"

    def wake(self) -> None:
        """Wake SEARCH after a control decision changes durable state."""
        self._wake.set()

    def clear_wake(self) -> None:
        """Clear a consumed SEARCH control signal before waiting again."""
        self._wake.clear()

    async def wait_wake(self) -> None:
        """Wait until a control operation permits SEARCH to reconsider work."""
        await self._wake.wait()

    def set_kaggle_download(self, value: bool | None) -> None:
        """Store the Kaggle integration decision in durable research state."""
        self._state.kaggle_download = value

    def add_branch(self, plan_id: str, branch: GitWorkBranch) -> None:
        """Bind one live Plan to its process-local Git worktree."""
        self._branches[plan_id] = branch

    def get_branch(self, plan_id: str) -> GitWorkBranch:
        """Return the process-local Git worktree bound to a Plan."""
        return self._branches[plan_id]

    def record_next_guidance(self, text: str) -> None:
        """Keep guidance for the next Plan input only."""
        self._next_guidance = text

    def record_persistent_guidance(self, text: str) -> None:
        """Keep guidance for every later Plan input in this process."""
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
        """Freeze persistent and one-shot guidance into a new Plan input."""
        guidance = [*self._persistent_guidance]
        if self._next_guidance is not None:
            guidance.append(self._next_guidance)
            self._next_guidance = None
        return guidance

    def add_running(self, plan_id: str, task: asyncio.Task) -> None:
        """Track the live task executing one Plan turn."""
        self._running[plan_id] = task

    def pop_running(self, plan_id: str) -> asyncio.Task | None:
        """Release a completed Plan turn from local task tracking."""
        return self._running.pop(plan_id, None)

    def running_ids(self) -> Iterable[str]:
        """Return a stable snapshot of locally running Plan IDs."""
        return tuple(self._running)

    @property
    def running_tasks(self) -> tuple[asyncio.Task, ...]:
        """Return a stable snapshot of locally running Plan tasks."""
        return tuple(self._running.values())

    @property
    def running_items(self) -> tuple[tuple[str, asyncio.Task], ...]:
        """(plan_id, task) pairs, for finding which plan a finished task belongs to.

        ``running_tasks`` yields bare Tasks. Unpacking one into ``(id, task)``
        does not raise a helpful error: a *completed* asyncio Task iterates to
        zero items, so the caller gets ``not enough values to unpack (expected
        2, got 0)`` from deep inside a generator expression.
        """
        return tuple(self._running.items())

    def clear_running(self) -> None:
        """Release all process-local Plan task records after shutdown."""
        self._running.clear()

    def set_next_hypothesis_id(self, hypothesis_id: str | None) -> None:
        """Store the one-shot manual hypothesis selection for SEARCH."""
        self._next_hypothesis_id = hypothesis_id


__all__ = ["SupervisorRunState"]
