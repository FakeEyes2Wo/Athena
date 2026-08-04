"""Unit tests for pre_gate (2-item rubric) and hard_gate (5 + per-perspective rubric)."""

import unittest

from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS, MAX_TOTAL_RISKS, hard_gate, perspective_ok, pre_gate
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GateVerdict,
    NoveltyEvidenceReport,
    SkepticReport,
    StructuralCheckReport,
    ValidationPlan,
    VerifierSpec,
)
from athena.workflows.search.review_board import REVIEW_PERSPECTIVES


def _structural(ok: bool = True) -> StructuralCheckReport:
    return StructuralCheckReport(
        idea_id="idea-1",
        premise_evidence_ok=ok,
        novel_hypothesis_testable=ok,
        violations=[] if ok else ["premise_missing_evidence"],
    )


def _falsifiability(ok: bool = True) -> FalsifiabilityReport:
    return FalsifiabilityReport(
        idea_id="idea-1",
        testable_implication="measure Y" if ok else "",
        unobservable_variables=[] if ok else ["motivation"],
        is_falsifiable=ok,
    )


class PreGateTest(unittest.TestCase):
    def test_pass_when_both_ok(self) -> None:
        decision = pre_gate(_structural(True), _falsifiability(True))
        self.assertEqual(GateVerdict.PASS, decision.verdict)
        self.assertIsNone(decision.blocking_factor)

    def test_revise_when_structural_fails(self) -> None:
        decision = pre_gate(_structural(False), _falsifiability(True))
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("evidence_traceable", decision.blocking_factor)

    def test_revise_when_falsifiability_fails(self) -> None:
        decision = pre_gate(_structural(True), _falsifiability(False))
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("falsifiable", decision.blocking_factor)

    def test_item_scores_carry_evidence(self) -> None:
        decision = pre_gate(_structural(True), _falsifiability(True))
        for item_score in decision.item_scores:
            self.assertTrue(item_score.evidence)

    def test_rejects_mismatched_idea_ids(self) -> None:
        mismatched = _falsifiability(True).model_copy(update={"idea_id": "idea-2"})
        with self.assertRaises(ValueError):
            pre_gate(_structural(True), mismatched)


def _novelty(idea_id: str = "idea-1", overlap: float = 0.2, uncertainty: float = 0.2) -> NoveltyEvidenceReport:
    return NoveltyEvidenceReport(
        idea_id=idea_id, nearest_work=[], facet_overlap={"problem": overlap, "method": overlap},
        coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64, uncertainty=uncertainty,
    )


def _reviews(risks_by_perspective: dict[str, int] | None = None,
             fatal: str | None = None, fatals: set[str] | None = None,
             failed: str | None = None, idea_id: str = "idea-1") -> list[SkepticReport]:
    """按 REVIEW_PERSPECTIVES 造齐一套报告；risks_by_perspective 指定各视角的风险条数。

    fatal 是"只有一个视角 fatal"场景的简写；fatals 支持同时标记多个视角 fatal（例如验证
    hard_gate 是否真的按 REVIEW_PERSPECTIVES 顺序扫描，而不是按调用方传入顺序）。两者可
    叠加使用。
    """
    counts = risks_by_perspective or {}
    fatal_ids = (fatals or set()) | ({fatal} if fatal else set())
    return [
        SkepticReport(
            idea_id=idea_id, perspective=p.perspective_id, critique="c",
            unaddressed_risks=[f"risk-{i}" for i in range(counts.get(p.perspective_id, 0))],
            fatal_flaw_found=(p.perspective_id in fatal_ids),
            failed=(failed == p.perspective_id),
        )
        for p in REVIEW_PERSPECTIVES
    ]


def _verifier() -> VerifierSpec:
    return VerifierSpec(
        verifier_type="ablation_replication", applicable_domains=["ai4s"], observable_vars=["metric"],
        statistical_assumptions=["same seed budget"], success_condition="s", failure_condition="f",
        inconclusive_condition="i", cost_ref="sha256:" + "c" * 64, supports_auto_exec=True,
        requires_human_approval=False,
    )


def _plan(idea_id: str = "idea-1", verifier: VerifierSpec | None = ...) -> ValidationPlan:
    resolved_verifier = _verifier() if verifier is ... else verifier
    return ValidationPlan(
        idea_id=idea_id, minimal_test="t", verifier=resolved_verifier,
        decision_rule="r", estimated_cost_ref="sha256:" + "d" * 64,
    )


class HardGateTest(unittest.TestCase):
    def test_revise_when_structural_fails_even_if_novelty_also_fails(self) -> None:
        decision = hard_gate(_structural(False), _falsifiability(True), _novelty(overlap=0.9),
                              _reviews(), _plan())
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("evidence_traceable", decision.blocking_factor)

    def test_revise_when_novelty_has_no_facet_evidence(self) -> None:
        # 空 facet_overlap = 检索侧一项都没打出来，"新颖"没有证据支撑，不能靠均值 0.0 蒙混
        # 过关；补一轮检索就能修正，所以是 REVISE 而不是 REJECT
        novelty = NoveltyEvidenceReport(
            idea_id="idea-1", nearest_work=[], facet_overlap={},
            coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64, uncertainty=0.9,
        )
        decision = hard_gate(_structural(True), _falsifiability(True), novelty, _reviews(), _plan())
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("novelty_ok", decision.blocking_factor)

    def test_novelty_evidence_records_uncertainty_without_gating_on_it(self) -> None:
        # uncertainty 必须出现在证据文案里供审计，但高 uncertainty 本身不改变 verdict
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(uncertainty=0.95),
                              _reviews(), _plan())
        novelty_score = next(s for s in decision.item_scores if s.item == "novelty_ok")
        self.assertIn("uncertainty=0.95", novelty_score.evidence)
        self.assertEqual(GateVerdict.PASS, decision.verdict)

    def test_reject_when_novelty_overlap_too_high(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(overlap=0.9),
                              _reviews(), _plan())
        self.assertEqual(GateVerdict.REJECT, decision.verdict)
        self.assertEqual("novelty_ok", decision.blocking_factor)

    def test_exploratory_when_no_verifier_matched(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(), _reviews(),
                              _plan(verifier=None))
        self.assertEqual(GateVerdict.EXPLORATORY, decision.verdict)
        self.assertEqual("verifier_ok", decision.blocking_factor)

    def test_rejects_mismatched_idea_ids(self) -> None:
        with self.assertRaises(ValueError):
            hard_gate(_structural(True), _falsifiability(True), _novelty(idea_id="idea-2"),
                      _reviews(), _plan())


class HardGateMultiPerspectiveTest(unittest.TestCase):
    def test_rubric_has_five_fixed_items_plus_one_per_perspective(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                             _reviews(), _plan())
        self.assertEqual("full", decision.gate_phase)
        self.assertEqual(5 + len(REVIEW_PERSPECTIVES), len(decision.item_scores))
        items = [s.item for s in decision.item_scores]
        self.assertIn("risk_total", items)
        for perspective in REVIEW_PERSPECTIVES:
            self.assertIn(f"risk_ok_{perspective.perspective_id}", items)
        for score in decision.item_scores:
            self.assertTrue(score.evidence)

    def test_blocking_factor_names_the_specific_perspective(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                             _reviews(fatal="statistics"), _plan())
        self.assertEqual(GateVerdict.REJECT, decision.verdict)
        self.assertEqual("risk_ok_statistics", decision.blocking_factor)

    def test_failed_perspective_blocks_instead_of_silently_passing(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                             _reviews(failed="methodology"), _plan())
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("risk_ok_methodology", decision.blocking_factor)

    def test_single_perspective_over_tolerance_blocks_without_fatal_or_failure(self) -> None:
        # 既没 fatal 也没 failed，仅某一视角风险数超单项阈值 —— 这条分支在判定链里先于
        # risk_total，需要独立覆盖
        decision = hard_gate(
            _structural(True), _falsifiability(True), _novelty(),
            _reviews({"methodology": MAX_TOLERATED_RISKS + 1}), _plan(),
        )
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("risk_ok_methodology", decision.blocking_factor)

    def test_risk_total_evidence_flags_incomplete_data(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                             _reviews(failed="methodology"), _plan())
        total = next(s for s in decision.item_scores if s.item == "risk_total")
        self.assertIn("incomplete: 1 perspective(s) failed", total.evidence)

    def test_risks_below_the_total_ceiling_still_pass(self) -> None:
        # 1/1/0 = 2 条，每项不超 MAX_TOLERATED_RISKS，总量也不超 MAX_TOTAL_RISKS
        decision = hard_gate(
            _structural(True), _falsifiability(True), _novelty(),
            _reviews({"methodology": 1, "statistics": 1}), _plan(),
        )
        self.assertEqual(GateVerdict.PASS, decision.verdict)

    def test_risks_spread_across_perspectives_are_caught_by_risk_total(self) -> None:
        # 2/1/1 = 4 条：每一项都不超 MAX_TOLERATED_RISKS，但总量超 MAX_TOTAL_RISKS。
        # 这正是拆分视角引入的单向放宽——拆分前这 4 条集中在一份报告里会直接 REVISE
        decision = hard_gate(
            _structural(True), _falsifiability(True), _novelty(),
            _reviews({"methodology": 2, "statistics": 1, "domain_consistency": 1}), _plan(),
        )
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("risk_total", decision.blocking_factor)

    def test_risk_total_ceiling_is_actually_reachable(self) -> None:
        # 判定链第 5 条（任一视角超单项阈值）先于第 6 条，所以第 6 条要触发就必须
        # "所有视角都不超单项阈值、但总量超标"。若 MAX_TOTAL_RISKS >= 单项阈值 × N，
        # 这两个条件数学上无法同时成立，risk_total 就成了恒满分的死代码。
        self.assertLess(MAX_TOTAL_RISKS, MAX_TOLERATED_RISKS * len(REVIEW_PERSPECTIVES))

    def test_every_risk_item_evidence_carries_the_cross_perspective_total(self) -> None:
        decision = hard_gate(
            _structural(True), _falsifiability(True), _novelty(),
            _reviews({"methodology": 2, "statistics": 1, "domain_consistency": 1}), _plan(),
        )
        for score in decision.item_scores:
            if score.item.startswith("risk_ok_") or score.item == "risk_total":
                self.assertIn("cross-perspective total unaddressed risks: 4", score.evidence)

    def test_rejects_incomplete_review_set(self) -> None:
        with self.assertRaises(ValueError):
            hard_gate(_structural(True), _falsifiability(True), _novelty(),
                      _reviews()[:2], _plan())

    def test_rejects_empty_reviews(self) -> None:
        with self.assertRaises(ValueError):
            hard_gate(_structural(True), _falsifiability(True), _novelty(), [], _plan())

    def test_rejects_duplicate_perspective_even_with_full_set_coverage(self) -> None:
        # 两份 methodology + 各一份其余视角：集合去重后仍与 REVIEW_PERSPECTIVES 的 id 集合
        # 相等（纯集合比较会放行），但长度是 4 != 3。长度检查把契约里的"exactly one"落到实处，
        # 否则 by_perspective 字典推导会静默丢弃前一份 methodology 的风险/fatal 标记。
        duplicated = _reviews() + [_reviews()[0]]
        with self.assertRaises(ValueError):
            hard_gate(_structural(True), _falsifiability(True), _novelty(), duplicated, _plan())

    def test_rejects_mismatched_idea_id_in_reviews(self) -> None:
        with self.assertRaises(ValueError):
            hard_gate(_structural(True), _falsifiability(True), _novelty(),
                      _reviews(idea_id="idea-2"), _plan())

    def test_result_is_independent_of_caller_ordering(self) -> None:
        # hard_gate 内部按 REVIEW_PERSPECTIVES 重排，不依赖调用方传入顺序。用一个 fatal
        # 视角测不出这一点——唯一命中项在任何顺序下都是同一个。这里让 methodology 与
        # domain_consistency 同时 fatal，并以 reversed() 顺序（domain_consistency 在前，
        # methodology 在后）传入：若 hard_gate 真的按 REVIEW_PERSPECTIVES 重排，
        # methodology 排在 domain_consistency 之前，blocking_factor 必须是
        # risk_ok_methodology；若它偷懒直接用调用方传入的顺序，会变成
        # risk_ok_domain_consistency，测试就能抓到这个回归。
        shuffled = list(reversed(_reviews(fatals={"methodology", "domain_consistency"})))
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                             shuffled, _plan())
        self.assertEqual("risk_ok_methodology", decision.blocking_factor)
        # 顺带锁住 item_scores 里 risk_ok_* 三项的出现顺序也等于 REVIEW_PERSPECTIVES 的
        # 顺序——重排的两个下游（blocking_factor 与 item_scores 顺序）都被覆盖。
        risk_items = [s.item for s in decision.item_scores if s.item.startswith("risk_ok_")]
        self.assertEqual(
            [f"risk_ok_{p.perspective_id}" for p in REVIEW_PERSPECTIVES], risk_items,
        )


class PerspectiveOkTest(unittest.TestCase):
    """辩论循环复用这个谓词作终止条件，所以它是公开契约而非实现细节。"""

    def _report(self, **kwargs) -> SkepticReport:
        base = dict(idea_id="idea-1", perspective="methodology", critique="c",
                    unaddressed_risks=[], fatal_flaw_found=False)
        base.update(kwargs)
        return SkepticReport(**base)

    def test_clean_review_passes(self) -> None:
        self.assertTrue(perspective_ok(self._report()))

    def test_failed_review_never_passes(self) -> None:
        # fail-closed：审阅没跑成不能等于审阅批准
        self.assertFalse(perspective_ok(self._report(failed=True)))

    def test_fatal_flaw_never_passes(self) -> None:
        self.assertFalse(perspective_ok(self._report(fatal_flaw_found=True)))

    def test_risks_over_tolerance_fail(self) -> None:
        over = ["r"] * (MAX_TOLERATED_RISKS + 1)
        self.assertFalse(perspective_ok(self._report(unaddressed_risks=over)))
        at_limit = ["r"] * MAX_TOLERATED_RISKS
        self.assertTrue(perspective_ok(self._report(unaddressed_risks=at_limit)))
