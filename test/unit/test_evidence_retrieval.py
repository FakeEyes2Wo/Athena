"""Unit tests for ResearchGapMiner and NoveltyEvidenceCollector (Agent + paper_rag two-phase pattern)."""

import json
import tempfile
import unittest

from athena.core.agent import Agent, AgentConfig, StreamEvent
from athena.core.tool import ToolRegistry
from athena.research.paper_rag.index import build_corpus_index
from athena.research.paper_rag.tool import PaperKeywordSearchTool
from athena.storage import LocalArtifactStore
from athena.workflows.prompts import GAP_MINER_SYSTEM_PROMPT, NOVELTY_SYSTEM_PROMPT
from athena.workflows.search.evidence_retrieval import (
    build_gap_miner_agent,
    build_novelty_agent,
    build_novelty_question,
    collect_novelty_evidence,
    mine_research_gaps,
)
from athena.workflows.search.idea_schemas import (
    GapMiningResponse,
    GapCandidateDraft,
    HypothesisPackage,
    NoveltyEvidenceJudgment,
    NoveltyEvidenceReport,
    EvidenceRef,
    ResearchProblemInput,
)
from unit.fakes import make_scripted_model
from unit.test_paper_rag import make_paper


def _problem() -> ResearchProblemInput:
    return ResearchProblemInput(question="Does X affect Y?", domain="biology", objective="find a mechanism")


# 占位 corpus_ref：MineResearchGapsTest 用的 _TextOnlyProvider 永远不会真的调用 paper_rag
# 工具，所以只需要一个格式合法的 sha256 引用，不必指向真实语料索引。
_FAKE_CORPUS_REF = "sha256:" + "a" * 64


def _package(sampling_probability: float = 0.42) -> HypothesisPackage:
    return HypothesisPackage(
        idea_id="idea-1", generation_strategy="s", novel_hypothesis="X causes Y",
        sampling_probability=sampling_probability,
        supported_premises=[], inference_chain=[], predicted_observations=["Y increases"],
        disconfirming_observations=["Y stays flat"], lineage_op="generate",
    )


class _TextOnlyProvider:
    """回放一段固定文本、不调用任何工具的假 provider，用来单测检索阶段的文本采集逻辑。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def stream(self, *_args):
        yield StreamEvent("text_delta", {"delta": self._text, "accumulated": self._text})
        yield StreamEvent("response_completed")


class _ToolCallingProvider:
    """先触发一次 paper_keyword_search 工具调用，再回放文本，用来验证 channels_used 采集。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self._calls = 0

    async def stream(self, *_args):
        self._calls += 1
        if self._calls == 1:
            yield StreamEvent(
                "function_call",
                {"call_id": "1", "name": "paper_keyword_search",
                 "arguments": {"corpus_ref": self._corpus_ref, "keywords": ["X"]}},
            )
        else:
            yield StreamEvent("text_delta", {"delta": self._text, "accumulated": self._text})
        yield StreamEvent("response_completed")


def _build_agent(provider) -> Agent:
    agent = Agent(AgentConfig(model="test-model", system_prompt="system", tools=ToolRegistry()))
    agent._provider = provider
    return agent


def _build_retrieval_agent() -> Agent:
    """构造一个只回放固定文本、不调用工具的检索 Agent，配合 _FAKE_CORPUS_REF 使用。"""
    return _build_agent(_TextOnlyProvider("found one closely related prior work"))


def _clean_novelty_judgment() -> NoveltyEvidenceJudgment:
    """一份无风险信号的 NoveltyEvidenceJudgment，供只关心 report 结构而非具体打分的用例复用。"""
    return NoveltyEvidenceJudgment(
        nearest_work=[], facet_overlap={}, coverage_estimate=0.5, unrecalled_risk=0.3,
        citation_cutoff_ok=True, retrieval_cutoff_ok=True, post_cutoff_similarity=0.1,
        possible_memorization=False, leakage_risk=0.1, historical_backtest_validity=True,
        uncertainty=0.2,
    )


class BuildRetrievalAgentTest(unittest.TestCase):
    def test_build_gap_miner_agent_binds_correct_system_prompt(self) -> None:
        agent = build_gap_miner_agent("test-model", ToolRegistry(), client=None)
        self.assertEqual(GAP_MINER_SYSTEM_PROMPT, agent.config.system_prompt)
        self.assertEqual("research_gap_miner", agent.name)

    def test_build_novelty_agent_binds_correct_system_prompt(self) -> None:
        agent = build_novelty_agent("test-model", ToolRegistry(), client=None)
        self.assertEqual(NOVELTY_SYSTEM_PROMPT, agent.config.system_prompt)
        self.assertEqual("novelty_evidence_collector", agent.name)


class MineResearchGapsTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_gaps_from_structured_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            agent = _build_agent(_TextOnlyProvider("no clear gap found in the retrieved text"))
            model = make_scripted_model([
                GapMiningResponse(gaps=[GapCandidateDraft(gap_type="open_problem", description="d")])
            ])
            gaps = await mine_research_gaps(
                _problem(), agent=agent, corpus_ref=_FAKE_CORPUS_REF, artifacts=artifacts, model=model,
            )
            self.assertEqual(1, len(gaps))
            self.assertEqual("open_problem", gaps[0].gap_type)
            self.assertTrue(gaps[0].context_ref.startswith("sha256:"))

    async def test_empty_gaps_list_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            agent = _build_agent(_TextOnlyProvider("nothing found"))
            model = make_scripted_model([GapMiningResponse(gaps=[])])
            gaps = await mine_research_gaps(
                _problem(), agent=agent, corpus_ref=_FAKE_CORPUS_REF, artifacts=artifacts, model=model,
            )
            self.assertEqual([], gaps)

    async def test_all_gaps_from_one_run_share_the_same_context_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            agent = _build_agent(_TextOnlyProvider("two gaps found"))
            model = make_scripted_model([GapMiningResponse(gaps=[
                GapCandidateDraft(gap_type="open_problem", description="d1"),
                GapCandidateDraft(gap_type="contradiction", description="d2"),
            ])])
            gaps = await mine_research_gaps(
                _problem(), agent=agent, corpus_ref=_FAKE_CORPUS_REF, artifacts=artifacts, model=model,
            )
            self.assertEqual(gaps[0].context_ref, gaps[1].context_ref)


class CollectNoveltyEvidenceTest(unittest.IsolatedAsyncioTestCase):
    async def _corpus_ref(self, artifacts: LocalArtifactStore) -> str:
        paper = await make_paper(artifacts, "p1", "Paper One", ["X has been shown to cause Y in prior work."])
        return await build_corpus_index(artifacts, [paper])

    async def test_report_has_no_verdict_and_carries_persisted_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            corpus_ref = await self._corpus_ref(artifacts)
            agent = _build_agent(_TextOnlyProvider("found one close prior work"))
            judgment = NoveltyEvidenceJudgment(
                nearest_work=[EvidenceRef(ref_id="c0", kind="paragraph")],
                facet_overlap={"problem": 0.6}, coverage_estimate=0.5, unrecalled_risk=0.3,
                citation_cutoff_ok=True, retrieval_cutoff_ok=True, post_cutoff_similarity=0.1,
                possible_memorization=False, leakage_risk=0.1, historical_backtest_validity=True,
                uncertainty=0.2,
            )
            model = make_scripted_model([judgment])
            report = await collect_novelty_evidence(
                _package(), agent=agent, artifacts=artifacts, corpus_ref=corpus_ref, model=model,
            )
            self.assertNotIn("verdict", type(report).model_fields)
            self.assertTrue(report.coverage_ref.startswith("sha256:"))
            self.assertTrue(report.temporal_ref.startswith("sha256:"))
            self.assertEqual(0.2, report.uncertainty)

    async def test_channels_used_records_actually_invoked_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            corpus_ref = await self._corpus_ref(artifacts)
            provider = _ToolCallingProvider("summary text")
            provider._corpus_ref = corpus_ref
            tools = ToolRegistry()
            tools.register(PaperKeywordSearchTool(artifacts))
            agent = Agent(AgentConfig(model="test-model", system_prompt="system", tools=tools))
            agent._provider = provider
            judgment = NoveltyEvidenceJudgment(
                nearest_work=[], facet_overlap={}, coverage_estimate=0.5, unrecalled_risk=0.3,
                citation_cutoff_ok=True, retrieval_cutoff_ok=True, post_cutoff_similarity=0.1,
                possible_memorization=False, leakage_risk=0.1, historical_backtest_validity=True,
                uncertainty=0.2,
            )
            model = make_scripted_model([judgment])
            report = await collect_novelty_evidence(
                _package(), agent=agent, artifacts=artifacts, corpus_ref=corpus_ref, model=model,
            )
            coverage_text = await artifacts.get_text(report.coverage_ref)
            self.assertIn("paper_keyword_search", coverage_text)


class NoveltyQueryLogRefTest(unittest.IsolatedAsyncioTestCase):
    async def test_report_exposes_query_log_ref_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = LocalArtifactStore(tmp)
            model = make_scripted_model([_clean_novelty_judgment()])
            report = await collect_novelty_evidence(
                _package(), agent=_build_retrieval_agent(), artifacts=artifacts,
                corpus_ref=_FAKE_CORPUS_REF, model=model,
            )
            self.assertIsNotNone(report.query_log_ref)
            self.assertTrue(report.query_log_ref.startswith("sha256:"))
            # 与 RetrievalCoverage 内部记录的是同一个引用，不是另存一份
            coverage = json.loads(await artifacts.get_text(report.coverage_ref))
            self.assertEqual(coverage["query_log_ref"], report.query_log_ref)

    def test_query_log_ref_defaults_to_none(self) -> None:
        # novelty 失败降级产出的空报告没有转录，此时 domain_consistency 退回完整检索
        report = NoveltyEvidenceReport(
            idea_id="idea-1", nearest_work=[], facet_overlap={},
            coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64,
            uncertainty=0.5,
        )
        self.assertIsNone(report.query_log_ref)


class BuildNoveltyQuestionTest(unittest.TestCase):
    def test_question_carries_hypothesis_corpus_and_predictions(self) -> None:
        question = build_novelty_question(_package(), _FAKE_CORPUS_REF)
        self.assertIn("X causes Y", question)
        self.assertIn(_FAKE_CORPUS_REF, question)
        self.assertIn("Y increases", question)

    def test_question_never_carries_sampling_probability(self) -> None:
        # novelty 提问同样属于"审阅侧"，不得锚定生成侧自评概率
        question = build_novelty_question(_package(sampling_probability=0.42), _FAKE_CORPUS_REF)
        self.assertNotIn("0.42", question)
        self.assertNotIn("sampling_probability", question)


class NoveltyInputFingerprintTest(unittest.IsolatedAsyncioTestCase):
    async def test_novelty_report_records_its_question_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalArtifactStore(tmp)
            report = await collect_novelty_evidence(
                _package(), agent=_build_retrieval_agent(), artifacts=store,
                corpus_ref=_FAKE_CORPUS_REF, model=make_scripted_model([_clean_novelty_judgment()]),
            )
            self.assertEqual(
                build_novelty_question(_package(), _FAKE_CORPUS_REF),
                await store.get_text(report.input_ref),
            )
