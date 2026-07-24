"""Agent — 流式工具调用循环。统一契约: AgentContext → AgentOutcome。"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from athena.core.agent.provider import ResponsesProvider
from athena.core.schemas import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent, ToolContext, ToolResult
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


# ── 统一契约类型 ─────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class AgentConfig:
    model: str
    system_prompt: str
    tools: ToolRegistry
    max_turns: int = 20
    max_tokens: int = 4096
    temperature: float = 0.1


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


class BaseAgent(ABC):
    name: str
    description: str

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentOutcome: ...

    async def tool(self, ctx: AgentContext, name: str, **inp: Any) -> ToolResult:
        return await ctx.tools.resolve(name).ainvoke(
            ToolContext(name, f"{ctx.turn.turn_id}:{name}", ctx.emit, ctx.cancel), **inp
        )


class Agent(BaseAgent):
    """流式工具调用 Agent Loop。

    每个 Turn 执行一次 ReAct 风格循环：
    system prompt 注入 → 用户输入注入 → 多轮采样直到 LLM 输出纯文本。
    concurrency_safe 工具并行执行，非安全工具按顺序串行化。
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        client: "AsyncOpenAI | None" = None,
        name: str = "agent",
        description: str = "",
    ) -> None:
        self.config = config
        self.name = name
        self.description = description or f"Agent: {config.model}"
        self._provider = ResponsesProvider(client=client)

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        mem = ctx.memory
        if mem is None:
            mem = ctx.memory = ContextManager()

        # 注入 system prompt（每个 ContextManager 生命周期仅一次）
        user = _load_input(ctx)
        if not _has_system(mem):
            mem.append(
                ModelRequest(
                    parts=[SystemPromptPart(content=self.config.system_prompt)]
                )
            )
        if user:
            mem.append(ModelRequest(parts=[UserPromptPart(content=user)]))

        # 多轮采样循环：LLM 输出工具调用 → 执行 → 写入结果 → 再次请求
        for _ in range(self.config.max_turns):
            if ctx.cancel.is_set():
                break
            outcome = await _sampling_loop(self.config, ctx, self._provider)
            if outcome.kind == "done":
                ref = f"result://{ctx.turn.turn_id}"
                return AgentOutcome(
                    result_ref=ref,
                    next_context_ref=f"context://{ctx.turn.turn_id}/next",
                )
            if outcome.kind == "error":
                raise RuntimeError(outcome.text or "provider stream failed")

        return AgentOutcome(
            result_ref=f"result://{ctx.turn.turn_id}",
            next_context_ref=f"context://{ctx.turn.turn_id}/next",
        )


# ── _sampling_loop ───────────────────────────────────────────────


async def _sampling_loop(
    config: AgentConfig, ctx: AgentContext, provider
) -> StepOutcome:
    """单次 LLM 采样 + 工具执行 + 结果回写。

    并发策略：
    - concurrency_safe 工具：创建 task 并等待 serial_barrier（如有），异步并行
    - 非 concurrency_safe 工具：先 gather 所有前置 task，自身成为 serial_barrier
      后续所有任务（无论是否安全）都必须等待它完成，保证串行顺序
    """
    mem = ctx.memory
    assert mem is not None

    tool_calls: list[ToolCall] = []
    tool_tasks: list[asyncio.Task[Any] | None] = []
    serial_barrier: asyncio.Task[Any] | None = None
    text = ""
    had_calls = False

    try:
        async for ev in provider.stream(config, mem.items, ctx.cancel):
            match ev.kind:
                case "text_delta":
                    text = ev.data.get("accumulated", text + ev.data.get("delta", ""))
                    await ctx.emit(
                        "agent/text_delta", f"event:{ctx.turn.turn_id}", ev.data
                    )

                case "function_call":
                    had_calls = True
                    tc = ToolCall(
                        call_id=ev.data["call_id"],
                        name=ev.data["name"],
                        arguments=ev.data.get("arguments", {}),
                    )
                    idx = len(tool_calls)
                    tool_calls.append(tc)
                    tool_tasks.append(None)

                    tool = config.tools.resolve(tc.name)
                    tctx = ToolContext(
                        tc.name, f"{ctx.turn.turn_id}:{tc.name}", ctx.emit, ctx.cancel
                    )

                    if tool.spec.concurrency_safe:
                        # 安全工具：等待串行屏障（如存在）后并行执行
                        async def _run_safe(
                            barrier: asyncio.Task[Any] | None = serial_barrier,
                            selected_tool=tool,
                            selected_ctx=tctx,
                            arguments=tc.arguments,
                        ) -> ToolResult:
                            if barrier is not None:
                                await barrier
                            return await selected_tool.ainvoke(
                                selected_ctx, **arguments
                            )

                        tool_tasks[idx] = asyncio.create_task(_run_safe())
                    else:
                        # 非安全工具：先完成所有前置任务，自身成为后续的屏障
                        previous = [
                            task for task in tool_tasks[:idx] if task is not None
                        ]

                        async def _run_serial(
                            pending: list[asyncio.Task[Any]] = previous,
                            selected_tool=tool,
                            selected_ctx=tctx,
                            arguments=tc.arguments,
                        ) -> ToolResult:
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                            return await selected_tool.ainvoke(
                                selected_ctx, **arguments
                            )

                        serial_barrier = asyncio.create_task(_run_serial())
                        tool_tasks[idx] = serial_barrier

                case "response_completed":
                    break

                case "error":
                    # 流错误：取消未完成的工具任务后返回 error 步进
                    for task in tool_tasks:
                        if task is not None and not task.done():
                            task.cancel()
                    if tool_tasks:
                        await asyncio.gather(
                            *[task for task in tool_tasks if task is not None],
                            return_exceptions=True,
                        )
                    return StepOutcome(kind="error", text=ev.data.get("message", ""))

    except asyncio.CancelledError:
        # 外部取消信号（ThreadRuntime 关闭 / Turn 中断）→ 清理工具任务后传播
        for task in tool_tasks:
            if task is not None and not task.done():
                task.cancel()
        if tool_tasks:
            await asyncio.gather(
                *[task for task in tool_tasks if task is not None],
                return_exceptions=True,
            )
        raise
    except Exception as exc:
        logger.error("Provider stream 失败: %s", exc)
        for t in tool_tasks:
            if t and not t.done():
                t.cancel()
        return StepOutcome(kind="error", text=f"{type(exc).__name__}: {exc}")

    # 等待所有工具执行完成
    results: list[Any] = [None] * len(tool_tasks)
    if tool_tasks:
        gathered = await asyncio.gather(
            *[t for t in tool_tasks if t], return_exceptions=True
        )
        gi = 0
        for i, t in enumerate(tool_tasks):
            if t:
                results[i] = gathered[gi]
                gi += 1

    # 写入 assistant 消息（文本 + 工具调用）
    if had_calls and tool_calls:
        parts: list[Any] = []
        if text:
            parts.append(TextPart(content=text))
        for tc in tool_calls:
            parts.append(
                ToolCallPart(
                    tool_name=tc.name,
                    tool_call_id=tc.call_id,
                    args=json.dumps(tc.arguments, ensure_ascii=False),
                )
            )
        mem.append(ModelResponse(parts=parts))

    # 写入工具返回结果到对话历史
    for i, tc in enumerate(tool_calls):
        r = results[i]
        content = _to_str(r)
        if len(content) > 50000:
            content = content[:24950] + "\n...[TRUNCATED]...\n" + content[-24950:]
        mem.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=tc.name, content=content, tool_call_id=tc.call_id
                    ),
                ]
            )
        )

    # 无工具调用且有文本 → 完成；有工具调用 → 继续下一轮
    if not had_calls and text:
        mem.append(ModelResponse(parts=[TextPart(content=text)]))
        return StepOutcome(kind="done", text=text)
    if had_calls:
        return StepOutcome(kind="continue")
    return StepOutcome(kind="done", text=text)


# ── create_agent / agent_runner ───────────────────────────────────


def agent_runner(agent: BaseAgent, tools: ToolRegistry):
    """将 BaseAgent 适配为两套 Runner 签名，向后兼容 ThreadRuntime。

    ThreadRuntime 会检测 runner 是否有 run_with_context 属性：
    - 有 → 传递 (thread, turn, emit, memory, cancel) 五参数
    - 无 → 传递 (thread, turn, emit) 三参数

    通过 setattr 将两个闭包装载到同一可调用对象上，让 ThreadRuntime
    无需修改即可同时支持新旧两种 Runner 签名。
    """

    async def _run(
        thread: AthenaThread, turn: AthenaTurn, emit: EmitEvent
    ) -> AgentOutcome:
        ctx = AgentContext(thread, turn, emit, tools, asyncio.Event())
        return await agent.run(ctx)

    async def _run_with_context(
        thread: AthenaThread,
        turn: AthenaTurn,
        emit: EmitEvent,
        memory: "ContextManager | None",
        cancel: asyncio.Event,
    ) -> AgentOutcome:
        ctx = AgentContext(thread, turn, emit, tools, cancel, memory)
        return await agent.run(ctx)

    setattr(_run, "run_with_context", _run_with_context)
    return _run


def create_agent(
    model: str,
    tools: ToolRegistry,
    system_prompt: str,
    *,
    client: "AsyncOpenAI | None" = None,
    max_turns: int = 20,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    name: str = "agent",
    description: str = "",
) -> Agent:
    config = AgentConfig(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return Agent(config, client=client, name=name, description=description)


# ── 内部工具函数 ─────────────────────────────────────────────────


def _to_str(r: Any) -> str:
    """将 ToolResult 或异常转为字符串，用于写入对话历史。"""
    if isinstance(r, ToolResult):
        if not r.success:
            return f"[ERROR] {r.error}" if r.error else "[ERROR]"
        if r.data is None:
            return "[OK]"
        return (
            json.dumps(r.data, ensure_ascii=False)
            if isinstance(r.data, (dict, list))
            else str(r.data)
        )
    if isinstance(r, BaseException):
        return f"[ERROR] {type(r).__name__}: {r}"
    return str(r)


def _load_input(ctx: AgentContext) -> str:
    """从 turn.request_ref 提取用户输入。支持 artifact:// 前缀的文件读取。"""
    ref = ctx.turn.request_ref
    if ref and ref.startswith("artifact://"):
        try:
            return Path(ref[len("artifact://") :]).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            # 文件不存在、权限不足或编码错误时回退到原始 ref
            pass
    return ref or ""


def _has_system(cm: "ContextManager") -> bool:
    """检查 ContextManager 中是否已包含 system prompt。"""
    for msg in cm.items:
        if isinstance(msg, ModelRequest) and any(
            getattr(p, "part_kind", None) == "system-prompt" for p in msg.parts
        ):
            return True
    return False
