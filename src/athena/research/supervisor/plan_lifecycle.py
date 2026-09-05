"""Explicit orchestration facade for durable Supervisor Plans."""

import json

from athena.core.contracts import ArtifactRef
from athena.core.research_models import Hypothesis
from athena.research.supervisor.deps import SupervisorDeps
from athena.research.supervisor.plan_runtime import (
    CompletedPlanTurn,
    PlanRuntime,
)
from athena.research.supervisor.plans import PlanInput
from athena.research.supervisor.run_state import SupervisorRunState
from athena.research.supervisor.settlement import PlanSettlement


class PlanLifecycle:
    """Coordinate Plan runtime, settlement, and durable state publication."""

    def __init__(
        self, owner: object, deps: SupervisorDeps, run: SupervisorRunState
    ) -> None:
        self._owner = owner
        self._deps = deps
        self._runtime = PlanRuntime(
            owner,
            deps,
            run,
            save_state=self.save_state,
            publish_state=self.publish_state,
            persist_state=self.persist_state,
        )
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

    async def run_turn(self, plan_id: str) -> CompletedPlanTurn:
        """Run one Plan Agent turn through the runtime boundary."""
        return await self._runtime.run_turn(plan_id)

    async def start_plan(self, hypothesis_id: str) -> str:
        """Create one Plan through the runtime boundary."""
        return await self._runtime.start_plan(hypothesis_id)

    async def plan_input(self, plan_id: str) -> PlanInput:
        """Load the immutable input frozen for one active Plan."""
        return await self._runtime.plan_input(plan_id)

    def workspace_path(self, plan_id: str):
        """Return the worktree path bound to a stable Plan identity."""
        return self._runtime.workspace_path(plan_id)

    def workspace(self, plan_id: str):
        """Return the branch owned by one active Plan."""
        return self._runtime.workspace(plan_id)

    @staticmethod
    def plan_identity(plan_id: str) -> dict[str, str]:
        """Project the stable identity shared by all Plan resources."""
        return PlanRuntime.plan_identity(plan_id)

    async def recover(self, state=None):
        """Reconcile persisted Plans through the runtime boundary."""
        return await self._runtime.recover(state)

    async def settle_plan(
        self, plan_id: str, best_ref: ArtifactRef | None, result
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
        parent_experiment = self._tree.get_experiment(parent_id)
        return parent_id, self._tree.get_hypothesis(parent_experiment.hypothesis_id)

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


__all__ = [
    "CompletedPlanTurn",
    "PlanLifecycle",
]
