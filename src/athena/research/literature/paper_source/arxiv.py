"""arXiv 元数据解析与源码/PDF 下载。

三个主机各有分工，混用会白白浪费请求预算：

- ``export.arxiv.org/api/query?id_list=`` 一次最多解析 60 个 id 的最新版本号与元数据，
  是版本固定的唯一批量来源；
- ``oaipmh.arxiv.org`` 提供完整版本历史与许可证，但 ``GetRecord`` 只能一次一篇，因此按
  策略开关按需调用；
- ``arxiv.org/src`` 与 ``arxiv.org/pdf`` 是唯一按篇计费的下载调用，直连可省掉
  ``/e-print`` 的 301 跳转。

版本号必须先由前者解析、再拼进后者。不带版本下载会让同一 ``paper_key`` 在不同时间对应
不同正文，而 ``PaperContent`` 的 chunk 主键是按 ``paper_key`` 命名空间生成的。
"""

import urllib.parse
from dataclasses import dataclass, field
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from athena.research.literature.paper_source.http import HostRateLimiter, HttpResponse
from athena.research.literature.paper_source.schemas import (
    normalize_arxiv_id,
    normalize_doi,
)

ARXIV_QUERY_URL = "https://export.arxiv.org/api/query"
ARXIV_OAI_URL = "https://oaipmh.arxiv.org/oai"
ARXIV_SRC_URL = "https://arxiv.org/src/{locator}"
ARXIV_PDF_URL = "https://arxiv.org/pdf/{locator}"
ARXIV_ID_BATCH = 60
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_ATOM_NS = "{http://arxiv.org/schemas/atom}"
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
RAW_NS = "{http://arxiv.org/OAI/arXivRaw/}"


class ArxivMetadata(BaseModel):
    """一篇 arXiv 论文的解析结果；``latest_version`` 决定下载时钉哪个版本。"""

    arxiv_id: str = Field(description="Bare arXiv id without version.")
    latest_version: int | None = Field(
        default=None, description="Highest version number announced by arXiv."
    )
    title: str = Field(default="", description="Whitespace-normalized title.")
    authors: list[str] = Field(
        default_factory=list, description="Author display names."
    )
    abstract: str = Field(default="", description="Abstract text.")
    categories: list[str] = Field(
        default_factory=list, description="arXiv category terms."
    )
    published: str = Field(default="", description="First submission timestamp.")
    updated: str = Field(default="", description="Latest version timestamp.")
    doi: str | None = Field(
        default=None, description="Publisher DOI when the authors registered one."
    )
    journal_ref: str | None = Field(
        default=None, description="Journal reference when published."
    )
    license: str | None = Field(
        default=None, description="License URL; only OAI-PMH exposes this."
    )
    versions: list[str] = Field(
        default_factory=list, description="Version labels such as v1, v2."
    )


@dataclass(slots=True)
class ArxivResolution:
    """一次批量版本解析的结果与失败原因。"""

    metadata: dict[str, ArxivMetadata] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class ArxivError(ValueError):
    """arXiv 返回的响应无法解析成元数据。"""


def collapse_whitespace(value: str | None) -> str:
    """把 XML 里的换行与缩进压成单空格；arXiv 的 title 常带硬换行。"""
    return " ".join((value or "").split())


def versioned_locator(arxiv_id: str, version: int | None) -> str:
    """拼出下载定位符；``("2501.10120", 2)`` → ``"2501.10120v2"``。"""
    return f"{arxiv_id}v{version}" if version else arxiv_id


def parse_atom_entry(entry: ElementTree.Element) -> ArxivMetadata | None:
    """把一个 Atom entry 转成元数据；arXiv 的错误条目返回 None。"""
    raw_id = collapse_whitespace(entry.findtext(ATOM_NS + "id"))
    if "api/errors" in raw_id:
        return None
    arxiv_id, version = normalize_arxiv_id(raw_id)
    if not arxiv_id:
        return None
    doi = collapse_whitespace(entry.findtext(ARXIV_ATOM_NS + "doi"))
    return ArxivMetadata(
        arxiv_id=arxiv_id,
        latest_version=version,
        title=collapse_whitespace(entry.findtext(ATOM_NS + "title")),
        authors=[
            collapse_whitespace(author.findtext(ATOM_NS + "name"))
            for author in entry.findall(ATOM_NS + "author")
        ],
        abstract=collapse_whitespace(entry.findtext(ATOM_NS + "summary")),
        categories=[
            term
            for term in (
                node.get("term") for node in entry.findall(ATOM_NS + "category")
            )
            if term
        ],
        published=collapse_whitespace(entry.findtext(ATOM_NS + "published")),
        updated=collapse_whitespace(entry.findtext(ATOM_NS + "updated")),
        doi=normalize_doi(doi) or None,
        journal_ref=collapse_whitespace(entry.findtext(ARXIV_ATOM_NS + "journal_ref"))
        or None,
        versions=[f"v{version}"] if version else [],
    )


def parse_atom_feed(payload: bytes) -> dict[str, ArxivMetadata]:
    """解析 ``api/query`` 的 Atom 响应，按裸 id 索引元数据。

    只解析来自固定 arXiv 主机、经 TLS 校验的响应，因此使用标准库 ElementTree 而不额外
    引入 XML 加固依赖。
    """
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        # arXiv 在过载时会返回 HTML 错误页而非 Atom → 视为解析失败
        raise ArxivError(f"arXiv Atom response is not valid XML: {error}") from error
    resolved: dict[str, ArxivMetadata] = {}
    for entry in root.findall(ATOM_NS + "entry"):
        metadata = parse_atom_entry(entry)
        if metadata:
            resolved[metadata.arxiv_id] = metadata
    return resolved


def parse_raw_record(payload: bytes) -> ArxivMetadata | None:
    """解析 OAI-PMH ``arXivRaw`` 响应，取出完整版本历史与许可证。"""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        # OAI 端点返回非 XML（限流页 / 网关错误）→ 视为解析失败
        raise ArxivError(f"arXiv OAI response is not valid XML: {error}") from error
    if root.find(OAI_NS + "error") is not None:
        return None
    raw = root.find(
        f"{OAI_NS}GetRecord/{OAI_NS}record/{OAI_NS}metadata/{RAW_NS}arXivRaw"
    )
    if raw is None:
        return None
    arxiv_id, _ = normalize_arxiv_id(collapse_whitespace(raw.findtext(RAW_NS + "id")))
    if not arxiv_id:
        return None
    versions = [
        label
        for label in (node.get("version") for node in raw.findall(RAW_NS + "version"))
        if label
    ]
    latest = max((int(label.lstrip("v")) for label in versions), default=None)
    return ArxivMetadata(
        arxiv_id=arxiv_id,
        latest_version=latest,
        title=collapse_whitespace(raw.findtext(RAW_NS + "title")),
        authors=[collapse_whitespace(raw.findtext(RAW_NS + "authors"))],
        abstract=collapse_whitespace(raw.findtext(RAW_NS + "abstract")),
        categories=collapse_whitespace(raw.findtext(RAW_NS + "categories")).split(),
        doi=normalize_doi(collapse_whitespace(raw.findtext(RAW_NS + "doi"))) or None,
        journal_ref=collapse_whitespace(raw.findtext(RAW_NS + "journal-ref")) or None,
        license=collapse_whitespace(raw.findtext(RAW_NS + "license")) or None,
        versions=versions,
    )


class ArxivClient:
    """arXiv 的三端点访问器；所有节奏控制都由注入的 ``HostRateLimiter`` 负责。"""

    def __init__(self, http: HostRateLimiter) -> None:
        self._http = http

    async def resolve_batch(self, arxiv_ids: list[str]) -> ArxivResolution:
        """按 60 个一批解析最新版本与元数据；未收录的 id 不会出现在返回值里。

        例如 ``await client.resolve_batch(["2501.10120"])`` 得到的
        ``resolution.metadata["2501.10120"].latest_version`` 就是下载时要钉的版本。
        """
        resolution = ArxivResolution()
        for start in range(0, len(arxiv_ids), ARXIV_ID_BATCH):
            batch = arxiv_ids[start : start + ARXIV_ID_BATCH]
            await self._resolve_one_batch(batch, resolution)
        return resolution

    async def _resolve_one_batch(
        self, batch: list[str], resolution: ArxivResolution
    ) -> None:
        """解析一个不超过 60 个 id 的批次，失败原因累积到 resolution.errors。"""
        query = urllib.parse.urlencode(
            {"id_list": ",".join(batch), "max_results": len(batch)}
        )
        response = await self._http.get(f"{ARXIV_QUERY_URL}?{query}")
        if not response.ok:
            resolution.errors.append(
                f"arXiv metadata query returned HTTP {response.status} for {len(batch)} ids."
            )
            return
        try:
            resolution.metadata.update(parse_atom_feed(response.body))
        except ArxivError as error:
            # Atom 响应损坏 → 该批次全部视为未解析，由上层按策略跳过或放宽版本要求
            resolution.errors.append(str(error))

    async def fetch_raw_record(self, arxiv_id: str) -> ArxivMetadata | None:
        """通过 OAI-PMH 取完整版本历史与许可证；每篇 1 次请求，id 不存在返回 None。"""
        query = urllib.parse.urlencode(
            {
                "verb": "GetRecord",
                "identifier": f"oai:arXiv.org:{arxiv_id}",
                "metadataPrefix": "arXivRaw",
            }
        )
        response = await self._http.get(f"{ARXIV_OAI_URL}?{query}")
        if not response.ok:
            return None
        try:
            return parse_raw_record(response.body)
        except ArxivError:
            # OAI 响应损坏 → 许可证是可选增强，缺失不应中断取源
            return None

    async def fetch_source(self, arxiv_id: str, version: int | None) -> HttpResponse:
        """下载 ``/src/{id}v{n}`` 的原始字节；不请求传输压缩以保证字节与服务端一致。"""
        locator = versioned_locator(arxiv_id, version)
        return await self._http.get(ARXIV_SRC_URL.format(locator=locator))

    async def fetch_pdf(self, arxiv_id: str, version: int | None) -> HttpResponse:
        """下载 ``/pdf/{id}v{n}``，仅在源码不可用或策略要求 PDF 时调用。"""
        locator = versioned_locator(arxiv_id, version)
        return await self._http.get(ARXIV_PDF_URL.format(locator=locator))
