"""RunSession — 门面构造的每 turn 受限视图(COMPAT: 保留 kernel RunSession 消费接口)。

清理条件: AgentRunner 协议统一为线程 runner、BaseAgentRunner 不再消费 session 后。
mailbox 读即清,checkpoint 为空操作。
"""

from typing import Any

from pydantic_ai.messages import ModelMessage

from athena.core.agent.types import AgentId, AgentMessage
from athena.memory.context_manager import ContextManager


class _MemoryView:
    """ContextManager 只读视图;``raw`` 暴露底层 ContextManager(BaseAgent 适配器用)。"""

    def __init__(self, memory: ContextManager, *, allow_rollback: bool = True) -> None:
        self._memory = memory
        self._allow_rollback = allow_rollback

    @property
    def raw(self) -> ContextManager:
        return self._memory

    @property
    def items(self):
        return self._memory.items

    @property
    def tokens(self):
        return self._memory.tokens

    @property
    def version(self):
        return self._memory.version

    @property
    def limit(self):
        return self._memory.limit

    def token_margin(self, ratio: float = 0.85):
        return self._memory.token_margin(ratio)

    def snapshot(self):
        return self._memory.snapshot()

    def items_since(self, idx: int):
        return self._memory.items_since(idx)

    def rollback(self, idx: int) -> None:
        if not self._allow_rollback:
            raise AttributeError("rollback is runtime-internal")
        self._memory.rollback(idx)

    def append(self, msg: ModelMessage) -> None:
        raise AttributeError("memory writes go through ThreadRuntime")

    def replace_range(self, *args, **kwargs) -> None:
        raise AttributeError("memory writes go through ThreadRuntime")


class RunSession:
    """每 turn 的受限 session 视图;未读 mailbox 读即清(=checkpoint)。"""

    def __init__(
        self,
        *,
        agent_id: AgentId,
        kernel: Any,
        context_ref: str,
        memory: ContextManager,
        mailbox: list[AgentMessage],
    ) -> None:
        self._agent_id = agent_id
        self._kernel = kernel
        self._context_ref = context_ref
        self._memory_view = _MemoryView(memory, allow_rollback=False)
        self._mailbox = mailbox

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @property
    def kernel(self) -> Any:
        return self._kernel

    @property
    def context_ref(self) -> str:
        return self._context_ref

    @property
    def memory(self) -> _MemoryView:
        return self._memory_view

    def receive_messages(self) -> list[AgentMessage]:
        unread = list(self._mailbox)
        self._mailbox.clear()
        return unread

    def checkpoint(self) -> None:
        pass  # COMPAT: mailbox 读即清,游标推进为空操作;清理条件: BaseAgentRunner 不再依赖 session.checkpoint 后
