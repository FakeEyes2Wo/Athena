"""编排工具权限矩阵测试（设计 §5.2、registered-agent-catalog §5）。"""

import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.orchestration import RunToolProjector
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.codec import JsonCodec
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import BaseAgent
from athena.core.agent.types import AgentSpec, RunStatus
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


def _runtime(agent: BaseAgent, agent_type: str, tmp_path) -> AgentRuntime:
    registry = AgentTypeRegistry()
    runner = BaseAgentRunner(agent, projector=RunToolProjector(), agent_type=agent_type)
    registry.register(
        agent_type, lambda _aid, _cfg=None: AgentSpec(runner=runner, codec=JsonCodec())
    )
    registry.register(
        "plot",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(_SimpleAgent()), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


@pytest.mark.asyncio
async def test_data_agent_can_spawn_plot(tmp_path) -> None:
    agent = SpawningAgent("plot")
    rt = _runtime(agent, "data", tmp_path)
    agent_id, run = await rt.create_root("data", {"content": ""})
    summary = await rt.wait_run(run, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert agent.spawn_error is None  # data 可 spawn plot（§5）
    assert agent.child_ids  # plot 子已创建
    await rt.aclose()


@pytest.mark.asyncio
async def test_data_agent_cannot_spawn_data(tmp_path) -> None:
    agent = SpawningAgent("data")
    rt = _runtime(agent, "data", tmp_path)
    agent_id, run = await rt.create_root("data", {"content": ""})
    summary = await rt.wait_run(run, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert agent.spawn_error is not None  # data 不可 spawn 另一 data
    assert not agent.child_ids
    await rt.aclose()


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


class RecordingTarget(BaseAgent):
    """记录每轮 ctx.messages，观察 send 工具投递的 mailbox 消息。"""

    def __init__(self) -> None:
        self.recorded: list[list] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.recorded.append(list(ctx.messages))
        return AgentOutcome(result_ref="result://ok")


@pytest.mark.asyncio
async def test_send_tool_source_is_calling_agent(tmp_path) -> None:
    """设计 §4.3：Agent 工具调用的消息 source = 调用方 agent_id。"""
    registry = AgentTypeRegistry()
    target_agent = RecordingTarget()
    registry.register(
        "target",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(target_agent), codec=JsonCodec()
        ),
    )
    sender = SenderAgent()
    registry.register(
        "sender",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(
                sender, projector=RunToolProjector(), agent_type="data"
            ),
            codec=JsonCodec(),
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    target_id, _ = await rt.create_root("target", {"content": ""}, name="target-root")
    sender_id, run = await rt.create_root(
        "sender", {"content": target_id}, name="sender-root"
    )
    await rt.wait_run(run, timeout=5)
    # 触发 target 一轮，读取 mailbox 中由 sender 工具投递的消息
    follow = await rt.followup(target_id, {"content": ""})
    await rt.wait_run(follow, timeout=5)
    last = target_agent.recorded[-1]
    note = next(m for m in last if m.content == "note")
    assert note.source == sender_id  # 非 user
    await rt.aclose()
