"""Execution stages for the Academic Survey pipeline."""

import asyncio
import time
from typing import TYPE_CHECKING

from openai import OpenAIError

from athena.core.agent.models import AgentContext
from athena.core.contracts import ArtifactRef
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent
from athena.research.literature.paper_markdown.models import (
    PaperContent,
    PaperConversionRequest,
)
from athena.research.literature.paper_markdown.processor import (
    PaperProcessor,
    VisualInterpretationRequiredError,
)
from athena.research.literature.paper_rag.index import (
    BIBLIOGRAPHY_KIND,
    build_corpus_index,
    split_sentences,
)
from athena.research.literature.paper_scout.agent import PaperScoutAgent
from athena.research.literature.paper_scout.schemas import (
    PaperScoutResult,
    ScoutCorpus,
    ScoutRequest,
    ScoutStats,
)
from athena.research.literature.paper_scout.scorer import (
    DEFAULT_PASSES,
    GradedRelevanceScorer,
)
from athena.research.literature.paper_scout.selection import LlmBoundarySelector
from athena.research.literature.paper_source.fetcher import PaperSourceFetcher
from athena.research.literature.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRequest,
    PaperSourceResult,
)
from athena.research.literature.survey.library import (
    conversion_key,
    copy_refs,
    scout_key,
)
from athena.research.literature.survey.pipeline import (
    PaperOutcome,
    PipelineState,
    SurveyReport,
    SurveyRequest,
)

if TYPE_CHECKING:
    from athena.research.literature.survey.wiring import SurveyStack

from athena.research.literature.survey.pipeline import (
    INDEXABLE_QUALITY,
    MIN_PLAUSIBLE_MARKDOWN,
    NOT_ATTEMPTED,
    SHRED_MIN_SENTENCE_CHARS,
    SHRED_MIN_SENTENCES,
)


class _PerPaperVision:
    """把共享的视觉解读器包一层，按篇统计调用与失败。

    此前 ``PaperOutcome.vision_calls`` 记的是 ``interpreter.calls - calls_before``——
    一个**共享**计数器在这篇论文转换期间的增量。转换是并发的（默认 4 篇），所以这个
    增量里混着同时在跑的其他论文的调用。真机三轮验证：逐篇求和 3996 / 3513 / 1244，
    而报告总数是 340 / 361 / 135，**大了 9–12 倍**。

    这条 bug 尤其值得记一笔，因为它旁边就写着正确做法：``conversion_seconds`` 特意在
    信号量**之内**开始计时，注释解释的正是"排队等待会被算进单篇成本，让每篇耗时都趋同
    于批次总耗时，成本数字就失去意义"。同一个函数里，时间做对了，调用数做错了。

    ``inner`` 为 ``None`` 时（没配视觉模型）本代理不该被交给 ``PaperProcessor``——
    它必须收到真正的 ``None`` 才会走"退回仅证据文本"那条路。
    """

    def __init__(self, inner: object | None) -> None:
        self.inner = inner
        self.calls = 0
        self.failures = 0

    async def interpret(self, request):
        """转发一次解读，并把这一次记在**本篇**账上。"""
        self.calls += 1
        try:
            return await self.inner.interpret(request)
        except Exception:
            # 计数后原样上抛：由 visual_policy 决定整篇失败还是退回证据文本
            self.failures += 1
            raise


class ScoutStage:
    """PaperScout 阶段：检索、缓存、直接 arxiv 路径与结果登记。"""

    def __init__(
        self,
        stack: "SurveyStack",
        request: SurveyRequest,
        state: PipelineState,
        emit: EmitEvent,
    ) -> None:
        self.stack = stack
        self.request = request
        self.state = state
        self.emit = emit

    async def run(self) -> ArtifactRef | None:
        """跑 PaperScout，把交付集合转成取源请求引用；零交付时返回 ``None``。"""
        if self.request.arxiv_ids:
            return await self._direct_source_request()
        started = time.monotonic()
        backends, references = self.stack.build_backends()
        agent = PaperScoutAgent(
            self.stack.artifacts,
            backends,
            references,
            GradedRelevanceScorer(
                self.stack.client, self.stack.effective_scorer_model()
            ),
            model=self.stack.model,
            client=self.stack.client,
            selector=LlmBoundarySelector(self.stack.client, self.stack.model),
            reranker=self.stack.reranker,
        )
        candidates = self.request.max_papers * self.request.source_candidate_multiple
        scout_request = ScoutRequest(
            query=self.request.query,
            published_to=self.request.published_to,
            max_steps=self.request.max_steps,
            search_top_k=self.request.search_top_k,
            expand_top_k=self.request.expand_top_k,
            max_papers=candidates,
            max_seconds=self.request.max_seconds,
            retain_threshold=self.request.retain_threshold,
            require_retrievable_source=self.request.require_retrievable_source,
            paper_source_policy=PaperSourcePolicy(
                prefer=self.request.prefer,
                max_papers=candidates,
                stop_after_fetched=self.request.max_papers,
                visual_policy=self.request.visual_policy,
            ),
        )
        request_json = scout_request.model_dump_json()
        cached = await self._cached(request_json)
        if cached is not None:
            self.state.report.timings.scout_seconds = round(
                time.monotonic() - started, 3
            )
            self.state.report.scout_cached = True
            return await self._read_result(cached)
        request_ref = await self.stack.artifacts.put_text(request_json)
        outcome = await agent.run(self._agent_context(request_ref))
        self.state.report.timings.scout_seconds = round(time.monotonic() - started, 3)
        await self._cache(request_json, outcome.result_ref)
        return await self._read_result(outcome.result_ref)

    def cache_key(self, request_json: str) -> str:
        """本次检索在库里的键；请求 + 打分器指纹，见 ``library.scout_key``。"""
        return scout_key(request_json, self.fingerprint())

    def fingerprint(self) -> str:
        """打分器的身份：模型名 + 打分遍数 + 同分次序用的交叉编码器。"""
        return (
            f"{self.stack.effective_scorer_model()}"
            f"/{DEFAULT_PASSES}"
            f"/{self.stack.rerank_model()}"
        )

    async def _cached(self, request_json: str) -> ArtifactRef | None:
        """取回同一份请求上次跑出的检索结果，并把它引用的 blob 复制回本地存储。"""
        library = self.stack.library
        if library is None or self.request.fresh_scout:
            return None
        payload = await library.load_text(self.cache_key(request_json))
        if payload is None:
            return None
        try:
            result = PaperScoutResult.model_validate_json(payload)
        except ValueError:
            return None
        refs = [result.corpus_ref, result.stats_ref, result.paper_source_request_ref]
        wanted = [ref for ref in refs if ref]
        if await copy_refs(library.store, self.stack.artifacts, wanted) != len(wanted):
            return None
        self.state.report.warnings.append("scout_cache_hit")
        return await self.stack.artifacts.put_text(payload)

    async def _cache(self, request_json: str, result_ref: ArtifactRef) -> None:
        """把这次检索的结果连同它引用的 blob 一起存进库。"""
        library = self.stack.library
        if library is None:
            return
        payload = await self.stack.artifacts.get_text(result_ref)
        try:
            result = PaperScoutResult.model_validate_json(payload)
        except ValueError:
            return
        refs = [result.corpus_ref, result.stats_ref, result.paper_source_request_ref]
        await copy_refs(
            self.stack.artifacts, library.store, [ref for ref in refs if ref]
        )
        await library.save_text(self.cache_key(request_json), payload)

    async def _direct_source_request(self) -> ArtifactRef | None:
        """把显式给出的 arXiv id 直接做成取源请求，并登记为待处理论文。"""
        papers = []
        for raw in self.request.arxiv_ids:
            identity = PaperIdentity(arxiv_id=raw)
            key = identity.paper_key()
            papers.append(PaperRef(identity=identity))
            self.state.outcomes[key] = PaperOutcome(
                paper_key=key,
                title=raw,
                relevance=1.0,
                fetch_status=NOT_ATTEMPTED,
                conversion_status="no_source",
            )
            self.state.register_identifiers(key, identity)
        self.state.report.scout.retained_papers = len(papers)
        self.state.convert_cap = max(self.request.max_papers, len(papers))
        request = PaperSourceRequest(
            papers=papers,
            policy=PaperSourcePolicy(
                prefer=self.request.prefer,
                max_papers=self.state.convert_cap,
                visual_policy=self.request.visual_policy,
            ),
        )
        return await self.stack.artifacts.put_text(request.model_dump_json())

    async def _read_result(self, result_ref: str) -> ArtifactRef | None:
        """读回 scout 结果，登记每篇论文的初始状态。"""
        result = PaperScoutResult.model_validate_json(
            await self.stack.artifacts.get_text(result_ref)
        )
        corpus = ScoutCorpus.model_validate_json(
            await self.stack.artifacts.get_text(result.corpus_ref)
        )
        stats = ScoutStats.model_validate_json(
            await self.stack.artifacts.get_text(result.stats_ref)
        )
        self.state.report.scout_result_ref = result_ref
        self.state.report.scout = stats
        self.state.report.retain_threshold = self.request.retain_threshold
        histogram: dict[str, int] = {}
        for paper in corpus.pool:
            bucket = f"{paper.relevance:.2f}"
            histogram[bucket] = histogram.get(bucket, 0) + 1
        self.state.report.score_histogram = dict(
            sorted(histogram.items(), reverse=True)
        )
        self.state.report.warnings.extend(result.warnings)
        self.state.report.scout_status = result.status
        self.state.retained = list(corpus.retained)
        self.state.paper_edges = {
            source: list(targets) for source, targets in corpus.reference_edges.items()
        }
        self.state.report.reference_lookups = len(self.state.paper_edges)
        for paper in corpus.retained:
            self.state.outcomes[paper.paper_key] = PaperOutcome(
                paper_key=paper.paper_key,
                title=paper.title,
                relevance=paper.relevance,
                fetch_status=NOT_ATTEMPTED,
                conversion_status="no_source",
            )
            self.state.register_identifiers(paper.paper_key, paper)
        return result.paper_source_request_ref

    def _agent_context(self, request_ref: ArtifactRef) -> AgentContext:
        """构造 PaperScout 需要的最小 Turn 上下文。"""
        thread = AthenaThread(
            thread_id="survey",
            session_id="survey",
            status="running",
            context_ref=request_ref,
        )
        turn = AthenaTurn(
            turn_id="survey-turn",
            thread_id="survey",
            request_ref=request_ref,
            status="running",
        )
        return AgentContext(
            thread=thread,
            turn=turn,
            emit=self.emit,
            tools=ToolRegistry(),
            cancel=asyncio.Event(),
        )


class FetchStage:
    """paper_source 阶段：批量取源并登记逐篇结果。"""

    def __init__(
        self, stack: "SurveyStack", request: SurveyRequest, state: PipelineState
    ) -> None:
        self.stack = stack
        self.request = request
        self.state = state

    async def run(self, source_request_ref: ArtifactRef) -> PaperSourceResult:
        """批量取源，把逐篇结果登记进报告。"""
        started = time.monotonic()
        request = PaperSourceRequest.model_validate_json(
            await self.stack.artifacts.get_text(source_request_ref)
        )
        fetcher = PaperSourceFetcher(
            self.stack.artifacts,
            http=self.stack.http,
            contact_email=self.stack.contact_email or None,
            openalex_api_key=self.stack.openalex_api_key or None,
            cache=self.stack.locator_cache(),
        )
        result = await fetcher.fetch(request)
        self.state.report.timings.source_seconds = round(time.monotonic() - started, 3)
        self.state.report.source_result_ref = await self.stack.artifacts.put_text(
            result.model_dump_json()
        )
        for record in result.records:
            key = self.state.canonical_key(record)
            outcome = self.state.outcome_for(key)
            outcome.fetch_status = record.status
            outcome.source_locator = record.source_locator or ""
            outcome.source_kind = "tex" if record.tex_source_ref else ""
            if not outcome.source_kind and record.pdf_ref:
                outcome.source_kind = "pdf"
            self.state.register_identifiers(key, record.identity)
            self.state.conversion_keys[record.paper_key] = key
        self.state.report.fetched = result.stats.fetched
        self.state.report.fetch_failed = result.stats.failed + result.stats.skipped
        self.state.report.fetch_attempted = result.stats.attempted
        return result


class ConversionStage:
    """paper_markdown 阶段：并发转换、按篇计数、缓存与失败收敛。"""

    def __init__(
        self, stack: "SurveyStack", request: SurveyRequest, state: PipelineState
    ) -> None:
        self.stack = stack
        self.request = request
        self.state = state

    async def run(self, source_result: PaperSourceResult) -> list[PaperContent]:
        """转换取到源的论文，按 ``state.convert_cap`` 截断后并发执行。"""
        started = time.monotonic()
        fetched = [
            (
                self.state.conversion_keys.get(record.paper_key, record.paper_key),
                record.conversion_request_ref,
            )
            for record in source_result.records
            if record.conversion_request_ref
        ]
        jobs = fetched[: self.state.convert_cap]
        for key, _ in fetched[self.state.convert_cap :]:
            self.state.outcome_for(key).conversion_status = "surplus"
        self.state.report.surplus_dropped = len(fetched) - len(jobs)
        if not jobs:
            self.state.report.timings.markdown_seconds = round(
                time.monotonic() - started, 3
            )
            return []
        limit = asyncio.Semaphore(self.request.conversion_concurrency)
        results = await asyncio.gather(
            *(self._convert_one(limit, key, ref) for key, ref in jobs)
        )
        self.state.report.timings.markdown_seconds = round(
            time.monotonic() - started, 3
        )
        return [item for item in results if item is not None]

    async def _convert_one(
        self,
        limit: asyncio.Semaphore,
        paper_key: str,
        request_ref: ArtifactRef,
    ) -> PaperContent | None:
        """转换一篇论文；失败只记进报告，不影响同批其他论文。"""
        outcome = self.state.outcome_for(paper_key)
        counter = _PerPaperVision(self.stack.visual_interpreter)
        processor = PaperProcessor(
            self.stack.artifacts,
            counter if counter.inner is not None else None,
            None,
            ghostscript=self.stack.ghostscript or None,
        )
        cached = await self._cached_conversion(request_ref)
        if cached is not None:
            outcome.conversion_cached = True
            content = cached
        else:
            async with limit:
                started = time.monotonic()
                content = await self._process(processor, outcome, request_ref)
                outcome.conversion_seconds = round(time.monotonic() - started, 3)
            if content is not None:
                await self._cache_conversion(request_ref, content)
        outcome.vision_calls = counter.calls
        if content is None:
            return None
        outcome.conversion_status = "converted"
        outcome.quality_status = content.quality_status
        outcome.quality_codes = list(content.quality_codes)
        markdown = await self.stack.artifacts.get_text(content.markdown_ref)
        outcome.markdown_chars = len(markdown)
        outcome.suspect_empty = len(markdown) < MIN_PLAUSIBLE_MARKDOWN
        outcome.chunks = len(content.chunks)
        outcome.visuals = len(content.visuals)
        outcome.visuals_interpreted = sum(
            1 for item in content.visuals if item.interpretation_status == "interpreted"
        )
        return content

    async def _conversion_cache_key(self, request_ref: ArtifactRef) -> str | None:
        """本篇转换在库里的键；请求读不出来时返回 ``None``（走正常转换路径）。"""
        try:
            request = PaperConversionRequest.model_validate_json(
                await self.stack.artifacts.get_text(request_ref)
            )
        except (ValueError, OSError):
            return None
        return conversion_key(
            request.paper_id or "",
            request.tex_source_ref,
            request.pdf_ref,
            self.request.visual_policy,
        )

    async def _cached_conversion(self, request_ref: ArtifactRef) -> PaperContent | None:
        """取回这篇论文上次转换出来的结果。"""
        library = self.stack.library
        if library is None:
            return None
        key = await self._conversion_cache_key(request_ref)
        if key is None:
            return None
        return await library.load_paper(key, self.stack.artifacts)

    async def _cache_conversion(
        self, request_ref: ArtifactRef, content: PaperContent
    ) -> None:
        """把这篇论文的转换产物存进库。"""
        library = self.stack.library
        if library is None:
            return
        key = await self._conversion_cache_key(request_ref)
        if key is not None:
            await library.save_paper(key, content, self.stack.artifacts)

    async def _process(
        self,
        processor: PaperProcessor,
        outcome: PaperOutcome,
        request_ref: ArtifactRef,
    ) -> PaperContent | None:
        """执行单篇转换并落盘；异常收敛成报告里的一条失败记录。"""
        try:
            payload = await self.stack.artifacts.get_text(request_ref)
            request = PaperConversionRequest.model_validate_json(payload)
            content = await processor.process(request)
        except VisualInterpretationRequiredError as error:
            outcome.conversion_status = "failed"
            outcome.error = f"VisualInterpretationRequiredError: {error}"
            return None
        except Exception as error:  # noqa: BLE001 - preserve conversion failure capture
            outcome.conversion_status = "failed"
            outcome.error = f"{type(error).__name__}: {error}"
            return None
        outcome.paper_content_ref = await self.stack.artifacts.put_text(
            content.model_dump_json()
        )
        return content


class IndexStage:
    """paper_rag 阶段：质量门禁、碎片判定与建索引。"""

    def __init__(
        self, stack: "SurveyStack", request: SurveyRequest, state: PipelineState
    ) -> None:
        self.stack = stack
        self.request = request
        self.state = state

    async def run(self, papers: list[PaperContent]) -> None:
        """按质量门禁筛选后建语料索引。"""
        if not self.request.build_index:
            return
        selected = [item for item in papers if await self._indexable(item)]
        chosen = {item.paper_id or "" for item in selected}
        for content in papers:
            raw = content.paper_id or ""
            key = self.state.conversion_keys.get(raw, raw)
            self.state.outcome_for(key).indexed = raw in chosen
        if not selected:
            return
        started = time.monotonic()
        try:
            self.state.report.corpus_ref = await build_corpus_index(
                self.stack.artifacts,
                selected,
                self.stack.embedder,
                vectors=self.stack.vector_cache(),
                paper_edges=self._index_edges(selected),
            )
        except OpenAIError as error:
            self.state.report.warnings.append(f"index_failed: {type(error).__name__}")
            for content in selected:
                raw = content.paper_id or ""
                self.state.outcome_for(
                    self.state.conversion_keys.get(raw, raw)
                ).indexed = False
        self.state.report.timings.index_seconds = round(time.monotonic() - started, 3)

    async def _indexable(self, content: PaperContent) -> bool:
        """语料门禁：两条存在性判定一票否决，``degraded`` 默认放行。"""
        raw = content.paper_id or ""
        key = self.state.conversion_keys.get(raw, raw)
        outcome = self.state.outcome_for(key)
        if outcome.suspect_empty:
            return False
        if await judge_shredded(
            content, outcome, self.stack.artifacts, self.state.report
        ):
            return False
        if not self.request.strict_quality:
            return True
        return content.quality_status in INDEXABLE_QUALITY

    def _index_edges(self, selected: list[PaperContent]) -> dict[str, list[str]]:
        """把引用图从 scout 的 ``paper_key`` 翻成语料里的 ``paper_id``。"""
        canonical_to_raw: dict[str, str] = {}
        for content in selected:
            raw = content.paper_id or ""
            canonical_to_raw.setdefault(self.state.conversion_keys.get(raw, raw), raw)
        translated: dict[str, list[str]] = {}
        for source, targets in self.state.paper_edges.items():
            source_raw = canonical_to_raw.get(source)
            if source_raw is None:
                continue
            linked = [
                canonical_to_raw[target]
                for target in targets
                if target in canonical_to_raw and canonical_to_raw[target] != source_raw
            ]
            if linked:
                translated[source_raw] = list(dict.fromkeys(linked))
        self.state.report.reference_edges = sum(len(v) for v in translated.values())
        return translated


async def judge_shredded(
    content: PaperContent,
    outcome: PaperOutcome,
    artifacts,
    report: SurveyReport,
) -> bool:
    """正文是否被切成了碎片而不是句子；是则拒绝入语料并登记原因。"""
    units = await content.load_retrieval_units(artifacts)
    lengths = [
        end - start
        for unit in units
        if unit.kind != BIBLIOGRAPHY_KIND
        for start, end in split_sentences(unit.text)
    ]
    if len(lengths) < SHRED_MIN_SENTENCES:
        return False
    mean_length = sum(lengths) / len(lengths)
    if mean_length >= SHRED_MIN_SENTENCE_CHARS:
        return False
    outcome.shredded = True
    report.shredded_papers.append(
        f"{outcome.paper_key}: {len(lengths)} 句，平均 {mean_length:.0f} 字符"
        f"（低于 {SHRED_MIN_SENTENCE_CHARS}），判为提取碎片，未入语料"
    )
    return True
