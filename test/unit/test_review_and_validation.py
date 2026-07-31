"""Unit tests for SkepticReviewer, VerifierRegistry, and ValidationPlanner."""

import tempfile
import unittest

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from athena.storage import LocalArtifactStore
from athena.workflows.search.idea_schemas import HypothesisPackage, SkepticJudgment
from athena.workflows.search.review_and_validation import (
    BUILTIN_VERIFIERS,
    match_verifier,
    plan_validation,
    skeptic_review,
)
from unit.fakes import make_scripted_model


def _package(sampling_probability: float = 0.9) -> HypothesisPackage:
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=sampling_probability, supported_premises=[], inference_chain=[],
        predicted_observations=["Y increases"], disconfirming_observations=["Y stays flat"],
        lineage_op="generate",
    )


class SkepticReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_maps_judgment_to_report(self) -> None:
        model = make_scripted_model([
            SkepticJudgment(critique="premise is weak", unaddressed_risks=["confound"], fatal_flaw_found=False)
        ])
        report = await skeptic_review(_package(), model=model)
        self.assertEqual("idea-1", report.idea_id)
        self.assertEqual(["confound"], report.unaddressed_risks)
        self.assertFalse(report.fatal_flaw_found)

    async def test_runs_with_a_high_sampling_probability_candidate_too(self) -> None:
        # sampling_probability 本身不会被读取用来构造 prompt(见 review_and_validation.py 的
        # skeptic_review 实现，只取 novel_hypothesis/premises/predictions/disconfirmers)，
        # 这里只需确认不同 sampling_probability 的候选都能正常跑通，不会意外影响审阅结果。
        model = make_scripted_model([
            SkepticJudgment(critique="c", unaddressed_risks=[], fatal_flaw_found=False)
        ])
        report = await skeptic_review(_package(sampling_probability=0.42), model=model)
        self.assertEqual("idea-1", report.idea_id)

    async def test_skeptic_review_excludes_sampling_probability_from_prompt(self) -> None:
        # 验证 skeptic_review 不会把 sampling_probability 泄漏到 LLM prompt 中。
        # 这是 Co-Scientist 设计的核心不变量：审阅侧必须看不到生成侧的自评置信度。
        captured_messages: list[ModelMessage] = []

        def capture_and_respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            captured_messages.extend(messages)
            # 返回预设结果，忽略实际消息内容
            args = SkepticJudgment(
                critique="test", unaddressed_risks=[], fatal_flaw_found=False
            ).model_dump(mode="json")
            tool_name = info.output_tools[0].name if info.output_tools else "final_result"
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

        model = FunctionModel(capture_and_respond)
        test_probability = 0.42
        package = _package(sampling_probability=test_probability)

        # 运行 skeptic_review
        await skeptic_review(package, model=model)

        # 检查 prompt 文本中不含 sampling_probability 值
        prompt_text = str(captured_messages)
        self.assertNotIn("0.42", prompt_text)
        self.assertNotIn("sampling_probability", prompt_text)
        # 但应该包含其他关键字段
        self.assertIn("X causes Y", prompt_text)  # novel_hypothesis
        self.assertIn("Y increases", prompt_text)  # predicted_observations


class VerifierRegistryTest(unittest.TestCase):
    def test_matches_known_domain(self) -> None:
        verifier = match_verifier(_package(), "biology")
        self.assertIsNotNone(verifier)
        self.assertEqual("controlled_experiment_ttest", verifier.verifier_type)

    def test_returns_none_for_unknown_domain(self) -> None:
        self.assertIsNone(match_verifier(_package(), "underwater_basket_weaving"))

    def test_domain_matching_is_case_and_space_insensitive(self) -> None:
        verifier = match_verifier(_package(), "Machine Learning")
        self.assertIsNotNone(verifier)
        self.assertEqual("ablation_replication", verifier.verifier_type)

    def test_builtin_registry_is_non_empty(self) -> None:
        self.assertGreater(len(BUILTIN_VERIFIERS), 0)


class PlanValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_none_verifier_produces_exploratory_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            plan = await plan_validation(_package(), None, artifacts=artifacts)
            self.assertIsNone(plan.verifier)
            self.assertIn("EXPLORATORY", plan.decision_rule)
            self.assertTrue(plan.estimated_cost_ref.startswith("sha256:"))

    async def test_matched_verifier_produces_bound_plan_with_real_cost_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            verifier = match_verifier(_package(), "biology")
            plan = await plan_validation(_package(), verifier, artifacts=artifacts)
            self.assertIsNotNone(plan.verifier)
            self.assertTrue(plan.verifier.cost_ref.startswith("sha256:"))
            self.assertTrue(plan.estimated_cost_ref.startswith("sha256:"))
