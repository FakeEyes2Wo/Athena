"""Unit tests for the REVISE debate loop."""

import unittest

from athena.workflows.search.idea_schemas import (
    ClaimEvidence, ClaimRole, HypothesisPackage, RevisionDraft, SkepticReport,
)
from athena.workflows.search.revision import build_revision_prompt, revise_candidate
from unit.fakes import make_routed_model


def _package(sampling_probability: float = 0.42) -> HypothesisPackage:
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=sampling_probability,
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[], predicted_observations=["Y increases"],
        disconfirming_observations=["Y flat"], lineage_op="generate",
    )


def _reviews() -> list[SkepticReport]:
    return [
        SkepticReport(idea_id="idea-1", perspective="methodology", critique="no control arm",
                      unaddressed_risks=["confound"], fatal_flaw_found=False),
        SkepticReport(idea_id="idea-1", perspective="statistics", critique="underpowered",
                      unaddressed_risks=["n too small"], fatal_flaw_found=False),
        SkepticReport(idea_id="idea-1", perspective="domain_consistency",
                      critique="mechanism plausible", unaddressed_risks=[], fatal_flaw_found=False),
    ]


def _draft() -> RevisionDraft:
    return RevisionDraft(
        rebuttal="a matched control arm is now specified",
        changes_made=["added control arm"],
        revised_novel_hypothesis="X causes Y relative to a matched control",
        revised_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        revised_predicted_observations=["Y increases versus control"],
        revised_disconfirming_observations=["Y flat versus control"],
    )


class RevisionPromptTest(unittest.TestCase):
    def test_prompt_carries_blocking_item_and_all_three_critiques(self) -> None:
        prompt = build_revision_prompt(
            _package(), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[])
        self.assertIn("risk_ok_methodology", prompt)
        self.assertIn("no control arm", prompt)      # 被拦视角
        self.assertIn("underpowered", prompt)        # 另外两个视角，防"修好 A 弄坏 B"
        self.assertIn("mechanism plausible", prompt)

    def test_prompt_never_leaks_sampling_probability(self) -> None:
        # 泄漏面一：prompt。reviser 全程看不到这个数，rebuttal 才不可能复述它
        prompt = build_revision_prompt(
            _package(sampling_probability=0.42), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[])
        self.assertNotIn("0.42", prompt)
        self.assertNotIn("sampling_probability", prompt)


class ReviseCandidateTest(unittest.IsolatedAsyncioTestCase):
    async def test_revised_package_keeps_id_and_probability_bumps_round(self) -> None:
        model = make_routed_model({"Blocking rubric item:": _draft()})
        revised, draft = await revise_candidate(
            _package(), blocking_factor="risk_ok_methodology",
            debated_perspective="methodology", reviews=_reviews(), prior_rounds=[], model=model)
        self.assertEqual("idea-1", revised.idea_id)             # hard_gate 要求全套报告同 id
        self.assertEqual(0.42, revised.sampling_probability)    # 代码搬运，不向 LLM 索要
        self.assertEqual(1, revised.revision_round)
        self.assertEqual("revise", revised.lineage_op)
        self.assertEqual("X causes Y relative to a matched control", revised.novel_hypothesis)
        self.assertEqual("a matched control arm is now specified", draft.rebuttal)

    async def test_invalid_revision_raises_and_validator_is_not_relaxed(self) -> None:
        # 空 disconfirmers 构造不出合法 HypothesisPackage —— 这是预期路径，按 reviser 失败处理，
        # 绝不放宽校验器（那两条不变量是 evidence_traceable 这项 rubric 的结构基础）
        bad = _draft().model_copy(update={"revised_disconfirming_observations": []})
        model = make_routed_model({"Blocking rubric item:": bad})
        with self.assertRaises(ValueError):
            await revise_candidate(
                _package(), blocking_factor="risk_ok_methodology",
                debated_perspective="methodology", reviews=_reviews(), prior_rounds=[], model=model)
