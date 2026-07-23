"""Unit tests for the pre_gate langgraph workflow (single CompiledGraph)."""

import unittest

from athena.workflows.search.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    GateVerdict,
    HypothesisDraft,
    ResearchProblemInput,
)
from athena.workflows.search.workflow import PRE_GATE_GRAPH, run_pre_gate
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


class PreGateGraphStructureTest(unittest.TestCase):
    def test_graph_has_all_four_nodes_on_one_compiled_graph(self) -> None:
        # 反馈要点:不应该是"分布式 CompiledGraph"——四个步骤必须都是同一张编译图上的节点，
        # 而不是图外面再用普通函数串联
        node_names = set(PRE_GATE_GRAPH.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(
            {"generate", "structural_check", "falsifiability_check", "gate"}, node_names
        )


class RunPreGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_pass_end_to_end_via_compiled_graph(self) -> None:
        fake = FakeChatModel([_valid_draft(), _falsifiability_judgment(True)])
        node, package, decision = await run_pre_gate(_problem(), model=fake)
        self.assertEqual(GateVerdict.PASS, decision.verdict)
        self.assertEqual("GATED_PASS", node.status)
        self.assertEqual(node.node_id, package.idea_id)

    async def test_revise_when_falsifiability_fails(self) -> None:
        fake = FakeChatModel([_valid_draft(), _falsifiability_judgment(False)])
        node, _, decision = await run_pre_gate(_problem(), model=fake)
        self.assertEqual(GateVerdict.REVISE, decision.verdict)
        self.assertEqual("falsifiable", decision.blocking_factor)
        self.assertEqual("REVISION_REQUESTED", node.status)

    async def test_retries_once_then_succeeds(self) -> None:
        fake = FakeChatModel([
            ValueError("bad json"), _valid_draft(), _falsifiability_judgment(True),
        ])
        node, package, decision = await run_pre_gate(_problem(), model=fake)
        self.assertEqual(node.node_id, package.idea_id)
        self.assertEqual(GateVerdict.PASS, decision.verdict)

    async def test_raises_after_exhausting_generation_retries(self) -> None:
        fake = FakeChatModel([ValueError("bad json"), ValueError("bad json again")])
        with self.assertRaises(ValueError):
            await run_pre_gate(_problem(), model=fake)
