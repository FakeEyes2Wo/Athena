"""Agent — 流式工具调用循环。统一契约: AgentContext → AgentOutcome。"""

import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from athena.core.retry import is_transient_error

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
from athena.core.agent.provider import BaseProvider, create_provider
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import AskUser, EmitEvent, ToolContext, ToolResult
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from athena.core.contracts import ArtifactStore

logger = logging.getLogger(__name__)

_MAX_STRUCTURED_RETRIES = 3

# ```json … ``` — 模型在带工具的对话里习惯把最终 JSON 包进 markdown 代码块。
_FENCED_JSON = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
_EMBEDDED_JSON = re.compile(r"```json\s*(.+?)\s*```", re.DOTALL)


def _unfenced(text: str) -> str:
    """剥掉结构化输出外面的 markdown 代码块围栏。

    带工具时不能再发 ``response_format``（见 ``provider.stream``），模型于是自由地
    把终态 JSON 包进 ```json 围栏。真机上这一条足以打死整个 SEARCH：Ideator 连着三次
    返回围栏 JSON，重试预算耗尽后抛 ``structured output invalid after retries``。
    围栏是格式噪声不是内容错误，直接剥掉，把重试预算留给真正的 schema 不匹配。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        match = _EMBEDDED_JSON.search(stripped)
        if match:
            return match.group(1)
        decoder = json.JSONDecoder()
        for start, char in enumerate(stripped):
            if char != "{":
                continue
            try:
                _, end = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                # A prose brace or incomplete object precedes the structured result.
                continue
            if not stripped[start + end :].strip():
                return stripped[start : start + end]
        return text
    match = _FENCED_JSON.search(stripped)
    return match.group(1) if match else text


# LLM 响应流断线最多重连次数（supervisor_design §6.1）+ 退避基准秒数。
_MAX_STREAM_RETRIES = 5
_RETRY_BASE_DELAY = 1.0


class BaseAgent(ABC):
    """Agent 抽象基类 — 定义 ``run`` 和 ``tool`` 统一契约。

    所有 Agent 实现必须提供 ``name``、``description`` 和 ``run`` 方法。
    """

    name: str
    description: str

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """运行业务 Agent 的一个 turn（BaseAgent 公共契约）。"""
        ...

    async def tool(self, ctx: AgentContext, name: str, **inp: Any) -> ToolResult:
        """按名称调用工具并返回 ToolResult（业务 Agent 编排用）。"""
        return await ctx.tools.resolve(name).ainvoke(
            ToolContext(
                name,
                f"{ctx.turn.turn_id}:{name}",
                ctx.emit,
                ctx.cancel,
                session_id=ctx.thread.session_id,
            ),
            **inp,
        )


class Agent(BaseAgent):
    """流式工具调用 Agent Loop。

    每个 Turn 执行一次 ReAct 风格循环：
    system prompt 注入 → 用户输入注入 → 多轮采样直到 LLM 输出纯文本。
    concurrency_safe 工具并行执行，非安全工具按顺序串行化。
    """

    def __init__(
        self,
        model: BaseProvider,
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
        """Agent 显示名（来自 config）。"""
        return self.config.name

    @property
    def description(self) -> str:
        """Agent 描述（模型名）。"""
        return f"Agent: {self.model.model_name}"

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """ReAct 循环：system 注入 → 多轮采样（工具调用/返回）直到纯文本输出。"""
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
                        instance = self._output_type.model_validate_json(
                            _unfenced(outcome.text)
                        )
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


async def _unknown_tool_result(name: str, available: list[str]) -> ToolResult:
    """未知工具名 → 可恢复的工具错误，列出已注册工具供同一 turn 重试。"""
    return ToolResult(
        success=False,
        error=f"unknown tool: {name}; available_tools={available}",
        data={"available_tools": available},
    )


async def _truncated_tool_result(
    name: str, raw_chars: int, finish_reason: str
) -> ToolResult:
    """实参被输出上限截断 → 可恢复的工具错误，直说原因并给出可执行的出路。

    关键是让模型知道"不是你参数写错了，是你写太长被切了"。只报 schema 校验失败
    的话，模型会原样重发同一个超长调用，每次都在同一个地方被截断。
    """
    return ToolResult(
        success=False,
        error=(
            f"tool call '{name}' was cut off at the model output-token limit "
            f"(finish_reason={finish_reason or 'length'}; {raw_chars} characters "
            "of arguments arrived before the cut, so the JSON is incomplete and "
            "no argument could be parsed). This is NOT a schema mistake — your "
            "arguments were too long. Resending the same call will be cut off at "
            "the same place. Split the work into several smaller calls: for a "
            "file, write the first section with write_file, then add each "
            "remaining section with a separate append_file call."
        ),
        data={
            "truncated": True,
            "raw_argument_chars": raw_chars,
            "finish_reason": finish_reason or "length",
        },
    )


async def _cancel_tool_tasks(tool_tasks: list[asyncio.Task[Any]]) -> None:
    """取消并等待所有在途工具任务；吞掉取消引发的异常。"""
    pending = [t for t in tool_tasks if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _sampling_loop(agent: Agent, ctx: AgentContext) -> StepOutcome:
    """单次 LLM 采样 + 工具执行 + 结果回写；对瞬时流错误自动重连。

    Codex 式技术重试：LLM 响应流断线最多重连 ``_MAX_STREAM_RETRIES`` 次，指数
    退避 + jitter；仅在尚未派发任何工具调用（无副作用）时重试，工具已执行后的
    错误视为业务失败，原样返回。
    """
    retries_left = _MAX_STREAM_RETRIES
    while True:
        outcome, transient = await _sample_once(agent, ctx)
        if not transient or retries_left <= 1:
            return outcome
        retries_left -= 1
        delay = (
            _RETRY_BASE_DELAY
            * (2 ** (_MAX_STREAM_RETRIES - retries_left))
            * (0.5 + random.random())
        )
        await asyncio.sleep(delay)


async def _sample_once(agent: Agent, ctx: AgentContext) -> tuple[StepOutcome, bool]:
    """单次采样步进；返回 (outcome, transient)。

    并发策略：
    - concurrency_safe 工具：创建 task 并等待 serial_barrier（如有），异步并行
    - 非 concurrency_safe 工具：先 gather 所有前置 task，自身成为 serial_barrier
      后续所有任务（无论是否安全）都必须等待它完成，保证串行顺序

    ``transient=True`` 仅表示"瞬时流错误且尚未派发工具调用"（可安全重连）。
    """
    mem = ctx.memory
    assert mem is not None

    tool_calls: list[ToolCall] = []
    tool_tasks: list[asyncio.Task[Any]] = []
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
                        await ctx.emit(
                            "agent/function_call",
                            f"event:{ctx.turn.turn_id}:{tc.call_id}",
                            {"name": tc.name, "arguments": tc.arguments},
                        )
                        tool_calls.append(tc)

                        if event.data.get("truncated"):
                            # 实参被 max_tokens 截断 → 可恢复工具错误（同一 turn
                            # 重试，不终止 worker）。必须在 resolve 之前拦下：工具
                            # 本身会把空参数报成"缺必填参数"，那条信息会把模型引向
                            # 原样重发，而不是分块重写。
                            tool_tasks.append(
                                asyncio.create_task(
                                    _truncated_tool_result(
                                        tc.name,
                                        int(event.data.get("raw_argument_chars", 0)),
                                        str(event.data.get("finish_reason", "")),
                                    )
                                )
                            )
                            continue

                        try:
                            tool = agent.tools.resolve(tc.name)
                        except KeyError:
                            # 未知工具名 → 可恢复工具错误（同一 turn 重试，不终止 worker）
                            tool_tasks.append(
                                asyncio.create_task(
                                    _unknown_tool_result(
                                        tc.name,
                                        [spec.name for spec in agent.tools.specs],
                                    )
                                )
                            )
                            continue
                        tctx = ToolContext(
                            tc.name,
                            f"{ctx.turn.turn_id}:{tc.name}",
                            ctx.emit,
                            ctx.cancel,
                            ask_user=ctx.ask_user,
                            session_id=ctx.thread.session_id,
                        )

                        dependencies = (
                            ((serial_barrier,) if serial_barrier is not None else ())
                            if tool.spec.concurrency_safe
                            else tuple(tool_tasks)
                        )
                        task = asyncio.create_task(
                            _execute_tool_after(tool, tctx, tc.arguments, dependencies)
                        )
                        tool_tasks.append(task)
                        if not tool.spec.concurrency_safe:
                            serial_barrier = task

                    case "response_completed":
                        break

                    case "error":
                        # 流错误：取消未完成的工具任务后返回 error 步进
                        await _cancel_tool_tasks(tool_tasks)
                        transient = not had_calls and is_transient_error(
                            event.data.get("message", "")
                        )
                        return (
                            StepOutcome(
                                kind="error", text=event.data.get("message", "")
                            ),
                            transient,
                        )

    except asyncio.CancelledError:
        # 外部取消信号（ThreadRuntime 关闭 / Turn 中断）→ 清理工具任务后传播
        await _cancel_tool_tasks(tool_tasks)
        raise
    except Exception as exc:  # Provider 流未预期异常 → 清理工具任务后返回错误
        logger.error("Provider stream 失败: %s", exc)
        await _cancel_tool_tasks(tool_tasks)
        transient = not had_calls and is_transient_error(exc)
        return StepOutcome(kind="error", text=f"{type(exc).__name__}: {exc}"), transient

    outcome = await _finalize_step(mem, tool_calls, tool_tasks, text)
    return outcome, False


async def _finalize_step(
    mem: ContextManager,
    tool_calls: list[ToolCall],
    tool_tasks: list[asyncio.Task[Any]],
    text: str,
) -> StepOutcome:
    """收集工具结果、写回消息历史，并决定下一步的 StepOutcome。"""
    results = await asyncio.gather(*tool_tasks, return_exceptions=True)

    # 写入 assistant 消息（文本 + 工具调用）
    if tool_calls:
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
    for tc, r in zip(tool_calls, results, strict=True):
        if isinstance(r, ToolResult):
            if not r.success:
                content = f"[ERROR] {r.error}" if r.error else "[ERROR]"
            elif r.data is None:
                content = "[OK]"
            else:
                content = (
                    json.dumps(r.data, ensure_ascii=False)
                    if isinstance(r.data, (dict, list))
                    else str(r.data)
                )
        elif isinstance(r, BaseException):
            content = f"[ERROR] {type(r).__name__}: {r}"
        else:
            content = str(r)
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
    if tool_calls:
        return StepOutcome(kind="continue")
    if text:
        mem.append(ModelResponse(parts=[TextPart(content=text)]))
    return StepOutcome(kind="done", text=text)


async def _execute_tool_after(
    tool: BaseTool,
    tctx: ToolContext,
    arguments: dict[str, Any],
    dependencies: tuple[asyncio.Task[Any], ...],
) -> ToolResult:
    """等待派发时冻结的前序任务，再执行工具；安全工具继承串行屏障的失败。"""
    if dependencies:
        await asyncio.gather(
            *dependencies, return_exceptions=not tool.spec.concurrency_safe
        )
    return await tool.ainvoke(tctx, **arguments)


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
    config: AgentConfig | None = None,
) -> Agent:
    """由模型名构造 Agent；采样设置由 AgentConfig 统一解析和传递。"""
    provider = create_provider(model, client=client)
    return Agent(provider, tools, system_prompt, config or AgentConfig(name="agent"))


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
