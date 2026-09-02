"""DuckDuckGo 网页搜索与抓取工具（无 key；http 可注入便于测试）。

语义参考 Codex ``codex-rs/ext/web-search`` 的 ``web/run``：批量查询、domains/recency
过滤、``response_length``、结果 ref_id，以及 open/find 两步式页面操作。
"""

import html
import re
import urllib.parse

from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.literature.paper_source.http import (
    HostRateLimiter,
    UrllibTransport,
)

DDG_HTML = "https://html.duckduckgo.com/html/"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
RESPONSE_LIMITS = {"short": 3, "medium": 8, "long": 15}
MAX_QUERIES = 4

_RESULT_LINK = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_SCRIPT_STYLE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _decode_url(href: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    return urllib.parse.unquote(query.get("uddg", [href])[0])


def _compose_query(query: str, domains: list[str] | None) -> str:
    """domains 过滤转为 DDG ``site:`` 查询词。"""
    sites = " OR ".join(f"site:{d}" for d in domains or [])
    return f"{query} ({sites})" if sites else query


def _recency_filter(days: int) -> str:
    """天数映射到 DDG df 参数（d/w/m/y）。"""
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 30:
        return "m"
    return "y"


def extract_page_text(page_html: str, *, url: str, max_chars: int) -> dict:
    """提取标题与可读正文。"""
    match = _TITLE.search(page_html)
    title = html.unescape(_clean(match.group(1))) if match else ""
    body = _WHITESPACE.sub(
        " ", html.unescape(_TAG.sub(" ", _SCRIPT_STYLE.sub(" ", page_html)))
    ).strip()
    text = "\n".join(part for part in (title, body) if part)
    return {
        "url": url,
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def find_in_page(
    text: str, pattern: str, *, context_chars: int = 160, max_matches: int = 12
) -> dict:
    """大小写无关查找；返回 capped 摘录与真实命中总数（Codex find）。"""
    if not pattern:
        return {"matches": [], "total": 0}
    needle, haystack = pattern.lower(), text.lower()
    matches: list[dict] = []
    total, cursor = 0, 0
    while True:
        position = haystack.find(needle, cursor)
        if position < 0:
            break
        total += 1
        if len(matches) < max_matches:
            start, end = max(0, position - context_chars), min(
                len(text), position + len(pattern) + context_chars
            )
            excerpt = " ".join(text[start:end].split())
            if start:
                excerpt = f"…{excerpt}"
            if end < len(text):
                excerpt = f"{excerpt}…"
            matches.append({"offset": position, "excerpt": excerpt})
        cursor = position + max(1, len(needle))
    return {"matches": matches, "total": total}


class WebSession:
    """web_search/web_fetch 共享的 ref_id 会话（等价 Codex turn0search/turn0fetch）。"""

    def __init__(self, http: HostRateLimiter | None = None) -> None:
        self.http = http or HostRateLimiter(
            transport=UrllibTransport(),
            bucket_intervals={"html.duckduckgo.com": 1.0},
        )
        self._urls: dict[str, str] = {}
        self._pages: dict[str, dict] = {}
        self._searches = 0
        self._fetches = 0

    def search_refs(self, count: int) -> list[str]:
        call, self._searches = self._searches, self._searches + 1
        return [f"turn0search{call}-{i}" for i in range(count)]

    def fetch_ref(self) -> str:
        ref_id, self._fetches = f"turn0fetch{self._fetches}", self._fetches + 1
        return ref_id

    def remember_url(self, ref_id: str, url: str) -> None:
        self._urls[ref_id] = url

    def remember_page(self, ref_id: str, page: dict) -> None:
        self._urls[ref_id] = page["url"]
        self._pages[ref_id] = page

    def page(self, ref_id: str) -> dict | None:
        return self._pages.get(ref_id)

    def url(self, ref_id: str) -> str | None:
        return self._urls.get(ref_id)


class _WebTool(BaseTool):
    """共享 WebSession 的公共构造。"""

    def __init__(
        self,
        http: HostRateLimiter | None = None,
        session: WebSession | None = None,
    ) -> None:
        self.session = session or WebSession(http)
        self.http = http or self.session.http


class WebSearchTool(_WebTool):
    spec = ToolSpec(
        name="web_search",
        description=(
            "Search the public web (DuckDuckGo). Use 'search_query' to run up to 4 "
            "queries in one call; each query supports 'domains' and 'recency' (days). "
            "Use 'response_length' to control result count (short=3, medium=8, "
            "long=15); more than 3 queries requires medium or long. Results carry a "
            "ref_id for web_fetch; never expose ref_ids to the user. The legacy "
            "'query' form is for a single simple search."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "search_query": {
                    "type": "array",
                    "maxItems": MAX_QUERIES,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "q": {"type": "string", "minLength": 1},
                            "domains": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "recency": {"type": "integer", "minimum": 1},
                        },
                        "required": ["q"],
                    },
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                },
                "response_length": {
                    "type": "string",
                    "enum": ["short", "medium", "long"],
                },
                "domains": {"type": "array", "items": {"type": "string"}},
                "recency": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
    )

    def _queries(self, input: dict) -> list[dict]:
        batch = input.get("search_query")
        if isinstance(batch, list) and batch:
            if len(batch) > MAX_QUERIES:
                raise ValueError(
                    f"search_query must have at most {MAX_QUERIES} queries"
                )
            if len(batch) > 3 and input.get("response_length") == "short":
                raise ValueError(
                    "more than 3 search_query entries requires response_length medium or long"
                )
            return batch
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("provide a non-empty 'query' or 'search_query'")
        return [
            {
                "q": query.strip(),
                "domains": input.get("domains"),
                "recency": input.get("recency"),
            }
        ]

    @staticmethod
    def _limit(input: dict) -> int:
        return RESPONSE_LIMITS.get(
            input.get("response_length"),
            max(1, min(int(input.get("max_results") or 8), 20)),
        )

    async def search(
        self,
        query: str,
        limit: int = 8,
        *,
        domains: list[str] | None = None,
        recency: int | None = None,
    ) -> list[dict]:
        params: dict[str, str] = {"q": _compose_query(query, domains)}
        if recency is not None:
            params["df"] = _recency_filter(int(recency))
        response = await self.http.get(
            f"{DDG_HTML}?{urllib.parse.urlencode(params)}", {"User-Agent": BROWSER_UA}
        )
        if not response.ok:
            raise RuntimeError(f"duckduckgo returned HTTP {response.status}")
        text = response.body.decode("utf-8", errors="replace")
        links, snippets = _RESULT_LINK.findall(text), _SNIPPET.findall(text)
        refs = self.session.search_refs(min(len(links), limit))
        results: list[dict] = []
        for index, (href, title) in enumerate(links[:limit]):
            result = {
                "ref_id": refs[index],
                "title": _clean(title),
                "url": _decode_url(href),
                "snippet": _clean(snippets[index]) if index < len(snippets) else "",
                "query": _compose_query(query, domains),
            }
            self.session.remember_url(result["ref_id"], result["url"])
            results.append(result)
        return results

    async def search_many(self, queries: list[dict], limit: int) -> list[dict]:
        if len(queries) > MAX_QUERIES:
            raise ValueError(f"search_query must have at most {MAX_QUERIES} queries")
        per_query = max(2, limit // max(1, len(queries)))
        seen: set[str] = set()
        combined: list[dict] = []
        for item in queries:
            query = item.get("q")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("each search_query entry requires a non-empty 'q'")
            for result in await self.search(
                query,
                per_query,
                domains=item.get("domains"),
                recency=item.get("recency"),
            ):
                if result["url"] not in seen:
                    seen.add(result["url"])
                    combined.append(result)
        return combined[:limit]

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        queries, limit = self._queries(input), self._limit(input)
        if len(queries) == 1:
            spec = queries[0]
            results = await self.search(
                spec["q"],
                limit,
                domains=spec.get("domains"),
                recency=spec.get("recency"),
            )
        else:
            results = await self.search_many(queries, limit)
        return ToolResult(data={"results": results, "count": len(results)})


class WebFetchTool(_WebTool):
    spec = ToolSpec(
        name="web_fetch",
        description=(
            "Open a web page by 'url' or by a 'ref_id' returned from web_search. "
            "Add 'pattern' to return short excerpts around each match instead of the "
            "whole page (Codex find). Text is truncated to max_chars (default 12000)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "ref_id": {"type": "string", "minLength": 1},
                "pattern": {"type": "string", "minLength": 1},
                "max_chars": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 50000,
                    "default": 12000,
                },
            },
            "additionalProperties": False,
        },
    )

    async def fetch(self, url: str, max_chars: int = 12000) -> dict:
        response = await self.http.get(url, {"User-Agent": BROWSER_UA})
        if not response.ok:
            raise RuntimeError(f"web_fetch returned HTTP {response.status}")
        return extract_page_text(
            response.body.decode("utf-8", errors="replace"),
            url=url,
            max_chars=max_chars,
        )

    async def fetch_ref(self, ref_id: str, max_chars: int = 12000) -> dict:
        cached = self.session.page(ref_id)
        if cached is not None:
            return dict(cached)
        url = self.session.url(ref_id)
        if url is None:
            raise ValueError(f"unknown web ref_id: {ref_id}")
        page = await self.fetch(url, max_chars)
        page["ref_id"] = ref_id
        self.session.remember_page(ref_id, page)
        return page

    @staticmethod
    def _matches(page: dict, pattern: str) -> dict:
        found = find_in_page(page["text"], pattern)
        return {
            "ref_id": page.get("ref_id"),
            "url": page["url"],
            "title": page["title"],
            "pattern": pattern,
            "matches": found["matches"],
            "count": found["total"],
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        max_chars = max(500, min(int(input.get("max_chars") or 12000), 50000))
        pattern = (input.get("pattern") or "").strip() or None
        ref_id = (input.get("ref_id") or "").strip()
        if ref_id:
            page = await self.fetch_ref(ref_id, max_chars)
        else:
            url = (input.get("url") or "").strip()
            if not url:
                raise ValueError("provide a non-empty 'url' or 'ref_id'")
            page = await self.fetch(url, max_chars)
            page["ref_id"] = self.session.fetch_ref()
            self.session.remember_page(page["ref_id"], page)
        return ToolResult(data=self._matches(page, pattern) if pattern else page)


def build_web_tools() -> ToolRegistry:
    """构造共享一个会话的网页搜索与抓取工具。"""
    session = WebSession()
    registry = ToolRegistry()
    registry.register(WebSearchTool(session=session))
    registry.register(WebFetchTool(session=session))
    return registry
