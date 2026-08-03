"""Runner 装配 — agent profile 注册表、审批闸门、离线 mock runner。

``AgentRuntime`` 是传给 ``RuntimeThreadManager`` 的那个稳定可调用对象：
它本身不变，内部持有的 agent 可以被 ``/model`` 和 ``/agent`` 热替换，
下一个 Turn 就会用新的。
"""

import asyncio
import dataclasses
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any

from athena.agents.demo_agent import create_demo_agent
from athena.core.agent.agent import Agent, AgentOutcome, agent_runner
from athena.core.schemas import AthenaThread, AthenaTurn
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import (
    TOOL_BEGIN,
    TOOL_DENIED,
    ToolContext,
    ToolResult,
)

ApprovalGate = Callable[[ToolContext, dict], Awaitable[bool]]

DEFAULT_PROFILE = "demo"

MODEL_ENV = "ATHENA_TUI_MODEL"
"""默认模型的环境变量。profile 工厂的签名默认值只是最后兜底。"""

FALLBACK_MODEL = "claude-haiku-4-5-20251001"

MOCK_REPLY = (
    "这是 **mock runner** 的回复，用来在没有 API key 时验证 TUI 的渲染链路。\n\n"
    "- 流式文本会先出现在底部动态区\n"
    "- 工具调用定稿后落进 scrollback\n"
    "- `Esc` 可以中断这一轮\n"
)


class GatedTool(BaseTool):
    """在真实工具执行前插入一次审批。被拒时发 ``tool/denied`` 并返回失败结果。"""

    def __init__(self, inner: BaseTool, gate: ApprovalGate) -> None:
        self.spec = inner.spec
        self._inner = inner
        self._gate = gate

    async def execute(self, input: dict, ctx: ToolContext) -> Any:
        return await self._inner.execute(input, ctx)

    async def ainvoke(self, ctx: ToolContext, **input: Any) -> ToolResult:
        if await self._gate(ctx, input):
            return await self._inner.ainvoke(ctx, **input)
        payload = {"tool": ctx.tool_name, "call_id": ctx.call_id, "args": input}
        await ctx.emit(TOOL_BEGIN, f"ev:{ctx.call_id}:begin", payload)
        await ctx.emit(
            TOOL_DENIED,
            f"ev:{ctx.call_id}:denied",
            {
                "tool": ctx.tool_name,
                "call_id": ctx.call_id,
                "ok": False,
                "error": "用户拒绝了该工具调用",
            },
        )
        return ToolResult(data=None, success=False, error="用户拒绝了该工具调用")


def gated_registry(source: ToolRegistry, gate: ApprovalGate) -> ToolRegistry:
    """复制一份注册表，每个工具都套上审批闸门。只用公开 API 读取原表。"""
    wrapped = ToolRegistry()
    for spec in source.specs:
        wrapped.register(GatedTool(source.resolve(spec.name), gate))
    return wrapped


def default_profiles() -> dict[str, Callable[[str], Agent]]:
    """内置 agent profile。新增 profile 只需要在这里加一个工厂函数。"""
    return {"demo": create_demo_agent}


class AgentRuntime:
    """可热切换 agent 的 runner 门面。

    ``ThreadRuntime`` 通过 ``run_with_context`` 属性判断 runner 支持五参数签名，
    所以这里两种签名都实现，转发给当前 agent 对应的 ``agent_runner`` 闭包。
    """

    def __init__(
        self,
        *,
        profiles: dict[str, Callable[[str], Agent]] | None = None,
        profile: str = DEFAULT_PROFILE,
        model: str = "",
        gate: ApprovalGate | None = None,
    ) -> None:
        self._profiles = profiles or default_profiles()
        if profile not in self._profiles:
            raise KeyError(f"unknown agent profile: {profile}")
        self.profile = profile
        self.gate = gate
        self._agent: Agent
        self._runner: Any
        self.model = model or self._probe_default_model(profile)
        self.rebuild()

    @property
    def profile_names(self) -> list[str]:
        return sorted(self._profiles)

    @property
    def tool_names(self) -> list[str]:
        return [spec.name for spec in self._agent.config.tools.specs]

    def set_model(self, model: str) -> None:
        self.model = model
        self.rebuild()

    def set_profile(self, profile: str) -> None:
        if profile not in self._profiles:
            raise KeyError(f"unknown agent profile: {profile}")
        self.profile = profile
        self.model = self._probe_default_model(profile)
        self.rebuild()

    def rebuild(self) -> None:
        """按当前 profile/model 重建 agent，并在有闸门时替换工具注册表。"""
        agent = self._profiles[self.profile](self.model)
        if self.gate is not None:
            config = dataclasses.replace(
                agent.config, tools=gated_registry(agent.config.tools, self.gate)
            )
            agent = Agent(
                config,
                name=agent.name,
                description=agent.description,
            )
        self._agent = agent
        self._runner = agent_runner(agent, agent.config.tools)

    async def __call__(self, thread: AthenaThread, turn: AthenaTurn, emit) -> Any:
        return await self._runner(thread, turn, emit)

    async def run_with_context(
        self, thread: AthenaThread, turn: AthenaTurn, emit, memory, cancel
    ) -> Any:
        return await self._runner.run_with_context(thread, turn, emit, memory, cancel)

    def _probe_default_model(self, profile: str) -> str:
        """默认模型：环境变量优先，其次读 profile 工厂的签名默认值。

        ``/agent`` 切 profile 时也走这里，所以 ``ATHENA_TUI_MODEL`` 在整个会话内
        都生效，不会被切换 profile 悄悄换回内置默认值。
        """
        override = os.environ.get(MODEL_ENV)
        if override:
            return override
        factory = self._profiles[profile]
        default = getattr(factory, "__defaults__", None)
        if default:
            return str(default[0])
        return FALLBACK_MODEL


class MockRunner:
    """离线 runner —— 无需 API key 就能跑通事件流与渲染。

    刻意复用真实的事件 kind（``agent/text_delta`` / ``tool/begin`` / ``tool/end``），
    让 mock 路径和真实路径走同一套归约与渲染代码。
    """

    def __init__(self, *, delay: float = 0.02) -> None:
        self._delay = delay

    async def run_with_context(
        self, thread: AthenaThread, turn: AthenaTurn, emit, memory, cancel
    ) -> AgentOutcome:
        await self._stream(emit, turn, f"收到：{turn.request_ref}\n\n")
        call_id = f"{turn.turn_id}:mock-{random.randint(1000, 9999)}"
        await emit(
            "tool/begin",
            f"ev:{call_id}:begin",
            {"tool": "list_dir", "call_id": call_id, "args": {"path": "."}},
        )
        await asyncio.sleep(self._delay * 5)
        await emit(
            "tool/end",
            f"ev:{call_id}:end",
            {
                "tool": "list_dir",
                "call_id": call_id,
                "ok": True,
                "preview": "  [DIR] src\n  [DIR] test\n  [FILE] pyproject.toml",
                "duration_ms": int(self._delay * 5000),
            },
        )
        await self._stream(emit, turn, MOCK_REPLY)
        return AgentOutcome(
            result_ref=f"result://{turn.turn_id}",
            next_context_ref=f"context://{turn.turn_id}/next",
        )

    async def __call__(self, thread: AthenaThread, turn: AthenaTurn, emit) -> Any:
        return await self.run_with_context(thread, turn, emit, None, asyncio.Event())

    async def _stream(self, emit, turn: AthenaTurn, text: str) -> None:
        """按词切片发 text_delta，模拟真实 provider 的累积语义。"""
        accumulated = ""
        for chunk in _chunks(text):
            accumulated += chunk
            await emit(
                "agent/text_delta",
                f"event:{turn.turn_id}",
                {"delta": chunk, "accumulated": accumulated},
            )
            await asyncio.sleep(self._delay)


def _chunks(text: str, size: int = 6) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
