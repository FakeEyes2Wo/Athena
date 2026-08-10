"""SEARCH 图注册与唯一 SOTA 测试（supervisor_imp_docs Task 6）。

覆盖：全部有效假设先入图再选择；唯一 SOTA 严格按 test_score；tie 保留当前
SOTA；EvaluationPolicy 的成本预检切换 single-test。
"""

import pytest

from athena.core.research_models import Hypothesis
from athena.research.contracts import CandidateEvaluation, ExecutionConfig
from athena.research.evaluation import CostSnapshot, EvaluationPolicy
from athena.research.search import SearchService


def _hypothesis(statement: str) -> Hypothesis:
    return Hypothesis(statement=statement, intervention="x", expected_effect="up")


def six_hypotheses() -> list[Hypothesis]:
    return [_hypothesis(f"hypothesis {i}") for i in range(6)]


def candidate(
    score: float, *, candidate_id: str = "c", kfold_mean: float | None = None
) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate_id=candidate_id,
        test_score=score,
        kfold_mean=kfold_mean,
        direction="maximize",
    )


def test_all_valid_hypotheses_enter_graph_before_selection() -> None:
    search = SearchService()
    result = search.register_and_select(six_hypotheses(), selected_count=2)
    assert len(result.graph.pending_hypotheses()) == 6
    assert len(result.ranking.selected_ids) == 2
    assert len(result.ranking.deferred_ids) == 4
    # 未选中不标 REFUTED/REJECTED，只是 deferred
    assert all(h.status == "PROPOSED" for h in result.graph.pending_hypotheses())


def test_test_score_alone_selects_unique_sota() -> None:
    search = SearchService()
    result = search.freeze_round(
        parent_score=0.80,
        candidates=[
            candidate(0.83, candidate_id="a", kfold_mean=0.79),
            candidate(0.82, candidate_id="b", kfold_mean=0.90),
        ],
    )
    assert result.sota is not None
    assert result.sota.test_score == 0.83  # K-fold mean 不参与 SOTA
    assert result.ranking.sota_experiment_id == "a"
    assert result.winner == "candidate"


def test_tie_keeps_current_sota() -> None:
    search = SearchService()
    assert search.compare(0.8, 0.8 + 1e-13) == "current"
    # 平局不产生新 SOTA
    result = search.freeze_round(
        parent_score=0.8,
        candidates=[candidate(0.8 + 1e-13, candidate_id="t")],
    )
    assert result.sota is None
    assert result.ranking.sota_experiment_id is None
    assert result.winner == "current"


def test_minimize_direction_sorts_lowest_first() -> None:
    search = SearchService()
    low = CandidateEvaluation(candidate_id="low", test_score=0.2, direction="minimize")
    high = CandidateEvaluation(
        candidate_id="high", test_score=0.9, direction="minimize"
    )
    result = search.freeze_round(
        parent_score=0.5, candidates=[high, low], selected_count=1
    )
    assert result.ranking.selected_ids == ["low"]
    assert result.sota is not None and result.sota.candidate_id == "low"


def test_evaluation_policy_switches_to_single_test_when_expensive() -> None:
    policy = EvaluationPolicy()
    cfg = ExecutionConfig()  # kfold_policy=auto
    cheap = CostSnapshot(
        baseline_full_train_duration=1.0,
        remaining_duration_seconds=3600.0,
        selected_candidates=2,
        k_folds=5,
    )
    assert policy.choose_mode(cfg, cheap) == "kfold-5"
    # 成本预检：estimated > 40% 剩余时长 → single-test
    expensive = CostSnapshot(
        baseline_full_train_duration=400.0,
        remaining_duration_seconds=3600.0,
        selected_candidates=2,
        k_folds=5,
    )
    assert policy.choose_mode(cfg, expensive) == "single-test"
    # disabled → single-test；required → kfold-5
    assert (
        policy.choose_mode(ExecutionConfig(kfold_policy="disabled"), cheap)
        == "single-test"
    )
    assert (
        policy.choose_mode(ExecutionConfig(kfold_policy="required"), cheap) == "kfold-5"
    )
