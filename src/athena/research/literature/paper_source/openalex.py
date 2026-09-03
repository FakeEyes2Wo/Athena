"""OpenAlex 免费元数据与（默认关闭的）计费内容端点。

只有 ``api.openalex.org`` 的 singleton 查询是免费的：匿名访问按 ``mailto`` 进入礼貌池，
每日额度以美元计（匿名 0.10 美元/天，注册免费 key 后 1 美元/天）。``content.openalex.org``
的 PDF 与 GROBID XML 按 credit 计费且必须带 API key，因此这里要求
``PaperSourcePolicy.allow_paid_content_api`` 与显式 key 同时具备才会发起请求。

OpenAlex 的记录只作为线索使用：实测同一篇论文的 ``best_oa_location`` 会指向第三方镜像，
年份、类型与 DOI 也出现过与出版方记录不一致的情况，且缓存内容不带抓取时间与校验和。权威
版本与正文字节仍然来自 arXiv。
"""

import json
import urllib.parse

from pydantic import BaseModel, Field

from athena.research.literature.paper_source.http import HostRateLimiter, HttpResponse
from athena.research.literature.paper_source.schemas import (
    normalize_arxiv_id,
    normalize_doi,
)

OPENALEX_WORK_URL = "https://api.openalex.org/works/{locator}"
OPENALEX_CONTENT_URL = "https://content.openalex.org/works/{work_id}.pdf"


class OpenAlexWork(BaseModel):
    """OpenAlex work 中与取源相关的子集。"""

    openalex_id: str = Field(
        default="", description="Bare work id such as W4406604119."
    )
    doi: str | None = Field(default=None, description="Normalized DOI when present.")
    title: str = Field(default="", description="Display name reported by OpenAlex.")
    publication_year: int | None = Field(
        default=None, description="Publication year reported by OpenAlex."
    )
    cited_by_count: int = Field(
        default=0, ge=0, description="OpenAlex cited_by_count at fetch time."
    )
    is_oa: bool = Field(default=False, description="Open-access flag, unverified.")
    oa_status: str = Field(default="", description="OpenAlex oa_status bucket.")
    pdf_url: str | None = Field(
        default=None,
        description="Best open-access PDF URL, possibly a third-party mirror.",
    )
    license: str | None = Field(
        default=None, description="License string on the best open-access location."
    )
    arxiv_id: str | None = Field(
        default=None, description="arXiv id recovered from DOI or open-access URLs."
    )


def bare_openalex_id(value: str) -> str:
    """从 ``https://openalex.org/W123`` 取出 ``W123``。"""
    return value.strip().rstrip("/").rsplit("/", 1)[-1]


def recover_arxiv_id(payload: dict) -> str:
    """从 DOI 与开放获取链接里回收 arXiv id，找不到返回空串。

    OpenAlex 不保证提供 ``ids.arxiv``，但 arXiv 论文的 DOI 或 ``oa_url`` 几乎总能还原
    出 id，这比用标题去 arXiv 反查安全得多。
    """
    best = payload.get("best_oa_location") or {}
    candidates = [
        str(payload.get("doi") or ""),
        str((payload.get("open_access") or {}).get("oa_url") or ""),
        str(best.get("pdf_url") or ""),
        str(best.get("landing_page_url") or ""),
    ]
    for candidate in candidates:
        arxiv_id, _ = normalize_arxiv_id(candidate.rsplit("/", 1)[-1])
        if arxiv_id:
            return arxiv_id
    return ""


def parse_work(payload: dict) -> OpenAlexWork:
    """把 OpenAlex work JSON 收敛成取源需要的字段。"""
    best = payload.get("best_oa_location") or {}
    access = payload.get("open_access") or {}
    year = payload.get("publication_year")
    count = payload.get("cited_by_count")
    return OpenAlexWork(
        openalex_id=bare_openalex_id(str(payload.get("id") or "")),
        doi=normalize_doi(str(payload.get("doi") or "")) or None,
        title=str(payload.get("display_name") or payload.get("title") or ""),
        publication_year=year if isinstance(year, int) else None,
        cited_by_count=count if isinstance(count, int) and count >= 0 else 0,
        is_oa=bool(access.get("is_oa")),
        oa_status=str(access.get("oa_status") or ""),
        pdf_url=str(best.get("pdf_url") or access.get("oa_url") or "") or None,
        license=str(best.get("license") or "") or None,
        arxiv_id=recover_arxiv_id(payload) or None,
    )


class OpenAlexClient:
    """OpenAlex 访问器；免费元数据与计费内容端点分开暴露，避免误触发计费。"""

    def __init__(
        self,
        http: HostRateLimiter,
        contact_email: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._http = http
        self._contact_email = contact_email
        self._api_key = api_key

    @property
    def has_api_key(self) -> bool:
        """是否具备访问计费内容端点的凭据。"""
        return bool(self._api_key)

    async def fetch_work(self, locator: str) -> OpenAlexWork | None:
        """按 ``W...`` / ``doi:10.x/y`` / ``pmid:123`` 取一条 work；未收录返回 None。

        例如 ``await client.fetch_work("doi:10.1145/3654777")`` 返回的 ``pdf_url`` 只是
        候选线索，仍需下载后按魔数确认。
        """
        query = (
            f"?{urllib.parse.urlencode({'mailto': self._contact_email})}"
            if self._contact_email
            else ""
        )
        url = (
            OPENALEX_WORK_URL.format(locator=urllib.parse.quote(locator, safe=":/"))
            + query
        )
        response = await self._http.get(url)
        if not response.ok:
            return None
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError:
            # OpenAlex 限流或网关错误时返回 HTML → 视为未收录，由上层记录诊断
            return None
        return parse_work(payload) if isinstance(payload, dict) else None

    async def fetch_content_pdf(self, work_id: str) -> HttpResponse | None:
        """计费内容端点；没有 API key 时返回 None 而不是发出必然失败的请求。"""
        if not self._api_key:
            return None
        query = urllib.parse.urlencode({"api_key": self._api_key})
        url = (
            f"{OPENALEX_CONTENT_URL.format(work_id=bare_openalex_id(work_id))}?{query}"
        )
        return await self._http.get(url)
