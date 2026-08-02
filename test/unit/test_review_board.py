"""Unit tests for the multi-perspective review board."""

import tempfile
import unittest

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from athena.core.agent import Agent, AgentConfig, StreamEvent
from athena.core.tool import ToolRegistry
from athena.storage import LocalArtifactStore
from athena.workflows.prompts import (
    REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT,
    REVIEW_METHODOLOGY_SYSTEM_PROMPT,
    REVIEW_STATISTICS_SYSTEM_PROMPT,
)
from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    HypothesisPackage,
    NoveltyEvidenceReport,
    SkepticJudgment,
)
from athena.workflows.search.review_board import (
    REVIEW_PERSPECTIVES,
    ReviewPerspective,
    build_domain_consistency_agent,
    review_board,
    review_one_perspective,
)
from unit.fakes import make_routed_model

_FAKE_CORPUS_REF = "sha256:" + "c" * 64


class ReviewPerspectivesTest(unittest.TestCase):
    def test_exactly_three_perspectives_in_stable_order(self) -> None:
        self.assertEqual(
            ["methodology", "statistics", "domain_consistency"],
            [p.perspective_id for p in REVIEW_PERSPECTIVES],
        )

    def test_perspective_ids_are_unique(self) -> None:
        ids = [p.perspective_id for p in REVIEW_PERSPECTIVES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_only_domain_consistency_needs_retrieval(self) -> None:
        needing = [p.perspective_id for p in REVIEW_PERSPECTIVES if p.needs_retrieval]
        self.assertEqual(["domain_consistency"], needing)

    def test_each_perspective_binds_its_own_system_prompt(self) -> None:
        by_id = {p.perspective_id: p.system_prompt for p in REVIEW_PERSPECTIVES}
        self.assertEqual(REVIEW_METHODOLOGY_SYSTEM_PROMPT, by_id["methodology"])
        self.assertEqual(REVIEW_STATISTICS_SYSTEM_PROMPT, by_id["statistics"])
        self.assertEqual(REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT, by_id["domain_consistency"])

    def test_perspective_is_frozen(self) -> None:
        with self.assertRaises(Exception):
            REVIEW_PERSPECTIVES[0].perspective_id = "mutated"

    def test_system_prompts_do_not_mention_sampling_probability(self) -> None:
        # Co-Scientist 硬约束：审阅侧看不到生成侧的自评置信度，连字段名都不该出现
        for perspective in REVIEW_PERSPECTIVES:
            self.assertNotIn("sampling_probability", perspective.system_prompt)


class _StaticTextProvider:
    """总是回放固定文本、不调用工具的假 provider；同时记下每次收到的完整消息文本

    （含 system prompt、注入的 question/prior_retrieval），供测试核对 prompt 内容
    真的到达了 provider，而不是只验证"没抛异常"这种弱断言。
    """

    def __init__(self, text: str = "no contradicting work found") -> None:
        self._text = text
        self.calls = 0
        self.captured_prompts: list[str] = []

    async def stream(self, config, messages, cancel):
        self.calls += 1
        self.captured_prompts.append(str(messages))
        yield StreamEvent("text_delta", {"delta": self._text, "accumulated": self._text})
        yield StreamEvent("response_completed")


def _build_domain_agent(provider: _StaticTextProvider | None = None) -> Agent:
    agent = Agent(AgentConfig(model="test-model", system_prompt="system", tools=ToolRegistry()))
    agent._provider = provider or _StaticTextProvider()
    return agent


def _package(sampling_probability: float = 0.42) -> HypothesisPackage:
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=sampling_probability,
        supported_premises=[
            ClaimEvidence(claim="X correlates with Y", role=ClaimRole.SUPPORTED_PREMISE,
                          supporting_refs=["ev-0"]),
        ],
        inference_chain=[], predicted_observations=["Y increases"],
        disconfirming_observations=["Y unchanged"], lineage_op="generate",
    )


def _novelty(query_log_ref: str | None = "sha256:" + "e" * 64) -> NoveltyEvidenceReport:
    return NoveltyEvidenceReport(
        idea_id="idea-1", nearest_work=[], facet_overlap={"problem": 0.1},
        coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64,
        query_log_ref=query_log_ref, uncertainty=0.2,
    )


def _routes(**overrides) -> dict:
    base = {
        "Review perspective: methodology": SkepticJudgment(
            critique="no control group", unaddressed_risks=["confound"], fatal_flaw_found=False),
        "Review perspective: statistics": SkepticJudgment(
            critique="underpowered", unaddressed_risks=[], fatal_flaw_found=False),
        "Review perspective: domain_consistency": SkepticJudgment(
            critique="consistent with known mechanism", unaddressed_risks=[],
            fatal_flaw_found=False),
    }
    base.update(overrides)
    return base


class BuildDomainConsistencyAgentTest(unittest.TestCase):
    def test_binds_correct_system_prompt(self) -> None:
        agent = build_domain_consistency_agent("test-model", ToolRegistry(), client=None)
        self.assertEqual(REVIEW_DOMAIN_CONSISTENCY_SYSTEM_PROMPT, agent.config.system_prompt)
        self.assertEqual("domain_consistency_reviewer", agent.name)


class ReviewBoardTest(unittest.IsolatedAsyncioTestCase):
    async def test_produces_one_report_per_perspective_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = await review_board(
                _package(), _novelty(), domain_review_agent=_build_domain_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_routes()),
            )
            self.assertEqual(
                ["methodology", "statistics", "domain_consistency"],
                [r.perspective for r in reports],
            )
            self.assertTrue(all(r.idea_id == "idea-1" for r in reports))
            self.assertFalse(any(r.failed for r in reports))

    async def test_only_domain_consistency_carries_a_transcript_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = await review_board(
                _package(), _novelty(), domain_review_agent=_build_domain_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_routes()),
            )
            by_id = {r.perspective: r for r in reports}
            self.assertIsNone(by_id["methodology"].transcript_ref)
            self.assertIsNone(by_id["statistics"].transcript_ref)
            self.assertTrue(by_id["domain_consistency"].transcript_ref.startswith("sha256:"))

    async def test_only_domain_consistency_runs_a_retrieval_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _StaticTextProvider()
            await review_board(
                _package(), _novelty(), domain_review_agent=_build_domain_agent(provider),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_routes()),
            )
            # methodology / statistics 全程只走 single_turn_chat，不触发任何检索循环
            self.assertEqual(1, provider.calls)

    async def test_failed_perspective_is_marked_not_silently_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = _routes()
            routes["Review perspective: statistics"] = ValueError("provider hiccup")
            reports = await review_board(
                _package(), _novelty(), domain_review_agent=_build_domain_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(routes),
            )
            by_id = {r.perspective: r for r in reports}
            self.assertTrue(by_id["statistics"].failed)
            self.assertIn("provider hiccup", by_id["statistics"].critique)
            self.assertFalse(by_id["methodology"].failed)

    async def test_missing_transcript_falls_back_to_full_retrieval(self) -> None:
        # 转录复用是优化不是前置条件：novelty 失败降级时 query_log_ref 为 None
        with tempfile.TemporaryDirectory() as tmp:
            reports = await review_board(
                _package(), _novelty(query_log_ref=None),
                domain_review_agent=_build_domain_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_routes()),
            )
            by_id = {r.perspective: r for r in reports}
            self.assertFalse(by_id["domain_consistency"].failed)
            self.assertTrue(by_id["domain_consistency"].transcript_ref.startswith("sha256:"))

    async def test_domain_consistency_receives_the_reused_transcript(self) -> None:
        # 核心卖点的成功路径：[5] 的检索转录必须真的读出来、送进 domain_consistency 的
        # 检索 prompt，而不是只有"读不到时优雅降级"这一条路径被测到。
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            marker = "PRIOR-TRANSCRIPT-MARKER: no contradicting evidence found in corpus X"
            real_transcript_ref = await artifacts.put_text(marker)
            provider = _StaticTextProvider()
            await review_board(
                _package(), _novelty(query_log_ref=real_transcript_ref),
                domain_review_agent=_build_domain_agent(provider),
                artifacts=artifacts, corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(_routes()),
            )
            self.assertEqual(1, provider.calls)
            self.assertTrue(any(marker in prompt for prompt in provider.captured_prompts))


class ReviewRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_is_retried_once_then_succeeds(self) -> None:
        calls = {"n": 0}

        def respond(messages, info):
            if "Review perspective: statistics" in str(messages):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ValueError("transient hiccup")
            args = SkepticJudgment(
                critique="c", unaddressed_risks=[], fatal_flaw_found=False
            ).model_dump(mode="json")
            tool_name = info.output_tools[0].name if info.output_tools else "final_result"
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

        with tempfile.TemporaryDirectory() as tmp:
            reports = await review_board(
                _package(), _novelty(), domain_review_agent=_build_domain_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=FunctionModel(respond),
            )
            by_id = {r.perspective: r for r in reports}
            self.assertFalse(by_id["statistics"].failed)
            self.assertEqual(2, calls["n"])

    async def test_persistent_failure_still_marks_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = _routes()
            routes["Review perspective: statistics"] = ValueError("always down")
            reports = await review_board(
                _package(), _novelty(), domain_review_agent=_build_domain_agent(),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=make_routed_model(routes),
            )
            by_id = {r.perspective: r for r in reports}
            self.assertTrue(by_id["statistics"].failed)


class SamplingProbabilityLeakTest(unittest.IsolatedAsyncioTestCase):
    def test_skeptic_judgment_schema_does_not_leak_the_field_name(self) -> None:
        # SkepticJudgment 是 pydantic-ai 的 output tool，它的 JSON schema（包括每个字段的
        # description）会整个发给 provider——不是只有 messages 里的文本才会到达模型面前。
        # 之前 critique 字段的 description 原文写着 "...generator's sampling_probability"，
        # str(messages) 断言看不到这个 schema，所以那条 leak 一直没被 test_no_perspective_
        # sees_the_generator_self_score 这类断言拦住。直接对 model_json_schema() 做字符串
        # 检查，零成本、不依赖 provider，且真的能看见 output tool 发出去的内容。
        schema_text = str(SkepticJudgment.model_json_schema())
        self.assertNotIn("sampling_probability", schema_text)

    async def test_no_perspective_sees_the_generator_self_score(self) -> None:
        # 从 test_validation.py 迁移而来的 Co-Scientist 核心不变量，现在覆盖全部三个视角
        captured: list[str] = []

        def capture_and_respond(messages, info):
            captured.append(str(messages))
            args = SkepticJudgment(
                critique="c", unaddressed_risks=[], fatal_flaw_found=False
            ).model_dump(mode="json")
            tool_name = info.output_tools[0].name if info.output_tools else "final_result"
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

        with tempfile.TemporaryDirectory() as tmp:
            provider = _StaticTextProvider()
            await review_board(
                _package(sampling_probability=0.42), _novelty(),
                domain_review_agent=_build_domain_agent(provider),
                artifacts=LocalArtifactStore(tmp), corpus_ref=_FAKE_CORPUS_REF,
                model=FunctionModel(capture_and_respond),
            )
            self.assertEqual(3, len(captured))
            for prompt_text in captured:
                self.assertNotIn("0.42", prompt_text)
                self.assertNotIn("sampling_probability", prompt_text)
            # 但候选内容本身必须在 prompt 里
            self.assertTrue(any("X causes Y" in text for text in captured))

            # domain_consistency 走 agent 检索循环、不走 model=，是独立的一条 prompt 构造
            # 路径，同一条不变量要在这条路径上单独核对，否则它可以悄悄泄漏而不被发现。
            self.assertEqual(1, provider.calls)
            for prompt_text in provider.captured_prompts:
                self.assertNotIn("0.42", prompt_text)
                self.assertNotIn("sampling_probability", prompt_text)
            self.assertTrue(any("X causes Y" in text for text in provider.captured_prompts))
