"""paper_source 的批量取源请求与结果模型。

上游 AcademicSurvey 只能给出"检索阶段看到的东西"：跨命名空间且不带前缀的 id、可能过时
的标题、可能失效的下载链接。本模块把这些线索收敛成显式契约，并把取源结果直接产出为
``PaperConversionRequest`` 引用，使 paper_markdown 的输入不再依赖任何隐式约定。
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from athena.core.schemas import ArtifactRef
from athena.research.paper_markdown.schemas import (
    ChunkingConfig,
    ProcessingDiagnostic,
    TexSourceFormat,
    VisualPolicy,
)

ARXIV_DOI_PREFIX = "10.48550/"
ARXIV_NEW_ID = re.compile(r"(\d{4}\.\d{4,5})(?:v(\d+))?")
ARXIV_OLD_ID = re.compile(r"([a-z-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v(\d+))?")
DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")
ARXIV_ID_MARKERS = ("/abs/", "/pdf/", "/src/", "/e-print/", "oai:arxiv.org:")
ARXIV_ID_PREFIXES = ("arxiv:", "arxiv.")

SourceHintKind = Literal["arxiv_abs", "oa_pdf", "publisher_pdf", "unknown"]
SourcePreference = Literal["tex", "pdf", "both"]
FetchStatus = Literal["fetched", "skipped", "failed"]


def normalize_doi(value: str) -> str:
    """剥掉 DOI 的 URL/前缀写法并小写化；``"https://doi.org/10.1145/X"`` → ``"10.1145/x"``。"""
    text = value.strip().lower()
    for prefix in DOI_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip().strip("/")
    return text if text.startswith("10.") else ""


def normalize_arxiv_id(value: str) -> tuple[str, int | None]:
    """把任意 arXiv 写法拆成裸 id 与版本号；``"arXiv:2501.10120v2"`` → ``("2501.10120", 2)``。

    上游（SPAR 及各检索通道）一律剥掉版本号，因此这里必须同时接受带版本与不带版本的
    输入；无法识别时返回 ``("", None)`` 而不是抛异常，让调用方以诊断形式暴露。
    """
    text = value.strip()
    for prefix in ARXIV_ID_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    lowered = text.lower()
    for marker in ARXIV_ID_MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            text = text[index + len(marker) :]
            break
    text = text.strip("/").removesuffix(".pdf")
    match = ARXIV_NEW_ID.fullmatch(text) or ARXIV_OLD_ID.fullmatch(text)
    if not match:
        return "", None
    version = match.group(2)
    return match.group(1), int(version) if version else None


class PaperIdentity(BaseModel):
    """上游能提供的全部身份线索；至少要有一个非标题标识符。

    ``paper_key()`` 用固定优先级选出 RAG 命名空间键：期刊 DOI > arXiv id > S2 > OpenAlex
    > PubMed。arXiv 自铸的 ``10.48550/`` DOI 被排除在 DOI 优先级之外 —— 它只是 arXiv id
    的另一种写法，且只有 2022 年之后的投稿才有，若参与优先级会让同一批语料按投稿年份分裂
    成两个命名空间。
    """

    arxiv_id: str | None = Field(
        default=None,
        description="Bare arXiv id without version, e.g. 2501.10120.",
    )
    doi: str | None = Field(
        default=None, description="Normalized lowercase DOI without URL prefix."
    )
    s2_paper_id: str | None = Field(
        default=None, description="Semantic Scholar SHA-1 paperId."
    )
    openalex_id: str | None = Field(
        default=None, description="OpenAlex work id, e.g. W4406604119."
    )
    pmid: str | None = Field(default=None, description="PubMed identifier.")
    pmc_id: str | None = Field(default=None, description="PubMed Central identifier.")
    title: str | None = Field(
        default=None,
        description=(
            "Upstream title used only to cross-check resolved records, "
            "never as a lookup key."
        ),
    )

    @field_validator("doi", mode="before")
    @classmethod
    def clean_doi(cls, value: object) -> object:
        """把 DOI 归一到裸 ``10.x/y`` 形式，无法识别时置空。"""
        if not isinstance(value, str):
            return value
        return normalize_doi(value) or None

    @field_validator("arxiv_id", mode="before")
    @classmethod
    def clean_arxiv_id(cls, value: object) -> object:
        """剥掉 arXiv id 上的版本号与 URL 包装，无法识别时置空。"""
        if not isinstance(value, str):
            return value
        return normalize_arxiv_id(value)[0] or None

    @model_validator(mode="after")
    def require_identifier(self) -> "PaperIdentity":
        """从 arXiv DOI 反推 arXiv id，并拒绝只有标题的条目。

        标题匹配正是上游元数据出错的主要来源（SPAR 在拿不到任何 id 时会退化为
        ``md5(title)``），所以只有标题的条目在入口就被拒绝。
        """
        if self.arxiv_id is None and self.doi and self.doi.startswith(ARXIV_DOI_PREFIX):
            derived = normalize_arxiv_id(self.doi[len(ARXIV_DOI_PREFIX) :])[0]
            if derived:
                self.arxiv_id = derived
        if not self.has_identifier():
            raise ValueError(
                "PaperIdentity requires at least one non-title identifier."
            )
        return self

    def has_identifier(self) -> bool:
        """是否存在任何可用于查询的标识符。"""
        return any(
            (
                self.arxiv_id,
                self.doi,
                self.s2_paper_id,
                self.openalex_id,
                self.pmid,
                self.pmc_id,
            )
        )

    def paper_key(self) -> str:
        """返回跨通道稳定的 RAG 命名空间键；例如 ``"arxiv:2501.10120"``。"""
        if self.doi and not self.doi.startswith(ARXIV_DOI_PREFIX):
            return f"doi:{self.doi}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.doi:
            return f"doi:{self.doi}"
        if self.s2_paper_id:
            return f"s2:{self.s2_paper_id}"
        if self.openalex_id:
            return f"openalex:{self.openalex_id}"
        if self.pmid:
            return f"pmid:{self.pmid}"
        return f"pmc:{self.pmc_id}"


class SourceHint(BaseModel):
    """检索阶段顺带拿到的下载线索，用于减少解析请求；非权威，必须校验字节。"""

    url: str = Field(description="Candidate download or landing page URL.")
    kind: SourceHintKind = Field(
        default="unknown", description="What the upstream channel believes this URL is."
    )
    channel: str = Field(
        default="", description="Retrieval channel that produced the hint."
    )
    is_open_access: bool | None = Field(
        default=None, description="Upstream open-access claim, unverified."
    )
    license: str | None = Field(
        default=None, description="Upstream license string, unverified."
    )


class PaperRef(BaseModel):
    """一篇被判定 relevant 的论文交给 paper_source 的全部输入。"""

    identity: PaperIdentity = Field(description="Identifier clues from the upstream.")
    upstream_metadata: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Retrieval-stage metadata such as title, year, and venue; "
            "treated as untrusted."
        ),
    )
    hints: list[SourceHint] = Field(
        default_factory=list, description="Candidate download URLs seen during search."
    )
    retrieval_channels: list[str] = Field(
        default_factory=list, description="Channels that surfaced this paper."
    )
    matched_queries: list[str] = Field(
        default_factory=list, description="Upstream queries this paper answered."
    )


class PaperSourcePolicy(BaseModel):
    """批次级取源策略；成本与正确性的开关都集中在这里。"""

    prefer: SourcePreference = Field(
        default="tex",
        description="Preferred representation; tex keeps equations and labels intact.",
    )
    allow_unpinned_version: bool = Field(
        default=False,
        description=(
            "Allow downloading without a resolved arXiv version; "
            "makes paper_key unstable over time."
        ),
    )
    allow_non_arxiv_channels: bool = Field(
        default=True,
        description="Allow open-access PDF hints and OpenAlex metadata for non-arXiv papers.",
    )
    allow_paid_content_api: bool = Field(
        default=False,
        description="Allow the metered OpenAlex content endpoint; requires an API key.",
    )
    fetch_license: bool = Field(
        default=False,
        description=(
            "Fetch arXiv license and version history via OAI-PMH; "
            "costs one extra request per paper."
        ),
    )
    max_papers: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Hard cap on papers attempted per request.",
    )
    stop_after_fetched: int = Field(
        default=0,
        ge=0,
        description=(
            "Stop attempting once this many papers have usable bytes; 0 attempts "
            "every accepted paper. Lets a caller ask for N papers without guessing "
            "the failure rate, which differs sharply by channel."
        ),
    )
    visual_policy: VisualPolicy = Field(
        default="required",
        description="Visual policy written into every generated PaperConversionRequest.",
    )
    chunking: ChunkingConfig = Field(
        default_factory=ChunkingConfig,
        description="Chunking config written into every generated PaperConversionRequest.",
    )


class PaperSourceRequest(BaseModel):
    """一次批量取源请求。

    批量是必需的：arXiv 的元数据端点按 id 列表计费（一次最多 60 个），逐篇调用会把请求
    预算放大两个数量级。
    """

    papers: list[PaperRef] = Field(
        min_length=1, description="Papers to fetch, in upstream ranking order."
    )
    policy: PaperSourcePolicy = Field(
        default_factory=PaperSourcePolicy, description="Batch-level fetch policy."
    )
    corpus_ref: ArtifactRef | None = Field(
        default=None,
        description="Optional upstream survey corpus artifact kept for provenance only.",
    )


class PaperSourceRecord(BaseModel):
    """一篇论文的取源结果，含可直接交给 paper_markdown 的请求引用。"""

    ref_index: int = Field(ge=0, description="Index of the paper in the input batch.")
    paper_key: str = Field(description="Namespaced RAG identity key.")
    identity: PaperIdentity = Field(
        description="Identity after enrichment from resolved records."
    )
    status: FetchStatus = Field(
        description="Whether usable source bytes were obtained."
    )
    channel: str = Field(
        default="", description="Channel that produced the stored bytes."
    )
    source_locator: str | None = Field(
        default=None,
        description="Exact provenance locator such as arxiv:2501.10120v2/src.",
    )
    version: str | None = Field(
        default=None, description="Resolved source version, e.g. v2."
    )
    version_pinned: bool = Field(
        default=False,
        description="Whether the download URL carried an explicit version.",
    )
    cache_hit: bool = Field(
        default=False, description="Whether bytes came from the locator cache."
    )
    tex_source_ref: ArtifactRef | None = Field(
        default=None, description="Stored TeX source package artifact."
    )
    tex_source_format: TexSourceFormat | None = Field(
        default=None, description="Format detected by magic-byte sniffing."
    )
    pdf_ref: ArtifactRef | None = Field(
        default=None, description="Stored PDF artifact."
    )
    license: str | None = Field(
        default=None, description="License recorded by the source channel."
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Metadata written into the generated PaperConversionRequest.",
    )
    conversion_request_ref: ArtifactRef | None = Field(
        default=None,
        description="Persisted PaperConversionRequest ready for the paper_markdown tool.",
    )
    diagnostics: list[ProcessingDiagnostic] = Field(
        default_factory=list, description="Fetch-stage quality facts for this paper."
    )


class PaperSourceStats(BaseModel):
    """一次批量取源的成本与产出摘要。"""

    requested: int = Field(ge=0, description="Papers supplied by the upstream.")
    accepted: int = Field(
        ge=0, description="Papers eligible to attempt after the max_papers cap."
    )
    attempted: int = Field(
        default=0,
        ge=0,
        description=(
            "Papers actually attempted; below accepted when stop_after_fetched hit."
        ),
    )
    fetched: int = Field(ge=0, description="Papers with usable stored source bytes.")
    skipped: int = Field(ge=0, description="Papers rejected by policy before download.")
    failed: int = Field(ge=0, description="Papers whose every channel failed.")
    cache_hits: int = Field(
        ge=0, description="Downloads served from the locator cache."
    )
    http_requests: int = Field(ge=0, description="Actual HTTP GETs issued.")
    tex_sources: int = Field(ge=0, description="Papers stored as TeX source packages.")
    pdf_sources: int = Field(ge=0, description="Papers stored as PDF only.")


class PaperSourceResult(BaseModel):
    """批量取源的持久化结果；正文字节与转换请求都在 ArtifactStore 中。"""

    schema_version: Literal["1.0"] = Field(
        default="1.0", description="PaperSourceResult schema version."
    )
    records: list[PaperSourceRecord] = Field(
        description="Per-paper results in input order."
    )
    stats: PaperSourceStats = Field(description="Batch cost and outcome summary.")
    diagnostics: list[ProcessingDiagnostic] = Field(
        default_factory=list,
        description=(
            "Batch-level facts that belong to no single paper, "
            "such as cap truncation."
        ),
    )

    def conversion_request_refs(self) -> list[ArtifactRef]:
        """按输入顺序返回可直接调用 paper_markdown 的请求引用。"""
        return [
            record.conversion_request_ref
            for record in self.records
            if record.conversion_request_ref
        ]
