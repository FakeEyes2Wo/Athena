"""Agent 私有记忆跨重启恢复（设计 §9、agent-memory-human-wait §2）。"""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import RolloutResourcesFactory
from athena.core.agent_kernel.types import AgentSpec, RunStatus

from ._support import JsonCodec


def _kernel(project_root: Path, runner) -> AgentKernel:
    kernel = AgentKernel(resources_factory=RolloutResourcesFactory(project_root))
    kernel._type_registry.register(
        "mem",
        lambda _aid, _cfg=None: AgentSpec(runner=runner, codec=JsonCodec()),
    )
    return kernel


class MemoryWriterRunner:
    """runner：把请求文本写入 session memory（同步追加 rollout）。"""

    async def run(self, request, *, session, emit) -> dict:
        session.append_message(
            ModelRequest(parts=[UserPromptPart(content=request["msg"])])
        )
        return {"ok": True}


@pytest.mark.asyncio
async def test_memory_survives_restart_via_rollout(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, MemoryWriterRunner())
    await kernel.start()
    agent_id, run1 = await kernel.create_root("mem", {"msg": "first"})
    summary = await kernel.wait_run(run1, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    context_ref = kernel._store.require_agent(agent_id).context_ref
    assert context_ref is not None and Path(context_ref).exists()
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    await kernel.aclose()

    # 重启：同一 project_root 工厂按 context_ref 恢复私有记忆
    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot,
        journal=journal,
        resources_factory=RolloutResourcesFactory(tmp_path),
        type_registry=kernel._type_registry,
        max_active_agents=8,
    )
    await recovered.start()
    session = recovered._sessions[agent_id]
    contents = [
        p.content
        for m in session.memory.items
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert "first" in contents  # 上一轮写入的消息已恢复
    await recovered.aclose()
