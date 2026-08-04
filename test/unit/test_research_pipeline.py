"""全链路驱动测试 — 用真 schema 对象过真 ArtifactStore，只替换昂贵的 I/O。

四段的替身都返回真正的 pydantic 对象（``PaperScoutResult`` / ``PaperSourceResult``
/ ``PaperContent``），因此交接契约本身是被真实检验的；被替换掉的只有网络下载、
TeX 解析和模型调用。
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import httpx
from openai import RateLimitError

from athena.core.agent.agent import AgentOutcome
from athena.research import pipeline as pipeline_module
from athena.research.paper_markdown.schemas import (
    PaperChunk,
    PaperContent,
    PaperProvenance,
)
from athena.research.paper_scout.schemas import (
    PaperScoutResult,
    ScoutCorpus,
    ScoutPaper,
    ScoutRequest,
    ScoutStats,
)
from athena.research.paper_source.http import HostRateLimiter
from athena.research.paper_source.schemas import (
    PaperIdentity,
    PaperSourceRecord,
    PaperSourceRequest,
    PaperSourceResult,
    PaperSourceStats,
)
from athena.research.pipeline import SurveyPipeline, SurveyRequest
from athena.research.wiring import ResearchStack
from athena.storage.artifact_store import LocalArtifactStore

PAPERS = [
    ("arxiv:2501.00001", "Scaling Laws for Tabular Models", 1.0),
    ("arxiv:2501.00002", "Regularization Transfer Across Domains", 1.0),
]


class FakeInterpreter:
    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0


class FakeEmbedder:
    model = "fake-embedder"

    def __init__(self) -> None:
        self.calls = 0
        self.embedded = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.embedded += len(texts)
        return [[1.0, 0.0] for _ in texts]


async def make_content(
    store: LocalArtifactStore,
    paper_id: str,
    *,
    quality: str = "pass",
    chunks: int = 2,
    empty: bool = False,
) -> PaperContent:
    """构造一篇最小但字段合法的 PaperContent。

    ``empty`` 复现 PDF-wrapper 投稿：markdown 只有一行，其余字段一切正常。
    """
    body = f"# {paper_id}" if empty else f"# {paper_id}\n\n" + "body text. " * 400
    blank = await store.put_text(body)
    units = [
        PaperChunk(
            chunk_id=f"c{position}",
            kind="paragraph",
            content_ref=await store.put_text(f"body {position} of {paper_id}"),
            heading_path=["Method"],
            char_start=0,
            char_end=10,
            token_estimate=3,
        )
        for position in range(chunks)
    ]
    return PaperContent(
        paper_id=paper_id,
        title=paper_id,
        provenance=PaperProvenance(
            source_kind="tex",
            source_ref=blank,
            source_fingerprint="0" * 64,
            converter="test-1.0",
        ),
        markdown_ref=blank,
        diagnostics_ref=await store.put_text("[]"),
        quality_status=quality,
        quality_codes=[] if quality == "pass" else ["chunk_heading_missing"],
        chunks=units,
    )


class FakeScoutAgent:
    """写入真实的 ScoutCorpus / PaperScoutResult，并生成取源请求。"""

    source_request: PaperSourceRequest | None = None
    papers = PAPERS
    dropped_no_source = 0
    seen_request: ScoutRequest | None = None

    def __init__(self, artifacts, backends, references, scorer, *, model, client):
        self.artifacts = artifacts

    async def run(self, ctx) -> AgentOutcome:
        type(self).seen_request = ScoutRequest.model_validate_json(
            await self.artifacts.get_text(ctx.turn.request_ref)
        )
        retained = [
            ScoutPaper(
                paper_key=key,
                arxiv_id=key.split(":")[1],
                title=title,
                source="search",
                relevance=score,
            )
            for key, title, score in type(self).papers
        ]
        stats_ref = await self.artifacts.put_text(
            ScoutStats(dropped_no_source=type(self).dropped_no_source).model_dump_json()
        )
        corpus = ScoutCorpus(
            query="q", retained=retained, pool=retained, actions=[], stats_ref=stats_ref
        )
        corpus_ref = await self.artifacts.put_text(corpus.model_dump_json())
        source_ref = None
        if type(self).source_request is not None:
            source_ref = await self.artifacts.put_text(
                type(self).source_request.model_dump_json()
            )
        result = PaperScoutResult(
            status="complete",
            corpus_ref=corpus_ref,
            stats_ref=stats_ref,
            paper_source_request_ref=source_ref,
            paper_count=len(retained),
        )
        return AgentOutcome(
            result_ref=await self.artifacts.put_text(result.model_dump_json()),
            next_context_ref="context://x",
        )


class FakeFetcher:
    """按 ``statuses`` 决定每篇论文是否拿到源文件。

    ``enriched`` 模拟取源阶段补出期刊 DOI 后 ``paper_key`` 改写的真实行为：
    键是输入批次下标，值是解析后的 DOI。
    """

    statuses: list[str] = ["fetched", "fetched"]
    conversion_refs: list[str] = []
    enriched: dict[int, str] = {}

    def __init__(self, artifacts, **kwargs) -> None:
        self.artifacts = artifacts

    async def fetch(self, request, cancel=None) -> PaperSourceResult:
        records = []
        for index, (key, _title, _score) in enumerate(PAPERS):
            status = type(self).statuses[index]
            arxiv_id = key.split(":")[1]
            doi = type(self).enriched.get(index, "")
            identity = PaperIdentity(arxiv_id=arxiv_id, doi=doi or None)
            records.append(
                PaperSourceRecord(
                    ref_index=index,
                    paper_key=identity.paper_key(),
                    identity=identity,
                    status=status,
                    tex_source_ref=(
                        "sha256:" + "1" * 64 if status == "fetched" else None
                    ),
                    conversion_request_ref=(
                        type(self).conversion_refs[index]
                        if status == "fetched"
                        else None
                    ),
                )
            )
        fetched = sum(1 for item in records if item.status == "fetched")
        return PaperSourceResult(
            records=records,
            stats=PaperSourceStats(
                requested=len(records),
                accepted=len(records),
                fetched=fetched,
                skipped=0,
                failed=len(records) - fetched,
                cache_hits=0,
                http_requests=4,
                tex_sources=fetched,
                pdf_sources=0,
            ),
        )


class FakeProcessor:
    """按 ``outcomes`` 决定每篇论文转换成功、失败还是降级。"""

    outcomes: dict[str, object] = {}
    empty: set = set()
    delay: float = 0.0

    def __init__(self, artifacts, interpreter, refiner, **kwargs) -> None:
        self.artifacts = artifacts

    async def process(self, request) -> PaperContent:
        outcome = type(self).outcomes[request.paper_id]
        if isinstance(outcome, Exception):
            raise outcome
        if type(self).delay:
            await asyncio.sleep(type(self).delay)
        return await make_content(
            self.artifacts,
            request.paper_id,
            quality=outcome,
            empty=request.paper_id in type(self).empty,
        )


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="pipeline_"))
        self.interpreter = FakeInterpreter()
        self.embedder = FakeEmbedder()
        self.stack = ResearchStack(
            artifacts=self.store,
            client=object(),
            model="m",
            http=HostRateLimiter(),
            embedder=self.embedder,
            visual_interpreter=self.interpreter,
        )
        FakeScoutAgent.papers = PAPERS
        FakeScoutAgent.dropped_no_source = 0
        FakeScoutAgent.seen_request = None
        FakeFetcher.statuses = ["fetched", "fetched"]
        FakeFetcher.enriched = {}
        FakeProcessor.outcomes = {key: "pass" for key, _t, _s in PAPERS}
        FakeProcessor.empty = set()
        FakeProcessor.delay = 0.0
        FakeScoutAgent.source_request = PaperSourceRequest(
            papers=[
                {"identity": {"arxiv_id": key.split(":")[1]}} for key, _t, _s in PAPERS
            ]
        )
        FakeFetcher.conversion_refs = [
            await self._conversion_ref(key) for key, _t, _s in PAPERS
        ]
        self.indexed: list[list[PaperContent]] = []

    async def _conversion_ref(self, paper_id: str) -> str:
        return await self.store.put_text(
            pipeline_module.PaperConversionRequest(
                pdf_ref="sha256:" + "2" * 64,
                paper_id=paper_id,
                visual_policy="best_effort",
            ).model_dump_json()
        )

    async def fake_index(self, store, papers, embedder=None, **kwargs) -> str:
        self.indexed.append(list(papers))
        if embedder is not None:
            await embedder.embed(["probe"])
        return "sha256:" + "9" * 64

    async def run_pipeline(self, **overrides):
        request = SurveyRequest(query="tabular auc", **overrides)
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperSourceFetcher=FakeFetcher,
            PaperProcessor=FakeProcessor,
            GradedRelevanceScorer=mock.MagicMock(),
            build_corpus_index=self.fake_index,
        ):
            return await SurveyPipeline(self.stack, request).run()

    async def test_happy_path_carries_every_paper_to_the_index(self) -> None:
        report = await self.run_pipeline()

        self.assertEqual("complete", report.status)
        self.assertEqual(2, report.converted())
        self.assertEqual(0.0, report.conversion_failure_rate())
        self.assertEqual("sha256:" + "9" * 64, report.corpus_ref)
        self.assertTrue(all(item.indexed for item in report.papers))
        self.assertEqual([2], [len(batch) for batch in self.indexed])

    async def test_report_records_per_paper_cost_and_quality(self) -> None:
        report = await self.run_pipeline()

        first = report.papers[0]
        self.assertEqual("fetched", first.fetch_status)
        self.assertEqual("tex", first.source_kind)
        self.assertEqual("converted", first.conversion_status)
        self.assertEqual("pass", first.quality_status)
        self.assertEqual(2, first.chunks)
        self.assertGreaterEqual(first.conversion_seconds, 0.0)

    async def test_fetch_failure_keeps_the_paper_in_the_report(self) -> None:
        FakeFetcher.statuses = ["fetched", "failed"]

        report = await self.run_pipeline()

        failed = next(item for item in report.papers if item.fetch_status == "failed")
        self.assertEqual("no_source", failed.conversion_status)
        self.assertFalse(failed.indexed)
        self.assertEqual(1, report.converted())

    async def test_conversion_failure_does_not_stop_the_batch(self) -> None:
        FakeProcessor.outcomes = {
            PAPERS[0][0]: "pass",
            PAPERS[1][0]: ValueError("tex parse blew up"),
        }

        report = await self.run_pipeline()

        broken = next(item for item in report.papers if item.error)
        self.assertEqual("failed", broken.conversion_status)
        self.assertIn("ValueError: tex parse blew up", broken.error)
        self.assertEqual(1, report.converted())
        self.assertEqual(0.5, report.conversion_failure_rate())
        self.assertEqual([1], [len(batch) for batch in self.indexed])

    async def test_required_visual_failure_is_reported_as_its_own_kind(self) -> None:
        FakeProcessor.outcomes = {
            PAPERS[0][0]: pipeline_module.VisualInterpretationRequiredError("no vlm"),
            PAPERS[1][0]: "pass",
        }

        report = await self.run_pipeline(visual_policy="required")

        broken = next(item for item in report.papers if item.error)
        self.assertIn("VisualInterpretationRequiredError", broken.error)

    async def test_degraded_papers_are_indexed_by_default(self) -> None:
        """degraded 是存在性判定：一条诊断否决整篇，与论文规模无关。

        实测 5 篇被挡的论文逐条核对后 4 篇是误判；而 paper_rag 是 chunk 级检索，
        坏掉的公式或表格 chunk 本来就捞不出来，局部缺陷不该否决整篇。
        """
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "degraded"}

        report = await self.run_pipeline()

        self.assertTrue(all(item.indexed for item in report.papers))
        self.assertEqual([2], [len(batch) for batch in self.indexed])

    async def test_strict_quality_restores_the_narrow_gate(self) -> None:
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "degraded"}

        report = await self.run_pipeline(strict_quality=True)

        degraded = next(
            item for item in report.papers if item.quality_status == "degraded"
        )
        self.assertEqual("converted", degraded.conversion_status)
        self.assertFalse(degraded.indexed)
        self.assertEqual([1], [len(batch) for batch in self.indexed])

    async def test_no_index_flag_skips_the_encoding_cost(self) -> None:
        report = await self.run_pipeline(build_index=False)

        self.assertIsNone(report.corpus_ref)
        self.assertEqual([], self.indexed)
        self.assertEqual(0, report.embed_calls)

    async def test_empty_scout_delivery_produces_a_report_not_an_error(self) -> None:
        FakeScoutAgent.source_request = None

        report = await self.run_pipeline()

        self.assertEqual(2, len(report.papers))
        self.assertEqual(0, report.converted())
        self.assertIsNone(report.corpus_ref)
        self.assertTrue(all(item.fetch_status == "skipped" for item in report.papers))

    async def test_papers_are_ordered_by_relevance(self) -> None:
        FakeScoutAgent.papers = [
            ("arxiv:2501.00001", "Low", 0.6),
            ("arxiv:2501.00002", "High", 0.95),
        ]

        report = await self.run_pipeline()

        self.assertEqual(
            ["arxiv:2501.00002", "arxiv:2501.00001"],
            [item.paper_key for item in report.papers],
        )

    async def test_enriched_identity_merges_back_into_one_paper(self) -> None:
        """取源补上期刊 DOI 会让 paper_key 变化，报告里必须仍然只有一篇。

        真机第一次运行就命中了这一条：scout 交出 ``arxiv:2303.02505``，取源解析出
        期刊 DOI 后按自己的优先级换成 ``doi:10.14569/...``，报告因此把同一篇论文
        记成了两条——一条"取源跳过"，一条"转换成功但相关性 0"。
        """
        doi = "10.14569/ijacsa.2023.0140286"
        enriched = f"doi:{doi}"
        original = PAPERS[0][0]
        FakeFetcher.statuses = ["fetched", "failed"]
        FakeFetcher.enriched = {0: doi}
        FakeProcessor.outcomes = {enriched: "pass"}
        FakeFetcher.conversion_refs = [await self._conversion_ref(enriched), None]

        report = await self.run_pipeline()

        self.assertEqual(2, len(report.papers))
        merged = next(item for item in report.papers if item.paper_key == original)
        self.assertEqual("fetched", merged.fetch_status)
        self.assertEqual("converted", merged.conversion_status)
        self.assertEqual(1.0, merged.relevance)
        self.assertTrue(merged.indexed)
        self.assertNotIn(enriched, [item.paper_key for item in report.papers])

    async def test_silent_content_loss_is_flagged_and_never_indexed(self) -> None:
        """转换报成功但正文近乎为空 → 必须被识别出来并挡在语料外。

        真机批量运行命中：``arxiv:1412.6980`` 的 TeX 包用 ``\\includepdf`` 套着外部
        PDF，转换产出 29 个字符、0 条诊断，质量却是 ``pass``。这种"成功但空"比报错
        危险——它不进失败率，还会让语料看起来已经覆盖这篇论文。
        """
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "pass"}
        FakeProcessor.empty = {PAPERS[1][0]}

        report = await self.run_pipeline(strict_quality=True)

        hollow = next(item for item in report.papers if item.paper_key == PAPERS[1][0])
        healthy = next(item for item in report.papers if item.paper_key == PAPERS[0][0])
        self.assertTrue(hollow.suspect_empty)
        self.assertEqual("converted", hollow.conversion_status)
        self.assertEqual("pass", hollow.quality_status)
        self.assertFalse(hollow.indexed)
        self.assertFalse(healthy.suspect_empty)
        self.assertTrue(healthy.indexed)
        self.assertEqual(1, report.suspect_empty_count())
        self.assertEqual([1], [len(batch) for batch in self.indexed])

    async def test_empty_check_holds_even_in_the_permissive_default(self) -> None:
        """suspect_empty 表达的是"正文根本没提取出来"，和局部缺陷不是一回事。"""
        FakeProcessor.empty = {PAPERS[1][0]}

        report = await self.run_pipeline()

        hollow = next(item for item in report.papers if item.paper_key == PAPERS[1][0])
        self.assertFalse(hollow.indexed)

    async def test_conversion_time_excludes_the_concurrency_wait(self) -> None:
        """单篇耗时必须从拿到信号量之后算起，否则排队时间会被计进成本。

        判据是两篇的**差值**而不是绝对上限。并发为 1 时第二篇要整整等第一篇一轮：计时
        写错的话它约等于第一篇的两倍，差值接近 delay；写对的话两篇都约等于 delay，差值
        只剩调度抖动。绝对阈值在负载高时会假失败——真机上就撞到过一次。
        """
        FakeProcessor.delay = 0.2

        report = await self.run_pipeline(conversion_concurrency=1)

        times = [item.conversion_seconds for item in report.papers]
        self.assertLess(max(times) - min(times), FakeProcessor.delay)
        self.assertGreaterEqual(min(times), FakeProcessor.delay)

    async def test_timings_and_call_counts_are_recorded(self) -> None:
        report = await self.run_pipeline()

        self.assertGreater(report.timings.total_seconds, 0.0)
        self.assertGreaterEqual(report.timings.scout_seconds, 0.0)
        self.assertEqual(1, report.embed_calls)
        self.assertEqual(0, report.vision_calls)

    async def test_a_failed_index_still_produces_a_report(self) -> None:
        """真机命中：50 篇跑到建索引时编码配额耗尽，异常带走了整份报告。

        建索引是最后一段，也是唯一会一次性打光配额的一段；而取源与转换才是花钱的部分，
        它们的产物都已落盘。这里必须降级成"没有语料"，不能降级成"没有报告"。
        """

        async def failing_index(*args, **kwargs):
            raise RateLimitError(
                "Allocated quota exceeded",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://example.invalid")
                ),
                body=None,
            )

        request = SurveyRequest(query="tabular auc")
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperSourceFetcher=FakeFetcher,
            PaperProcessor=FakeProcessor,
            GradedRelevanceScorer=mock.MagicMock(),
            build_corpus_index=failing_index,
        ):
            report = await SurveyPipeline(self.stack, request).run()

        self.assertEqual(2, report.converted())
        self.assertIsNone(report.corpus_ref)
        self.assertEqual("partial", report.status)
        self.assertFalse(any(item.indexed for item in report.papers))
        self.assertIn("index_failed: RateLimitError", report.warnings)
        self.assertTrue(all(item.paper_content_ref for item in report.papers))

    async def test_the_delivery_budget_defaults_to_fifty(self) -> None:
        """成本已经量过，默认按调研需要给量；要省钱就调小它，而不是抬门槛。"""
        self.assertEqual(50, SurveyRequest(query="q").max_papers)

        await self.run_pipeline()

        self.assertEqual(50, FakeScoutAgent.seen_request.max_papers)

    async def test_the_fetchable_filter_reaches_paper_scout(self) -> None:
        await self.run_pipeline()
        self.assertTrue(FakeScoutAgent.seen_request.require_retrievable_source)

        await self.run_pipeline(require_retrievable_source=False)
        self.assertFalse(FakeScoutAgent.seen_request.require_retrievable_source)

    async def test_dropped_unfetchable_papers_are_reported(self) -> None:
        """交付变少要能分辨原因：池子差，还是这批命中全在付费墙后。"""
        FakeScoutAgent.dropped_no_source = 7

        report = await self.run_pipeline()

        self.assertEqual(7, report.scout_dropped_no_source)
