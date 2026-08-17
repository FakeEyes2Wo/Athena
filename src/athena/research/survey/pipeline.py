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
from typing import TYPE_CHECKING, Literal

from openai import OpenAIError
from pydantic import BaseModel, Field

from athena.core.agent.models import AgentContext
from athena.core.contracts import ArtifactRef
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.core.tool_types import EmitEvent
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
from athena.research.paper_rag.index import (
    BIBLIOGRAPHY_KIND,
    build_corpus_index,
    split_sentences,
)
from athena.research.paper_scout.agent import PaperScoutAgent
from athena.research.paper_scout.schemas import (
    RETAIN_THRESHOLD,
    PaperScoutResult,
    ScoutCorpus,
    ScoutRequest,
    ScoutStats,
)
from athena.research.paper_scout.scorer import DEFAULT_PASSES, GradedRelevanceScorer
from athena.research.paper_scout.selection import LlmBoundarySelector
from athena.research.survey.library import (
    conversion_key,
    copy_refs,
    scout_key,
)
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

if TYPE_CHECKING:
    from athena.research.survey.wiring import SurveyStack

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
    ``scored_papers`` 也是 240），``max_papers`` 只在 ``_finish`` 里做最后一次截断。
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
    scout_pool: int = Field(default=0, ge=0, description="Papers accepted into pool.")
    scout_retained: int = Field(
        default=0,
        ge=0,
        description="Papers PaperScout delivered as fetch candidates.",
    )
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
    boundary_tier: int = Field(
        default=0,
        ge=0,
        description="Papers tied at the delivery cut; see ScoutStats.boundary_tier.",
    )
    boundary_reranked: bool = Field(
        default=False,
        description=(
            "The tie at the cut was resolved by the LLM boundary selector. Distinct "
            "from affinity_calls below: that is the cross-encoder ordering every tie "
            "in the pool, this is one LLM call on the tier at the cut."
        ),
    )
    affinity_calls: int = Field(
        default=0,
        ge=0,
        description="Cross-encoder requests that ordered papers tied on relevance.",
    )
    affinity_failures: int = Field(
        default=0,
        ge=0,
        description=(
            "Cross-encoder batches that failed after retry. Those papers sort last "
            "within their grade, so this is a bias indicator, not just a cost one."
        ),
    )
    facets: list[str] = Field(
        default_factory=list, description="Facets the topic was split into."
    )
    facet_coverage: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Fraction of facets delivered."
    )
    shredded_papers: list[str] = Field(
        default_factory=list,
        description=(
            "Papers refused by the shred gate, one line each with the numbers behind "
            "the refusal. A list rather than a count: this gate discards a paper the "
            "grader wanted, so every refusal has to be reviewable."
        ),
    )
    scout_dropped_no_source: int = Field(
        default=0,
        ge=0,
        description=(
            "Papers above the threshold that were dropped for having no fetchable "
            "source, and so never competed for a delivery slot."
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
        self.report = SurveyReport(query=request.query, status="empty")
        self._emit = emit or _silent_emit
        self._outcomes: dict[str, PaperOutcome] = {}
        self._by_identifier: dict[str, str] = {}
        self._conversion_keys: dict[str, str] = {}
        self._retained: list = []
        self._paper_edges: dict[str, list[str]] = {}
        # 取源可以多要几篇垫底，转换不能——转换才是花钱的那一段
        self._convert_cap = request.max_papers
        # 独立跑时事实全部落在 SurveyReport 里；接进 loop 后它是个十几分钟的后台任务，
        # 没有逐段回报的话，外部无法区分"正在取第 7 篇"与"卡死了"。
        self._emit = emit or _silent_emit

    async def run(self) -> SurveyReport:
        """执行全链路，返回逐篇结果与成本账。"""
        started = time.monotonic()
        source_request_ref = await self._scout()
        await self._emit(
            "survey/scouted",
            self.report.scout_result_ref or "",
            {"pool": self.report.scout_pool, "retained": self.report.scout_retained},
        )
        if source_request_ref is not None:
            source_result = await self._fetch(source_request_ref)
            await self._emit(
                "survey/fetched",
                self.report.source_result_ref or "",
                {
                    "fetched": self.report.fetched,
                    "attempted": self.report.fetch_attempted,
                },
            )
            papers = await self._convert(source_result)
            # 逐篇计数只能从登记项来：``report.papers`` 要到 ``run`` 收尾时才成型，
            # 在这里读 ``report.converted()`` 恒为 0
            await self._emit(
                "survey/converted",
                "",
                {"converted": self._count("conversion_status"), "papers": len(papers)},
            )
            await self._index(papers)
            await self._emit(
                "survey/indexed",
                self.report.corpus_ref or "",
                {"indexed": self._count("indexed")},
            )
        self.report.timings.total_seconds = round(time.monotonic() - started, 3)
        self.report.http_requests = self.stack.http.request_count
        self._collect_model_costs()
        if self.stack.library is not None:
            self.report.library = self.stack.library.stats()
        # 报告的论文列表在此统一构建：登记项在各段被就地改写，最后一次成型才能
        # 保证顺序稳定，也避免用 pydantic 的值相等去判断"这一项是否已加入"
        self.report.papers = sorted(
            self._outcomes.values(),
            key=lambda item: (-item.relevance, item.paper_key),
        )
        self.report.status = self.final_status()
        return self.report

    def _count(self, field: str) -> int:
        """统计登记项里某个进度字段成立的篇数，供中途的进度事件使用。

        ``conversion_status`` 比对 ``"converted"``，其余字段按真值判断。
        """
        values = [getattr(item, field) for item in self._outcomes.values()]
        if field == "conversion_status":
            return sum(1 for value in values if value == "converted")
        return sum(1 for value in values if value)

    def final_status(self) -> str:
        """本次运行的结论，只看它自己产出了什么。

        ``empty`` 一篇都没转换成功；``partial`` 转换出了东西但要的产物缺了一件，
        目前只有一种情况——要求建索引却没拿到 ``corpus_ref``；否则 ``complete``。

        这里刻意不看上游后端的健康度。此前是直接沿用 ``PaperScoutResult.status``，
        于是 Semantic Scholar 零星 429 就能把整轮标成 ``partial``——真机上出现过
        语料建好、7 篇全进索引、报告却写着 ``partial`` 的情况，看报告的人只会以为
        语料没建成。后端的问题在 ``warnings`` 与 ``scout_status`` 里，不该冒充结论。

        逐篇的取源与转换失败同样不降级：那是尽力而为流水线的正常产出，篇数、失败率
        和逐篇原因都已经在报告里，用一个总状态去概括只会丢掉信息。
        """
        if not self.report.converted():
            return "empty"
        if self.request.build_index and self.report.corpus_ref is None:
            return "partial"
        return "complete"

    def candidate_cap(self) -> int:
        """交给取源的候选上限；实际尝试几篇由 ``stop_after_fetched`` 决定。"""
        return self.request.max_papers * self.request.source_candidate_multiple

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
            GradedRelevanceScorer(
                self.stack.client, self.stack.effective_scorer_model()
            ),
            model=self.stack.model,
            client=self.stack.client,
            # 边界档重排用策略模型那一档：这一次调用决定将近一半的交付集合，是全链路
            # 里单次影响最大的一次判断，不该省在这里。
            selector=LlmBoundarySelector(self.stack.client, self.stack.model),
            # 同分论文的次序信号。没配 ATHENA_RERANK_MODEL 时为 None，排序退回散列。
            reranker=self.stack.reranker,
        )
        candidates = self.candidate_cap()
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
        cached = await self._cached_scout(request_json)
        if cached is not None:
            self.report.timings.scout_seconds = round(time.monotonic() - started, 3)
            self.report.scout_cached = True
            return await self._read_scout_result(cached)
        request_ref = await self.stack.artifacts.put_text(request_json)
        outcome = await agent.run(self._agent_context(request_ref))
        self.report.timings.scout_seconds = round(time.monotonic() - started, 3)
        await self._cache_scout(request_json, outcome.result_ref)
        return await self._read_scout_result(outcome.result_ref)

    def _scout_cache_key(self, request_json: str) -> str:
        """本次检索在库里的键；请求 + 打分器指纹，见 ``library.scout_key``。"""
        return scout_key(request_json, self._scorer_fingerprint())

    def _scorer_fingerprint(self) -> str:
        """打分器的身份：模型名 + 打分遍数 + 同分次序用的交叉编码器。

        三样都不在 ``ScoutRequest`` 里（由组合根决定），却都会改变交付集合，所以必须
        显式进缓存键。``DEFAULT_PASSES`` 从 1 改成 2 的那次，正是因为它不在键里而让库里
        的旧结果继续命中——改了等于没改，而报告上看不出任何异常。

        rerank 模型同样要进：真机一轮 352 篇里 197 篇同分，换掉拆平局的那个信号，交付
        集合里将近一半会变。
        """
        return (
            f"{self.stack.effective_scorer_model()}"
            f"/{DEFAULT_PASSES}"
            f"/{self.stack.rerank_model()}"
        )

    async def _cached_scout(self, request_json: str) -> ArtifactRef | None:
        """取回同一份请求上次跑出的检索结果，并把它引用的 blob 复制回本地存储。

        检索占 10 篇尺寸下 71% 的墙钟（实测 581/820 秒），而它是**确定性输入的函数**：
        同一份 ``ScoutRequest`` 再跑一遍不会得到新东西，只会重付 4 次策略调用与几十次
        打分。想要新的一批论文时应当改请求（或走 ``fresh_scout``），而不是靠"每次都
        重跑"来碰运气——那正是 tie_break 散列造成的不可复现，不是特性。

        任何一环缺失都当作未命中：缓存是纯加速，宁可重跑也不能交出半份结果。
        """
        library = self.stack.library
        if library is None or self.request.fresh_scout:
            return None
        payload = await library.load_text(self._scout_cache_key(request_json))
        if payload is None:
            return None
        try:
            result = PaperScoutResult.model_validate_json(payload)
        except ValueError:
            # 库里的结果与当前 schema 不兼容 → 重新检索
            return None
        refs = [result.corpus_ref, result.stats_ref, result.paper_source_request_ref]
        wanted = [ref for ref in refs if ref]
        if await copy_refs(library.store, self.stack.artifacts, wanted) != len(wanted):
            return None
        self.report.warnings.append("scout_cache_hit")
        return await self.stack.artifacts.put_text(payload)

    async def _cache_scout(self, request_json: str, result_ref: ArtifactRef) -> None:
        """把这次检索的结果连同它引用的 blob 一起存进库。"""
        library = self.stack.library
        if library is None:
            return
        payload = await self.stack.artifacts.get_text(result_ref)
        try:
            result = PaperScoutResult.model_validate_json(payload)
        except ValueError:
            # 结果读不出来就不缓存；这一轮照常继续
            return
        refs = [result.corpus_ref, result.stats_ref, result.paper_source_request_ref]
        await copy_refs(
            self.stack.artifacts, library.store, [ref for ref in refs if ref]
        )
        await library.save_text(self._scout_cache_key(request_json), payload)

    async def _direct_source_request(self) -> ArtifactRef | None:
        """把显式给出的 arXiv id 直接做成取源请求，并登记为待处理论文。

        这条路径不做取源垫底：显式点名的论文就是要的全部，多取无从取起，少取也不该
        被别的论文顶替。
        """
        papers = []
        for raw in self.request.arxiv_ids:
            identity = PaperIdentity(arxiv_id=raw)
            key = identity.paper_key()
            papers.append(PaperRef(identity=identity))
            self._outcomes[key] = PaperOutcome(
                paper_key=key,
                title=raw,
                relevance=1.0,
                fetch_status=NOT_ATTEMPTED,
                conversion_status="no_source",
            )
            self._register_identifiers(key, identity)
        self.report.scout_retained = len(papers)
        self._convert_cap = max(self.request.max_papers, len(papers))
        request = PaperSourceRequest(
            papers=papers,
            policy=PaperSourcePolicy(
                prefer=self.request.prefer,
                max_papers=self._convert_cap,
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
        stats = ScoutStats.model_validate_json(
            await self.stack.artifacts.get_text(result.stats_ref)
        )
        self.report.scout_result_ref = result_ref
        self.report.scout_pool = len(corpus.pool)
        self.report.scout_retained = len(corpus.retained)
        self.report.scout_dropped_no_source = stats.dropped_no_source
        self.report.boundary_tier = stats.boundary_tier
        self.report.boundary_reranked = stats.boundary_reranked
        self.report.affinity_calls = stats.rerank_calls
        self.report.affinity_failures = stats.rerank_failures
        self.report.facets = list(stats.facets)
        self.report.facet_coverage = stats.facet_coverage
        self.report.retain_threshold = self.request.retain_threshold
        # 分数分布是决定门槛该放在哪的唯一依据：交付 2 篇既可能是"池里只有 2 篇好的"，
        # 也可能是"18 篇 2 分被门槛挡住了"，只看交付量分不出这两种情况
        histogram: dict[str, int] = {}
        for paper in corpus.pool:
            bucket = f"{paper.relevance:.2f}"
            histogram[bucket] = histogram.get(bucket, 0) + 1
        self.report.score_histogram = dict(sorted(histogram.items(), reverse=True))
        self.report.warnings.extend(result.warnings)
        self.report.scout_status = result.status
        # 留着交付的 ScoutPaper 本体：取 references 需要它们的标识符，而 PaperOutcome
        # 只存了 paper_key
        self._retained = list(corpus.retained)
        # 引用图由 scout 建好带过来：那一层本来就持有 reference backend，在这里另建一份
        # 会让管线的单测打到真实网络上。
        self._paper_edges = {
            source: list(targets) for source, targets in corpus.reference_edges.items()
        }
        self.report.reference_lookups = len(self._paper_edges)
        for paper in corpus.retained:
            self._outcomes[paper.paper_key] = PaperOutcome(
                paper_key=paper.paper_key,
                title=paper.title,
                relevance=paper.relevance,
                fetch_status=NOT_ATTEMPTED,
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
        for prefix, attribute in (
            ("arxiv", "arxiv_id"),
            ("doi", "doi"),
            ("s2", "s2_paper_id"),
        ):
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
            # 落盘的定位符缓存。内容寻址存储能去重字节，去重不了 HTTP 请求，而 arXiv
            # 每 3 秒只允许一次——不给路径的话缓存只活在进程内，重跑必然重新下载一遍。
            cache=self.stack.locator_cache(),
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
        self.report.fetch_attempted = result.stats.attempted
        return result

    async def _convert(self, source_result: PaperSourceResult) -> list[PaperContent]:
        """转换取到源的论文，按 ``_convert_cap`` 截断后并发执行。

        ``paper_source`` 保持上游顺序（``fetcher.fetch`` 顺序遍历 ``accepted``），而
        上游顺序就是相关性降序，所以"前 N 条取到源的记录"正是相关性最高的 N 篇。
        截断放在转换之前而不是取源之前：垫底的意义就是先取回来再挑，取源不调模型，
        转换才是花钱的那一段。
        """
        started = time.monotonic()
        fetched = [
            (
                self._conversion_keys.get(record.paper_key, record.paper_key),
                record.conversion_request_ref,
            )
            for record in source_result.records
            if record.conversion_request_ref
        ]
        jobs = fetched[: self._convert_cap]
        for key, _ in fetched[self._convert_cap :]:
            self._outcome_for(key).conversion_status = "surplus"
        self.report.surplus_dropped = len(fetched) - len(jobs)
        if not jobs:
            self.report.timings.markdown_seconds = round(time.monotonic() - started, 3)
            return []
        processor = PaperProcessor(
            self.stack.artifacts,
            self.stack.visual_interpreter,
            None,
            ghostscript=self.stack.ghostscript or None,
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
        cached = await self._cached_conversion(request_ref)
        if cached is not None:
            outcome.conversion_cached = True
            content = cached
        else:
            # 计时必须在拿到信号量之后开始：并发受限时排队等待会被算进单篇成本，
            # 让每篇的耗时都趋同于批次总耗时，成本数字就失去意义
            async with limit:
                started = time.monotonic()
                content = await self._process(processor, outcome, request_ref)
                outcome.conversion_seconds = round(time.monotonic() - started, 3)
            if content is not None:
                await self._cache_conversion(request_ref, content)
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

    async def _conversion_cache_key(self, request_ref: ArtifactRef) -> str | None:
        """本篇转换在库里的键；请求读不出来时返回 ``None``（走正常转换路径）。

        键取自 ``PaperConversionRequest`` 里的源引用，而那些引用本身就是内容散列——
        "同一篇论文的同一份源码"因此天然是同一个键，不必另外指纹。
        """
        try:
            request = PaperConversionRequest.model_validate_json(
                await self.stack.artifacts.get_text(request_ref)
            )
        except (ValueError, OSError):
            # 请求本身有问题 → 交给 _process 去报这条失败，不在缓存层吞掉
            return None
        return conversion_key(
            request.paper_id or "",
            request.tex_source_ref,
            request.pdf_ref,
            self.request.visual_policy,
        )

    async def _cached_conversion(self, request_ref: ArtifactRef) -> PaperContent | None:
        """取回这篇论文上次转换出来的结果。

        转换是整条链路里唯一按篇调用多模态模型的一段（44 篇实测 1347 次视觉调用），
        而它的输入完全由源字节决定。缓存键里带转换器版本与视觉策略，改了解析器或换了
        策略会自动失效——需要人记得手动清的缓存迟早会喂回过期结果，而那种错误只会表现为
        "这次改动没效果"。
        """
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
        selected = [item for item in papers if await self._indexable(item)]
        chosen = {item.paper_id or "" for item in selected}
        for content in papers:
            raw = content.paper_id or ""
            key = self._conversion_keys.get(raw, raw)
            self._outcome_for(key).indexed = raw in chosen
        if not selected:
            return
        started = time.monotonic()
        try:
            self.report.corpus_ref = await build_corpus_index(
                self.stack.artifacts,
                selected,
                self.stack.embedder,
                vectors=self.stack.vector_cache(),
                paper_edges=self._index_edges(selected),
            )
        except OpenAIError as error:
            # 建索引是最后一段，也是唯一会一次性打光编码配额的一段。异常逃出去会连同
            # 前面所有已完成的取源与转换一起丢掉——而那才是真正花了钱的部分，且每篇的
            # PaperContent 已经落盘，重跑只需重新编码。因此降级成"没有语料"而不是没有报告。
            # 状态不在这里写：``corpus_ref`` 留空本身就是证据，``final_status``
            # 统一按"要的产物缺没缺"下结论
            self.report.warnings.append(f"index_failed: {type(error).__name__}")
            for content in selected:
                raw = content.paper_id or ""
                self._outcome_for(self._conversion_keys.get(raw, raw)).indexed = False
        self.report.timings.index_seconds = round(time.monotonic() - started, 3)

    def _index_edges(self, selected: list[PaperContent]) -> dict[str, list[str]]:
        """把引用图从 scout 的 ``paper_key`` 翻成语料里的 ``paper_id``。

        两套 id 不同是既有事实（``_register_identifiers`` 的注释说明了为什么），所以这一步
        必须走同一套并表逻辑，否则边会连到不存在的落点上、在建索引时被整批丢掉。
        """
        canonical_to_raw: dict[str, str] = {}
        for content in selected:
            raw = content.paper_id or ""
            canonical_to_raw.setdefault(self._conversion_keys.get(raw, raw), raw)
        translated: dict[str, list[str]] = {}
        for source, targets in self._paper_edges.items():
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
        self.report.reference_edges = sum(len(v) for v in translated.values())
        return translated

    async def _indexable(self, content: PaperContent) -> bool:
        """语料门禁：两条存在性判定一票否决，``degraded`` 默认放行。

        ``degraded`` 是存在性判定——出现**一条**内容缺失诊断就否决整篇，与论文规模
        无关。实测一批真实论文里，85 个 chunk 的论文因 1 条诊断出局，5 篇被挡的论文
        逐条核对后 4 篇是误判、1 篇只是参考文献未解析而正文完整。而 ``paper_rag`` 本
        身是 chunk 级检索：坏掉的公式或表格 chunk 不会被捞出来，局部缺陷不该否决整篇。

        一票否决的两条都不是"内容好不好"，而是"提取到底成没成功"：

        - ``suspect_empty`` —— 正文根本没提取出来。空壳既提供不了证据，又会让语料
          看起来已经覆盖这篇论文。
        - ``shredded``（见 ``_shredded``）—— 提取出来的不是正文而是碎片。

        ``strict_quality`` 保留严格口径，供需要"只要干净语料"的评测使用。
        """
        raw = content.paper_id or ""
        key = self._conversion_keys.get(raw, raw)
        outcome = self._outcome_for(key)
        if outcome.suspect_empty:
            return False
        if await self._shredded(content, outcome):
            return False
        if not self.request.strict_quality:
            return True
        return content.quality_status in INDEXABLE_QUALITY

    async def _shredded(self, content: PaperContent, outcome: PaperOutcome) -> bool:
        """正文是否被切成了碎片而不是句子；是则拒绝入语料并登记原因。

        **这道闸门是按代价加的，判据却只能是内容。** 2026-08-17 真机一轮里
        ``arxiv:1106.1813``（SMOTE）的 TeX 包缺 ``\\begin{document}``，入口推断失败，
        转换器把整包连成 774904 字符、140 chunk、**35151 句**——一篇占掉整个语料的
        66%、整次调研墙钟的 47%（2147 秒）。此前的门禁放行了它：``rag_chunk_oversized``
        属于"记录但不拦"，而那套判据问的是内容对不对，**不问代价**。

        但"太贵所以不要"不是个能写死的判据——长综述本来就该贵。真正的判据是那 35151
        条根本不是句子。把三轮真机的 44 篇 ``PaperContent`` 逐篇回放本闸门：

        ==========================  ========  ==========  ==========
        论文                        句数      平均句长    判定
        ==========================  ========  ==========  ==========
        ``arxiv:1106.1813``         35151     **30**      拦下
        ``arxiv:2512.05469``        957       69          放行（最接近门限）
        ``arxiv:2506.16791``        3039      92          放行
        ``doi:10.1038/s41598-...``  1077      99          放行
        ==========================  ========  ==========  ==========

        平均句长比"每 chunk 句数"更可靠：密度随体裁变化（综述天然长），而 30 个字符的
        "句子"在任何体裁里都不是句子，是 2002 年双栏排版被拆出来的断行。

        阈值取 40，落在 30 与 69 之间：比坏样本高 33%，比最接近的合法样本低 42%。
        **样本很薄**——44 篇、同一条 query、只有一个坏例子——所以宁可放过也不误杀，
        并且把每一次拒绝都记进 ``shredded_papers`` 让它可被复核。

        走 ``load_retrieval_units`` + ``split_sentences``、并同样跳过参考文献单元，
        与 ``build_corpus_index`` 逐字一致：闸门量的必须是建索引时**真会产生**的那些
        句子，两处定义一旦漂移，闸门就在量别的东西。代价是 chunk 正文被多读一遍，
        那是本地磁盘读，相对它要挡下的编码开销可以忽略。
        """
        units = await content.load_retrieval_units(self.stack.artifacts)
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
        self.report.shredded_papers.append(
            f"{outcome.paper_key}: {len(lengths)} 句，平均 {mean_length:.0f} 字符"
            f"（低于 {SHRED_MIN_SENTENCE_CHARS}），判为提取碎片，未入语料"
        )
        return True

    def _outcome_for(self, paper_key: str) -> PaperOutcome:
        """取出或新建一篇论文的登记项。

        取源阶段会用解析后的身份重算 ``paper_key``，可能与 scout 阶段不同（例如
        补上了期刊 DOI），因此这里必须容忍新 key 而不是断言存在。
        """
        outcome = self._outcomes.get(paper_key)
        if outcome is None:
            outcome = PaperOutcome(
                paper_key=paper_key,
                fetch_status=NOT_ATTEMPTED,
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

        全链路是确定性流程，不由 ``AgentRuntime`` 派发，因此这里自建 thread/turn；
        作为 agent type 注册后应改为由 ``BaseAgentRunner`` 注入。
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
            # 检索是全链路里最慢的一段（实测约占墙钟 70%），PaperScout 自己的
            # started/step/completed 事件是这段唯一的进度来源，必须往外传
            emit=self._emit,
            tools=ToolRegistry(),
            cancel=asyncio.Event(),
        )


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
