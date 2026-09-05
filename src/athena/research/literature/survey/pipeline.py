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

import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef
from athena.core.tool_types import EmitEvent
from athena.research.literature.contracts import VisualPolicy
from athena.research.literature.paper_markdown.models import (
    PaperContent,
    PaperConversionRequest,  # noqa: F401 - established survey contract surface
    QualityStatus,
)
from athena.research.literature.paper_scout.schemas import (
    RETAIN_THRESHOLD,
    ScoutStats,
)
from athena.research.literature.paper_source.schemas import (
    PaperSourceRecord,
    SourcePreference,
)

if TYPE_CHECKING:
    from athena.research.literature.survey.wiring import SurveyStack

INDEXABLE_QUALITY: tuple[QualityStatus, ...] = ("pass", "pass_with_notes")

SHRED_MIN_SENTENCE_CHARS = 40
"""平均句长低于这个值就判为提取碎片而非正文，理由与取值依据见 ``_shredded``。

三轮真机 44 篇回放：合法论文的最低平均句长 69 字符，唯一的坏例子 30 字符，40 落在两者
之间。
"""

SHRED_MIN_SENTENCES = 200
"""少于这么多句就不做碎片判定。

短论文的平均句长本来就不稳（一篇只有几十句的会议短文，一个公式密集的段落就能把均值
拉下来），而它们再碎也贵不到哪里去——这道闸门是为代价加的，不该去动那些根本不贵的。
回放里有两篇分别只有 136 和 141 句，正是被这条线放过去的；坏例子有 35151 句，离它两个
数量级。
"""

DEFAULT_CONVERSION_CONCURRENCY = 4
"""并发转换的篇数。

真机测过三档，都没有出现限流或延迟劣化：7 篇在并发 2 下串行 276.8 秒、墙钟 141.4 秒；
50 篇在并发 3 下串行 3641 秒、墙钟 1181 秒；10 篇在并发 3 下串行 568.6 秒、墙钟 204.7 秒
（与 FIFO 列表调度的理论值 204.7 秒吻合到小数点后一位）。同一批按并发 4 重算是 156.9 秒，
省 48 秒。

再往上收益递减：一批的墙钟不可能低于其中最慢的那一篇，上面那批里最慢的是 94.9 秒。
单篇成本主要花在视觉调用的往返上，本机 CPU 不是瓶颈。
"""

SOURCE_CANDIDATE_MULTIPLE = 3
"""交给 ``paper_source`` 的候选篇数是 ``max_papers`` 的几倍。

取不到源只有试过才知道：``require_retrievable_source`` 能挡掉"上游没给任何线索"的论文，
挡不住线索本身失效。真机上三种都遇到过——``doi.org`` 重定向回 0 字节、ACM 与 MDPI 对
非浏览器请求返回 403，其中 MDPI 那篇确实是开放获取，纯粹被反爬拦下。

**成功率按通道差一倍**：实测六轮 arXiv 81/89 = 91%（排除传输故障那轮是 78/78 = 100%），
期刊 17/36 = 47%。而交付集合的通道构成每轮都不同——同分次序改用散列之后，期刊占比从
15% 升到 46%，于是原先按 1.3 倍超额取源的做法当场失准，13 篇里 5 篇取不到，交付掉到 8 篇。

固定的超额系数必然随构成失准，所以不再猜：候选多给一些，由 ``stop_after_fetched`` 顺序
尝试、够数即停，多余的候选一次都不下载。这个倍数只是攻击面上限——全是期刊论文的最坏情况
需要 ``10 / 0.47 ≈ 22`` 次尝试，3 倍留了足够余量，而没取到就停不下来的风险由它兜住。
"""

MIN_PLAUSIBLE_MARKDOWN = 2000
"""低于这个字符数就认为正文没被真正提取出来。

真机第一次批量运行时命中：``arxiv:1412.6980`` 的 TeX 包只有一个 298 字节的
``arxiv.tex``，用 ``\\includepdf`` 套着 534KB 的外部 PDF——正文全在 PDF 里。TeX
路径产出 29 个字符的 Markdown、0 条诊断，质量门禁给了 ``pass``。这类 PDF-wrapper
投稿在 arXiv 上并不罕见，而静默的全文丢失比转换报错危险得多：报错会被计入失败率，
"成功但空"会带着 ``pass`` 一路进语料，让检索以为这篇论文已经覆盖。

阈值取得很松，只用来识别"几乎什么都没有"，不替代 ``paper_markdown`` 自己的质量门禁。
"""

NOT_ATTEMPTED = "not_attempted"
"""候选从未被下载过——``stop_after_fetched`` 在轮到它之前就够数了。

不能沿用 ``skipped``：那是 ``PaperSourceRecord`` 已有的状态，含义是"下载前被策略拒绝"。
两件事共用一个标签，报告里就分不出"我们没试"和"试了但不合规"。多取候选之前候选数等于
尝试数，这个状态不存在，所以此前没有暴露。
"""

ConversionStatus = Literal["converted", "failed", "no_source", "surplus"]
"""``surplus``：源拿到了，但相关性排在 ``max_papers`` 之外，因此不转换。

取源改成"够数即停"之后正常情况下不会再出现，保留是因为它仍然是这一层的正确守卫——
``paper_source`` 的停止目标由策略给出，而转换名额由本模块负责，两者不该互相假设。
它必须和 ``failed`` 分开：多取是策略决定的，算进转换失败率会污染那个数字。
"""


class SurveyRequest(BaseModel):
    """一次全链路调研的输入。

    ``max_papers`` 默认 10。在默认的 ``retain_threshold=0`` 下它是交付量的唯一控制，
    但它**只影响下游三段**：``paper_scout`` 会把整个候选池都打一遍分（实测池 240 篇、
    ``pool_size`` 也是 240），``max_papers`` 只在 ``_finish`` 里做最后一次截断。
    所以调小它省的是取源、转换和索引，省不到检索——要压检索成本得调 ``max_steps``。

    真机换算（50 篇跑出来的数据按前 10 篇重算）：scout 640s 不变，取源 68s，转换 167s
    （并发 3），索引 39s，合计约 15 分钟，其中 scout 占七成。取 50 时合计 40 分钟。

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
    max_papers: int = Field(
        default=20,
        ge=1,
        description=(
            "Papers carried downstream. Does NOT affect retrieval — the whole pool "
            "is scored either way; this only decides how many survive truncation. "
            "Raised from 10 because the cut lands inside a score tier (dozens tie "
            "at one value) and because PaperLibrary makes each paper a one-time cost."
        ),
    )
    max_steps: int = Field(default=6, ge=1, description="PaperScout step budget.")
    search_top_k: int = Field(
        default=50, ge=1, description="Results per search call; see SEARCH_TOP_K_NOTE."
    )
    expand_top_k: int = Field(default=20, ge=1, description="References per expand.")
    max_seconds: float = Field(
        default=1800.0, gt=0, description="Scout wall budget; raised with search_top_k."
    )
    retain_threshold: float = Field(
        default=RETAIN_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="PaperScout delivery threshold; see RETAIN_THRESHOLD.",
    )
    require_retrievable_source: bool = Field(
        default=True,
        description="Deliver only fetchable papers; see has_retrievable_source.",
    )
    source_candidate_multiple: int = Field(
        default=SOURCE_CANDIDATE_MULTIPLE,
        ge=1,
        description=(
            "Hand paper_source this multiple of max_papers as candidates; it stops "
            "attempting once max_papers succeed. See SOURCE_CANDIDATE_MULTIPLE."
        ),
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
    strict_quality: bool = Field(
        default=False,
        description="Only index papers whose quality gate returned pass or pass_with_notes.",
    )
    build_index: bool = Field(
        default=True, description="Build the RAG corpus index after conversion."
    )
    fresh_scout: bool = Field(
        default=False,
        description=(
            "Re-run PaperScout even when this exact request is already in the paper "
            "library. Off by default: the same ScoutRequest is a deterministic input, "
            "so re-running it only re-pays 71% of the wall clock for the same answer."
        ),
    )


class PaperOutcome(BaseModel):
    """一篇论文走完全链路的逐段结果，含成本与质量事实。"""

    paper_key: str = Field(description="Namespaced RAG identity key.")
    title: str = Field(default="", description="Upstream title.")
    relevance: float = Field(default=0.0, description="PaperScout relevance score.")
    fetch_status: str = Field(
        description=(
            "fetched, failed, skipped, or not_attempted. The last one means this "
            "candidate was never downloaded because max_papers already succeeded; "
            "paper_source's own 'skipped' means a policy rejected it before download."
        )
    )
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
    shredded: bool = Field(
        default=False,
        description=(
            "Conversion produced fragments rather than sentences, so the paper was "
            "kept out of the corpus. The mirror image of suspect_empty."
        ),
    )
    chunks: int = Field(default=0, ge=0)
    visuals: int = Field(default=0, ge=0)
    visuals_interpreted: int = Field(default=0, ge=0)
    vision_calls: int = Field(default=0, ge=0, description="Model calls for visuals.")
    conversion_seconds: float = Field(default=0.0, ge=0.0)
    conversion_cached: bool = Field(
        default=False,
        description="Conversion was replayed from the paper library, paying no model calls.",
    )
    error: str = Field(default="", description="Failure reason, empty when fine.")
    paper_content_ref: ArtifactRef | None = Field(default=None)


class StageTimings(BaseModel):
    """各段墙钟耗时，用于定位瓶颈。"""

    scout_seconds: float = Field(default=0.0, ge=0.0)
    source_seconds: float = Field(default=0.0, ge=0.0)
    markdown_seconds: float = Field(default=0.0, ge=0.0)
    references_seconds: float = Field(default=0.0, ge=0.0)
    index_seconds: float = Field(default=0.0, ge=0.0)
    total_seconds: float = Field(default=0.0, ge=0.0)


class SurveyReport(BaseModel):
    """一次全链路调研的完整结果与成本账。"""

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(description="The survey topic.")
    status: str = Field(
        description=(
            "complete, partial, or empty; describes this run's own products, not the "
            "health of any upstream backend. See SurveyPipeline.final_status."
        )
    )
    scout_status: str = Field(
        default="",
        description=(
            "PaperScout's own verdict, kept separate: a rate-limited search backend "
            "says nothing about whether the corpus was built."
        ),
    )
    corpus_ref: ArtifactRef | None = Field(
        default=None, description="Corpus index ready for the retrieval tools."
    )
    scout_result_ref: ArtifactRef | None = Field(default=None)
    source_result_ref: ArtifactRef | None = Field(default=None)
    fetch_attempted: int = Field(
        default=0,
        ge=0,
        description=(
            "Candidates paper_source actually tried before max_papers succeeded; "
            "the rest were never downloaded."
        ),
    )
    surplus_dropped: int = Field(
        default=0,
        ge=0,
        description=(
            "Papers that fetched successfully but ranked beyond max_papers, so they "
            "were never converted. Normally zero now that fetching stops on target."
        ),
    )
    scout: ScoutStats = Field(
        default_factory=ScoutStats,
        description=(
            "PaperScout's own run statistics. Boundary tier, facets, busy-seconds and "
            "rerank call/failure counts all live here instead of being mirrored as "
            "SurveyReport fields."
        ),
    )
    shredded_papers: list[str] = Field(
        default_factory=list,
        description=(
            "Papers refused by the shred gate, one line each with the numbers behind "
            "the refusal. A list rather than a count: this gate discards a paper the "
            "grader wanted, so every refusal has to be reviewable."
        ),
    )
    retain_threshold: float = Field(
        default=RETAIN_THRESHOLD,
        description="Delivery threshold this run used; delivery counts mean nothing without it.",
    )
    score_histogram: dict[str, int] = Field(
        default_factory=dict,
        description="Pool relevance distribution, for deciding where the threshold belongs.",
    )
    fetched: int = Field(
        default=0, ge=0, description="Papers with usable source bytes."
    )
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
    reference_lookups: int = Field(
        default=0,
        ge=0,
        description="Delivered papers whose reference list was resolved upstream.",
    )
    reference_edges: int = Field(
        default=0,
        ge=0,
        description=(
            "In-corpus paper-to-paper citation edges. Parsing bibliographies alone "
            "yielded 1 edge across 20 papers, which makes paper_cites inert."
        ),
    )
    scout_cached: bool = Field(
        default=False, description="PaperScout was replayed from the paper library."
    )
    library: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Paper library hit/miss counters for this run. A second run of the same "
            "query should show near-zero model and http cost; see `timings`."
        ),
    )
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
        垫底富余的论文同样不计入：它们取到了源却按策略不转换，算进去只会让这个
        数字随取源策略浮动。
        """
        attempted = [
            item
            for item in self.papers
            if item.fetch_status == "fetched" and item.conversion_status != "surplus"
        ]
        if not attempted:
            return 0.0
        failed = sum(1 for item in attempted if item.conversion_status == "failed")
        return failed / len(attempted)


class PipelineState:
    """Mutable state shared by the survey execution stages."""

    def __init__(self, report: SurveyReport, convert_cap: int) -> None:
        self.report = report
        self.outcomes: dict[str, PaperOutcome] = {}
        self.by_identifier: dict[str, str] = {}
        self.conversion_keys: dict[str, str] = {}
        self.retained: list = []
        self.paper_edges: dict[str, list[str]] = {}
        self.convert_cap = convert_cap

    def outcome_for(self, paper_key: str) -> PaperOutcome:
        """Return or create a paper outcome."""
        outcome = self.outcomes.get(paper_key)
        if outcome is None:
            outcome = PaperOutcome(
                paper_key=paper_key,
                fetch_status=NOT_ATTEMPTED,
                conversion_status="no_source",
            )
            self.outcomes[paper_key] = outcome
        return outcome

    def register_identifiers(self, paper_key: str, source: object) -> None:
        """Map source identifiers to the canonical paper key."""
        for prefix, attribute in (
            ("arxiv", "arxiv_id"),
            ("doi", "doi"),
            ("s2", "s2_paper_id"),
        ):
            value = getattr(source, attribute, "") or ""
            if value:
                self.by_identifier[f"{prefix}:{value.lower()}"] = paper_key

    def canonical_key(self, record: PaperSourceRecord) -> str:
        """Return the existing key matching a source record."""
        identity = record.identity
        for prefix, value in (
            ("arxiv", identity.arxiv_id),
            ("doi", identity.doi),
            ("s2", identity.s2_paper_id),
        ):
            if value:
                known = self.by_identifier.get(f"{prefix}:{value.lower()}")
                if known is not None:
                    return known
        return record.paper_key

    def count(self, field: str) -> int:
        """Count outcomes with a populated progress field."""
        values = [getattr(item, field) for item in self.outcomes.values()]
        if field == "conversion_status":
            return sum(value == "converted" for value in values)
        return sum(bool(value) for value in values)


from athena.research.literature.survey.stages import (
    ConversionStage,
    FetchStage,
    IndexStage,
    ScoutStage,
    judge_shredded,
)


class SurveyPipeline:
    """把 scout / source / markdown / index 四段串成一次可测量的运行。

    每一段的失败都不终止流程：scout 一篇都没交付时后面各段自然为空，取源失败的
    论文不进转换，转换失败的论文不进索引。这样一次运行总能产出完整的
    ``SurveyReport``，而不是在半路抛异常丢掉已经付出的成本。
    """

    def __init__(
        self,
        stack: "SurveyStack",
        request: SurveyRequest,
        *,
        emit: EmitEvent | None = None,
    ) -> None:
        self.stack = stack
        self.request = request
        self.state = PipelineState(
            SurveyReport(query=request.query, status="empty"), request.max_papers
        )
        self.report = self.state.report
        self._emit = emit or _silent_emit

    async def run(self) -> SurveyReport:
        """执行全链路，返回逐篇结果与成本账。"""
        started = time.monotonic()
        source_request_ref = await ScoutStage(
            self.stack, self.request, self.state, self._emit
        ).run()
        await self._emit(
            "survey/scouted",
            self.report.scout_result_ref or "",
            {
                "pool": self.report.scout.pool_size,
                "retained": self.report.scout.retained_papers,
            },
        )
        if source_request_ref is not None:
            source_result = await FetchStage(self.stack, self.request, self.state).run(
                source_request_ref
            )
            await self._emit(
                "survey/fetched",
                self.report.source_result_ref or "",
                {
                    "fetched": self.report.fetched,
                    "attempted": self.report.fetch_attempted,
                },
            )
            papers = await ConversionStage(self.stack, self.request, self.state).run(
                source_result
            )
            await self._emit(
                "survey/converted",
                "",
                {
                    "converted": self.state.count("conversion_status"),
                    "papers": len(papers),
                },
            )
            await IndexStage(self.stack, self.request, self.state).run(papers)
            await self._emit(
                "survey/indexed",
                self.report.corpus_ref or "",
                {"indexed": self.state.count("indexed")},
            )
        self.report.timings.total_seconds = round(time.monotonic() - started, 3)
        self.report.http_requests = self.stack.http.request_count
        self._collect_model_costs()
        if self.stack.library is not None:
            self.report.library = self.stack.library.stats()
        self.report.papers = sorted(
            self.state.outcomes.values(),
            key=lambda item: (-item.relevance, item.paper_key),
        )
        self.report.status = self.final_status()
        return self.report

    def final_status(self) -> str:
        """本次运行的结论，只看它自己产出了什么。"""
        if not self.report.converted():
            return "empty"
        if self.request.build_index and self.report.corpus_ref is None:
            return "partial"
        return "complete"

    def candidate_cap(self) -> int:
        """交给取源的候选上限；实际尝试几篇由 ``stop_after_fetched`` 决定。"""
        return self.request.max_papers * self.request.source_candidate_multiple

    def _scout_cache_key(self, request_json: str) -> str:
        """本次检索在库里的键；请求 + 打分器指纹，见 ``library.scout_key``。"""
        return ScoutStage(self.stack, self.request, self.state, self._emit).cache_key(
            request_json
        )

    async def _shredded(self, content: PaperContent, outcome: PaperOutcome) -> bool:
        """正文是否被切成了碎片而不是句子；是则拒绝入语料并登记原因。"""
        return await judge_shredded(content, outcome, self.stack.artifacts, self.report)

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


async def _silent_emit(_kind: str, _ref: str, _data: dict | None = None) -> None:
    """默认事件汇：不订阅时事实全部落在 SurveyReport 里。"""


async def run_survey(
    stack: "SurveyStack",
    request: SurveyRequest,
    *,
    emit: EmitEvent | None = None,
) -> SurveyReport:
    """跑一次完整的 Academic Survey，返回逐篇结果与成本账。

    ``emit`` 订阅四段的进度事件（``paper_scout/*`` 与 ``survey/*``）。全链路十几分钟
    起步，放进长时运行的宿主里而不报进度，看上去与卡死没有区别；不订阅时行为不变。
    """
    return await SurveyPipeline(stack, request, emit=emit).run()
