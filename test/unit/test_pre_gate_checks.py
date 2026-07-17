"""Unit tests for structural_check and falsifiability_check."""

import unittest

from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    HypothesisPackage,
)
from athena.workflows.search.pre_gate_checks import falsifiability_check, structural_check
from unit.fakes import FakeChatModel


def _make_package(**overrides) -> HypothesisPackage:
    defaults = dict(
        idea_id="idea-1",
        generation_strategy="single_strategy_v1",
        novel_hypothesis="X causes Y",
        supported_premises=[
            ClaimEvidence(claim="Y correlates with X", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[],
        predicted_observations=["Y increases when X increases"],
        disconfirming_observations=["Y stays flat when X increases"],
        lineage_op="generate",
    )
    defaults.update(overrides)
    return HypothesisPackage(**defaults)


class StructuralCheckTest(unittest.TestCase):
    def test_well_formed_package_passes(self) -> None:
        report = structural_check(_make_package())
        self.assertTrue(report.premise_evidence_ok)
        self.assertTrue(report.novel_hypothesis_testable)
        self.assertEqual([], report.violations)
        self.assertEqual("idea-1", report.idea_id)

    def test_zero_premises_does_not_vacuously_pass(self) -> None:
        # all([]) 恒真，若不显式排除该情况,零证据前提会被误判为"证据可追溯"
        report = structural_check(_make_package(supported_premises=[]))
        self.assertFalse(report.premise_evidence_ok)
        self.assertIn("premise_missing_evidence", report.violations)

    def test_non_premise_roles_are_excluded_from_evidence_check(self) -> None:
        # DERIVED_INFERENCE 没有绑证据的不变量,即便 supporting_refs 为空也不该拖累
        # premise_evidence_ok,只要真正的 SUPPORTED_PREMISE 都绑了证据
        package = _make_package(
            supported_premises=[
                ClaimEvidence(claim="Y correlates with X", role=ClaimRole.SUPPORTED_PREMISE,
                              supporting_refs=["ev-0"]),
                ClaimEvidence(claim="derived claim", role=ClaimRole.DERIVED_INFERENCE,
                              supporting_refs=[]),
            ]
        )
        report = structural_check(package)
        self.assertTrue(report.premise_evidence_ok)
        self.assertNotIn("premise_missing_evidence", report.violations)


class FalsifiabilityCheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_falsifiable_judgment_maps_to_report(self) -> None:
        fake = FakeChatModel([
            FalsifiabilityJudgment(
                testable_implication="Measure Y after manipulating X",
                unobservable_variables=[],
                is_falsifiable=True,
            )
        ])
        report = await falsifiability_check(_make_package(), model=fake)
        self.assertTrue(report.is_falsifiable)
        self.assertEqual("idea-1", report.idea_id)

    async def test_unfalsifiable_judgment_maps_to_report(self) -> None:
        fake = FakeChatModel([
            FalsifiabilityJudgment(
                testable_implication="",
                unobservable_variables=["internal motivation"],
                is_falsifiable=False,
            )
        ])
        report = await falsifiability_check(_make_package(), model=fake)
        self.assertFalse(report.is_falsifiable)
        self.assertEqual(["internal motivation"], report.unobservable_variables)
