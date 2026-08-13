"""RunSession — 门面构造的每 turn 受限视图(COMPAT: 保留退役 kernel 的 RunSession 消费接口)。

清理条件: AgentRunner 协议统一为线程 runner、BaseAgentRunner 不再消费 session 后。
mailbox 读即清,checkpoint 为空操作。
"""

from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelMessage

from athena.core.agent.types import AgentId, AgentMessage
from athena.memory.context_manager import ContextManager

if TYPE_CHECKING:
    from athena.core.agent.agent_runtime import AgentRuntime


class _MemoryView:
    """ContextManager 只读视图；``raw`` 暴露底层 ContextManager（BaseAgent 适配器用）。

    COMPAT: 保留退役 kernel 的 _MemoryView 消费接口(BaseAgent 适配器经 ``raw`` 访问
    ContextManager);清理条件: AgentRunner 协议统一为线程 runner、BaseAgentRunner
    不再消费 session 后。
    """

    def __init__(self, memory: ContextManager) -> None:
        self._memory = memory

    @property
    def raw(self) -> ContextManager:
        return self._memory

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
        runtime: "AgentRuntime",
        context_ref: str,
        memory: ContextManager,
        mailbox: list[AgentMessage],
    ) -> None:
        self._agent_id = agent_id
        self._runtime = runtime
        self._context_ref = context_ref
        self._memory_view = _MemoryView(memory)
        self._mailbox = mailbox

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @property
    def runtime(self) -> "AgentRuntime":
        return self._runtime

    @property
    def context_ref(self) -> str:
        return self._context_ref

    @property
    def memory(self) -> _MemoryView:
        return self._memory_view

    def receive_messages(self) -> list[AgentMessage]:
        # 只读不删：turn 失败时（memory 已回滚）未读消息须保留待重试，
        # 由 checkpoint() 在正常返回/进入等待后才提交消费。
        return list(self._mailbox)

    def checkpoint(self) -> None:
        # 提交 mailbox 消费：仅正常返回或进入持久化等待后调用，失败路径不调用，
        # 故在此才清空，保证失败 turn 的消息不被吞掉。
        self._mailbox.clear()
