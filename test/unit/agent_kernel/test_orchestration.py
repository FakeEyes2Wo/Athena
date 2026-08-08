"""编排工具权限矩阵测试（设计 §5.2、registered-agent-catalog §5）。"""

import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.orchestration import RunToolProjector
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import AgentSpec, RunStatus
from athena.core.tool_types import ToolContext


class _SimpleAgent(BaseAgent):
    """占位业务 Agent：返回结果引用。"""

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        return AgentOutcome(result_ref="result://ok")


class SpawningAgent(BaseAgent):
    """尝试 spawn 指定类型的业务 Agent；捕获 PermissionError。"""

    def __init__(self, target: str) -> None:
        self.target = target
        self.spawn_error: str | None = None
        self.child_ids: list[str] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        spawn = ctx.tools.resolve("spawn")
        result = await spawn.ainvoke(
            ToolContext("spawn", "c1", ctx.emit, ctx.cancel),
            agent_type=self.target,
        )
        if result.success:
            self.child_ids.append(result.data["agent_id"])
        else:
            self.spawn_error = result.error
        return AgentOutcome(result_ref="result://ok")


def _kernel(agent: BaseAgent, agent_type: str) -> AgentKernel:
    kernel = AgentKernel(resources_factory=InMemoryResourcesFactory())
    runner = BaseAgentRunner(agent, projector=RunToolProjector(), agent_type=agent_type)
    kernel._type_registry.register(
        agent_type, lambda _aid, _cfg=None: AgentSpec(runner=runner, codec=JsonCodec())
    )
    kernel._type_registry.register(
        "plot",
        lambda _aid, _cfg=None: AgentSpec(runner=_SimpleAgent(), codec=JsonCodec()),
    )
    return kernel


@pytest.mark.asyncio
async def test_data_agent_can_spawn_plot() -> None:
    agent = SpawningAgent("plot")
    kernel = _kernel(agent, "data")
    await kernel.start()
    agent_id, run = await kernel.create_root("data", {"content": ""})
    summary = await kernel.wait_run(run, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert agent.spawn_error is None  # data 可 spawn plot（§5）
    assert agent.child_ids  # plot 子已创建
    await kernel.aclose()


@pytest.mark.asyncio
async def test_data_agent_cannot_spawn_data() -> None:
    agent = SpawningAgent("data")
    kernel = _kernel(agent, "data")
    await kernel.start()
    agent_id, run = await kernel.create_root("data", {"content": ""})
    summary = await kernel.wait_run(run, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert agent.spawn_error is not None  # data 不可 spawn 另一 data
    assert not agent.child_ids
    await kernel.aclose()


def test_default_permission_matrix_shapes() -> None:
    """registered-agent-catalog §5：supervisor 全量；plot 不创建业务子 Agent。"""
    permissions = RunToolProjector().permissions
    assert "data" in permissions["supervisor"]
    assert permissions["plot"] == set()
    assert permissions["data"] == {"plot"}
    assert "data" not in permissions["data"]  # data 不可 spawn 另一 data


class SenderAgent(BaseAgent):
    """用 send 工具向目标投递消息。"""

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        send = ctx.tools.resolve("send")
        await send.ainvoke(
            ToolContext("send", "c1", ctx.emit, ctx.cancel),
            agent_id=ctx.input_text,
            content="note",
        )
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_send_tool_source_is_calling_agent() -> None:
    """设计 §4.3：Agent 工具调用的消息 source = 调用方 agent_id。"""
    kernel = AgentKernel(resources_factory=InMemoryResourcesFactory())
    sender = SenderAgent()
    kernel._type_registry.register(
        "sender",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(
                sender, projector=RunToolProjector(), agent_type="data"
            ),
            codec=JsonCodec(),
        ),
    )
    kernel._type_registry.register(
        "target",
        lambda _aid, _cfg=None: AgentSpec(runner=_SimpleAgent(), codec=JsonCodec()),
    )
    await kernel.start()
    target_id, _ = await kernel.create_root(
        "target", {"content": ""}, name="target-root"
    )
    sender_id, run = await kernel.create_root(
        "sender", {"content": target_id}, name="sender-root"
    )
    await kernel.wait_run(run, timeout=2)
    msgs = kernel._store.mailbox(target_id)
    assert msgs and msgs[0].source == sender_id  # 非 user
    assert msgs[0].content == "note"
    await kernel.aclose()
