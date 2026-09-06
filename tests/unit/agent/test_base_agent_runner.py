"""BaseAgent 业务契约经适配器在 AgentRuntime 上运行（设计 §4.2）。

含 memory-flow-fixes §Mailbox To Memory 的 mailbox→memory 回归（信封 JSON 格式、
与 trigger 完全一致时不重复追加）。
"""

import json

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.agents.base_runner import BaseAgentRunner, _MAILBOX_PREFIX
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import BaseAgent
from athena.core.agent.types import AgentSpec, RunStatus

from ._support import JsonCodec


class EchoOutcomeAgent(BaseAgent):
    """确定性业务 Agent：读输入、返回持久化结果引用，不直接写 memory。"""

    def __init__(self) -> None:
        self.seen_inputs: list[str | None] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.seen_inputs.append(ctx.input_text)
        return AgentOutcome(result_ref=f"result://{ctx.input_text}")


def _runtime(agent: BaseAgent, tmp_path) -> AgentRuntime:
    registry = AgentTypeRegistry()
    registry.register(
        "echo",
        lambda _aid: AgentSpec(runner=BaseAgentRunner(agent), codec=JsonCodec()),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


@pytest.mark.asyncio
async def test_base_agent_runs_on_runtime(tmp_path) -> None:
    agent = EchoOutcomeAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run_id = await rt.create_root("echo", {"content": "hello"})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    response = JsonCodec().decode_response(summary.response_ref)
    assert response["result_ref"] == "result://hello"
    assert agent.seen_inputs == ["hello"]
    await rt.aclose()


@pytest.mark.asyncio
async def test_base_agent_survives_followup_with_memory(tmp_path) -> None:
    agent = EchoOutcomeAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root("echo", {"content": "first"})
    await rt.wait_run(run1, timeout=5)
    run2 = await rt.followup(agent_id, {"content": "second"})
    summary = await rt.wait_run(run2, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert agent.seen_inputs == ["first", "second"]
    await rt.aclose()


class MessageCollectingAgent(BaseAgent):
    """记录每轮收到的结构化消息。"""

    def __init__(self) -> None:
        self.messages: list = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.messages = list(ctx.messages)
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_agent_receives_structured_messages(tmp_path) -> None:
    """设计 §4.3：AgentContext.messages 按序包含触发请求与未读 mailbox。"""
    agent = MessageCollectingAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root("echo", {"content": "first"})
    await rt.wait_run(run1, timeout=5)
    await rt.send_message(agent_id, "unread-note")
    run2 = await rt.followup(agent_id, {"content": "second"})
    await rt.wait_run(run2, timeout=5)
    contents = [m.content for m in agent.messages]
    assert contents[0] == "second"  # 触发消息在前
    assert "unread-note" in contents  # 未读 mailbox 随后
    await rt.aclose()


class MessageProbeAgent(BaseAgent):
    """记录每轮 messages 的 content 列表。"""

    def __init__(self) -> None:
        self.recorded: list[list[str | None]] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.recorded.append([m.content for m in ctx.messages])
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_empty_request_has_no_fake_trigger(tmp_path) -> None:
    """设计 §4.4：空唤醒请求不生成假 trigger。"""
    agent = MessageProbeAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root("echo", {"content": "hello"})
    await rt.wait_run(run1, timeout=5)
    run2 = await rt.followup(agent_id, {})
    await rt.wait_run(run2, timeout=5)
    assert agent.recorded[0] == ["hello"]  # 首轮有 trigger
    assert agent.recorded[1] == []  # 空请求无假 trigger
    await rt.aclose()


@pytest.mark.asyncio
async def test_checkpoint_prevents_redelivery_on_next_run(tmp_path) -> None:
    """设计 §4.4：正常返回后提交 mailbox cursor，后续 run 不重投已读消息。"""
    agent = MessageProbeAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root("echo", {"content": "first"})
    await rt.wait_run(run1, timeout=5)
    await rt.send_message(agent_id, "note")
    run2 = await rt.followup(agent_id, {"content": "second"})
    await rt.wait_run(run2, timeout=5)
    assert "note" in agent.recorded[1]  # 第二轮读到未读
    run3 = await rt.followup(agent_id, {"content": "third"})
    await rt.wait_run(run3, timeout=5)
    assert agent.recorded[2] == ["third"]  # note 已提交游标，不再重投
    await rt.aclose()


class MemoryProbeAgent(BaseAgent):
    """记录每轮 memory 项；把非空 input_text 追加为用户消息（模拟业务 agent 写记忆）。"""

    def __init__(self) -> None:
        self.snapshots: list[list] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        if ctx.input_text:
            ctx.memory.append(
                ModelRequest(parts=[UserPromptPart(content=ctx.input_text)])
            )
        self.snapshots.append(ctx.memory.items)
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_mailbox_only_wake_appends_json_envelope_to_memory(tmp_path) -> None:
    """memory-flow-fixes：空唤醒把未读 mailbox 消息以稳定 JSON 信封落入 model memory。"""
    agent = MemoryProbeAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root("echo", {"content": "first"})
    await rt.wait_run(run1, timeout=5)

    await rt.send_message(agent_id, "mail-only", ["artifact://evidence"])
    run2 = await rt.followup(agent_id, {})  # 空唤醒：无 trigger，仅 mailbox 未读
    await rt.wait_run(run2, timeout=5)

    contents = [
        part.content
        for message in agent.snapshots[-1]
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    payload = json.loads(
        next(
            text for text in contents if text.startswith(f"{_MAILBOX_PREFIX}\n")
        ).split("\n", 1)[1]
    )
    assert payload == {
        "source": None,
        "content": "mail-only",
        "context_refs": ["artifact://evidence"],
    }
    await rt.aclose()


@pytest.mark.asyncio
async def test_matching_trigger_not_doubled_in_memory(tmp_path) -> None:
    """memory-flow-fixes：mailbox 消息与当前 trigger 完全一致时不追加（不重复）。"""
    agent = MemoryProbeAgent()
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root("echo", {"content": "first"})
    await rt.wait_run(run1, timeout=5)

    await rt.send_message(agent_id, "same", ["artifact://same"])
    run2 = await rt.followup(
        agent_id,
        {"content": "same", "context_refs": ["artifact://same"]},
    )
    await rt.wait_run(run2, timeout=5)

    # trigger 由业务 agent 追加为 input_text 用户消息（恰一次），无 mailbox 信封重复
    contents = [
        getattr(part, "content", "")
        for message in agent.snapshots[-1]
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert sum(content == "same" for content in contents) == 1
    assert not any(content.startswith(f"{_MAILBOX_PREFIX}\n") for content in contents)
    await rt.aclose()
