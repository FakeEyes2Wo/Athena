"""OpenAlex locator normalization tests."""

import json

import pytest

from athena.research.literature.paper_source.http import HttpResponse
from athena.research.literature.paper_source.openalex import (
    OpenAlexClient,
    normalize_work_locator,
)


def test_normalize_work_locator_adds_doi_prefix() -> None:
    assert normalize_work_locator("10.1023/A:1010933404324") == (
        "doi:10.1023/a:1010933404324"
    )
    assert normalize_work_locator("doi:10.1023/A:1010933404324") == (
        "doi:10.1023/a:1010933404324"
    )
    assert normalize_work_locator("W2911964244") == "W2911964244"


@pytest.mark.asyncio
async def test_openalex_client_requests_bare_doi_as_doi_locator() -> None:
    urls: list[str] = []

    class Http:
        async def get(self, url: str) -> HttpResponse:
            urls.append(url)
            return HttpResponse(
                200, url, json.dumps({"id": "https://openalex.org/W1"}).encode()
            )

    work = await OpenAlexClient(Http()).fetch_work("10.1023/A:1010933404324")

    assert work is not None
    assert "/works/doi:10.1023/a:1010933404324" in urls[0]
