"""SEARCH scheduling loop and plan-turn dispatch."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from athena.agents.supervisor_agent import MAX_PLAN_TURNS
from athena.core.research_tree import ExperimentStatus
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.experiment import decide_settlement
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
        self, plan_id: str, best_ref: str | None, result: Any
    ) -> None:
        """Settle one completed Plan."""
        ...

    async def register_hypotheses(self, hypotheses: list) -> dict[str, object]:
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
        self._task = asyncio.current_task()
        while not self._run.is_stopped():
            if self._state.status != "RUNNING":
                # 暂停/等待人工决策：不再派发新 turn，直到 /resume 或选择操作唤醒。
                self._run.clear_wake()
                await self._run.wait_wake()
                if self._run.is_stopped():
                    return
                continue
            generated = await self._fill_slots()
            if not self._run.running_ids():
                if generated:
                    continue
                if await self._wait_for_manual_selection():
                    continue
                return
            done, _pending = await asyncio.wait(
                self._run.running_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if self._run.is_stopped():
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
                item_id
                for item_id, item_task in self._run.running_items
                if item_task is task
            )
            self._run.pop_running(plan_id)
            try:
                completed = task.result()
            except (
                Exception
            ) as exc:  # noqa: BLE001 - task boundary catches all failures
                # A Plan task may fail after leaving the scheduler's task set.
                # Park SEARCH with durable feedback so recovery can retry it.
                plan = self._state.plans.get(plan_id)
                if plan is not None:
                    detail = f"{type(exc).__name__}: {exc}"
                    failure = PlanFailure(
                        kind="turn_crashed", detail=detail
                    ).to_summary()[:1200]
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
                        "text": f"Plan {plan_id} failed: {exc}",
                    },
                )
                return
            await self._apply_completed_turn(completed)

    async def cancel(self) -> None:
        """Cancel the live SEARCH loop, if another task owns it."""
        task = self._task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._task is task:
            self._task = None

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
                if self._deps.research.ideator is None:
                    before = len(self._tree.pending_hypotheses())
                    await self._deps.research.supervisor(
                        f"Propose exactly {action.count} new SEARCH hypotheses."
                    )
                    if self._run.is_stopped():
                        return generated
                    generated = len(self._tree.pending_hypotheses()) > before
                else:
                    hypotheses = await self._deps.research.ideator(action.count)
                    if self._run.is_stopped():
                        return generated
                    registered = await self._plans.register_hypotheses(hypotheses)
                    # 以"实际入图数量"判断本轮是否产出了新候选，避免 ideator 返回
                    # 但全部被过滤时把 SEARCH 循环拖成空转（无法推进到 VALIDATE）。
                    generated = len(registered["hypothesis_ids"]) > 0
                continue
            plan_id = action.plan_id or action.hypothesis_id
            if plan_id is None:
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
            self._launch_turn(plan_id)
        return generated

    def _launch_turn(self, plan_id: str) -> None:
        if plan_id not in self._run.running_ids():
            self._run.add_running(plan_id, asyncio.create_task(self._run_turn(plan_id)))

    async def _apply_completed_turn(self, completed) -> None:
        plan_id = completed.plan_id
        state = self._state.plans[plan_id]
        if completed.result is not None and completed.result.next_state is not None:
            state = completed.result.next_state
            self._state.plans[plan_id] = state
        if completed.result is not None and completed.result.kind == "diff_rejected":
            # A diff that does not implement the intervention is not a trusted
            # experiment; do not settle it. Let the PlanAgent repair and retry.
            await self._plans.persist_state()
            return
        if completed.decision is None:
            if state.turn_limit is not None and state.turns_used >= state.turn_limit:
                self._state.status = "WAITING"
            await self._plans.persist_state()
            return
        if completed.decision.decision == "abandon" and state.best_ref is None:
            await self._plans.settle_plan(plan_id, None, completed.result)
            await self._plans.publish_state()
            return
        settlement = decide_settlement(
            state,
            completed.decision,
            report_ref=completed.result.report_ref if completed.result else None,
        )
        if settlement.action == "continue":
            self._plans.save_state()
        elif settlement.action == "wait":
            if self._deps.phases.auto_validate or self._deps.phases.skip_validate:
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

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        """Queue one validated Hypothesis for the next manual SEARCH slot."""
        self._tree.get_hypothesis(hypothesis_id)
        existing = self._tree.experiment_for_hypothesis(hypothesis_id)
        if existing is not None and self._tree.get_experiment(existing).status in {
            ExperimentStatus.SUCCEEDED,
            ExperimentStatus.FAILED,
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
