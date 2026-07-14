"""ToolRouter 与外部工具适配器共享的最小契约。

工具调用以 artifact 交接请求和结果，避免把大数据、长日志或敏感上下文直接放入
Agent 消息。Router 只按 capability 分发；工具权限、并发优先级、重试和审批仍由
Scheduler、ThreadManager 与 Supervisor 协作管理。
"""

from typing import Protocol

from athena.core.schemas import ArtifactRef


class ToolAdapter(Protocol):
    """由单一 capability 名称寻址的异步工具适配器。"""

    async def invoke(self, request_ref: ArtifactRef) -> ArtifactRef:
        """执行请求 artifact，并返回可审计结果的 artifact 引用。"""
