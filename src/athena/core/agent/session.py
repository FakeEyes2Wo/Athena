"""Runner session with thread-owned memory and explicit mailbox acknowledgement."""

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from athena.core.agent.types import AgentId, AgentMessage
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from athena.core.agent.agent_runtime import AgentRuntime


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class RunSession:
    """Stable turn bindings; ThreadRuntime checkpoints mutations to memory."""

    agent_id: AgentId
    runtime: "AgentRuntime"
    context_ref: str
    memory: ContextManager
    mailbox: list[AgentMessage] | deque[AgentMessage] = field(repr=False)

    def receive_messages(self) -> list[AgentMessage]:
        """Read pending messages without acknowledging them."""
        # 只读不删：turn 失败时（memory 已回滚）未读消息须保留待重试，
        # 由 checkpoint() 在正常返回/进入等待后才提交消费。
        return list(self.mailbox)

    def checkpoint(self) -> None:
        """Acknowledge mailbox consumption after a successful or waiting turn."""
        # 提交 mailbox 消费：仅正常返回或进入持久化等待后调用，失败路径不调用，
        # 故在此才清空，保证失败 turn 的消息不被吞掉。
        self.mailbox.clear()
