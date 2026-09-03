"""Plan creation, workspace ownership, settlement, and recovery helpers."""

import json
import logging
from pathlib import Path

from athena.core.contracts import ArtifactRef
from athena.core.research_models import (
    ComparisonVerdict,
    EvalResult,
    ExperimentPlan,
    Hypothesis,
)
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.experiment import (
    load_best,
    read_eval_handoff,
)
from athena.research.supervisor.plans import PlanInput, PlanState
from athena.research.supervisor.policy import Outcome
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.statistics import (
    MetricEvidence,
    settle_statistically,
    two_sided_p_value,
)

logger = logging.getLogger(__name__)


def _compare_metric(
    candidate: float,
    reference: float,
    direction: str,
    tolerance: float,
) -> Outcome:
    delta = (
        candidate - reference
        if direction == "maximize"
        else reference - candidate
    )
    if delta > tolerance:
        return Outcome.WIN
    if delta < -tolerance:
        return Outcome.LOSS
    return Outcome.DRAW


def _status_for_outcome(outcome: Outcome | None) -> str:
    """Map a settlement outcome to a conservative hypothesis status.

    DRAW and missing evidence must never be reported as REFUTED: a tie is not
    evidence against the hypothesis, so it is INCONCLUSIVE.
    """
    if outcome is Outcome.WIN:
        return "SUPPORTED"
    if outcome is Outcome.LOSS:
        return "REFUTED"
    return "INCONCLUSIVE"


class PlanLifecycle:
    """Own the stable identity and durable lifecycle of SEARCH Plans."""

    def __init__(
        self, owner: object, deps: SupervisorDeps, run: SupervisorRunState
    ) -> None:
        self._owner = owner
        self._deps = deps
        self._run = run

    @property
    def _state(self):
        return self._owner.state

    @property
    def _tree(self):
        return self._owner.tree

    def _save_state(self) -> None:
        self._state.save(self._deps.state_path)

    async def _publish_state(self) -> None:
        await self._deps.publish(
            "state", {"type": "state", **self._state.model_dump(mode="json")}
        )

    async def _persist_state(self) -> None:
        """Save durable state and publish one snapshot to subscribers."""
        self._save_state()
        await self._publish_state()

    async def start_plan(self, hypothesis_id: str) -> str:
        """Freeze one Hypothesis input and create its stable execution identity."""
        if self._deps.evaluator_ref is None:
            baselines = self._tree.experiments(kind="baseline")
            if not baselines:
                raise RuntimeError("SEARCH Plan requires a frozen evaluator")
            self._deps.evaluator_ref = baselines[0].plan.run_config_ref
        if hypothesis_id in self._state.plans:
            if self._tree.experiment_for_hypothesis(hypothesis_id) is None:
                raise RuntimeError(
                    f"active plan {hypothesis_id} is missing experiment "
                    f"exp_{hypothesis_id}; remove the plan or repair the tree"
                )
            return hypothesis_id
        hypothesis = self._tree.get_hypothesis(hypothesis_id)
        existing = self._tree.experiment_for_hypothesis(hypothesis_id)
        if existing is not None and self._tree.get_experiment(existing).status in {
            ExperimentStatus.SUCCEEDED,
            ExperimentStatus.FAILED,
        }:
            raise ValueError(f"hypothesis already settled: {hypothesis_id}")

        reference_id = hypothesis.parent_id or self._tree.best_experiment_id()
        if reference_id is None:
            raise ValueError("SEARCH Plan requires a frozen reference experiment")
        reference = self._tree.get_experiment(reference_id)
        if reference.eval is None:
            raise ValueError("reference experiment requires a trusted metric")
        reference_hypothesis = self._tree.get_hypothesis(reference.hypothesis_id)
        active = self._tree.active_hypotheses(reference_id, hypothesis)[:-1]
        tree_ref = await self._deps.store.put_text(
            json.dumps(self._tree.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        guidance = self._run.take_guidance()
        plan_input = PlanInput(
            hypothesis=hypothesis,
            active_ancestor_hypotheses=active,
            reference_experiment_id=reference_id,
            reference_metric=reference.eval.primary,
            reference_priority=reference_hypothesis.priority,
            direction=self._deps.direction,
            tolerance=self._deps.tolerance,
            # 判胜阈值必须随搜索预算变严。预算 N 意味着要在 N 个候选里挑最大值，
            # 而最大值本身会随 N 增长：纯噪声（σ≈0.0074）下 N=4 的冠军期望
            # +0.0076，N=16 是 +0.0131，N=64 是 +0.0174——最后这个数已经和本次
            # 真机冠军实际拿到的 +0.0180 一样大。不做校正，加预算买到的只是
            # 更好看的数字。
            min_effect_size=self._deps.tolerance,
            family_size=max(1, self._state.search_limit),
            evaluator_ref=self._deps.evaluator_ref,
            tree_ref=tree_ref,
            eval_handoff=await read_eval_handoff(
                self._deps.store, self._deps.evaluator_ref
            ),
            human_context="\n".join(guidance),
            initial_turn_limit=hypothesis.turn_limit,
            initial_patience=hypothesis.patience,
        )
        context_ref = await self._deps.store.put_text(plan_input.model_dump_json())
        branch = await self._deps.workspaces.create(reference.commit, hypothesis_id)
        self._run.add_branch(hypothesis_id, branch)
        experiment_id = f"exp_{hypothesis_id}"
        self._tree.add_experiment(
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
        self._tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)
        self._state.plans[hypothesis_id] = PlanState(
            kind="SEARCH",
            context_ref=context_ref,
            turns_used=0,
            turn_limit=hypothesis.turn_limit,
            patience=hypothesis.patience,
        )
        self._save_state()
        # start_plan 与 settle_plan 之间可能隔很多个 turn/很久；若这里只保存
        # state.json 而不保存 research_tree.json，进程一旦在这段窗口内崩溃，
        # 恢复时 state.plans 仍在但 tree 缺少 exp_{hypothesis_id}，最终在
        # settle_plan 中抛 unknown experiment id。先落 state、再落 tree，并让
        # recover() 具备“state 有 Plan、tree 缺实验”时的重建能力。
        self._tree.save(self._deps.tree_path)
        await self._deps.agents.resume_agent(
            hypothesis_id, agent_type="plan", name=hypothesis_id
        )
        await self._publish_state()
        return hypothesis_id

    async def plan_input(self, plan_id: str) -> PlanInput:
        """Load the immutable input frozen for one active Plan."""
        return PlanInput.model_validate_json(
            await self._deps.store.get_text(self._state.plans[plan_id].context_ref)
        )

    def workspace_path(self, plan_id: str) -> Path:
        """Return the real worktree path bound to a stable Plan identity."""
        return Path(self._run.get_branch(plan_id).path)

    def workspace(self, plan_id: str) -> GitWorkBranch:
        """Return the branch owned by one active Plan."""
        return self._run.get_branch(plan_id)

    @staticmethod
    def plan_identity(plan_id: str) -> dict[str, str]:
        """Project the stable SEARCH identity shared by all owned resources."""
        return {
            "plan": plan_id,
            "agent": plan_id,
            "workspace": plan_id,
            "log": plan_id,
        }

    async def recover(self, state: ResearchState | None = None) -> ResearchState:
        """Reconcile persisted Plans without inventing missing frozen resources."""
        if self._deps.tree_path.is_file():
            self._owner.tree = ResearchTree.load(self._deps.tree_path)
        candidate = state or self._state
        artifact_presence: dict[str, bool] = {}
        for plan in candidate.plans.values():
            try:
                await self._deps.store.get_text(plan.context_ref)
            except Exception:
                artifact_presence[plan.context_ref] = False
            else:
                artifact_presence[plan.context_ref] = True
        workspace_presence: dict[str, bool] = {}
        repaired_experiments = False
        for plan_id, plan in candidate.plans.items():
            if not artifact_presence[plan.context_ref]:
                workspace_presence[plan_id] = False
                continue
            try:
                plan_input = PlanInput.model_validate_json(
                    await self._deps.store.get_text(plan.context_ref)
                )
                if plan_input.reference_experiment_id is None:
                    raise ValueError("Plan input has no reference experiment")
                reference = self._tree.get_experiment(
                    plan_input.reference_experiment_id
                )
                branch = await self._deps.workspaces.create(reference.commit, plan_id)
            except Exception:
                workspace_presence[plan_id] = False
            else:
                self._run.add_branch(plan_id, branch)
                workspace_presence[plan_id] = Path(branch.path).is_dir()
                if (
                    plan.kind == "SEARCH"
                    and workspace_presence[plan_id]
                    and self._repair_missing_experiment(
                        plan_id, plan.context_ref, plan_input, branch
                    )
                ):
                    repaired_experiments = True
        if repaired_experiments:
            self._tree.save(self._deps.tree_path)
        self._owner.state = self._deps.recovery.reconcile(
            candidate,
            self._tree,
            workspace_exists=lambda plan_id: workspace_presence.get(plan_id, False),
            artifact_exists=lambda ref: artifact_presence.get(ref, False),
        )
        for plan_id in self._state.plans:
            if workspace_presence.get(plan_id):
                await self._deps.agents.resume_agent(
                    plan_id, agent_type="plan", name=plan_id
                )
        await self._persist_state()
        return self._state

    def _repair_missing_experiment(
        self,
        plan_id: str,
        context_ref: str,
        plan_input: PlanInput,
        branch: GitWorkBranch,
    ) -> bool:
        """Recreate a RUNNING search experiment lost in the state-saved/tree-not-saved window.

        Returns True when the experiment was added to the in-memory tree and should be
        persisted by the caller. If the hypothesis/reference cannot be found the orphan
        Plan is left for ``Recovery.reconcile`` to drop.
        """
        if self._tree.experiment_for_hypothesis(plan_id) is not None:
            return False
        try:
            hypothesis = self._tree.get_hypothesis(plan_id)
            reference = self._tree.get_experiment(plan_input.reference_experiment_id)
        except KeyError as exc:
            logger.warning("cannot repair missing experiment exp_%s: %s", plan_id, exc)
            return False
        experiment_id = f"exp_{plan_id}"
        try:
            self._tree.add_experiment(
                experiment_id,
                Experiment(
                    parent_id=hypothesis.parent_id,
                    hypothesis_id=plan_id,
                    commit=reference.commit,
                    plan=ExperimentPlan(
                        kind="search",
                        change=hypothesis.intervention,
                        run_config_ref=context_ref,
                        budget={},
                        acceptance_rule="trusted score",
                    ),
                    gitwork=branch,
                    status=ExperimentStatus.RUNNING,
                ),
            )
        except (KeyError, ValueError) as exc:
            logger.warning("cannot repair missing experiment exp_%s: %s", plan_id, exc)
            return False
        return True

    async def _settle_plan(
        self,
        plan_id: str,
        best_ref: ArtifactRef | None,
        result,
    ) -> None:
        """Persist one final Experiment before removing the active Plan."""
        plan_input = await self.plan_input(plan_id)
        hypothesis = self._tree.get_hypothesis(plan_id)
        experiment_id = f"exp_{plan_id}"
        primary: float | None = None
        outcome: Outcome | None = None
        if best_ref is None:
            self._tree.transition_experiment(
                experiment_id,
                ExperimentStatus.FAILED,
                error="settled without a trusted result",
            )
        else:
            best = await load_best(best_ref, self._deps.store)
            comparison: ComparisonVerdict | None = None
            reference = plan_input.reference_metric
            if reference is None:
                outcome = None
            elif best.std_error is not None:
                verdict = settle_statistically(
                    MetricEvidence(
                        metric=best.metric,
                        std_error=best.std_error,
                        n=best.n,
                    ),
                    reference,
                    direction=plan_input.direction,
                    min_effect_size=plan_input.min_effect_size,
                    alpha=plan_input.alpha,
                    family_size=plan_input.family_size,
                )
                outcome = {
                    "SUPPORTED": Outcome.WIN,
                    "REFUTED": Outcome.LOSS,
                }.get(verdict)  # INCONCLUSIVE -> None
                p_value = two_sided_p_value(best.metric, reference, best.std_error)
                if p_value is not None:
                    comparison = ComparisonVerdict(
                        winner={
                            "SUPPORTED": "candidate",
                            "REFUTED": "baseline",
                        }.get(verdict, "tie"),
                        p_value=p_value,
                    )
            else:
                outcome = _compare_metric(
                    best.metric,
                    reference,
                    plan_input.direction,
                    plan_input.tolerance,
                )
            evidence_ref = best.evidence_ref
            artifacts = {"evidence": evidence_ref}
            try:
                evidence = json.loads(await self._deps.store.get_text(evidence_ref))
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
            self._tree.complete_experiment(
                experiment_id,
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=best.metric,
                    per_sample=evidence_ref,
                ),
                verdict=comparison,
                artifacts=artifacts,
                commit=best.commit,
            )
        if outcome is None:
            # 实验无有效证据：标记 INCONCLUSIVE，不按胜负更新评级。
            self._tree.update_hypothesis_status(plan_id, "INCONCLUSIVE")
        else:
            hypothesis.priority = self._deps.scheduler.settle(
                plan_input.reference_priority, outcome
            )
            self._tree.update_hypothesis_status(
                plan_id, _status_for_outcome(outcome)
            )
        if primary is not None:
            sota_id = self._tree.best_experiment_id()
            if sota_id is None:
                self._tree.set_sota(experiment_id)
            else:
                sota = self._tree.get_experiment(sota_id)
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
                    # SOTA 指针是**操作性**的："下一条假设从哪儿分叉"，用点估计
                    # 选最有希望的那个是对的。但它不是科学结论，而下游（报告、
                    # VALIDATE、人）会把"SOTA 迁移了"读成"找到了改进"。
                    #
                    # 真机 2026-08-30：hyp_a058326db0e8 的区间判据在 family_size=1
                    # 时就已经是 INCONCLUSIVE（0.8716 ± 1.96×0.0200 覆盖了基线
                    # 0.8535），SOTA 指针仍然迁了过去，并且没有任何一处记下这个
                    # 分歧。留出集后来证实：ΔPR-AUC 的 95% 区间含 0。
                    if comparison is not None and comparison.winner != "candidate":
                        logger.warning(
                            "SOTA moved to %s on a point estimate (%.4f vs %.4f) "
                            "while the interval verdict was %s (p=%.3f, "
                            "family_size=%d). The pointer is operational, not a "
                            "demonstrated improvement.",
                            experiment_id,
                            primary,
                            sota.eval.primary if sota.eval else float("nan"),
                            comparison.winner,
                            comparison.p_value,
                            plan_input.family_size,
                        )
                    self._tree.set_sota(experiment_id)
        self._tree.save(self._deps.tree_path)
        self._state.plans.pop(plan_id)
        self._save_state()
        if self._deps.on_plan_settled is not None:
            await self._deps.on_plan_settled(plan_id)
        # A settled Plan owns a stable PlanAgent thread. Reap it now so finished
        # SEARCH plans do not accumulate for the rest of the process lifetime.
        try:
            await self._deps.agents.reap(plan_id)
        except Exception:
            logger.warning("failed to reap settled Plan agent %s", plan_id, exc_info=True)

    def _sota_parent(self) -> tuple[str, Hypothesis]:
        """Return (sota_experiment_id, sota_hypothesis) for seeding hypotheses.

        The refusal has to say *who* establishes the SOTA, not just that one is
        missing. On 2026-09-02 the Supervisor hit the old one-line message during
        the task-understanding turn, concluded it had to fix the problem itself,
        and dispatched a General Agent to "build and validate a SOTA baseline".
        That agent's shell is not sandboxed: it walked to a *different* project's
        directory, trained on its 43,051-row split instead of this task's 507,789
        rows, and reported PR-AUC 0.8668. The Supervisor believed it, and the run
        went to SEARCH with no platform split, no frozen evaluator and no EDA.

        PREPARE owns the baseline precisely so that it is built against the
        frozen evaluator and the platform's own split. Anything built beside that
        is not comparable to what SEARCH will score.
        """
        parent_id = self._tree.best_experiment_id()
        if parent_id is None:
            raise ValueError(
                "cannot propose SEARCH hypotheses without a SOTA. The PREPARE "
                "phase establishes it automatically, against the frozen "
                "evaluator and the platform's own train/search/final split -- "
                "it has not finished yet. Do NOT build a baseline yourself and "
                "do NOT dispatch an agent to build one: a model trained on any "
                "other data or scored by any other evaluator is not comparable "
                "to what SEARCH measures. End this turn; propose hypotheses "
                "once PREPARE reports its trusted metric."
            )
        parent_experiment = self._tree.get_experiment(parent_id)
        return parent_id, self._tree.get_hypothesis(parent_experiment.hypothesis_id)

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        parent_id, parent_hypothesis = self._sota_parent()
        hypothesis = Hypothesis.model_validate(
            {
                **payload,
                "parent_id": parent_id,
                "priority": self._deps.scheduler.seed(parent_hypothesis),
            }
        )
        hypothesis_id = self._tree.add_hypothesis(hypothesis)
        self._tree.save(self._deps.tree_path)
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
        priority = self._deps.scheduler.seed(parent_hypothesis)
        hypothesis_ids = [
            self._tree.add_hypothesis(
                hypothesis.model_copy(
                    update={
                        "id": None,
                        "order": None,
                        "parent_id": parent_id,
                        "priority": priority,
                    }
                )
            )
            for hypothesis in hypotheses
        ]
        self._tree.save(self._deps.tree_path)
        await self._publish_state()
        return {"hypothesis_ids": hypothesis_ids}

    async def checkpoint_evaluator(self, ref: ArtifactRef) -> dict[str, object]:
        """Persist one frozen evaluator bundle so PREPARE resumes past evaluator."""
        self._deps.evaluator_ref = ref
        self._state.evaluator_ref = ref
        await self._persist_state()
        return {"evaluator_ref": ref}

    async def checkpoint_final_evaluator(
        self, ref: ArtifactRef
    ) -> dict[str, object]:
        """Persist the hidden final-test evaluator used only by VALIDATE."""
        self._deps.final_evaluator_ref = ref
        self._state.final_evaluator_ref = ref
        await self._persist_state()
        return {"final_evaluator_ref": ref}

    async def dispatch_general(self, task: str) -> dict[str, object]:
        """Dispatch one General Agent to do concrete work and return its result.

        第一次成功的 general 调研作为断点写入 state；同一任务后续调用直接返回
        缓存，避免续跑时重复派发 worker。不同任务不命中缓存，照常派发且不覆盖
        已保存的调研断点。
        """
        if self._deps.run_general_turn is None:
            raise RuntimeError("General Agent dispatch is not configured")
        task = task.strip()
        if not task:
            raise ValueError("general task must be nonblank")
        owned = self._state.task_research_task == task
        ref = self._state.task_research_ref
        if ref is not None and owned:
            try:
                cached = json.loads(await self._deps.store.get_text(ref))
            except (OSError, ValueError):
                # artifact 缺失或内容损坏 → 清掉引用并落盘，避免重启后反复撞坏缓存
                self._state.task_research_ref = None
                ref = None
                await self._persist_state()
                cached = None
            if isinstance(cached, dict) and cached:
                return {"cached": True, **cached}
        # 仅在"同一任务且尚无缓存"时复用旧 worker id；不同任务必须开新线程，
        # 否则新任务会混进调研线程记忆并劫持调研断点。
        prior_agent_id = (
            self._state.task_research_agent_id if ref is None and owned else None
        )
        outcome = await self._deps.run_general_turn(task, prior_agent_id)
        if ref is None and self._state.task_research_task in (None, task):
            self._state.task_research_task = task
            self._state.task_research_agent_id = outcome.agent_id
            self._state.task_research_ref = await self._deps.store.put_text(
                json.dumps(outcome.result, ensure_ascii=False)
            )
            await self._persist_state()
        return outcome.result


__all__ = ["PlanLifecycle"]
