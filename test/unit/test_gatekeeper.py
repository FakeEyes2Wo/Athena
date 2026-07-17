"""Unit tests for pre_gate."""

import unittest

from athena.workflows.search.gatekeeper import pre_gate
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GateVerdict,
    StructuralCheckReport,
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
