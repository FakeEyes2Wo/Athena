"""把上游论文线索解析成版本固定的 TeX/PDF artifact 与 paper_markdown 转换请求。

阶段顺序是刻意的：先批量解析 arXiv 版本（每 60 篇 1 次请求），再按篇下载（唯一按篇计费
的阶段），最后按魔数判定字节到底是 TeX 包还是 PDF。

第三步不能省。arXiv 对纯 PDF 投稿的 ``/src`` 端点直接返回 PDF 字节，而 paper_markdown 的
``auto`` 解包在识别不出归档格式时会把任意字节包装成 ``main.tex`` 且不报错 —— 于是一篇论
文会静默变成一段乱码正文，进 RAG 后无法追查。按魔数分流到 ``pdf_ref`` 是唯一可靠的防线。

``paper_key`` 只由上游给出的身份计算，不受网络补全影响：chunk 主键是
``{paper_key}:{chunk_id}``，若 key 随某次 DOI 补全成功与否而改变，同一篇论文会在语料库里
出现两份。
"""

import asyncio
import difflib
import gzip
import io
import json
import re
import tarfile
import urllib.parse
import zipfile
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from athena.core.artifact_store import ArtifactIntegrityError, ArtifactNotFoundError
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.literature.contracts import (
    ProcessingDiagnostic,
    TexSourceFormat,
)
from athena.research.literature.paper_source.arxiv import (
    ArxivClient,
    ArxivMetadata,
    ArxivResolution,
    versioned_locator,
)
from athena.research.literature.paper_source.http import (
    HostRateLimiter,
    HttpResponse,
    HttpTransportError,
)
from athena.research.literature.paper_source.openalex import (
    OpenAlexClient,
    OpenAlexWork,
)
from athena.research.literature.paper_source.schemas import (
    PaperIdentity,
    PaperRef,
    PaperSourcePolicy,
    PaperSourceRecord,
    PaperSourceRequest,
    PaperSourceResult,
    PaperSourceStats,
)

TEX_MARKERS = (b"\\documentclass", b"\\begin{document}", b"\\section", b"\\input{")
TEX_FORMATS = frozenset({"auto", "tar", "tar.gz", "zip", "gzip", "plain"})
PDF_HINT_KINDS = frozenset({"oa_pdf", "publisher_pdf"})
TITLE_MATCH_THRESHOLD = 0.75
MAX_PDF_CANDIDATES = 3
MAX_SOURCE_BYTES = 256 * 1024 * 1024
NON_ALNUM = re.compile(r"[^0-9a-z]+")


class PayloadError(ValueError):
    """Raised when a compressed source payload cannot be inspected safely."""


def diagnostic(level: str, code: str, message: str) -> ProcessingDiagnostic:
    """Construct a stable source diagnostic record."""
    return ProcessingDiagnostic(level=level, code=code, message=message)


def _is_tar_payload(payload: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*"):
            return True
    except tarfile.ReadError:
        return False


def _decompress_gzip(payload: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
            decompressed = archive.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        raise PayloadError("Invalid gzip TeX source.") from error
    if len(decompressed) > MAX_SOURCE_BYTES:
        raise PayloadError("Gzip TeX source exceeds the 256 MiB safety limit.")
    return decompressed


def _gzip_format(payload: bytes) -> TexSourceFormat | None:
    """Distinguish gzip-wrapped tar archives from plain compressed TeX."""
    try:
        inner = _decompress_gzip(payload)
    except PayloadError:
        return None
    return "tar.gz" if _is_tar_payload(inner) else "gzip"


def _looks_like_tex(payload: bytes) -> bool:
    """Recognize plain TeX while rejecting HTML error pages."""
    head = payload[:8192].lstrip()
    if head.startswith(b"<") or b"<html" in head.lower():
        return False
    return any(marker in payload for marker in TEX_MARKERS)


def sniff_payload(payload: bytes) -> tuple[str, TexSourceFormat | None]:
    """Classify source bytes as PDF, TeX package, or unknown content."""
    if not payload:
        return "unknown", None
    if payload.startswith(b"%PDF-"):
        return "pdf", None
    if zipfile.is_zipfile(io.BytesIO(payload)):
        return "tex", "zip"
    if payload.startswith(b"\x1f\x8b"):
        detected = _gzip_format(payload)
        return ("tex", detected) if detected else ("unknown", None)
    if _is_tar_payload(payload):
        return "tex", "tar"
    if _looks_like_tex(payload):
        return "tex", "plain"
    return "unknown", None


def _normalize_title(value: str) -> str:
    """Normalize title text for cross-source comparison."""
    return NON_ALNUM.sub(" ", value.lower()).strip()


def title_similarity(left: str, right: str) -> float:
    """Return normalized title similarity, treating missing titles as unknown."""
    first = _normalize_title(left)
    second = _normalize_title(right)
    if not first or not second:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def pdf_hint_urls(paper: PaperRef) -> list[str]:
    """Return distinct HTTP PDF hints in upstream order."""
    return list(
        dict.fromkeys(hint.url for hint in paper.hints if hint.kind in PDF_HINT_KINDS)
    )


def openalex_locator(identity: PaperIdentity) -> str:
    """Select the most stable OpenAlex lookup locator available."""
    if identity.openalex_id:
        return identity.openalex_id
    if identity.doi:
        return f"doi:{identity.doi}"
    if identity.pmid:
        return f"pmid:{identity.pmid}"
    return ""


@dataclass(slots=True)
class FetchedPayload:
    """One validated downloaded or cached source payload."""

    kind: str
    ref: ArtifactRef
    channel: str
    locator: str
    tex_format: TexSourceFormat | None = None
    cache_hit: bool = False


@dataclass(slots=True)
class ChannelOutcome:
    """Result of one source channel attempt."""

    payload: FetchedPayload | None = None
    attempted: bool = False


def conversion_request(
    record: PaperSourceRecord,
    policy: PaperSourcePolicy,
) -> dict[str, object]:
    """Build the immutable request consumed by the Markdown converter."""
    return {
        "tex_source_ref": record.tex_source_ref,
        "tex_source_format": record.tex_source_format or "auto",
        "tex_entrypoint": None,
        "pdf_ref": record.pdf_ref,
        "paper_id": record.paper_key,
        "metadata": record.metadata,
        "visual_policy": policy.visual_policy,
        "chunking": policy.chunking.model_dump(),
    }


def fetch_status(outcome: ChannelOutcome) -> str:
    """Classify a channel outcome as fetched, failed, or policy-skipped."""
    if outcome.payload:
        return "fetched"
    return "failed" if outcome.attempted else "skipped"


ALLOWED_SCHEMES = frozenset({"http", "https"})
CACHE_VERSION = 1

DEFAULT_FETCH_CONCURRENCY = 4
"""同时尝试取源的篇数。

不是靠它压 arXiv：那一路由 ``HostRateLimiter`` 按 3 秒一个排队，并发多少都一样。真正
并行起来的是出版商那一路——一轮实测 32 次尝试里 DOI 占 17 次，而 **12 次失败全在 DOI
通道**（MDPI/IEEE/Wiley 反爬），超时比成功还贵，正是最该并行的一类。

取 4 而不是更大，是因为 ``stop_after_fetched`` 的代价随它线性增长：每一波最多有
``concurrency - 1`` 次下载在够数之后才回来，那几篇会被丢掉。4 与
``DEFAULT_CONVERSION_CONCURRENCY`` 对齐，两段的并发形状保持一致。
"""


class LocatorCache:
    """把 ``arxiv:2501.10120v2/src`` 这类稳定定位符映射到已下载内容。

    内容寻址存储能去重字节，但去重不了 HTTP 请求；arXiv 每 3 秒只允许 1 次请求，所以重跑
    同一批语料必须完全不触网。可选的 JSON 落盘让缓存跨进程存活。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._entries: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._loaded = self._path is None

    def _load_sync(self) -> dict[str, dict]:
        """从磁盘读取缓存；文件缺失或损坏时返回空表。"""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # 首次运行还没有缓存文件 → 从空表开始
            return {}
        except json.JSONDecodeError:
            # 上次写入被中断留下半个 JSON → 丢弃缓存重新下载，不影响正确性
            return {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return entries if isinstance(entries, dict) else {}

    def _save_sync(self) -> None:
        """把缓存整表写回磁盘。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": CACHE_VERSION, "entries": self._entries}
        self._path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    async def get(self, locator: str) -> dict | None:
        """读取定位符对应的缓存条目。"""
        async with self._lock:
            if not self._loaded:
                self._entries = await asyncio.to_thread(self._load_sync)
                self._loaded = True
            entry = self._entries.get(locator)
        return dict(entry) if isinstance(entry, dict) else None

    async def put(self, locator: str, entry: dict) -> None:
        """写入定位符条目并按需落盘。"""
        async with self._lock:
            self._entries[locator] = entry
            if self._path is not None:
                await asyncio.to_thread(self._save_sync)


@dataclass(slots=True)
class PaperSourceRuntime:
    """Explicit resources shared by one or more source-fetch requests."""

    artifacts: ArtifactStore
    http: HostRateLimiter | None = None
    cache: LocatorCache | None = None
    concurrency: int = DEFAULT_FETCH_CONCURRENCY
    contact_email: InitVar[str | None] = None
    openalex_api_key: InitVar[str | None] = None
    arxiv: ArxivClient = field(init=False)
    openalex: OpenAlexClient = field(init=False)

    def __post_init__(
        self, contact_email: str | None, openalex_api_key: str | None
    ) -> None:
        self.http = self.http or HostRateLimiter(contact_email=contact_email)
        self.cache = self.cache or LocatorCache()
        self.concurrency = max(1, self.concurrency)
        self.arxiv = ArxivClient(self.http)
        self.openalex = OpenAlexClient(self.http, contact_email, openalex_api_key)


@dataclass(slots=True)
class _FetchAttempt:
    """Transient state for one ranked paper's source-channel attempts."""

    index: int
    paper: PaperRef
    policy: PaperSourcePolicy
    resolved: ArxivMetadata | None
    diagnostics: list[ProcessingDiagnostic] = field(default_factory=list)
    identity: PaperIdentity = field(init=False)
    paper_key: str = field(init=False)

    def __post_init__(self) -> None:
        self.identity = self.paper.identity.model_copy(deep=True)
        self.paper_key = self.paper.identity.paper_key()

    @property
    def version(self) -> int | None:
        return self.resolved.latest_version if self.resolved else None


@dataclass(frozen=True, slots=True)
class _Download:
    """One HTTP response together with its stable source identity."""

    response: HttpResponse
    locator: str
    channel: str


class PaperSourceFetcher:
    """把一批 ``PaperRef`` 解析成 ``PaperSourceRecord`` 与可直接执行的转换请求。

    所有网络节奏由注入的 ``HostRateLimiter`` 统一控制，单元测试替换其 transport 即可完全
    离线运行。
    """

    def __init__(self, runtime: PaperSourceRuntime) -> None:
        self.runtime = runtime

    async def fetch(
        self, request: PaperSourceRequest, cancel: asyncio.Event | None = None
    ) -> PaperSourceResult:
        """执行一次批量取源，返回逐篇结果与成本统计。"""
        # 阶段 1：按策略上限截断，保留上游排序
        started_requests = self.runtime.http.request_count
        accepted = request.papers[: request.policy.max_papers]
        diagnostics: list[ProcessingDiagnostic] = []
        dropped = len(request.papers) - len(accepted)
        if dropped:
            diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.max_papers_truncated",
                    f"Dropped {dropped} papers beyond the max_papers cap "
                    f"of {request.policy.max_papers}.",
                )
            )

        # 阶段 2：批量解析 arXiv 最新版本与元数据（每 60 篇 1 次请求）
        resolution = await self._resolve_versions(accepted, diagnostics)

        # 阶段 3：取源并生成转换请求（唯一按篇计费的阶段）
        #
        # ``stop_after_fetched`` 让调用方按"要几篇成功的"下单，而不是按"试几篇"。取源
        # 成功率按通道差一倍——实测六轮 arXiv 81/89 = 91%，期刊 17/36 = 47%（出版商反爬：
        # IEEE 返回 0 字节，MDPI 与 ACM 403）——而交付集合的通道构成每轮都不同，任何固定
        # 的超额系数都会在构成变化时失准。所以按名次收结果、够数即停。
        #
        # 按 ``concurrency`` 分波并发，而不是挨个等：一轮实测 32 次尝试花掉 387.8 秒
        # （平均 12.1 秒），而 12 次失败全部来自 DOI 通道的出版商反爬——超时比成功还贵。
        # 尝试的构成是 arXiv 15 / DOI 17，两类落在 ``HostRateLimiter`` 的不同桶里：
        # arXiv 那些照旧 3 秒一个排队，DOI 那些可以完全并行。
        #
        # **records 与串行版逐字相同**：``gather`` 保序，收的时候仍按名次、够数即停，
        # 超出停止点的那几条直接丢掉。代价是每波最多有 ``concurrency - 1`` 次多余下载
        # ——用有限的额外带宽换墙钟，不是零成本。
        records: list[PaperSourceRecord] = []
        target = request.policy.stop_after_fetched
        fetched_so_far = 0
        window = self.runtime.concurrency
        for start in range(0, len(accepted), window):
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            if target and fetched_so_far >= target:
                break
            batch = list(enumerate(accepted))[start : start + window]
            attempted = await asyncio.gather(
                *(
                    self._fetch_one(
                        _FetchAttempt(
                            index,
                            paper,
                            request.policy,
                            resolution.metadata.get(paper.identity.arxiv_id or ""),
                        )
                    )
                    for index, paper in batch
                )
            )
            # 按名次收，够数即停：丢掉本波超出停止点的那几条，让 records 与串行版逐字相同
            for record in attempted:
                if target and fetched_so_far >= target:
                    break
                records.append(record)
                if record.status == "fetched":
                    fetched_so_far += 1
        if target and len(records) < len(accepted):
            diagnostics.append(
                diagnostic(
                    "info",
                    "paper_source.stopped_after_target",
                    f"Stopped after {fetched_so_far} papers were fetched; "
                    f"{len(accepted) - len(records)} candidates were never attempted.",
                )
            )

        # 阶段 4：汇总统计
        stats = PaperSourceStats(
            requested=len(request.papers),
            accepted=len(accepted),
            attempted=len(records),
            fetched=sum(1 for record in records if record.status == "fetched"),
            skipped=sum(1 for record in records if record.status == "skipped"),
            failed=sum(1 for record in records if record.status == "failed"),
            cache_hits=sum(1 for record in records if record.cache_hit),
            http_requests=self.runtime.http.request_count - started_requests,
            tex_sources=sum(1 for record in records if record.tex_source_ref),
            pdf_sources=sum(
                1 for record in records if record.pdf_ref and not record.tex_source_ref
            ),
        )
        return PaperSourceResult(records=records, stats=stats, diagnostics=diagnostics)

    async def _resolve_versions(
        self, papers: list[PaperRef], diagnostics: list[ProcessingDiagnostic]
    ) -> ArxivResolution:
        """批量解析所有 arXiv id 的最新版本；批次级失败记入批次诊断。"""
        ordered: list[str] = []
        for paper in papers:
            arxiv_id = paper.identity.arxiv_id
            if arxiv_id and arxiv_id not in ordered:
                ordered.append(arxiv_id)
        if not ordered:
            return ArxivResolution()
        try:
            resolution = await self.runtime.arxiv.resolve_batch(ordered)
        except HttpTransportError as error:
            # 版本解析发生在逐篇取源之前，异常逃出去等于整批一篇都拿不到。退回空解析：
            # 各篇按未钉版本继续，由 version_unresolved 那条既有策略决定跳过还是放宽。
            diagnostics.append(
                diagnostic(
                    "error",
                    "paper_source.transport_failed",
                    f"arXiv metadata lookup was unreachable: {error}",
                )
            )
            return ArxivResolution()
        for message in resolution.errors:
            diagnostics.append(
                diagnostic("error", "paper_source.arxiv_lookup_failed", message)
            )
        return resolution

    async def _fetch_one(self, attempt: _FetchAttempt) -> PaperSourceRecord:
        """取单篇论文：补全身份 → 按通道下载 → 落盘 → 生成转换请求。

        ``paper_key`` 在任何网络调用之前就由上游身份算定，绝不受补全成败影响：它是 RAG
        chunk 主键的命名空间，一旦随某次 DOI 补全漂移，同一篇论文会在语料库里出现两份。
        """
        if attempt.policy.fetch_license and attempt.identity.arxiv_id:
            attempt.resolved = await self._merge_license(
                attempt.identity.arxiv_id, attempt.resolved
            )
        # 版本号在合并 OAI 记录之后才定。批量元数据端点被限流时（429/503）整批都解析不出
        # 版本，而 OAI 记录自带完整版本历史：取它兜底，一次瞬时抖动才不会让整批论文因
        # version_unresolved 全部跳过。Atom 已解析出版本时 _merge_license 会保留原值。
        metadata = self._build_metadata(attempt)
        try:
            outcome = await self._download(attempt)
        except HttpTransportError as error:
            # 这一篇的某个通道在传输层失败（重试耗尽后抛出）→ 只让这一篇失败。取源是
            # 逐篇循环，异常逃出去会让已经取到的论文一起丢掉，而取源恰恰是唯一按篇计费
            # 的阶段：50 篇跑到第 12 篇挂掉，前 11 篇的下载就白花了。
            attempt.diagnostics.append(
                diagnostic(
                    "error",
                    "paper_source.transport_failed",
                    f"Every channel for {attempt.paper_key} was unreachable: {error}",
                )
            )
            outcome = ChannelOutcome(attempted=True)
        record = PaperSourceRecord(
            ref_index=attempt.index,
            paper_key=attempt.paper_key,
            identity=attempt.identity,
            status=fetch_status(outcome),
            version=f"v{attempt.version}" if attempt.version else None,
            version_pinned=attempt.version is not None,
            license=attempt.resolved.license if attempt.resolved else None,
            metadata=metadata,
        )
        if outcome.payload is not None:
            await self._apply_payload(attempt, record, outcome.payload)
        elif not outcome.attempted:
            attempt.diagnostics.append(
                diagnostic(
                    "error",
                    "paper_source.no_channel_available",
                    f"No permitted channel could supply source bytes for {attempt.paper_key}.",
                )
            )
        # pydantic 在构造时会复制列表，因此收尾处把活的诊断列表挂回记录
        record.diagnostics = attempt.diagnostics
        return record

    async def _merge_license(
        self, arxiv_id: str, resolved: ArxivMetadata | None
    ) -> ArxivMetadata | None:
        """按策略额外取一次 OAI-PMH 记录，补上许可证与完整版本历史。"""
        raw = await self.runtime.arxiv.fetch_raw_record(arxiv_id)
        if raw is None:
            return resolved
        if resolved is None:
            return raw
        return resolved.model_copy(
            update={"license": raw.license, "versions": raw.versions}
        )

    def _build_metadata(self, attempt: _FetchAttempt) -> dict[str, str]:
        """合并上游与已解析元数据；解析结果优先，并做标题交叉校验。"""
        paper = attempt.paper
        identity = attempt.identity
        resolved = attempt.resolved
        metadata = {key: str(value) for key, value in paper.upstream_metadata.items()}
        if identity.title and "title" not in metadata:
            metadata["title"] = identity.title
        metadata["paper_key"] = attempt.paper_key
        if paper.retrieval_channels:
            metadata["retrieval_channels"] = ",".join(paper.retrieval_channels)
        if resolved is None:
            return metadata
        upstream_title = identity.title or metadata.get("title", "")
        if title_similarity(upstream_title, resolved.title) < TITLE_MATCH_THRESHOLD:
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.title_mismatch",
                    f"Upstream title '{upstream_title}' does not match "
                    f"resolved title '{resolved.title}'.",
                )
            )
        if identity.doi is None and resolved.doi:
            identity.doi = resolved.doi
        metadata.update(
            {
                "title": resolved.title,
                "authors": "; ".join(resolved.authors),
                "arxiv_id": resolved.arxiv_id,
                "categories": ",".join(resolved.categories),
                "published": resolved.published,
                "updated": resolved.updated,
            }
        )
        if resolved.doi:
            metadata["doi"] = resolved.doi
        if resolved.license:
            metadata["license"] = resolved.license
        return metadata

    async def _download(self, attempt: _FetchAttempt) -> ChannelOutcome:
        """按 arXiv → 开放获取的顺序尝试通道，第一个产出可用字节的通道胜出。"""
        attempted = False
        if attempt.identity.arxiv_id:
            outcome = await self._download_arxiv(attempt)
            attempted = attempted or outcome.attempted
            if outcome.payload:
                return outcome
        if not attempt.policy.allow_non_arxiv_channels:
            attempt.diagnostics.append(
                diagnostic(
                    "info",
                    "paper_source.non_arxiv_channel_disabled",
                    "Policy forbids open-access and OpenAlex channels.",
                )
            )
            return ChannelOutcome(attempted=attempted)
        outcome = await self._download_open_access(attempt)
        return ChannelOutcome(outcome.payload, attempted or outcome.attempted)

    async def _download_arxiv(self, attempt: _FetchAttempt) -> ChannelOutcome:
        """按固定版本下载 arXiv 源码，源码不可用时退到 PDF。"""
        arxiv_id = attempt.identity.arxiv_id
        if arxiv_id is None:
            return ChannelOutcome()
        version = attempt.version
        policy = attempt.policy
        if version is None and not policy.allow_unpinned_version:
            attempt.diagnostics.append(
                diagnostic(
                    "error",
                    "paper_source.version_unresolved",
                    f"arXiv version for {arxiv_id} is unresolved and unpinned "
                    "downloads are disabled.",
                )
            )
            return ChannelOutcome()
        if version is None:
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.unpinned_version",
                    f"Downloading {arxiv_id} without a version; the same paper_key "
                    "may resolve to different text later.",
                )
            )
        if policy.prefer != "pdf":
            outcome = await self._arxiv_channel(attempt, "src")
            if outcome.payload:
                return outcome
        return await self._arxiv_channel(attempt, "pdf")

    async def _arxiv_channel(
        self, attempt: _FetchAttempt, endpoint: str
    ) -> ChannelOutcome:
        """下载 ``/src`` 或 ``/pdf`` 端点，先查定位符缓存。"""
        arxiv_id = attempt.identity.arxiv_id
        if arxiv_id is None:
            return ChannelOutcome()
        version = attempt.version
        locator = f"arxiv:{versioned_locator(arxiv_id, version)}/{endpoint}"
        channel = f"arxiv_{endpoint}"
        cached = await self._cached_payload(locator, channel)
        if cached:
            return ChannelOutcome(cached, True)
        if endpoint == "src":
            response = await self.runtime.arxiv.fetch_source(arxiv_id, version)
        else:
            response = await self.runtime.arxiv.fetch_pdf(arxiv_id, version)
        if not response.ok:
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.arxiv_source_unavailable",
                    f"arXiv /{endpoint} returned HTTP {response.status} for {locator}.",
                )
            )
            return ChannelOutcome(attempted=True)
        return await self._accept_payload(
            attempt, _Download(response, locator, channel)
        )

    async def _download_open_access(self, attempt: _FetchAttempt) -> ChannelOutcome:
        """非 arXiv 通道：上游 PDF 线索 → OpenAlex 开放获取链接 → 计费内容端点。"""
        work = await self._openalex_work(attempt)
        candidates = [("hint_pdf", url) for url in pdf_hint_urls(attempt.paper)]
        if work and work.pdf_url:
            candidates.append(("openalex_pdf", work.pdf_url))
        attempted = False
        for channel, url in candidates[:MAX_PDF_CANDIDATES]:
            outcome = await self._fetch_url(attempt, url, channel)
            attempted = attempted or outcome.attempted
            if outcome.payload:
                return outcome
        if not attempt.policy.allow_paid_content_api or work is None:
            return ChannelOutcome(attempted=attempted)
        return await self._openalex_content(attempt, work, attempted)

    async def _openalex_work(self, attempt: _FetchAttempt) -> OpenAlexWork | None:
        """取一条 OpenAlex work 作为非 arXiv 论文的元数据与链接来源。"""
        locator = openalex_locator(attempt.identity)
        if not locator:
            return None
        work = await self.runtime.openalex.fetch_work(locator)
        if work is None:
            attempt.diagnostics.append(
                diagnostic(
                    "info",
                    "paper_source.openalex_lookup_failed",
                    f"OpenAlex has no usable record for {locator}.",
                )
            )
        return work

    async def _openalex_content(
        self,
        attempt: _FetchAttempt,
        work: OpenAlexWork,
        attempted: bool,
    ) -> ChannelOutcome:
        """计费内容端点；缺少 API key 时只记录诊断，绝不发出必然失败的请求。"""
        if not self.runtime.openalex.has_api_key:
            attempt.diagnostics.append(
                diagnostic(
                    "info",
                    "paper_source.paid_content_unavailable",
                    "Policy allows the metered OpenAlex content API "
                    "but no API key is configured.",
                )
            )
            return ChannelOutcome(attempted=attempted)
        response = await self.runtime.openalex.fetch_content_pdf(work.openalex_id)
        if response is None or not response.ok:
            status = response.status if response else 0
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.http_error",
                    f"OpenAlex content API returned HTTP {status} for {work.openalex_id}.",
                )
            )
            return ChannelOutcome(attempted=True)
        locator = f"openalex:{work.openalex_id}/content"
        return await self._accept_payload(
            attempt, _Download(response, locator, "openalex_content")
        )

    async def _fetch_url(
        self, attempt: _FetchAttempt, url: str, channel: str
    ) -> ChannelOutcome:
        """下载一个上游给出的候选链接；只允许 http(s)，先查定位符缓存。"""
        if urllib.parse.urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.url_scheme_rejected",
                    f"Refusing non-HTTP upstream download hint: {url}.",
                )
            )
            return ChannelOutcome()
        locator = f"url:{url}"
        cached = await self._cached_payload(locator, channel)
        if cached:
            return ChannelOutcome(cached, True)
        try:
            response = await self.runtime.http.get(url)
        except HttpTransportError as error:
            # 上游线索指向的第三方主机不可达（DNS/TLS/超时）→ 记一条诊断继续试下一个
            # 候选。这些 URL 来自检索后端，域名完全不可控，一个握手超时不该让整批取源
            # 中断，更不该让同一篇论文放弃 OpenAlex 通道。
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.transport_failed",
                    f"Download hint {url} was unreachable: {error}",
                )
            )
            return ChannelOutcome(attempted=True)
        if not response.ok:
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.http_error",
                    f"Download hint {url} returned HTTP {response.status}.",
                )
            )
            return ChannelOutcome(attempted=True)
        return await self._accept_payload(
            attempt, _Download(response, locator, channel)
        )

    async def _cached_payload(
        self, locator: str, channel: str
    ) -> FetchedPayload | None:
        """命中定位符缓存并确认字节仍在 artifact 存储中时重建 payload。"""
        entry = await self.runtime.cache.get(locator)
        if not entry:
            return None
        ref = str(entry.get("ref") or "")
        kind = str(entry.get("kind") or "")
        tex_format = str(entry.get("format") or "")
        if kind not in ("tex", "pdf") or not ref:
            return None
        if tex_format and tex_format not in TEX_FORMATS:
            return None
        try:
            await self.runtime.artifacts.get_bytes(ref)
        except (ArtifactNotFoundError, ArtifactIntegrityError):
            # artifact 目录被清理或损坏 → 当作缓存未命中，重新下载
            return None
        return FetchedPayload(
            kind=kind,
            ref=ref,
            channel=channel,
            locator=locator,
            tex_format=tex_format or None,
            cache_hit=True,
        )

    async def _accept_payload(
        self,
        attempt: _FetchAttempt,
        download: _Download,
    ) -> ChannelOutcome:
        """按魔数判定下载字节、落盘并写入定位符缓存。"""
        response = download.response
        locator = download.locator
        channel = download.channel
        encoding = response.header("content-encoding").strip().lower()
        if encoding and encoding != "identity":
            attempt.diagnostics.append(
                diagnostic(
                    "warning",
                    "paper_source.transport_encoded",
                    f"{locator} was returned with Content-Encoding {encoding} "
                    "despite an identity request.",
                )
            )
        kind, tex_format = sniff_payload(response.body)
        if kind == "unknown":
            attempt.diagnostics.append(
                diagnostic(
                    "error",
                    "paper_source.payload_not_recognized",
                    f"{locator} returned {len(response.body)} bytes that are "
                    "neither a TeX package nor a PDF.",
                )
            )
            return ChannelOutcome(attempted=True)
        if kind == "pdf" and channel == "arxiv_src":
            attempt.diagnostics.append(
                diagnostic(
                    "info",
                    "paper_source.pdf_only_submission",
                    f"{locator} served PDF bytes; arXiv holds no TeX source "
                    "for this submission.",
                )
            )
        ref = await self.runtime.artifacts.put_bytes(response.body)
        await self.runtime.cache.put(
            locator, {"kind": kind, "format": tex_format or "", "ref": ref}
        )
        payload = FetchedPayload(
            kind=kind,
            ref=ref,
            channel=channel,
            locator=locator,
            tex_format=tex_format,
        )
        return ChannelOutcome(payload, True)

    async def _apply_payload(
        self,
        attempt: _FetchAttempt,
        record: PaperSourceRecord,
        payload: FetchedPayload,
    ) -> None:
        """把下载结果写进记录，并持久化对应的 PaperConversionRequest。"""
        record.status = "fetched"
        record.channel = payload.channel
        record.source_locator = payload.locator
        record.cache_hit = payload.cache_hit
        if payload.kind == "tex":
            record.tex_source_ref = payload.ref
            record.tex_source_format = payload.tex_format or "auto"
        else:
            record.pdf_ref = payload.ref
        if (
            attempt.policy.prefer == "both"
            and record.tex_source_ref
            and record.identity.arxiv_id
        ):
            extra = await self._arxiv_channel(attempt, "pdf")
            if extra.payload and extra.payload.kind == "pdf":
                record.pdf_ref = extra.payload.ref
        record.metadata["source_locator"] = payload.locator
        record.metadata["source_channel"] = payload.channel
        request = conversion_request(record, attempt.policy)
        record.conversion_request_ref = await self.runtime.artifacts.put_text(
            json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        )


class PaperFetchTool(BaseTool):
    """Expose one configured source fetcher to an Agent."""

    spec = ToolSpec(
        name="paper_fetch",
        description=(
            "Resolve paper identifiers into version-pinned TeX or PDF artifacts "
            "and persisted paper_markdown conversion requests."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Artifact reference containing a PaperSourceRequest.",
                }
            },
            "required": ["request_ref"],
            "additionalProperties": False,
        },
        concurrency_safe=False,
    )

    def __init__(self, fetcher: PaperSourceFetcher) -> None:
        self.fetcher = fetcher

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        request_ref = input.get("request_ref")
        if not isinstance(request_ref, str) or not request_ref.strip():
            raise ValueError("request_ref must be a non-empty artifact reference.")
        if ctx.cancel.is_set():
            raise asyncio.CancelledError
        artifacts = self.fetcher.runtime.artifacts
        payload = await artifacts.get_text(request_ref)
        result = await self.fetcher.fetch(
            PaperSourceRequest.model_validate_json(payload), ctx.cancel
        )
        result_ref = await artifacts.put_text(result.model_dump_json())
        conversion_refs = result.conversion_request_refs()
        return ToolResult(
            data={
                "result_ref": result_ref,
                "conversion_request_refs": conversion_refs,
                "fetched": result.stats.fetched,
                "skipped": result.stats.skipped,
                "failed": result.stats.failed,
                "http_requests": result.stats.http_requests,
            },
            artifacts=[result_ref, *conversion_refs],
        )
