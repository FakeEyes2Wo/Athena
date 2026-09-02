"""DuckDuckGo 网页搜索与网页抓取的解析单元测试（假 HTTP，不触网）。"""

import urllib.parse

import pytest

from athena.research.literature.paper_source.http import HttpResponse
from athena.retrieval.web_search import (
    WebFetchTool,
    WebSearchTool,
    WebSession,
    build_web_tools,
    extract_page_text,
    find_in_page,
)

_HTML = """<!doctype html><html>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example Title</a>
<a class="result__snippet" href="https://example.com/page"><b>Example</b> snippet text</a>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org&amp;rut=def">Second</a>
<a class="result__snippet" href="https://example.org">Second snippet</a>
</html>"""

_PAGE_HTML = """<!doctype html><html><head><title>Readable page</title>
<style>.x{color:red}</style>
<script>console.log('drop me')</script></head>
<body><h1>Hello</h1><p>This is <b>readable</b> text &amp; more.</p></body></html>"""


class _FakeHttp:
    def __init__(self, body: str = _HTML) -> None:
        self.body = body.encode()
        self.urls: list[str] = []

    async def get(self, url: str, headers: dict | None = None) -> HttpResponse:
        self.urls.append(url)
        return HttpResponse(status=200, url=url, body=self.body, headers={})


async def test_search_parses_title_url_and_snippet() -> None:
    results = await WebSearchTool(http=_FakeHttp()).search("test", limit=10)
    assert len(results) == 2
    assert results[0] == {
        "ref_id": "turn0search0-0",
        "title": "Example Title",
        "url": "https://example.com/page",
        "snippet": "Example snippet text",
        "query": "test",
    }
    assert results[1]["title"] == "Second"
    assert results[1]["url"] == "https://example.org"


async def test_search_many_composes_domain_and_recency_filters_and_dedups() -> None:
    http = _FakeHttp()
    tool = WebSearchTool(http=http)
    results = await tool.search_many(
        [
            {"q": "ml model", "domains": ["arxiv.org"], "recency": 7},
            {"q": "ml model", "domains": ["arxiv.org", "openreview.net"]},
        ],
        limit=8,
    )
    assert len(results) == 2  # same fake result set → URL 去重
    decoded = [urllib.parse.unquote(url) for url in http.urls]
    assert any("site:arxiv.org" in url for url in decoded)
    assert any("df=w" in url for url in decoded)


async def test_search_many_rejects_more_than_four_queries() -> None:
    tool = WebSearchTool(http=_FakeHttp())
    with pytest.raises(ValueError, match="at most 4"):
        await tool.search_many([{"q": str(i)} for i in range(5)], limit=8)


def test_extract_page_text_strips_scripts_styles_and_tags() -> None:
    data = extract_page_text(_PAGE_HTML, url="https://example.com", max_chars=2000)

    assert data["title"] == "Readable page"
    assert "console.log" not in data["text"]
    assert "Hello" in data["text"]
    assert "readable" in data["text"]
    assert data["truncated"] is False


async def test_web_fetch_returns_title_and_text() -> None:
    tool = WebFetchTool(http=_FakeHttp(_PAGE_HTML))

    result = await tool.execute({"url": "https://example.com"}, ctx=None)

    assert result.success
    assert result.data["title"] == "Readable page"
    assert "readable" in result.data["text"]


async def test_web_fetch_opens_search_ref_without_repeating_search() -> None:
    search_tool = WebSearchTool(http=_FakeHttp())
    results = await search_tool.search("test", limit=1)
    fetch_tool = WebFetchTool(http=_FakeHttp(_PAGE_HTML), session=search_tool.session)

    result = await fetch_tool.execute({"ref_id": results[0]["ref_id"]}, ctx=None)

    assert result.success
    assert result.data["url"] == "https://example.com/page"
    assert result.data["ref_id"] == results[0]["ref_id"]
    assert "readable" in result.data["text"]


async def test_web_fetch_find_returns_excerpts_around_pattern() -> None:
    tool = WebFetchTool(http=_FakeHttp(_PAGE_HTML))

    result = await tool.execute(
        {"url": "https://example.com", "pattern": "readable"}, ctx=None
    )

    assert result.success
    assert result.data["count"] >= 1
    assert result.data["matches"][0]["offset"] >= 0
    assert "readable" in result.data["matches"][0]["excerpt"].lower()


async def test_web_fetch_rejects_unknown_ref() -> None:
    tool = WebFetchTool(http=_FakeHttp(_PAGE_HTML))
    with pytest.raises(ValueError, match="unknown web ref_id"):
        await tool.execute({"ref_id": "turn0search9-9"}, ctx=None)


async def test_web_fetch_rejects_non_http_urls() -> None:
    tool = WebFetchTool(http=_FakeHttp(_PAGE_HTML))
    with pytest.raises(ValueError, match="only supports http and https"):
        await tool.execute({"url": "file:///tmp/private.txt"}, ctx=None)


def test_find_in_page_is_case_insensitive_and_bounded() -> None:
    found = find_in_page("Alpha alpha ALPHA", "alpha", max_matches=2)
    assert found["total"] == 3
    assert len(found["matches"]) == 2
    assert all("alpha" in match["excerpt"].lower() for match in found["matches"])


async def test_more_than_three_queries_requires_medium_or_long() -> None:
    tool = WebSearchTool(http=_FakeHttp())
    with pytest.raises(ValueError, match="medium or long"):
        await tool.execute(
            {
                "search_query": [{"q": str(i)} for i in range(4)],
                "response_length": "short",
            },
            ctx=None,
        )


def test_shared_session_resolves_search_refs() -> None:
    session = WebSession(http=_FakeHttp())
    refs = session.search_refs(3)
    session.remember_url(refs[1], "https://example.com")
    assert session.url(refs[1]) == "https://example.com"
    assert session.page(refs[1]) is None


def test_build_web_tools_shares_search_session() -> None:
    tools = build_web_tools()

    search = tools.resolve("web_search")
    fetch = tools.resolve("web_fetch")

    assert search.session is fetch.session
