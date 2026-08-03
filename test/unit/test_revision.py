"""Unit tests for the REVISE debate loop."""

import tempfile
import unittest

from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.search.evidence_retrieval import build_novelty_question
from athena.workflows.search.idea_schemas import (
    ClaimEvidence, ClaimRole, GATE_RUBRIC_VERSION, GateDecision, GateVerdict,
    HypothesisPackage, NoveltyEvidenceReport, RevisionDraft, SkepticReport,
)
from athena.workflows.search.review_board import REVIEW_PERSPECTIVES, build_perspective_input
from athena.workflows.search.revision import (
    build_revision_prompt, is_no_op_revision, is_revisable, novelty_is_stale,
    revise_candidate, select_debate_opponent, stale_perspectives,
)
from unit.fakes import make_routed_model

_FAKE_CORPUS_REF = "sha256:" + "c" * 64


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


async def _novelty(store: LocalArtifactStore, package: HypothesisPackage) -> NoveltyEvidenceReport:
    # 模块级 helper（而非 StalenessTest 的方法）：Task 9 的测试需要复用同一份构造逻辑。
    return NoveltyEvidenceReport(
        idea_id="idea-1", nearest_work=[], facet_overlap={"problem": 0.1},
        coverage_ref=_FAKE_CORPUS_REF, temporal_ref=_FAKE_CORPUS_REF,
        query_log_ref=await store.put_text("old transcript"),
        input_ref=await store.put_text(build_novelty_question(package, _FAKE_CORPUS_REF)),
        uncertainty=0.2,
    )


async def _reviews_with_refs(
    store: LocalArtifactStore, package: HypothesisPackage
) -> list[SkepticReport]:
    # 模块级 helper：与 _novelty 同理，供 Task 9 直接复用。
    out = []
    for perspective in REVIEW_PERSPECTIVES:
        prompt = build_perspective_input(
            package, perspective, corpus_ref=_FAKE_CORPUS_REF,
            prior_transcript="old transcript")
        out.append(SkepticReport(
            idea_id="idea-1", perspective=perspective.perspective_id, critique="c",
            unaddressed_risks=[], fatal_flaw_found=False,
            input_ref=await store.put_text(prompt),
        ))
    return out


def _decision(verdict: GateVerdict, blocking_factor: str | None) -> GateDecision:
    return GateDecision(idea_id="idea-1", gate_phase="full", verdict=verdict,
                        rubric_version=GATE_RUBRIC_VERSION, item_scores=[],
                        blocking_factor=blocking_factor)


class RevisionEligibilityTest(unittest.TestCase):
    def test_risk_items_are_revisable(self) -> None:
        self.assertTrue(is_revisable(_decision(GateVerdict.REVISE, "risk_ok_methodology")))
        self.assertTrue(is_revisable(_decision(GateVerdict.REVISE, "risk_total")))

    def test_novelty_ok_is_excluded(self) -> None:
        # 该分支只由 facet_overlap 为空触发 = 检索侧失败，改假设文本不会让检索恢复
        self.assertFalse(is_revisable(_decision(GateVerdict.REVISE, "novelty_ok")))

    def test_non_revise_verdicts_are_excluded(self) -> None:
        for verdict in (GateVerdict.PASS, GateVerdict.REJECT, GateVerdict.EXPLORATORY):
            self.assertFalse(is_revisable(_decision(verdict, "risk_ok_methodology")))


class DebateOpponentTest(unittest.TestCase):
    def test_risk_ok_item_names_its_own_opponent(self) -> None:
        self.assertEqual("statistics",
                         select_debate_opponent("risk_ok_statistics", _reviews()))

    def test_risk_total_picks_the_noisiest_perspective(self) -> None:
        reviews = [
            SkepticReport(idea_id="idea-1", perspective="methodology", critique="c",
                          unaddressed_risks=["a"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="statistics", critique="c",
                          unaddressed_risks=["a", "b", "c"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="domain_consistency", critique="c",
                          unaddressed_risks=["a"], fatal_flaw_found=False),
        ]
        self.assertEqual("statistics", select_debate_opponent("risk_total", reviews))

    def test_ties_break_by_review_perspectives_order_not_caller_order(self) -> None:
        # 确定性：同样输入必须选出同一个对手。刻意用倒序传入，证明函数内部按
        # REVIEW_PERSPECTIVES 重排，而不是听凭调用方（gather 入参顺序）的顺序。
        tied = [
            SkepticReport(idea_id="idea-1", perspective="domain_consistency", critique="c",
                          unaddressed_risks=["a", "b"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="statistics", critique="c",
                          unaddressed_risks=["a", "b"], fatal_flaw_found=False),
            SkepticReport(idea_id="idea-1", perspective="methodology", critique="c",
                          unaddressed_risks=["a", "b"], fatal_flaw_found=False),
        ]
        self.assertEqual("methodology", select_debate_opponent("risk_total", tied))


class NoOpGuardTest(unittest.TestCase):
    def test_identical_content_is_a_no_op(self) -> None:
        same = RevisionDraft(
            rebuttal="nothing really changed", changes_made=[],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=_package().supported_premises,
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=_package().disconfirming_observations,
        )
        self.assertTrue(is_no_op_revision(same, _package()))

    def test_disconfirmer_only_change_is_NOT_a_no_op(self) -> None:
        # 判定域必须是四个 revised_* 字段，不是 novel_hypothesis 的相似度。按后者判定，
        # 最典型的修订（只补 disconfirmer，假设文本一字未动）会被当成噪声掐死，
        # 对手一次都不会被叫到，主路径直接失效。
        draft = RevisionDraft(
            rebuttal="added an explicit disconfirmer", changes_made=["added disconfirmer"],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=_package().supported_premises,
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=["Y flat versus matched control"],
        )
        self.assertFalse(is_no_op_revision(draft, _package()))

    def test_premise_only_change_is_NOT_a_no_op(self) -> None:
        draft = RevisionDraft(
            rebuttal="bound a second evidence ref", changes_made=["added ref"],
            revised_novel_hypothesis=_package().novel_hypothesis,
            revised_premises=[ClaimEvidence(claim="X correlates with Y",
                                            role=ClaimRole.SUPPORTED_PREMISE,
                                            supporting_refs=["ev-0", "ev-1"])],
            revised_predicted_observations=_package().predicted_observations,
            revised_disconfirming_observations=_package().disconfirming_observations,
        )
        self.assertFalse(is_no_op_revision(draft, _package()))


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


class StalenessTest(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_package_is_not_stale_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            self.assertFalse(await novelty_is_stale(
                package, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(set(), await stale_perspectives(
                package, reviews, artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="old transcript"))

    async def test_changed_hypothesis_makes_novelty_and_all_perspectives_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(update={"novel_hypothesis": "Z causes Y"})
            self.assertTrue(await novelty_is_stale(
                revised, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(
                {"methodology", "statistics", "domain_consistency"},
                await stale_perspectives(revised, reviews, artifacts=store,
                                         corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))

    async def test_disconfirmer_only_revision_spares_both_retrieval_reports(self) -> None:
        # 成本论证的支点：典型修订必须**证明性地**不触发 novelty 与 domain_consistency，
        # 那是仅有的两个 retrieval loop。这条一旦失守，整轮设计的成本前提就没了。
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = await _novelty(store, package)
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(
                update={"disconfirming_observations": ["Y flat versus matched control"]})
            self.assertFalse(await novelty_is_stale(
                revised, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(
                {"methodology", "statistics"},
                await stale_perspectives(revised, reviews, artifacts=store,
                                         corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))

    async def test_new_transcript_cascades_to_domain_consistency(self) -> None:
        # 级联：修订只改 predicted_observations，domain_consistency 自身字段（novel_hypothesis）
        # 没变，但 novelty 重跑换了转录，它必须跟着 stale。
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            reviews = await _reviews_with_refs(store, package)
            revised = package.model_copy(update={"predicted_observations": ["Y doubles"]})
            stale = await stale_perspectives(
                revised, reviews, artifacts=store, corpus_ref=_FAKE_CORPUS_REF,
                prior_transcript="freshly retrieved transcript")   # 刷新后的转录
            self.assertIn("domain_consistency", stale)

    async def test_missing_fingerprint_is_always_stale(self) -> None:
        # fail-closed：失败降级的报告没有可信指纹，无法证明未失效就不能复用
        with tempfile.TemporaryDirectory() as tmp:
            store, package = LocalArtifactStore(tmp), _package()
            novelty = (await _novelty(store, package)).model_copy(
                update={"input_ref": None})
            reviews = [r.model_copy(update={"input_ref": None})
                       for r in await _reviews_with_refs(store, package)]
            self.assertTrue(await novelty_is_stale(
                package, novelty, artifacts=store, corpus_ref=_FAKE_CORPUS_REF))
            self.assertEqual(
                {"methodology", "statistics", "domain_consistency"},
                await stale_perspectives(package, reviews, artifacts=store,
                                         corpus_ref=_FAKE_CORPUS_REF,
                                         prior_transcript="old transcript"))
