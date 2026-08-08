"""BaseAgent -> AgentKernel 的 runner 适配器（设计 §4.2、agent-kernel-runtime §4.1）。

业务 Agent 遵循 ``BaseAgent.run(AgentContext) -> AgentOutcome`` 公共契约；
Kernel 的 runner 协议是 ``run(request, *, session, emit)``。本适配器在每个 turn
构造最小 :class:`AgentContext`（thread/turn 为派生展示值，memory 为会话私有
上下文），并把 ``AgentOutcome`` 的持久化引用作为响应返回。
"""

import asyncio

from athena.agents.orchestration import RunToolProjector, _TurnEnded
from athena.core.agent.models import AgentContext
from athena.core.agent.runtime import BaseAgent
from athena.core.agent.types import AgentMessage
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry


class BaseAgentRunner:
    """把 :class:`BaseAgent` 适配为 Kernel 的 runner。"""

    def __init__(
        self,
        agent: BaseAgent,
        *,
        tools: ToolRegistry | None = None,
        projector: RunToolProjector | None = None,
        agent_type: str = "agent",
    ) -> None:
        self._agent = agent
        self._base_tools = tools or ToolRegistry()
        self._projector = projector
        self._agent_type = agent_type

    async def run(self, request, *, session, emit) -> dict:
        """构造 AgentContext 并运行业务 Agent，返回持久化引用。

        COMPAT: wait 唤醒的空请求不生成假 trigger(kernel §4.4 语义);只有正常返回
        或进入持久化等待后才提交 mailbox cursor。清理条件: 等待语义内建到 thread 后。
        """
        # 触发消息：真实 Run 请求解码；空唤醒（{}）无 trigger
        trigger = None
        if isinstance(request, dict) and request.get("content") is not None:
            trigger = AgentMessage(
                source="user",
                content=request["content"],
                context_refs=request.get("context_refs") or [],
            )
        unread = session.receive_messages()
        input_text = trigger.content if trigger is not None else ""
        messages = [trigger, *unread] if trigger is not None else unread
        # 每 turn 按 agent_type + RunSession 投影编排工具（设计 §5.2）
        tools = (
            self._projector.build(self._agent_type, session)
            if self._projector is not None
            else self._base_tools
        )
        ctx = AgentContext(
            thread=AthenaThread(
                thread_id=session.agent_id,
                session_id=session.agent_id,
                status="running",
                context_ref=session.context_ref,
            ),
            turn=AthenaTurn(
                turn_id=f"{session.agent_id}-turn",
                thread_id=session.agent_id,
                request_ref=session.context_ref,
                status="running",
            ),
            emit=emit,
            tools=tools,
            cancel=asyncio.Event(),
            memory=session.memory.raw,
            input_text=input_text,  # 迁移期兼容视图：仅当前触发消息 content
            messages=messages,
        )
        try:
            outcome = await self._agent.run(ctx)
        except _TurnEnded:
            # wait 工具已登记等待：turn 干净结束，模型输出与后续调用不可提交
            session.checkpoint()
            return {"result_ref": None, "wait": "registered"}
        session.checkpoint()  # 正常返回才推进已读 mailbox 游标
        # Kernel 只认 result_ref；next_context_ref 为迁移字段，忽略（§4.3）
        return {"result_ref": outcome.result_ref}
