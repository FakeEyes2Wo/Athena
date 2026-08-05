"""Unit tests for the pure Elo-rank-based Hypothesis Selector (step [10])."""

import unittest

from athena.research.ranking import RankedCandidate
from athena.workflows.search.hypothesis_selector import select_next_hypotheses
from athena.workflows.search.idea_schemas import (
    GATE_RUBRIC_VERSION,
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    HypothesisPackage,
    PipelineCandidateResult,
    StructuralCheckReport,
)


def _result(idea_id: str) -> PipelineCandidateResult:
    package = HypothesisPackage(
        idea_id=idea_id, generation_strategy="s", novel_hypothesis="h",
        supported_premises=[], inference_chain=[], predicted_observations=["p"],
        disconfirming_observations=["d"], lineage_op="generate",
    )
    structural = StructuralCheckReport(
        idea_id=idea_id, premise_evidence_ok=True, novel_hypothesis_testable=True)
    falsifiability = FalsifiabilityReport(
        idea_id=idea_id, testable_implication="t", unobservable_variables=[], is_falsifiable=True)
    decision = GateDecision(
        idea_id=idea_id, gate_phase="full", verdict=GateVerdict.PASS,
        rubric_version=GATE_RUBRIC_VERSION, item_scores=[])
    return PipelineCandidateResult(
        package=package, structural=structural, falsifiability=falsifiability, decision=decision)


def _ranked(idea_id: str, rating: float) -> RankedCandidate:
    return RankedCandidate(
        idea_id=idea_id, rating=rating, comparisons=1,
        rubric_version="hypo-pri-list/v1", evidence=[],
    )


class SelectNextHypothesesTest(unittest.TestCase):
    def test_picks_the_single_highest_rated_candidate_by_default(self) -> None:
        results = [_result("idea-1"), _result("idea-2")]
        ranking = [_ranked("idea-1", 1100.0), _ranked("idea-2", 1300.0)]
        selected = select_next_hypotheses(results, ranking)
        self.assertEqual(["idea-2"], [r.package.idea_id for r in selected])

    def test_budget_caps_how_many_are_returned(self) -> None:
        results = [_result("idea-1"), _result("idea-2"), _result("idea-3")]
        ranking = [_ranked("idea-1", 1000.0), _ranked("idea-2", 1300.0), _ranked("idea-3", 1200.0)]
        selected = select_next_hypotheses(results, ranking, budget=2)
        self.assertEqual(["idea-2", "idea-3"], [r.package.idea_id for r in selected])

    def test_zero_budget_selects_nothing(self) -> None:
        results = [_result("idea-1")]
        ranking = [_ranked("idea-1", 1000.0)]
        self.assertEqual([], select_next_hypotheses(results, ranking, budget=0))

    def test_negative_budget_selects_nothing(self) -> None:
        results = [_result("idea-1")]
        ranking = [_ranked("idea-1", 1000.0)]
        self.assertEqual([], select_next_hypotheses(results, ranking, budget=-1))

    def test_empty_ranking_selects_nothing(self) -> None:
        self.assertEqual([], select_next_hypotheses([_result("idea-1")], []))

    def test_budget_larger_than_available_candidates_returns_all_of_them(self) -> None:
        results = [_result("idea-1")]
        ranking = [_ranked("idea-1", 1000.0)]
        selected = select_next_hypotheses(results, ranking, budget=5)
        self.assertEqual(1, len(selected))

    def test_dangling_ranking_entry_raises_instead_of_silently_skipping(self) -> None:
        # results/ranking 必须来自同一次 run_full_pipeline 调用；不匹配是调用方的错误，
        # 不能悄悄吞掉伪装成"候选选少了"
        results = [_result("idea-1")]
        ranking = [_ranked("idea-does-not-exist", 1500.0)]
        with self.assertRaises(KeyError):
            select_next_hypotheses(results, ranking)

    def test_returns_full_audit_record_not_just_the_idea_id(self) -> None:
        # 把 selector 内部的交叉引用换成直接返回 ranking 条目（比如误把 RankedCandidate
        # 当结果返回），这条测试会失败：RankedCandidate 没有 .structural/.decision 这些字段
        results = [_result("idea-1")]
        ranking = [_ranked("idea-1", 1000.0)]
        selected = select_next_hypotheses(results, ranking)
        self.assertEqual(GateVerdict.PASS, selected[0].decision.verdict)
        self.assertTrue(selected[0].structural.premise_evidence_ok)
