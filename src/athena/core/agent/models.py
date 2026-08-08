"""Data models shared by Agent runtime and control components."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from athena.core.agent_kernel.types import AgentMessage


@dataclass(slots=True, frozen=True)
class AgentConfig:
    """Agent 运行配置 — 模型、系统提示、工具集和采样参数。"""

    max_turns: int = 20
    max_tokens: int = 4096
    temperature: float = 0.1
    name: str = "code-agent"


@dataclass(slots=True)
class AgentOutcome:
    """Agent 运行结果 — 目标合同只含 ``result_ref``。

    ``next_context_ref`` 是旧 ThreadRuntime 迁移字段；Kernel 必须忽略私有记忆
    的 context_ref 由 AgentSession 持有，不是业务结果。旧调用方迁移后删除。
    """

    result_ref: str
    next_context_ref: str = ""


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
    """每 Turn 上下文。memory 由 ThreadRuntime 注入，Agent 不自行创建。

    ``messages`` 按提交顺序包含触发请求与未读 mailbox 消息（设计
    dynamic-agent-orchestration §4.3）；``input_text`` 是迁移期对当前触发
    消息 ``content`` 的兼容视图，不能承载或替代 ``context_refs``。
    """

    thread: AthenaThread
    turn: AthenaTurn
    emit: EmitEvent
    tools: ToolRegistry
    cancel: asyncio.Event
    memory: "ContextManager | None" = None
    input_text: str | None = None
    messages: list["AgentMessage"] = field(default_factory=list)
