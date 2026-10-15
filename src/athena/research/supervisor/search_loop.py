"""SEARCH scheduling loop and plan-turn dispatch."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.core.agent.types import AgentCommandError, ErrorCode
from athena.core.contracts import ArtifactRef
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ExperimentStatus
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.events import bounded_redacted_tail
from athena.research.supervisor.experiment import PlanTurnResult, decide_settlement
from athena.research.supervisor.plan_runtime import CompletedPlanTurn
from athena.research.supervisor.plans import PlanFailure
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.scheduling import ScheduleKind

logger = logging.getLogger(__name__)


class PlanCallbacks(Protocol):
    """Public Plan operations required by the SEARCH loop."""

    def save_state(self) -> None:
        """Persist durable state without publishing an event."""
        ...

    async def persist_state(self) -> None:
        """Persist and publish durable state."""
        ...

    async def publish_state(self) -> None:
        """Publish the current durable state."""
        ...

    async def start_plan(self, hypothesis_id: str) -> str:
        """Create one Plan for a queued hypothesis."""
        ...

    async def settle_plan(
        self, plan_id: str, best_ref: ArtifactRef | None, result: PlanTurnResult | None
    ) -> None:
        """Settle one completed Plan."""
        ...

    async def register_hypotheses(
        self, hypotheses: list[Hypothesis]
    ) -> dict[str, object]:
        """Register hypotheses produced by an Ideator."""
        ...


class SearchLoop:
    """Run the rolling SEARCH scheduler and own Plan-turn dispatch decisions."""

    def __init__(
        self,
        owner: object,
        deps: SupervisorDeps,
        run: SupervisorRunState,
        plans: PlanCallbacks,
        *,
        run_turn: Callable[[str], Awaitable[CompletedPlanTurn]],
    ) -> None:
        self._owner = owner
        self._deps = deps
        self._run = run
        self._plans = plans
        self._run_turn = run_turn
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        self._waiting_for_control = False

    @property
    def _state(self):
        return self._owner.state

    @property
    def _tree(self):
        return self._owner.tree

    def _spawn_search(self) -> None:
        """(重新)进入 SEARCH 调度循环（当前空闲且 RUNNING 时）。

        auto 模式下 ``run_search`` 达到 search_limit 后返回、状态置 WAITING，随后
        ``/resume``、``configure_search``、``update_waiting_plan_budget`` 会把状态改回
        RUNNING，但若不在此重新拉起后台调度任务，就永远不再推进（静默 liveness 失败）。
        """
        if (
            self._state.phase == "SEARCH"
            and self._state.status == "RUNNING"
            and (self._task is None or self._task.done())
        ):
            self._cancel_requested = False
            task = asyncio.create_task(self.run_search())
            task.add_done_callback(self._on_search_done)
            self._task = task

    def _on_search_done(self, task: asyncio.Task) -> None:
        """检索后台 SEARCH 任务的异常，避免 'Task exception was never retrieved'。"""
        if self._task is task:
            self._task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("SEARCH scheduling loop crashed: %r", exc)
            # A crashed scheduler must not leave the UI permanently RUNNING.
            self._state.status = "FAILED"
            try:
                asyncio.create_task(self._plans.persist_state())
            except Exception:
                logger.warning(
                    "failed to persist FAILED state after SEARCH crash",
                    exc_info=True,
                )

    async def run_search(self) -> None:
        """Run rolling SEARCH scheduling."""
        current = asyncio.current_task()
        if self._task is None or self._task.done():
            self._cancel_requested = False
        self._task = current
        while not self._run.is_stopped() and not self._cancel_requested:
            if self._state.status != "RUNNING" and not self._run.running_ids():
                # Stop dispatch while paused, but drain already-running turns
                # first so their trusted results survive a restart.
                self._waiting_for_control = True
                try:
                    self._run.clear_wake()
                    if self._cancel_requested:
                        return
                    await self._run.wait_wake()
                finally:
                    self._waiting_for_control = False
                if self._run.is_stopped() or self._cancel_requested:
                    return
                continue
            try:
                generated = (
                    await self._fill_slots()
                    if self._state.status == "RUNNING"
                    else False
                )
            except AgentCommandError as exc:
                # Trigger: pause closes the Agent command during slot filling; only that pause race is parked below.
                if not self._is_pause_interruption(exc):
                    raise
                # pause() changes the durable status and closes the Agent
                # dispatch gate before an in-flight ideator can follow up.
                # That expected race should park scheduling, not fail SEARCH.
                await self._plans.persist_state()
                return
            if not self._run.running_ids():
                if generated:
                    continue
                if await self._wait_for_manual_selection():
                    continue
                if any(
                    plan.kind == "SEARCH"
                    and plan.turn_limit is not None
                    and plan.turns_used >= plan.turn_limit
                    for plan in self._state.plans.values()
                ):
                    self._state.status = "WAITING"
                    await self._plans.persist_state()
                return
            done, _pending = await asyncio.wait(
                self._run.running_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if self._run.is_stopped() or self._cancel_requested:
                return
            task = next(iter(done))
            # ``running_items``、不是 ``running_tasks``：后者给的是裸 Task，把它
            # 解包成 ``(id, task)`` 不会得到一条有用的报错——**已完成**的 asyncio
            # Task 迭代出 0 个元素，于是这里抛的是
            # ``not enough values to unpack (expected 2, got 0)``。
            #
            # 真机（2026-08-29）：SEARCH 每次有候选跑完就崩在这里，两次实验分别拿到
            # 0.8523 和 0.8621 却一次都没能结算。日志里只有那句解包错误。
            plan_id = next(
                (
                    item_id
                    for item_id, item_task in self._run.running_items
                    if item_task is task
                ),
                None,
            )
            if plan_id is None:
                # A Human cancellation removed this task while wait was pending.
                continue
            self._run.pop_running(plan_id)
            try:
                completed = task.result()
            except (
                Exception  # noqa: BLE001 - task boundary catches all failures
            ) as exc:
                # A Plan task may fail after leaving the scheduler's task set.
                # Park SEARCH with durable feedback so recovery can retry it.
                plan = self._state.plans.get(plan_id)
                diagnostic = bounded_redacted_tail(f"{type(exc).__name__}: {exc}", 1180)
                if plan is not None:
                    failure = PlanFailure(
                        kind="turn_crashed", detail=diagnostic
                    ).to_summary()
                    self._state.plans[plan_id] = plan.model_copy(
                        update={"last_failure": failure}
                    )
                self._state.status = "WAITING"
                await self._plans.persist_state()
                await self._deps.phases.publish(
                    "output",
                    {
                        "source": "supervisor",
                        "channel": "error",
                        "text": f"Plan {plan_id} failed: turn_crashed: {diagnostic}",
                    },
                )
                if self._run.running_ids():
                    continue
                return
            await self._apply_completed_turn(completed)

    def _is_pause_interruption(self, error: AgentCommandError) -> bool:
        """Recognize only AgentRuntime's explicit pause gate rejection."""
        return (
            self._state.status != "RUNNING"
            and error.code is ErrorCode.CLOSED
            and str(error) == "runtime paused"
        )

    async def cancel(self) -> None:
        """Cancel the live SEARCH loop, if another task owns it."""
        task = self._task
        if task is None or task.done() or task is asyncio.current_task():
            return
        self._cancel_requested = True
        self._run.wake()
        # A paused loop has no Plan work to interrupt. Wake it and let it leave
        # its control wait cooperatively; cancelling a waiter can strand the
        # scheduler during shutdown on Windows event loops.
        if not self._waiting_for_control:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._task is task:
            self._task = None

    async def cancel_plan(self, plan_id: str, reason: str) -> dict[str, object]:
        """Persist a terminal cancellation before interrupting its live worker."""
        if not reason.strip():
            raise ValueError("cancellation reason must be nonblank")
        experiment_id = self._tree.experiment_for_hypothesis(plan_id)
        if experiment_id is None:
            raise ValueError(f"unknown SEARCH Plan: {plan_id}")
        experiment = self._tree.get_experiment(experiment_id)
        if experiment.status not in {
            ExperimentStatus.RUNNING,
            ExperimentStatus.CANCELLED,
        }:
            raise ValueError(f"Plan is already settled: {plan_id}")
        if experiment.status is ExperimentStatus.RUNNING:
            plan = self._state.plans.get(plan_id)
            if plan is None or plan.kind != "SEARCH":
                raise ValueError(f"unknown SEARCH Plan: {plan_id}")
            reason_ref = await self._deps.runtime.store.put_text(reason.strip())
            # Recheck after artifact I/O: normal settlement may have won the race.
            if (
                self._tree.get_experiment(experiment_id).status
                is not ExperimentStatus.RUNNING
            ):
                raise ValueError(f"Plan is already settled: {plan_id}")
            self._tree.transition_experiment(experiment_id, ExperimentStatus.CANCELLED)
            self._tree.attach_artifact(experiment_id, "cancellation_reason", reason_ref)
            self._tree.update_hypothesis_status(plan_id, "INCONCLUSIVE")
        # Tree-first persistence matches normal settlement. Recovery drops a
        # terminal experiment even if the following state save was interrupted.
        self._tree.save(self._deps.paths.tree_path)
        self._state.plans.pop(plan_id, None)
        self._plans.save_state()
        if self._run.next_hypothesis_id == plan_id:
            self._run.set_next_hypothesis_id(None)
        task = self._run.pop_running(plan_id)
        if task is not None:
            task.cancel()
        try:
            try:
                await asyncio.wait_for(
                    self._deps.runtime.agents.interrupt(plan_id, "plan_cancelled"),
                    timeout=10,
                )
            except AgentCommandError as exc:
                # Trigger: cancellation interrupts an already-closed Agent command; ignore only NOT_FOUND.
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
        finally:
            if task is not None:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True), timeout=10
                )
        await self._plans.publish_state()
        return {"plan_id": plan_id, "status": "CANCELLED"}

    async def _wait_for_manual_selection(self) -> bool:
        """Block the SEARCH loop until a Human selects a hypothesis in manual mode.

        Returns True when scheduling should re-run (selection made or mode
        switched back to auto), False when the loop should exit (not manual or
        nothing pending to select from).
        """
        if not self._state.manual_mode:
            return False
        if self._run.next_hypothesis_id is not None:
            return True
        if not self._tree.pending_hypotheses():
            return False
        self._state.status = "WAITING"
        await self._plans.persist_state()
        self._run.clear_wake()
        await self._run.wait_wake()
        return not self._run.is_stopped()

    async def _corpus_ideation(self) -> bool:
        """语料落地（或扩充）之后补一轮 ideation，让调研的产出真的被读到。

        非阻塞设计（SEARCH 绝不为调研停等）本身没问题，但它单独并不成立：调度器只在
        "没有假设可排"时才 GENERATE，而第一轮 ideation 几乎必然早于调研完成——真机实测
        语料就绪比第一轮 ideation 晚约两分钟，那一轮把队列填满之后调度器再没需要生成，
        于是十几分钟的调研成果一次都没被读到，6 条假设 0 条引用语料。

        触发条件是**语料版本变了**，不是"补过没有"。语料现在可以增量扩充，用布尔标记
        会让扩充进来的新论文永远读不到——第一轮补过就再也不补了。

        补的这一轮不动实验预算：它只往队列里加候选，跑几个仍由 ``search_limit`` 决定。
        """
        corpus_ref = self._state.corpus_ref
        if corpus_ref is None or corpus_ref == self._state.corpus_ideated_ref:
            return False
        if self._deps.research.ideator is None:
            # 没有 Ideator 就没有"读语料的那一步"，记下版本避免每轮重试。
            self._state.corpus_ideated_ref = corpus_ref
            await self._plans.persist_state()
            return False
        # 先落状态再跑：重入时不会为同一份语料补第二轮。
        self._state.corpus_ideated_ref = corpus_ref
        await self._plans.persist_state()
        hypotheses = await self._deps.research.ideator(
            self._state.hypotheses_per_ideator
        )
        if self._run.is_stopped():
            return False
        registered = await self._plans.register_hypotheses(hypotheses)
        return len(registered["hypothesis_ids"]) > 0

    async def _fill_slots(self) -> bool:
        """Apply Scheduler actions until all available slots are accounted for."""
        if self._run.is_stopped():
            return False
        generated = await self._corpus_ideation()
        if self._run.is_stopped():
            return generated
        actions = self._deps.search.scheduler.next_actions(
            self._state,
            self._tree,
            self._run.running_ids(),
            human_next=self._run.next_hypothesis_id,
            manual=self._state.manual_mode,
        )
        for action in actions:
            if self._run.is_stopped():
                return generated
            if action.kind is ScheduleKind.GENERATE:
                new_candidates = await self._generate_hypotheses(action.count)
                if new_candidates is None:
                    return generated
                generated = new_candidates
                continue
            plan_id = action.plan_id or action.hypothesis_id
            if plan_id is None:
                continue
            experiment_id = self._tree.experiment_for_hypothesis(plan_id)
            if (
                experiment_id is not None
                and self._tree.get_experiment(experiment_id).status
                is ExperimentStatus.CANCELLED
            ):
                continue
            if action.kind in {
                ScheduleKind.START_NEW,
                ScheduleKind.START_NEXT_HYPOTHESIS,
            }:
                await self._plans.start_plan(plan_id)
                if self._run.is_stopped():
                    return generated
            if action.kind is ScheduleKind.START_NEXT_HYPOTHESIS:
                self._run.set_next_hypothesis_id(None)
            if plan_id not in self._state.plans:
                continue
            self._launch_turn(plan_id)
        return generated

    async def _generate_hypotheses(self, count: int) -> bool | None:
        """Generate and register SEARCH candidates through the configured provider."""
        ideator = self._deps.research.ideator
        if ideator is None:
            before = len(self._tree.pending_hypotheses())
            await self._deps.research.supervisor(
                f"Propose exactly {count} new SEARCH hypotheses."
            )
            if self._run.is_stopped():
                return None
            return len(self._tree.pending_hypotheses()) > before

        hypotheses = await ideator(count)
        if self._run.is_stopped():
            return None
        registered = await self._plans.register_hypotheses(hypotheses)
        # Count newly registered graph nodes so empty proposals cannot spin SEARCH.
        return len(registered["hypothesis_ids"]) > 0

    def _launch_turn(self, plan_id: str) -> None:
        if plan_id not in self._run.running_ids():
            self._run.add_running(plan_id, asyncio.create_task(self._run_turn(plan_id)))

    async def _apply_completed_turn(self, completed: CompletedPlanTurn) -> None:
        plan_id = completed.plan_id
        if plan_id not in self._state.plans:
            return
        state = self._state.plans[plan_id]
        if completed.result is not None and completed.result.next_state is not None:
            state = completed.result.next_state
            self._state.plans[plan_id] = state
        if completed.decision is not None and completed.decision.decision == "abandon":
            await self._plans.settle_plan(plan_id, state.best_ref, completed.result)
            await self._plans.publish_state()
            return
        if (
            completed.failure is not None
            and completed.failure.kind == "turn_infrastructure_failed"
        ):
            # Agent service / artifact-store failures cannot be repaired by
            # changing candidate code. Preserve the Plan and its diagnostic for
            # an explicit resume after the infrastructure is available again.
            await self._pause_for_infrastructure(
                plan_id,
                completed.failure.detail,
                recovery="Restore the Agent/storage service before resuming SEARCH.",
            )
            return
        if completed.result is not None and completed.result.kind in {
            "execution_infrastructure_failed",
            "evaluator_infrastructure_failed",
        }:
            # Candidate code cannot repair a broken backend or frozen evaluator.
            # Preserve the Plan for an explicit resume after the cause is fixed.
            await self._pause_for_infrastructure(
                plan_id,
                completed.result.error,
                recovery="Restore the execution/evaluator service before resuming SEARCH.",
            )
            return
        if (
            completed.decision is not None
            and completed.decision.decision == "submit"
            and completed.result is None
            and completed.failure is None
            and state.kind == "SEARCH"
            and state.best_ref is not None
        ):
            # PlanRuntime short-circuits submit when a trusted historical best
            # exists; never route that valid, result-less action through failure
            # retry handling for missing Agent results or infrastructure faults.
            await self._plans.settle_plan(plan_id, state.best_ref, None)
            await self._plans.publish_state()
            return
        if completed.decision is None or (
            completed.result is not None and completed.result.kind != "scored"
        ):
            # A submit without a trusted best still proposes an experiment; a
            # failed run must reach another repair turn, including rejected diffs.
            exhausted = (
                state.turn_limit is not None and state.turns_used >= state.turn_limit
            )
            if exhausted:
                if (
                    state.best_ref is not None
                    or self._deps.phases.validation_mode != "manual"
                ):
                    await self._plans.settle_plan(
                        plan_id, state.best_ref, completed.result
                    )
                else:
                    self._state.status = "WAITING"
            await self._plans.persist_state()
            return
        settlement = decide_settlement(
            state,
            completed.decision,
            report_ref=completed.result.report_ref if completed.result else None,
        )
        if settlement.action == "continue":
            self._plans.save_state()
        elif settlement.action == "wait":
            if self._deps.phases.validation_mode != "manual":
                # auto 模式无人补充缺失的 report / 延长预算 → 用历史 best 结算，
                # 释放并发槽让调度器继续 GENERATE，搜索得以收敛（否则死锁）。
                await self._plans.settle_plan(plan_id, state.best_ref, completed.result)
            else:
                self._state.status = "WAITING"
                self._plans.save_state()
        else:
            await self._plans.settle_plan(
                plan_id, settlement.best_ref, completed.result
            )
        await self._plans.publish_state()

    async def _pause_for_infrastructure(
        self, plan_id: str, detail: str | None, *, recovery: str
    ) -> None:
        """Persist SEARCH as WAITING and publish its infrastructure cause."""
        self._state.status = "WAITING"
        await self._plans.persist_state()
        await self._deps.phases.publish(
            "output",
            {
                "source": "supervisor",
                "channel": "error",
                "text": f"Plan {plan_id} paused: {detail}. {recovery}",
            },
        )

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        """Queue one validated Hypothesis for the next manual SEARCH slot."""
        self._tree.get_hypothesis(hypothesis_id)
        existing = self._tree.experiment_for_hypothesis(hypothesis_id)
        if existing is not None and self._tree.get_experiment(existing).status in {
            ExperimentStatus.SUCCEEDED,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }:
            raise ValueError(f"hypothesis already settled: {hypothesis_id}")
        self._run.set_next_hypothesis_id(hypothesis_id)
        if self._state.status == "WAITING":
            self._state.status = "RUNNING"
            self._plans.save_state()
        self._run.wake()
        return {"selected": hypothesis_id}

    async def configure_search(self, **payload: object) -> dict[str, object]:
        """Apply bounded SEARCH budget and concurrency settings."""
        search_limit = payload.get("search_limit")
        concurrency = payload.get("concurrency")
        if search_limit is not None:
            value = int(search_limit)
            if value < 1:
                raise ValueError("search_limit must be at least 1")
            self._state.search_limit = value
        if concurrency is not None:
            value = int(concurrency)
            if value < 1:
                raise ValueError("concurrency must be at least 1")
            self._state.concurrency = value
        # 交互模式下预算用尽停在 WAITING：追加预算即恢复 SEARCH。
        if self._state.phase == "SEARCH" and self._state.status == "WAITING":
            self._state.status = "RUNNING"
        await self._plans.persist_state()
        self._spawn_search()
        return {
            "search_limit": self._state.search_limit,
            "concurrency": self._state.concurrency,
        }

    async def update_waiting_plan_budget(self, **payload: object) -> dict[str, object]:
        """Extend one exhausted Plan so SEARCH may dispatch it again."""
        plan_id = str(payload.get("plan_id", ""))
        try:
            plan = self._state.plans[plan_id]
        except KeyError as exc:
            # Human control named a Plan absent from durable state.
            raise KeyError(f"unknown Plan: {plan_id}") from exc
        exhausted = plan.turn_limit is not None and plan.turns_used >= plan.turn_limit
        if not exhausted:
            raise ValueError(f"Plan is not waiting: {plan_id}")
        turn_limit = payload.get("turn_limit")
        unlimited = bool(payload.get("unlimited_turns", False))
        patience = payload.get("patience")
        if turn_limit is not None and unlimited:
            raise ValueError("turn_limit and unlimited_turns are mutually exclusive")
        updates: dict[str, object] = {}
        if turn_limit is not None:
            value = int(turn_limit)
            if value > MAX_PLAN_TURNS:
                raise ValueError(
                    f"turn_limit exceeds configured maximum {MAX_PLAN_TURNS}"
                )
            if value <= plan.turns_used:
                raise ValueError("turn_limit must exceed turns already used")
            updates["turn_limit"] = value
        elif unlimited:
            updates["turn_limit"] = None
        if patience is not None:
            value = int(patience)
            if value < 1 or value > 5:
                raise ValueError("patience must be between 1 and 5")
            updates["patience"] = value
        if not updates:
            raise ValueError("at least one Plan budget must be provided")
        self._state.plans[plan_id] = plan.model_copy(update=updates)
        self._state.status = "RUNNING"
        await self._plans.persist_state()
        self._spawn_search()
        return {"plan_id": plan_id, **updates}

    async def set_manual_mode(self, manual: bool) -> dict[str, object]:
        """Toggle SEARCH scheduling between auto (priority queue) and manual.

        Manual mode pauses after each hypothesis batch and waits for
        ``select_next_hypothesis``. Switching back to auto wakes the loop.
        """
        self._state.manual_mode = bool(manual)
        if self._state.status == "WAITING":
            self._state.status = "RUNNING"
        await self._plans.persist_state()
        self._run.wake()
        return {"manual_mode": self._state.manual_mode}


__all__ = ["SearchLoop"]
