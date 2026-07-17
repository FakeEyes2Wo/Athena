"""Unit tests for the Idea Generation P0 Pydantic models."""

import unittest

from pydantic import ValidationError

from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    FalsifiabilityReport,
    HypothesisDraft,
)


def _premise(**overrides) -> dict:
    defaults = dict(claim="Y correlates with X", role=ClaimRole.SUPPORTED_PREMISE,
                     supporting_refs=["ev-0"])
    defaults.update(overrides)
    return defaults


def _draft(**overrides) -> dict:
    defaults = dict(
        statement="X causally increases Y",
        intervention="Knock out X",
        expected_effect="Y decreases",
        generation_strategy="single_strategy_v1",
        supported_premises=[ClaimEvidence(**_premise())],
        inference_chain=[],
        predicted_observations=["Y decreases after knockout"],
        disconfirming_observations=["Y stays flat after knockout"],
    )
    defaults.update(overrides)
    return defaults


class ClaimEvidenceTest(unittest.TestCase):
    def test_supported_premise_requires_evidence_refs(self) -> None:
        with self.assertRaises(ValidationError):
            ClaimEvidence(**_premise(supporting_refs=[]))

    def test_novel_hypothesis_must_not_carry_evidence_refs(self) -> None:
        with self.assertRaises(ValidationError):
            ClaimEvidence(claim="X causes Y", role=ClaimRole.NOVEL_HYPOTHESIS,
                           supporting_refs=["ev-0"])

    def test_well_formed_premise_is_accepted(self) -> None:
        claim = ClaimEvidence(**_premise())
        self.assertEqual(["ev-0"], claim.supporting_refs)


class HypothesisDraftTest(unittest.TestCase):
    def test_requires_predicted_observations(self) -> None:
        with self.assertRaises(ValidationError):
            HypothesisDraft(**_draft(predicted_observations=[]))

    def test_requires_disconfirming_observations(self) -> None:
        with self.assertRaises(ValidationError):
            HypothesisDraft(**_draft(disconfirming_observations=[]))

    def test_well_formed_draft_is_accepted(self) -> None:
        draft = HypothesisDraft(**_draft())
        self.assertEqual("X causally increases Y", draft.statement)


class FalsifiabilityJudgmentTest(unittest.TestCase):
    def test_falsifiable_requires_non_empty_testable_implication(self) -> None:
        with self.assertRaises(ValidationError):
            FalsifiabilityJudgment(
                is_falsifiable=True, testable_implication="", unobservable_variables=[]
            )

    def test_falsifiable_rejects_whitespace_only_testable_implication(self) -> None:
        with self.assertRaises(ValidationError):
            FalsifiabilityJudgment(
                is_falsifiable=True, testable_implication="   ", unobservable_variables=[]
            )

    def test_well_formed_falsifiable_judgment_is_accepted(self) -> None:
        judgment = FalsifiabilityJudgment(
            is_falsifiable=True,
            testable_implication="Measure Y after manipulating X",
            unobservable_variables=[],
        )
        self.assertTrue(judgment.is_falsifiable)

    def test_unfalsifiable_judgment_allows_empty_testable_implication(self) -> None:
        judgment = FalsifiabilityJudgment(
            is_falsifiable=False, testable_implication="", unobservable_variables=["motivation"]
        )
        self.assertFalse(judgment.is_falsifiable)


class FalsifiabilityReportTest(unittest.TestCase):
    def test_falsifiable_requires_non_empty_testable_implication(self) -> None:
        with self.assertRaises(ValidationError):
            FalsifiabilityReport(
                idea_id="idea-1",
                is_falsifiable=True,
                testable_implication="",
                unobservable_variables=[],
            )

    def test_well_formed_falsifiable_report_is_accepted(self) -> None:
        report = FalsifiabilityReport(
            idea_id="idea-1",
            is_falsifiable=True,
            testable_implication="Measure Y after manipulating X",
            unobservable_variables=[],
        )
        self.assertEqual("idea-1", report.idea_id)

    def test_unfalsifiable_report_allows_empty_testable_implication(self) -> None:
        report = FalsifiabilityReport(
            idea_id="idea-1",
            is_falsifiable=False,
            testable_implication="",
            unobservable_variables=["motivation"],
        )
        self.assertFalse(report.is_falsifiable)
