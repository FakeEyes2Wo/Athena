"""Crash reconciliation across ResearchState and ResearchTree."""

from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.state import ResearchState

_REF = "sha256:" + "a" * 64


def _state() -> ResearchState:
    return ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=10,
        concurrency=4,
        plans={
            "h1": PlanState(
                kind="SEARCH",
                context_ref=_REF,
                turns_used=1,
                turn_limit=12,
                patience=4,
            )
        },
    )


def _tree(*, settled: bool = False) -> ResearchTree:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="h1", statement="claim", intervention="change", expected_effect="improve"
        )
    )
    if settled:
        tree.add_experiment(
            "e1",
            Experiment(
                hypothesis_id="h1",
                commit="c1",
                plan=ExperimentPlan(
                    kind="search",
                    change="change",
                    run_config_ref=_REF,
                    budget={},
                    acceptance_rule="trusted score",
                ),
                gitwork=GitWorkBranch(
                    path="C:/worktrees/h1", branch="athena/plan/h1", base_commit="c0"
                ),
                status=ExperimentStatus.FAILED,
                error="settled before crash",
            ),
        )
    return tree


def test_tree_settled_but_state_active_is_reconciled_once() -> None:
    reconciled = Recovery().reconcile(
        _state(),
        _tree(settled=True),
        workspace_exists=lambda _plan_id: True,
        artifact_exists=lambda _ref: True,
    )

    assert "h1" not in reconciled.plans
    assert reconciled.status == "RUNNING"


def test_active_plan_without_experiment_is_dropped() -> None:
    reconciled = Recovery().reconcile(
        _state(),
        _tree(),
        workspace_exists=lambda _plan_id: True,
        artifact_exists=lambda _ref: True,
    )

    assert "h1" not in reconciled.plans
    assert reconciled.status == "RUNNING"


def test_missing_context_drops_orphan_plan() -> None:
    reconciled = Recovery().reconcile(
        _state(),
        _tree(),
        workspace_exists=lambda _plan_id: True,
        artifact_exists=lambda _ref: False,
    )

    assert "h1" not in reconciled.plans
    assert reconciled.status == "RUNNING"


def test_missing_workspace_drops_orphan_plan() -> None:
    reconciled = Recovery().reconcile(
        _state(),
        _tree(),
        workspace_exists=lambda _plan_id: False,
        artifact_exists=lambda _ref: True,
    )

    assert "h1" not in reconciled.plans
    assert reconciled.status == "RUNNING"


def test_proposed_hypothesis_without_plan_stays_queued() -> None:
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="h_new",
            statement="claim",
            intervention="change",
            expected_effect="improve",
        )
    )

    reconciled = Recovery().reconcile(
        _state(),
        tree,
        workspace_exists=lambda _plan_id: True,
        artifact_exists=lambda _ref: True,
    )

    assert reconciled.plans == {}
    assert tree.pending_hypotheses()


def test_settled_prepare_plan_is_removed_when_baseline_final_experiment_exists() -> (
    None
):
    state = ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=10,
        concurrency=4,
        plans={
            "prepare": PlanState(
                kind="PREPARE", context_ref=_REF, turns_used=0, turn_limit=12
            )
        },
    )
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="h_baseline",
            statement="claim",
            intervention="change",
            expected_effect="improve",
        )
    )
    tree.add_experiment(
        "e_baseline",
        Experiment(
            hypothesis_id="h_baseline",
            commit="c1",
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=_REF,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/prepare",
                branch="athena/plan/prepare",
                base_commit="c0",
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval={"experiment_id": "e_baseline", "primary": 0.5, "per_sample": _REF},
        ),
    )
    tree.update_hypothesis_status("h_baseline", "SUPPORTED")

    reconciled = Recovery().reconcile(
        state,
        tree,
        workspace_exists=lambda _plan_id: True,
        artifact_exists=lambda _ref: True,
    )

    assert "prepare" not in reconciled.plans


def test_non_terminal_baseline_keeps_prepare_plan() -> None:
    state = ResearchState(
        status="RUNNING",
        phase="SEARCH",
        search_limit=10,
        concurrency=4,
        plans={
            "prepare": PlanState(
                kind="PREPARE", context_ref=_REF, turns_used=0, turn_limit=12
            )
        },
    )
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="h_baseline",
            statement="claim",
            intervention="change",
            expected_effect="improve",
        )
    )
    tree.add_experiment(
        "e_baseline",
        Experiment(
            hypothesis_id="h_baseline",
            commit="c1",
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=_REF,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path="C:/worktrees/prepare",
                branch="athena/plan/prepare",
                base_commit="c0",
            ),
            status=ExperimentStatus.RUNNING,
        ),
    )

    reconciled = Recovery().reconcile(
        state,
        tree,
        workspace_exists=lambda _plan_id: True,
        artifact_exists=lambda _ref: True,
    )

    assert "prepare" in reconciled.plans
