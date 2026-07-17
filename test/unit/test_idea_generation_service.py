"""End-to-end unit tests for run_pre_gate (fully offline via FakeChatModel)."""

import unittest

from athena.workflows.search.idea_generation_service import run_pre_gate
from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    GateVerdict,
    HypothesisDraft,
    ResearchProblemInput,
)
from unit.fakes import FakeChatModel


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


class RunPreGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_pass_end_to_end(self) -> None:
        # 队列顺序：第一次消费在 generate_hypothesis 里，第二次消费在 falsifiability_check 里
        fake = FakeChatModel([
            _valid_draft(),
            FalsifiabilityJudgment(
                testable_implication="Measure Y after X knockout",
                unobservable_variables=[],
                is_falsifiable=True,
            ),
        ])
        node, package, decision = await run_pre_gate(_problem(), model=fake)
        self.assertEqual(GateVerdict.PASS, decision.verdict)
        self.assertEqual("GATED_PASS", node.status)
        self.assertEqual(node.node_id, package.idea_id)

    async def test_revise_when_falsifiability_fails(self) -> None:
        fake = FakeChatModel([
            _valid_draft(),
            FalsifiabilityJudgment(
                testable_implication="",
                unobservable_variables=["internal state"],
                is_falsifiable=False,
            ),
        ])
        node, _, decision = await run_pre_gate(_problem(), model=fake)
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("falsifiable", decision.blocking_factor)
        self.assertEqual("REVISION_REQUESTED", node.status)
