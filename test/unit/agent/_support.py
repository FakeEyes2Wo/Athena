import asyncio
import json
from pathlib import Path

from athena.agents.base_runner import BaseAgentRunner
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent

# COMPAT: Task 6 迁移后改 from athena.core.agent.{codec,registry,types} import ...
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.registry import AgentTypeRegistry
from athena.core.agent_kernel.types import AgentSpec
from athena.core.artifact_store import LocalArtifactStore


class StubAgent(BaseAgent):
    """把 input_text 写为 Artifact 的确定性 agent。"""

    def __init__(self, store):
        self._store = store

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        ref = await self._store.put_text(json.dumps({"result": ctx.input_text}))
        return AgentOutcome(result_ref=ref)


class BlockingAgent(BaseAgent):
    """置位 gate 后阻塞,等待被中断。"""

    def __init__(self, gate: asyncio.Event):
        self._gate = gate

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self._gate.set()
        await asyncio.sleep(30)
        return AgentOutcome(result_ref="art:never")


def make_runtime(tmp_path: Path) -> AgentRuntime:
    """构造只注册 ``stub`` 类型的门面。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "stub",
        lambda aid, cfg=None: AgentSpec(
            runner=BaseAgentRunner(StubAgent(store)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / "sessions",
    )
    rt.start()
    return rt
