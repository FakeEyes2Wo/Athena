"""AgentRuntime 公开类型与契约（设计 §1.3、§2、§4.4、§4.5）。"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, runtime_checkable

from athena.core.contracts import ArtifactRef

if TYPE_CHECKING:
    from athena.core.agent.session import RunSession

RequestT = TypeVar("RequestT", contravariant=True)
ResponseT = TypeVar("ResponseT", covariant=True)

AgentPath = tuple[str, ...]
AgentId = str
RunId = str


class AgentStatus(str, Enum):
    """Agent 生命周期状态（设计 §7.1）。

    WAITING / WAITING_FOR_HUMAN 不占执行槽且可跨进程恢复；CLOSED 是唯一 Agent 终态。
    """

    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_FOR_HUMAN = "waiting_for_human"
    ERROR = "error"
    CLOSED = "closed"


class RunStatus(str, Enum):
    """Run 生命周期状态（§1.3）。INTERRUPTED 是 Run 终态而非 Agent 终态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
)


class ReturnWhen(Enum):
    """wait_agent 返回条件（§3.6）。"""

    FIRST_COMPLETED = "first_completed"
    ALL_COMPLETED = "all_completed"


class ErrorCode(str, Enum):
    """稳定错误 code（§4.5）。"""

    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    BUSY = "BUSY"
    CLOSED = "CLOSED"
    LIMIT_REACHED = "LIMIT_REACHED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INVALID_REQUEST = "INVALID_REQUEST"
    CODEC_ERROR = "CODEC_ERROR"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class AgentError(RuntimeError):
    """Agent 领域异常基类（§4.5）。"""


class AgentCommandError(AgentError):
    """命令错误，带稳定 code；失败命令必须零状态变更。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


class AgentBusyError(AgentCommandError):
    """目标已有非终态 Run，followup 被拒绝（§3.3）。"""

    def __init__(
        self, message: str = "agent busy", *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(ErrorCode.BUSY, message, details=details)


class AgentRunFailed(AgentError):
    """Run 执行失败（FAILED 终态）后 wait() 抛出的领域异常。"""


class AgentRunInterrupted(AgentError):
    """Run 被中断后 wait() 抛出的领域异常（§3.5）。"""


@runtime_checkable
class EventSink(Protocol):
    """只追加当前 Run 事件的异步回调（§2.2）。"""

    async def __call__(
        self, kind: str, event_ref: ArtifactRef, data: dict[str, Any] | None = None
    ) -> None: ...


@runtime_checkable
class AgentRunner(Protocol[RequestT, ResponseT]):
    """执行一个已类型化请求的协议（§2.2）。session 为绑定本 Run 的受限视图。"""

    async def run(
        self,
        request: RequestT,
        *,
        session: "RunSession",
        emit: EventSink,
    ) -> ResponseT: ...


@runtime_checkable
class AgentCodec(Protocol[RequestT, ResponseT]):
    """请求、响应与 artifact 之间的类型化编解码（§2.2）。"""

    def encode_request(self, value: RequestT) -> ArtifactRef: ...
    def decode_request(self, ref: ArtifactRef) -> RequestT: ...
    def encode_response(self, value: ResponseT) -> ArtifactRef: ...
    def decode_response(self, ref: ArtifactRef) -> ResponseT: ...


class JsonCodec:
    """请求/响应以 JSON 字符串作为 ArtifactRef。"""

    def encode_request(self, value: object) -> str:
        """把请求对象编码为 JSON 字符串（ArtifactRef 形式）。"""
        return json.dumps(value, ensure_ascii=False)

    def decode_request(self, ref: str) -> object:
        """把请求 JSON 字符串解码回对象。"""
        return json.loads(ref)

    def encode_response(self, value: object) -> str:
        """把响应对象编码为 JSON 字符串（ArtifactRef 形式）。"""
        return json.dumps(value, ensure_ascii=False)

    def decode_response(self, ref: str) -> object:
        """把响应 JSON 字符串解码回对象。"""
        return json.loads(ref)


@dataclass(frozen=True)
class AgentSpec(Generic[RequestT, ResponseT]):
    """Agent 的不可变能力说明（设计 §4.1）。agent_type 由注册表键表达，不再内嵌。"""

    runner: AgentRunner[RequestT, ResponseT]
    codec: AgentCodec[RequestT, ResponseT]


@dataclass(frozen=True)
class AgentMessage:
    """投递至目标 mailbox 的消息（设计 §4.3）。

    source 为 None 表示内部控制消息（AgentRuntime 生成的 completion）；content 说明意图，
    正式结果（报告、rubric、评审、图片等）经 context_refs 传递。
    """

    source: AgentId | None
    content: str
    context_refs: list[ArtifactRef] = field(default_factory=list)

    @property
    def is_control(self) -> bool:
        return self.source is None


@dataclass(frozen=True)
class AgentEvent:
    """Session journal 中的一条事件（§2.3）。sequence 为 Session 内单调。"""

    run_id: RunId
    sequence: int
    kind: str
    event_ref: ArtifactRef
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunSummary:
    """Run 的只读摘要（§2.3）。"""

    run_id: RunId
    agent_id: AgentId
    status: RunStatus
    response_ref: ArtifactRef | None = None
    error: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AgentSnapshot:
    """Agent 元数据快照（设计 §4.1 list_agents）。"""

    agent_id: AgentId
    path: AgentPath
    name: str
    agent_type: str
    status: AgentStatus
    parent_id: AgentId | None
    pending_run_id: RunId | None = None


@dataclass(frozen=True)
class AgentWaitResult:
    """wait_agent 的结果（§3.6）。completed 按冻结目标给出终态摘要。"""

    completed: dict[AgentId, RunSummary]
    timed_out: bool = False
