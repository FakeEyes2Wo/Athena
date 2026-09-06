"""Crash reconciliation for durable Supervisor state and ResearchTree history."""

from collections.abc import Callable

from athena.core.research_tree import ResearchTree
from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.state import ResearchState

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def _is_settled(
    plan_id: str,
    plan: PlanState,
    tree: ResearchTree,
    has_final_baseline: bool,
    validation: dict[str, object] | None,
) -> bool:
    """Return True when a crash-left plan should be dropped."""
    if plan.kind == "SEARCH":
        experiment_id = tree.experiment_for_hypothesis(plan_id)
        if experiment_id is None:
            # A SEARCH plan missing a stored experiment is considered settled.
            return True
        return tree.get_experiment(experiment_id).status in _TERMINAL
    if plan.kind == "PREPARE":
        return has_final_baseline
    if plan.kind == "VALIDATE":
        return validation is not None
    return False


def reconcile(
    state: ResearchState,
    tree: ResearchTree,
    workspace_exists: Callable[[str], bool],
    artifact_exists: Callable[[str], bool],
) -> ResearchState:
    """Remove settled Plans and expose missing recovery prerequisites as WAITING."""
    has_final_baseline = any(
        experiment.status in _TERMINAL
        for experiment in tree.experiments(kind="baseline")
    )
    plans: dict[str, PlanState] = {}
    waiting = False
    for plan_id, plan in state.plans.items():
        if _is_settled(plan_id, plan, tree, has_final_baseline, state.validation):
            continue
        if not artifact_exists(plan.context_ref) or not workspace_exists(plan_id):
            waiting = True
        plans[plan_id] = plan
    return state.model_copy(
        update={"plans": plans, "status": "WAITING" if waiting else state.status}
    )


class Recovery:
    """Compatibility shim for legacy call sites."""

    def __call__(
        self,
        state: ResearchState,
        tree: ResearchTree,
        workspace_exists: Callable[[str], bool],
        artifact_exists: Callable[[str], bool],
    ) -> ResearchState:
        return self.reconcile(state, tree, workspace_exists, artifact_exists)

    @staticmethod
    def reconcile(
        state: ResearchState,
        tree: ResearchTree,
        workspace_exists: Callable[[str], bool],
        artifact_exists: Callable[[str], bool],
    ) -> ResearchState:
        return reconcile(state, tree, workspace_exists, artifact_exists)


__all__ = ["reconcile", "Recovery"]
