"""Supervisor 编排工具投影（设计 agent-kernel-runtime §5.2）。

``RunToolProjector`` 每 turn 按 ``agent_type + RunSession`` 构造 ToolRegistry：
spawn/send/followup 的 source、parent 与项目范围来自当前 session；wait 工具成功
登记等待后以内部控制流终止当前 turn。内部信号与投影器名称不进入公共合同。
"""

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.session import RunSession
from athena.core.agent.types import AgentId
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolSpec


class _TurnEnded(BaseException):
    """wait 工具成功登记后终止当前 turn；由 BaseAgentRunner 捕获，不进入公共合同。"""

    def __init__(self, *, request_id: str | None = None) -> None:
        super().__init__()
        self.request_id = request_id


def _task_payload(input: dict) -> dict:
    """编排工具把 content/context_refs 组装为任务消息。"""
    return {
        "content": input.get("content", ""),
        "context_refs": input.get("context_refs", []),
    }


_MESSAGE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "agent_id": {"type": "string"},
        "content": {"type": "string", "default": ""},
        "context_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["agent_id"],
}
"""send / followup 共用的输入 schema。"""


# 静态权限矩阵（设计 registered-agent-catalog §5）：agent_type -> 允许 spawn 的类型
DEFAULT_PERMISSIONS: dict[str, set[str]] = {
    "data": {"plot"},
    "ideator": {"ideator", "reflection", "plot"},
    "code": {"plot"},
    "reflection": set(),
    "plot": set(),
}


class _RuntimeTool(BaseTool):
    """绑定 runtime 与调用方 agent_id；用于 spawn、send 和 wait。"""

    def __init__(self, runtime: AgentRuntime, agent_id: AgentId) -> None:
        self._runtime = runtime
        self._agent_id = agent_id


class _SpawnTool(_RuntimeTool):
    """创建新 Agent 实例；类型经静态权限矩阵校验（§5.2）。"""

    spec = ToolSpec(
        name="spawn",
        description="创建新 Agent 实例并返回其 id 与首个 Run id",
        input_schema={
            "type": "object",
            "properties": {
                "agent_type": {"type": "string"},
                "content": {"type": "string", "default": ""},
                "context_refs": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"},
            },
            "required": ["agent_type"],
        },
    )

    def __init__(
        self, runtime: AgentRuntime, parent_id: AgentId, allowed: set[str]
    ) -> None:
        super().__init__(runtime, agent_id=parent_id)
        self._allowed = allowed

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        """创建新 Agent 实例并返回其 id 与首个 Run id。"""
        agent_type = input["agent_type"]
        if agent_type not in self._allowed:
            raise PermissionError(
                f"agent_type {agent_type!r} not allowed for this agent"
            )
        agent_id, run_id = await self._runtime.spawn(
            self._agent_id, agent_type, _task_payload(input), name=input.get("name")
        )
        return {"agent_id": agent_id, "run_id": run_id}


class _SendTool(_RuntimeTool):
    """只投递消息，不触发目标 turn；source 为调用方 agent_id（§4.3）。"""

    spec = ToolSpec(
        name="send",
        description="向目标 Agent 投递消息，不唤醒目标",
        input_schema=_MESSAGE_INPUT_SCHEMA,
    )

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        """向目标 Agent 投递消息，不触发目标 turn。"""
        await self._runtime.send_message(
            input["agent_id"],
            input.get("content", ""),
            input.get("context_refs", []),
            source=self._agent_id,
        )
        return {"sent": True}


class _FollowupTool(BaseTool):
    """follow-up 原实例并创建新 turn；目标 agent 由输入指定。"""

    spec = ToolSpec(
        name="followup",
        description="向原 Agent 实例投递后续任务并创建新 turn",
        input_schema=_MESSAGE_INPUT_SCHEMA,
    )

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        """follow-up 原实例并创建新 turn，返回新 Run id。"""
        run_id = await self._runtime.followup(input["agent_id"], _task_payload(input))
        return {"run_id": run_id}


class _WaitForTool(_RuntimeTool):
    """持久化登记对目标 Agent 的依赖等待并结束当前 turn。"""

    spec = ToolSpec(
        name="wait_for",
        description="等待一组 Agent 完成并结束当前 turn",
        input_schema={
            "type": "object",
            "properties": {"agent_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["agent_ids"],
        },
    )

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        """登记对目标 Agent 的持久化等待并结束当前 turn。"""
        await self._runtime.wait_for(self._agent_id, input["agent_ids"])
        raise _TurnEnded()


class _WaitForHumanTool(_RuntimeTool):
    """持久化登记人工等待并结束当前 turn，返回稳定 request id。"""

    spec = ToolSpec(
        name="wait_for_human",
        description="请求用户确认并结束当前 turn",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "default": ""},
                "context_refs": {"type": "array", "items": {"type": "string"}},
            },
        },
    )

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        """登记人工等待并结束当前 turn，返回稳定 request id。"""
        request_id = await self._runtime.wait_for_human(
            self._agent_id,
            input.get("content", ""),
            input.get("context_refs", []),
        )
        raise _TurnEnded(request_id=request_id)


class RunToolProjector:
    """按 agent_type + RunSession 构造编排 ToolRegistry（§5.2）。

    权限由静态矩阵决定：spawn 只注入允许创建的类型；``plot`` 不获得创建业务
    子 Agent 的能力；wait 工具对可编排类型注入。
    """

    def __init__(self, permissions: dict[str, set[str]] | None = None) -> None:
        self._permissions = permissions or DEFAULT_PERMISSIONS

    @property
    def permissions(self) -> dict[str, set[str]]:
        """静态权限矩阵（agent_type -> 允许 spawn 的类型）。"""
        return self._permissions

    def build(self, agent_type: str, session: RunSession) -> ToolRegistry:
        """按 agent_type 构造编排 ToolRegistry，权限来自静态矩阵。"""
        registry = ToolRegistry()
        allowed = self._permissions.get(agent_type, set())
        runtime = session.runtime
        agent_id = session.agent_id
        registry.register(_SpawnTool(runtime, agent_id, allowed))
        registry.register(_SendTool(runtime, agent_id))
        registry.register(_FollowupTool(runtime))
        if agent_type != "plot":
            registry.register(_WaitForTool(runtime, agent_id))
            registry.register(_WaitForHumanTool(runtime, agent_id))
        return registry
