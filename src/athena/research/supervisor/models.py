"""Supervisor 一步式 Plan/Operation 领域模型（supervisor_design §5）。

每份 SupervisorPlan 只覆盖一个协调步骤，但可以批量并行多个 worker。
Operation 使用小型通用白名单；不允许阶段黑盒或隐藏 spawn/wait 的领域大操作。
"""

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef, NonBlankText


def utc_now() -> str:
    """当前 UTC 时间的 ISO 字符串（整个 supervisor 子系统的唯一时钟）。"""
    return datetime.now(timezone.utc).isoformat()


class ControlStatus(StrEnum):
    """execution 控制状态，与研究阶段正交（supervisor_design §4.2）。"""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ResearchPhase(StrEnum):
    """研究阶段，完全由已提交事实投影（supervisor_design §4.1）。"""

    IDLE = "IDLE"
    PREPARE = "PREPARE"
    SEARCH = "SEARCH"
    VALIDATE = "VALIDATE"
    COMPLETED = "COMPLETED"


class PlanStatus(StrEnum):
    """Plan 耐久化状态机（supervisor_design §7）。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationStatus(StrEnum):
    """Operation 耐久化状态机（supervisor_design §7）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class OperationType(StrEnum):
    """小型通用 operation 白名单（supervisor_design §5）。"""

    SPAWN_BATCH = "SPAWN_BATCH"
    FOLLOWUP_AGENT = "FOLLOWUP_AGENT"
    WAIT_AGENTS = "WAIT_AGENTS"
    RUN_SERVICE = "RUN_SERVICE"
    COMMIT_FACTS = "COMMIT_FACTS"
    SET_EXECUTION_STATUS = "SET_EXECUTION_STATUS"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    RESERVE_FINAL_TEST = "RESERVE_FINAL_TEST"
    ADVANCE_FINAL_TEST = "ADVANCE_FINAL_TEST"


class SupervisorOperation(BaseModel):
    """一个最小协调操作；状态由 PlanJournal 持久化。"""

    operation_id: str
    operation_type: OperationType
    idempotency_key: str
    inputs: dict[str, object] = Field(default_factory=dict)
    input_refs: list[ArtifactRef] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    status: OperationStatus = OperationStatus.PENDING
    agent_id: str | None = None
    run_id: str | None = None
    result_refs: list[ArtifactRef] = Field(default_factory=list)
    outputs: dict[str, object] = Field(default_factory=dict)
    error: str | None = None


class LeaseToken(BaseModel):
    """execution 的写锁令牌：非 owner 不能写，旧 generation 的写入被 fencing。"""

    execution_id: str
    owner_id: str
    generation: int
    expires_at: str


class SupervisorPlan(BaseModel):
    """一步式 Plan；operations 有序，Executor 按顺序 fail-fast 执行。"""

    plan_id: str
    execution_id: str
    sequence: int
    snapshot_version: int
    reason_code: str
    preconditions: list[str] = Field(default_factory=list)
    operations: list[SupervisorOperation] = Field(default_factory=list)
    wait_policy: str = "ALL_COMPLETED"
    status: PlanStatus = PlanStatus.PENDING
    created_at: str
    completed_at: str | None = None


class HumanRequest(BaseModel):
    """项目级持久化人工请求（supervisor_design §8.1）。"""

    request_id: NonBlankText
    execution_id: str
    plan_id: str | None = None
    reason_code: str
    question: str
    context_refs: list[ArtifactRef] = Field(default_factory=list)
    input_mode: str = "single_select"
    options: list[str] = Field(default_factory=list)
    allow_free_text: bool = False
    response_schema: dict[str, object] | None = None
    default_answer: str | None = None
    expires_at: str | None = None
    status: str = "OPEN"
    answer: str | None = None
    answer_source: str | None = None
    requesting_agent_id: str | None = None
    created_at: str
    answered_at: str | None = None
