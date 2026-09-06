"""Single owner for durable Plan execution, recovery, and settlement."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from athena.core.contracts import ArtifactRef
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch, GitWorkspaceError
from athena.research.clarification.context import (
    ConfirmedTaskContextError,
    ConfirmedTaskContextProvider,
)
from athena.research.evaluation.spec import read_eval_handoff
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.events import wait_run_events
from athena.research.supervisor.experiment import PlanTurnResult, load_agent_result
from athena.research.supervisor.plans import (
    PlanDecision,
    PlanFailure,
    PlanInput,
    PlanState,
)
from athena.research.supervisor.prompt_context import (
    data_contract_block,
    handoff_block,
    hypothesis_block,
)
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.settlement import PlanSettlement
from athena.research.supervisor.state import ResearchState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompletedPlanTurn:
    """One Agent decision and its optional trusted experiment result."""

    plan_id: str
    decision: PlanDecision | None
    result: PlanTurnResult | None
    failure: PlanFailure | None = None


class PlanRuntime:
    """Own Plan inputs, workspaces, turns, hypotheses, settlement, and recovery."""

    def __init__(
        self,
        owner: object,
        deps: SupervisorDeps,
        run: SupervisorRunState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self._run = run
        self._settlement = PlanSettlement(
            owner,
            deps,
            plan_input=self.plan_input,
            save_state=self.save_state,
        )

    @property
    def _state(self):
        return self._owner.state

    @property
    def _tree(self):
        return self._owner.tree

    def save_state(self) -> None:
        """Save the current durable state checkpoint."""
        self._state.save(self._deps.paths.state_path)

    async def publish_state(self) -> None:
        """Publish the current durable state snapshot."""
        await self._deps.phases.publish(
            "state", {"type": "state", **self._state.model_dump(mode="json")}
        )

    async def persist_state(self) -> None:
        """Save durable state and publish one snapshot to subscribers."""
        self.save_state()
        await self.publish_state()

    @staticmethod
    def _previous_failure_block(summary: str | None) -> str:
        failure = PlanFailure.from_summary(summary)
        return failure.to_prompt_block() if failure is not None else ""

    def _corpus_block(self, plan_input: PlanInput) -> str:
        """Render corpus pointers named by the Plan's frozen hypothesis."""
        corpus_ref = getattr(self._state, "corpus_ref", None)
        hypothesis = plan_input.hypothesis
        if corpus_ref is None or hypothesis is None or not hypothesis.sources:
            return ""
        return (
            f"\n\nThe literature corpus for this task is available "
            f"(corpus_ref={corpus_ref!r}). This hypothesis was formed from: "
            f"{', '.join(hypothesis.sources)}. Use paper_search and "
            "paper_chunk_read on those papers for the implementation details the "
            "hypothesis leaves open — exact loss formulation, hyper-parameter "
            "ranges, preprocessing. Follow what they report; do not invent "
            "numbers they do not give."
        )

    def _turn_prompt(
        self,
        plan_id: str,
        state: PlanState,
        plan_input: PlanInput,
        previous_failure: str | None,
    ) -> str:
        """Assemble the complete model-visible prompt for one Plan turn."""
        hypothesis = plan_input.hypothesis
        hypothesis_context = (
            hypothesis_block(
                hypothesis.statement,
                hypothesis.intervention,
                hypothesis.expected_effect,
            )
            if hypothesis is not None
            else ""
        )
        return (
            f"Continue Plan {plan_id}. Turns used: {state.turns_used}; "
            f"turn limit: {state.turn_limit}; patience: {state.patience}; "
            f"stale rounds: {state.stale_rounds}."
            + (f"\n\n{plan_input.task_context}" if plan_input.task_context else "")
            + (
                f"\n\nHuman guidance:\n{plan_input.human_context}"
                if plan_input.human_context
                else ""
            )
            + hypothesis_context
            + handoff_block(plan_input.eval_handoff)
            + self._corpus_block(plan_input)
            + data_contract_block(self._state.data_contract or "")
            + self._previous_failure_block(previous_failure)
        )

    async def _confirmed_task_context(self, state: ResearchState | None = None) -> str:
        """Load the authoritative task snapshot for a new or legacy Plan."""
        return await ConfirmedTaskContextProvider(
            state or self._state,
            self._deps.runtime.store,
            self._deps.paths.state_path.parent,
        ).load()

    async def run_turn(self, plan_id: str) -> CompletedPlanTurn:
        """Run one Plan Agent turn and persist failures for the next prompt."""
        state = self._state.plans[plan_id]
        previous_failure = state.last_failure
        state = state.model_copy(
            update={"turns_used": state.turns_used + 1, "last_failure": None}
        )
        self._state.plans[plan_id] = state
        await self.persist_state()
        try:
            plan_input = await self.plan_input(plan_id)
            run_id = await self._deps.runtime.agents.followup(
                plan_id,
                {
                    "content": self._turn_prompt(
                        plan_id, state, plan_input, previous_failure
                    ),
                    "context_refs": [state.context_ref],
                },
            )
            publish = self._deps.research.publish_agent_event
            if publish is None:
                summary = await self._deps.runtime.agents.wait_run(run_id)
            else:
                summary = await wait_run_events(
                    self._deps.runtime.agents,
                    run_id,
                    lambda kind, ref, data: publish(plan_id, kind, ref, data),
                )
            decision = await load_agent_result(
                summary, self._deps.runtime.store, PlanDecision
            )
            if decision is None:
                return CompletedPlanTurn(plan_id, None, None)
        except Exception as exc:  # noqa: BLE001 - normalize external turn boundary
            failure = PlanFailure(
                kind="turn_infrastructure_failed",
                detail=f"{type(exc).__name__}: {' '.join(str(exc).split())}",
            )
            await self._record_failure(plan_id, failure)
            return CompletedPlanTurn(plan_id, None, None, failure)
        if decision.decision == "abandon" and state.best_ref is None:
            return CompletedPlanTurn(plan_id, decision, None)
        if plan_id not in self._state.plans:
            return CompletedPlanTurn(plan_id, None, None)
        result = await self._deps.research.plan(plan_id, state)
        await self._record_turn_failure(plan_id, result)
        return CompletedPlanTurn(plan_id, decision, result)

    async def _record_turn_failure(self, plan_id: str, result: PlanTurnResult) -> None:
        """Persist a failed turn's reason for the immediately following prompt."""
        if result.error:
            await self._record_failure(
                plan_id,
                PlanFailure(kind=result.kind, detail=result.error),
            )

    async def _record_failure(self, plan_id: str, failure: PlanFailure) -> None:
        """Persist one normalized Plan failure diagnostic."""
        current = self._state.plans.get(plan_id)
        if current is None:
            return
        self._state.plans[plan_id] = current.model_copy(
            update={"last_failure": failure.to_summary()[:1200]}
        )
        await self.persist_state()

    async def start_plan(self, hypothesis_id: str) -> str:
        """Freeze one Hypothesis input and create its stable execution identity."""
        evaluator_ref = self._state.evaluator_ref
        if evaluator_ref is None:
            baselines = self._tree.experiments(kind="baseline")
            if not baselines:
                raise RuntimeError("SEARCH Plan requires a frozen evaluator")
            evaluator_ref = baselines[0].plan.run_config_ref
            self._state.evaluator_ref = evaluator_ref
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
            ExperimentStatus.CANCELLED,
        }:
            raise ValueError(f"hypothesis already settled: {hypothesis_id}")
        reference_id = hypothesis.parent_id or self._tree.best_experiment_id()
        if reference_id is None:
            raise ValueError("SEARCH Plan requires a frozen reference experiment")
        reference = self._tree.get_experiment(reference_id)
        if reference.eval is None:
            raise ValueError("reference experiment requires a trusted metric")
        reference_hypothesis = self._tree.get_hypothesis(reference.hypothesis_id)
        tree_ref = await self._deps.runtime.store.put_text(
            json.dumps(self._tree.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        guidance = self._run.take_guidance()
        task_context = await self._confirmed_task_context()
        plan_input = PlanInput(
            hypothesis=hypothesis,
            reference_experiment_id=reference_id,
            reference_metric=reference.eval.primary,
            reference_priority=reference_hypothesis.priority,
            direction=self._deps.search.direction,
            tolerance=self._deps.search.tolerance,
            min_effect_size=self._deps.search.tolerance,
            family_size=max(1, self._state.search_limit),
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            eval_handoff=await read_eval_handoff(
                self._deps.runtime.store, evaluator_ref
            ),
            human_context="\n".join(guidance),
            task_context=task_context,
        )
        context_ref = await self._deps.runtime.store.put_text(
            plan_input.model_dump_json()
        )
        branch = await self._deps.runtime.workspaces.create(
            reference.commit, hypothesis_id
        )
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
        self.save_state()
        self._tree.save(self._deps.paths.tree_path)
        await self._deps.runtime.agents.resume_agent(
            hypothesis_id, agent_type="plan", name=hypothesis_id
        )
        await self.publish_state()
        return hypothesis_id

    async def plan_input(self, plan_id: str) -> PlanInput:
        """Load the immutable input frozen for one active Plan."""
        return PlanInput.model_validate_json(
            await self._deps.runtime.store.get_text(
                self._state.plans[plan_id].context_ref
            )
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
        return {"plan": plan_id, "agent": plan_id, "workspace": plan_id, "log": plan_id}

    async def recover(self, state: ResearchState | None = None) -> ResearchState:
        """Reconcile persisted Plans without inventing missing frozen resources."""
        if self._deps.paths.tree_path.is_file():
            self._owner.tree.replace_with(ResearchTree.load(self._deps.paths.tree_path))
        candidate = state or self._state
        artifact_presence: dict[str, bool] = {}
        workspace_presence: dict[str, bool] = {}
        tree_changed = False
        for plan_id, original_plan in list(candidate.plans.items()):
            plan = original_plan
            try:
                context_json = await self._deps.runtime.store.get_text(plan.context_ref)
            except (OSError, ValueError):
                artifact_presence[plan.context_ref] = False
                workspace_presence[plan_id] = False
                continue
            artifact_presence[plan.context_ref] = True
            try:
                plan_input = PlanInput.model_validate_json(context_json)
                if plan.kind == "SEARCH" and not plan_input.task_context.strip():
                    task_context = await self._confirmed_task_context(candidate)
                    migrated_ref = await self._deps.runtime.store.put_text(
                        plan_input.model_copy(
                            update={"task_context": task_context}
                        ).model_dump_json()
                    )
                    plan = plan.model_copy(update={"context_ref": migrated_ref})
                    candidate.plans[plan_id] = plan
                    artifact_presence[migrated_ref] = True
                    plan_input = PlanInput.model_validate_json(
                        await self._deps.runtime.store.get_text(migrated_ref)
                    )
                    experiment_id = self._tree.experiment_for_hypothesis(plan_id)
                    if experiment_id is not None:
                        experiment = self._tree.get_experiment(experiment_id)
                        experiment.plan = experiment.plan.model_copy(
                            update={"run_config_ref": migrated_ref}
                        )
                        tree_changed = True
                if plan_input.reference_experiment_id is None:
                    raise ValueError("Plan input has no reference experiment")
                reference = self._tree.get_experiment(
                    plan_input.reference_experiment_id
                )
                branch = await self._deps.runtime.workspaces.create(
                    reference.commit, plan_id
                )
            except ConfirmedTaskContextError as exc:
                logger.warning(
                    "parking legacy Plan %s without confirmed task context: %s",
                    plan_id,
                    exc,
                )
                workspace_presence[plan_id] = False
            except (GitWorkspaceError, KeyError, OSError, ValueError):
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
                    tree_changed = True
        if tree_changed:
            self._tree.save(self._deps.paths.tree_path)
        recovered = self._deps.search.recovery(
            candidate,
            self._tree,
            workspace_exists=lambda plan_id: workspace_presence.get(plan_id, False),
            artifact_exists=lambda ref: artifact_presence.get(ref, False),
        )
        # ResearchRuntime and Supervisor begin with the same durable state object.
        # Keep that identity across recovery so public callers, event projection,
        # and phase mutation continue to observe one authoritative snapshot.
        current = self._state
        for field_name in ResearchState.model_fields:
            setattr(current, field_name, getattr(recovered, field_name))
        for plan_id in self._state.plans:
            if workspace_presence.get(plan_id):
                await self._deps.runtime.agents.resume_agent(
                    plan_id, agent_type="plan", name=plan_id
                )
        await self.persist_state()
        return self._state

    def _repair_missing_experiment(
        self,
        plan_id: str,
        context_ref: str,
        plan_input: PlanInput,
        branch: GitWorkBranch,
    ) -> bool:
        """Repair a RUNNING search experiment lost between state and tree saves."""
        if self._tree.experiment_for_hypothesis(plan_id) is not None:
            return False
        try:
            hypothesis = self._tree.get_hypothesis(plan_id)
            reference = self._tree.get_experiment(plan_input.reference_experiment_id)
        except KeyError as exc:
            logger.warning("cannot repair missing experiment exp_%s: %s", plan_id, exc)
            return False
        try:
            self._tree.add_experiment(
                f"exp_{plan_id}",
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

    async def settle_plan(
        self,
        plan_id: str,
        best_ref: ArtifactRef | None,
        result: PlanTurnResult | None,
    ) -> None:
        """Settle one trusted result through the settlement boundary."""
        await self._settlement.settle_plan(plan_id, best_ref, result)

    def _sota_parent(self) -> tuple[str, Hypothesis]:
        """Return the current SOTA experiment and its hypothesis.

        The refusal names the owner of the baseline, because the Supervisor is a
        model and a one-line "without a SOTA" reads as a problem it should solve
        itself. On 2026-09-02 it hit exactly that during the task-understanding
        turn, concluded it had to fix it, and dispatched a General Agent to
        "build and validate a SOTA baseline". That agent's shell is not
        sandboxed: it walked into a *different* project's directory, trained on
        its 43,051-row split instead of this task's 507,789 rows, and reported
        PR-AUC 0.8668. The Supervisor believed it, and the run went to SEARCH
        with no platform split, no frozen evaluator and no EDA.

        PREPARE owns the baseline precisely so that it is built against the
        frozen evaluator and the platform's own split. Anything built beside
        that is not comparable to what SEARCH will score.
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
        parent = self._tree.get_experiment(parent_id)
        return parent_id, self._tree.get_hypothesis(parent.hypothesis_id)

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        """Register one hypothesis derived from the current SOTA experiment."""
        parent_id, parent_hypothesis = self._sota_parent()
        hypothesis = Hypothesis.model_validate(
            {
                **payload,
                "parent_id": parent_id,
                "priority": self._deps.search.scheduler.seed(parent_hypothesis),
            }
        )
        hypothesis_id = self._tree.add_hypothesis(hypothesis)
        self._tree.save(self._deps.paths.tree_path)
        await self.publish_state()
        return {"hypothesis_id": hypothesis_id}

    async def register_hypotheses(
        self, hypotheses: list[Hypothesis]
    ) -> dict[str, object]:
        """Register one Ideator batch under the current SOTA."""
        parent_id, parent_hypothesis = self._sota_parent()
        priority = self._deps.search.scheduler.seed(parent_hypothesis)
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
        self._tree.save(self._deps.paths.tree_path)
        await self.publish_state()
        return {"hypothesis_ids": hypothesis_ids}

    async def checkpoint_evaluator(self, ref: ArtifactRef) -> dict[str, object]:
        """Persist one frozen evaluator bundle."""
        self._state.evaluator_ref = ref
        await self.persist_state()
        return {"evaluator_ref": ref}

    async def checkpoint_final_evaluator(self, ref: ArtifactRef) -> dict[str, object]:
        """Persist the hidden final evaluator used only by VALIDATE."""
        self._state.final_evaluator_ref = ref
        await self.persist_state()
        return {"final_evaluator_ref": ref}

    async def dispatch_general(self, task: str) -> dict[str, object]:
        """Dispatch one General Agent task and cache its result."""
        if self._deps.research.general is None:
            raise RuntimeError("General Agent dispatch is not configured")
        task = task.strip()
        if not task:
            raise ValueError("general task must be nonblank")
        owned = self._state.task_research_task == task
        ref = self._state.task_research_ref
        if ref is not None and owned:
            try:
                cached = json.loads(await self._deps.runtime.store.get_text(ref))
            except (OSError, ValueError):
                self._state.task_research_ref = None
                ref = None
                await self.persist_state()
                cached = None
            if isinstance(cached, dict) and cached:
                return {"cached": True, **cached}
        prior_agent_id = (
            self._state.task_research_agent_id if ref is None and owned else None
        )
        outcome = await self._deps.research.general(task, prior_agent_id)
        if ref is None and self._state.task_research_task in (None, task):
            self._state.task_research_task = task
            self._state.task_research_agent_id = outcome.agent_id
            self._state.task_research_ref = await self._deps.runtime.store.put_text(
                json.dumps(outcome.result, ensure_ascii=False)
            )
            await self.persist_state()
        return outcome.result
