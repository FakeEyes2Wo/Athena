"""Single-writer orchestration for autonomous SEARCH Plans."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from athena.agents.supervisor_agent import MAX_PLAN_TURNS, SupervisorActions
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash, new_id
from athena.core.agent.types import AgentCommandError, ErrorCode
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch, GitWorkspace
from athena.research.contracts import GeneralTurnOutcome
from athena.research.supervisor.experiment import (
    PlanTurnResult,
    decide_settlement,
    load_agent_result,
    load_best,
)
from athena.research.supervisor.plans import (
    PlanDecision,
    PlanInput,
    PlanState,
    wait_run_events,
)
from athena.research.supervisor.prepare import PrepareResult
from athena.research.report import build_final_report
from athena.research.rubrics.models import EvaluationPolicy
from athena.research.supervisor.policy import Outcome
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import (
    ScheduleKind,
    Scheduler,
    count_search_attempts,
)
from athena.research.supervisor.state import ResearchState

logger = logging.getLogger(__name__)

PlanTurn = Callable[[str, PlanState], Awaitable[PlanTurnResult]]
SupervisorTurn = Callable[[str], Awaitable[str]]
Publish = Callable[[Literal["output", "state"], dict[str, object]], Awaitable[None]]
PreparePhase = Callable[[], Awaitable[PrepareResult]]
ValidationPhase = Callable[[CommitHash, float], Awaitable[object]]
IdeatorTurn = Callable[[int], Awaitable[list[Hypothesis]]]
HypothesisRubricTurn = Callable[[list[Hypothesis]], Awaitable[list[Hypothesis]]]
GeneralTurn = Callable[[str, str | None], Awaitable[GeneralTurnOutcome]]
PublishAgentEvent = Callable[[str, str, str, dict | None], Awaitable[None] | None]


def _compare_metric(
    candidate: float,
    reference: float,
    direction: Literal["maximize", "minimize"],
    tolerance: float,
) -> Outcome:
    delta = candidate - reference if direction == "maximize" else reference - candidate
    if delta > tolerance:
        return Outcome.WIN
    if delta < -tolerance:
        return Outcome.LOSS
    return Outcome.DRAW


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


@dataclass(frozen=True)
class _CompletedTurn:
    plan_id: str
    decision: PlanDecision | None
    result: PlanTurnResult | None


class Supervisor(SupervisorActions):
    """Own the only durable ResearchState and ResearchTree mutation loop."""

    def __init__(
        self,
        *,
        project_root: Path,
        state_root: Path | None = None,
        state: ResearchState,
        tree: ResearchTree,
        store: ArtifactStore,
        agents: AgentRuntime,
        workspaces: GitWorkspace,
        scheduler: Scheduler,
        recovery: Recovery,
        evaluator_ref: ArtifactRef | None,
        run_plan_turn: PlanTurn,
        run_supervisor_turn: SupervisorTurn,
        publish: Publish,
        auto_validate: bool = False,
        run_ideator_turn: IdeatorTurn | None = None,
        run_hypothesis_rubric: HypothesisRubricTurn | None = None,
        run_general_turn: GeneralTurn | None = None,
        direction: Literal["maximize", "minimize"] = "maximize",
        tolerance: float = 0.0,
        run_prepare_phase: PreparePhase | None = None,
        run_validation_phase: ValidationPhase | None = None,
        publish_agent_event: PublishAgentEvent | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        _athena = (
            Path(state_root)
            if state_root is not None
            else self._project_root / ".athena"
        )
        self._state_path = _athena / "state.json"
        self._tree_path = _athena / "research_tree.json"
        self.state = state
        self.tree = tree
        self._store = store
        self._agents = agents
        self._workspaces = workspaces
        self._scheduler = scheduler
        self._recovery = recovery
        self._evaluator_ref = (
            evaluator_ref if evaluator_ref is not None else state.evaluator_ref
        )
        self._direction = direction
        self._tolerance = tolerance
        self._run_plan_turn = run_plan_turn
        self._run_supervisor_turn = run_supervisor_turn
        self._run_ideator_turn = run_ideator_turn
        self._run_hypothesis_rubric = run_hypothesis_rubric
        self._run_general_turn = run_general_turn
        self._publish = publish
        self._auto_validate = auto_validate
        self._run_prepare_phase = run_prepare_phase
        self._run_validation_phase = run_validation_phase
        self._publish_agent_event = publish_agent_event
        self._branches: dict[str, GitWorkBranch] = {}
        self._next_guidance: str | None = None
        self._persistent_guidance: list[str] = []
        self._running: dict[str, asyncio.Task[_CompletedTurn]] = {}
        self._next_hypothesis_id: str | None = None
        self._stopped = False
        # None=关闭, True=接入且下载, False=接入但不下载（任务理解阶段由 SupervisorAgent 决定）。
        self._kaggle_download: bool | None = state.kaggle_download
        self._evaluation_policy: EvaluationPolicy | None = None
        # 手动模式下等待人工选定假设时，唤醒 run_search 循环的信号。
        self._wake = asyncio.Event()
        # SEARCH 调度循环的后台任务（供 WAITING→RUNNING 重入）；首轮由 start() 直接 await。
        self._search_task: asyncio.Task | None = None

    @property
    def running_plan_ids(self) -> tuple[str, ...]:
        """Return currently dispatched Plan IDs."""
        return tuple(self._running)

    @property
    def next_hypothesis_id(self) -> str | None:
        """Return the pending one-shot Human selection, if any."""
        return self._next_hypothesis_id

    @property
    def evaluator_ref(self) -> ArtifactRef | None:
        """Return the frozen evaluator owned by the current research run."""
        return self._evaluator_ref

    @property
    def evaluation_policy(self) -> EvaluationPolicy | None:
        """Return the in-memory policy applied to authoritative comparisons."""
        return self._evaluation_policy

    @property
    def evaluation_policy_ref(self) -> ArtifactRef | None:
        """Return the persisted Evaluation Policy artifact reference."""
        return self.state.evaluation_policy_ref

    @property
    def kaggle_enabled(self) -> bool:
        return self._kaggle_download is not None

    @property
    def kaggle_download(self) -> bool:
        return self._kaggle_download is not False

    async def set_kaggle_enabled(
        self, enabled: bool, download: bool = True
    ) -> dict[str, object]:
        self._kaggle_download = bool(download) if enabled else None
        self.state.kaggle_download = self._kaggle_download
        await self._persist_state()
        return {"kaggle_enabled": self.kaggle_enabled, "download": self.kaggle_download}

    async def record_task_understanding(self, **payload: object) -> dict[str, object]:
        """Persist the Supervisor's structured task understanding and surface it."""
        self.state.task_understanding = dict(payload)
        await self._persist_state()
        return {"recorded": True, "task_understanding": self.state.task_understanding}

    async def checkpoint_evaluator(self, ref: ArtifactRef) -> dict[str, object]:
        """Persist one frozen evaluator bundle so PREPARE resumes past evaluator."""
        self._evaluator_ref = ref
        self.state.evaluator_ref = ref
        await self._persist_state()
        return {"evaluator_ref": ref}

    def apply_evaluation_policy(self, policy: EvaluationPolicy) -> None:
        """Set the single authoritative direction before evaluator/Plan creation."""
        self._evaluation_policy = policy
        self._direction = policy.direction

    async def checkpoint_evaluation_policy(
        self, ref: ArtifactRef, policy: EvaluationPolicy
    ) -> dict[str, object]:
        """Apply and persist the frozen policy for resume and downstream routing."""
        self.apply_evaluation_policy(policy)
        self.state.evaluation_policy_ref = ref
        await self._persist_state()
        return {
            "evaluation_policy_ref": ref,
            "primary_metric": policy.primary_metric,
            "direction": policy.direction,
        }

    async def read_hypotheses(self) -> dict[str, object]:
        """Read-only snapshot of pending hypotheses, SOTA and SEARCH attempts."""
        pending = [
            {
                "id": hypothesis.id,
                "statement": hypothesis.statement,
                "priority": hypothesis.priority,
            }
            for hypothesis in self.tree.pending_hypotheses()
        ]
        return {
            "pending": pending,
            "sota": self.tree.best_experiment_id(),
            "attempts": count_search_attempts(self.state, self.tree),
            "search_limit": self.state.search_limit,
        }

    async def record_guidance(self, text: str, scope: str) -> dict[str, object]:
        """Record guidance that will be frozen only into later Plan inputs."""
        if not text.strip():
            raise ValueError("guidance text must be nonblank")
        if scope == "next":
            self._next_guidance = text
        elif scope == "persistent":
            self._persistent_guidance.append(text)
        else:
            raise ValueError(f"unsupported guidance scope: {scope}")
        return {"text": text, "scope": scope}

    async def start_plan(self, hypothesis_id: str) -> str:
        """Freeze one Hypothesis input and create its stable execution identity."""
        if self._evaluator_ref is None:
            baselines = self.tree.experiments(kind="baseline")
            if not baselines:
                raise RuntimeError("SEARCH Plan requires a frozen evaluator")
            self._evaluator_ref = baselines[0].plan.run_config_ref
        if hypothesis_id in self.state.plans:
            return hypothesis_id
        hypothesis = self.tree.get_hypothesis(hypothesis_id)
        existing = self.tree.experiment_for_hypothesis(hypothesis_id)
        if existing is not None and self.tree.get_experiment(existing).status in {
            ExperimentStatus.SUCCEEDED,
            ExperimentStatus.FAILED,
        }:
            raise ValueError(f"hypothesis already settled: {hypothesis_id}")

        reference_id = hypothesis.parent_id or self.tree.best_experiment_id()
        if reference_id is None:
            raise ValueError("SEARCH Plan requires a frozen reference experiment")
        reference = self.tree.get_experiment(reference_id)
        if reference.eval is None:
            raise ValueError("reference experiment requires a trusted metric")
        reference_hypothesis = self.tree.get_hypothesis(reference.hypothesis_id)
        active = self.tree.active_hypotheses(reference_id, hypothesis)[:-1]
        tree_ref = await self._store.put_text(
            json.dumps(self.tree.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        guidance = [*self._persistent_guidance]
        if self._next_guidance is not None:
            guidance.append(self._next_guidance)
            self._next_guidance = None
        plan_input = PlanInput(
            hypothesis=hypothesis,
            active_ancestor_hypotheses=active,
            reference_experiment_id=reference_id,
            reference_metric=reference.eval.primary,
            reference_priority=reference_hypothesis.priority,
            direction=self._direction,
            tolerance=self._tolerance,
            evaluator_ref=self._evaluator_ref,
            tree_ref=tree_ref,
            human_context="\n".join(guidance),
            initial_turn_limit=hypothesis.turn_limit,
            initial_patience=hypothesis.patience,
        )
        context_ref = await self._store.put_text(plan_input.model_dump_json())
        branch = await self._workspaces.create(reference.commit, hypothesis_id)
        self._branches[hypothesis_id] = branch
        experiment_id = f"exp_{hypothesis_id}"
        self.tree.add_experiment(
            experiment_id,
            Experiment(
                parent_id=hypothesis.parent_id,
                hypothesis_id=hypothesis_id,
                commit=reference.commit,
                plan=ExperimentPlan(
                    kind="search",
                    change=hypothesis.intervention,
                    run_config_ref=context_ref,
                    budget={},
                    acceptance_rule="trusted score",
                ),
                gitwork=branch,
            ),
        )
        self.tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)
        self.state.plans[hypothesis_id] = PlanState(
            kind="SEARCH",
            context_ref=context_ref,
            turns_used=0,
            turn_limit=hypothesis.turn_limit,
            patience=hypothesis.patience,
        )
        self._save_state()
        await self._agents.resume_agent(
            hypothesis_id, agent_type="plan", name=hypothesis_id
        )
        await self._publish_state()
        return hypothesis_id

    async def plan_input(self, plan_id: str) -> PlanInput:
        """Load the immutable input frozen for one active Plan."""
        return PlanInput.model_validate_json(
            await self._store.get_text(self.state.plans[plan_id].context_ref)
        )

    def workspace_path(self, plan_id: str) -> Path:
        """Return the real worktree path bound to a stable Plan identity."""
        return Path(self._branches[plan_id].path)

    def workspace(self, plan_id: str) -> GitWorkBranch:
        """Return the branch owned by one active Plan."""
        return self._branches[plan_id]

    @staticmethod
    def plan_identity(plan_id: str) -> dict[str, str]:
        """Project the stable SEARCH identity shared by all owned resources."""
        return {
            "plan": plan_id,
            "agent": plan_id,
            "workspace": plan_id,
            "log": plan_id,
        }

    def _save_state(self) -> None:
        self.state.save(self._state_path)

    async def _publish_state(self) -> None:
        await self._publish(
            "state", {"type": "state", **self.state.model_dump(mode="json")}
        )

    async def _persist_state(self) -> None:
        """Save durable state and publish one snapshot to subscribers."""
        self._save_state()
        await self._publish_state()

    async def start(self) -> None:
        """Start the Supervisor lifecycle."""
        self._stopped = False
        try:
            if self.state.phase == "PREPARE":
                await self._run_prepare()
            else:
                await self.recover()
            await self.continue_phase()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 阶段执行失败（如 evaluator 基础设施不可用）在此统一观测：置 FAILED
            # 并发布，避免被 fire-and-forget 的 supervisor task 吞掉、阶段卡在 PREPARE。
            self.state.status = "FAILED"
            self._save_state()
            await self._publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "error",
                    "text": f"research failed: {exc}",
                },
            )
            await self._publish_state()

    async def continue_phase(self) -> None:
        """Execute the current phase for an idle run (interactive resume).

        ``start()`` runs PREPARE→SEARCH→VALIDATE once. In interactive mode
        (no ``auto_validate``) SEARCH parks at ``WAITING`` and the machine
        returns; a later Human turn may transition the phase through
        ``set_phase_decision`` or extend the Search budget. Re-enter the phase
        machine here to actually run the chosen phase.
        """
        if self.state.phase == "SEARCH":
            await self.run_search()
            if self._stopped:
                return
            if self._auto_validate:
                await self._transition_phase("VALIDATE")
            elif self._search_limit_reached() and self.state.status == "RUNNING":
                self.state.status = "WAITING"
                await self._persist_state()
                await self._budget_gate()
        if self.state.phase == "VALIDATE":
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
            await self._run_supervisor_turn(prompt)
        except Exception:
            # 门失败不阻断：状态已 WAITING，人类仍可手动 configure_search / set_phase_decision
            pass

    async def _run_prepare(self) -> None:
        if self._run_prepare_phase is None:
            raise RuntimeError("PREPARE phase adapter is not configured")
        if self.tree.best_experiment_id() is not None:
            # 断点续传：tree 已含可信 SOTA baseline（崩溃窗口为 tree 已写、phase 未转），
            # 跳过重跑 PREPARE，直接进入 SEARCH。
            await self._publish(
                "output",
                {
                    "source": "supervisor",
                    "channel": "text",
                    "text": "PREPARE: 已有可信 baseline（断点续传），跳过 PREPARE。",
                },
            )
            await self._transition_phase("SEARCH")
            return
        result = await self._run_prepare_phase()
        hypothesis_id = "baseline"
        experiment_id = "exp_baseline"
        if self.tree.best_experiment_id() is None:
            self.tree.add_hypothesis(
                Hypothesis(
                    id=hypothesis_id,
                    statement="trusted PREPARE baseline",
                    intervention="establish the baseline implementation",
                    expected_effect="provide the SEARCH reference metric",
                )
            )
            self.tree.add_experiment(
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
                        path=str(self._project_root),
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
            self.tree.set_sota(experiment_id)
            self.tree.save(self._tree_path)
        self._evaluator_ref = result.evaluator_ref
        await self._publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": f"PREPARE completed with trusted metric {result.metric}.",
            },
        )
        await self._transition_phase("SEARCH")

    async def _run_validation(self) -> None:
        if self._run_validation_phase is None:
            raise RuntimeError("VALIDATE phase adapter is not configured")
        sota_id = self.tree.best_experiment_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a frozen SOTA")
        sota = self.tree.get_experiment(sota_id)
        if sota.eval is None:
            raise RuntimeError("VALIDATE requires a trusted SOTA metric")
        result = await self._run_validation_phase(sota.commit, sota.eval.primary)
        validation = result.model_dump(mode="json")
        # VALIDATE 完成后生成并持久化最终报告：内容寻址 Artifact 供 GUI/审计读取，
        # ``report_ref`` 写入 state.validation，最终文本也发布到会话流。
        report_ref = await self._store.put_text(
            build_final_report(self.tree, validation)
        )
        validation["report_ref"] = report_ref
        self.state.validation = validation
        self.state.phase = "COMPLETED"
        self.state.status = "COMPLETED"
        self._save_state()
        await self._publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": _final_report_text(self.state.validation),
            },
        )
        await self._publish_state()
        # Kaggle 竞赛：COMPLETED 后让 SupervisorAgent 自行决定是否提交最终预测。
        if self.kaggle_enabled:
            try:
                await self._run_supervisor_turn(
                    "Research is COMPLETED. If this run targeted a Kaggle competition, "
                    "dispatch a General Agent to submit the final predictions."
                )
            except Exception:
                logger.warning("post-COMPLETED supervisor turn failed", exc_info=True)

    async def _transition_phase(self, phase: Literal["SEARCH", "VALIDATE"]) -> None:
        self.state.phase = phase
        self.state.status = "RUNNING"
        await self._persist_state()

    async def checkpoint_validation(self, result_ref: ArtifactRef) -> None:
        """Persist one recoverable VALIDATE result through the single writer."""
        self.state.validation = {"result_ref": result_ref}
        await self._persist_state()

    def _search_limit_reached(self) -> bool:
        attempts = count_search_attempts(self.state, self.tree)
        return attempts >= self.state.search_limit and not self._running

    async def recover(self, state: ResearchState | None = None) -> ResearchState:
        """Reconcile persisted Plans without inventing missing frozen resources."""
        if self._tree_path.is_file():
            self.tree = ResearchTree.load(self._tree_path)
        candidate = state or self.state
        artifact_presence: dict[str, bool] = {}
        for plan in candidate.plans.values():
            try:
                await self._store.get_text(plan.context_ref)
            except Exception:
                artifact_presence[plan.context_ref] = False
            else:
                artifact_presence[plan.context_ref] = True
        workspace_presence: dict[str, bool] = {}
        for plan_id, plan in candidate.plans.items():
            if not artifact_presence[plan.context_ref]:
                workspace_presence[plan_id] = False
                continue
            try:
                plan_input = PlanInput.model_validate_json(
                    await self._store.get_text(plan.context_ref)
                )
                if plan_input.reference_experiment_id is None:
                    raise ValueError("Plan input has no reference experiment")
                reference = self.tree.get_experiment(plan_input.reference_experiment_id)
                branch = await self._workspaces.create(reference.commit, plan_id)
            except Exception:
                workspace_presence[plan_id] = False
            else:
                self._branches[plan_id] = branch
                workspace_presence[plan_id] = Path(branch.path).is_dir()
        self.state = self._recovery.reconcile(
            candidate,
            self.tree,
            workspace_exists=lambda plan_id: workspace_presence.get(plan_id, False),
            artifact_exists=lambda ref: artifact_presence.get(ref, False),
        )
        for plan_id in self.state.plans:
            if workspace_presence.get(plan_id):
                await self._agents.resume_agent(
                    plan_id, agent_type="plan", name=plan_id
                )
        await self._persist_state()
        return self.state

    def _spawn_search(self) -> None:
        """(重新)进入 SEARCH 调度循环（当前空闲且 RUNNING 时）。

        auto 模式下 ``run_search`` 达到 search_limit 后返回、状态置 WAITING，随后
        ``/resume``、``configure_search``、``update_waiting_plan_budget`` 会把状态改回
        RUNNING，但若不在此重新拉起后台调度任务，就永远不再推进（静默 liveness 失败）。
        """
        if (
            self.state.phase == "SEARCH"
            and self.state.status == "RUNNING"
            and not self._stopped
            and (self._search_task is None or self._search_task.done())
        ):
            task = asyncio.create_task(self.run_search())
            task.add_done_callback(self._on_search_done)
            self._search_task = task

    @staticmethod
    def _on_search_done(task: asyncio.Task) -> None:
        """检索后台 SEARCH 任务的异常，避免 'Task exception was never retrieved'。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("SEARCH scheduling loop crashed: %r", exc)

    async def run_search(self) -> None:
        """Run rolling SEARCH scheduling."""
        self._stopped = False
        while not self._stopped:
            if self.state.status != "RUNNING":
                # 暂停/等待人工决策：不再派发新 turn，直到 /resume 或选择操作唤醒。
                self._wake.clear()
                await self._wake.wait()
                if self._stopped:
                    return
                continue
            generated = await self._fill_slots()
            if not self._running:
                if generated:
                    continue
                if await self._wait_for_manual_selection():
                    continue
                return
            done, _pending = await asyncio.wait(
                tuple(self._running.values()), return_when=asyncio.FIRST_COMPLETED
            )
            if self._stopped:
                return
            task = next(iter(done))
            plan_id = next(
                item_id
                for item_id, item_task in self._running.items()
                if item_task is task
            )
            self._running.pop(plan_id)
            try:
                completed = task.result()
            except Exception:
                await self._publish_state()
                continue
            await self._apply_completed_turn(completed)

    async def _wait_for_manual_selection(self) -> bool:
        """Block the SEARCH loop until a Human selects a hypothesis in manual mode.

        Returns True when scheduling should re-run (selection made or mode
        switched back to auto), False when the loop should exit (not manual or
        nothing pending to select from).
        """
        if not self.state.manual_mode:
            return False
        if self._next_hypothesis_id is not None:
            return True
        if not self.tree.pending_hypotheses():
            return False
        self.state.status = "WAITING"
        await self._persist_state()
        self._wake.clear()
        await self._wake.wait()
        return not self._stopped

    async def _fill_slots(self) -> bool:
        """Apply Scheduler actions until all available slots are accounted for."""
        if self._stopped:
            return False
        generated = False
        actions = self._scheduler.next_actions(
            self.state,
            self.tree,
            self._running,
            human_next=self._next_hypothesis_id,
            manual=self.state.manual_mode,
        )
        for action in actions:
            if self._stopped:
                return generated
            if action.kind is ScheduleKind.GENERATE:
                if self._run_ideator_turn is None:
                    before = len(self.tree.pending_hypotheses())
                    await self._run_supervisor_turn(
                        f"Propose exactly {action.count} new SEARCH hypotheses."
                    )
                    if self._stopped:
                        return generated
                    generated = len(self.tree.pending_hypotheses()) > before
                else:
                    hypotheses = await self._run_ideator_turn(action.count)
                    if self._stopped:
                        return generated
                    registered = await self.register_hypotheses(hypotheses)
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
                await self.start_plan(plan_id)
                if self._stopped:
                    return generated
            if action.kind is ScheduleKind.START_NEXT_HYPOTHESIS:
                self._next_hypothesis_id = None
            self._launch_turn(plan_id)
        return generated

    def _launch_turn(self, plan_id: str) -> None:
        if plan_id not in self._running:
            self._running[plan_id] = asyncio.create_task(self._run_one_turn(plan_id))

    async def _run_one_turn(self, plan_id: str) -> _CompletedTurn:
        """Spend one turn durably, run the Agent, then execute trusted scoring."""
        state = self.state.plans[plan_id]
        state = state.model_copy(update={"turns_used": state.turns_used + 1})
        self.state.plans[plan_id] = state
        await self._persist_state()
        try:
            run_id = await self._agents.followup(
                plan_id,
                {
                    "content": (
                        f"Continue Plan {plan_id}. Turns used: {state.turns_used}; "
                        f"turn limit: {state.turn_limit}; patience: {state.patience}; "
                        f"stale rounds: {state.stale_rounds}."
                    ),
                    "context_refs": [state.context_ref],
                },
            )
            if self._publish_agent_event is None:
                summary = await self._agents.wait_run(run_id)
            else:
                summary = await wait_run_events(
                    self._agents,
                    run_id,
                    lambda kind, ref, data: self._publish_agent_event(
                        plan_id, kind, ref, data
                    ),
                )
            decision = await load_agent_result(summary, self._store, PlanDecision)
            if decision is None:
                return _CompletedTurn(plan_id, None, None)
        except Exception:
            return _CompletedTurn(plan_id, None, None)
        if decision.decision == "abandon" and state.best_ref is None:
            return _CompletedTurn(plan_id, decision, None)
        result = await self._run_plan_turn(plan_id, state)
        return _CompletedTurn(plan_id, decision, result)

    async def _apply_completed_turn(self, completed: _CompletedTurn) -> None:
        plan_id = completed.plan_id
        state = self.state.plans[plan_id]
        if completed.result is not None and completed.result.next_state is not None:
            state = completed.result.next_state
            self.state.plans[plan_id] = state
        if completed.decision is None:
            if state.turn_limit is not None and state.turns_used >= state.turn_limit:
                self.state.status = "WAITING"
            await self._persist_state()
            return
        if completed.decision.decision == "abandon" and state.best_ref is None:
            await self._settle_plan(plan_id, None, completed.result)
            await self._publish_state()
            return
        settlement = decide_settlement(
            state,
            completed.decision,
            report_ref=completed.result.report_ref if completed.result else None,
        )
        if settlement.action == "continue":
            self._save_state()
        elif settlement.action == "wait":
            if self._auto_validate:
                # auto 模式无人补充缺失的 report / 延长预算 → 用历史 best 结算，
                # 释放并发槽让调度器继续 GENERATE，搜索得以收敛（否则死锁）。
                await self._settle_plan(plan_id, state.best_ref, completed.result)
            else:
                self.state.status = "WAITING"
                self._save_state()
        else:
            await self._settle_plan(plan_id, settlement.best_ref, completed.result)
        await self._publish_state()

    async def _settle_plan(
        self,
        plan_id: str,
        best_ref: ArtifactRef | None,
        result: PlanTurnResult | None,
    ) -> None:
        """Persist one final Experiment before removing the active Plan."""
        plan_input = await self.plan_input(plan_id)
        hypothesis = self.tree.get_hypothesis(plan_id)
        experiment_id = f"exp_{plan_id}"
        primary: float | None = None
        outcome: Outcome | None = None
        if best_ref is None:
            self.tree.transition_experiment(
                experiment_id,
                ExperimentStatus.FAILED,
                error="settled without a trusted result",
            )
        else:
            best = await load_best(best_ref, self._store)
            reference = plan_input.reference_metric
            outcome = (
                Outcome.WIN
                if reference is None
                else _compare_metric(
                    best.metric,
                    reference,
                    plan_input.direction,
                    plan_input.tolerance,
                )
            )
            evidence_ref = best.evidence_ref
            artifacts = {"evidence": evidence_ref}
            try:
                evidence = json.loads(await self._store.get_text(evidence_ref))
            except (json.JSONDecodeError, OSError, ValueError):
                evidence = {}
            for key in ("predictions_ref", "report_ref"):
                ref = evidence.get(key) if isinstance(evidence, dict) else None
                if isinstance(ref, str):
                    artifacts[key.removesuffix("_ref")] = ref
            if "predictions" not in artifacts and result is not None:
                if result.predictions_ref is not None:
                    artifacts["predictions"] = result.predictions_ref
            if "report" not in artifacts and result is not None:
                if result.report_ref is not None:
                    artifacts["report"] = result.report_ref
            primary = best.metric
            self.tree.complete_experiment(
                experiment_id,
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=best.metric,
                    per_sample=evidence_ref,
                ),
                verdict=None,
                artifacts=artifacts,
                commit=best.commit,
            )
        if outcome is None:
            # 实验无有效证据：标记 INCONCLUSIVE，不按胜负更新评级。
            self.tree.update_hypothesis_status(plan_id, "INCONCLUSIVE")
        else:
            hypothesis.priority = self._scheduler.settle(
                plan_input.reference_priority, outcome
            )
            self.tree.update_hypothesis_status(
                plan_id, "SUPPORTED" if outcome is Outcome.WIN else "REFUTED"
            )
        if primary is not None:
            sota_id = self.tree.best_experiment_id()
            if sota_id is None:
                self.tree.set_sota(experiment_id)
            else:
                sota = self.tree.get_experiment(sota_id)
                if (
                    sota.eval is None
                    or _compare_metric(
                        primary,
                        sota.eval.primary,
                        plan_input.direction,
                        plan_input.tolerance,
                    )
                    is Outcome.WIN
                ):
                    self.tree.set_sota(experiment_id)
        self.tree.save(self._tree_path)
        self.state.plans.pop(plan_id)
        self._save_state()

    async def message(self, text: str) -> str:
        """Delegate ordinary Human text to the long-lived SupervisorAgent."""
        return await self._run_supervisor_turn(text)

    async def pause(self) -> str:
        """Pause new Agent dispatch while retaining durable unfinished work."""
        self.state.status = "WAITING"
        self._agents.pause()
        await self._persist_state()
        return self.state.status

    async def resume(self) -> str:
        """Resume Agent dispatch after an explicit Human command."""
        self._agents.resume()
        self.state.status = "RUNNING"
        await self._persist_state()
        self._wake.set()
        self._spawn_search()
        return self.state.status

    async def request_stop(self) -> str:
        """Persist an explicit Human stop and cancel locally running Plans."""
        await self.stop()
        self.state.status = "STOPPED"
        await self._persist_state()
        return self.state.status

    async def stop(self) -> None:
        """Stop scheduling and interrupt every locally owned Plan turn."""
        self._stopped = True
        self._wake.set()
        for plan_id in tuple(self._running):
            try:
                await self._agents.interrupt(plan_id, "supervisor_stopped")
            except AgentCommandError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
        for task in self._running.values():
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    def _sota_parent(self) -> tuple[str, Hypothesis]:
        """Return (sota_experiment_id, sota_hypothesis) for seeding hypotheses."""
        parent_id = self.tree.best_experiment_id()
        if parent_id is None:
            raise ValueError("cannot propose SEARCH hypotheses without a SOTA")
        parent_experiment = self.tree.get_experiment(parent_id)
        return parent_id, self.tree.get_hypothesis(parent_experiment.hypothesis_id)

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        parent_id, parent_hypothesis = self._sota_parent()
        hypothesis = Hypothesis.model_validate(
            {
                **payload,
                "parent_id": parent_id,
                "priority": self._scheduler.seed(parent_hypothesis),
            }
        )
        hypothesis_id = self.tree.add_hypothesis(hypothesis)
        self.tree.save(self._tree_path)
        await self._publish_state()
        return {"hypothesis_id": hypothesis_id}

    async def register_hypotheses(
        self, hypotheses: list[Hypothesis]
    ) -> dict[str, object]:
        """Register every Ideator hypothesis into the graph in one write.

        每个 Ideator 生成的假设都进入 graph（含多 lane 的近似重复候选），统一
        挂在当前 SOTA 下并播种优先级后单次保存/发布。去重只发生在排序/选择阶段
        （见 ``Scheduler._queued``），不在入图时丢弃任何结构有效的假设。
        """
        parent_id, parent_hypothesis = self._sota_parent()
        priority = self._scheduler.seed(parent_hypothesis)
        prepared = [
            hypothesis.model_copy(
                update={
                    "id": new_id("hyp"),
                    "order": None,
                    "parent_id": parent_id,
                    "priority": priority,
                    "rubric_score": None,
                    "rubric_ref": None,
                }
            )
            for hypothesis in hypotheses
        ]
        if prepared and self._run_hypothesis_rubric is not None:
            try:
                prepared = await self._run_hypothesis_rubric(prepared)
            except Exception as error:
                logger.warning(
                    "hypothesis rubric failed; using deterministic fallback",
                    exc_info=True,
                )
                await self._publish(
                    "output",
                    {
                        "source": "supervisor",
                        "channel": "error",
                        "text": (
                            "Hypothesis Rubric unavailable; Selector will use its "
                            f"deterministic fallback: {error}"
                        ),
                    },
                )
        hypothesis_ids = [self.tree.add_hypothesis(item) for item in prepared]
        self.tree.save(self._tree_path)
        await self._publish_state()
        return {"hypothesis_ids": hypothesis_ids}

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        self.tree.get_hypothesis(hypothesis_id)
        existing = self.tree.experiment_for_hypothesis(hypothesis_id)
        if existing is not None and self.tree.get_experiment(existing).status in {
            ExperimentStatus.SUCCEEDED,
            ExperimentStatus.FAILED,
        }:
            raise ValueError(f"hypothesis already settled: {hypothesis_id}")
        self._next_hypothesis_id = hypothesis_id
        if self.state.status == "WAITING":
            self.state.status = "RUNNING"
            self._save_state()
        self._wake.set()
        return {"selected": hypothesis_id}

    async def configure_search(self, **payload: object) -> dict[str, object]:
        search_limit = payload.get("search_limit")
        concurrency = payload.get("concurrency")
        if search_limit is not None:
            value = int(search_limit)
            if value < 1:
                raise ValueError("search_limit must be at least 1")
            self.state.search_limit = value
        if concurrency is not None:
            value = int(concurrency)
            if value < 1:
                raise ValueError("concurrency must be at least 1")
            self.state.concurrency = value
        # 交互模式下预算用尽停在 WAITING：追加预算即恢复 SEARCH。
        if self.state.phase == "SEARCH" and self.state.status == "WAITING":
            self.state.status = "RUNNING"
        await self._persist_state()
        self._spawn_search()
        return {
            "search_limit": self.state.search_limit,
            "concurrency": self.state.concurrency,
        }

    async def update_waiting_plan_budget(self, **payload: object) -> dict[str, object]:
        plan_id = str(payload.get("plan_id", ""))
        try:
            plan = self.state.plans[plan_id]
        except KeyError as exc:
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
        self.state.plans[plan_id] = plan.model_copy(update=updates)
        self.state.status = "RUNNING"
        await self._persist_state()
        self._spawn_search()
        return {"plan_id": plan_id, **updates}

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        if decision not in {"SEARCH", "VALIDATE"}:
            raise ValueError(f"invalid phase decision: {decision}")
        if decision == "VALIDATE":
            if self.tree.best_experiment_id() is None:
                raise ValueError(
                    "VALIDATE requires a trusted SOTA baseline from completed PREPARE"
                )
            if self.state.phase != "SEARCH":
                raise ValueError(
                    f"cannot VALIDATE from phase {self.state.phase}; run SEARCH first"
                )
        await self._transition_phase(decision)
        if decision == "SEARCH":
            self._spawn_search()
        elif decision == "VALIDATE":
            # 交互路径下 ``start()`` 的生命周期已结束（SEARCH 后停在 WAITING），
            # 单写者在此直接把 VALIDATE 阶段跑完，否则只改 phase 标志永远到不了 COMPLETED。
            await self._run_validation()
        return {"decision": decision}

    async def set_manual_mode(self, manual: bool) -> dict[str, object]:
        """Toggle SEARCH scheduling between auto (priority queue) and manual.

        Manual mode pauses after each hypothesis batch and waits for
        ``select_next_hypothesis``. Switching back to auto wakes the loop.
        """
        self.state.manual_mode = bool(manual)
        if self.state.status == "WAITING":
            self.state.status = "RUNNING"
        await self._persist_state()
        self._wake.set()
        return {"manual_mode": self.state.manual_mode}

    async def read_state(self) -> dict[str, object]:
        """Read-only snapshot of the current research phase and configuration."""
        return {
            "phase": self.state.phase,
            "status": self.state.status,
            "search_limit": self.state.search_limit,
            "concurrency": self.state.concurrency,
            "manual_mode": self.state.manual_mode,
            "validation_pending": self.state.validation is not None,
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
            for plan_id, plan in self.state.plans.items()
        ]
        return {"plans": plans, "running": list(self._running)}

    async def dispatch_general(self, task: str) -> dict[str, object]:
        """Dispatch one General Agent to do concrete work and return its result.

        第一次成功的 general 调研作为断点写入 state；同一任务后续调用直接返回
        缓存，避免续跑时重复派发 worker。不同任务不命中缓存，照常派发且不覆盖
        已保存的调研断点。
        """
        if self._run_general_turn is None:
            raise RuntimeError("General Agent dispatch is not configured")
        task = task.strip()
        if not task:
            raise ValueError("general task must be nonblank")
        owned = self.state.task_research_task == task
        ref = self.state.task_research_ref
        if ref is not None and owned:
            try:
                cached = json.loads(await self._store.get_text(ref))
            except (OSError, ValueError):
                # artifact 缺失或内容损坏 → 清掉引用并落盘，避免重启后反复撞坏缓存
                self.state.task_research_ref = None
                ref = None
                await self._persist_state()
                cached = None
            if isinstance(cached, dict) and cached:
                return {"cached": True, **cached}
        # 仅在"同一任务且尚无缓存"时复用旧 worker id；不同任务必须开新线程，
        # 否则新任务会混进调研线程记忆并劫持调研断点。
        prior_agent_id = (
            self.state.task_research_agent_id if ref is None and owned else None
        )
        outcome = await self._run_general_turn(task, prior_agent_id)
        if ref is None and self.state.task_research_task in (None, task):
            self.state.task_research_task = task
            self.state.task_research_agent_id = outcome.agent_id
            self.state.task_research_ref = await self._store.put_text(
                json.dumps(outcome.result, ensure_ascii=False)
            )
            await self._persist_state()
        return outcome.result


__all__ = [
    "GeneralTurn",
    "IdeatorTurn",
    "PlanTurn",
    "Publish",
    "Supervisor",
    "SupervisorTurn",
]
