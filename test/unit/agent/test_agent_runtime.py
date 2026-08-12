import asyncio
import json

import pytest
from pydantic_ai.messages import ModelRequest

from ._support import BlockingAgent, make_runtime

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentConfig, AgentContext, AgentOutcome
from athena.core.agent.provider import StreamEvent
from athena.core.agent.runtime import Agent

from athena.core.agent.types import JsonCodec
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import (
    AgentCommandError,
    AgentSpec,
    AgentStatus,
    ErrorCode,
    RunStatus,
)
from athena.agents.base_runner import BaseAgentRunner
from athena.core.artifact_store import LocalArtifactStore
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry, tool
from athena.memory.context_manager import ContextManager


class FailingAgent:
    async def run(self, ctx):
        del ctx
        raise RuntimeError("analysis.py failed: TypeError: labels")


async def test_create_root_uses_caller_supplied_agent_id(tmp_path):
    rt = make_runtime(tmp_path)

    agent_id, run_id = await rt.create_root(
        "stub", {"content": "first"}, agent_id="hyp_vit"
    )

    assert agent_id == "hyp_vit"
    await rt.wait_run(run_id, timeout=5)
    assert (tmp_path / "sessions" / "hyp_vit.jsonl").is_file()
    await rt.aclose()


async def test_create_root_is_idempotent_for_same_stable_id_and_type(tmp_path):
    rt = make_runtime(tmp_path)

    first, second = await asyncio.gather(
        rt.create_root("stub", {"content": "first"}, agent_id="hyp_vit"),
        rt.create_root("stub", {"content": "first"}, agent_id="hyp_vit"),
    )

    assert second == first
    await rt.wait_run(first[1], timeout=5)
    assert len(rt.list_agents()) == 1
    await rt.aclose()


@pytest.mark.parametrize(
    "agent_id",
    [
        "",
        " ",
        "\t\r\n",
        "../x",
        "a/b",
        "a\\b",
        ".",
        "..",
        "a<b",
        "a>b",
        "a:b",
        'a"b',
        "a|b",
        "a?b",
        "a*b",
        "a\x01b",
        "name.",
        "name ",
        "CON",
        "con.txt",
        "PRN",
        "aux.csv",
        "NUL",
        "COM1",
        "com9.log",
        "LPT1",
        "lpt9.txt",
    ],
)
async def test_create_root_rejects_unsafe_stable_id(tmp_path, agent_id):
    rt = make_runtime(tmp_path)

    with pytest.raises(AgentCommandError) as exc_info:
        await rt.create_root("stub", {"content": "first"}, agent_id=agent_id)

    assert exc_info.value.code == ErrorCode.INVALID_REQUEST
    assert rt.list_agents() == []
    await rt.aclose()


async def test_create_root_rejects_stable_id_bound_to_different_type(tmp_path):
    rt = make_runtime(tmp_path)
    rt._registry.register(
        "other",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(FailingAgent()), codec=JsonCodec()
        ),
    )
    _, run_id = await rt.create_root("stub", {"content": "first"}, agent_id="hyp_vit")
    await rt.wait_run(run_id, timeout=5)

    with pytest.raises(AgentCommandError) as exc_info:
        await rt.create_root("other", {"content": "second"}, agent_id="hyp_vit")

    assert exc_info.value.code == ErrorCode.INVALID_REQUEST
    assert rt.agent_snapshot("hyp_vit").agent_type == "stub"
    await rt.aclose()


async def test_resume_agent_rejects_blank_and_wrong_type_stable_ids(tmp_path):
    rt = make_runtime(tmp_path)

    with pytest.raises(AgentCommandError) as blank:
        await rt.resume_agent(" ", agent_type="stub")
    assert blank.value.code == ErrorCode.INVALID_REQUEST

    _, run_id = await rt.create_root("stub", {"content": "first"}, agent_id="hyp_vit")
    await rt.wait_run(run_id, timeout=5)
    with pytest.raises(AgentCommandError) as collision:
        await rt.resume_agent("hyp_vit", agent_type="other")
    assert collision.value.code == ErrorCode.INVALID_REQUEST
    await rt.aclose()


def test_base_agent_runner_has_no_plaintext_log_writer(tmp_path):
    with pytest.raises(TypeError):
        BaseAgentRunner(FailingAgent(), log_dir=tmp_path)


async def test_create_root_and_followup_run_turns(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "hello"})
    assert agent_id and run_id
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    payload = json.loads(summary.response_ref)
    store = LocalArtifactStore(tmp_path / "artifacts")
    result = json.loads(await store.get_text(payload["result_ref"]))
    assert result["result"] == "hello"

    run2 = await rt.followup(agent_id, {"content": "world"})
    summary2 = await rt.wait_run(run2, timeout=5)
    payload2 = json.loads(summary2.response_ref)
    result2 = json.loads(await store.get_text(payload2["result_ref"]))
    assert result2["result"] == "world"
    await rt.aclose()


async def test_send_message_delivers_to_next_turn(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "first"})
    await rt.wait_run(run_id, timeout=5)
    await rt.send_message(agent_id, "queued-note")
    run2 = await rt.followup(agent_id, {"content": "second"})
    summary2 = await rt.wait_run(run2, timeout=5)
    payload2 = json.loads(summary2.response_ref)
    store = LocalArtifactStore(tmp_path / "artifacts")
    result2 = json.loads(await store.get_text(payload2["result_ref"]))
    assert result2["result"] == "second"  # trigger 仍是本轮 content
    await rt.aclose()


async def test_interrupt_marks_run_interrupted(tmp_path):
    gate = asyncio.Event()
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "block",
        lambda aid, cfg=None: AgentSpec(
            runner=BaseAgentRunner(BlockingAgent(gate)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    agent_id, run_id = await rt.create_root("block", {"content": "x"})
    await asyncio.wait_for(gate.wait(), timeout=2)  # 等 turn 进入运行
    assert rt.agent_status(agent_id) == AgentStatus.RUNNING
    await rt.interrupt(agent_id, "test stop")
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.INTERRUPTED
    await rt.aclose()


async def test_spawn_records_parent(tmp_path):
    rt = make_runtime(tmp_path)
    parent, run1 = await rt.create_root("stub", {"content": "p"})
    await rt.wait_run(run1, timeout=5)
    child, run2 = await rt.spawn(parent, "stub", {"content": "c"})
    await rt.wait_run(run2, timeout=5)
    snaps = {s.agent_id: s for s in rt.list_agents()}
    assert snaps[child].parent_id == parent
    assert snaps[child].path == (
        "root",
        "stub",
    )  # 名称缺省 = agent_type;root 固定名为 "root"
    await rt.aclose()


async def test_failed_run_summary_preserves_exception_message(tmp_path):
    registry = AgentTypeRegistry()
    registry.register(
        "fail",
        lambda aid, cfg=None: AgentSpec(
            runner=BaseAgentRunner(FailingAgent()), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()

    _agent_id, run_id = await rt.create_root("fail", {"content": "x"})
    summary = await rt.wait_run(run_id, timeout=5)

    assert summary.status == RunStatus.FAILED
    assert summary.error == "RuntimeError: analysis.py failed: TypeError: labels"
    await rt.aclose()


class _RecoveryProvider:
    """流式 fake provider：先调未知 ``edit_file`` → 看到可恢复错误 → 改调 ``write_file`` → 完成。"""

    def __init__(self) -> None:
        self.model_name = "fake"
        self.samples = 0

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        self.samples += 1
        if self.samples == 1:
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "c1",
                    "name": "edit_file",
                    "arguments": {"path": "a.py"},
                },
            )
        elif self.samples == 2:
            # 第二轮：确认上一轮 edit_file 的工具返回携带 available_tools，再改调已注册工具
            content = "".join(
                str(getattr(p, "content", ""))
                for m in messages
                if isinstance(m, ModelRequest)
                for p in m.parts
                if getattr(p, "part_kind", None) == "tool-return"
            )
            assert "unknown tool: edit_file" in content
            assert "available_tools" in content
            assert "write_file" in content
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "c2",
                    "name": "write_file",
                    "arguments": {"path": "a.txt", "content": "hi"},
                },
            )
        else:
            yield StreamEvent(
                kind="text_delta", data={"delta": "done", "accumulated": "done"}
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


async def _noop_emit(*_a: object) -> None:
    return None


async def test_unknown_tool_call_recovers_same_turn() -> None:
    """未知工具名 → 可恢复工具错误（列出可用工具），同 turn 改调已注册工具完成。"""
    provider = _RecoveryProvider()

    @tool
    async def write_file(path: str, content: str) -> dict:
        return {"path": path, "content": content}

    registry = ToolRegistry()
    registry.register(write_file)
    agent = Agent(provider, registry, "system", AgentConfig(max_turns=5))
    ctx = AgentContext(
        thread=AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="c1"
        ),
        turn=AthenaTurn(
            turn_id="t1-turn", thread_id="t1", request_ref="c1", status="running"
        ),
        emit=_noop_emit,
        cancel=asyncio.Event(),
        tools=registry,
        memory=ContextManager(),
        input_text="hello",
    )
    outcome = await agent.run(ctx)
    assert outcome.result_ref is not None
    # edit_file → 恢复后 write_file → 完成，同一 turn 内采样 3 次
    assert provider.samples == 3
