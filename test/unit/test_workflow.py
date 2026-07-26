"""Unit tests for the pre_gate async pipeline (run_pre_gate)."""

import unittest

from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    GateVerdict,
    HypothesisDraft,
    ResearchProblemInput,
)
from athena.workflows.search.workflow import run_pre_gate
from unit.fakes import make_scripted_model


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
