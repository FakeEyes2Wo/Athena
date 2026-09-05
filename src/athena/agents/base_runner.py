"""BaseAgent -> AgentRuntime 的 runner 适配器（设计 §4.2、agent-kernel-runtime §4.1）。

业务 Agent 遵循 ``BaseAgent.run(AgentContext) -> AgentOutcome`` 公共契约；
AgentRuntime 的 runner 协议是 ``run(request, *, session, emit)``。本适配器在每个
turn 构造最小 :class:`AgentContext`（thread/turn 为派生展示值，memory 为会话私有
上下文），并把 ``AgentOutcome`` 的持久化引用作为响应返回。
"""

import asyncio
import json
from collections.abc import Callable

from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.agents.orchestration import RunToolProjector, _TurnEnded
from athena.core.agent.models import AgentContext
from athena.core.agent.runtime import BaseAgent
from athena.core.agent.types import AgentMessage
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import AskUser

_MAILBOX_PREFIX = "[ATHENA MAILBOX MESSAGE]"


def _same_message(left: AgentMessage | None, right: AgentMessage) -> bool:
    """mailbox 消息与当前 trigger 是否完全一致（内容 + context_refs）。"""
    return left is not None and (
        left.content == right.content and left.context_refs == right.context_refs
    )


def _append_mailbox_messages(
    memory, trigger: AgentMessage | None, unread: list[AgentMessage]
) -> None:
    """把未读 mailbox 消息以稳定 JSON 信封追加到 model memory。

    ``_MAILBOX_PREFIX`` 行 + 紧凑 JSON（source/content/context_refs），供 model
    阅读并随 rollout 重放；与 trigger 完全一致的消息跳过，避免 send-plus-follow-up
    重复同一 prompt。
    """
    for message in unread:
        if _same_message(trigger, message):
            continue
        payload = {
            "source": message.source,
            "content": message.content,
            "context_refs": message.context_refs,
        }
        memory.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=(
                            f"{_MAILBOX_PREFIX}\n"
                            + json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            )
                        )
                    )
                ]
            )
        )


class BaseAgentRunner:
    """把 :class:`BaseAgent` 适配为 AgentRuntime 的 runner。"""

    def __init__(
        self,
        agent: BaseAgent,
        *,
        tools: ToolRegistry | None = None,
        projector: RunToolProjector | None = None,
        agent_type: str = "agent",
        ask_user: "Callable[[AthenaThread, AthenaTurn], AskUser | None] | None" = None,
    ) -> None:
        self._agent = agent
        self._base_tools = tools or ToolRegistry()
        self._projector = projector
        self._agent_type = agent_type
        self._ask_user = ask_user

    async def run(self, request, *, session, emit) -> dict:
        """构造 AgentContext 并运行业务 Agent，返回持久化引用。

        COMPAT: wait 唤醒的空请求不生成假 trigger(退役 AgentKernel §4.4 语义);只有
        正常返回或进入持久化等待后才提交 mailbox cursor。清理条件: 等待语义内建到 thread 后。
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
        # Mailbox → model memory（memory-flow-fixes §Mailbox To Memory）：未读消息以
        # 稳定信封追加到会话 ContextManager。发生在 ThreadRuntime 快照之后 → turn
        # 失败时 rollback 一并移除；与 trigger 完全一致的消息跳过。
        _append_mailbox_messages(session.memory, trigger, unread)
        input_text = trigger.content if trigger is not None else ""
        messages = [trigger, *unread] if trigger is not None else unread
        # 每 turn 按 agent_type + RunSession 投影编排工具（设计 §5.2）
        tools = (
            self._projector.build(self._agent_type, session)
            if self._projector is not None
            else self._base_tools
        )
        thread = AthenaThread(
            thread_id=session.agent_id,
            session_id=session.runtime.session_id,
            status="running",
            context_ref=session.context_ref,
        )
        turn = AthenaTurn(
            turn_id=f"{session.agent_id}-turn",
            thread_id=session.agent_id,
            request_ref=session.context_ref,
            status="running",
        )
        ctx = AgentContext(
            thread=thread,
            turn=turn,
            emit=emit,
            tools=tools,
            cancel=asyncio.Event(),
            memory=session.memory,
            input_text=input_text,  # 迁移期兼容视图：仅当前触发消息 content
            messages=messages,
            ask_user=self._ask_user(thread, turn) if self._ask_user else None,
        )
        try:
            outcome = await self._agent.run(ctx)
        except _TurnEnded:
            # wait 工具已登记等待：turn 干净结束，模型输出与后续调用不可提交
            session.checkpoint()
            return {"result_ref": None, "wait": "registered"}
        session.checkpoint()  # 正常返回才推进已读 mailbox 游标
        # AgentRuntime 只认 result_ref；next_context_ref 为迁移字段，忽略（§4.3）
        return {"result_ref": outcome.result_ref}
