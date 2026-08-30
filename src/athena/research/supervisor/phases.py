"""Research phase machine and human-facing control operations."""

import asyncio
import logging
import traceback

from athena.core.agent.types import AgentCommandError, ErrorCode
from athena.core.contracts import ArtifactRef
from athena.research.report import build_final_report
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.plan_lifecycle import PlanLifecycle
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.scheduler import count_search_attempts
from athena.research.supervisor.search_loop import SearchLoop

logger = logging.getLogger(__name__)


def _final_report_text(validation: dict) -> str:
    """Format the frozen VALIDATE conclusion into a readable final report."""
    parts = ["VALIDATE completed"]

    def metric(key: str) -> str | None:
        value = validation.get(key)
        if value is None:
            return None
        return f"{value:.4f}" if isinstance(value, float) else str(value)

    final_score = metric("final_test_score")
    if final_score is not None:
        parts.append(f"final test score {final_score}")
    gap = metric("generalization_gap")
    if gap is not None:
        parts.append(f"generalization gap {gap}")
    if validation.get("generalization_warning"):
        parts.append("generalization warning")
    return " · ".join(parts)


class PhaseMachine:
    """Run PREPARE/SEARCH/VALIDATE transitions and interactive control."""

    def __init__(
        self,
        owner: object,
        deps: SupervisorDeps,
        run: SupervisorRunState,
        plans: PlanLifecycle,
        search: SearchLoop,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self._run = run
        self._plans = plans
        self._search = search

    @property
    def _state(self):
        return self._owner.state

    @property
    def _tree(self):
        return self._owner.tree

    async def start(self) -> None:
        """Start the Supervisor lifecycle."""
        self._run.set_stopped(False)
        try:
            if self._state.phase == "PREPARE":
                await self._run_prepare()
            else:
                await self._owner.recover()
            await self.continue_phase()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 阶段执行失败（如 evaluator 基础设施不可用）在此统一观测：置 FAILED
            # 并发布，避免被 fire-and-forget 的 supervisor task 吞掉、阶段卡在 PREPARE。
            self._state.status = "FAILED"
            self._plans._save_state()
            tb = traceback.format_exc()
            logger.exception("research phase failed")
            await self._deps.publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "error",
                    "text": f"research failed: {exc}\n\n{tb}",
                },
            )
            await self._plans._publish_state()

    async def continue_phase(self) -> None:
        """Execute the current phase for an idle run (interactive resume).

        ``start()`` runs PREPARE→SEARCH→VALIDATE once. In interactive mode
        (no ``auto_validate``) SEARCH parks at ``WAITING`` and the machine
        returns; a later Human turn may transition the phase through
        ``set_phase_decision`` or extend the Search budget. Re-enter the phase
        machine here to actually run the chosen phase.
        """
        if self._state.phase == "SEARCH":
            await self._search.run_search()
            if self._run.is_stopped():
                return
            if self._deps.auto_validate:
                await self._transition_phase("VALIDATE")
            elif self._search_limit_reached() and self._state.status == "RUNNING":
                self._state.status = "WAITING"
                await self._plans._persist_state()
                await self._budget_gate()
        if self._state.phase == "VALIDATE":
            await self._run_validation()

    async def _budget_gate(self) -> None:
        """交互模式下预算用尽：Supervisor 汇总假设并向人类提出具体决策。

        走异步 message 循环——Supervisor 用 answer 问「+N 次 / validate / stop」，
        人类下一条 message 回复后，Supervisor 再调 configure_search / set_phase_decision。
        """
        prompt = (
            "SEARCH budget is exhausted. Read read_hypotheses, then in your answer "
            "ask the human one concrete question: how many more search attempts to "
            "add (a number), or 'validate' to proceed to VALIDATE, or 'stop'."
        )
        try:
            await self._deps.run_supervisor_turn(prompt)
        except Exception:
            # 门失败不阻断：状态已 WAITING，人类仍可手动 configure_search / set_phase_decision
            pass

    async def _run_prepare(self) -> None:
        if self._deps.run_prepare_phase is None:
            raise RuntimeError("PREPARE phase adapter is not configured")
        if self._tree.best_experiment_id() is not None:
            # 断点续传：tree 已含可信 SOTA baseline（崩溃窗口为 tree 已写、phase 未转），
            # 跳过重跑 PREPARE，直接进入 SEARCH。
            await self._deps.publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "text",
                    "text": "PREPARE: 已有可信 baseline（断点续传），跳过 PREPARE。",
                },
            )
            if self._run.is_stopped() or self._state.status == "WAITING":
                return
            await self._transition_phase("SEARCH")
            return
        result = await self._deps.run_prepare_phase()
        if self._run.is_stopped() or self._state.status == "WAITING":
            return
        hypothesis_id = "baseline"
        experiment_id = "exp_baseline"
        from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
        from athena.core.research_tree import Experiment, ExperimentStatus
        from athena.core.workspace import GitWorkBranch

        if self._tree.best_experiment_id() is None:
            self._tree.add_hypothesis(
                Hypothesis(
                    id=hypothesis_id,
                    statement="trusted PREPARE baseline",
                    intervention="establish the baseline implementation",
                    expected_effect="provide the SEARCH reference metric",
                )
            )
            self._tree.add_experiment(
                experiment_id,
                Experiment(
                    hypothesis_id=hypothesis_id,
                    commit=result.commit,
                    plan=ExperimentPlan(
                        kind="baseline",
                        change="prepare trusted baseline",
                        run_config_ref=result.evaluator_ref,
                        budget={},
                        acceptance_rule="trusted evaluator score",
                    ),
                    gitwork=GitWorkBranch(
                        path=str(self._deps.project_root),
                        branch="main",
                        base_commit=result.commit,
                    ),
                    status=ExperimentStatus.SUCCEEDED,
                    eval=EvalResult(
                        experiment_id=experiment_id,
                        primary=result.metric,
                        per_sample=result.evidence_ref,
                    ),
                    artifacts={
                        "predictions": result.predictions_ref,
                        "evidence": result.evidence_ref,
                        "report": result.report_ref,
                    },
                ),
            )
            self._tree.set_sota(experiment_id)
            self._tree.save(self._deps.tree_path)
        self._deps.evaluator_ref = result.evaluator_ref
        await self._deps.publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": f"PREPARE completed with trusted metric {result.metric}.",
            },
        )
        if self._run.is_stopped() or self._state.status == "WAITING":
            return
        await self._transition_phase("SEARCH")

    async def _run_validation(self) -> None:
        if self._deps.run_validation_phase is None:
            raise RuntimeError("VALIDATE phase adapter is not configured")
        sota_id = self._tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a frozen SOTA")
        sota = self._tree.get_experiment(sota_id)
        if sota.eval is None:
            raise RuntimeError("VALIDATE requires a trusted SOTA metric")
        result = await self._deps.run_validation_phase(sota.commit, sota.eval.primary)
        if self._run.is_stopped() or self._state.status == "WAITING":
            return
        validation = result.model_dump(mode="json")
        # VALIDATE 完成后生成并持久化最终报告：内容寻址 Artifact 供 GUI/审计读取，
        # ``report_ref`` 写入 state.validation，最终文本也发布到会话流。
        report_ref = await self._deps.store.put_text(
            build_final_report(self._tree, validation)
        )
        validation["report_ref"] = report_ref
        self._state.validation = validation
        self._state.phase = "COMPLETED"
        self._state.status = "COMPLETED"
        self._plans._save_state()
        await self._deps.publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": _final_report_text(self._state.validation),
            },
        )
        await self._plans._publish_state()
        # Kaggle 竞赛：COMPLETED 后让 SupervisorAgent 自行决定是否提交最终预测。
        if self._run.kaggle_enabled:
            try:
                await self._deps.run_supervisor_turn(
                    "Research is COMPLETED. If this run targeted a Kaggle competition, "
                    "dispatch a General Agent to submit the final predictions."
                )
            except Exception:
                logger.warning("post-COMPLETED supervisor turn failed", exc_info=True)

    async def _transition_phase(self, phase: str) -> None:
        self._state.phase = phase
        if self._run.is_stopped():
            self._state.status = "STOPPED"
        elif self._state.status != "WAITING":
            self._state.status = "RUNNING"
        await self._plans._persist_state()

    async def checkpoint_validation(self, result_ref: ArtifactRef) -> None:
        """Persist one recoverable VALIDATE result through the single writer."""
        self._state.validation = {"result_ref": result_ref}
        await self._plans._persist_state()

    def _search_limit_reached(self) -> bool:
        attempts = count_search_attempts(self._state, self._tree)
        return attempts >= self._state.search_limit and not self._run.running_ids()

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        if decision not in {"SEARCH", "VALIDATE"}:
            raise ValueError(f"invalid phase decision: {decision}")
        if decision == "VALIDATE":
            if self._tree.best_experiment_id() is None:
                raise ValueError(
                    "VALIDATE requires a trusted SOTA baseline from completed PREPARE"
                )
            if self._state.phase != "SEARCH":
                raise ValueError(
                    f"cannot VALIDATE from phase {self._state.phase}; run SEARCH first"
                )
        await self._transition_phase(decision)
        if decision == "SEARCH":
            self._search._spawn_search()
        elif decision == "VALIDATE":
            # 交互路径下 ``start()`` 的生命周期已结束（SEARCH 后停在 WAITING），
            # 单写者在此直接把 VALIDATE 阶段跑完，否则只改 phase 标志永远到不了 COMPLETED。
            await self._run_validation()
        return {"decision": decision}

    async def pause(self) -> str:
        """Pause new Agent dispatch while retaining durable unfinished work."""
        self._state.status = "WAITING"
        self._deps.agents.pause()
        await self._plans._persist_state()
        return self._state.status

    async def resume(self, *, restarting: bool = False) -> str:
        """Resume Agent dispatch after an explicit Human command.

        ``restarting=True`` is used when the phase task was cancelled by pause
        (PREPARE/VALIDATE) and is about to be re-created by ``ResearchRuntime``;
        in that case avoid double-spawning the SEARCH scheduler here.
        """
        self._deps.agents.resume()
        self._state.status = "RUNNING"
        await self._plans._persist_state()
        if not restarting:
            self._run.wake()
            self._search._spawn_search()
        return self._state.status

    async def suspend(self) -> str:
        """Park a live run at WAITING before its Supervisor is torn down.

        ``stop()`` never touches ``state.status``, so closing a runtime used to
        leave ``status="RUNNING"`` on disk with nothing running — the GUI then
        reopened the session showing "运行中" over a dead supervisor. WAITING
        rather than STOPPED keeps breakpoint-resume intact: ``rearm_if_terminal``
        only clears the supervisor task for FAILED/STOPPED/COMPLETED. Any other
        status is already an honest resting state and is left untouched.
        """
        if self._state.status != "RUNNING":
            return self._state.status
        self._state.status = "WAITING"
        await self._plans._persist_state()
        return self._state.status

    async def request_stop(self) -> str:
        """Persist an explicit Human stop and cancel locally running Plans."""
        await self.stop()
        self._state.status = "STOPPED"
        await self._plans._persist_state()
        return self._state.status

    async def stop(self) -> None:
        """Stop scheduling and interrupt every locally owned Plan turn."""
        self._run.set_stopped(True)
        for plan_id in tuple(self._run.running_ids()):
            try:
                await self._deps.agents.interrupt(plan_id, "supervisor_stopped")
            except AgentCommandError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
        for task in self._run.running_tasks:
            task.cancel()
        if self._run.running_tasks:
            await asyncio.gather(*self._run.running_tasks, return_exceptions=True)
        self._run.clear_running()
        # Stop is terminal for locally owned Plan agents: release their threads
        # and facade metadata. If the same project is restarted, Recovery re-creates
        # plan threads from durable state via resume_agent.
        for plan_id in tuple(self._state.plans):
            try:
                await self._deps.agents.reap(plan_id)
            except Exception:  # noqa: BLE001 - GC must never block shutdown
                logger.warning(
                    "failed to reap Plan agent %s during stop", plan_id, exc_info=True
                )

    async def message(self, text: str) -> str:
        """Delegate ordinary Human text to the long-lived SupervisorAgent."""
        return await self._deps.run_supervisor_turn(text)

    async def set_kaggle_enabled(
        self, enabled: bool, download: bool = True
    ) -> dict[str, object]:
        self._run.set_kaggle_download(bool(download) if enabled else None)
        self._state.kaggle_download = self._run.kaggle_download
        await self._plans._persist_state()
        return {
            "kaggle_enabled": self._run.kaggle_enabled,
            "download": self._run.kaggle_download,
        }

    async def record_task_understanding(self, **payload: object) -> dict[str, object]:
        """Persist the Supervisor's structured task understanding and surface it."""
        self._state.task_understanding = dict(payload)
        await self._plans._persist_state()
        return {
            "recorded": True,
            "task_understanding": self._state.task_understanding,
        }

    async def read_hypotheses(self) -> dict[str, object]:
        """Read-only snapshot of pending hypotheses, SOTA and SEARCH attempts."""
        pending = [
            {
                "id": hypothesis.id,
                "statement": hypothesis.statement,
                "priority": hypothesis.priority,
            }
            for hypothesis in self._tree.pending_hypotheses()
        ]
        return {
            "pending": pending,
            "sota": self._tree.best_experiment_id(),
            "attempts": count_search_attempts(self._state, self._tree),
            "search_limit": self._state.search_limit,
        }

    async def read_state(self) -> dict[str, object]:
        """Read-only snapshot of the current research phase and configuration."""
        return {
            "phase": self._state.phase,
            "status": self._state.status,
            "search_limit": self._state.search_limit,
            "concurrency": self._state.concurrency,
            "manual_mode": self._state.manual_mode,
            "validation_pending": self._state.validation is not None,
        }

    async def read_plans(self) -> dict[str, object]:
        """Read-only snapshot of running and waiting Plans."""
        plans = [
            {
                "plan_id": plan_id,
                "kind": plan.kind,
                "turns_used": plan.turns_used,
                "turn_limit": plan.turn_limit,
                "patience": plan.patience,
            }
            for plan_id, plan in self._state.plans.items()
        ]
        return {"plans": plans, "running": list(self._run.running_ids())}


__all__ = ["PhaseMachine", "_final_report_text"]
