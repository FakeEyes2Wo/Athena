"""DuckDuckGo 网页搜索与网页抓取的解析单元测试（假 HTTP，不触网）。"""

from athena.research.paper_source.http import HttpResponse
from athena.retrieval.web_search import WebFetchTool, WebSearchTool, extract_page_text

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

    async def get(self, url: str, headers: dict | None = None) -> HttpResponse:
        return HttpResponse(status=200, url=url, body=self.body, headers={})


async def test_search_parses_title_url_and_snippet() -> None:
    results = await WebSearchTool(http=_FakeHttp()).search("test", limit=10)
    assert len(results) == 2
    assert results[0] == {
        "title": "Example Title",
        "url": "https://example.com/page",
        "snippet": "Example snippet text",
    }
    assert results[1]["title"] == "Second"
    assert results[1]["url"] == "https://example.org"


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
