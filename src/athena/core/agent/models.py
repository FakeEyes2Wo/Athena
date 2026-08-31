"""Data models shared by Agent runtime and control components."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from athena.core.agent import settings
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import AskUser, EmitEvent
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from athena.core.agent.types import AgentMessage


@dataclass(slots=True, frozen=True)
class AgentConfig:
    """Agent 运行配置 — 模型、系统提示、工具集和采样参数。

    ``max_tokens``/``temperature``/``seed`` 的默认值从 ``settings`` 取（``LLM_MAX_TOKENS`` /
    ``LLM_TEMPERATURE`` / ``LLM_SEED`` 或 ``config.toml`` 的 ``[llm]``），不再是写死的字面量：评测规范
    普遍要求申报并固定采样参数，而写死的默认值既申报不了也调不动。
    ``seed=None`` 表示请求里不带该字段。
    """

    max_turns: int = 200
    max_tokens: int = field(default_factory=settings.max_tokens)
    temperature: float = field(default_factory=settings.temperature)
    name: str = "code-agent"
    tool_choice: Literal["auto", "required"] = "auto"
    seed: int | None = field(default_factory=settings.seed)


@dataclass(slots=True)
class AgentOutcome:
    """Agent 运行结果 — 目标合同只含 ``result_ref``。

    COMPAT: ``next_context_ref`` 是旧 ThreadRuntime 迁移字段;AgentRuntime 必须忽略私有记忆
    的 context_ref 由 AgentSession 持有,不是业务结果。清理条件: 旧调用方迁移完成后删除。
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
    ``ask_user`` 由外层注入的交互式提问回调；``request_user_input`` 工具
    await 它阻塞等待回答。
    """

    thread: AthenaThread
    turn: AthenaTurn
    emit: EmitEvent
    tools: ToolRegistry
    cancel: asyncio.Event
    memory: "ContextManager | None" = None
    input_text: str | None = None
    messages: list["AgentMessage"] = field(default_factory=list)
    ask_user: AskUser | None = None
