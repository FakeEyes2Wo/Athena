"""Unit tests for the Idea Generation P0 Pydantic models."""

import unittest

from pydantic import ValidationError

from athena.workflows.search.idea_schemas import (
    FACETS,
    GATE_RUBRIC_VERSION,
    ClaimEvidence,
    ClaimRole,
    EvidenceRef,
    FalsifiabilityJudgment,
    FalsifiabilityReport,
    GapCandidate,
    GapCandidateDraft,
    GapMiningResponse,
    GateDecision,
    GateVerdict,
    HypothesisDraft,
    HypothesisPackage,
    NoveltyEvidenceJudgment,
    NoveltyEvidenceReport,
    PairwiseJudgment,
    PipelineCandidateResult,
    RevisionDraft,
    RevisionRound,
    RetrievalCoverage,
    SkepticJudgment,
    SkepticReport,
    StructuralCheckReport,
    TemporalIntegrity,
    ValidationPlan,
    VerbalizedSamplingResponse,
    VerifierSpec,
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


def _package(**overrides) -> HypothesisPackage:
    defaults = dict(
        idea_id="idea-1", generation_strategy="single_strategy_v1", novel_hypothesis="n",
        supported_premises=[], inference_chain=[], predicted_observations=["p"],
        disconfirming_observations=["d"], lineage_op="generate",
    )
    defaults.update(overrides)
    return HypothesisPackage(**defaults)


def _pipeline_result(**overrides) -> PipelineCandidateResult:
    defaults = dict(
        package=_package(),
        structural=StructuralCheckReport(idea_id="idea-1", premise_evidence_ok=True,
                                        novel_hypothesis_testable=True),
        falsifiability=FalsifiabilityReport(idea_id="idea-1", testable_implication="t",
                                           unobservable_variables=[], is_falsifiable=True),
        decision=GateDecision(idea_id="idea-1", gate_phase="full", verdict=GateVerdict.REVISE,
                              rubric_version=GATE_RUBRIC_VERSION, item_scores=[]),
    )
    defaults.update(overrides)
    return PipelineCandidateResult(**defaults)


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


class HypothesisDraftSamplingProbabilityTest(unittest.TestCase):
    def test_defaults_to_one_when_omitted(self) -> None:
        draft = HypothesisDraft(**_draft())
        self.assertEqual(1.0, draft.sampling_probability)

    def test_accepts_explicit_value(self) -> None:
        draft = HypothesisDraft(**_draft(sampling_probability=0.4))
        self.assertEqual(0.4, draft.sampling_probability)

    def test_rejects_out_of_range_value(self) -> None:
        with self.assertRaises(ValidationError):
            HypothesisDraft(**_draft(sampling_probability=1.5))


class HypothesisPackageSamplingProbabilityTest(unittest.TestCase):
    def test_defaults_to_one_when_omitted(self) -> None:
        package = HypothesisPackage(
            idea_id="idea-1", generation_strategy="s", novel_hypothesis="n",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        self.assertEqual(1.0, package.sampling_probability)


class GapCandidateTest(unittest.TestCase):
    def test_gap_mining_response_defaults_to_empty_list(self) -> None:
        self.assertEqual([], GapMiningResponse().gaps)

    def test_draft_requires_known_gap_type(self) -> None:
        with self.assertRaises(ValidationError):
            GapCandidateDraft(gap_type="unknown_type", description="d")

    def test_full_gap_candidate_carries_context_ref(self) -> None:
        gap = GapCandidate(
            gap_id="gap-1", gap_type="open_problem", context_ref="sha256:" + "a" * 64,
            description="nobody has tested this in vivo",
        )
        self.assertEqual("open_problem", gap.gap_type)


class NoveltyEvidenceModelsTest(unittest.TestCase):
    def _judgment(self, **overrides) -> NoveltyEvidenceJudgment:
        defaults = dict(
            nearest_work=[EvidenceRef(ref_id="ev-1", kind="paper")],
            facet_overlap={"problem": 0.5, "method": 0.2},
            coverage_estimate=0.6, unrecalled_risk=0.3,
            citation_cutoff_ok=True, retrieval_cutoff_ok=True,
            post_cutoff_similarity=0.1, possible_memorization=False,
            leakage_risk=0.1, historical_backtest_validity=True, uncertainty=0.2,
        )
        defaults.update(overrides)
        return NoveltyEvidenceJudgment(**defaults)

    def test_well_formed_judgment_is_accepted(self) -> None:
        judgment = self._judgment()
        self.assertEqual(0.5, judgment.facet_overlap["problem"])

    def test_rejects_unknown_facet_keys(self) -> None:
        with self.assertRaises(ValidationError):
            self._judgment(facet_overlap={"not_a_facet": 0.5})

    def test_facets_constant_matches_expected_six(self) -> None:
        self.assertEqual(
            ("problem", "mechanism", "method", "data", "experiment", "conclusion"), FACETS,
        )

    def test_report_has_no_verdict_field(self) -> None:
        report = NoveltyEvidenceReport(
            idea_id="idea-1", nearest_work=[], facet_overlap={},
            coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64, uncertainty=0.2,
        )
        self.assertNotIn("verdict", type(report).model_fields)


class ValidationModelsTest(unittest.TestCase):
    def test_validation_plan_allows_none_verifier_for_exploratory(self) -> None:
        plan = ValidationPlan(
            idea_id="idea-1", minimal_test="none available", verifier=None,
            decision_rule="EXPLORATORY", estimated_cost_ref="sha256:" + "a" * 64,
        )
        self.assertIsNone(plan.verifier)

    def test_verifier_spec_round_trips(self) -> None:
        spec = VerifierSpec(
            verifier_type="ablation_replication", applicable_domains=["ai4s"],
            observable_vars=["metric"], statistical_assumptions=["same seed budget"],
            success_condition="s", failure_condition="f", inconclusive_condition="i",
            cost_ref="sha256:" + "a" * 64, supports_auto_exec=True, requires_human_approval=False,
        )
        self.assertTrue(spec.supports_auto_exec)


class SkepticModelsTest(unittest.TestCase):
    def test_judgment_and_report_share_shape(self) -> None:
        judgment = SkepticJudgment(critique="weak premise", unaddressed_risks=["confound"],
                                    fatal_flaw_found=False)
        report = SkepticReport(idea_id="idea-1", perspective="methodology", **judgment.model_dump())
        self.assertEqual(["confound"], report.unaddressed_risks)


class VerbalizedSamplingResponseTest(unittest.TestCase):
    def test_wraps_multiple_drafts(self) -> None:
        response = VerbalizedSamplingResponse(candidates=[
            HypothesisDraft(**_draft(sampling_probability=0.7)),
            HypothesisDraft(**_draft(sampling_probability=0.3)),
        ])
        self.assertEqual(2, len(response.candidates))


class PairwiseJudgmentTest(unittest.TestCase):
    def test_winner_must_be_a_or_b(self) -> None:
        with self.assertRaises(ValidationError):
            PairwiseJudgment(winner="candidate_c", rationale="r")

    def test_well_formed_judgment_is_accepted(self) -> None:
        judgment = PairwiseJudgment(winner="candidate_a", rationale="more falsifiable")
        self.assertEqual("candidate_a", judgment.winner)


class PipelineCandidateResultTest(unittest.TestCase):
    def test_novelty_reviews_and_plan_default_to_empty(self) -> None:
        package = HypothesisPackage(
            idea_id="idea-1", generation_strategy="s", novel_hypothesis="n",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        structural = StructuralCheckReport(idea_id="idea-1", premise_evidence_ok=False,
                                            novel_hypothesis_testable=True,
                                            violations=["premise_missing_evidence"])
        falsifiability = FalsifiabilityReport(idea_id="idea-1", testable_implication="t",
                                               unobservable_variables=[], is_falsifiable=True)
        decision = GateDecision(idea_id="idea-1", gate_phase="pre_gate", verdict=GateVerdict.REVISE,
                                 rubric_version=GATE_RUBRIC_VERSION, item_scores=[],
                                 blocking_factor="evidence_traceable")
        result = PipelineCandidateResult(package=package, structural=structural,
                                          falsifiability=falsifiability, decision=decision)
        self.assertIsNone(result.novelty)
        self.assertEqual([], result.reviews)
        self.assertIsNone(result.validation_plan)


class RevisionSchemaTest(unittest.TestCase):
    def test_revision_draft_has_no_sampling_probability_field(self) -> None:
        # schema 泄漏面：不依赖任何运行时路径，有人加字段当场红
        self.assertNotIn("sampling_probability", RevisionDraft.model_fields)

    def test_revision_draft_carries_rebuttal_and_changes(self) -> None:
        draft = RevisionDraft(
            rebuttal="the control group is now explicit",
            changes_made=["added a matched control arm"],
            revised_novel_hypothesis="X causes Y under Z",
            revised_premises=[], revised_predicted_observations=["Y increases"],
            revised_disconfirming_observations=["Y flat"],
        )
        self.assertEqual("the control group is now explicit", draft.rebuttal)

    def test_revision_round_debated_perspective_is_required_and_not_optional(self) -> None:
        # 设计 §3.2：可达入口只有 risk_ok_<p> 与 risk_total，两者都有对手，没有 None 的生产者
        self.assertIs(str, RevisionRound.model_fields["debated_perspective"].annotation)

    def test_revision_round_is_not_a_verdict(self) -> None:
        # 除 GateDecision 外任何报告类模型都不带 verdict
        self.assertNotIn("verdict", RevisionRound.model_fields)

    def test_package_defaults_to_revision_round_zero(self) -> None:
        self.assertEqual(0, _package().revision_round)

    def test_pipeline_result_defaults_to_no_revisions(self) -> None:
        result = _pipeline_result()
        self.assertEqual([], result.revisions)
        self.assertIsNone(result.revision_blocking_factor)
