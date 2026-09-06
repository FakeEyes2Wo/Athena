"""Research phase machine and human-facing control operations."""

import asyncio
import hashlib
import logging
import traceback
from typing import Any

from athena.core.agent.types import AgentCommandError, ErrorCode
from athena.core.contracts import ArtifactRef
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus
from athena.core.workspace import GitWorkBranch
from athena.research.experiment_documents import ProjectionContext
from athena.research.prepare.authority import BaselineAuthorityError
from athena.research.report import VALIDATION_SKIPPED_NOTICE, build_final_report
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.plan_lifecycle import PlanLifecycle
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.scheduling import count_search_attempts
from athena.research.supervisor.search_loop import SearchLoop

logger = logging.getLogger(__name__)

SKIPPED_VALIDATION_OUTPUT = "SEARCH 已完成；" + VALIDATION_SKIPPED_NOTICE


def _phase_failure_run_id(stage: str, error: Exception) -> str:
    """Return a stable, content-addressed identity for one phase failure."""
    error_type = f"{type(error).__module__}.{type(error).__qualname__}"
    normalized = f"{error_type}:{' '.join(str(error).split())}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{stage}-phase-failure-{digest}"


def _final_report_text(validation: dict) -> str:
    """Format the frozen VALIDATE conclusion into a readable final report."""
    parts = ["VALIDATE completed"]

    def metric(key: str) -> str | None:
        """Format one optional validation metric for the human report."""
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
        if self._state.phase == "PREPARE" and self._state.status in {
            "FAILED",
            "STOPPED",
        }:
            self._state.status = "RUNNING"
            await self._plans.persist_state()
        try:
            if self._state.phase == "PREPARE":
                await self._run_prepare()
            else:
                await self._owner.recover()
            await self.continue_phase()
        except asyncio.CancelledError:
            # Runtime pause or shutdown cancelled the lifecycle task.
            raise
        except Exception as exc:
            # 阶段执行失败（如 evaluator 基础设施不可用）在此统一观测：置 FAILED
            # 并发布，避免被 fire-and-forget 的 supervisor task 吞掉、阶段卡在 PREPARE。
            # 只发布 ``{exc}`` 会把 traceback 丢掉，而 phase 崩溃恰恰是最需要栈的
            # 那一类：真机（2026-08-29）上 SEARCH 崩在
            # "not enough values to unpack (expected 2, got 0)"，日志里只有这一句，
            # 没有任何线索指向是哪一行。
            failed_phase = self._state.phase
            self._state.status = "FAILED"
            canonical_saved = False
            try:
                self._plans.save_state()
                canonical_saved = True
            except Exception:
                logger.warning("failed to save FAILED phase state", exc_info=True)
            if canonical_saved:
                await self._record_phase_failure(failed_phase, exc)
            logger.exception("research phase failed")
            if isinstance(exc, BaselineAuthorityError):
                published_error = "research failed: baseline authority unavailable"
            else:
                published_error = f"research failed: {exc}\n\n{traceback.format_exc()}"
            await self._deps.phases.publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "error",
                    "text": published_error,
                },
            )
            await self._plans.publish_state()
            if failed_phase == "PREPARE":
                raise

    async def continue_phase(self) -> None:
        """Execute the current phase for an idle run (interactive resume).

        ``start()`` runs PREPARE→SEARCH once, then either enters VALIDATE under
        the existing policy or, when ``skip_validate`` is enabled, writes the
        SEARCH-only Final report and enters ``COMPLETED`` without a durable
        FINAL phase. When both ``skip_validate`` and ``auto_validate`` are
        disabled, SEARCH parks at ``WAITING`` and the machine returns; a later
        Human turn may transition the phase through ``set_phase_decision`` or
        extend the Search budget. Re-enter the phase machine here to actually
        run the chosen phase.
        """
        if self._state.phase == "SEARCH":
            await self._search.run_search()
            if self._run.is_stopped():
                return
            if self._deps.phases.validation_mode == "skip":
                await self._finalize_without_validation()
                return
            if self._deps.phases.validation_mode == "auto":
                await self._transition_phase("VALIDATE")
            elif self._search_limit_reached() and self._state.status == "RUNNING":
                self._state.status = "WAITING"
                await self._plans.persist_state()
                await self._budget_gate()
        if self._state.phase == "VALIDATE":
            await self._run_validation()

    async def _finalize_without_validation(self) -> None:
        """Commit a truthful terminal result directly from a settled SEARCH."""
        if self._state.phase != "SEARCH" or self._state.status != "RUNNING":
            raise RuntimeError("skip finalization requires a running SEARCH")
        if tuple(self._run.running_ids()):
            raise RuntimeError("skip finalization requires all SEARCH plans to settle")
        if self._state.validation is not None:
            raise RuntimeError("cannot skip VALIDATE with a validation checkpoint")
        sota_id = self._tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("skip finalization requires a trusted SEARCH SOTA")
        sota = self._tree.get_experiment(sota_id)
        if sota.eval is None:
            raise RuntimeError("skip finalization requires a trusted SEARCH score")

        snapshot = (
            self._state.phase,
            self._state.status,
            self._state.validation_skipped,
        )
        self._state.phase = "COMPLETED"
        self._state.status = "COMPLETED"
        self._state.validation_skipped = True
        try:
            self._plans.save_state()
        except BaseException:
            self._state.phase, self._state.status, self._state.validation_skipped = (
                snapshot
            )
            raise

        await self._record_skipped_final(sota_id, sota)

        try:
            await self._deps.phases.publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "text",
                    "text": SKIPPED_VALIDATION_OUTPUT,
                },
            )
        except Exception:
            logger.warning("failed to publish skipped-validation output", exc_info=True)
        try:
            await self._plans.publish_state()
        except Exception:
            logger.warning("failed to publish skipped-validation state", exc_info=True)

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
            await self._deps.research.supervisor(prompt)
        except Exception:  # noqa: BLE001,S110 - optional interactive gate
            # 门失败不阻断：状态已 WAITING，人类仍可手动 configure_search / set_phase_decision
            pass

    async def _run_prepare(self) -> None:
        if self._deps.phases.prepare is None:
            raise RuntimeError("PREPARE phase adapter is not configured")
        if self._tree.best_experiment_id() is not None:
            resume_is_attested = self._deps.phases.prepare_resume_is_attested
            if resume_is_attested is None or not await resume_is_attested():
                raise BaselineAuthorityError(
                    "local PREPARE baseline is not attested by external authority"
                )
            # 断点续传：tree 已含可信 SOTA baseline（崩溃窗口为 tree 已写、phase 未转），
            # 跳过重跑 PREPARE，直接进入 SEARCH。
            await self._deps.phases.publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "text",
                    "text": "PREPARE: 已有可信 baseline（断点续传），跳过 PREPARE。",
                },
            )
            if self._state.status != "RUNNING":
                return
            await self._transition_phase("SEARCH")
            return
        result = await self._deps.phases.prepare()
        if self._state.status != "RUNNING":
            return
        hypothesis_id = "baseline"
        experiment_id = "exp_baseline"
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
                        path=str(self._deps.paths.project_root),
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
        self._state.evaluator_ref = result.evaluator_ref
        self._tree.save(self._deps.paths.tree_path)
        self._plans.save_state()
        await self._record_baseline(result)
        await self._deps.phases.publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": f"PREPARE completed with trusted metric {result.metric}.",
            },
        )
        if self._state.status != "RUNNING":
            return
        await self._transition_phase("SEARCH")

    async def _run_validation(self) -> None:
        if self._deps.phases.validation is None:
            raise RuntimeError("VALIDATE phase adapter is not configured")
        sota_id = self._tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a frozen SOTA")
        sota = self._tree.get_experiment(sota_id)
        if sota.eval is None:
            raise RuntimeError("VALIDATE requires a trusted SOTA metric")
        result = await self._deps.phases.validation(sota.commit, sota.eval.primary)
        if self._run.is_stopped() or self._state.status == "WAITING":
            return
        validation = result.model_dump(mode="json")
        # VALIDATE 完成后生成并持久化最终报告：内容寻址 Artifact 供 GUI/审计读取，
        # ``report_ref`` 写入 state.validation，最终文本也发布到会话流。
        report_ref = await self._deps.runtime.store.put_text(
            build_final_report(self._tree, validation)
        )
        validation["report_ref"] = report_ref
        self._state.validation = validation
        self._state.phase = "COMPLETED"
        self._state.status = "COMPLETED"
        self._plans.save_state()
        await self._record_final(sota_id, sota, validation)
        await self._deps.phases.publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": _final_report_text(self._state.validation),
            },
        )
        await self._plans.publish_state()
        # Kaggle 竞赛：COMPLETED 后让 SupervisorAgent 自行决定是否提交最终预测。
        if self._run.kaggle_enabled:
            try:
                await self._deps.research.supervisor(
                    "Research is COMPLETED. If this run targeted a Kaggle competition, "
                    "dispatch a General Agent to submit the final predictions."
                )
            except Exception:
                logger.warning("post-COMPLETED supervisor turn failed", exc_info=True)

    async def _record_phase_failure(self, phase: str, error: Exception) -> None:
        """Persist one terminal phase failure with its direct cause."""
        stage = {"PREPARE": "baseline", "SEARCH": "search", "VALIDATE": "final"}.get(
            phase
        )
        if stage is None:
            return
        await self._project_stage(
            {
                "run_id": _phase_failure_run_id(stage, error),
                "stage": stage,
                "status": "FAILED",
                "metric": {"primary": None},
                "reason": {
                    "kind": "phase_failed",
                    "summary": " ".join(str(error).split())[:1000],
                },
                "provenance": {"phase": phase},
            }
        )

    async def _record_baseline(self, result: Any) -> None:
        """Persist the trusted PREPARE baseline and refresh derived reports."""
        experiment = self._tree.get_experiment("exp_baseline")
        await self._project_stage(
            {
                "run_id": "exp_baseline",
                "stage": "baseline",
                "status": experiment.status.value,
                "metric": {"primary": result.metric},
                "artifacts": experiment.artifacts,
                "reason": {
                    "kind": "trusted_score",
                    "summary": (
                        "The authoritative baseline passed the frozen SEARCH evaluator."
                    ),
                },
                "provenance": {
                    "experiment_id": "exp_baseline",
                    "hypothesis_id": "baseline",
                    "commit": result.commit,
                },
            }
        )

    async def _record_final(
        self, sota_id: str, sota: Experiment, validation: dict
    ) -> None:
        """Persist independent FINAL evaluation and its generalization cause."""
        artifacts = {
            key.removesuffix("_ref"): value
            for key, value in validation.items()
            if key.endswith("_ref") and isinstance(value, str)
        }
        warning = bool(validation.get("generalization_warning"))
        await self._project_stage(
            {
                "run_id": str(validation.get("result_id") or "final"),
                "stage": "final",
                "status": str(validation.get("status") or "COMPLETED"),
                "metric": {
                    "primary": validation.get("final_test_score"),
                    "reference": validation.get("test_score"),
                    "generalization_gap": validation.get("generalization_gap"),
                },
                "artifacts": artifacts,
                "reason": {
                    "kind": "generalization_warning" if warning else "final_score",
                    "summary": (
                        "FINAL score is worse than SEARCH beyond tolerance."
                        if warning
                        else "The frozen SOTA completed independent FINAL evaluation."
                    ),
                },
                "provenance": {
                    "sota_experiment_id": sota_id,
                    "sota_commit": validation.get("sota_commit") or sota.commit,
                    "validation_commit": validation.get("validation_commit"),
                },
            }
        )

    async def _record_skipped_final(self, sota_id: str, sota: Experiment) -> None:
        """Persist the stable, explicitly unvalidated final-stage record."""
        await self._project_stage(
            {
                "run_id": "final-skipped",
                "stage": "final",
                "status": "SKIPPED",
                "metric": {
                    "primary": None,
                    "reference": sota.eval.primary,
                },
                "reason": {
                    "kind": "validation_skipped",
                    "summary": "Independent VALIDATE was skipped by project policy.",
                },
                "provenance": {
                    "sota_experiment_id": sota_id,
                    "sota_commit": sota.commit,
                },
            }
        )

    async def _project_stage(self, event: dict[str, object]) -> None:
        """Project a stage after canonical saves, containing derived failures."""
        try:
            outcome = self._deps.runtime.documents.project(
                event,
                ProjectionContext(self._tree, self._state, self._deps.search.direction),
            )
        except Exception:
            logger.warning("document stage projection failed", exc_info=True)
            outcome = False
        await self._owner._publish_document_outcome(outcome)

    async def _transition_phase(self, phase: str) -> None:
        self._state.phase = phase
        if self._state.status == "STOPPED":
            self._state.status = "STOPPED"
        elif self._state.status != "WAITING":
            self._state.status = "RUNNING"
        await self._plans.persist_state()

    async def checkpoint_validation(self, result_ref: ArtifactRef) -> None:
        """Persist one recoverable VALIDATE result through the single writer."""
        self._state.validation = {"result_ref": result_ref}
        await self._plans.persist_state()

    def _search_limit_reached(self) -> bool:
        attempts = count_search_attempts(self._state, self._tree)
        return attempts >= self._state.search_limit and not self._run.running_ids()

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        """Apply a validated Human decision to continue SEARCH or enter VALIDATE."""
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
        self._deps.runtime.agents.pause()
        await self._plans.persist_state()
        return self._state.status

    async def resume(self, *, restarting: bool = False) -> str:
        """Resume Agent dispatch after an explicit Human command.

        ``restarting=True`` is used when the phase task was cancelled by pause
        (PREPARE/VALIDATE) and is about to be re-created by ``ResearchRuntime``;
        in that case avoid double-spawning the SEARCH scheduler here.
        """
        self._deps.runtime.agents.resume()
        self._state.status = "RUNNING"
        await self._plans.persist_state()
        if not restarting:
            self._run.wake()
            self._search._spawn_search()
        return self._state.status

    async def suspend(self) -> str:
        """Park a live run at WAITING before its Supervisor is torn down.

        WAITING rather than STOPPED keeps breakpoint-resume intact. ``stop()``
        enforces the same projection when called directly; this method lets the
        runtime publish the resting state before it tears down infrastructure.
        """
        if self._state.status != "RUNNING":
            return self._state.status
        self._state.status = "WAITING"
        await self._plans.persist_state()
        return self._state.status

    async def request_stop(self) -> str:
        """Persist an explicit Human stop and cancel locally running Plans."""
        self._state.status = "STOPPED"
        await self._plans.persist_state()
        await self.stop()
        return self._state.status

    async def stop(self) -> None:
        """Park scheduling and interrupt every locally owned Plan turn."""
        if self._state.status == "RUNNING":
            self._state.status = "WAITING"
            await self._plans.persist_state()
        self._run.wake()
        for plan_id in tuple(self._run.running_ids()):
            try:
                await self._deps.runtime.agents.interrupt(plan_id, "supervisor_stopped")
            except AgentCommandError as exc:
                # Plan Agent may finish between the running snapshot and interrupt.
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
        for task in self._run.running_tasks:
            task.cancel()
        if self._run.running_tasks:
            await asyncio.gather(*self._run.running_tasks, return_exceptions=True)
        self._run.clear_running()
        await self._search.cancel()
        # Stop is terminal for locally owned Plan agents: release their threads
        # and facade metadata. If the same project is restarted, Recovery re-creates
        # plan threads from durable state via resume_agent.
        for plan_id in tuple(self._state.plans):
            try:
                await self._deps.runtime.agents.reap(plan_id)
            except Exception:
                logger.warning(
                    "failed to reap Plan agent %s during stop", plan_id, exc_info=True
                )

    async def message(self, text: str) -> str:
        """Delegate ordinary Human text to the long-lived SupervisorAgent."""
        return await self._deps.research.supervisor(text)

    async def set_kaggle_enabled(
        self, enabled: bool, download: bool = True
    ) -> dict[str, object]:
        """Persist the Kaggle integration and download policy for this run."""
        self._run.set_kaggle_download(bool(download) if enabled else None)
        await self._plans.persist_state()
        return {
            "kaggle_enabled": self._run.kaggle_enabled,
            "download": self._run.kaggle_download,
        }

    async def record_task_understanding(self, **payload: object) -> dict[str, object]:
        """Persist the Supervisor's structured task understanding and surface it."""
        self._state.task_understanding = dict(payload)
        await self._plans.persist_state()
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


__all__ = ["SKIPPED_VALIDATION_OUTPUT", "PhaseMachine", "_final_report_text"]
