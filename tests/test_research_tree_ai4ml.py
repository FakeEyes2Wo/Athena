from athena.core.research.research_tree import ResearchTree, Experiment
from athena.core.schemas import Hypothesis, ExperimentPlan, MetricSpec
from athena.core.gitutils.workspace import GitWorkBranch


def make_exp(name: str, result: float, commit: str = "0" * 40) -> Experiment:
    return Experiment(
        commit=commit,
        hypothesis=Hypothesis(
            statement=name, intervention=name, expected_effect="improve"
        ),
        plan=ExperimentPlan(
            kind="test",
            change=name,
            run_config_ref="artifact://cfg",
            budget={},
            acceptance_rule="any",
        ),
        metric_type="AUC",
        result=result,
        gitwork=GitWorkBranch(
            path=f"/tmp/{name}", branch=f"test/{name}", base_commit=commit
        ),
    )


def test_best_experiment():
    tree = ResearchTree()
    root = tree.create_node(make_exp("baseline", 0.72))
    leaf = tree.create_node(make_exp("better", 0.78), parent_id=root.id)
    best = tree.best_experiment()
    assert best is not None
    assert best.exp.result_as_float() == 0.78


def test_pending_hypotheses():
    tree = ResearchTree()
    root = tree.create_node(make_exp("baseline", 0.72))
    h = Hypothesis(
        statement="try X",
        intervention="add X",
        expected_effect="+0.05",
        id="h1",
        parent_id=root.id,
        sources=[],
    )
    tree.add_hypothesis(h)
    pending = tree.pending_hypotheses()
    assert len(pending) == 1
    assert pending[0].statement == "try X"


def test_hypotheses_path():
    tree = ResearchTree()
    root = tree.create_node(make_exp("baseline", 0.72))
    h1 = Hypothesis(
        statement="step1",
        intervention="add A",
        expected_effect="+0.03",
        id="h1",
        parent_id=root.id,
        sources=[],
    )
    tree.add_hypothesis(h1)
    leaf = tree.create_node(make_exp("after_step1", 0.75), parent_id=root.id)
    h2 = Hypothesis(
        statement="step2",
        intervention="add B",
        expected_effect="+0.02",
        id="h2",
        parent_id=leaf.id,
        sources=[],
    )
    tree.add_hypothesis(h2)
    path = tree.hypotheses_path(leaf.id)
    assert len(path) == 1  # only h1 on path to leaf
    assert path[0].statement == "step1"
