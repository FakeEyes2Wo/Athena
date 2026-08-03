"""Academic Survey 全链路驱动：scout → source → markdown → RAG 索引。

四段之间的交接契约本来就是闭合的（``PaperScoutResult.paper_source_request_ref``
→ ``PaperSourceRecord.conversion_request_ref`` → ``PaperContent`` →
``build_corpus_index``），缺的只是一个把它们依次推进的东西。本模块就是那个东西。

刻意写成确定性流程而不是 Agent：四段之间没有需要多轮推理的决策——读多少篇、
失败怎么办、降级的要不要进索引，全部是可以提前定下的策略参数。交给模型只会让
成本不可预测，也违背"能用普通函数完成的不增加 Agent"。

``SurveyReport`` 是本模块的主要产出，不只是副产品：它按篇记录取源通道、转换耗时、
质量码与视觉调用数，用来回答"``paper_markdown`` 在真实数据上的失败率和单篇成本
是多少"——这个问题在此之前没有任何数据支撑，因为该模块从未在测试外运行过。
"""

import asyncio
import time
from typing import Literal

from pydantic import BaseModel, Field

from athena.core.agent.agent import AgentContext
from athena.core.schemas import ArtifactRef, AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.research.paper_markdown.processor import (
    PaperProcessor,
    VisualInterpretationRequiredError,
)
from athena.research.paper_markdown.schemas import (
    PaperContent,
    PaperConversionRequest,
    QualityStatus,
    VisualPolicy,
)
from athena.research.paper_rag.index import build_corpus_index
from athena.research.paper_scout.agent import PaperScoutAgent
from athena.research.paper_scout.schemas import (
    RETAIN_THRESHOLD,
    PaperScoutResult,
    ScoutCorpus,
    ScoutRequest,
)
from athena.research.paper_scout.scorer import GradedRelevanceScorer
from athena.research.paper_source.fetcher import PaperSourceFetcher
from athena.research.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRecord,
    PaperSourceRequest,
    PaperSourceResult,
    SourcePreference,
)
from athena.research.wiring import ResearchStack

INDEXABLE_QUALITY: tuple[QualityStatus, ...] = ("pass", "pass_with_notes")
DEFAULT_CONVERSION_CONCURRENCY = 2

MIN_PLAUSIBLE_MARKDOWN = 2000
"""低于这个字符数就认为正文没被真正提取出来。

真机第一次批量运行时命中：``arxiv:1412.6980`` 的 TeX 包只有一个 298 字节的
``arxiv.tex``，用 ``\\includepdf`` 套着 534KB 的外部 PDF——正文全在 PDF 里。TeX
路径产出 29 个字符的 Markdown、0 条诊断，质量门禁给了 ``pass``。这类 PDF-wrapper
投稿在 arXiv 上并不罕见，而静默的全文丢失比转换报错危险得多：报错会被计入失败率，
"成功但空"会带着 ``pass`` 一路进语料，让检索以为这篇论文已经覆盖。

阈值取得很松，只用来识别"几乎什么都没有"，不替代 ``paper_markdown`` 自己的质量门禁。
"""

ConversionStatus = Literal["converted", "failed", "no_source"]


class SurveyRequest(BaseModel):
    """一次全链路调研的输入。

    ``max_papers`` 默认取 5 而不是 ``PaperSourcePolicy`` 的 50：真正决定成本的是
    ``paper_markdown``（TeX/PDF 解析加逐图模型调用），首次真机验证要的是可观测，
    不是覆盖面。

    ``visual_policy`` 默认 ``best_effort`` 而不是 schema 默认的 ``required``：
    ``required`` 下任何一张图解读失败都会让整篇论文失败，测出来的是"有没有失败"，
    而 ``best_effort`` 会把每次失败记成一条诊断并继续，测出来的是"失败了多少、
    失败在哪"。要摸清失败率就得选后者。
    """

    query: str = Field(min_length=1, description="Natural language survey topic.")
    arxiv_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Skip retrieval and fetch these papers directly; used to measure the "
            "downstream stages without paying for or being gated by PaperScout."
        ),
    )
    max_papers: int = Field(default=5, ge=1, description="Papers carried downstream.")
    max_steps: int = Field(default=6, ge=1, description="PaperScout step budget.")
    search_top_k: int = Field(default=10, ge=1, description="Results per search call.")
    expand_top_k: int = Field(default=20, ge=1, description="References per expand.")
    max_seconds: float = Field(default=600.0, gt=0, description="Scout wall budget.")
    retain_threshold: float = Field(
        default=RETAIN_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="PaperScout delivery threshold; see RETAIN_THRESHOLD.",
    )
    published_to: str = Field(default="", description="Inclusive ISO date upper bound.")
    prefer: SourcePreference = Field(default="tex", description="Source preference.")
    visual_policy: VisualPolicy = Field(
        default="best_effort", description="Visual interpretation strictness."
    )
    conversion_concurrency: int = Field(
        default=DEFAULT_CONVERSION_CONCURRENCY,
        ge=1,
        description="Papers converted in parallel.",
    )
    index_degraded: bool = Field(
        default=False, description="Index papers whose quality gate did not pass."
    )
    build_index: bool = Field(
        default=True, description="Build the RAG corpus index after conversion."
    )


class PaperOutcome(BaseModel):
    """一篇论文走完全链路的逐段结果，含成本与质量事实。"""

    paper_key: str = Field(description="Namespaced RAG identity key.")
    title: str = Field(default="", description="Upstream title.")
    relevance: float = Field(default=0.0, description="PaperScout relevance score.")
    fetch_status: str = Field(description="fetched, skipped, or failed.")
    source_kind: str = Field(default="", description="tex or pdf when fetched.")
    source_locator: str = Field(default="", description="Exact provenance locator.")
    conversion_status: ConversionStatus = Field(description="Conversion outcome.")
    quality_status: str = Field(default="", description="Quality gate verdict.")
    quality_codes: list[str] = Field(default_factory=list)
    indexed: bool = Field(default=False, description="Entered the corpus index.")
    markdown_chars: int = Field(
        default=0, ge=0, description="Size of the converted Markdown."
    )
    suspect_empty: bool = Field(
        default=False,
        description="Conversion succeeded but produced implausibly little text.",
    )
    chunks: int = Field(default=0, ge=0)
    visuals: int = Field(default=0, ge=0)
    visuals_interpreted: int = Field(default=0, ge=0)
    vision_calls: int = Field(default=0, ge=0, description="Model calls for visuals.")
    conversion_seconds: float = Field(default=0.0, ge=0.0)
    error: str = Field(default="", description="Failure reason, empty when fine.")
    paper_content_ref: ArtifactRef | None = Field(default=None)


class StageTimings(BaseModel):
    """各段墙钟耗时，用于定位瓶颈。"""

    scout_seconds: float = Field(default=0.0, ge=0.0)
    source_seconds: float = Field(default=0.0, ge=0.0)
    markdown_seconds: float = Field(default=0.0, ge=0.0)
    index_seconds: float = Field(default=0.0, ge=0.0)
    total_seconds: float = Field(default=0.0, ge=0.0)


class SurveyReport(BaseModel):
    """一次全链路调研的完整结果与成本账。"""

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(description="The survey topic.")
    status: str = Field(description="complete, partial, or empty.")
    corpus_ref: ArtifactRef | None = Field(
        default=None, description="Corpus index ready for the retrieval tools."
    )
    scout_result_ref: ArtifactRef | None = Field(default=None)
    source_result_ref: ArtifactRef | None = Field(default=None)
    scout_pool: int = Field(default=0, ge=0, description="Papers accepted into pool.")
    scout_retained: int = Field(default=0, ge=0, description="Papers above threshold.")
    retain_threshold: float = Field(
        default=RETAIN_THRESHOLD,
        description="Delivery threshold this run used; delivery counts mean nothing without it.",
    )
    score_histogram: dict[str, int] = Field(
        default_factory=dict,
        description="Pool relevance distribution, for deciding where the threshold belongs.",
    )
    fetched: int = Field(default=0, ge=0, description="Papers with usable source bytes.")
    fetch_failed: int = Field(
        default=0, ge=0, description="Papers no channel could resolve."
    )
    papers: list[PaperOutcome] = Field(default_factory=list)
    timings: StageTimings = Field(default_factory=StageTimings)
    http_requests: int = Field(default=0, ge=0)
    vision_calls: int = Field(default=0, ge=0)
    vision_failures: int = Field(default=0, ge=0)
    embed_calls: int = Field(default=0, ge=0)
    embedded_texts: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)

    def converted(self) -> int:
        """成功转换的篇数。"""
        return sum(1 for item in self.papers if item.conversion_status == "converted")

    def suspect_empty_count(self) -> int:
        """转换报成功、正文却近乎为空的篇数。"""
        return sum(1 for item in self.papers if item.suspect_empty)

    def conversion_failure_rate(self) -> float:
        """``paper_markdown`` 的失败率；分母只算真正拿到源文件的论文。

        取源失败不计入分母：那是网络与开放获取的问题，和转换器的健壮性无关，
        混在一起会让这个数字既不能用来评估 paper_markdown，也不能用来评估取源。
        """
        attempted = [item for item in self.papers if item.fetch_status == "fetched"]
        if not attempted:
            return 0.0
        failed = sum(1 for item in attempted if item.conversion_status == "failed")
        return failed / len(attempted)


class SurveyPipeline:
    """把 scout / source / markdown / index 四段串成一次可测量的运行。

    每一段的失败都不终止流程：scout 一篇都没交付时后面各段自然为空，取源失败的
    论文不进转换，转换失败的论文不进索引。这样一次运行总能产出完整的
    ``SurveyReport``，而不是在半路抛异常丢掉已经付出的成本。
    """

    def __init__(self, stack: ResearchStack, request: SurveyRequest) -> None:
        self.stack = stack
        self.request = request
        self.report = SurveyReport(query=request.query, status="empty")
        self._outcomes: dict[str, PaperOutcome] = {}
        self._by_identifier: dict[str, str] = {}
        self._conversion_keys: dict[str, str] = {}

    async def run(self) -> SurveyReport:
        """执行全链路，返回逐篇结果与成本账。"""
        started = time.monotonic()
        source_request_ref = await self._scout()
        if source_request_ref is not None:
            source_result = await self._fetch(source_request_ref)
            papers = await self._convert(source_result)
            await self._index(papers)
        self.report.timings.total_seconds = round(time.monotonic() - started, 3)
        self.report.http_requests = self.stack.http.request_count
        self._collect_model_costs()
        # 报告的论文列表在此统一构建：登记项在各段被就地改写，最后一次成型才能
        # 保证顺序稳定，也避免用 pydantic 的值相等去判断"这一项是否已加入"
        self.report.papers = sorted(
            self._outcomes.values(),
            key=lambda item: (-item.relevance, item.paper_key),
        )
        return self.report

    async def _scout(self) -> ArtifactRef | None:
        """跑 PaperScout，把交付集合转成取源请求引用；零交付时返回 ``None``。

        给了 ``arxiv_ids`` 时整段跳过：检索是全链路里最慢也最贵的一段，而测量
        取源与转换的健壮性并不需要它，把两者绑在一起只会让下游的样本量受制于
        检索门槛。
        """
        if self.request.arxiv_ids:
            return await self._direct_source_request()
        started = time.monotonic()
        backends, references = self.stack.build_backends()
        agent = PaperScoutAgent(
            self.stack.artifacts,
            backends,
            references,
            GradedRelevanceScorer(self.stack.client, self.stack.model),
            model=self.stack.model,
            client=self.stack.client,
        )
        scout_request = ScoutRequest(
            query=self.request.query,
            published_to=self.request.published_to,
            max_steps=self.request.max_steps,
            search_top_k=self.request.search_top_k,
            expand_top_k=self.request.expand_top_k,
            max_papers=self.request.max_papers,
            max_seconds=self.request.max_seconds,
            retain_threshold=self.request.retain_threshold,
            paper_source_policy=PaperSourcePolicy(
                prefer=self.request.prefer,
                max_papers=self.request.max_papers,
                visual_policy=self.request.visual_policy,
            ),
        )
        request_ref = await self.stack.artifacts.put_text(
            scout_request.model_dump_json()
        )
        outcome = await agent.run(self._agent_context(request_ref))
        self.report.timings.scout_seconds = round(time.monotonic() - started, 3)
        return await self._read_scout_result(outcome.result_ref)

    async def _direct_source_request(self) -> ArtifactRef | None:
        """把显式给出的 arXiv id 直接做成取源请求，并登记为待处理论文。"""
        papers = []
        for raw in self.request.arxiv_ids:
            identity = PaperIdentity(arxiv_id=raw)
            key = identity.paper_key()
            papers.append(PaperRef(identity=identity))
            self._outcomes[key] = PaperOutcome(
                paper_key=key,
                title=raw,
                relevance=1.0,
                fetch_status="skipped",
                conversion_status="no_source",
            )
            self._register_identifiers(key, identity)
        self.report.status = "complete"
        self.report.scout_retained = len(papers)
        request = PaperSourceRequest(
            papers=papers,
            policy=PaperSourcePolicy(
                prefer=self.request.prefer,
                max_papers=max(self.request.max_papers, len(papers)),
                visual_policy=self.request.visual_policy,
            ),
        )
        return await self.stack.artifacts.put_text(request.model_dump_json())

    async def _read_scout_result(self, result_ref: str) -> ArtifactRef | None:
        """读回 scout 结果，登记每篇论文的初始状态。"""
        result = PaperScoutResult.model_validate_json(
            await self.stack.artifacts.get_text(result_ref)
        )
        corpus = ScoutCorpus.model_validate_json(
            await self.stack.artifacts.get_text(result.corpus_ref)
        )
        self.report.scout_result_ref = result_ref
        self.report.scout_pool = len(corpus.pool)
        self.report.scout_retained = len(corpus.retained)
        self.report.retain_threshold = self.request.retain_threshold
        # 分数分布是决定门槛该放在哪的唯一依据：交付 2 篇既可能是"池里只有 2 篇好的"，
        # 也可能是"18 篇 2 分被门槛挡住了"，只看交付量分不出这两种情况
        histogram: dict[str, int] = {}
        for paper in corpus.pool:
            bucket = f"{paper.relevance:.2f}"
            histogram[bucket] = histogram.get(bucket, 0) + 1
        self.report.score_histogram = dict(sorted(histogram.items(), reverse=True))
        self.report.warnings.extend(result.warnings)
        self.report.status = result.status
        for paper in corpus.retained:
            self._outcomes[paper.paper_key] = PaperOutcome(
                paper_key=paper.paper_key,
                title=paper.title,
                relevance=paper.relevance,
                fetch_status="skipped",
                conversion_status="no_source",
            )
            self._register_identifiers(paper.paper_key, paper)
        return result.paper_source_request_ref

    def _register_identifiers(self, paper_key: str, source: object) -> None:
        """给一篇论文的每个标识符登记它在本次运行里的规范 key。

        scout 与 paper_source 的 ``paper_key`` 优先级不同——前者 arXiv 优先（``expand``
        沿 arXiv id 工作），后者期刊 DOI 优先（arXiv 自铸 DOI 会让语料按投稿年份分裂）。
        同一篇论文因此会在交接处换 key，两边各自都对，把它们并回一条是本层的职责。
        """
        for prefix, attribute in (("arxiv", "arxiv_id"), ("doi", "doi"), ("s2", "s2_paper_id")):
            value = getattr(source, attribute, "") or ""
            if value:
                self._by_identifier[f"{prefix}:{value.lower()}"] = paper_key

    def _canonical_key(self, record: PaperSourceRecord) -> str:
        """把取源记录并回 scout 的那一条；查不到任何标识符时沿用记录自己的 key。"""
        identity = record.identity
        candidates = (
            ("arxiv", identity.arxiv_id),
            ("doi", identity.doi),
            ("s2", identity.s2_paper_id),
        )
        for prefix, value in candidates:
            if not value:
                continue
            known = self._by_identifier.get(f"{prefix}:{value.lower()}")
            if known is not None:
                return known
        return record.paper_key

    async def _fetch(self, source_request_ref: ArtifactRef) -> PaperSourceResult:
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
        )
        result = await fetcher.fetch(request)
        self.report.timings.source_seconds = round(time.monotonic() - started, 3)
        self.report.source_result_ref = await self.stack.artifacts.put_text(
            result.model_dump_json()
        )
        for record in result.records:
            key = self._canonical_key(record)
            outcome = self._outcome_for(key)
            outcome.fetch_status = record.status
            outcome.source_locator = record.source_locator or ""
            outcome.source_kind = "tex" if record.tex_source_ref else ""
            if not outcome.source_kind and record.pdf_ref:
                outcome.source_kind = "pdf"
            # 取源阶段解析出的新标识符（例如补上期刊 DOI）也要能并回同一条，
            # 否则转换阶段按 PaperContent.paper_id 查找时会再分裂一次
            self._register_identifiers(key, record.identity)
            self._conversion_keys[record.paper_key] = key
        self.report.fetched = result.stats.fetched
        self.report.fetch_failed = result.stats.failed + result.stats.skipped
        return result

    async def _convert(self, source_result: PaperSourceResult) -> list[PaperContent]:
        """并发转换所有拿到源文件的论文，逐篇计时并记录质量结果。"""
        started = time.monotonic()
        jobs = [
            (self._conversion_keys.get(record.paper_key, record.paper_key),
             record.conversion_request_ref)
            for record in source_result.records
            if record.conversion_request_ref
        ]
        if not jobs:
            self.report.timings.markdown_seconds = round(time.monotonic() - started, 3)
            return []
        processor = PaperProcessor(
            self.stack.artifacts, self.stack.visual_interpreter, None
        )
        limit = asyncio.Semaphore(self.request.conversion_concurrency)
        results = await asyncio.gather(
            *(self._convert_one(processor, limit, key, ref) for key, ref in jobs)
        )
        self.report.timings.markdown_seconds = round(time.monotonic() - started, 3)
        return [item for item in results if item is not None]

    async def _convert_one(
        self,
        processor: PaperProcessor,
        limit: asyncio.Semaphore,
        paper_key: str,
        request_ref: ArtifactRef,
    ) -> PaperContent | None:
        """转换一篇论文；失败只记进报告，不影响同批其他论文。"""
        outcome = self._outcome_for(paper_key)
        interpreter = self.stack.visual_interpreter
        calls_before = interpreter.calls if interpreter is not None else 0
        # 计时必须在拿到信号量之后开始：并发受限时排队等待会被算进单篇成本，
        # 让每篇的耗时都趋同于批次总耗时，成本数字就失去意义
        async with limit:
            started = time.monotonic()
            content = await self._process(processor, outcome, request_ref)
            outcome.conversion_seconds = round(time.monotonic() - started, 3)
        if interpreter is not None:
            outcome.vision_calls = interpreter.calls - calls_before
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
            # visual_policy=required 且视觉解读失败 → 整篇作废，这是该策略的定义
            outcome.conversion_status = "failed"
            outcome.error = f"VisualInterpretationRequiredError: {error}"
            return None
        except Exception as error:
            # TeX/PDF 解析、编码、模型调用都可能失败 → 记录类型与消息后继续下一篇
            outcome.conversion_status = "failed"
            outcome.error = f"{type(error).__name__}: {error}"
            return None
        outcome.paper_content_ref = await self.stack.artifacts.put_text(
            content.model_dump_json()
        )
        return content

    async def _index(self, papers: list[PaperContent]) -> None:
        """按质量门禁筛选后建语料索引。"""
        if not self.request.build_index:
            return
        selected = [item for item in papers if self._indexable(item)]
        chosen = {item.paper_id or "" for item in selected}
        for content in papers:
            raw = content.paper_id or ""
            key = self._conversion_keys.get(raw, raw)
            self._outcome_for(key).indexed = raw in chosen
        if not selected:
            return
        started = time.monotonic()
        self.report.corpus_ref = await build_corpus_index(
            self.stack.artifacts, selected, self.stack.embedder
        )
        self.report.timings.index_seconds = round(time.monotonic() - started, 3)

    def _indexable(self, content: PaperContent) -> bool:
        """质量门禁：默认只放行 ``pass`` 与 ``pass_with_notes``。

        ``suspect_empty`` 一票否决，``index_degraded`` 也覆盖不了：放宽门禁的用意是
        "宁可收进质量有瑕疵的论文"，而不是"收进一篇空壳"——空壳既提供不了证据，
        又会让语料看起来已经覆盖这篇论文。
        """
        raw = content.paper_id or ""
        key = self._conversion_keys.get(raw, raw)
        if self._outcome_for(key).suspect_empty:
            return False
        if self.request.index_degraded:
            return True
        return content.quality_status in INDEXABLE_QUALITY

    def _outcome_for(self, paper_key: str) -> PaperOutcome:
        """取出或新建一篇论文的登记项。

        取源阶段会用解析后的身份重算 ``paper_key``，可能与 scout 阶段不同（例如
        补上了期刊 DOI），因此这里必须容忍新 key 而不是断言存在。
        """
        outcome = self._outcomes.get(paper_key)
        if outcome is None:
            outcome = PaperOutcome(
                paper_key=paper_key,
                fetch_status="skipped",
                conversion_status="no_source",
            )
            self._outcomes[paper_key] = outcome
        return outcome

    def _collect_model_costs(self) -> None:
        """汇总视觉与编码模型的调用数。"""
        interpreter = self.stack.visual_interpreter
        if interpreter is not None:
            self.report.vision_calls = interpreter.calls
            self.report.vision_failures = interpreter.failures
        embedder = self.stack.embedder
        if embedder is not None:
            self.report.embed_calls = embedder.calls
            self.report.embedded_texts = embedder.embedded

    def _agent_context(self, request_ref: ArtifactRef) -> AgentContext:
        """构造 PaperScout 需要的最小 Turn 上下文。

        流程不跑在 ``app_server`` 的 ThreadRuntime 上，因此这里自建 thread/turn；
        接入 ``app_server`` 后应改为由 ThreadRuntime 注入。
        """
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
            emit=_silent_emit,
            tools=ToolRegistry(),
            cancel=asyncio.Event(),
        )


async def _silent_emit(_kind: str, _ref: str, _data: dict | None = None) -> None:
    """默认事件汇：全链路驱动不订阅事件流，事实全部落在 SurveyReport 里。"""


async def run_survey(stack: ResearchStack, request: SurveyRequest) -> SurveyReport:
    """跑一次完整的 Academic Survey，返回逐篇结果与成本账。"""
    return await SurveyPipeline(stack, request).run()
