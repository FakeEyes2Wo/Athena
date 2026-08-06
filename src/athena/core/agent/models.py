"""Data models shared by Agent runtime and control components."""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent
from athena.memory.context_manager import ContextManager


@dataclass(slots=True, frozen=True)
class AgentConfig:
    """Agent 运行配置 — 模型、系统提示、工具集和采样参数。"""

    max_turns: int = 20
    max_tokens: int = 4096
    temperature: float = 0.1
    name: str = "code-agent"


@dataclass(slots=True)
class AgentOutcome:
    """Agent 运行结果 — 替代 tuple 和多套返回签名。"""

    result_ref: str
    next_context_ref: str


@dataclass(slots=True)
class StepOutcome:
    """采样步进结果 — kind 驱动循环分派。"""

    kind: Literal["done", "continue", "error"]
    text: str = ""


@dataclass(slots=True)
class ToolCall:
    """LLM 输出的工具调用 — 包含调用 ID、名称和参数。"""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class AgentContext:
    """每 Turn 上下文。memory 由 ThreadRuntime 注入，Agent 不自行创建。"""

    thread: AthenaThread
    turn: AthenaTurn
    emit: EmitEvent
    tools: ToolRegistry
    cancel: asyncio.Event
    memory: "ContextManager | None" = None
    input_text: str | None = None
