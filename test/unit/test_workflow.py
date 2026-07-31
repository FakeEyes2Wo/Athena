"""Unit tests for the pre_gate async pipeline (run_pre_gate)."""

import tempfile
import unittest

from athena.core.agent import Agent, AgentConfig, StreamEvent
from athena.core.tool import ToolRegistry
from athena.storage import LocalArtifactStore
from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    GapMiningResponse,
    GateVerdict,
    HypothesisDraft,
    HypothesisPackage,
    NoveltyEvidenceJudgment,
    PairwiseJudgment,
    ResearchProblemInput,
    SkepticJudgment,
    VerbalizedSamplingResponse,
)
from athena.workflows.search.workflow import pairwise_compare, run_full_pipeline, run_pre_gate
from unit.fakes import make_scripted_model

# 占位 corpus_ref：这组测试只验证编排/门控/排序逻辑,不重复 Task 6 已经覆盖过的真实 paper_rag
# 检索集成,所以用一个格式合法但不指向真实内容的 sha256 引用即可。
_FAKE_CORPUS_REF = "sha256:" + "c" * 64


class _StaticTextProvider:
    """总是回放固定文本、不调用工具的假 provider,供 run_full_pipeline 集成测试复用。"""

    def __init__(self, text: str = "no notable findings") -> None:
        self._text = text

    async def stream(self, *_args):
        yield StreamEvent("text_delta", {"delta": self._text, "accumulated": self._text})
        yield StreamEvent("response_completed")


def _build_retrieval_agent() -> Agent:
    agent = Agent(AgentConfig(model="test-model", system_prompt="system", tools=ToolRegistry()))
    agent._provider = _StaticTextProvider()
    return agent


def _problem() -> ResearchProblemInput:
    return ResearchProblemInput(
        question="Does X affect Y?",
        domain="biology",
        objective="find a testable mechanism",
        evidence_texts=["Prior study found X correlates with Y in mice."],
    )


def _valid_draft() -> HypothesisDraft:
    return HypothesisDraft(
        statement="X causally increases Y",
        intervention="Knock out X in a cell line",
        expected_effect="Y decreases relative to control",
        generation_strategy="single_strategy_v1",
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y in mice", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[],
        predicted_observations=["Y decreases after X knockout"],
        disconfirming_observations=["Y stays the same after X knockout"],
    )


def _second_valid_draft() -> HypothesisDraft:
    """与 _valid_draft 主张完全不同的第二个候选，保证 deduplicate_candidates 不会把两者合并。"""
    return HypothesisDraft(
        statement="Chronic heat stress suppresses ribosome biogenesis in root meristems",
        intervention="Expose seedlings to 37C for six hours and profile nascent rRNA",
        expected_effect="Nascent rRNA abundance drops relative to ambient-temperature controls",
        generation_strategy="verbalized_sampling_v1",
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y in mice", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[],
        predicted_observations=["Nascent rRNA abundance drops under heat stress"],
        disconfirming_observations=["Nascent rRNA abundance is unchanged under heat stress"],
    )


def _clean_novelty_judgment() -> NoveltyEvidenceJudgment:
    """低 facet_overlap、无时间泄漏风险的数值性审计结果，用来让候选顺利通过 hard_gate。"""
    return NoveltyEvidenceJudgment(
        nearest_work=[], facet_overlap={"problem": 0.1}, coverage_estimate=0.5,
        unrecalled_risk=0.2, citation_cutoff_ok=True, retrieval_cutoff_ok=True,
        post_cutoff_similarity=0.1, possible_memorization=False, leakage_risk=0.1,
        historical_backtest_validity=True, uncertainty=0.2,
    )


def _falsifiability_judgment(ok: bool) -> FalsifiabilityJudgment:
    if ok:
        return FalsifiabilityJudgment(
            testable_implication="Measure Y after X knockout",
            unobservable_variables=[],
            is_falsifiable=True,
        )
    return FalsifiabilityJudgment(
        testable_implication="",
        unobservable_variables=["internal state"],
        is_falsifiable=False,
    )


class RunPreGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_pass_end_to_end(self) -> None:
        model = make_scripted_model([_valid_draft(), _falsifiability_judgment(True)])
        node, package, decision = await run_pre_gate(_problem(), model=model)
        self.assertEqual(GateVerdict.PASS, decision.verdict)
        self.assertEqual("X causally increases Y", node.statement)
        self.assertEqual(package.idea_id, decision.idea_id)

    async def test_revise_when_falsifiability_fails(self) -> None:
        model = make_scripted_model([_valid_draft(), _falsifiability_judgment(False)])
        _, _, decision = await run_pre_gate(_problem(), model=model)
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("falsifiable", decision.blocking_factor)

    async def test_retries_once_then_succeeds(self) -> None:
        model = make_scripted_model([
            ValueError("bad json"), _valid_draft(), _falsifiability_judgment(True),
        ])
        node, package, decision = await run_pre_gate(_problem(), model=model)
        self.assertEqual(package.idea_id, decision.idea_id)
        self.assertEqual(GateVerdict.PASS, decision.verdict)

    async def test_raises_after_exhausting_generation_retries(self) -> None:
        model = make_scripted_model([ValueError("bad json"), ValueError("bad json again")])
        with self.assertRaises(ValueError):
            await run_pre_gate(_problem(), model=model)


class PairwiseCompareTest(unittest.IsolatedAsyncioTestCase):
    async def test_agreement_between_forward_and_backward_picks_that_winner(self) -> None:
        package_a = HypothesisPackage(
            idea_id="idea-a", generation_strategy="s", novel_hypothesis="X causes Y",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        package_b = HypothesisPackage(
            idea_id="idea-b", generation_strategy="s", novel_hypothesis="Z inhibits W",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        # forward 调用里 candidate_a=package_a、candidate_b=package_b；backward 调用里
        # candidate_a=package_b、candidate_b=package_a（顺序对调）。forward 选 candidate_a
        # 即 package_a 获胜；backward 选 candidate_b，backward 的 candidate_b 就是
        # package_a，所以两次调用其实都指向 package_a 获胜——用来验证一致同意路径。
        model = make_scripted_model([
            PairwiseJudgment(winner="candidate_a", rationale="a wins forward"),
            PairwiseJudgment(winner="candidate_b", rationale="a still wins backward"),
        ])
        comparison = await pairwise_compare(package_a, package_b, model=model)
        self.assertEqual("idea-a", comparison.winner_id)
        # 仅断言 winner_id 无法区分"双向一致"与"双向分歧但 forward 结果凑巧相同"这两种情况
        # （production 代码在两个分支里都会把 winner_id 设成 forward_winner，只有 rationale
        # 会不同）；显式断言 rationale 包含 "agree" 且不包含 "disagreement"，这样今后若
        # backward 映射逻辑回归（例如忘记 backward 的 candidate_a 对应 package_b），即使
        # winner_id 恰好没变，这条测试也能通过 rationale 的变化捕捉到。
        self.assertIn("agree", comparison.rationale)
        self.assertNotIn("disagreement", comparison.rationale)

    async def test_disagreement_between_forward_and_backward_is_recorded_in_rationale(self) -> None:
        package_a = HypothesisPackage(
            idea_id="idea-a", generation_strategy="s", novel_hypothesis="X causes Y",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        package_b = HypothesisPackage(
            idea_id="idea-b", generation_strategy="s", novel_hypothesis="Z inhibits W",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        # forward 选 candidate_a（即 package_a）获胜；backward 也选 candidate_a，但 backward
        # 调用里 candidate_a 对应的是 package_b，所以 backward 实际上是 package_b 获胜——
        # 两次调用真正分歧，用来验证"不一致时以正向结果为准，且把分歧写进 rationale"这条规则。
        model = make_scripted_model([
            PairwiseJudgment(winner="candidate_a", rationale="a wins forward"),
            PairwiseJudgment(winner="candidate_a", rationale="b wins backward"),
        ])
        comparison = await pairwise_compare(package_a, package_b, model=model)
        self.assertEqual("idea-a", comparison.winner_id)
        self.assertIn("disagreement", comparison.rationale)


class RunFullPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_single_surviving_candidate_is_ranked_without_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            model = make_scripted_model([
                GapMiningResponse(gaps=[]),
                VerbalizedSamplingResponse(candidates=[_valid_draft()]),
                _falsifiability_judgment(True),
                NoveltyEvidenceJudgment(
                    nearest_work=[], facet_overlap={"problem": 0.1}, coverage_estimate=0.5,
                    unrecalled_risk=0.2, citation_cutoff_ok=True, retrieval_cutoff_ok=True,
                    post_cutoff_similarity=0.1, possible_memorization=False, leakage_risk=0.1,
                    historical_backtest_validity=True, uncertainty=0.2,
                ),
                SkepticJudgment(critique="looks solid", unaddressed_risks=[], fatal_flaw_found=False),
            ])

            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(), novelty_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1, model=model,
            )

            self.assertEqual(1, len(results))
            self.assertEqual("full", results[0].decision.gate_phase)
            self.assertEqual(1, len(ranking))
            self.assertEqual(0, ranking[0].comparisons)
            # validation_plan_ref 必须指向 ValidationPlan 本身，而不是复用成本估计的引用
            plan_ref = results[0].package.validation_plan_ref
            self.assertTrue(plan_ref.startswith("sha256:"))
            self.assertNotEqual(results[0].validation_plan.estimated_cost_ref, plan_ref)
            self.assertIn("minimal_test", await artifacts.get_text(plan_ref))

    async def test_candidate_revised_at_pre_gate_skips_expensive_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            model = make_scripted_model([
                GapMiningResponse(gaps=[]),
                VerbalizedSamplingResponse(candidates=[_valid_draft()]),
                _falsifiability_judgment(False),
            ])

            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(), novelty_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1, model=model,
            )

            self.assertEqual(1, len(results))
            self.assertEqual("pre_gate", results[0].decision.gate_phase)
            self.assertIsNone(results[0].novelty)
            self.assertIsNone(results[0].skeptic)
            self.assertEqual([], ranking)

    async def test_two_surviving_candidates_get_pairwise_compared_and_ranked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            # 脚本化顺序必须与 run_full_pipeline 里 single_turn_chat 的真实调用顺序一致：
            # [2]空白挖掘 → [3]多候选生成 → 候选1([4]可证伪性/[5]数值性/[6]反方审阅) →
            # 候选2(同样三次) → [9]一次 pairwise_compare 内部的正向+反向两次调用。
            model = make_scripted_model([
                GapMiningResponse(gaps=[]),
                VerbalizedSamplingResponse(candidates=[_valid_draft(), _second_valid_draft()]),
                _falsifiability_judgment(True),
                _clean_novelty_judgment(),
                SkepticJudgment(critique="solid", unaddressed_risks=[], fatal_flaw_found=False),
                _falsifiability_judgment(True),
                _clean_novelty_judgment(),
                SkepticJudgment(critique="also solid", unaddressed_risks=["minor"], fatal_flaw_found=False),
                PairwiseJudgment(winner="candidate_a", rationale="first candidate is more falsifiable"),
                PairwiseJudgment(winner="candidate_b", rationale="first candidate still wins reversed"),
            ])

            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(), novelty_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=2, model=model,
            )

            self.assertEqual(2, len(results))
            for result in results:
                self.assertEqual("full", result.decision.gate_phase)
                self.assertEqual(GateVerdict.PASS, result.decision.verdict)
            self.assertEqual(2, len(ranking))
            self.assertEqual([1, 1], [entry.comparisons for entry in ranking])
            # 双向一致判 candidate_a（即第一个存活候选）获胜，Elo 更新后它应排在首位
            self.assertEqual(results[0].package.idea_id, ranking[0].idea_id)
            self.assertTrue(all(entry.evidence for entry in ranking))
