"""DuckDuckGo 网页搜索的解析单元测试（假 HTTP，不触网）。"""

from athena.research.paper_source.http import HttpResponse
from athena.retrieval.web_search import WebSearchTool

_HTML = """<!doctype html><html>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example Title</a>
<a class="result__snippet" href="https://example.com/page"><b>Example</b> snippet text</a>
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org&amp;rut=def">Second</a>
<a class="result__snippet" href="https://example.org">Second snippet</a>
</html>"""


class _FakeHttp:
    async def get(self, url: str, headers: dict | None = None) -> HttpResponse:
        return HttpResponse(status=200, url=url, body=_HTML.encode(), headers={})


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
