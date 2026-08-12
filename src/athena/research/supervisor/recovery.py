"""Crash reconciliation for durable Supervisor state and ResearchTree history."""

from collections.abc import Callable

from athena.core.research_tree import ResearchTree
from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.state import ResearchState

_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


def _has_final_baseline(tree: ResearchTree) -> bool:
    """True when the tree holds a terminal PREPARE baseline experiment."""
    return any(
        experiment.status in _TERMINAL
        for experiment in tree.experiments(kind="baseline")
    )


def _is_settled(
    plan_id: str,
    plan: PlanState,
    tree: ResearchTree,
    has_final_baseline: bool,
    validation: dict[str, object] | None,
) -> bool:
    """True when a crash left a finished Plan still listed as active."""
    if plan.kind == "SEARCH":
        experiment_id = tree.experiment_for_hypothesis(plan_id)
        if experiment_id is None:
            return False
        return tree.get_experiment(experiment_id).status in _TERMINAL
    if plan.kind == "PREPARE":
        return has_final_baseline
    if plan.kind == "VALIDATE":
        return validation is not None
    return False


class Recovery:
    """Reconcile unfinished Plans without recreating frozen execution context."""

    @staticmethod
    def reconcile(
        state: ResearchState,
        tree: ResearchTree,
        workspace_exists: Callable[[str], bool],
        artifact_exists: Callable[[str], bool],
    ) -> ResearchState:
        """Remove settled Plans and expose missing recovery prerequisites as waiting."""
        has_final_baseline = _has_final_baseline(tree)
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
