from athena.core.ranking import HypothesisRanker, ProximityGraph
from athena.core.schemas import Hypothesis


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
    # h2 has been executed once, so lower sigma
    ranker.update([("h1", "h2", False)])  # h1 beats h2
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
    # h1 executed many times (low sigma), h2 never (high sigma) -- h2 should win on UCB
    for _ in range(10):
        ranker.update([("h1", "h_phantom", False)])
    selected = ranker.select([h1, h2], pg)
    assert selected == "h2"  # higher uncertainty bonus


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
    assert dist < 0.7  # very similar, should be close
