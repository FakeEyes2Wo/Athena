"""Agent 私有记忆跨重启恢复（设计 §9、agent-memory-human-wait §2）。

旧 AgentKernel 经 GraphStore snapshot 恢复；新 AgentRuntime 走 Codex 风格
确定性 rollout（``{rollout_dir}/{agent_id}.jsonl``），重开项目按 agent_id
经 ``resume_agent`` 自动恢复会话记忆。
"""

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, RunStatus

from ._support import JsonCodec


class MemoryWriterRunner:
    """runner：记录已恢复的 user 消息，并把请求文本写入 session memory。"""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def run(self, request, *, session, emit) -> dict:
        contents = [
            p.content
            for m in session.memory.raw.items
            for p in m.parts
            if isinstance(p, UserPromptPart)
        ]
        self.seen.append(contents)
        session.memory.raw.append(
            ModelRequest(parts=[UserPromptPart(content=request["msg"])])
        )
        return {"ok": True}


def _runtime(project_root, runner) -> AgentRuntime:
    registry = AgentTypeRegistry()
    registry.register(
        "mem",
        lambda _aid, _cfg=None: AgentSpec(runner=runner, codec=JsonCodec()),
    )
    return AgentRuntime(
        type_registry=registry,
        project_root=project_root,
        rollout_dir=project_root / "sessions",
    )


@pytest.mark.asyncio
async def test_memory_survives_restart_via_rollout(tmp_path) -> None:
    runner1 = MemoryWriterRunner()
    rt1 = _runtime(tmp_path, runner1)
    rt1.start()
    agent_id, run1 = await rt1.create_root("mem", {"msg": "first"})
    summary = await rt1.wait_run(run1, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert runner1.seen == [[]]  # 首轮：空上下文，随后写入 "first"
    await rt1.aclose()

    # 重启：同一 project_root + rollout_dir → 按 agent_id 确定性恢复私有记忆
    runner2 = MemoryWriterRunner()
    rt2 = _runtime(tmp_path, runner2)
    rt2.start()
    await rt2.resume_agent(agent_id, agent_type="mem")
    run2 = await rt2.followup(agent_id, {"msg": "second"})
    summary = await rt2.wait_run(run2, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert runner2.seen == [["first"]]  # 上一轮写入的消息已恢复
    await rt2.aclose()
