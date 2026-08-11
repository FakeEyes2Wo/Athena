"""Tests for the PaperScout multi-turn paper search agent."""

import asyncio
import json
import tempfile
import unittest
import zlib

from pydantic import ValidationError

from athena.core.agent.agent import AgentContext
from athena.core.schemas import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.research.paper_scout.agent import PaperScoutAgent
from athena.research.paper_scout.backends import (
    SEMANTIC_SCHOLAR_FIELDS,
    ArxivSearchBackend,
    BackendError,
    SemanticScholarBackend,
    open_access_pdf,
    paper_key_for,
    within_cutoff,
)
from athena.research.paper_scout.pool import (
    EMPTY_POOL,
    PaperPool,
    has_retrievable_source,
    truncate_abstract,
)
from athena.research.paper_scout.prompts import format_history
from athena.research.paper_scout.schemas import (
    ACCEPT_THRESHOLD,
    PASA_RETAIN_THRESHOLD,
    RETAIN_THRESHOLD,
    PaperScoutResult,
    ScoutCorpus,
    ScoutPaper,
    ScoutRequest,
    ScoutStats,
)
from athena.research.paper_scout.scorer import (
    DEFAULT_BATCH_SIZE,
    GradedRelevanceScorer,
    parse_grades,
    true_probability,
)
from athena.research.paper_scout.session import ScoutSession, process_reward
from athena.research.paper_scout.tool import (
    PaperScoutExpandTool,
    PaperScoutSearchTool,
)
from athena.research.paper_source.http import HostRateLimiter, HttpResponse
from athena.research.paper_source.schemas import PaperSourceRequest
from athena.storage import LocalArtifactStore

ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2009.02040v1</id>
    <title>Multivariate Time-series Anomaly Detection via Graph Attention Network</title>
    <summary>We propose a self-supervised framework.</summary>
    <published>2020-09-04T00:00:00Z</published>
    <updated>2020-09-04T00:00:00Z</updated>
    <author><name>Hang Zhao</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2405.11111v1</id>
    <title>A Much Later Paper</title>
    <summary>Published after the benchmark cutoff.</summary>
    <published>2024-05-01T00:00:00Z</published>
    <updated>2024-05-01T00:00:00Z</updated>
    <author><name>Someone Else</name></author>
  </entry>
</feed>
"""

S2_SEARCH = json.dumps(
    {
        "data": [
            {
                "paperId": "abc123",
                "title": "Deep Learning for Anomaly Detection",
                "abstract": "A survey.",
                "externalIds": {"ArXiv": "2103.04036", "DOI": "10.1145/1234"},
                "year": 2021,
                "publicationDate": "2021-03-06",
                "citationCount": 42,
            }
        ]
    }
).encode("utf-8")

S2_REFERENCES = json.dumps(
    {
        "data": [
            {
                "citedPaper": {
                    "paperId": "def456",
                    "title": "An Older Referenced Paper",
                    "abstract": "Foundational work.",
                    "externalIds": {"ArXiv": "1706.03762"},
                    "year": 2017,
                    "publicationDate": "2017-06-12",
                }
            },
            {"citedPaper": {"title": ""}},
        ]
    }
).encode("utf-8")


class StubTransport:
    """Returns a canned response per URL substring, recording every request."""

    def __init__(self, routes: dict[str, HttpResponse]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    async def get(self, url: str, headers: dict) -> HttpResponse:
        self.urls.append(url)
        for marker, response in self.routes.items():
            if marker in url:
                return response
        return HttpResponse(status=404, url=url, body=b"", headers={})


class StubScorer:
    """Scores papers by looking their title up in a fixed table."""

    model = "stub-scorer"

    def __init__(self, scores: dict[str, float], default: float = 0.0) -> None:
        self.scores = scores
        self.default = default
        self.calls = 0
        self.seen: list[str] = []

    async def score(self, query: str, papers: list[ScoutPaper]) -> list[float]:
        self.calls += 1
        self.seen.extend(paper.title for paper in papers)
        return [self.scores.get(paper.title, self.default) for paper in papers]


def limiter(transport: StubTransport) -> HostRateLimiter:
    """A rate limiter with no waiting, so tests never sleep."""

    async def no_sleep(_seconds: float) -> None:
        return None

    return HostRateLimiter(
        transport=transport,
        default_interval=0.0,
        bucket_intervals={"arxiv.org": 0.0, "api.semanticscholar.org": 0.0},
        max_retries=0,
        sleeper=no_sleep,
    )


def aid(key: str) -> str:
    """Map a short fixture key to a well-formed arXiv id.

    PaperIdentity validates the identifier format, so fixtures cannot use bare
    counters like "1" once the delivered set is turned into a paper_source request.
    """
    return "2401.%05d" % (zlib.crc32(key.encode()) % 90000 + 10000)


def paper(key: str, title: str, score: float, expanded: bool = False) -> ScoutPaper:
    return ScoutPaper(
        paper_key=f"arxiv:{aid(key)}",
        arxiv_id=aid(key),
        title=title,
        abstract="abstract text",
        source="search",
        relevance=score,
        expanded=expanded,
    )


def journal_paper(
    doi: str, title: str, score: float, open_access_pdf: str = ""
) -> ScoutPaper:
    """一篇没有 arXiv id 的期刊论文；给了 ``open_access_pdf`` 就是能下载的那种。"""
    return ScoutPaper(
        paper_key=f"doi:{doi}",
        doi=doi,
        title=title,
        abstract="abstract text",
        source="search",
        channel="semantic_scholar",
        relevance=score,
        open_access_pdf=open_access_pdf,
        is_open_access=True if open_access_pdf else None,
    )


class PaperKeyTest(unittest.TestCase):
    def test_identifier_priority(self):
        self.assertEqual(
            paper_key_for("2009.02040", "10.1/x", "s2", "T"), "arxiv:2009.02040"
        )
        self.assertEqual(paper_key_for("", "10.1/x", "s2", "T"), "doi:10.1/x")
        self.assertEqual(paper_key_for("", "", "S2ID", "T"), "s2:s2id")

    def test_title_fallback_normalises(self):
        self.assertEqual(
            paper_key_for("", "", "", "Attention Is All You Need!"),
            "title:attentionisallyouneed",
        )


class CutoffTest(unittest.TestCase):
    def test_empty_cutoff_keeps_everything(self):
        self.assertTrue(within_cutoff("2405.00001", "2024-05-01", ""))

    def test_arxiv_prefix_decides_before_the_date(self):
        self.assertTrue(within_cutoff("2403.00001", "", "2024-04-01"))
        self.assertFalse(within_cutoff("2405.00001", "", "2024-04-01"))

    def test_published_date_used_without_an_arxiv_id(self):
        self.assertTrue(within_cutoff("", "2020-01-01", "2024-04-01"))
        self.assertFalse(within_cutoff("", "2025-01-01", "2024-04-01"))

    def test_unknown_date_is_kept(self):
        self.assertTrue(within_cutoff("", "", "2024-04-01"))


class PoolTest(unittest.TestCase):
    def test_empty_pool_renders_a_notice(self):
        self.assertEqual(PaperPool().observation(), EMPTY_POOL)

    def test_add_is_idempotent_and_keeps_the_first_record(self):
        pool = PaperPool()
        self.assertTrue(pool.add(paper("1", "First", 0.9)))
        self.assertFalse(pool.add(paper("1", "Duplicate", 0.1)))
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.get(f"arxiv:{aid('1')}").title, "First")

    def test_same_title_under_different_identifiers_is_one_paper(self):
        pool = PaperPool()
        first = ScoutPaper(
            paper_key="s2:aaa",
            s2_paper_id="aaa",
            title="A Neural Model",
            source="search",
        )
        second = ScoutPaper(
            paper_key="s2:bbb",
            s2_paper_id="bbb",
            title="A neural model!",
            source="search",
        )
        self.assertTrue(pool.add(first))
        self.assertFalse(pool.add(second))
        self.assertTrue(pool.contains(second))
        self.assertEqual(len(pool), 1)

    def test_ranked_orders_by_relevance_descending(self):
        pool = PaperPool()
        for index, score in enumerate([0.2, 0.9, 0.5]):
            pool.add(paper(str(index), f"P{index}", score))
        self.assertEqual([item.relevance for item in pool.ranked()], [0.9, 0.5, 0.2])

    def test_mark_expanded_only_succeeds_once(self):
        pool = PaperPool()
        pool.add(paper("1", "First", 0.9))
        self.assertTrue(pool.mark_expanded(f"arxiv:{aid('1')}"))
        self.assertFalse(pool.mark_expanded(f"arxiv:{aid('1')}"))
        self.assertFalse(pool.mark_expanded("arxiv:missing"))

    def test_retained_applies_threshold_and_limit(self):
        pool = PaperPool()
        pool.add(paper("1", "High", 0.9))
        pool.add(paper("2", "Mid", 0.6))
        pool.add(paper("3", "Low", 0.2))
        self.assertEqual(len(pool.retained(PASA_RETAIN_THRESHOLD)), 2)
        self.assertEqual(len(pool.retained(PASA_RETAIN_THRESHOLD, 1)), 1)

    def test_observation_caps_each_list_at_ten(self):
        pool = PaperPool()
        for index in range(15):
            pool.add(paper(f"e{index}", f"Expanded {index}", 0.9, expanded=True))
            pool.add(paper(f"n{index}", f"New {index}", 0.8))
        entries = pool.observation().split("\n\n")[1:]
        self.assertEqual(sum(1 for item in entries if "[EXP]" in item), 10)
        self.assertEqual(sum(1 for item in entries if "[NEW]" in item), 10)

    def test_truncate_abstract_appends_an_ellipsis(self):
        self.assertEqual(truncate_abstract("a b c", 2), "a b...")
        self.assertEqual(truncate_abstract("a b", 2), "a b")


class PromptTest(unittest.TestCase):
    def test_history_renders_both_action_kinds(self):
        rendered = format_history(
            [("search", "graph anomaly"), ("expand", "2009.02040")]
        )
        self.assertEqual(rendered, "[Search] graph anomaly\n[Expand] 2009.02040")

    def test_empty_history_is_none(self):
        self.assertEqual(format_history([]), "None")


class ScorerTest(unittest.TestCase):
    def test_grades_are_mapped_into_the_unit_interval(self):
        self.assertEqual(parse_grades('{"1": 3, "2": 0}', 2), [1.0, 0.0])

    def test_partial_relevance_stays_below_the_retention_threshold(self):
        scores = parse_grades('{"1": 2, "2": 1}', 2)
        self.assertTrue(all(score < PASA_RETAIN_THRESHOLD for score in scores))
        self.assertTrue(all(score >= ACCEPT_THRESHOLD for score in scores))

    def test_only_a_full_match_clears_the_retention_threshold(self):
        self.assertGreaterEqual(parse_grades('{"1": 3}', 1)[0], PASA_RETAIN_THRESHOLD)

    def test_missing_entries_score_zero(self):
        self.assertEqual(parse_grades('{"1": 3}', 2), [1.0, 0.0])

    def test_surrounding_prose_is_tolerated(self):
        self.assertEqual(parse_grades('Here you go: {"1": 2} done', 1), [0.45])

    def test_out_of_range_grades_are_clamped(self):
        self.assertEqual(parse_grades('{"1": 9, "2": -4}', 2), [1.0, 0.0])

    def test_unparsable_output_scores_zero(self):
        self.assertEqual(parse_grades("no json at all", 2), [0.0, 0.0])

    def test_true_probability_is_none_without_logprobs(self):
        self.assertIsNone(true_probability(None))


class StubScoringClient:
    """记录每次打分请求的 prompt，可按论文标题触发一次失败。"""

    def __init__(self, reply: str, fail_on_title: str = "") -> None:
        self.reply = reply
        self.fail_on_title = fail_on_title
        self.prompts: list[str] = []
        self.chat = self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        if self.fail_on_title and self.fail_on_title in prompt:
            raise RuntimeError("scoring endpoint refused the batch")
        message = type("Message", (), {"content": self.reply})()
        choice = type("Choice", (), {"message": message})()
        return type("Reply", (), {"choices": [choice]})()


class ScorerBatchTest(unittest.IsolatedAsyncioTestCase):
    """批次大小决定请求数，而请求数是 scout 墙钟的主要来源。"""

    def test_default_batch_size_reflects_the_measured_tradeoff(self):
        """8 篇要 15.8 秒、24 篇要 23.7 秒——三倍的量只多花五成时间。"""
        self.assertEqual(24, DEFAULT_BATCH_SIZE)

    async def test_papers_are_split_into_batches_of_the_configured_size(self):
        client = StubScoringClient('{"1": 3, "2": 3, "3": 3}')
        scorer = GradedRelevanceScorer(client, "m", batch_size=3)

        scores = await scorer.score(
            "q", [paper(f"arxiv:{i}", f"T{i}", 0.0) for i in range(7)]
        )

        self.assertEqual(7, len(scores))
        self.assertEqual(3, scorer.calls)
        self.assertEqual(3, len(client.prompts))

    async def test_a_failed_batch_scores_zero_without_taking_down_the_rest(self):
        """整批按 0 分处理——打分失败绝不能把论文误判成高相关。

        批次越大，一次失败作废的论文越多，这是提高 ``batch_size`` 的代价。
        """
        client = StubScoringClient('{"1": 3, "2": 3}', fail_on_title="Doomed")
        scorer = GradedRelevanceScorer(client, "m", batch_size=2)
        papers = [
            paper("arxiv:1", "Fine one", 0.0),
            paper("arxiv:2", "Fine two", 0.0),
            paper("arxiv:3", "Doomed batch", 0.0),
            paper("arxiv:4", "Also doomed", 0.0),
        ]

        scores = await scorer.score("q", papers)

        self.assertEqual([1.0, 1.0, 0.0, 0.0], scores)


class RewardTest(unittest.TestCase):
    def test_top_three_above_threshold_minus_cost(self):
        self.assertAlmostEqual(process_reward([0.9, 0.5, 0.1], 0.1), 1.9)

    def test_only_the_best_three_count(self):
        self.assertAlmostEqual(process_reward([0.9] * 5, 0.05), 2.95)

    def test_no_accepted_papers_still_pays_the_cost(self):
        self.assertAlmostEqual(process_reward([], 0.1), -0.1)


class ArxivBackendTest(unittest.TestCase):
    def test_search_parses_entries_and_applies_the_cutoff(self):
        transport = StubTransport(
            {"export.arxiv.org": HttpResponse(200, "u", ATOM_FEED, {})}
        )
        backend = ArxivSearchBackend(limiter(transport))
        papers = asyncio.run(backend.search("anomaly detection", 10, "2024-04-01"))
        self.assertEqual([item.arxiv_id for item in papers], ["2009.02040"])
        self.assertEqual(papers[0].source, "search")
        self.assertEqual(papers[0].origin, "anomaly detection")
        self.assertIn("all%3Aanomaly+detection", transport.urls[0])

    def test_non_2xx_raises_backend_error(self):
        transport = StubTransport({"export.arxiv.org": HttpResponse(503, "u", b"", {})})
        backend = ArxivSearchBackend(limiter(transport))
        with self.assertRaises(BackendError):
            asyncio.run(backend.search("q", 5, ""))


class SemanticScholarBackendTest(unittest.TestCase):
    def test_search_maps_external_identifiers(self):
        transport = StubTransport(
            {"/paper/search": HttpResponse(200, "u", S2_SEARCH, {})}
        )
        backend = SemanticScholarBackend(limiter(transport), api_key="k")
        papers = asyncio.run(backend.search("anomaly", 5, ""))
        self.assertEqual(papers[0].arxiv_id, "2103.04036")
        self.assertEqual(papers[0].doi, "10.1145/1234")
        self.assertEqual(papers[0].citation_count, 42)

    def test_references_drop_untitled_entries(self):
        transport = StubTransport(
            {"/references": HttpResponse(200, "u", S2_REFERENCES, {})}
        )
        backend = SemanticScholarBackend(limiter(transport))
        seed = paper("2009.02040", "Seed", 0.9)
        refs = asyncio.run(backend.references(seed, 20))
        self.assertEqual([item.arxiv_id for item in refs], ["1706.03762"])
        self.assertEqual(refs[0].source, "expand")
        self.assertEqual(refs[0].origin, "Seed")

    def test_references_without_a_locator_return_nothing(self):
        transport = StubTransport({})
        backend = SemanticScholarBackend(limiter(transport))
        anonymous = ScoutPaper(paper_key="title:x", title="X", source="search")
        self.assertEqual(asyncio.run(backend.references(anonymous, 5)), [])
        self.assertEqual(transport.urls, [])


class StubBackend:
    """Search backend returning fixed papers, or raising a configured error."""

    def __init__(
        self, name: str, papers: list[ScoutPaper], error: Exception | None = None
    ):
        self.name = name
        self.papers = papers
        self.error = error
        self.queries: list[str] = []

    async def search(self, query: str, limit: int, cutoff: str) -> list[ScoutPaper]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.papers)

    async def references(self, paper: ScoutPaper, limit: int) -> list[ScoutPaper]:
        if self.error is not None:
            raise self.error
        return list(self.papers)


def session_for(
    backends: list, reference=None, scores: dict[str, float] | None = None, **kwargs
) -> ScoutSession:
    request = ScoutRequest(query="anomaly detection", **kwargs)
    return ScoutSession(request, backends, reference, StubScorer(scores or {}))


class SessionSearchTest(unittest.TestCase):
    def test_accepted_papers_enter_the_pool_with_their_score(self):
        backend = StubBackend("stub", [paper("1", "Relevant", 0.0)])
        session = session_for([backend], scores={"Relevant": 0.8})
        action = asyncio.run(session.search("graph anomaly"))
        self.assertEqual(action.accepted, 1)
        self.assertEqual(len(session.pool), 1)
        self.assertAlmostEqual(session.pool.get(f"arxiv:{aid('1')}").relevance, 0.8)

    def test_papers_below_tau_are_rejected(self):
        backend = StubBackend("stub", [paper("1", "Irrelevant", 0.0)])
        session = session_for([backend], scores={"Irrelevant": 0.0})
        action = asyncio.run(session.search("graph anomaly"))
        self.assertEqual(action.returned, 1)
        self.assertEqual(action.accepted, 0)
        self.assertEqual(len(session.pool), 0)

    def test_tau_boundary_is_inclusive(self):
        backend = StubBackend("stub", [paper("1", "Edge", 0.0)])
        session = session_for([backend], scores={"Edge": ACCEPT_THRESHOLD})
        asyncio.run(session.search("q"))
        self.assertEqual(len(session.pool), 1)

    def test_repeating_a_query_is_penalised_and_skips_the_backend(self):
        backend = StubBackend("stub", [paper("1", "Relevant", 0.0)])
        session = session_for([backend], scores={"Relevant": 0.8})
        asyncio.run(session.search("same"))
        action = asyncio.run(session.search("same"))
        self.assertTrue(action.repeated)
        self.assertAlmostEqual(action.reward, -0.5)
        self.assertEqual(backend.queries, ["same"])

    def test_duplicate_titles_from_different_backends_are_scored_once(self):
        arxiv_copy = ScoutPaper(
            paper_key="arxiv:2401.00001",
            arxiv_id="2401.00001",
            title="A Neural Model",
            source="search",
        )
        s2_copy = ScoutPaper(
            paper_key="s2:bbb",
            s2_paper_id="bbb",
            title="A neural model",
            source="search",
        )
        session = session_for(
            [StubBackend("a", [arxiv_copy]), StubBackend("b", [s2_copy])],
            scores={"A Neural Model": 0.9},
        )
        action = asyncio.run(session.search("q"))
        self.assertEqual(action.returned, 1)
        self.assertEqual(action.accepted, 1)
        self.assertEqual(session.scorer.seen, ["A Neural Model"])

    def test_results_are_deduplicated_across_backends(self):
        shared = paper("1", "Shared", 0.0)
        session = session_for(
            [StubBackend("a", [shared]), StubBackend("b", [shared])],
            scores={"Shared": 0.9},
        )
        action = asyncio.run(session.search("q"))
        self.assertEqual(action.returned, 1)
        self.assertEqual(len(session.pool), 1)

    def test_one_failing_backend_does_not_stop_the_other(self):
        session = session_for(
            [
                StubBackend("bad", [], error=BackendError("HTTP 429")),
                StubBackend("good", [paper("1", "Relevant", 0.0)]),
            ],
            scores={"Relevant": 0.9},
        )
        action = asyncio.run(session.search("q"))
        self.assertEqual(action.accepted, 1)
        self.assertTrue(session.errors)
        self.assertIn("bad", session.errors[0])

    def test_already_pooled_papers_are_not_rescored(self):
        backend = StubBackend("stub", [paper("1", "Relevant", 0.0)])
        session = session_for([backend], scores={"Relevant": 0.8})
        asyncio.run(session.search("first"))
        asyncio.run(session.search("second"))
        self.assertEqual(session.scorer.seen, ["Relevant"])


class SessionExpandTest(unittest.TestCase):
    def test_expanding_a_pooled_paper_adds_its_references(self):
        reference = StubBackend("refs", [paper("cited", "Cited", 0.0)])
        session = session_for([], reference=reference, scores={"Cited": 0.7})
        session.pool.add(paper("seed", "Seed", 0.9))
        action = asyncio.run(session.expand(aid("seed")))
        self.assertEqual(action.accepted, 1)
        self.assertTrue(session.pool.get(f"arxiv:{aid('seed')}").expanded)

    def test_expanding_an_unknown_paper_is_penalised(self):
        session = session_for([], reference=StubBackend("refs", []))
        action = asyncio.run(session.expand("9999.99999"))
        self.assertTrue(action.repeated)
        self.assertAlmostEqual(action.reward, -0.5)

    def test_expanding_twice_is_penalised(self):
        reference = StubBackend("refs", [paper("cited", "Cited", 0.0)])
        session = session_for([], reference=reference, scores={"Cited": 0.7})
        session.pool.add(paper("seed", "Seed", 0.9))
        first = asyncio.run(session.expand(aid("seed")))
        action = asyncio.run(session.expand(aid("seed")))
        self.assertFalse(first.repeated)
        self.assertTrue(action.repeated)

    def test_versioned_identifier_is_normalised(self):
        reference = StubBackend("refs", [])
        session = session_for([], reference=reference)
        seed = ScoutPaper(
            paper_key="arxiv:1706.03762",
            arxiv_id="1706.03762",
            title="Seed",
            source="search",
            relevance=0.9,
        )
        session.pool.add(seed)
        action = asyncio.run(session.expand("arXiv:1706.03762v5"))
        self.assertFalse(action.repeated)
        self.assertEqual(action.argument, "1706.03762")

    def test_reference_backend_failure_is_recorded(self):
        reference = StubBackend("refs", [], error=BackendError("HTTP 429"))
        session = session_for([], reference=reference)
        session.pool.add(paper("seed", "Seed", 0.9))
        action = asyncio.run(session.expand(aid("seed")))
        self.assertIn("refs", action.error)
        self.assertTrue(session.errors)


class ToolTest(unittest.TestCase):
    def test_tools_dispatch_through_the_registry(self):
        backend = StubBackend("stub", [paper("1", "Relevant", 0.0)])
        session = session_for([backend], scores={"Relevant": 0.9})
        registry = ToolRegistry()
        registry.register(PaperScoutSearchTool(session))
        registry.register(PaperScoutExpandTool(session))

        async def emit(_kind: str, _ref: str, _data=None) -> None:
            return None

        results = asyncio.run(
            registry.adispatch(
                [("paper_scout_search", "c1", {"query": "graph anomaly"})],
                emit,
                asyncio.Event(),
            )
        )
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].data["accepted"], 1)

    def test_both_tools_are_concurrency_safe(self):
        session = session_for([])
        self.assertTrue(PaperScoutSearchTool(session).spec.concurrency_safe)
        self.assertTrue(PaperScoutExpandTool(session).spec.concurrency_safe)

    def test_blank_query_is_rejected(self):
        session = session_for([])
        tool = PaperScoutSearchTool(session)
        ctx = ToolContext("t", "c", _noop_emit, asyncio.Event())
        result = asyncio.run(tool.ainvoke(ctx, query="  "))
        self.assertFalse(result.success)


async def _noop_emit(_kind: str, _ref: str, _data=None) -> None:
    return None


class ScriptedProvider:
    """Replays a fixed list of tool-call batches, one batch per policy step."""

    def __init__(self, batches: list[list[tuple[str, dict]]]) -> None:
        self.batches = batches
        self.prompts: list[str] = []

    async def stream(self, config, messages, cancel):
        self.prompts.append(str(messages[0].parts[0].content))
        index = len(self.prompts) - 1
        batch = self.batches[index] if index < len(self.batches) else []
        for position, (name, arguments) in enumerate(batch):
            yield _Event(
                "function_call",
                {
                    "call_id": f"c{index}{position}",
                    "name": name,
                    "arguments": arguments,
                },
            )
        yield _Event("response_completed", {"finish_reason": "stop"})


class _Event:
    def __init__(self, kind: str, data: dict) -> None:
        self.kind = kind
        self.data = data


def agent_context(request_ref: str) -> AgentContext:
    thread = AthenaThread(
        thread_id="t", session_id="s", status="running", context_ref="ctx"
    )
    turn = AthenaTurn(
        turn_id="turn", thread_id="t", request_ref=request_ref, status="running"
    )
    return AgentContext(thread, turn, _noop_emit, ToolRegistry(), asyncio.Event())


class AgentTest(unittest.TestCase):
    def run_agent(self, batches, backends, reference=None, scores=None, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = LocalArtifactStore(directory)
            request = ScoutRequest(query="anomaly detection", **kwargs)
            ref = asyncio.run(artifacts.put_text(request.model_dump_json()))
            agent = PaperScoutAgent(
                artifacts,
                backends,
                reference,
                StubScorer(scores or {}),
                model="stub-model",
            )
            agent._provider = ScriptedProvider(batches)
            outcome = asyncio.run(agent.run(agent_context(ref)))
            result = PaperScoutResult.model_validate_json(
                asyncio.run(artifacts.get_text(outcome.result_ref))
            )
            corpus = ScoutCorpus.model_validate_json(
                asyncio.run(artifacts.get_text(result.corpus_ref))
            )
            stats = ScoutStats.model_validate_json(
                asyncio.run(artifacts.get_text(result.stats_ref))
            )
            return result, corpus, stats, agent._provider

    def test_a_single_search_step_produces_a_retained_paper(self):
        result, corpus, stats, _ = self.run_agent(
            [[("paper_scout_search", {"query": "graph anomaly"})]],
            [StubBackend("stub", [paper("1", "Relevant", 0.0)])],
            scores={"Relevant": 0.9},
            max_steps=1,
        )
        self.assertEqual(result.paper_count, 1)
        self.assertEqual(corpus.retained[0].arxiv_id, aid("1"))
        self.assertEqual(stats.search_actions, 1)
        self.assertEqual(stats.stop_reason, "max_steps")

    def test_papers_below_an_explicit_threshold_are_excluded(self):
        result, corpus, _, _ = self.run_agent(
            [[("paper_scout_search", {"query": "q"})]],
            [StubBackend("stub", [paper("1", "Weak", 0.0)])],
            scores={"Weak": 0.2},
            max_steps=1,
            retain_threshold=PASA_RETAIN_THRESHOLD,
        )
        self.assertEqual(result.paper_count, 0)
        self.assertEqual(len(corpus.pool), 1)

    def test_unfetchable_papers_are_dropped_and_counted(self):
        """剔除要记数：交付变少可能是池子差，也可能是这批全在付费墙后。"""
        result, corpus, stats, _ = self.run_agent(
            [[("paper_scout_search", {"query": "q"})]],
            [
                StubBackend(
                    "stub",
                    [
                        paper("1", "Open", 0.0),
                        journal_paper("10.1/paywalled", "Closed", 0.0),
                    ],
                )
            ],
            scores={"Open": 0.9, "Closed": 0.9},
            max_steps=1,
        )
        self.assertEqual(result.paper_count, 1)
        self.assertEqual(len(corpus.pool), 2)
        self.assertEqual(stats.dropped_no_source, 1)

    def test_the_default_threshold_delivers_weak_papers_too(self):
        """默认 0 之下，进了池的论文都会交付；下游在全文层面自己重排。"""
        result, corpus, _, _ = self.run_agent(
            [[("paper_scout_search", {"query": "q"})]],
            [StubBackend("stub", [paper("1", "Weak", 0.0)])],
            scores={"Weak": 0.2},
            max_steps=1,
        )
        self.assertEqual(result.paper_count, 1)
        self.assertEqual(len(corpus.retained), len(corpus.pool))

    def test_an_empty_policy_response_stops_the_loop(self):
        _, _, stats, provider = self.run_agent(
            [[]], [StubBackend("stub", [])], max_steps=5
        )
        self.assertEqual(stats.stop_reason, "policy_returned_no_action")
        self.assertEqual(len(provider.prompts), 1)

    def test_three_idle_turns_terminate_the_run(self):
        repeat = [("paper_scout_search", {"query": "same"})]
        _, _, stats, provider = self.run_agent(
            [repeat] * 6,
            [StubBackend("stub", [paper("1", "Relevant", 0.0)])],
            scores={"Relevant": 0.9},
            max_steps=6,
        )
        self.assertEqual(stats.stop_reason, "pool_unchanged")
        self.assertEqual(len(provider.prompts), 4)

    def test_parallel_calls_are_capped_per_step(self):
        batch = [("paper_scout_search", {"query": f"q{index}"}) for index in range(5)]
        _, _, stats, _ = self.run_agent(
            [batch],
            [StubBackend("stub", [])],
            max_steps=1,
            max_parallel_calls=2,
        )
        self.assertEqual(stats.search_actions, 2)

    def test_observation_and_history_reach_the_second_prompt(self):
        _, _, _, provider = self.run_agent(
            [
                [("paper_scout_search", {"query": "graph anomaly"})],
                [("paper_scout_expand", {"arxiv_id": aid("1")})],
            ],
            [StubBackend("stub", [paper("1", "Relevant", 0.0)])],
            reference=StubBackend("refs", []),
            scores={"Relevant": 0.9},
            max_steps=2,
        )
        second = provider.prompts[1]
        self.assertIn("[Search] graph anomaly", second)
        self.assertIn("[NEW] Relevant", second)

    def test_backend_failure_marks_the_run_partial(self):
        result, _, stats, _ = self.run_agent(
            [[("paper_scout_search", {"query": "q"})]],
            [StubBackend("bad", [], error=BackendError("HTTP 429"))],
            max_steps=1,
        )
        self.assertEqual(result.status, "partial")
        self.assertTrue(stats.errors)
        self.assertEqual(result.warnings, ["bad"])

    def test_delivered_papers_become_a_paper_source_request(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = LocalArtifactStore(directory)
            request = ScoutRequest(query="anomaly detection", max_steps=1)
            ref = asyncio.run(artifacts.put_text(request.model_dump_json()))
            agent = PaperScoutAgent(
                artifacts,
                [StubBackend("stub", [paper("1", "Relevant", 0.0)])],
                None,
                StubScorer({"Relevant": 0.9}),
                model="stub-model",
            )
            agent._provider = ScriptedProvider(
                [[("paper_scout_search", {"query": "graph anomaly"})]]
            )
            outcome = asyncio.run(agent.run(agent_context(ref)))
            result = PaperScoutResult.model_validate_json(
                asyncio.run(artifacts.get_text(outcome.result_ref))
            )
            self.assertIsNotNone(result.paper_source_request_ref)
            source = PaperSourceRequest.model_validate_json(
                asyncio.run(artifacts.get_text(result.paper_source_request_ref))
            )
            self.assertEqual(len(source.papers), 1)
            self.assertEqual(source.papers[0].identity.arxiv_id, aid("1"))
            self.assertEqual(source.corpus_ref, result.corpus_ref)

    def test_papers_without_identifiers_yield_no_source_request(self):
        """``_paper_source_request`` 的标识符兜底 —— 只在关掉可取源过滤时才走得到。

        默认配置下纯标题的论文早在交付环节就被剔除了，但这层兜底仍要留着：它守的是
        ``PaperIdentity`` 的前置条件，而不是取源成功率。
        """
        with tempfile.TemporaryDirectory() as directory:
            artifacts = LocalArtifactStore(directory)
            request = ScoutRequest(
                query="anomaly detection", max_steps=1, require_retrievable_source=False
            )
            ref = asyncio.run(artifacts.put_text(request.model_dump_json()))
            titled = ScoutPaper(
                paper_key="title:only", title="Title Only", source="search"
            )
            agent = PaperScoutAgent(
                artifacts,
                [StubBackend("stub", [titled])],
                None,
                StubScorer({"Title Only": 0.9}),
                model="stub-model",
            )
            agent._provider = ScriptedProvider(
                [[("paper_scout_search", {"query": "q"})]]
            )
            outcome = asyncio.run(agent.run(agent_context(ref)))
            result = PaperScoutResult.model_validate_json(
                asyncio.run(artifacts.get_text(outcome.result_ref))
            )
            self.assertEqual(result.paper_count, 1)
            self.assertIsNone(result.paper_source_request_ref)

    def test_the_open_access_url_is_handed_to_paper_source_as_a_hint(self):
        """检索阶段已经拿到了下载链接，不传下去就得让 paper_source 再解析一次 OpenAlex。

        同一批里放一篇 arXiv 论文作对照：它走 arXiv 通道，不需要也不应该带线索。
        """
        with tempfile.TemporaryDirectory() as directory:
            artifacts = LocalArtifactStore(directory)
            request = ScoutRequest(query="auc", max_steps=1)
            ref = asyncio.run(artifacts.put_text(request.model_dump_json()))
            agent = PaperScoutAgent(
                artifacts,
                [
                    StubBackend(
                        "semantic_scholar",
                        [
                            paper("1", "Preprint", 0.0),
                            journal_paper("10.29220/csam", "L1-penalized", 0.0, OA_PDF),
                        ],
                    )
                ],
                None,
                StubScorer({"Preprint": 1.0, "L1-penalized": 0.9}),
                model="stub-model",
            )
            agent._provider = ScriptedProvider(
                [[("paper_scout_search", {"query": "q"})]]
            )
            outcome = asyncio.run(agent.run(agent_context(ref)))
            result = PaperScoutResult.model_validate_json(
                asyncio.run(artifacts.get_text(outcome.result_ref))
            )
            source_request = PaperSourceRequest.model_validate_json(
                asyncio.run(artifacts.get_text(result.paper_source_request_ref))
            )

        preprint, journal = source_request.papers
        self.assertEqual([], preprint.hints)
        self.assertEqual(1, len(journal.hints))
        self.assertEqual(OA_PDF, journal.hints[0].url)
        self.assertEqual("oa_pdf", journal.hints[0].kind)
        self.assertEqual("semantic_scholar", journal.hints[0].channel)
        self.assertTrue(journal.hints[0].is_open_access)

    def test_title_only_papers_are_dropped_before_delivery_by_default(self):
        result, corpus, stats, _ = self.run_agent(
            [[("paper_scout_search", {"query": "q"})]],
            [
                StubBackend(
                    "stub",
                    [
                        ScoutPaper(
                            paper_key="title:only", title="Title Only", source="search"
                        )
                    ],
                )
            ],
            scores={"Title Only": 0.9},
            max_steps=1,
        )
        self.assertEqual(result.paper_count, 0)
        self.assertEqual(len(corpus.pool), 1)
        self.assertEqual(stats.dropped_no_source, 1)
        self.assertIsNone(result.paper_source_request_ref)

    def test_max_papers_truncates_only_the_delivered_set(self):
        papers = [paper(str(index), f"P{index}", 0.0) for index in range(4)]
        result, corpus, _, _ = self.run_agent(
            [[("paper_scout_search", {"query": "q"})]],
            [StubBackend("stub", papers)],
            scores={f"P{index}": 0.9 for index in range(4)},
            max_steps=1,
            max_papers=2,
        )
        self.assertEqual(result.paper_count, 2)
        self.assertEqual(len(corpus.pool), 4)


if __name__ == "__main__":
    unittest.main()


class RetainThresholdTest(unittest.TestCase):
    """交付门槛是请求字段，默认 0 —— 见 RETAIN_THRESHOLD 的说明。"""

    def test_default_delivers_the_whole_pool(self):
        """默认不按分数截断：IdeaGeneration 里漏报不可恢复，误报只是多花算力。"""
        self.assertEqual(0.0, ScoutRequest(query="q").retain_threshold)

    def test_paper_threshold_is_still_available_for_reproduction(self):
        self.assertEqual(0.5, PASA_RETAIN_THRESHOLD)

    def test_threshold_is_rejected_outside_the_score_range(self):
        with self.assertRaises(ValidationError):
            ScoutRequest(query="q", retain_threshold=1.5)

    def test_each_band_admits_the_expected_grades(self):
        """0.45 是 2 分、0.2 是 1 分；打分离散，门槛只有三种有意义的取值。"""
        pool = PaperPool()
        for index, score in enumerate((1.0, 0.45, 0.2)):
            pool.add(
                ScoutPaper(
                    paper_key=f"arxiv:100{index}",
                    title=f"Paper {index}",
                    source="search",
                    relevance=score,
                )
            )

        self.assertEqual(1, len(pool.retained(PASA_RETAIN_THRESHOLD)))
        self.assertEqual(2, len(pool.retained(0.3)))
        self.assertEqual(3, len(pool.retained(RETAIN_THRESHOLD)))

    def test_max_papers_is_the_only_cap_at_the_default_threshold(self):
        pool = PaperPool()
        for index, score in enumerate((1.0, 0.45, 0.45, 0.2, 0.2)):
            pool.add(
                ScoutPaper(
                    paper_key=f"arxiv:200{index}",
                    title=f"Paper {index}",
                    source="search",
                    relevance=score,
                )
            )

        top = pool.retained(RETAIN_THRESHOLD, 3)

        self.assertEqual(3, len(top))
        self.assertEqual([1.0, 0.45, 0.45], [item.relevance for item in top])


OA_PDF = (
    "http://www.csam.or.kr/journal/download_pdf.php?doi=10.29220/CSAM.2024.31.2.203"
)


class FetchableDeliveryTest(unittest.TestCase):
    """交付集合只留取得到源的论文 —— 见 ``has_retrievable_source``。

    真机命中：一次调研的交付前 8 篇里有 2 篇是付费墙期刊，取源阶段直接 ``skipped``，
    白占了两个名额，而池里还有 28 篇同分的 arXiv 论文在排队。

    判据是"拿不拿得到字节"而不是"有没有 arXiv id"：同一批数据里那篇 GOLD 期刊论文
    上一轮真的取到了源，只按 arXiv id 判会把它一起误杀。
    """

    def build_pool(self) -> PaperPool:
        """复刻那次真机运行的分数结构：1.0 档 4 篇，其中 2 篇是付费墙期刊。"""
        pool = PaperPool()
        pool.add(paper("1", "Tabular GAN", 1.0))
        pool.add(paper("2", "Robust Low-Rank", 1.0))
        pool.add(journal_paper("10.1002/cpe.70882", "Student Performance", 1.0))
        pool.add(journal_paper("10.1016/j.cbc.2026.108984", "Driver Genes", 1.0))
        for index, name in enumerate(("Consistency", "PAC-Bayes", "Proximal"), start=3):
            pool.add(paper(str(index), name, 0.45))
        return pool

    def test_an_arxiv_id_makes_a_paper_fetchable(self):
        self.assertTrue(has_retrievable_source(paper("1", "T", 1.0)))

    def test_an_open_access_pdf_also_makes_a_paper_fetchable(self):
        """真机对照：这篇 GOLD 期刊论文上一轮确实取到了源，不该被当成取不到。"""
        self.assertTrue(
            has_retrievable_source(journal_paper("10.29220/csam", "T", 1.0, OA_PDF))
        )

    def test_a_paywalled_paper_is_not_fetchable(self):
        self.assertFalse(has_retrievable_source(journal_paper("10.1/x", "T", 1.0)))
        self.assertFalse(
            has_retrievable_source(
                ScoutPaper(
                    paper_key="s2:aaa", s2_paper_id="aaa", title="T", source="search"
                )
            )
        )

    def test_open_access_journals_keep_their_delivery_slot(self):
        pool = self.build_pool()
        pool.add(journal_paper("10.29220/csam", "L1-penalized AUC", 1.0, OA_PDF))

        kept = pool.retained(RETAIN_THRESHOLD, require_retrievable_source=True)

        self.assertIn("doi:10.29220/csam", [item.paper_key for item in kept])
        self.assertEqual(6, len(kept))

    def test_unfetchable_papers_never_reach_delivery(self):
        kept = self.build_pool().retained(
            RETAIN_THRESHOLD, require_retrievable_source=True
        )

        self.assertTrue(all(item.arxiv_id for item in kept))
        self.assertEqual(5, len(kept))

    def test_the_freed_slots_go_to_the_next_fetchable_papers(self):
        """过滤必须在截断之前：否则名额只是空着，后面能下载的论文仍然轮不上。"""
        pool = self.build_pool()

        without = [item.paper_key for item in pool.retained(RETAIN_THRESHOLD, 4)]
        with_filter = [
            item.paper_key
            for item in pool.retained(
                RETAIN_THRESHOLD, 4, require_retrievable_source=True
            )
        ]

        self.assertEqual(2, sum(1 for key in without if key.startswith("doi:")))
        self.assertEqual(4, len(with_filter))
        self.assertTrue(all(key.startswith("arxiv:") for key in with_filter))

    def test_the_filter_is_on_by_default(self):
        self.assertTrue(ScoutRequest(query="q").require_retrievable_source)

    def test_it_can_be_turned_off_for_benchmarks(self):
        """检索基准比对论文身份，不比对能不能下载，关掉才不会凭空压低 Recall。"""
        kept = self.build_pool().retained(
            RETAIN_THRESHOLD, require_retrievable_source=False
        )

        self.assertEqual(7, len(kept))
        self.assertEqual(2, sum(1 for item in kept if not item.arxiv_id))


class OpenAccessFieldTest(unittest.TestCase):
    """``openAccessPdf`` 的解析 —— 三种形状都来自真实响应。"""

    def test_a_gold_journal_yields_a_usable_url(self):
        url, flag = open_access_pdf(
            {"isOpenAccess": True, "openAccessPdf": {"url": OA_PDF, "status": "GOLD"}}
        )
        self.assertEqual(OA_PDF, url)
        self.assertTrue(flag)

    def test_a_closed_paper_yields_an_empty_url_not_a_missing_key(self):
        """付费墙论文返回的是空串而不是缺字段，判据只能是 url 非空。"""
        url, flag = open_access_pdf(
            {
                "isOpenAccess": False,
                "openAccessPdf": {"url": "", "status": "CLOSED", "license": None},
            }
        )
        self.assertEqual("", url)
        self.assertFalse(flag)

    def test_missing_and_null_payloads_degrade_to_unknown(self):
        self.assertEqual(("", None), open_access_pdf({}))
        self.assertEqual(("", None), open_access_pdf({"openAccessPdf": None}))

    def test_the_fields_are_requested_from_the_backend(self):
        """字段与标题摘要同批返回，漏掉它就等于把这条信号丢在上游。"""
        self.assertIn("openAccessPdf", SEMANTIC_SCHOLAR_FIELDS)
        self.assertIn("isOpenAccess", SEMANTIC_SCHOLAR_FIELDS)
