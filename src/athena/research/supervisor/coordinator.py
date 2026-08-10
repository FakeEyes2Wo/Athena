"""SupervisorCoordinator — 后台一步式协调循环（supervisor_design §7/§8）。

``step()`` 执行一个循环：加载权威事实 → 恢复未完成 Plan 或创建下一步 Plan →
校验 → 执行 → 重读事实。``run()`` 持续 step 直到无事可做或控制状态阻止推进。
首版为确定性实现；同一项目同一时间只允许一个写 Coordinator。
"""

import asyncio
from collections.abc import Awaitable, Callable

from athena.core.contracts import new_id
from athena.research.supervisor.executor import PlanExecutor
from athena.research.supervisor.journal import (
    LeaseConflictError,
    PlanJournal,
    StaleLeaseError,
)
from athena.research.supervisor.models import (
    ControlStatus,
    LeaseToken,
    PlanStatus,
    SupervisorPlan,
)
from athena.research.supervisor.planner import DeterministicSupervisorPlanner
from athena.research.supervisor.state import Budget, ProjectFacts, ProjectStateStore
from athena.research.supervisor.validator import PlanValidator, ValidationError


class SupervisorCoordinator:
    """持续执行一步式协调循环，直到项目完成、失败、暂停或取消。"""

    def __init__(
        self,
        *,
        journal: PlanJournal,
        state: ProjectStateStore,
        planner: DeterministicSupervisorPlanner,
        validator: PlanValidator,
        executor: PlanExecutor,
    ) -> None:
        self._journal = journal
        self._state = state
        self._planner = planner
        self._validator = validator
        self._executor = executor
        self._execution_id: str | None = None
        self._on_event: (
            Callable[[str, dict[str, object]], Awaitable[None] | None] | None
        ) = None
        self._owner_id = new_id("coord")
        self._lease: LeaseToken | None = None
        self._last_plan_failed = False

    def bind(
        self,
        execution_id: str,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        """绑定当前活动 execution 与事件回调（启动 RUN 时调用一次）。"""
        self._execution_id = execution_id
        self._on_event = on_event

    @property
    def execution_id(self) -> str | None:
        return self._execution_id

    async def run(self) -> ControlStatus:
        """持续执行一步式协调，直到项目完成或控制状态非 RUNNING（设计 §8）。

        启动时获取 execution 写锁；非 owner 只能只读，丢锁立即停止写入。
        """
        if self._execution_id is None:
            raise RuntimeError("coordinator not bound; call bind() first")
        try:
            self._lease = self._journal.claim_lease(self._execution_id, self._owner_id)
        except LeaseConflictError:
            return self._state.control_status()  # 其他 owner 持有锁 → 只读
        while True:
            try:
                control = await self.step()
            except StaleLeaseError:
                return self._state.control_status()  # 丢锁 → 停止写入
            if control is not ControlStatus.RUNNING:
                return control
            if self._last_plan_failed:
                return control  # 业务失败不无限重发同一 Plan（恢复/修复属后续任务）
            if (
                self._select_plan(self._state.facts(), self._state.budget(), control)
                is None
            ):
                return control  # 项目完成或暂无下一步 → 停止循环
            await asyncio.sleep(0)  # 让出事件循环，避免饿死其他协程

    async def step(self) -> ControlStatus:
        """执行一个协调循环并返回当前控制状态。

        循环：重读事实 → 恢复未完成 Plan 或创建下一步 → 校验 → 执行 → 重读。
        """
        if self._execution_id is None:
            raise RuntimeError("coordinator not bound; call bind() first")
        if self._lease is not None:
            self._journal.heartbeat(self._lease)
        self._last_plan_failed = False
        facts = self._state.facts()
        budget = self._state.budget()
        control = self._state.control_status()
        plan = self._select_plan(facts, budget, control)
        if plan is None:
            return control
        await self._validate_and_execute(plan, budget, control)
        return self._state.control_status()

    def _select_plan(
        self, facts: ProjectFacts, budget: Budget, control: ControlStatus
    ) -> SupervisorPlan | None:
        """返回待恢复或新建的下一步 Plan；控制状态非 RUNNING 或无事可做返回 None。"""
        if control is not ControlStatus.RUNNING:
            return None
        unfinished = self._journal.load_unfinished_plan(self._execution_id)
        if unfinished is not None:
            # 恢复：已持久化 Plan 的 snapshot_version 可能因中途已提交事实而过期
            # （如 SEARCH 的 register 已提交 graph_ref 后再崩溃）。刷新为当前版本，
            # 让校验通过并只重放未完成 op——不重复已提交事实/预算。
            unfinished.snapshot_version = self._journal.snapshot_version()
            return unfinished
        return self._planner.next_plan(self._execution_id, facts, budget, control)

    async def _validate_and_execute(
        self, plan: SupervisorPlan, budget: Budget, control: ControlStatus
    ) -> None:
        """校验后执行 Plan；校验失败或执行失败都会把 Plan 标为 FAILED。"""
        lease = self._lease
        if lease is None:
            raise RuntimeError("coordinator not bound to a lease; call run() first")
        try:
            self._validator.validate(plan, budget, control)
        except ValidationError as exc:
            plan.status = PlanStatus.FAILED
            self._journal.save_plan(plan, lease=lease)
            self._last_plan_failed = True
            await self._emit(
                "plan/failed", {"plan_id": plan.plan_id, "error": str(exc)}
            )
            self._fail_execution()
            return
        self._consume_plan_budget(plan, budget)
        await self._emit(
            "plan/started", {"plan_id": plan.plan_id, "reason": plan.reason_code}
        )
        await self._executor.execute(plan, lease=lease)
        self._last_plan_failed = plan.status is PlanStatus.FAILED
        if self._last_plan_failed:
            self._fail_execution()
        await self._emit(
            "plan/completed",
            {"plan_id": plan.plan_id, "status": plan.status.value},
        )

    def _fail_execution(self) -> None:
        """业务失败 → execution 进入终态 FAILED（STATUS 不再显示 RUNNING，入口不永久轮询）。

        只把仍 RUNNING 的 execution 置 FAILED；已处于 PAUSED/CANCELLED/FAILED 等
        终态时不覆盖。显式 SET_EXECUTION_STATUS FAILED 的计划（评审耗尽、无 SOTA）
        本身成功，不触发本路径。
        """
        if self._state.control_status() is ControlStatus.RUNNING:
            self._state.set_control_status(ControlStatus.FAILED)

    def _consume_plan_budget(self, plan: SupervisorPlan, budget: Budget) -> None:
        """每次 Plan 执行消费一次总 Plan 预算。

        搜索实验预算由 executor 的 complete_operation 幂等消费（单事务）。
        """
        budget.consume_plan()
        self._state.save_budget(budget)

    async def _emit(self, kind: str, data: dict[str, object]) -> None:
        """写入 durable outbox 并转发给绑定回调（支持同步/异步回调）。"""
        if self._execution_id is not None:
            try:
                self._journal.append_event(self._execution_id, kind, data)
            except Exception:
                pass  # durable outbox：发布失败不影响已提交事实
        if self._on_event is not None:
            result = self._on_event(kind, data)
            if asyncio.iscoroutine(result):
                await result
