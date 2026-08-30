"""ReAct 循环的退化保护：连续重复同一工具调用时强制收尾。

``Agent.run`` 的唯一出口是"模型这一轮不调工具"。模型一旦退化成反复发同一个
tool call（temperature 0.1 下，上下文不再实质变化就必然如此），循环会一直烧到
``max_turns``。2026-08-29 真机实测：一个 EDA worker 逐字重复同一条 shell 命令
70 次，全程零产出；``_run_one`` 的 retries 又把它放大成 3 倍。

修法沿用业界 agent harness 的通行组合：先让模型**看见**自己在重复（把这件事写进
对话），再在结构上**剥夺**它继续调工具的能力（收尾轮不带任何工具）——此时 provider
走的正是本仓库既有的"无工具 → 严格 response_format"路径，输出必然是可校验的 JSON。
"""

import asyncio

from pydantic import BaseModel
import pytest

from athena.core.agent.models import AgentConfig, AgentContext
from athena.core.agent.provider import ResponsesProvider, StreamEvent
from athena.core.agent.runtime import Agent
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolSpec


class _Report(BaseModel):
    """收尾轮要求的结构化输出。"""

    summary: str
    handoff_file: str


_FINAL_JSON = '{"summary": "done", "handoff_file": "R.md"}'


class _ProbeTool(BaseTool):
    spec = ToolSpec(name="probe", description="probe", input_schema={})

    async def execute(self, input: dict, ctx: ToolContext):
        del input, ctx
        return "always the same result"


class _LoopingProvider:
    """永远发同一个 tool call；只有在收到空工具集时才给文本。"""

    def __init__(self) -> None:
        self.tool_turns = 0
        self.final_turns = 0

    async def stream(self, _config, tools, _messages, _cancel, **_kwargs):
        if not list(tools.specs):
            self.final_turns += 1
            yield StreamEvent(
                "text_delta", {"delta": _FINAL_JSON, "accumulated": _FINAL_JSON}
            )
        else:
            self.tool_turns += 1
            yield StreamEvent(
                "function_call",
                {
                    "call_id": f"call-{self.tool_turns}",
                    "name": "probe",
                    "arguments": {"q": "identical"},
                },
            )
        yield StreamEvent("response_completed")


class _VaryingProvider:
    """每轮参数都不同的正常 agent；断路器不该误伤它。"""

    def __init__(self, tool_turns: int) -> None:
        self.budget = tool_turns
        self.tool_turns = 0
        self.final_turns = 0

    async def stream(self, _config, tools, _messages, _cancel, **_kwargs):
        if not list(tools.specs):
            self.final_turns += 1
            yield StreamEvent(
                "text_delta", {"delta": _FINAL_JSON, "accumulated": _FINAL_JSON}
            )
        elif self.tool_turns < self.budget:
            self.tool_turns += 1
            yield StreamEvent(
                "function_call",
                {
                    "call_id": f"call-{self.tool_turns}",
                    "name": "probe",
                    "arguments": {"q": f"different-{self.tool_turns}"},
                },
            )
        else:
            yield StreamEvent(
                "text_delta", {"delta": _FINAL_JSON, "accumulated": _FINAL_JSON}
            )
        yield StreamEvent("response_completed")


def _agent(provider, *, max_turns: int = 30) -> tuple[Agent, AgentContext]:
    tools = ToolRegistry()
    tools.register(_ProbeTool())
    agent = Agent(
        ResponsesProvider("model"),
        tools,
        "system",
        AgentConfig(max_turns=max_turns),
        output_type=_Report,
    )
    agent.model = provider

    async def emit(_kind, _event_ref, _data=None):
        return None

    ctx = AgentContext(
        AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="ctx://0"
        ),
        AthenaTurn(
            turn_id="t1.1", thread_id="t1", request_ref="request", status="running"
        ),
        emit,
        tools,
        asyncio.Event(),
    )
    return agent, ctx


@pytest.mark.asyncio
async def test_identical_tool_calls_trigger_a_toolless_finalization() -> None:
    """连续重复同一调用达到阈值后，用无工具的收尾轮把结果逼出来。"""
    provider = _LoopingProvider()
    agent, ctx = _agent(provider)

    outcome = await agent.run(ctx)

    # 重复被及时掐断，而不是烧满 max_turns。
    assert provider.tool_turns == 3
    assert provider.final_turns == 1
    assert outcome.result_ref


@pytest.mark.asyncio
async def test_varying_tool_calls_are_not_interrupted() -> None:
    """参数每轮都变的正常 agent 不该被断路器误伤。"""
    provider = _VaryingProvider(tool_turns=6)
    agent, ctx = _agent(provider)

    outcome = await agent.run(ctx)

    assert provider.tool_turns == 6
    # 模型自己收的尾，没有触发强制收尾轮。
    assert provider.final_turns == 0
    assert outcome.result_ref
