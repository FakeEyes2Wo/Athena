"""无 key 的 DuckDuckGo 网页搜索工具（单类，http 可注入便于测试）。"""

import html
import re
import urllib.parse

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec
from athena.research.paper_source.http import HostRateLimiter, UrllibTransport

DDG_HTML = "https://html.duckduckgo.com/html/"
# 非浏览器 UA 会触发 DDG 反爬 challenge，必须用浏览器 UA。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_RESULT_LINK = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _decode_url(href: str) -> str:
    target = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get(
        "uddg", [href]
    )[0]
    return urllib.parse.unquote(target)


class WebSearchTool(BaseTool):
    spec = ToolSpec(
        name="web_search",
        description=(
            "Search the public web (DuckDuckGo) for a natural-language query. "
            "Returns up to max_results results, each with title, url and snippet. "
            "Use it to look up current methods, libraries, documentation or facts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, http: HostRateLimiter | None = None) -> None:
        self.http = http or HostRateLimiter(
            transport=UrllibTransport(),
            bucket_intervals={"html.duckduckgo.com": 1.0},
        )

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        url = f"{DDG_HTML}?{urllib.parse.urlencode({'q': query})}"
        response = await self.http.get(url, {"User-Agent": BROWSER_UA})
        if not response.ok:
            raise RuntimeError(f"duckduckgo returned HTTP {response.status}")
        text = response.body.decode("utf-8", errors="replace")
        links = _RESULT_LINK.findall(text)
        snippets = _SNIPPET.findall(text)
        results = []
        for i, (href, title) in enumerate(links[:limit]):
            results.append(
                {
                    "title": _clean(title),
                    "url": _decode_url(href),
                    "snippet": _clean(snippets[i]) if i < len(snippets) else "",
                }
            )
        return results

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        limit = max(1, min(int(input.get("max_results") or 8), 20))
        results = await self.search(query.strip(), limit)
        return ToolResult(data={"results": results, "count": len(results)})
