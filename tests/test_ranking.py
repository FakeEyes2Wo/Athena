from athena.experiment.ranking import HypothesisRanker, ProximityGraph
from athena.experiment import ranking as ranking_module
from athena.core.research_models import Hypothesis


def test_compatibility_search_loop_is_canonical() -> None:
    from athena.experiment.search_loop import SearchLoop as CompatibilitySearchLoop
    from athena.workflows.search.search_loop import SearchLoop as CanonicalSearchLoop

    assert CompatibilitySearchLoop is CanonicalSearchLoop


def test_ranker_selects_highest_score():
    ranker = HypothesisRanker()
    pg = ProximityGraph()
    h1 = Hypothesis(
        statement="A",
        intervention="add X",
        expected_effect="+0.05",
        id="h1",
        parent_id="root",
        sources=[],
        status="PROPOSED",
    )
    h2 = Hypothesis(
        statement="B",
        intervention="add Y",
        expected_effect="+0.03",
        id="h2",
        parent_id="root",
        sources=[],
        status="PROPOSED",
    )
    # h2 已执行一次，因此 sigma 更低
    ranker.update([("h1", "h2", False)])  # h1 击败 h2
    selected = ranker.select([h1, h2], pg)
    assert selected == "h1"


def test_ranker_explores_unknown():
    ranker = HypothesisRanker()
    pg = ProximityGraph()
    h1 = Hypothesis(
        statement="A",
        intervention="add X",
        expected_effect="+0.05",
        id="h1",
        parent_id="root",
        sources=[],
        status="PROPOSED",
    )
    h2 = Hypothesis(
        statement="B",
        intervention="add Y",
        expected_effect="+0.03",
        id="h2",
        parent_id="root",
        sources=[],
        status="PROPOSED",
    )
    # h1 执行多次（低 sigma），h2 从未执行（高 sigma）—— h2 应在 UCB 中胜出
    for _ in range(10):
        ranker.update([("h1", "h_phantom", False)])
    selected = ranker.select([h1, h2], pg)
    assert selected == "h2"  # 更高的不确定性奖励


def test_proximity_penalty():
    ranker = HypothesisRanker()
    pg = ProximityGraph()
    h1 = Hypothesis(
        statement="add dropout",
        intervention="add Dropout(0.5)",
        expected_effect="+0.02",
        id="h1",
        parent_id="root",
        sources=[],
        status="PROPOSED",
    )
    h2 = Hypothesis(
        statement="add dropout too",
        intervention="add Dropout(0.3)",
        expected_effect="+0.02",
        id="h2",
        parent_id="root",
        sources=[],
        status="PROPOSED",
    )
    pg.add(h1)
    dist = pg.min_distance_to_executed(h2)
    assert dist < 0.7  # 非常相似，距离应很小


def test_near_duplicate_is_penalized_more_than_diverse_candidate():
    ranker = HypothesisRanker()
    proximity = ProximityGraph()
    executed = Hypothesis(
        id="done",
        statement="add dropout",
        intervention="add dropout to hidden layers",
        expected_effect="reduce validation loss",
        sources=["common_knowledge"],
        status="PROPOSED",
    )
    near_duplicate = Hypothesis(
        id="near",
        statement="add dropout",
        intervention="add dropout to hidden layer",
        expected_effect="reduce validation loss",
        sources=["common_knowledge"],
        status="PROPOSED",
    )
    diverse = Hypothesis(
        id="diverse",
        statement="use gradient boosting",
        intervention="replace the neural model with lightgbm",
        expected_effect="improve validation score",
        sources=["common_knowledge"],
        status="PROPOSED",
    )
    proximity.add(executed)

    assert ranker.select([near_duplicate, diverse], proximity, beta=2.0) == "diverse"


def test_fixed_seed_produces_stable_selection():
    hypotheses = [
        Hypothesis(
            id="h1",
            statement="standardize features",
            intervention="apply standard scaler",
            expected_effect="improve validation score",
            sources=["common_knowledge"],
            status="PROPOSED",
        ),
        Hypothesis(
            id="h2",
            statement="add interactions",
            intervention="cross correlated features",
            expected_effect="improve validation score",
            sources=["common_knowledge"],
            status="PROPOSED",
        ),
    ]

    comparisons = [
        ("h1", "h2", False),
        ("h2", "h1", False),
        ("h1", "h2", False),
        ("h1", "h3", False),
    ] * 16
    first_ranker = HypothesisRanker(seed=11)
    second_ranker = HypothesisRanker(seed=11)
    other_seed = HypothesisRanker(seed=12)
    for ranker in (first_ranker, second_ranker, other_seed):
        ranker.update(comparisons)

    assert first_ranker.estimate("h1") == second_ranker.estimate("h1")
    assert first_ranker.estimate("h1") != other_seed.estimate("h1")
    assert first_ranker.select(hypotheses, ProximityGraph()) == second_ranker.select(
        hypotheses, ProximityGraph()
    )


def test_bradley_terry_repeated_wins_raise_winner_strength():
    strengths = ranking_module.fit_bradley_terry({}, [("winner", "loser")] * 20)

    assert strengths["winner"] > 0.0
    assert strengths["loser"] < 0.0
    assert abs(sum(strengths.values())) < 1e-12


def test_ties_do_not_change_ranker_estimate():
    ranker = HypothesisRanker(seed=7)
    before = ranker.estimate("h1")

    ranker.update([("h1", "h2", True)])

    assert ranker.estimate("h1") == before


def test_selection_score_penalizes_near_duplicates():
    near = ranking_module.selection_score(0.0, 1.0, 0.0, beta=2.0)
    diverse = ranking_module.selection_score(0.0, 1.0, 1.0, beta=2.0)

    assert near < diverse
