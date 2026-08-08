"""Supervisor 编排工具投影（设计 agent-kernel-runtime §5.2）。

``RunToolProjector`` 每 turn 按 ``agent_type + RunSession`` 构造 ToolRegistry：
spawn/send/followup 的 source、parent 与项目范围来自当前 session；wait 工具成功
登记等待后以内部控制流终止当前 turn。内部信号与投影器名称不进入公共合同。
"""

from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import RunSession
from athena.core.agent_kernel.types import AgentId
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


# 静态权限矩阵（设计 registered-agent-catalog §5）：agent_type -> 允许 spawn 的类型
DEFAULT_PERMISSIONS: dict[str, set[str]] = {
    "supervisor": {"data", "plot", "reflection", "ideator", "code", "report"},
    "data": {"plot"},
    "ideator": {"ideator", "reflection", "plot"},
    "code": {"plot"},
    "report": {"plot"},
    "reflection": set(),
    "plot": set(),
}


class _SpawnTool(BaseTool):
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
        self, kernel: AgentKernel, parent_id: AgentId, allowed: set[str]
    ) -> None:
        self._kernel = kernel
        self._parent = parent_id
        self._allowed = allowed

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        agent_type = input["agent_type"]
        if agent_type not in self._allowed:
            raise PermissionError(
                f"agent_type {agent_type!r} not allowed for this agent"
            )
        agent_id, run_id = await self._kernel.spawn(
            self._parent, agent_type, _task_payload(input), name=input.get("name")
        )
        return {"agent_id": agent_id, "run_id": run_id}


class _SendTool(BaseTool):
    """只投递消息，不触发目标 turn；source 为调用方 agent_id（§4.3）。"""

    spec = ToolSpec(
        name="send",
        description="向目标 Agent 投递消息，不唤醒目标",
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "content": {"type": "string", "default": ""},
                "context_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["agent_id"],
        },
    )

    def __init__(self, kernel: AgentKernel, agent_id: AgentId) -> None:
        self._kernel = kernel
        self._agent_id = agent_id

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        await self._kernel.send_message(
            input["agent_id"],
            input.get("content", ""),
            input.get("context_refs", []),
            source=self._agent_id,
        )
        return {"sent": True}


class _FollowupTool(BaseTool):
    """follow-up 原实例并创建新 turn。"""

    spec = ToolSpec(
        name="followup",
        description="向原 Agent 实例投递后续任务并创建新 turn",
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "content": {"type": "string", "default": ""},
                "context_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["agent_id"],
        },
    )

    def __init__(self, kernel: AgentKernel) -> None:
        self._kernel = kernel

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        run_id = await self._kernel.followup(input["agent_id"], _task_payload(input))
        return {"run_id": run_id}


class _WaitForTool(BaseTool):
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

    def __init__(self, kernel: AgentKernel, agent_id: AgentId) -> None:
        self._kernel = kernel
        self._agent_id = agent_id

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        await self._kernel.wait_for(self._agent_id, input["agent_ids"])
        raise _TurnEnded()


class _WaitForHumanTool(BaseTool):
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

    def __init__(self, kernel: AgentKernel, agent_id: AgentId) -> None:
        self._kernel = kernel
        self._agent_id = agent_id

    async def execute(self, input: dict, ctx: ToolContext) -> dict:
        request_id = await self._kernel.wait_for_human(
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
        registry = ToolRegistry()
        allowed = self._permissions.get(agent_type, set())
        kernel = session.kernel
        agent_id = session.agent_id
        registry.register(_SpawnTool(kernel, agent_id, allowed))
        registry.register(_SendTool(kernel, agent_id))
        registry.register(_FollowupTool(kernel))
        if agent_type != "plot":
            registry.register(_WaitForTool(kernel, agent_id))
            registry.register(_WaitForHumanTool(kernel, agent_id))
        return registry
