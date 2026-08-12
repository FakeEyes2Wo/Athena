"""Single-writer orchestration for autonomous SEARCH Plans."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from athena.agents.supervisor_agent import MAX_PLAN_TURNS, SupervisorActions
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.agent.types import AgentCommandError, ErrorCode, RunStatus
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch, GitWorkspace
from athena.research.supervisor.plans import wait_run_events
from athena.research.supervisor.experiment import (
    PlanTurnResult,
    decide_settlement,
    load_best,
)
from athena.research.supervisor.plans import PlanDecision, PlanInput, PlanState
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.policy import Outcome
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import ScheduleKind, Scheduler
from athena.research.supervisor.state import ResearchState

PlanTurn = Callable[[str, PlanState], Awaitable[PlanTurnResult]]
SupervisorTurn = Callable[[str], Awaitable[str]]
Publish = Callable[[Literal["output", "state"], dict[str, object]], Awaitable[None]]
PreparePhase = Callable[[], Awaitable[PrepareResult]]
ValidationPhase = Callable[[CommitHash, float], Awaitable[object]]
IdeatorTurn = Callable[[int], Awaitable[list[Hypothesis]]]
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
        direction: Literal["maximize", "minimize"] = "maximize",
        tolerance: float = 0.0,
        run_prepare_phase: PreparePhase | None = None,
        run_validation_phase: ValidationPhase | None = None,
        publish_agent_event: PublishAgentEvent | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._state_path = self._project_root / ".athena" / "state.json"
        self._tree_path = self._project_root / ".athena" / "research_tree.json"
        self.state = state
        self.tree = tree
        self._store = store
        self._agents = agents
        self._workspaces = workspaces
        self._scheduler = scheduler
        self._recovery = recovery
        self._evaluator_ref = evaluator_ref
        self._direction = direction
        self._tolerance = tolerance
        self._run_plan_turn = run_plan_turn
        self._run_supervisor_turn = run_supervisor_turn
        self._run_ideator_turn = run_ideator_turn
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
        if self.tree.experiment_for_hypothesis(hypothesis_id) is not None:
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

    async def start(self) -> None:
        """Start the Supervisor lifecycle."""
        self._stopped = False
        if self.state.phase == "PREPARE":
            await self._run_prepare()
        else:
            await self.recover()
        if self.state.phase == "SEARCH":
            await self.run_search()
            if self._search_limit_reached():
                if self._auto_validate:
                    await self._transition_phase("VALIDATE")
                elif self.state.status == "RUNNING":
                    self.state.status = "WAITING"
                    self._save_state()
                    await self._publish_state()
        if self.state.phase == "VALIDATE":
            await self._run_validation()

    async def _run_prepare(self) -> None:
        if self._run_prepare_phase is None:
            raise RuntimeError("PREPARE phase adapter is not configured")
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
        self.state.validation = (
            result.model_dump(mode="json")
            if hasattr(result, "model_dump")
            else dict(result)
        )
        self.state.phase = "COMPLETED"
        self.state.status = "COMPLETED"
        self._save_state()
        await self._publish(
            "output",
            {
                "source": "supervisor",
                "channel": "text",
                "text": "VALIDATE completed.",
            },
        )
        await self._publish_state()

    async def _transition_phase(self, phase: Literal["SEARCH", "VALIDATE"]) -> None:
        self.state.phase = phase
        self.state.status = "RUNNING"
        self._save_state()
        await self._publish_state()

    async def checkpoint_validation(self, result_ref: ArtifactRef) -> None:
        """Persist one recoverable VALIDATE result through the single writer."""
        self.state.validation = {"result_ref": result_ref}
        self._save_state()
        await self._publish_state()

    def _search_limit_reached(self) -> bool:
        attempts = len(self.tree.experiments(kind="search")) + sum(
            plan.kind == "SEARCH" for plan in self.state.plans.values()
        )
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
        self._save_state()
        await self._publish_state()
        return self.state

    async def run_search(self) -> None:
        """Run rolling SEARCH scheduling."""
        self._stopped = False
        while not self._stopped:
            generated = await self._fill_slots()
            if not self._running:
                if generated:
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
                    await self.register_hypotheses(hypotheses)
                    generated = len(hypotheses) > 0
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
        self._save_state()
        await self._publish_state()
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
            if (
                summary.status is not RunStatus.COMPLETED
                or summary.response_ref is None
            ):
                return _CompletedTurn(plan_id, None, None)
            response = json.loads(summary.response_ref)
            decision_ref = response.get("result_ref")
            if not isinstance(decision_ref, str):
                return _CompletedTurn(plan_id, None, None)
            decision = PlanDecision.model_validate_json(
                await self._store.get_text(decision_ref)
            )
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
            self._save_state()
            await self._publish_state()
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
        branch = self._branches[plan_id]
        experiment_id = f"exp_{plan_id}"
        if best_ref is None:
            experiment = Experiment(
                parent_id=hypothesis.parent_id,
                hypothesis_id=plan_id,
                commit=branch.base_commit,
                plan=ExperimentPlan(
                    kind="search",
                    change=hypothesis.intervention,
                    run_config_ref=self.state.plans[plan_id].context_ref,
                    budget={},
                    acceptance_rule="trusted score",
                ),
                gitwork=branch,
                status=ExperimentStatus.FAILED,
                error="settled without a trusted result",
            )
            outcome = Outcome.LOSS
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
            experiment = Experiment(
                parent_id=hypothesis.parent_id,
                hypothesis_id=plan_id,
                commit=best.commit,
                plan=ExperimentPlan(
                    kind="search",
                    change=hypothesis.intervention,
                    run_config_ref=self.state.plans[plan_id].context_ref,
                    budget={},
                    acceptance_rule="trusted score",
                ),
                gitwork=branch,
                status=ExperimentStatus.SUCCEEDED,
                eval=EvalResult(
                    experiment_id=experiment_id,
                    primary=best.metric,
                    per_sample=evidence_ref,
                ),
                artifacts=artifacts,
            )
        self.tree.add_experiment(experiment_id, experiment)
        hypothesis.priority = self._scheduler.settle(
            plan_input.reference_priority, outcome
        )
        self.tree.update_hypothesis_status(
            plan_id, "SUPPORTED" if outcome is Outcome.WIN else "REFUTED"
        )
        if experiment.status is ExperimentStatus.SUCCEEDED:
            sota_id = self.tree.best_experiment_id()
            if sota_id is None:
                self.tree.set_sota(experiment_id)
            else:
                sota = self.tree.get_experiment(sota_id)
                if (
                    sota.eval is None
                    or _compare_metric(
                        experiment.eval.primary,
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
        self._save_state()
        await self._publish_state()
        return self.state.status

    async def resume(self) -> str:
        """Resume Agent dispatch after an explicit Human command."""
        self._agents.resume()
        self.state.status = "RUNNING"
        self._save_state()
        await self._publish_state()
        return self.state.status

    async def request_stop(self) -> str:
        """Persist an explicit Human stop and cancel locally running Plans."""
        await self.stop()
        self.state.status = "STOPPED"
        self._save_state()
        await self._publish_state()
        return self.state.status

    async def stop(self) -> None:
        """Stop scheduling and interrupt every locally owned Plan turn."""
        self._stopped = True
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

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        parent_id = self.tree.best_experiment_id()
        if parent_id is None:
            raise ValueError("cannot propose SEARCH hypothesis without a SOTA")
        parent_experiment = self.tree.get_experiment(parent_id)
        parent_hypothesis = self.tree.get_hypothesis(parent_experiment.hypothesis_id)
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
        """Register one Ideator's HypothesisBatch in one atomic write.

        批量登记：统一挂在当前 SOTA 下并播种优先级，单次保存/发布。
        """
        parent_id = self.tree.best_experiment_id()
        if parent_id is None:
            raise ValueError("cannot register SEARCH hypotheses without a SOTA")
        parent_experiment = self.tree.get_experiment(parent_id)
        parent_hypothesis = self.tree.get_hypothesis(parent_experiment.hypothesis_id)
        hypothesis_ids: list[str] = []
        for hypothesis in hypotheses:
            payload = hypothesis.model_dump()
            payload.update(
                {
                    "id": None,
                    "parent_id": parent_id,
                    "priority": self._scheduler.seed(parent_hypothesis),
                }
            )
            hypothesis_ids.append(
                self.tree.add_hypothesis(Hypothesis.model_validate(payload))
            )
        self.tree.save(self._tree_path)
        await self._publish_state()
        return {"hypothesis_ids": hypothesis_ids}

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        self.tree.get_hypothesis(hypothesis_id)
        if self.tree.experiment_for_hypothesis(hypothesis_id) is not None:
            raise ValueError(f"hypothesis already settled: {hypothesis_id}")
        self._next_hypothesis_id = hypothesis_id
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
        self._save_state()
        await self._publish_state()
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
        self._save_state()
        await self._publish_state()
        return {"plan_id": plan_id, **updates}

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        if decision not in {"SEARCH", "VALIDATE"}:
            raise ValueError(f"invalid phase decision: {decision}")
        await self._transition_phase(decision)
        return {"decision": decision}


__all__ = ["IdeatorTurn", "PlanTurn", "Publish", "Supervisor", "SupervisorTurn"]
