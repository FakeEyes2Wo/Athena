"""Unit tests for hypothesis-graph algorithms and experiment queries."""

from athena.core.research_models import (
    EvalResult,
    ExperimentPlan,
    Hypothesis,
)
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.gui import experiments, graph


def _plan(kind: str = "search") -> ExperimentPlan:
    return ExperimentPlan(
        kind=kind,
        change="tweak the model",
        rubrics=["primary improves"],
        run_config_ref="artifact_config_1",
        budget={"turns": 5},
        acceptance_rule="primary improves",
    )


def _gitwork() -> GitWorkBranch:
    return GitWorkBranch(path="/tmp/w", branch="exp", base_commit="commit_0")


def _experiment(
    hyp_id: str,
    parent_id: str | None,
    *,
    kind: str = "search",
    status: ExperimentStatus = ExperimentStatus.PENDING,
    eval_: EvalResult | None = None,
) -> Experiment:
    return Experiment(
        parent_id=parent_id,
        hypothesis_id=hyp_id,
        commit="commit_0",
        plan=_plan(kind),
        gitwork=_gitwork(),
        status=status,
        eval=eval_,
    )


def build_tree() -> ResearchTree:
    """Build a small tree with lineage, one supersedes link, SOTA, and a pending hypothesis.

    Structure (experiments, with hypotheses)::

        exp0(hyp0, SUCCEEDED baseline, SOTA) ── exp1(hyp1, SUCCEEDED) ── exp2(hyp2, SUCCEEDED) ── exp3(hyp3, PENDING, supersedes hyp2)
        exp0 ── (hyp4, no experiment, PROPOSED)
    """
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(id="hyp0", statement="s0", intervention="i0", expected_effect="e0", status="SUPPORTED", order=0)
    )
    tree.add_experiment(
        "exp0",
        _experiment("hyp0", None, kind="baseline", status=ExperimentStatus.SUCCEEDED,
                    eval_=EvalResult(experiment_id="exp0", primary=0.50, per_sample="artifact_ps0")),
    )
    tree.set_sota("exp0")

    tree.add_hypothesis(
        Hypothesis(id="hyp1", statement="s1", intervention="i1", expected_effect="e1", status="SUPPORTED", order=1, parent_id="exp0")
    )
    tree.add_experiment(
        "exp1",
        _experiment("hyp1", "exp0", status=ExperimentStatus.SUCCEEDED,
                    eval_=EvalResult(experiment_id="exp1", primary=0.60, per_sample="artifact_ps1")),
    )

    tree.add_hypothesis(
        Hypothesis(id="hyp2", statement="s2", intervention="i2", expected_effect="e2", status="SUPPORTED", order=2, parent_id="exp1")
    )
    tree.add_experiment(
        "exp2",
        _experiment("hyp2", "exp1", status=ExperimentStatus.SUCCEEDED,
                    eval_=EvalResult(experiment_id="exp2", primary=0.55, per_sample="artifact_ps2")),
    )

    tree.add_hypothesis(
        Hypothesis(id="hyp3", statement="s3", intervention="i3", expected_effect="e3", order=3, parent_id="exp2", supersedes=["hyp2"])
    )
    tree.add_experiment("exp3", _experiment("hyp3", "exp2"))

    tree.add_hypothesis(
        Hypothesis(id="hyp4", statement="s4", intervention="i4", expected_effect="e4", order=4, parent_id="exp0", priority=10)
    )
    return tree


def test_build_hypothesis_graph_nodes_and_edges():
    tree = build_tree()
    result = graph.build_hypothesis_graph(tree)

    nodes = {n["id"]: n for n in result["nodes"]}
    assert len(nodes) == 5

    # SOTA marker + primary on the SUCCEEDED baseline node.
    assert nodes["hyp0"]["sota"] is True
    assert nodes["hyp0"]["primary"] == 0.5
    assert nodes["hyp0"]["experiment_id"] == "exp0"

    # A PROPOSED hypothesis without an experiment has no experiment_id/primary.
    assert nodes["hyp4"]["experiment_id"] is None
    assert nodes["hyp4"]["primary"] is None

    edges = {(e["kind"], e["source"], e["target"]) for e in result["edges"]}
    assert ("lineage", "hyp0", "hyp1") in edges
    assert ("lineage", "hyp1", "hyp2") in edges
    assert ("lineage", "hyp2", "hyp3") in edges
    assert ("supersedes", "hyp3", "hyp2") in edges
    # hyp4 has no experiment, so no lineage edge to it.
    assert not any(e[2] == "hyp4" for e in edges)


def test_topological_order():
    tree = build_tree()
    assert graph.topological_order(tree)["order"] == ["hyp0", "hyp1", "hyp2", "hyp3", "hyp4"]


def test_cycle_detect_acyclic():
    tree = build_tree()
    result = graph.cycle_detect(tree)
    assert result["acyclic"] is True
    assert result["lineage"]["acyclic"] is True
    assert result["supersedes"]["acyclic"] is True
    assert result["supersedes"]["cycles"] == []


def test_lineage():
    tree = build_tree()
    result = graph.lineage(tree, {"experiment_id": "exp2"})
    assert result["path"] == ["exp0", "exp1", "exp2"]
    assert [h["id"] for h in result["hypotheses"]] == ["hyp0", "hyp1", "hyp2"]


def test_active_hypotheses():
    tree = build_tree()
    result = graph.active_hypotheses(tree, {"experiment_id": "exp2", "child_hypothesis_id": "hyp3"})
    assert [h["id"] for h in result["active"]] == ["hyp0", "hyp1", "hyp3"]
    assert result["superseded"] == ["hyp2"]


def test_descendants():
    tree = build_tree()
    assert graph.descendants(tree, {"experiment_id": "exp0"})["descendants"] == ["exp1", "exp2", "exp3"]


def test_best_path():
    tree = build_tree()
    result = graph.best_path(tree)
    assert result["sota_id"] == "exp0"
    assert result["path"] == ["exp0"]
    assert [h["id"] for h in result["hypotheses"]] == ["hyp0"]


def test_rank_pending_by_priority():
    tree = build_tree()
    result = graph.rank_pending(tree)
    # hyp4 has priority 10 (higher priority), hyp3 default 1000.
    assert [r["id"] for r in result["ranked"]] == ["hyp4", "hyp3"]


def test_supersedes_closure():
    tree = build_tree()
    assert graph.supersedes_closure(tree, {"hypothesis_id": "hyp3"})["closure"] == ["hyp2"]


def test_refutation_reachability():
    tree = build_tree()
    result = graph.refutation_reachability(tree, {"hypothesis_id": "hyp1"})
    assert result["affected"] == ["hyp2", "hyp3"]


def test_experiments_list_and_detail():
    tree = build_tree()
    listed = experiments.list_experiments(tree)
    assert [d["experiment"]["id"] for d in listed] == ["exp0", "exp1", "exp2", "exp3"]

    detail = experiments.get_experiment_detail(tree, "exp2")
    assert detail["path"] == ["exp0", "exp1", "exp2"]
    assert detail["descendants"] == ["exp3"]
    assert detail["hypothesis"]["id"] == "hyp2"


def test_experiment_transition_rejects_illegal():
    tree = build_tree()
    # SUCCEEDED is terminal: no further transition is allowed.
    try:
        experiments.transition(tree, "exp0", "RUNNING")
    except ValueError:
        return
    raise AssertionError("expected illegal transition to raise ValueError")
