"""BaseAgent 业务契约经适配器在 AgentKernel 上运行（设计 §4.2）。"""

import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import AgentSpec, RunStatus

from ._support import JsonCodec


class EchoOutcomeAgent(BaseAgent):
    """确定性业务 Agent：读输入、返回持久化结果引用，不直接写 memory。"""

    def __init__(self) -> None:
        self.seen_inputs: list[str | None] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.seen_inputs.append(ctx.input_text)
        return AgentOutcome(result_ref=f"result://{ctx.input_text}")


def _kernel(agent: BaseAgent) -> AgentKernel:
    kernel = AgentKernel(resources_factory=InMemoryResourcesFactory())
    kernel._type_registry.register(
        "echo",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(agent), codec=JsonCodec()
        ),
    )
    return kernel


@pytest.mark.asyncio
async def test_base_agent_runs_on_kernel() -> None:
    agent = EchoOutcomeAgent()
    kernel = _kernel(agent)
    await kernel.start()
    agent_id, run_id = await kernel.create_root("echo", {"content": "hello"})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    response = JsonCodec().decode_response(summary.response_ref)
    assert response["result_ref"] == "result://hello"
    assert agent.seen_inputs == ["hello"]
    await kernel.aclose()


@pytest.mark.asyncio
async def test_base_agent_survives_followup_with_memory() -> None:
    agent = EchoOutcomeAgent()
    kernel = _kernel(agent)
    await kernel.start()
    agent_id, run1 = await kernel.create_root("echo", {"content": "first"})
    await kernel.wait_run(run1, timeout=2)
    run2 = await kernel.followup(agent_id, {"content": "second"})
    summary = await kernel.wait_run(run2, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert agent.seen_inputs == ["first", "second"]
    await kernel.aclose()


class MessageCollectingAgent(BaseAgent):
    """记录每轮收到的结构化消息。"""

    def __init__(self) -> None:
        self.messages: list = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.messages = list(ctx.messages)
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_agent_receives_structured_messages() -> None:
    """设计 §4.3：AgentContext.messages 按序包含触发请求与未读 mailbox。"""
    agent = MessageCollectingAgent()
    kernel = _kernel(agent)
    await kernel.start()
    agent_id, run1 = await kernel.create_root("echo", {"content": "first"})
    await kernel.wait_run(run1, timeout=2)
    await kernel.send_message(agent_id, "unread-note")
    run2 = await kernel.followup(agent_id, {"content": "second"})
    await kernel.wait_run(run2, timeout=2)
    contents = [m.content for m in agent.messages]
    assert contents[0] == "second"  # 触发消息在前
    assert "unread-note" in contents  # 未读 mailbox 随后
    await kernel.aclose()


class MessageProbeAgent(BaseAgent):
    """记录每轮 messages 的 content 列表。"""

    def __init__(self) -> None:
        self.recorded: list[list[str | None]] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.recorded.append([m.content for m in ctx.messages])
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_empty_request_has_no_fake_trigger() -> None:
    """设计 §4.4：空唤醒请求不生成假 trigger。"""
    agent = MessageProbeAgent()
    kernel = _kernel(agent)
    await kernel.start()
    agent_id, run1 = await kernel.create_root("echo", {"content": "hello"})
    await kernel.wait_run(run1, timeout=2)
    run2 = await kernel.followup(agent_id, {})
    await kernel.wait_run(run2, timeout=2)
    assert agent.recorded[0] == ["hello"]  # 首轮有 trigger
    assert agent.recorded[1] == []  # 空请求无假 trigger
    await kernel.aclose()


@pytest.mark.asyncio
async def test_checkpoint_prevents_redelivery_on_next_run() -> None:
    """设计 §4.4：正常返回后提交 mailbox cursor，后续 run 不重投已读消息。"""
    agent = MessageProbeAgent()
    kernel = _kernel(agent)
    await kernel.start()
    agent_id, run1 = await kernel.create_root("echo", {"content": "first"})
    await kernel.wait_run(run1, timeout=2)
    await kernel.send_message(agent_id, "note")
    run2 = await kernel.followup(agent_id, {"content": "second"})
    await kernel.wait_run(run2, timeout=2)
    assert "note" in agent.recorded[1]  # 第二轮读到未读
    run3 = await kernel.followup(agent_id, {"content": "third"})
    await kernel.wait_run(run3, timeout=2)
    assert agent.recorded[2] == ["third"]  # note 已提交游标，不再重投
    await kernel.aclose()
