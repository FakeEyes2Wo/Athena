"""Unit tests for pre_gate (2-item rubric) and hard_gate (5-item rubric)."""

import unittest

from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS, hard_gate, pre_gate
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GateVerdict,
    NoveltyEvidenceReport,
    SkepticReport,
    StructuralCheckReport,
    ValidationPlan,
    VerifierSpec,
)


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


def _skeptic(idea_id: str = "idea-1", fatal: bool = False, risks: list[str] | None = None) -> SkepticReport:
    return SkepticReport(idea_id=idea_id, critique="c", unaddressed_risks=risks or [], fatal_flaw_found=fatal)


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
    def test_pass_when_everything_is_clean(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(), _skeptic(), _plan())
        self.assertEqual("full", decision.gate_phase)
        self.assertEqual(GateVerdict.PASS, decision.verdict)
        self.assertEqual(5, len(decision.item_scores))

    def test_revise_when_structural_fails_even_if_novelty_also_fails(self) -> None:
        decision = hard_gate(_structural(False), _falsifiability(True), _novelty(overlap=0.9), _skeptic(), _plan())
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("evidence_traceable", decision.blocking_factor)

    def test_revise_when_novelty_has_no_facet_evidence(self) -> None:
        # 空 facet_overlap = 检索侧一项都没打出来，"新颖"没有证据支撑，不能靠均值 0.0 蒙混
        # 过关；补一轮检索就能修正，所以是 REVISE 而不是 REJECT
        novelty = NoveltyEvidenceReport(
            idea_id="idea-1", nearest_work=[], facet_overlap={},
            coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64, uncertainty=0.9,
        )
        decision = hard_gate(_structural(True), _falsifiability(True), novelty, _skeptic(), _plan())
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("novelty_ok", decision.blocking_factor)

    def test_novelty_evidence_records_uncertainty_without_gating_on_it(self) -> None:
        # uncertainty 必须出现在证据文案里供审计，但高 uncertainty 本身不改变 verdict
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(uncertainty=0.95),
                              _skeptic(), _plan())
        novelty_score = next(s for s in decision.item_scores if s.item == "novelty_ok")
        self.assertIn("uncertainty=0.95", novelty_score.evidence)
        self.assertEqual(GateVerdict.PASS, decision.verdict)

    def test_reject_when_novelty_overlap_too_high(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(overlap=0.9), _skeptic(), _plan())
        self.assertEqual(GateVerdict.REJECT, decision.verdict)
        self.assertEqual("novelty_ok", decision.blocking_factor)

    def test_reject_when_skeptic_finds_fatal_flaw(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                              _skeptic(fatal=True), _plan())
        self.assertEqual(GateVerdict.REJECT, decision.verdict)

    def test_revise_when_unaddressed_risks_exceed_tolerance_without_fatal_flaw(self) -> None:
        risks = ["confound"] * (MAX_TOLERATED_RISKS + 1)
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                              _skeptic(risks=risks), _plan())
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("risk_ok", decision.blocking_factor)

    def test_pass_when_unaddressed_risks_within_tolerance(self) -> None:
        # 反方审阅几乎总能挑出一两条风险；阈值以内不应拦住候选，否则没有候选能走到排序步骤
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(),
                              _skeptic(risks=["minor_confound"]), _plan())
        self.assertEqual(GateVerdict.PASS, decision.verdict)
        self.assertIsNone(decision.blocking_factor)

    def test_exploratory_when_no_verifier_matched(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(), _skeptic(),
                              _plan(verifier=None))
        self.assertEqual(GateVerdict.EXPLORATORY, decision.verdict)
        self.assertEqual("verifier_ok", decision.blocking_factor)

    def test_rejects_mismatched_idea_ids(self) -> None:
        with self.assertRaises(ValueError):
            hard_gate(_structural(True), _falsifiability(True), _novelty(idea_id="idea-2"),
                      _skeptic(), _plan())

    def test_item_scores_carry_evidence(self) -> None:
        decision = hard_gate(_structural(True), _falsifiability(True), _novelty(), _skeptic(), _plan())
        for item_score in decision.item_scores:
            self.assertTrue(item_score.evidence)
