"""Unit tests for the pre_gate async pipeline (run_pre_gate)."""

import asyncio
import tempfile
import unittest
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from athena.core.agent import Agent, AgentConfig, StreamEvent
from athena.core.tool import ToolRegistry
from athena.storage import LocalArtifactStore
from athena.workflows.search.gatekeeper import MAX_TOLERATED_RISKS
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
    RevisionDraft,
    SkepticJudgment,
    VerbalizedSamplingResponse,
)
from athena.workflows.search.review_board import REVIEW_PERSPECTIVES, build_review_prompt
from athena.workflows.search.revision import MAX_DEBATE_ROUNDS
from athena.workflows.search.workflow import (
    RETRIEVAL_CONCURRENCY,
    pairwise_compare,
    run_full_pipeline,
    run_pre_gate,
)
from unit.fakes import make_routed_model, make_scripted_model, tool_call_response

# 占位 corpus_ref：这组测试只验证编排/门控/排序逻辑,不重复 Task 6 已经覆盖过的真实 paper_rag
# 检索集成,所以用一个格式合法但不指向真实内容的 sha256 引用即可。
_FAKE_CORPUS_REF = "sha256:" + "c" * 64

# 路由锚点：取各 prompt 模板中唯一、且互不为前缀的片段。GAP_MINER_SUMMARY 与
# NOVELTY_SUMMARY 共享前缀 "Summarize the following literature exploration transcript
# as a structured "，所以锚点必须取分岔之后的部分。
_ROUTE_GAP_MINING = "structured list of gaps"
_ROUTE_GENERATION = "Verbalized Sampling"
_ROUTE_FALSIFIABILITY = "falsifiability auditor"
_ROUTE_NOVELTY = "novelty and temporal-integrity assessment"
_ROUTE_REVIEW_METHODOLOGY = "Review perspective: methodology"
_ROUTE_REVIEW_STATISTICS = "Review perspective: statistics"
_ROUTE_REVIEW_DOMAIN = "Review perspective: domain_consistency"
_ROUTE_PAIRWISE = "impartial pairwise judge"
_ROUTE_REVISER = "Blocking rubric item:"
_ROUTE_DEBATE = "Debate round"


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


def _third_valid_draft() -> HypothesisDraft:
    """第三个候选，主张与前两个候选词集几乎不重叠，供 5-候选并发场景使用。"""
    return HypothesisDraft(
        statement="Mitochondrial calcium uptake accelerates dendritic spine pruning during sleep",
        intervention="Block mitochondrial calcium uniporter in cortical neurons overnight",
        expected_effect="Spine pruning rate drops relative to untreated controls",
        generation_strategy="verbalized_sampling_v1",
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y in mice", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[],
        predicted_observations=["Spine pruning rate drops after uniporter blockade"],
        disconfirming_observations=["Spine pruning rate is unchanged after uniporter blockade"],
    )


def _fourth_valid_draft() -> HypothesisDraft:
    """第四个候选，主张与前三个候选词集几乎不重叠，供 5-候选并发场景使用。"""
    return HypothesisDraft(
        statement="Gut microbiome diversity loss impairs vaccine antibody titers in aged mice",
        intervention="Deplete gut flora with broad-spectrum antibiotics before vaccination",
        expected_effect="Antibody titers drop relative to flora-intact controls",
        generation_strategy="verbalized_sampling_v1",
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y in mice", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[],
        predicted_observations=["Antibody titers drop after flora depletion"],
        disconfirming_observations=["Antibody titers are unchanged after flora depletion"],
    )


def _fifth_valid_draft() -> HypothesisDraft:
    """第五个候选，主张与前四个候选词集几乎不重叠，供 5-候选并发场景使用。"""
    return HypothesisDraft(
        statement="Circadian clock disruption elevates hepatic lipid peroxidation after high-fat feeding",
        intervention="Knock out core clock gene Bmal1 in hepatocytes before high-fat feeding",
        expected_effect="Lipid peroxidation markers rise relative to clock-intact controls",
        generation_strategy="verbalized_sampling_v1",
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y in mice", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[],
        predicted_observations=["Lipid peroxidation markers rise after Bmal1 knockout"],
        disconfirming_observations=["Lipid peroxidation markers are unchanged after Bmal1 knockout"],
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


def _full_pipeline_routes(drafts: list[HypothesisDraft]) -> dict:
    """run_full_pipeline 全绿路径的路由表。

    Example:
        >>> routes = _full_pipeline_routes([_valid_draft()])  # doctest: +SKIP
        >>> _ROUTE_GENERATION in routes  # doctest: +SKIP
        True
    """
    return {
        _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
        _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=drafts),
        _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
        _ROUTE_NOVELTY: _clean_novelty_judgment(),
        _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
            critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
            critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
            critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
        _ROUTE_PAIRWISE: PairwiseJudgment(winner="candidate_a", rationale="a is stronger"),
    }


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
        #
        # 用 make_routed_model 而非 make_scripted_model：forward/backward 并发发起后由
        # FunctionModel 在线程池里执行，谁先 pop(0) 不再有序，按调用顺序消费队列的
        # make_scripted_model 在并发下是 flaky 的。路由版按 prompt 内容而非调用序分发，
        # 锚点必须落在 "candidate_a: " 紧跟的文本上——两个候选的原文都会同时出现在同一条
        # prompt 里（只是 candidate_a/candidate_b 位置对调），单纯用候选原文当锚点会同时命中
        # forward 和 backward 两条 prompt，触发 make_routed_model 的“命中数必须恰好为 1”校验失败。
        model = make_routed_model({
            f"candidate_a: {package_a.novel_hypothesis}":
                PairwiseJudgment(winner="candidate_a", rationale="a wins forward"),
            f"candidate_a: {package_b.novel_hypothesis}":
                PairwiseJudgment(winner="candidate_b", rationale="a still wins backward"),
        })
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
        #
        # 同上一条测试，改用 make_routed_model 消除并发下的线程调度顺序依赖。
        model = make_routed_model({
            f"candidate_a: {package_a.novel_hypothesis}":
                PairwiseJudgment(winner="candidate_a", rationale="a wins forward"),
            f"candidate_a: {package_b.novel_hypothesis}":
                PairwiseJudgment(winner="candidate_a", rationale="b wins backward"),
        })
        comparison = await pairwise_compare(package_a, package_b, model=model)
        self.assertEqual("idea-a", comparison.winner_id)
        self.assertIn("disagreement", comparison.rationale)

    async def test_both_directions_are_still_called_after_concurrency_change(self) -> None:
        """并发化不得改变双向比较的语义：两次调用都要发生，winner 映射保持正确。

        不试图断言两次调用在时间上重叠——FunctionModel 的 respond 是同步函数，写时序断言
        只会得到一个脆弱且实际什么都没验证的测试。"深度减半"由 gather 的代码结构保证。
        """
        prompts: list[str] = []

        def respond(messages, info):
            prompts.append(str(messages))
            args = PairwiseJudgment(winner="candidate_a", rationale="r").model_dump(mode="json")
            tool_name = info.output_tools[0].name if info.output_tools else "final_result"
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

        package_a = HypothesisPackage(
            idea_id="idea-a", generation_strategy="s", novel_hypothesis="X causes Y",
            supported_premises=[], inference_chain=[], predicted_observations=["p"],
            disconfirming_observations=["d"], lineage_op="generate",
        )
        package_b = package_a.model_copy(update={"idea_id": "idea-b",
                                                  "novel_hypothesis": "Z inhibits W"})
        comparison = await pairwise_compare(package_a, package_b,
                                            model=FunctionModel(respond))

        self.assertEqual(2, len(prompts))
        # 一次正向（candidate_a=package_a）、一次反向（candidate_a=package_b），顺序不重要
        first_positions = {p.index("X causes Y") < p.index("Z inhibits W") for p in prompts}
        self.assertEqual({True, False}, first_positions)
        # 两次都选 candidate_a，反向的 candidate_a 是 package_b，故为双向分歧，取正向结果
        self.assertEqual("idea-a", comparison.winner_id)
        self.assertIn("disagreement", comparison.rationale)


class RunFullPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_single_surviving_candidate_is_ranked_without_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            model = make_routed_model({
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[_valid_draft()]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
                _ROUTE_NOVELTY: _clean_novelty_judgment(),
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(critique="looks solid", unaddressed_risks=[],
                                                            fatal_flaw_found=False),
                _ROUTE_REVIEW_STATISTICS: SkepticJudgment(critique="looks solid", unaddressed_risks=[],
                                                           fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(critique="looks solid", unaddressed_risks=[],
                                                       fatal_flaw_found=False),
            })

            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(), novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
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
            model = make_routed_model({
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[_valid_draft()]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(False),
            })

            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(), novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1, model=model,
            )

            self.assertEqual(1, len(results))
            self.assertEqual("pre_gate", results[0].decision.gate_phase)
            self.assertIsNone(results[0].novelty)
            self.assertEqual([], results[0].reviews)
            self.assertEqual([], ranking)

    async def test_two_surviving_candidates_get_pairwise_compared_and_ranked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            model = make_routed_model({
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(
                    candidates=[_valid_draft(), _second_valid_draft()]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
                _ROUTE_NOVELTY: _clean_novelty_judgment(),
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(critique="solid", unaddressed_risks=[],
                                                            fatal_flaw_found=False),
                _ROUTE_REVIEW_STATISTICS: SkepticJudgment(critique="solid", unaddressed_risks=[],
                                                           fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(critique="solid", unaddressed_risks=[],
                                                       fatal_flaw_found=False),
                _ROUTE_PAIRWISE: PairwiseJudgment(winner="candidate_a", rationale="first is more falsifiable"),
            })

            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(), novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=2, model=model,
            )

            self.assertEqual(2, len(results))
            for result in results:
                self.assertEqual("full", result.decision.gate_phase)
                self.assertEqual(GateVerdict.PASS, result.decision.verdict)
            self.assertEqual(2, len(ranking))
            self.assertEqual([1, 1], [entry.comparisons for entry in ranking])
            # 路由版下正反两次调用返回同一 winner 标签，按 pairwise_compare 的映射即为双向分歧，
            # 以正向结果（第一个存活候选）为准；"双向一致"路径由 PairwiseCompareTest 用顺序版覆盖
            self.assertEqual(results[0].package.idea_id, ranking[0].idea_id)
            self.assertTrue(all(entry.evidence for entry in ranking))


class FailureDegradationTest(unittest.IsolatedAsyncioTestCase):
    async def test_novelty_failure_does_not_drag_down_domain_consistency(self) -> None:
        """一次检索抖动不该同时废掉 novelty_ok 和 risk_ok_domain_consistency 两项。"""
        with tempfile.TemporaryDirectory() as tmp:
            drafts = [_valid_draft()]
            routes = _full_pipeline_routes(drafts)
            routes[_ROUTE_NOVELTY] = ValueError("retrieval hiccup")
            results, _ = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=1, model=make_routed_model(routes),
            )
            result = results[0]
            # novelty 降级成空报告：facet_overlap 为空 -> novelty_ok 判 REVISE
            self.assertEqual({}, result.novelty.facet_overlap)
            self.assertIsNone(result.novelty.query_log_ref)
            self.assertEqual(GateVerdict.REVISE, result.decision.verdict)
            self.assertEqual("novelty_ok", result.decision.blocking_factor)
            # domain_consistency 不受牵连：退回完整检索，仍然正常产出
            by_id = {r.perspective: r for r in result.reviews}
            self.assertFalse(by_id["domain_consistency"].failed)

    async def test_falsifiability_failure_degrades_to_not_falsifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            drafts = [_valid_draft()]
            routes = _full_pipeline_routes(drafts)
            routes[_ROUTE_FALSIFIABILITY] = ValueError("provider down")
            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=1, model=make_routed_model(routes),
            )
            self.assertEqual("pre_gate", results[0].decision.gate_phase)
            self.assertEqual(GateVerdict.REVISE, results[0].decision.verdict)
            self.assertEqual("falsifiable", results[0].decision.blocking_factor)
            self.assertFalse(results[0].falsifiability.is_falsifiable)
            self.assertEqual([], ranking)

    async def test_pairwise_failure_is_skipped_without_crashing_ranking(self) -> None:
        """spec 6.2 降级表第二行（Task 8 已实现,这里补覆盖）：某对 pairwise 比较失败时,
        跳过这一对,Elo 少一条记录,而不是让整个排序阶段崩掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            drafts = [_valid_draft(), _second_valid_draft()]
            routes = _full_pipeline_routes(drafts)
            routes[_ROUTE_PAIRWISE] = ValueError("judge unavailable")
            results, ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=2, model=make_routed_model(routes),
            )
            self.assertEqual(2, len(results))
            # 两个候选都存活但唯一一对比较失败被跳过：都停在 0 条 comparisons,而不是抛异常
            self.assertEqual(2, len(ranking))
            self.assertEqual([0, 0], sorted(entry.comparisons for entry in ranking))


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_concurrency_is_capped(self) -> None:
        """假 provider 记录并发峰值，断言不超过 RETRIEVAL_CONCURRENCY。

        场景必须让真实并发真的有机会超过上限才有意义：每个候选内部 novelty ->
        domain_consistency 是串行的，所以同时在飞的检索数上限 = 存活候选数；用
        sample_size=2 只能到 2，永远撞不到 RETRIEVAL_CONCURRENCY=4，测不出限流有没有生效。
        这里用 5 个互不相似的候选（MAX_VERBALIZED_SAMPLES），5 个候选同时跑 novelty 检索时
        无限流的话 peak 会到 5 > 4，这样"peak <= 4"才是一条真的会失败的断言。
        """
        class _CountingProvider:
            def __init__(self) -> None:
                self.active = 0
                self.peak = 0

            async def stream(self, *_args):
                self.active += 1
                self.peak = max(self.peak, self.active)
                await asyncio.sleep(0)
                yield StreamEvent("text_delta", {"delta": "t", "accumulated": "t"})
                # 递减必须在最后一个 yield 之前：core/agent/agent.py 的 _sampling_loop 收到
                # response_completed 就 break，不会把生成器消费完，写在最后一个 yield 之后的
                # 代码永远不执行——那样 active 就只增不减，peak 测的是"累计发起的检索调用总数"
                # 而不是"任意时刻并发数"，测试会变成假阳性。
                self.active -= 1
                yield StreamEvent("response_completed")

        with tempfile.TemporaryDirectory() as tmp:
            provider = _CountingProvider()
            novelty_agent = _build_retrieval_agent()
            novelty_agent._provider = provider
            domain_agent = _build_retrieval_agent()
            domain_agent._provider = provider
            drafts = [
                _valid_draft(), _second_valid_draft(), _third_valid_draft(),
                _fourth_valid_draft(), _fifth_valid_draft(),
            ]
            _, _ = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=novelty_agent, domain_review_agent=domain_agent,
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=5,
                model=make_routed_model(_full_pipeline_routes(drafts)),
            )
            self.assertLessEqual(provider.peak, RETRIEVAL_CONCURRENCY)

    async def test_ranking_is_deterministic_across_runs(self) -> None:
        """同一输入跑两次，排名必须完全一致——Elo 是在线增量更新，喂入顺序影响评分。

        必须用 5 个存活候选（C(5,2)=10 次比较）而非 2 个：2 个候选只有 C(2,2)=1 次比较，
        不存在"喂入顺序"这回事，测不出 Task 8 要保证的按 (i, j) 索引序喂入 Elo 这条规则——
        任何把 pairs/comparisons 顺序改乱的回归都不会被这条测试发现。5 个互不相似的候选
        复用 test_retrieval_concurrency_is_capped 已经验证过的那组（两两 Jaccard <= 0.053，
        远低于去重阈值 0.8），保证不会被 deduplicate_candidates 合并掉。
        """
        rankings = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                drafts = [
                    _valid_draft(), _second_valid_draft(), _third_valid_draft(),
                    _fourth_valid_draft(), _fifth_valid_draft(),
                ]
                _, ranking = await run_full_pipeline(
                    _problem(), gap_miner_agent=_build_retrieval_agent(),
                    novelty_agent=_build_retrieval_agent(),
                    domain_review_agent=_build_retrieval_agent(),
                    artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                    sample_size=5,
                    model=make_routed_model(_full_pipeline_routes(drafts)),
                )
                rankings.append([(e.idea_id, e.rating) for e in ranking])
        self.assertEqual(
            [r[1] for r in rankings[0]], [r[1] for r in rankings[1]],
            "Elo ratings must not depend on task completion order",
        )

    async def test_reports_are_not_crossed_between_candidates(self) -> None:
        """并发重构最典型的缺陷是闭包变量捕获错，症状正是候选 A 的 package 配上候选 B 的报告。"""
        with tempfile.TemporaryDirectory() as tmp:
            drafts = [_valid_draft(), _second_valid_draft()]
            results, _ = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=2,
                model=make_routed_model(_full_pipeline_routes(drafts)),
            )
            for result in results:
                idea_id = result.package.idea_id
                self.assertEqual(idea_id, result.structural.idea_id)
                self.assertEqual(idea_id, result.falsifiability.idea_id)
                self.assertEqual(idea_id, result.novelty.idea_id)
                self.assertEqual(idea_id, result.decision.idea_id)
                for review in result.reviews:
                    self.assertEqual(idea_id, review.idea_id)

    async def test_domain_reviewer_runs_once_per_survivor_only(self) -> None:
        """两个检索 Agent 各挂独立计数器：domain_review_agent 只应为存活候选跑，且每个
        存活候选恰好跑一次；pre_gate 就被筛掉的候选不该触发它。共用一个计数器分不清是谁
        跑的。退化情形（0 存活 -> 0 次调用）与正例（N 存活 -> N 次调用）都要覆盖，否则
        名字里的"per survivor"从未被真正断言过。"""
        class _CountingProvider:
            def __init__(self) -> None:
                self.calls = 0

            async def stream(self, *_args):
                self.calls += 1
                yield StreamEvent("text_delta", {"delta": "t", "accumulated": "t"})
                yield StreamEvent("response_completed")

        with tempfile.TemporaryDirectory() as tmp:
            novelty_provider = _CountingProvider()
            domain_provider = _CountingProvider()
            novelty_agent = _build_retrieval_agent()
            novelty_agent._provider = novelty_provider
            domain_agent = _build_retrieval_agent()
            domain_agent._provider = domain_provider
            # 两个候选，其中一个在 pre_gate 就被筛掉：可证伪性审计对所有候选返回 False
            drafts = [_valid_draft(), _second_valid_draft()]
            routes = _full_pipeline_routes(drafts)
            routes[_ROUTE_FALSIFIABILITY] = _falsifiability_judgment(False)
            results, _ = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=novelty_agent, domain_review_agent=domain_agent,
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=2, model=make_routed_model(routes),
            )
            survivors = [r for r in results if r.decision.gate_phase == "full"]
            self.assertEqual(0, len(survivors))
            # 没有候选走到步骤 [5]/[6]，两个检索 Agent 都不该被触发
            self.assertEqual(0, novelty_provider.calls)
            self.assertEqual(0, domain_provider.calls)

        # 正例：两个候选都通过 pre_gate 存活，domain_review_agent 应恰好为每个存活候选
        # 跑一次（2 次），而不是 0 次（上面已覆盖）或者被漏跑/去重成 1 次。
        with tempfile.TemporaryDirectory() as tmp:
            novelty_provider = _CountingProvider()
            domain_provider = _CountingProvider()
            novelty_agent = _build_retrieval_agent()
            novelty_agent._provider = novelty_provider
            domain_agent = _build_retrieval_agent()
            domain_agent._provider = domain_provider
            drafts = [_valid_draft(), _second_valid_draft()]
            results, _ = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=novelty_agent, domain_review_agent=domain_agent,
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=2, model=make_routed_model(_full_pipeline_routes(drafts)),
            )
            survivors = [r for r in results if r.decision.gate_phase == "full"]
            self.assertEqual(2, len(survivors))
            self.assertEqual(2, domain_provider.calls)

    async def test_results_preserve_candidate_input_order(self) -> None:
        """results 必须保持 candidates 输入序——这是 Elo 确定性的真正前提。"""
        with tempfile.TemporaryDirectory() as tmp:
            drafts = [_valid_draft(), _second_valid_draft()]
            results, _ = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(),
                domain_review_agent=_build_retrieval_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                sample_size=2,
                model=make_routed_model(_full_pipeline_routes(drafts)),
            )
            self.assertEqual(
                [d.statement for d in drafts],
                [r.package.novel_hypothesis for r in results],
            )


class RevisionLoopPipelineTest(unittest.IsolatedAsyncioTestCase):
    """端到端覆盖修订闭环接入 run_full_pipeline 之后的四种路径：清除后终审 PASS、跑满
    MAX_DEBATE_ROUNDS 仍未收敛、novelty_ok 拦截根本不可修订故不入环、以及"辩论清除了被拦项
    但终局刷新在另一视角新增风险"这种末轮 cleared 与终审结果不一致的审计场景。

    四个 _run* 辅助都只改 disconfirming_observations（"disconfirmer-only 修订"），刻意不碰
    novel_hypothesis / predicted_observations：这样 novelty 与 domain_consistency 的输入
    指纹不受影响，不触发检索,把每个场景收敛到只需要处理 methodology/statistics 两个纯文本
    视角,断言只需要关心door门控与轮次记录本身。
    """

    async def _run_debate_to_pass(self) -> tuple[list, list]:
        # blocking = risk_ok_statistics；对手（statistics）重表态后风险清零 → 终审 PASS。
        # 修订只改 disconfirming_observations，methodology 因此在终局刷新里被重跑一次
        # （单一路由复用同一份 "ok" 响应即可，不需要区分两次调用），novelty/domain_consistency
        # 不受影响，只各跑一次。
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            draft = _valid_draft()
            blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
            revision_draft = RevisionDraft(
                rebuttal="a matched control cohort now grounds the disconfirmer",
                changes_made=["tightened the disconfirming observation"],
                revised_novel_hypothesis=draft.statement,
                revised_premises=draft.supported_premises,
                revised_predicted_observations=draft.predicted_observations,
                revised_disconfirming_observations=[
                    "Y stays the same after X knockout, confirmed by a matched control cohort",
                ],
            )
            routes = {
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[draft]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
                _ROUTE_NOVELTY: _clean_novelty_judgment(),
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                    critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVISER: revision_draft,
                _ROUTE_DEBATE: SkepticJudgment(
                    critique="cleared after revision", unaddressed_risks=[], fatal_flaw_found=False),
            }
            return await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(), domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                model=make_routed_model(routes),
            )

    async def test_blocked_candidate_debates_and_passes(self) -> None:
        # blocking = risk_ok_statistics；对手重表态后风险清零 → 终审 PASS
        results, _ranking = await self._run_debate_to_pass()
        result = results[0]
        self.assertEqual(GateVerdict.PASS, result.decision.verdict)
        self.assertEqual("risk_ok_statistics", result.revision_blocking_factor)
        self.assertEqual(1, len(result.revisions))
        self.assertTrue(result.revisions[0].cleared)

    async def _run_non_converging(self) -> tuple[list, list]:
        # 每轮 reviser 都产出实质改动但对手始终不买账 -> 跑满 MAX_DEBATE_ROUNDS 仍未清除。
        # 两轮 reviser 必须路由到不同的 RevisionDraft，否则第二轮的"修订"与第一轮已经落地的
        # 当前稿逐字段相同，is_no_op_revision 会提前掐断循环——照抄
        # test_revision.py::RunDebateTest._routes 的技巧：用只在各自那一轮出现的锚点文本区分
        # （"(this is the first round)" 只在首轮出现；"reviewer replied=" 后接的对手回话文本
        # 只会出现在第二轮的 prior_rounds 摘要里）。
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            draft = _valid_draft()
            blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
            reviewer_critique = "still underpowered after revision"
            round1_draft = RevisionDraft(
                rebuttal="expanded the sample-size discussion",
                changes_made=["clarified sample-size assumptions"],
                revised_novel_hypothesis=draft.statement,
                revised_premises=draft.supported_premises,
                revised_predicted_observations=draft.predicted_observations,
                revised_disconfirming_observations=[
                    "Y stays the same after X knockout, round 1 revision"],
            )
            round2_draft = RevisionDraft(
                rebuttal="added a power-analysis appendix",
                changes_made=["added a power analysis"],
                revised_novel_hypothesis=draft.statement,
                revised_premises=draft.supported_premises,
                revised_predicted_observations=draft.predicted_observations,
                revised_disconfirming_observations=[
                    "Y stays the same after X knockout, round 2 revision"],
            )
            routes = {
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[draft]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
                _ROUTE_NOVELTY: _clean_novelty_judgment(),
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                    critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                "(this is the first round)": round1_draft,
                f"reviewer replied={reviewer_critique}": round2_draft,
                _ROUTE_DEBATE: SkepticJudgment(
                    critique=reviewer_critique, unaddressed_risks=blocking_risks, fatal_flaw_found=False),
            }
            return await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(), domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                model=make_routed_model(routes),
            )

    async def test_non_converging_candidate_is_archived_as_revise(self) -> None:
        results, _ranking = await self._run_non_converging()
        result = results[0]
        self.assertEqual(GateVerdict.REVISE, result.decision.verdict)
        self.assertNotEqual(GateVerdict.EXPLORATORY, result.decision.verdict)
        self.assertEqual(MAX_DEBATE_ROUNDS, len(result.revisions))
        for round_record in result.revisions:
            self.assertTrue(round_record.package_ref.startswith("sha256:"))
            self.assertTrue(round_record.rebuttal_ref.startswith("sha256:"))

    async def _run_with_empty_facets(self) -> tuple[list, list]:
        # facet_overlap 为空 -> hard_gate 判 REVISE/novelty_ok，is_revisable 排除该项 ->
        # 直接归档，不进闭环。
        #
        # 路由里刻意放一套"辩论会成功"的 reviser/辩论响应（会让某个视角清零风险、产出 1 轮
        # RevisionRound），而不是干脆不放这两个锚点：run_debate 把 revise_candidate 的任何
        # 异常都在 except Exception 里吞掉（包括 make_routed_model 0 命中抛出的
        # AssertionError），返回空 rounds——如果只是不放锚点，即便 is_revisable 的守卫被
        # 误删、闭环被意外触发，也只会因 0 命中被吞掉、rounds 仍是 []，
        # assertEqual([], results[0].revisions) 测不出任何东西（详见 test-9 review）。放一套
        # 会让辩论真正跑通、产出非空 rounds 的响应，这条断言才会在守卫被删掉时真正变红。
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            draft = _valid_draft()
            empty_facet_novelty = NoveltyEvidenceJudgment(
                nearest_work=[], facet_overlap={}, coverage_estimate=0.0, unrecalled_risk=0.8,
                citation_cutoff_ok=True, retrieval_cutoff_ok=True, post_cutoff_similarity=0.0,
                possible_memorization=False, leakage_risk=0.0, historical_backtest_validity=True,
                uncertainty=0.9,
            )
            # 会被"意外触发的辩论"消费的响应：实质改动 + 对手清零风险，保证一旦被调用就会
            # 产出至少 1 条非空 RevisionRound。
            revision_draft = RevisionDraft(
                rebuttal="a matched control cohort now grounds the disconfirmer",
                changes_made=["tightened the disconfirming observation"],
                revised_novel_hypothesis=draft.statement,
                revised_premises=draft.supported_premises,
                revised_predicted_observations=draft.predicted_observations,
                revised_disconfirming_observations=[
                    "Y stays the same after X knockout, confirmed by a matched control cohort",
                ],
            )
            routes = {
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[draft]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
                _ROUTE_NOVELTY: empty_facet_novelty,
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVISER: revision_draft,
                _ROUTE_DEBATE: SkepticJudgment(
                    critique="cleared after revision", unaddressed_risks=[], fatal_flaw_found=False),
            }
            return await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(), domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                model=make_routed_model(routes),
            )

    async def test_novelty_ok_block_never_enters_the_loop(self) -> None:
        # facet_overlap 为空 → hard_gate 判 REVISE/novelty_ok → 直接归档，reviser 零调用。
        results, _ranking = await self._run_with_empty_facets()
        self.assertEqual("novelty_ok", results[0].decision.blocking_factor)
        self.assertEqual([], results[0].revisions)
        self.assertIsNone(results[0].revision_blocking_factor)

    async def _run_cleared_then_blocked_elsewhere(self) -> tuple[list, list]:
        # 辩论清除了 risk_ok_methodology（对手是 methodology），但修订同时改动了
        # disconfirming_observations，让 statistics 的存档指纹失效；终局刷新重跑 statistics
        # 这一次改口新增风险 -> 终审仍 REVISE(risk_ok_statistics)，而末轮 cleared 保持 True。
        #
        # statistics 在同一条流水线里被 model 调用两次（初审 ok、终局刷新新增风险），两次的
        # prompt 只有 disconfirming_observations 这一段不同，不能用通用锚点 "Review
        # perspective: statistics" 区分（两次调用都会命中，造成同一 key 无法给出两种不同响应；
        # 若额外加一个更具体的 key，两个锚点会在同一次调用里都命中，触发
        # make_routed_model 的"命中数必须恰好为一"校验）。用 build_review_prompt 直接算出
        # 两次调用各自的精确 prompt 全文当 key，天然互斥、也不会与其他路由碰撞。
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            draft = _valid_draft()
            revised_disconfirming = [
                "Y stays the same after X knockout, confirmed by a matched control cohort"]
            mirror_before = HypothesisPackage(
                idea_id="mirror", generation_strategy="s", novel_hypothesis=draft.statement,
                sampling_probability=draft.sampling_probability,
                supported_premises=draft.supported_premises, inference_chain=draft.inference_chain,
                predicted_observations=draft.predicted_observations,
                disconfirming_observations=draft.disconfirming_observations, lineage_op="generate",
            )
            mirror_after = mirror_before.model_copy(
                update={"disconfirming_observations": revised_disconfirming})
            statistics_perspective = next(
                p for p in REVIEW_PERSPECTIVES if p.perspective_id == "statistics")
            initial_statistics_prompt = build_review_prompt(mirror_before, statistics_perspective)
            refreshed_statistics_prompt = build_review_prompt(mirror_after, statistics_perspective)

            blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
            revision_draft = RevisionDraft(
                rebuttal="a matched control cohort now grounds the disconfirmer",
                changes_made=["tightened the disconfirming observation"],
                revised_novel_hypothesis=draft.statement,
                revised_premises=draft.supported_premises,
                revised_predicted_observations=draft.predicted_observations,
                revised_disconfirming_observations=revised_disconfirming,
            )
            routes = {
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[draft]),
                _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
                _ROUTE_NOVELTY: _clean_novelty_judgment(),
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                    critique="missing control", unaddressed_risks=blocking_risks,
                    fatal_flaw_found=False),
                initial_statistics_prompt: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                refreshed_statistics_prompt: SkepticJudgment(
                    critique="power dropped after the revision",
                    unaddressed_risks=[f"new risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)],
                    fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVISER: revision_draft,
                _ROUTE_DEBATE: SkepticJudgment(
                    critique="cleared after revision", unaddressed_risks=[], fatal_flaw_found=False),
            }
            return await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(), domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                model=make_routed_model(routes),
            )

    async def test_cleared_debate_can_still_fail_the_final_gate(self) -> None:
        # 辩论清除了 risk_ok_methodology，但终局刷新让 statistics 新增风险 → 终审仍 REVISE，
        # 而末轮 cleared 保持 True。二者不一致本身就是审计信号，不抛异常、不做一致性校正。
        results, _ranking = await self._run_cleared_then_blocked_elsewhere()
        result = results[0]
        self.assertEqual(GateVerdict.REVISE, result.decision.verdict)
        self.assertEqual("risk_ok_statistics", result.decision.blocking_factor)
        self.assertTrue(result.revisions[-1].cleared)

    async def _run_no_op_debate(self) -> tuple[list, dict]:
        # blocking = risk_ok_statistics；reviser 直接产出一份逐字段不变的 no-op 修订，
        # is_no_op_revision 在第一轮就掐断循环——对手（statistics）与终局刷新全程零调用。
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            draft = _valid_draft()
            blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
            no_op_draft = RevisionDraft(
                rebuttal="nothing substantive to add",
                changes_made=[],
                revised_novel_hypothesis=draft.statement,
                revised_premises=draft.supported_premises,
                revised_predicted_observations=draft.predicted_observations,
                revised_disconfirming_observations=draft.disconfirming_observations,
            )
            falsifiability_calls = {"n": 0}
            routes = {
                _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
                _ROUTE_GENERATION: VerbalizedSamplingResponse(candidates=[draft]),
                _ROUTE_NOVELTY: _clean_novelty_judgment(),
                _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                    critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
                _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                    critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
                _ROUTE_REVISER: no_op_draft,
            }

            def respond(messages, info):
                # falsifiability 单独计数，不放进 routes：no-op 分支必须只在 _screen_candidate
                # 里被调用一次，不该在 refresh_stale_evidence 里被无条件重跑第二次——这是
                # finding 4 的核心断言，make_routed_model 本身不计数，所以这里手写一个
                # FunctionModel，其余锚点复用与 make_routed_model 相同的"命中数恰为一"规则。
                prompt = str(messages)
                if _ROUTE_FALSIFIABILITY in prompt:
                    falsifiability_calls["n"] += 1
                    return tool_call_response(_falsifiability_judgment(True), info)
                hits = [key for key in routes if key in prompt]
                assert len(hits) == 1, hits
                return tool_call_response(routes[hits[0]], info)

            results, _ranking = await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(), domain_review_agent=_build_retrieval_agent(),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF, sample_size=1,
                model=FunctionModel(respond),
            )
            return results, falsifiability_calls

    async def test_no_op_debate_does_not_rerun_falsifiability_or_regate(self) -> None:
        # 设计 §5.4：no-op guard 意味着立即退出、不再花钱——终局刷新（含无条件的
        # falsifiability_check）与第二次 hard_gate 都不该跑，但 no-op 轮次本身仍要留痕
        # （revisions 长度为 1），revision_blocking_factor 因为没有真实修订而留 None。
        results, falsifiability_calls = await self._run_no_op_debate()
        result = results[0]
        self.assertEqual(GateVerdict.REVISE, result.decision.verdict)
        self.assertEqual("risk_ok_statistics", result.decision.blocking_factor)
        self.assertEqual(1, len(result.revisions))
        self.assertFalse(result.revisions[0].cleared)
        self.assertIsNone(result.revisions[0].reviewer_response_ref)
        self.assertIsNone(result.revision_blocking_factor)
        self.assertEqual(1, falsifiability_calls["n"])


class RevisionLoopConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """并发/确定性护栏：三个候选同时进入修订闭环时，两条不可退让的保证仍然成立——候选间的
    package/记录不串味（闭包捕获错误的经典症状），以及 Elo 排序在相同输入下逐次运行保持
    一致（Elo 是在线增量更新，喂入顺序会改变评分，全靠 results 保持 candidates 输入序）。

    generate_candidates 是唯一被替身的一步：真实实现每次调用都用 uuid4 现场给候选分配
    idea_id，而确定性断言要求同一输入跑两次能拿到完全相同的 idea_id 序列，随机分配做不到
    这个前提；多候选生成/去重本身已有 candidate_generation 专属测试覆盖，不是这里要测的
    对象。[4]-[9]（screen -> audit -> 修订闭环 -> pairwise 排序）全部走 run_full_pipeline
    真实实现，这正是本类要覆盖的并发路径。
    """

    def _multi_candidate_routes(self) -> dict:
        # 三个候选共用一套路由：全部在 statistics 视角被拦（unaddressed_risks 数严格超过
        # MAX_TOLERATED_RISKS），辩论一轮清零风险 -> 终局刷新（novel_hypothesis 被改动，
        # novelty + methodology + domain_consistency 因指纹失配重跑；statistics 在辩论时
        # 已经刷新过指纹，不会被重复判 stale）-> 终审 PASS -> 三个存活候选两两 pairwise。
        # reviser 的固定响应文本刻意与三个候选的原始 statement 都不同，避免任何一个候选被
        # is_no_op_revision 误判成空转、对手一次都不会被调用。
        blocking_risks = [f"risk {i}" for i in range(MAX_TOLERATED_RISKS + 1)]
        revision_draft = RevisionDraft(
            rebuttal="a matched control cohort and an expanded sample now ground the claim",
            changes_made=["added a matched control cohort", "expanded the sample size"],
            revised_novel_hypothesis=(
                "A shared, control-adjusted revision distinct from any original candidate"),
            revised_premises=[ClaimEvidence(
                claim="X correlates with Y in mice", role=ClaimRole.SUPPORTED_PREMISE,
                supporting_refs=["ev-0"])],
            revised_predicted_observations=["The revised effect holds versus a matched control"],
            revised_disconfirming_observations=[
                "The revised effect disappears versus a matched control"],
        )
        return {
            _ROUTE_GAP_MINING: GapMiningResponse(gaps=[]),
            _ROUTE_FALSIFIABILITY: _falsifiability_judgment(True),
            _ROUTE_NOVELTY: _clean_novelty_judgment(),
            _ROUTE_REVIEW_METHODOLOGY: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
            _ROUTE_REVIEW_STATISTICS: SkepticJudgment(
                critique="underpowered", unaddressed_risks=blocking_risks, fatal_flaw_found=False),
            _ROUTE_REVIEW_DOMAIN: SkepticJudgment(
                critique="ok", unaddressed_risks=[], fatal_flaw_found=False),
            _ROUTE_REVISER: revision_draft,
            _ROUTE_DEBATE: SkepticJudgment(
                critique="cleared after revision", unaddressed_risks=[], fatal_flaw_found=False),
            _ROUTE_PAIRWISE: PairwiseJudgment(winner="candidate_a", rationale="first is stronger"),
        }

    def _fixed_candidates(self) -> list[HypothesisPackage]:
        # idea_id 写死，不走 generate_candidates 现场 uuid4 分配：确定性测试要求同一输入
        # 跑两次能拿到完全相同的 idea_id 序列。三个候选复用已验证过两两 Jaccard 远低于去重
        # 阈值的三份 draft，保证不会被 deduplicate_candidates 合并掉。
        drafts = [_valid_draft(), _second_valid_draft(), _third_valid_draft()]
        return [
            HypothesisPackage(
                idea_id=f"idea-fixed-{index}", generation_strategy=draft.generation_strategy,
                sampling_probability=draft.sampling_probability, novel_hypothesis=draft.statement,
                supported_premises=draft.supported_premises, inference_chain=draft.inference_chain,
                predicted_observations=draft.predicted_observations,
                disconfirming_observations=draft.disconfirming_observations, lineage_op="generate",
            )
            for index, draft in enumerate(drafts)
        ]

    async def _run_multi_candidate_all_blocked(self) -> tuple[list, list]:
        """跑一遍三候选全阻塞 -> 辩论清除 -> 终审 PASS -> 排序，供两条测试复用。

        只替身 generate_candidates 这一步（原因见类文档字符串），[4] 及之后全部是
        run_full_pipeline 的真实实现，本函数本身不重新实现任何编排逻辑。

        Example:
            >>> results, ranking = await self._run_multi_candidate_all_blocked()  # doctest: +SKIP
            >>> len(results)  # doctest: +SKIP
            3
        """
        async def fixed_generate(problem, gaps, *, sample_size, model):
            return self._fixed_candidates()

        with patch("athena.workflows.search.workflow.generate_candidates",
                   side_effect=fixed_generate):
            return await run_full_pipeline(
                _problem(), gap_miner_agent=_build_retrieval_agent(),
                novelty_agent=_build_retrieval_agent(), domain_review_agent=_build_retrieval_agent(),
                artifacts=self._store, corpus_ref=_FAKE_CORPUS_REF, sample_size=3,
                model=make_routed_model(self._multi_candidate_routes()),
            )

    async def test_revision_records_never_cross_candidates(self) -> None:
        # 并发重构最典型的缺陷是闭包捕获错误，症状正是候选 A 的 package 配上候选 B 的记录
        with tempfile.TemporaryDirectory() as tmp:
            self._store = LocalArtifactStore(tmp)
            results, _ranking = await self._run_multi_candidate_all_blocked()
            for result in results:
                for round_record in result.revisions:
                    stored = HypothesisPackage.model_validate_json(
                        await self._store.get_text(round_record.package_ref))
                    self.assertEqual(result.package.idea_id, stored.idea_id)

    async def test_elo_ranking_stays_deterministic_with_the_revision_loop(self) -> None:
        # 光比 idea_id 顺序不够强：本测试三个候选的 pairwise 胜负是可传递的（0 恒胜 1/2，
        # 1 恒胜 2），乱序喂入 Elo 时排名的名次顺序不受影响，只有具体评分会变——用这组固定
        # 路由验证过（见 task-10 报告），把 record_comparison 的两次调用顺序对调，
        # rank() 返回的 idea_id 顺序不变但 rating 精确值不同。所以决定性断言必须落在 rating
        # 上，不能只看 idea_id 顺序，否则 asyncio.gather 意外被换成 as_completed 这类真实
        # 回归会被这条测试放过。
        with tempfile.TemporaryDirectory() as tmp:
            self._store = LocalArtifactStore(tmp)
            first, _ = await self._run_multi_candidate_all_blocked()
            second, _ = await self._run_multi_candidate_all_blocked()
            self.assertEqual([r.decision.verdict for r in first],
                             [r.decision.verdict for r in second])
            _, ranking_a = await self._run_multi_candidate_all_blocked()
            _, ranking_b = await self._run_multi_candidate_all_blocked()
            self.assertEqual([c.idea_id for c in ranking_a], [c.idea_id for c in ranking_b])
            self.assertEqual([(c.idea_id, c.rating) for c in ranking_a],
                             [(c.idea_id, c.rating) for c in ranking_b])
