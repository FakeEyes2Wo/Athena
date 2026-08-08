"""工具类型、常量和轻量级数据类。

零逻辑 — ``tool.py`` 和 ``agent.py`` 共享的纯数据容器。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

EmitEvent = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]
"""事件发射器: ``(kind: str, artifact_ref: str, data: dict | None) -> None``。"""

AskUser = Callable[[str], Awaitable[str | None]]
"""用户输入请求回调: ``(prompt) -> 回答文本``；``None`` 表示取消/超时。"""

TOOL_BEGIN = "tool/begin"
TOOL_END = "tool/end"
TOOL_ERROR = "tool/error"

# 工具返回/日志文本写入对话历史的截断上限；保留头尾便于调试。
_MAX_RESULT_CHARS = 50_000
_TRUNCATED_MARK = "\n...[TRUNCATED]...\n"


def truncate_text(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    """超过 ``limit`` 时保留头尾各一半，中间用截断标记连接。"""
    if len(text) <= limit:
        return text
    half = (limit - len(_TRUNCATED_MARK)) // 2
    return text[:half] + _TRUNCATED_MARK + text[-half:]


@dataclass(slots=True)
class ToolSpec:
    """工具描述 — 最小化字段，删除 category/exposure/read_only/destructive。"""

    name: str
    description: str
    input_schema: dict  # JSON Schema
    concurrency_safe: bool = True
    """是否支持并行执行 — AgentLoop 以此决定串行还是并发。"""
    max_result_chars: int = 50_000

    def to_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(slots=True)
class ToolResult:
    """标准化的工具输出。"""

    data: Any
    success: bool = True
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolContext:
    """每次调用的上下文 — 每次工具调用时重新创建。"""

    tool_name: str
    call_id: str
    emit: EmitEvent
    cancel: asyncio.Event
    ask_user: AskUser | None = None
    """交互式提问回调 — 由 Agent 从 AgentContext 透传，工具用它请求用户输入。"""
