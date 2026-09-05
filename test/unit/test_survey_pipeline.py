"""全链路驱动测试 — 用真 schema 对象过真 ArtifactStore，只替换昂贵的 I/O。

四段的替身都返回真正的 pydantic 对象（``PaperScoutResult`` / ``PaperSourceResult``
/ ``PaperContent``），因此交接契约本身是被真实检验的；被替换掉的只有网络下载、
TeX 解析和模型调用。
"""

import asyncio
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

import httpx
from openai import RateLimitError

from athena.core.agent.models import AgentOutcome
from athena.core.artifact_store import LocalArtifactStore
from athena.research.literature.paper_markdown.models import (
    PaperChunk,
    PaperContent,
    PaperConversionRequest,
    PaperProvenance,
)
from athena.research.literature.paper_markdown.processor import (
    VisualInterpretationRequiredError,
)
from athena.research.literature.paper_rag.index import split_sentences
from athena.research.literature.paper_rag.models import CorpusBuildOptions
from athena.research.literature.paper_scout.schemas import (
    PaperScoutResult,
    ScoutCorpus,
    ScoutPaper,
    ScoutRequest,
    ScoutStats,
)
from athena.research.literature.paper_source.http import HostRateLimiter
from athena.research.literature.paper_source.schemas import (
    PaperIdentity,
    PaperSourceRecord,
    PaperSourceRequest,
    PaperSourceResult,
    PaperSourceStats,
)
from athena.research.literature.survey import pipeline as pipeline_module
from athena.research.literature.survey.library import PaperLibrary
from athena.research.literature.survey.pipeline import (
    SHRED_MIN_SENTENCES,
    PaperOutcome,
    ScoutStage,
    SurveyPipeline,
    SurveyRequest,
    SurveyRuntime,
    judge_shredded,
)
from athena.research.literature.survey.wiring import SurveyStack

PAPERS = [
    ("arxiv:2501.00001", "Scaling Laws for Tabular Models", 1.0),
    ("arxiv:2501.00002", "Regularization Transfer Across Domains", 1.0),
]


class FakeInterpreter:
    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0

    async def interpret(self, request):
        """记一次调用并让出事件循环，好让并发转换真的交错起来。"""
        self.calls += 1
        await asyncio.sleep(0)
        return request


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
    shredded: bool = False,
    shred_repeat: int = 40,
) -> PaperContent:
    """构造一篇最小但字段合法的 PaperContent。

    ``empty`` 复现 PDF-wrapper 投稿：markdown 只有一行，其余字段一切正常。
    ``shredded`` 复现另一头——``arxiv:1106.1813`` 那种入口推断失败的 TeX 包：句子数量
    极多而每句只是断行碎片。两者都是"转换报成功但提取没成功"。
    """
    body = f"# {paper_id}" if empty else f"# {paper_id}\n\n" + "body text. " * 400
    blank = await store.put_text(body)
    units = [
        PaperChunk(
            chunk_id=f"c{position}",
            kind="paragraph",
            content_ref=await store.put_text(
                "Chawla. Bowyer. Hall. JAIR. 2002. pp 321. " * shred_repeat
                if shredded
                else f"body {position} of {paper_id}"
            ),
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
    rerank_calls = 0
    rerank_failures = 0
    seen_request: ScoutRequest | None = None
    seen_reranker: object = None
    status = "complete"
    warnings: ClassVar[list[str]] = []

    def __init__(self, runtime):
        self.artifacts = runtime.artifacts
        type(self).seen_reranker = runtime.services.reranker

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
            ScoutStats(
                pool_size=len(retained),
                retained_papers=len(retained),
                dropped_no_source=type(self).dropped_no_source,
                rerank_calls=type(self).rerank_calls,
                rerank_failures=type(self).rerank_failures,
            ).model_dump_json()
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
            status=type(self).status,
            corpus_ref=corpus_ref,
            stats_ref=stats_ref,
            paper_source_request_ref=source_ref,
            paper_count=len(retained),
            warnings=list(type(self).warnings),
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

    statuses: ClassVar[list[str]] = ["fetched", "fetched"]
    conversion_refs: ClassVar[list[str]] = []
    enriched: ClassVar[dict[int, str]] = {}

    def __init__(self, artifacts) -> None:
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

    outcomes: ClassVar[dict[str, object]] = {}
    empty: ClassVar[set] = set()
    shredded: ClassVar[set] = set()
    delay: float = 0.0
    visuals_per_paper: ClassVar[dict[str, int]] = {}

    def __init__(self, artifacts, interpreter, refiner, **kwargs) -> None:
        self.artifacts = artifacts
        self.interpreter = interpreter

    async def process(self, request) -> PaperContent:
        outcome = type(self).outcomes[request.paper_id]
        if isinstance(outcome, Exception):
            raise outcome
        if type(self).delay:
            await asyncio.sleep(type(self).delay)
        for _ in range(type(self).visuals_per_paper.get(request.paper_id, 0)):
            await self.interpreter.interpret(request)
        return await make_content(
            self.artifacts,
            request.paper_id,
            quality=outcome,
            chunks=40 if request.paper_id in type(self).shredded else 2,
            empty=request.paper_id in type(self).empty,
            shredded=request.paper_id in type(self).shredded,
        )


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        source_patch = mock.patch.object(
            SurveyStack,
            "source_fetcher",
            lambda stack: FakeFetcher(stack.artifacts),
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="pipeline_"))
        self.interpreter = FakeInterpreter()
        self.embedder = FakeEmbedder()
        self.stack = SurveyStack(
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
        FakeScoutAgent.status = "complete"
        FakeScoutAgent.warnings = []
        FakeFetcher.statuses = ["fetched", "fetched"]
        FakeFetcher.enriched = {}
        FakeProcessor.outcomes = {key: "pass" for key, _t, _s in PAPERS}
        FakeProcessor.empty = set()
        FakeProcessor.shredded = set()
        FakeProcessor.visuals_per_paper = {}
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
            PaperConversionRequest(
                pdf_ref="sha256:" + "2" * 64,
                paper_id=paper_id,
                visual_policy="best_effort",
            ).model_dump_json()
        )

    async def fake_index(
        self, store, papers, options: CorpusBuildOptions | None = None
    ) -> str:
        self.indexed.append(list(papers))
        if options is not None and options.embedder is not None:
            await options.embedder.embed(["probe"])
        return "sha256:" + "9" * 64

    async def run_pipeline(self, *, emit=None, **overrides):
        request = SurveyRequest(query="tabular auc", **overrides)
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperProcessor=FakeProcessor,
            GradedRelevanceScorer=mock.MagicMock(),
            build_corpus_index=self.fake_index,
        ):
            return await SurveyPipeline(SurveyRuntime(self.stack, request, emit)).run()

    async def test_each_stage_reports_progress_to_a_subscribed_emit(self) -> None:
        events: list[tuple[str, dict]] = []

        async def record(kind: str, _ref: str, data: dict | None = None) -> None:
            events.append((kind, data or {}))

        report = await self.run_pipeline(emit=record)

        kinds = [kind for kind, _ in events]
        self.assertEqual(
            kinds,
            [
                "survey/scouted",
                "survey/fetched",
                "survey/converted",
                "survey/indexed",
            ],
        )
        payloads = dict(events)
        self.assertEqual(payloads["survey/scouted"]["retained"], len(PAPERS))
        self.assertEqual(payloads["survey/scouted"]["pool"], len(PAPERS))
        self.assertEqual(payloads["survey/fetched"]["fetched"], len(PAPERS))
        self.assertEqual(payloads["survey/converted"]["converted"], report.converted())
        self.assertEqual(payloads["survey/indexed"]["indexed"], len(PAPERS))

    async def test_progress_is_optional_and_absent_by_default(self) -> None:
        report = await self.run_pipeline()

        self.assertEqual(report.status, "complete")

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
            PAPERS[0][0]: VisualInterpretationRequiredError("no vlm"),
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
        self.assertTrue(
            all(item.fetch_status == "not_attempted" for item in report.papers)
        )

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

    async def test_vision_calls_are_counted_per_paper_not_off_a_shared_dial(
        self,
    ) -> None:
        """并发转换下，每篇的视觉调用数必须是它自己的，不能是共享计数器的增量。

        原实现记的是 ``interpreter.calls - calls_before``，而转换默认 4 篇并发，
        增量里混着同时在跑的其他论文。真机三轮：逐篇求和 3996 / 3513 / 1244，报告
        总数 340 / 361 / 135——**大了 9–12 倍**。

        这个用例特意让两篇论文交错执行（``FakeInterpreter.interpret`` 每次 ``sleep(0)``
        让出事件循环），旧实现在这种情况下必然多记。
        """
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "pass"}
        FakeProcessor.visuals_per_paper = {PAPERS[0][0]: 3, PAPERS[1][0]: 7}

        report = await self.run_pipeline()

        by_key = {item.paper_key: item for item in report.papers}
        self.assertEqual(3, by_key[PAPERS[0][0]].vision_calls)
        self.assertEqual(7, by_key[PAPERS[1][0]].vision_calls)
        self.assertEqual(
            report.vision_calls, sum(item.vision_calls for item in report.papers)
        )

    async def test_a_paper_without_visuals_is_charged_nothing(self) -> None:
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "pass"}
        FakeProcessor.visuals_per_paper = {PAPERS[1][0]: 5}

        report = await self.run_pipeline()

        by_key = {item.paper_key: item for item in report.papers}
        self.assertEqual(0, by_key[PAPERS[0][0]].vision_calls)
        self.assertEqual(5, by_key[PAPERS[1][0]].vision_calls)

    async def test_shredded_extraction_is_refused_and_reported(self) -> None:
        """转换报成功但产出的是碎片而不是句子 → 挡在语料外，并留下可复核的理由。

        真机命中：``arxiv:1106.1813``（SMOTE）的 TeX 包缺 ``\begin{document}``，入口
        推断失败，转换器把整包连成 774904 字符、35151 句、平均每句 30 字符。它一篇占
        掉整个语料 66%、整次调研墙钟的 47%（2147 秒），而当时的门禁放行了它——
        ``rag_chunk_oversized`` 属于"记录但不拦"，那套判据问内容对不对，不问代价。
        """
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "pass"}
        FakeProcessor.shredded = {PAPERS[1][0]}

        report = await self.run_pipeline()

        shredded = next(i for i in report.papers if i.paper_key == PAPERS[1][0])
        healthy = next(i for i in report.papers if i.paper_key == PAPERS[0][0])
        self.assertTrue(shredded.shredded)
        self.assertEqual("converted", shredded.conversion_status)
        self.assertFalse(shredded.indexed)
        self.assertFalse(healthy.shredded)
        self.assertTrue(healthy.indexed)
        self.assertEqual([1], [len(batch) for batch in self.indexed])

    async def test_a_refusal_carries_the_numbers_behind_it(self) -> None:
        """这道闸门会丢掉打分器要的论文，所以每次拒绝都必须能被复核，不能只是个计数。"""
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "pass"}
        FakeProcessor.shredded = {PAPERS[1][0]}

        report = await self.run_pipeline()

        self.assertEqual(1, len(report.shredded_papers))
        line = report.shredded_papers[0]
        self.assertIn(PAPERS[1][0], line)
        self.assertIn("句", line)
        self.assertIn("字符", line)

    async def test_normal_papers_are_never_touched_by_the_shred_gate(self) -> None:
        """正常论文一篇都不能被误伤——真机 44 篇回放里只有 1 篇触发。"""
        FakeProcessor.outcomes = {PAPERS[0][0]: "pass", PAPERS[1][0]: "degraded"}

        report = await self.run_pipeline()

        self.assertEqual([], report.shredded_papers)
        self.assertTrue(all(not item.shredded for item in report.papers))

    async def test_short_papers_are_exempt_from_the_shred_judgement(self) -> None:
        """句数太少时均值不稳，而它们再碎也贵不到哪里去；闸门是为代价加的。"""
        content = await make_content(
            self.store, "arxiv:0001.0001", chunks=1, shredded=True, shred_repeat=10
        )
        outcome = PaperOutcome(
            paper_key="arxiv:0001.0001",
            fetch_status="fetched",
            conversion_status="converted",
        )
        runtime = SurveyRuntime(self.stack, SurveyRequest(query="q"))

        units = await content.load_retrieval_units(self.store)
        total = sum(len(split_sentences(unit.text)) for unit in units)
        self.assertLess(total, SHRED_MIN_SENTENCES)
        self.assertFalse(
            await judge_shredded(content, outcome, self.store, runtime.report)
        )
        self.assertEqual([], runtime.report.shredded_papers)
        self.assertFalse(outcome.shredded)

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
            PaperProcessor=FakeProcessor,
            GradedRelevanceScorer=mock.MagicMock(),
            build_corpus_index=failing_index,
        ):
            report = await SurveyPipeline(SurveyRuntime(self.stack, request)).run()

        self.assertEqual(2, report.converted())
        self.assertIsNone(report.corpus_ref)
        self.assertEqual("partial", report.status)
        self.assertFalse(any(item.indexed for item in report.papers))
        self.assertIn("index_failed: RateLimitError", report.warnings)
        self.assertTrue(all(item.paper_content_ref for item in report.papers))

    async def test_the_delivery_budget_and_search_depth_have_deliberate_defaults(
        self,
    ) -> None:
        """三个默认值互相牵制，改任何一个都要连着看另外两个。

        ``search_top_k`` 决定池子里有什么（实测深度 10 只浮现 1/10 篇 gold、50 浮现
        6/10）；``max_seconds`` 决定跑得完几步——它一直是真正绑定的那条约束，只提深度
        不放宽墙钟就是拿广度换深度；``max_papers`` 只决定截断留几篇，对检索没有影响。
        """
        request = SurveyRequest(query="q")

        self.assertEqual(50, request.search_top_k)
        self.assertEqual(1800.0, request.max_seconds)
        self.assertEqual(20, request.max_papers)

    async def test_the_scorer_uses_its_own_model_when_one_is_configured(self) -> None:
        """打分是调用最多的一环，必须能独立换成轻量模型。"""
        self.stack.scorer_model = "flash"
        scorer = mock.MagicMock()
        request = SurveyRequest(query="tabular auc")
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperProcessor=FakeProcessor,
            GradedRelevanceScorer=scorer,
            build_corpus_index=self.fake_index,
        ):
            await SurveyPipeline(SurveyRuntime(self.stack, request)).run()

        self.assertEqual("flash", scorer.call_args.args[1])

    async def test_the_reranker_actually_reaches_the_scout_agent(self) -> None:
        """装配上有、真正跑的时候没有——这条链路上已经栽过三次的那种失败。

        边界重排器超时、重排目标取错、打分器配置没进缓存键，三次都是"看上去做完了、
        实际什么也没做"。同分次序信号同样只在 ``PaperScoutAgent`` 里生效，装在 stack 上
        不等于传了下去。
        """
        self.stack.reranker = mock.MagicMock(model="gte-rerank-v2")
        FakeScoutAgent.seen_reranker = None
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperProcessor=FakeProcessor,
            build_corpus_index=self.fake_index,
        ):
            await SurveyPipeline(
                SurveyRuntime(self.stack, SurveyRequest(query="tabular auc"))
            ).run()

        self.assertIs(self.stack.reranker, FakeScoutAgent.seen_reranker)

    async def test_no_reranker_passes_none_rather_than_failing(self) -> None:
        """没配 ATHENA_RERANK_MODEL 时链路照常跑完，排序退回散列。"""
        self.stack.reranker = None
        FakeScoutAgent.seen_reranker = mock.MagicMock()
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperProcessor=FakeProcessor,
            build_corpus_index=self.fake_index,
        ):
            report = await SurveyPipeline(
                SurveyRuntime(self.stack, SurveyRequest(query="tabular auc"))
            ).run()

        self.assertIsNone(FakeScoutAgent.seen_reranker)
        self.assertEqual("complete", report.status)

    async def test_affinity_cost_reaches_the_report(self) -> None:
        """报告上看不见的东西等于没跑过。

        六点十那次核对之所以能下结论，全靠分数分布这个间接证据。同分排序不改变分数
        分布，所以它必须自己在报告上留下计数——否则下一次"它到底跑没跑"又只能靠猜。
        """
        FakeScoutAgent.rerank_calls = 12
        FakeScoutAgent.rerank_failures = 1
        self.addCleanup(setattr, FakeScoutAgent, "rerank_calls", 0)
        self.addCleanup(setattr, FakeScoutAgent, "rerank_failures", 0)
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperProcessor=FakeProcessor,
            build_corpus_index=self.fake_index,
        ):
            report = await SurveyPipeline(
                SurveyRuntime(self.stack, SurveyRequest(query="tabular auc"))
            ).run()

        self.assertEqual(12, report.scout.rerank_calls)
        self.assertEqual(1, report.scout.rerank_failures)

    def test_the_rerank_model_is_part_of_the_scout_cache_key(self) -> None:
        """换掉拆平局的信号，将近一半的交付集合会变——缓存必须跟着失效。

        真机一轮 352 篇里 197 篇同分。``DEFAULT_PASSES`` 那次正是漏了这一步：改了配置，
        库里的旧结果继续命中，报告上看不出任何异常。
        """
        request = SurveyRequest(query="tabular auc")
        self.stack.reranker = None
        without = ScoutStage(SurveyRuntime(self.stack, request)).cache_key("{}")
        self.stack.reranker = mock.MagicMock(model="gte-rerank-v2")
        with_rerank = ScoutStage(SurveyRuntime(self.stack, request)).cache_key("{}")
        self.stack.reranker = mock.MagicMock(model="some-other-reranker")
        other = ScoutStage(SurveyRuntime(self.stack, request)).cache_key("{}")

        self.assertNotEqual(without, with_rerank)
        self.assertNotEqual(with_rerank, other)

    async def test_the_scorer_falls_back_to_the_policy_model(self) -> None:
        """不配打分模型时行为与分开之前完全一致。"""
        scorer = mock.MagicMock()
        request = SurveyRequest(query="tabular auc")
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=FakeScoutAgent,
            PaperProcessor=FakeProcessor,
            GradedRelevanceScorer=scorer,
            build_corpus_index=self.fake_index,
        ):
            await SurveyPipeline(SurveyRuntime(self.stack, request)).run()

        self.assertEqual(self.stack.model, scorer.call_args.args[1])

    async def test_source_gets_extra_candidates_and_a_success_target(self) -> None:
        """取源按"要几篇成功的"下单，而不是按"试几篇"。

        成功率按通道差一倍（实测 arXiv 91%、期刊 47%），而交付集合的通道构成每轮都不同，
        任何固定的超额系数都会随构成失准——真机上就失准过一次，13 篇里 5 篇取不到。
        """
        report = await self.run_pipeline(max_papers=10)
        policy = FakeScoutAgent.seen_request.paper_source_policy

        self.assertEqual(30, FakeScoutAgent.seen_request.max_papers)
        self.assertEqual(30, policy.max_papers)
        self.assertEqual(10, policy.stop_after_fetched)
        self.assertEqual(report.scout.retained_papers, len(FakeScoutAgent.papers))

    async def test_the_candidate_multiple_is_configurable(self) -> None:
        await self.run_pipeline(max_papers=10, source_candidate_multiple=5)

        self.assertEqual(50, FakeScoutAgent.seen_request.max_papers)

    async def test_surplus_papers_are_fetched_but_never_converted(self) -> None:
        """垫底富余取到源就停在那里：不转换、不进语料、不算转换失败。

        它必须和 ``failed`` 分开——垫底篇数是策略决定的，混进失败率会让那个数字
        随取源策略浮动，而它衡量的本该是转换器的健壮性。
        """
        report = await self.run_pipeline(max_papers=1)

        self.assertEqual(1, report.surplus_dropped)
        self.assertEqual(1, report.converted())
        self.assertEqual(0.0, report.conversion_failure_rate())
        surplus = [
            item for item in report.papers if item.conversion_status == "surplus"
        ]
        self.assertEqual(1, len(surplus))
        self.assertEqual("fetched", surplus[0].fetch_status)
        self.assertFalse(surplus[0].indexed)
        self.assertEqual(1, len(self.indexed[0]))

    async def test_explicit_paper_ids_are_never_treated_as_surplus(self) -> None:
        """点名的论文就是要的全部：不做垫底，也不被 ``max_papers`` 截掉。"""
        report = await self.run_pipeline(
            arxiv_ids=["2501.00001", "2501.00002"], max_papers=1
        )

        self.assertEqual(0, report.surplus_dropped)
        self.assertEqual(2, report.converted())
        self.assertEqual("complete", report.status)

    async def test_a_rate_limited_search_backend_does_not_downgrade_the_run(
        self,
    ) -> None:
        """检索后端零星限流不该把整轮标成 partial。

        真机上出现过语料建好、论文全进索引、报告却写着 ``partial`` 的情况——原因只是
        Semantic Scholar 429。看报告的人会以为语料没建成。后端的问题归 ``warnings``
        和 ``scout_status``，总状态只回答"这一轮自己产出了什么"。
        """
        FakeScoutAgent.status = "partial"
        FakeScoutAgent.warnings = ["semantic_scholar"]

        report = await self.run_pipeline()

        self.assertEqual("complete", report.status)
        self.assertEqual("partial", report.scout_status)
        self.assertIn("semantic_scholar", report.warnings)
        self.assertIsNotNone(report.corpus_ref)

    async def test_a_run_that_converts_nothing_is_empty(self) -> None:
        """一篇都没转换成功时是 ``empty``，与后端是否报过错无关。"""
        FakeFetcher.statuses = ["failed", "failed"]

        report = await self.run_pipeline()

        self.assertEqual("empty", report.status)

    async def test_the_fetchable_filter_reaches_paper_scout(self) -> None:
        await self.run_pipeline()
        self.assertTrue(FakeScoutAgent.seen_request.require_retrievable_source)

        await self.run_pipeline(require_retrievable_source=False)
        self.assertFalse(FakeScoutAgent.seen_request.require_retrievable_source)

    async def test_dropped_unfetchable_papers_are_reported(self) -> None:
        """交付变少要能分辨原因：池子差，还是这批命中全在付费墙后。"""
        FakeScoutAgent.dropped_no_source = 7

        report = await self.run_pipeline()

        self.assertEqual(7, report.scout.dropped_no_source)


class LibraryReuseTest(unittest.IsolatedAsyncioTestCase):
    """第二次调研只该为新论文付钱。

    此前每一次调研都从零开始：同一篇论文重新下载、重新转换（含视觉调用）、重新编码。
    这些用例把"重跑不再付钱"钉住，因为省下来的正是链路里最贵的两段。
    """

    async def asyncSetUp(self) -> None:
        source_patch = mock.patch.object(
            SurveyStack,
            "source_fetcher",
            lambda stack: FakeFetcher(stack.artifacts),
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.store = LocalArtifactStore(tempfile.mkdtemp(prefix="reuse_store_"))
        self.library = PaperLibrary(tempfile.mkdtemp(prefix="reuse_lib_"))
        self.interpreter = FakeInterpreter()
        self.embedder = FakeEmbedder()
        self.stack = SurveyStack(
            artifacts=self.store,
            client=object(),
            model="m",
            http=HostRateLimiter(),
            embedder=self.embedder,
            visual_interpreter=self.interpreter,
            library=self.library,
        )
        FakeScoutAgent.papers = PAPERS
        FakeScoutAgent.dropped_no_source = 0
        FakeScoutAgent.seen_request = None
        FakeScoutAgent.status = "complete"
        FakeScoutAgent.warnings = []
        FakeScoutAgent.runs = 0
        FakeFetcher.statuses = ["fetched", "fetched"]
        FakeFetcher.enriched = {}
        FakeProcessor.outcomes = {key: "pass" for key, _t, _s in PAPERS}
        FakeProcessor.empty = set()
        FakeProcessor.shredded = set()
        FakeProcessor.visuals_per_paper = {}
        FakeProcessor.delay = 0.0
        FakeProcessor.runs = 0
        FakeScoutAgent.source_request = PaperSourceRequest(
            papers=[
                {"identity": {"arxiv_id": key.split(":")[1]}} for key, _t, _s in PAPERS
            ]
        )
        FakeFetcher.conversion_refs = [
            await self._conversion_ref(key) for key, _t, _s in PAPERS
        ]

    async def _conversion_ref(self, paper_id: str) -> str:
        return await self.store.put_text(
            PaperConversionRequest(
                pdf_ref="sha256:" + "2" * 64,
                paper_id=paper_id,
                visual_policy="best_effort",
            ).model_dump_json()
        )

    async def run_pipeline(self, **overrides):
        request = SurveyRequest(query="tabular auc", **overrides)
        with mock.patch.multiple(
            pipeline_module,
            PaperScoutAgent=CountingScoutAgent,
            PaperProcessor=CountingProcessor,
            GradedRelevanceScorer=mock.MagicMock(),
        ):
            return await SurveyPipeline(SurveyRuntime(self.stack, request)).run()

    async def test_a_second_identical_run_pays_no_conversion_and_no_scout(self) -> None:
        """转换是唯一按篇调多模态模型的一段；检索占 71% 的墙钟。两者都不该重付。"""
        first = await self.run_pipeline()

        self.assertEqual(2, first.converted())
        self.assertFalse(first.scout_cached)
        conversions_after_first = CountingProcessor.runs
        scouts_after_first = CountingScoutAgent.runs

        second = await self.run_pipeline()

        self.assertEqual(2, second.converted())
        self.assertTrue(second.scout_cached)
        self.assertEqual(conversions_after_first, CountingProcessor.runs)
        self.assertEqual(scouts_after_first, CountingScoutAgent.runs)
        self.assertTrue(all(item.conversion_cached for item in second.papers))

    async def test_the_second_run_still_produces_the_same_corpus(self) -> None:
        """省钱不能以产物退化为代价：缓存命中的语料必须和现算的一样。"""
        first = await self.run_pipeline()
        second = await self.run_pipeline()

        self.assertEqual(first.corpus_ref, second.corpus_ref)
        self.assertEqual(
            [item.paper_key for item in first.papers],
            [item.paper_key for item in second.papers],
        )

    async def test_fresh_scout_forces_the_search_to_run_again(self) -> None:
        """想要新的一批论文时有明确的开关，而不是靠"每次都重跑"碰运气。"""
        await self.run_pipeline()
        before = CountingScoutAgent.runs

        report = await self.run_pipeline(fresh_scout=True)

        self.assertFalse(report.scout_cached)
        self.assertEqual(before + 1, CountingScoutAgent.runs)

    async def test_changing_the_visual_policy_invalidates_the_conversion_cache(
        self,
    ) -> None:
        """``required`` 与 ``best_effort`` 产出不同的 PaperContent；共用缓存会让开关失灵。"""
        await self.run_pipeline()
        before = CountingProcessor.runs

        await self.run_pipeline(visual_policy="required")

        self.assertEqual(before + len(PAPERS), CountingProcessor.runs)

    async def test_without_a_library_nothing_is_cached(self) -> None:
        """没有库时行为与此前完全一致——缓存是可选增益，不是新的必需依赖。"""
        self.stack.library = None
        await self.run_pipeline()
        before = CountingProcessor.runs

        report = await self.run_pipeline()

        self.assertFalse(report.scout_cached)
        self.assertEqual(before + len(PAPERS), CountingProcessor.runs)


class CountingScoutAgent(FakeScoutAgent):
    """记下真正跑过几次检索的 FakeScoutAgent。"""

    runs = 0

    async def run(self, ctx):
        CountingScoutAgent.runs += 1
        return await super().run(ctx)


class CountingProcessor(FakeProcessor):
    """记下真正转换过几篇的 FakeProcessor。"""

    runs = 0

    async def process(self, request):
        CountingProcessor.runs += 1
        return await super().process(request)
