"""PlanValidator — 权限、门槛、预算与前置条件校验（supervisor_design §6）。

至少检查 snapshot_version、控制状态、operation 白名单、worker 权限矩阵、输入
refs 存在、阶段硬门槛、预算、idempotency 与 final-test 恰好一次。首版实现核心
子集：版本、状态、白名单、预算与 idempotency。
"""

from athena.research.services import RUN_SERVICE_NAMES
from athena.research.supervisor.journal import PlanJournal
from athena.research.supervisor.models import (
    ControlStatus,
    OperationType,
    SupervisorOperation,
    SupervisorPlan,
)
from athena.research.supervisor.state import Budget, ProjectStateStore

# Supervisor 允许 spawn 的静态 worker 类型集合（§2.2 所有权边界）。
_ALLOWED_WORKERS = frozenset({"init", "data", "reflection", "ideator", "code", "plot"})


class ValidationError(ValueError):
    """Plan 未通过校验；message 列出全部失败原因。"""


class PlanValidator:
    """校验 Plan 是否可在当前事实与预算下执行。"""

    def __init__(self, journal: PlanJournal, state: ProjectStateStore) -> None:
        self._journal = journal
        self._state = state

    def validate(
        self,
        plan: SupervisorPlan,
        budget: Budget,
        control_status: ControlStatus,
    ) -> None:
        """校验通过返回 None；否则抛 ``ValidationError``（含全部原因）。"""
        failures: list[str] = []
        if control_status is not ControlStatus.RUNNING:
            failures.append(f"control status is {control_status.value}, not RUNNING")
        if plan.snapshot_version != self._journal.snapshot_version():
            failures.append(
                "stale snapshot_version: "
                f"plan={plan.snapshot_version}, current={self._journal.snapshot_version()}"
            )
        if not budget.can_plan():
            failures.append("plan budget exhausted")
        seen_keys: set[str] = set()
        for operation in plan.operations:
            if operation.operation_type not in OperationType:
                failures.append(
                    f"operation type not in whitelist: {operation.operation_type}"
                )
            if operation.idempotency_key in seen_keys:
                failures.append(
                    f"duplicate idempotency_key in plan: {operation.idempotency_key}"
                )
            seen_keys.add(operation.idempotency_key)
            # 不检查已提交 idempotency：executor 的 complete_operation 幂等重放，
            # 恢复时已完成 op 由 execute 跳过，不需在 validator 拒绝。
            self._validate_worker_permission(operation, failures)
            self._validate_service_name(operation, failures)
        if failures:
            raise ValidationError("; ".join(failures))

    def _validate_service_name(
        self, operation: SupervisorOperation, failures: list[str]
    ) -> None:
        """RUN_SERVICE 的名称必须属于静态白名单（§5）。"""
        if operation.operation_type is not OperationType.RUN_SERVICE:
            return
        service = operation.inputs.get("service")
        if not isinstance(service, str) or service not in RUN_SERVICE_NAMES:
            failures.append(f"service not in RUN_SERVICE whitelist: {service}")

    def _validate_worker_permission(
        self, operation: SupervisorOperation, failures: list[str]
    ) -> None:
        """SPAWN_BATCH 的 worker 类型必须属于 Supervisor 允许的静态集合。"""
        if operation.operation_type is not OperationType.SPAWN_BATCH:
            return
        spawns = operation.inputs.get("spawns")
        if not isinstance(spawns, list):
            failures.append("SPAWN_BATCH inputs require a 'spawns' list")
            return
        for spec in spawns:
            if not isinstance(spec, dict):
                failures.append("spawn spec must be a dict")
                continue
            agent_type = spec.get("agent_type")
            if agent_type not in _ALLOWED_WORKERS:
                failures.append(f"worker type not allowed: {agent_type}")
