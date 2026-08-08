"""Agent — 流式工具调用循环。统一契约: AgentContext → AgentOutcome。"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from athena.core.agent.models import (
    AgentConfig,
    AgentContext,
    AgentOutcome,
    StepOutcome,
    ToolCall,
)
from athena.core.agent.provider import ResponsesProvider
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import AskUser, EmitEvent, ToolContext, ToolResult
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from athena.core.contracts import ArtifactStore

logger = logging.getLogger(__name__)

_MAX_STRUCTURED_RETRIES = 3


class BaseAgent(ABC):
    """Agent 抽象基类 — 定义 ``run`` 和 ``tool`` 统一契约。

    所有 Agent 实现必须提供 ``name``、``description`` 和 ``run`` 方法。
    """

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
        model: ResponsesProvider,
        tools: ToolRegistry,
        system_prompt: str,
        config: AgentConfig | None = None,
        *,
        output_type: type[BaseModel] | None = None,
        artifacts: "ArtifactStore | None" = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.config = config or AgentConfig()
        self._output_type = output_type
        self._artifacts = artifacts

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def description(self) -> str:
        return f"Agent: {self.model.model_name}"

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        mem = ctx.memory
        if mem is None:
            mem = ctx.memory = ContextManager()

        # 注入 system prompt（每个 ContextManager 生命周期仅一次）
        user = _load_input(ctx)
        if self.system_prompt and not _has_system(mem):
            mem.append(
                ModelRequest(parts=[SystemPromptPart(content=self.system_prompt)])
            )
        if user:
            mem.append(ModelRequest(parts=[UserPromptPart(content=user)]))

        # 多轮采样循环：LLM 输出工具调用 → 执行 → 写入结果 → 再次请求
        retries = 0
        for _ in range(self.config.max_turns):
            if ctx.cancel.is_set():
                break
            outcome = await _sampling_loop(self, ctx)
            if outcome.kind == "done":
                if self._output_type is not None:
                    try:
                        instance = self._output_type.model_validate_json(outcome.text)
                    except ValidationError as exc:
                        if retries >= _MAX_STRUCTURED_RETRIES:
                            raise RuntimeError(
                                f"structured output invalid after retries: {exc}"
                            ) from exc
                        retries += 1
                        mem.append(
                            ModelRequest(
                                parts=[
                                    UserPromptPart(
                                        content=(
                                            "Previous JSON output was invalid: "
                                            f"{exc}\nReturn JSON matching the schema."
                                        )
                                    )
                                ]
                            )
                        )
                        continue
                    json_text = instance.model_dump_json()
                    ref = (
                        await self._artifacts.put_text(json_text)
                        if self._artifacts is not None
                        else f"result://{ctx.turn.turn_id}"
                    )
                    return AgentOutcome(
                        result_ref=ref,
                        next_context_ref=f"context://{ctx.turn.turn_id}/next",
                    )
                ref = f"result://{ctx.turn.turn_id}"
                return AgentOutcome(
                    result_ref=ref,
                    next_context_ref=f"context://{ctx.turn.turn_id}/next",
                )
            if outcome.kind == "error":
                raise RuntimeError(outcome.text or "provider stream failed")

        if self._output_type is not None:
            raise RuntimeError("max turns exhausted without structured output")

        return AgentOutcome(
            result_ref=f"result://{ctx.turn.turn_id}",
            next_context_ref=f"context://{ctx.turn.turn_id}/next",
        )


async def _cancel_tool_tasks(tool_tasks: list[asyncio.Task[Any] | None]) -> None:
    """取消并等待所有在途工具任务；吞掉取消引发的异常。"""
    pending = [t for t in tool_tasks if t is not None and not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _sampling_loop(agent: Agent, ctx: AgentContext) -> StepOutcome:
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
        async with aclosing(
            agent.model.stream(
                agent.config,
                agent.tools,
                mem.items,
                ctx.cancel,
                output_type=agent._output_type,
            )
        ) as stream:
            async for event in stream:
                match event.kind:
                    case "text_delta":
                        text = event.data.get(
                            "accumulated", text + event.data.get("delta", "")
                        )
                        await ctx.emit(
                            "agent/text_delta", f"event:{ctx.turn.turn_id}", event.data
                        )

                    case "function_call":
                        had_calls = True
                        tc = ToolCall(
                            call_id=event.data["call_id"],
                            name=event.data["name"],
                            arguments=event.data.get("arguments", {}),
                        )
                        idx = len(tool_calls)
                        tool_calls.append(tc)
                        tool_tasks.append(None)

                        tool = agent.tools.resolve(tc.name)
                        tctx = ToolContext(
                            tc.name,
                            f"{ctx.turn.turn_id}:{tc.name}",
                            ctx.emit,
                            ctx.cancel,
                            ask_user=ctx.ask_user,
                        )

                        task = _dispatch_tool_call(
                            tool, tctx, tc, idx, tool_tasks, serial_barrier
                        )
                        tool_tasks[idx] = task
                        if not tool.spec.concurrency_safe:
                            serial_barrier = task

                    case "response_completed":
                        break

                    case "error":
                        # 流错误：取消未完成的工具任务后返回 error 步进
                        await _cancel_tool_tasks(tool_tasks)
                        return StepOutcome(
                            kind="error", text=event.data.get("message", "")
                        )

    except asyncio.CancelledError:
        # 外部取消信号（ThreadRuntime 关闭 / Turn 中断）→ 清理工具任务后传播
        await _cancel_tool_tasks(tool_tasks)
        raise
    except Exception as exc:  # Provider 流未预期异常 → 清理工具任务后返回错误
        logger.error("Provider stream 失败: %s", exc)
        await _cancel_tool_tasks(tool_tasks)
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


def _dispatch_tool_call(
    tool,
    tctx: ToolContext,
    tc: ToolCall,
    idx: int,
    tool_tasks: list[asyncio.Task[Any] | None],
    serial_barrier: asyncio.Task[Any] | None,
) -> asyncio.Task[Any]:
    """根据工具是否并发安全，创建并返回对应的异步任务。

    将 function_call 分支中的嵌套逻辑抽取为独立函数，
    避免 match/case 内出现超过 3 层的代码嵌套。
    """
    if tool.spec.concurrency_safe:

        async def _run_safe(
            barrier: asyncio.Task[Any] | None = serial_barrier,
            selected_tool=tool,
            selected_ctx=tctx,
            arguments=tc.arguments,
        ) -> ToolResult:
            if barrier is not None:
                await barrier
            return await selected_tool.ainvoke(selected_ctx, **arguments)

        return asyncio.create_task(_run_safe())
    else:
        previous = [task for task in tool_tasks[:idx] if task is not None]

        async def _run_serial(
            pending: list[asyncio.Task[Any]] = previous,
            selected_tool=tool,
            selected_ctx=tctx,
            arguments=tc.arguments,
        ) -> ToolResult:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return await selected_tool.ainvoke(selected_ctx, **arguments)

        return asyncio.create_task(_run_serial())


def agent_runner(
    agent: BaseAgent,
    tools: ToolRegistry,
    *,
    ask_user: Callable[[AthenaThread, AthenaTurn], AskUser | None] | None = None,
):
    """将 BaseAgent 适配为两套 Runner 签名，向后兼容 ThreadRuntime。

    ``ask_user`` 是 ``(thread, turn) -> (prompt) -> 回答`` 的工厂：每次 run
    时绑定线程上下文注入 AgentContext，供 ``request_user_input`` 工具阻塞
    等待用户回答；未提供则该工具返回错误。

    ThreadRuntime 会检测 runner 是否有 run_with_context 属性：
    - 有 → 传递 (thread, turn, emit, memory, cancel) 五参数
    - 无 → 传递 (thread, turn, emit) 三参数

    通过 setattr 将两个闭包装载到同一可调用对象上，让 ThreadRuntime
    无需修改即可同时支持新旧两种 Runner 签名。
    """

    def _bind(thread: AthenaThread, turn: AthenaTurn) -> AskUser | None:
        return ask_user(thread, turn) if ask_user else None

    async def _run(
        thread: AthenaThread, turn: AthenaTurn, emit: EmitEvent
    ) -> AgentOutcome:
        ctx = AgentContext(
            thread,
            turn,
            emit,
            tools,
            asyncio.Event(),
            ask_user=_bind(thread, turn),
        )
        return await agent.run(ctx)

    async def _run_with_context(
        thread: AthenaThread,
        turn: AthenaTurn,
        emit: EmitEvent,
        memory: "ContextManager | None",
        cancel: asyncio.Event,
    ) -> AgentOutcome:
        ctx = AgentContext(
            thread,
            turn,
            emit,
            tools,
            cancel,
            memory,
            ask_user=_bind(thread, turn),
        )
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
) -> Agent:
    """创建 Agent 实例的便捷工厂函数。

    将分散的配置参数统一构造为 AgentConfig 和 Agent 对象。
    """
    provider = ResponsesProvider(model, client=client)
    config = AgentConfig(max_turns, max_tokens, temperature, name)
    return create_code_agent(provider, tools, system_prompt, config)


def create_code_agent(
    model: ResponsesProvider,
    tools: ToolRegistry,
    system_prompt: str,
    config: AgentConfig | None = None,
) -> Agent:
    return Agent(model, tools, system_prompt, config)


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
    """优先使用显式文本，否则从 request_ref 读取文本或 artifact 文件。"""
    if ctx.input_text is not None:
        return ctx.input_text
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
