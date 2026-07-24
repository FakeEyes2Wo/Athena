"""执行内核 Submission / Op 模型。

对齐 Codex ``protocol::Submission`` 和 ``protocol::Op``。
Submission 是 ThreadRuntime 内部控制消息，不是公开协议 DTO。
"""

import asyncio
from dataclasses import dataclass, field

from athena.core.schemas import ArtifactRef

# Op — 操作判别联合


@dataclass(slots=True)
class StartTurn:
    """启动一次 Turn。turn_id 即 submission_id。"""

    turn_id: str
    request_ref: ArtifactRef


@dataclass(slots=True)
class InterruptTurn:
    """中断正在运行的 Turn。"""

    turn_id: str
    reason: str


@dataclass(slots=True)
class GetForkSnapshot:
    """获取指定 Turn 的 fork 快照上下文。"""

    after_turn_id: str | None = None


@dataclass(slots=True)
class ShutdownThread:
    """关闭 ThreadRuntime。"""

    reason: str = "client_shutdown"


# 未来: ResolveApproval, ResolveUserInput
Op = StartTurn | InterruptTurn | GetForkSnapshot | ShutdownThread

# Submission — 内控消息


@dataclass(slots=True)
class Submission:
    """ThreadRuntime 内部控制消息。

    对齐 Codex ``protocol::Submission { id, op, ... }``。
    ``reply`` 只表示操作已被接纳或控制操作已完成，绝不等待整个 Turn 结束。
    """

    id: str
    op: Op
    reply: asyncio.Future[object] = field(default_factory=asyncio.Future)
    trace_context: dict[str, str] | None = None


# RuntimeSignal — runner 终态


@dataclass(slots=True)
class RunnerSucceeded:
    """runner 正常完成。"""

    turn_id: str
    result_ref: ArtifactRef
    next_context_ref: ArtifactRef


@dataclass(slots=True)
class RunnerFailed:
    """runner 异常退出。"""

    turn_id: str
    exception_type: str


@dataclass(slots=True)
class RunnerCancelled:
    """runner 被取消。"""

    turn_id: str


RuntimeSignal = RunnerSucceeded | RunnerFailed | RunnerCancelled

# TurnRuntime — 活动 Turn 所有权对象


@dataclass(slots=True)
class TurnRuntime:
    """活动 Turn 的所有权对象。ThreadRuntime 最多持有一个。

    对齐 Codex ``state/turn.rs`` ActiveTurn / RunningTask 模式。
    """

    turn_id: str
    request_ref: ArtifactRef
    runner_task: asyncio.Task[None]
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    done: "asyncio.Future[TurnTerminalState]" = field(default_factory=asyncio.Future)

    @property
    def is_active(self) -> bool:
        return not self.done.done()


@dataclass(slots=True)
class TurnTerminalState:
    """runner 退出后的终态信息。"""

    result_ref: ArtifactRef | None = None
    next_context_ref: ArtifactRef | None = None
    exception_type: str | None = None
    cancelled: bool = False
