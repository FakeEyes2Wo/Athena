"""PaperScout 两个动作背后的真实检索后端。

论文在评测时把检索后端统一成一次网页搜索，训练时则用本地 Milvus 快照加 ar5iv 全文。
两者本仓库都没有，因此这里用 Athena 已有的 HTTP 层接到公开学术 API：

- ``search`` → arXiv Atom 接口与 Semantic Scholar relevance search；
- ``expand`` → Semantic Scholar 的 ``/references``。参考实现是抓 ar5iv 全文、用三条正则
  从参考文献串里抠标题、再按标题反查；``/references`` 直接给出结构化的被引论文，省掉
  两层易错解析，也少一次全文下载。

所有请求都经过 ``HostRateLimiter``：它按服务分桶串行化并对 429/5xx 退避重试，这对
Semantic Scholar 是必需的——实测即使按 1 秒间隔，该端点仍会零星返回 429。
"""

import json
import urllib.parse
from typing import Protocol

from athena.research.paper_scout.schemas import ScoutPaper
from athena.research.paper_source.arxiv import ARXIV_QUERY_URL, parse_atom_feed
from athena.research.paper_source.http import HostRateLimiter
from athena.research.paper_source.schemas import normalize_arxiv_id, normalize_doi

SEMANTIC_SCHOLAR_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
SEMANTIC_SCHOLAR_REFERENCES = (
    "https://api.semanticscholar.org/graph/v1/paper/{locator}/references"
)
SEMANTIC_SCHOLAR_FIELDS = (
    "title,abstract,externalIds,year,publicationDate,citationCount,"
    "isOpenAccess,openAccessPdf"
)
SEMANTIC_SCHOLAR_INTERVAL = 1.1
ARXIV_MAX_RESULTS = 50


class BackendError(RuntimeError):
    """检索后端返回了非 2xx，或响应无法解析。"""


class SearchBackend(Protocol):
    """``search`` 动作的后端契约。"""

    name: str

    async def search(self, query: str, limit: int, cutoff: str) -> list[ScoutPaper]:
        """按相关性返回论文；``cutoff`` 非空时只返回不晚于该日期的论文。"""


class ReferenceBackend(Protocol):
    """``expand`` 动作的后端契约。"""

    name: str

    async def references(self, paper: ScoutPaper, limit: int) -> list[ScoutPaper]:
        """返回该论文引用的论文。"""


def paper_key_for(arxiv_id: str, doi: str, s2_paper_id: str, title: str) -> str:
    """按 arXiv → DOI → S2 → 标题的优先级生成去重键。

    优先用真实标识符；只有三者都缺失时才退到规范化标题，避免不同论文因标题相近被并掉。
    ``paper_key_for("2009.02040", "", "", "t")`` 返回 ``"arxiv:2009.02040"``。
    """
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    if doi:
        return f"doi:{doi}"
    if s2_paper_id:
        return f"s2:{s2_paper_id.lower()}"
    return "title:" + "".join(char for char in title if char.isalnum()).lower()


def open_access_pdf(record: dict) -> tuple[str, bool | None]:
    """从 S2 记录里取出开放获取 PDF 链接与 OA 判定。

    付费墙论文返回的是 ``{"url": "", "status": "CLOSED"}`` 而不是缺字段，因此判据
    只能是"url 非空"，不能是"字段存在"。实测三例：Wiley 与 Elsevier 的两篇给空串，
    一份 GOLD 期刊给出的链接与上一轮真正取到源时用的 URL 完全一致。

    这两个字段与既有字段在同一次请求里返回，不额外增加任何 HTTP 往返。
    """
    payload = record.get("openAccessPdf")
    url = str(payload.get("url") or "").strip() if isinstance(payload, dict) else ""
    flag = record.get("isOpenAccess")
    return url, flag if isinstance(flag, bool) else None


def within_cutoff(arxiv_id: str, published_date: str, cutoff: str) -> bool:
    """判断论文是否在发布日期上限之内；``cutoff`` 为空时恒为真。

    新式 arXiv id 的前四位就是 YYMM，比元数据里的日期更难缺失，因此优先用它；只有拿不到
    id 前缀时才回退到 ``published_date``。两者都没有时保留论文，并由调用方在统计里体现。
    """
    if not cutoff:
        return True
    prefix = arxiv_id.split(".")[0]
    if len(prefix) == 4 and prefix.isdigit():
        return prefix <= cutoff[2:4] + cutoff[5:7]
    if published_date:
        return published_date[:10] <= cutoff
    return True


class ArxivSearchBackend:
    """arXiv Atom 接口的相关性搜索。

    查询整体作为不加引号的 ``all:`` 提交。三种写法实测下来：加引号是精确短语匹配，四个
    真实查询里有两个直接返回 0–1 条；逐词 ``AND`` 在词多时同样会塌缩；不加引号能稳定拿满
    结果且不丢已有命中，因此选它。论文也要求模型只给自然语言查询、不带字段前缀和布尔
    运算符，这与不加引号的提交方式一致。
    """

    name = "arxiv"

    def __init__(self, http: HostRateLimiter) -> None:
        self.http = http

    async def search(self, query: str, limit: int, cutoff: str) -> list[ScoutPaper]:
        """执行一次 arXiv 搜索。"""
        cleaned = query.replace('"', " ").strip()
        params = {
            "search_query": f"all:{cleaned}",
            "start": 0,
            "max_results": min(limit, ARXIV_MAX_RESULTS),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        response = await self.http.get(
            f"{ARXIV_QUERY_URL}?{urllib.parse.urlencode(params)}"
        )
        if not response.ok:
            raise BackendError(f"arxiv returned HTTP {response.status}")
        papers = []
        for metadata in parse_atom_feed(response.body).values():
            published = metadata.published[:10]
            if not within_cutoff(metadata.arxiv_id, published, cutoff):
                continue
            papers.append(
                ScoutPaper(
                    paper_key=paper_key_for(
                        metadata.arxiv_id, metadata.doi or "", "", metadata.title
                    ),
                    arxiv_id=metadata.arxiv_id,
                    doi=normalize_doi(metadata.doi or ""),
                    title=metadata.title,
                    abstract=metadata.abstract,
                    year=int(published[:4]) if published[:4].isdigit() else None,
                    published_date=published,
                    source="search",
                    origin=query,
                    channel=self.name,
                )
            )
        return papers[:limit]


class SemanticScholarBackend:
    """Semantic Scholar Graph API：relevance search 与单跳引用。

    ``api_key`` 只由调用方注入，模块不读环境变量。无 key 时该端点几乎必然 429，因此
    生产组合根应显式提供凭据或接受该通道缺失。
    """

    name = "semantic_scholar"

    def __init__(self, http: HostRateLimiter, api_key: str | None = None) -> None:
        self.http = http
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    async def search(self, query: str, limit: int, cutoff: str) -> list[ScoutPaper]:
        """执行一次 Semantic Scholar 相关性搜索。"""
        params = {"query": query, "limit": limit, "fields": SEMANTIC_SCHOLAR_FIELDS}
        response = await self.http.get(
            f"{SEMANTIC_SCHOLAR_SEARCH}?{urllib.parse.urlencode(params)}",
            self._headers(),
        )
        if not response.ok:
            raise BackendError(f"semantic_scholar returned HTTP {response.status}")
        payload = parse_json(response.body)
        records = payload.get("data") if isinstance(payload, dict) else None
        return self._papers(records, "search", query, cutoff, limit)

    async def references(self, paper: ScoutPaper, limit: int) -> list[ScoutPaper]:
        """返回该论文引用的论文；缺少可用定位符时返回空列表。"""
        locator = self._locator(paper)
        if not locator:
            return []
        params = {"limit": limit, "fields": SEMANTIC_SCHOLAR_FIELDS}
        url = SEMANTIC_SCHOLAR_REFERENCES.format(
            locator=urllib.parse.quote(locator, safe=":")
        )
        response = await self.http.get(
            f"{url}?{urllib.parse.urlencode(params)}", self._headers()
        )
        if not response.ok:
            raise BackendError(f"semantic_scholar returned HTTP {response.status}")
        payload = parse_json(response.body)
        rows = payload.get("data") if isinstance(payload, dict) else None
        cited = [
            row.get("citedPaper")
            for row in rows or []
            if isinstance(row, dict) and isinstance(row.get("citedPaper"), dict)
        ]
        return self._papers(cited, "expand", paper.title, "", limit)

    def _locator(self, paper: ScoutPaper) -> str:
        if paper.arxiv_id:
            return f"arXiv:{paper.arxiv_id}"
        if paper.s2_paper_id:
            return paper.s2_paper_id
        return f"DOI:{paper.doi}" if paper.doi else ""

    def _papers(
        self, records: object, source: str, origin: str, cutoff: str, limit: int
    ) -> list[ScoutPaper]:
        if not isinstance(records, list):
            return []
        papers = []
        for record in records[:limit]:
            paper = self._paper(record, source, origin)
            if paper is None:
                continue
            if not within_cutoff(paper.arxiv_id, paper.published_date, cutoff):
                continue
            papers.append(paper)
        return papers

    def _paper(self, record: object, source: str, origin: str) -> ScoutPaper | None:
        if not isinstance(record, dict):
            return None
        title = str(record.get("title") or "").strip()
        if not title:
            return None
        external = record.get("externalIds")
        external = external if isinstance(external, dict) else {}
        arxiv_id, _ = normalize_arxiv_id(str(external.get("ArXiv") or ""))
        doi = normalize_doi(str(external.get("DOI") or ""))
        s2_id = str(record.get("paperId") or "")
        published = str(record.get("publicationDate") or "")
        year = record.get("year")
        citations = record.get("citationCount")
        pdf_url, is_open_access = open_access_pdf(record)
        return ScoutPaper(
            paper_key=paper_key_for(arxiv_id, doi, s2_id, title),
            arxiv_id=arxiv_id,
            doi=doi,
            s2_paper_id=s2_id,
            title=title,
            abstract=str(record.get("abstract") or ""),
            year=year if isinstance(year, int) else None,
            published_date=published,
            citation_count=citations if isinstance(citations, int) else None,
            open_access_pdf=pdf_url,
            is_open_access=is_open_access,
            source=source,  # type: ignore[arg-type]
            origin=origin,
            channel=self.name,
        )


def parse_json(body: bytes) -> object:
    """解析响应体；非 JSON 时抛 ``BackendError`` 而不是返回空结果。"""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        # 端点返回了 HTML 错误页或截断的 JSON → 当作后端失败而非零命中
        raise BackendError(f"unparsable response body: {error}") from error


def build_default_backends(
    http: HostRateLimiter | None = None,
    *,
    semantic_scholar_api_key: str | None = None,
    contact_email: str | None = None,
) -> tuple[list[SearchBackend], ReferenceBackend | None]:
    """构造共享限流器的默认后端；凭据只由调用方显式注入。"""
    limiter = http or HostRateLimiter(
        bucket_intervals={"api.semanticscholar.org": SEMANTIC_SCHOLAR_INTERVAL},
        contact_email=contact_email,
    )
    semantic_scholar = SemanticScholarBackend(limiter, semantic_scholar_api_key)
    return [ArxivSearchBackend(limiter), semantic_scholar], semantic_scholar
