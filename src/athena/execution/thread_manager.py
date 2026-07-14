"""Agent 的异步 ``Thread → Turn → Event`` 执行边界。

ThreadManager 只提供 Agent 执行接口，不承担 Session 工作流调度或重复实现
checkpoint；Scheduler 通过事件流获得进度和结果，LangGraph（如采用）只负责
Session checkpoint。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from athena.core.schemas import ArtifactRef, AthenaThread, AthenaTurn


class ThreadManager(ABC):
    """Agent 执行接口；工作流推进、预算与优先级管理仍由 Scheduler 负责。"""

    @abstractmethod
    async def start(self, session_id: str, context_ref: ArtifactRef) -> AthenaThread:
        """为一个 Session 创建可恢复线程，并以 artifact 提供初始上下文。"""

    @abstractmethod
    async def submit(self, thread_id: str, request_ref: ArtifactRef) -> AthenaTurn:
        """提交任务并立即返回 Turn；完成状态和结果通过事件流异步取得。"""

    @abstractmethod
    async def fork(self, thread_id: str, after_turn_id: str | None = None) -> AthenaThread:
        """仅从已完成 Turn 派生子线程，子线程不得修改父线程历史。"""

    @abstractmethod
    async def interrupt(self, thread_id: str, turn_id: str, reason: str) -> None:
        """中断运行中的 Turn，并将中断原因写入事件事实。"""

    @abstractmethod
    def events(self, thread_id: str) -> AsyncIterator[ArtifactRef]:
        """流式返回消息、工具调用、patch、审批、进度和结果事件的引用。"""
