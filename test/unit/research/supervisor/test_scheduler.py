"""Behavioral tests for deterministic rolling SEARCH scheduling."""

from athena.core.research_models import Hypothesis
from athena.core.research_tree import Experiment, ResearchTree
from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.scheduler import (
    ScheduleAction,
    Scheduler,
    count_search_attempts,
)
from athena.research.supervisor.state import ResearchState

_CONTEXT_REF = "sha256:" + "d" * 64
_BEST_REF = "sha256:" + "b" * 64


def _plan(
    *, turns: int = 0, limit: int | None = 12, best: str | None = _BEST_REF
) -> PlanState:
    return PlanState(
        kind="SEARCH",
        context_ref=_CONTEXT_REF,
        turns_used=turns,
        turn_limit=limit,
        patience=4,
        best_ref=best,
    )


def _state(
    plans: dict[str, PlanState] | None = None,
    *,
    concurrency: int = 4,
    search_limit: int = 10,
) -> ResearchState:
    return ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=search_limit,
        concurrency=concurrency,
        plans=plans or {},
    )


def _tree(*hypothesis_ids: str) -> ResearchTree:
    tree = ResearchTree()
    for order, hypothesis_id in enumerate(hypothesis_ids):
        tree.add_hypothesis(
            Hypothesis(
                id=hypothesis_id,
                statement=f"claim {hypothesis_id}",
                intervention=f"change {hypothesis_id}",
                expected_effect="improve trusted metric",
                priority=1000.0,
                order=order,
            )
        )
    return tree


def _final_experiment(
    hypothesis_id: str, *, kind: str = "search", experiment_id: str
) -> Experiment:
    return Experiment.model_validate(
        {
            "parent_id": None,
            "hypothesis_id": hypothesis_id,
            "commit": "abc123",
            "plan": {
                "kind": kind,
                "change": f"test {hypothesis_id}",
                "run_config_ref": "artifact://run-config",
                "budget": {},
                "acceptance_rule": "trusted evaluation completes",
            },
            "gitwork": {
                "path": f"C:/worktrees/{hypothesis_id}",
                "branch": f"search/{hypothesis_id}",
                "base_commit": "base123",
            },
            "status": "SUCCEEDED",
            "eval": {
                "experiment_id": experiment_id,
                "primary": 0.8,
                "per_sample": _BEST_REF,
            },
        }
    )


def _running_experiment(
    hypothesis_id: str, *, experiment_id: str, kind: str = "search"
) -> Experiment:
    return Experiment.model_validate(
        {
            "parent_id": None,
            "hypothesis_id": hypothesis_id,
            "commit": "abc124",
            "plan": {
                "kind": kind,
                "change": f"test {hypothesis_id}",
                "run_config_ref": "artifact://run-config",
                "budget": {},
                "acceptance_rule": "trusted evaluation completes",
            },
            "gitwork": {
                "path": f"C:/worktrees/{hypothesis_id}",
                "branch": f"search/{hypothesis_id}",
                "base_commit": "base123",
            },
            "status": "RUNNING",
        }
    )


def _tree_with_final(hypothesis_id: str, *, kind: str = "search") -> ResearchTree:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id=hypothesis_id,
            statement="claim",
            intervention="change",
            expected_effect="improve",
        )
    )
    experiment_id = f"exp_{hypothesis_id}"
    tree.add_experiment(
        experiment_id,
        _final_experiment(hypothesis_id, kind=kind, experiment_id=experiment_id),
    )
    return tree


def test_completed_slot_is_refilled_without_batch_barrier() -> None:
    state = _state({"h1": _plan(), "h2": _plan(), "h3": _plan()})
    tree = _tree("h1", "h2", "h3", "h5")

    actions = Scheduler().next_actions(state, tree, {"h1", "h2", "h3"})

    assert actions == [ScheduleAction.StartNew("h5")]


def test_waiting_plan_releases_slot_and_resumes_before_new_plan() -> None:
    state = _state({"h_old": _plan(turns=3, limit=3, best=None)}, concurrency=1)
    tree = _tree("h_old", "h_new")

    assert Scheduler().next_actions(state, tree, set()) == [
        ScheduleAction.StartNew("h_new")
    ]

    state.plans["h_old"].turn_limit = 5
    assert Scheduler().next_actions(state, tree, set()) == [
        ScheduleAction.Resume("h_old")
    ]


def test_policy_priority_and_fifo_order_new_hypotheses() -> None:
    tree = _tree("h_fifo_first", "h_high", "h_fifo_second")
    tree.get_hypothesis("h_high").priority = 1016.0
    state = _state(concurrency=3)

    assert Scheduler().next_actions(state, tree, set()) == [
        ScheduleAction.StartNew("h_high"),
        ScheduleAction.StartNew("h_fifo_first"),
        ScheduleAction.StartNew("h_fifo_second"),
    ]


def test_human_next_precedes_policy_queue_without_priority_mutation() -> None:
    tree = _tree("h_high", "h_requested")
    tree.get_hypothesis("h_high").priority = 2000.0
    requested_priority = tree.get_hypothesis("h_requested").priority

    actions = Scheduler().next_actions(
        _state(concurrency=1), tree, set(), human_next="h_requested"
    )

    assert actions == [ScheduleAction.StartNextHypothesis("h_requested")]
    assert tree.get_hypothesis("h_requested").priority == requested_priority


def test_generate_fills_only_remaining_slots_and_attempt_budget() -> None:
    state = _state({"h_active": _plan()}, concurrency=4, search_limit=3)
    tree = _tree("h_active")

    assert Scheduler().next_actions(state, tree, {"h_active"}) == [
        ScheduleAction.Generate(2)
    ]


def test_fresh_search_asks_for_all_unfilled_slots() -> None:
    assert Scheduler().next_actions(_state(), ResearchTree(), set()) == [
        ScheduleAction.Generate(4)
    ]


def test_prepare_plan_does_not_count_as_search_attempt() -> None:
    prepare = PlanState(
        kind="PREPARE", context_ref=_CONTEXT_REF, turns_used=0, turn_limit=12
    )
    state = _state({"prepare": prepare}, concurrency=2, search_limit=2)

    assert Scheduler().next_actions(state, ResearchTree(), set()) == [
        ScheduleAction.Generate(2)
    ]


def test_settled_search_experiment_counts_as_attempt() -> None:
    assert count_search_attempts(_state(), _tree_with_final("h1")) == 1


def test_final_search_experiment_counts_after_hypothesis_settlement() -> None:
    tree = _tree_with_final("h1")
    tree.update_hypothesis_status("h1", "SUPPORTED")

    assert count_search_attempts(_state(), tree) == 1


def test_active_search_plan_counts_as_attempt() -> None:
    assert count_search_attempts(_state({"h1": _plan()}), ResearchTree()) == 1


def test_narrow_experiments_query_filters_by_plan_kind() -> None:
    tree = _tree("h_settled", "h_running")
    tree.add_experiment(
        "e_settled",
        _final_experiment("h_settled", experiment_id="e_settled"),
    )
    tree.add_experiment(
        "e_running",
        _running_experiment("h_running", experiment_id="e_running"),
    )

    assert [exp.hypothesis_id for exp in tree.experiments()] == [
        "h_settled",
        "h_running",
    ]
    assert [exp.hypothesis_id for exp in tree.experiments(kind="search")] == [
        "h_settled",
        "h_running",
    ]
    assert tree.experiments(kind="baseline") == []


def test_count_search_attempts_counts_only_terminal_search_experiments() -> None:
    tree = _tree("h_settled", "h_running")
    tree.add_experiment(
        "e_settled",
        _final_experiment("h_settled", experiment_id="e_settled"),
    )
    tree.add_experiment(
        "e_running",
        _running_experiment("h_running", experiment_id="e_running"),
    )

    assert count_search_attempts(_state(), tree) == 1
