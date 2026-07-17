"""Unit tests for the IdeaGenerator langgraph agent."""

import unittest

from athena.agents.search.research_agent import generate_hypothesis
from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    HypothesisDraft,
    ResearchProblemInput,
)
from unit.fakes import FakeChatModel


def _problem() -> ResearchProblemInput:
    return ResearchProblemInput(
        question="Does X affect Y?",
        domain="biology",
        objective="find a testable mechanism",
        constraints=[],
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


class GenerateHypothesisTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_on_first_attempt(self) -> None:
        fake = FakeChatModel([_valid_draft()])
        node, package = await generate_hypothesis(_problem(), model=fake)
        self.assertEqual(node.node_id, package.idea_id)
        self.assertEqual("X causally increases Y", package.novel_hypothesis)
        self.assertEqual(["ev-0"], node.evidence_refs)
        self.assertEqual(f"inline://{node.node_id}", node.package_ref)

    async def test_retries_once_then_succeeds(self) -> None:
        fake = FakeChatModel([ValueError("bad json"), _valid_draft()])
        node, package = await generate_hypothesis(_problem(), model=fake)
        self.assertEqual(node.node_id, package.idea_id)

    async def test_raises_after_exhausting_retries(self) -> None:
        fake = FakeChatModel([ValueError("bad json"), ValueError("bad json again")])

        # 只断言"最终抛出 ValueError"不够：_generate_node 的重试循环用 `except Exception` 兜底，
        # 如果 MAX_GENERATION_ATTEMPTS 回归成 3，第 3 次尝试会因为 FakeChatModel 的结果队列已经
        # 耗尽（只预置了 2 个结果）而命中 fakes.py 里的 AssertionError（"ran out of scripted
        # responses"）——但 _FakeStructuredRunnable.ainvoke 在队列为空时不会再 pop，所以这第 3 次
        # 尝试同样会被 `except Exception` 吞掉、重新包装成 ValueError，最终仍然是 assertRaises(
        # ValueError) 通过，掩盖了多重试一次的回归。要真正证明恰好只调用了 2 次 LLM，必须直接对调用
        # 次数计数，而不是依赖队列耗尽后的副作用（队列耗尽状态在 2 次和 3+ 次尝试后是一样的，无法
        # 用它反推调用次数）。
        call_count = 0
        original_with_structured_output = fake.with_structured_output

        def counting_with_structured_output(schema: type) -> object:
            nonlocal call_count
            call_count += 1
            return original_with_structured_output(schema)

        fake.with_structured_output = counting_with_structured_output

        with self.assertRaises(ValueError):
            await generate_hypothesis(_problem(), model=fake)

        self.assertEqual(2, call_count)
