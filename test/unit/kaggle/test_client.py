"""KaggleApiClient 的单元测试，用假传输避免触网。"""

import io
import json
import zipfile
from unittest import mock

from athena.kaggle.auth import KaggleCredentials
from athena.kaggle.client import KaggleApiClient, KaggleApiError
from athena.research.literature.paper_source.http import HttpResponse


class FakeHttp:
    """记录请求、返回预定响应的最小 GET 传输。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.url = ""
        self.headers: dict[str, str] = {}

    async def get(self, url: str, headers: dict[str, str]) -> HttpResponse:
        self.url = url
        self.headers = dict(headers)
        return HttpResponse(status=self.status, url=url, body=self.body, headers={})


def _client(http: FakeHttp) -> KaggleApiClient:
    return KaggleApiClient(KaggleCredentials(bearer_token="KGAT_t"), http=http)


async def test_list_competitions_sends_bearer_and_returns_list() -> None:
    http = FakeHttp(json.dumps([{"ref": "titanic", "title": "Titanic"}]).encode())
    client = _client(http)
    result = await client.list_competitions(search="tit")
    assert result == [{"ref": "titanic", "title": "Titanic"}]
    assert http.headers.get("Authorization") == "Bearer KGAT_t"
    assert "search=tit" in http.url


async def test_get_competition_returns_dict() -> None:
    http = FakeHttp(json.dumps({"ref": "x", "title": "X"}).encode())
    client = _client(http)
    result = await client.get_competition("x")
    assert result["title"] == "X"
    assert http.url.endswith("/competitions/get/x")


async def test_download_competition_returns_paths(tmp_path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "train.csv").write_text("x\n1\n", encoding="utf-8")
    client = KaggleApiClient(KaggleCredentials(bearer_token="KGAT_t"))
    with mock.patch("kagglehub.competition_download", return_value=str(dest)):
        paths = await client.download_competition("x", dest)
    assert [p.name for p in paths] == ["train.csv"]


async def test_get_notebook_returns_source_from_json() -> None:
    http = FakeHttp(json.dumps({"source": "print('hello')"}).encode())
    client = _client(http)

    source = await client.get_notebook("owner/slug")

    assert source == "print('hello')"
    assert "kernel=owner%2Fslug" in http.url


async def test_get_notebook_reads_blob_source_from_pull_response() -> None:
    """Kaggle v1 pull 响应把 notebook 源码放在 ``blob.source``。"""
    http = FakeHttp(
        json.dumps(
            {"metadata": {"ref": "owner/slug"}, "blob": {"source": '{"cells": []}'}}
        ).encode()
    )
    client = _client(http)

    source = await client.get_notebook("owner/slug")

    assert source == '{"cells": []}'


async def test_get_notebook_tolerates_utf8_bom() -> None:
    body = "\ufeff" + json.dumps(
        {"metadata": {"ref": "owner/slug"}, "blob": {"source": '{"cells": []}'}}
    )
    http = FakeHttp(body.encode("utf-8"))
    client = _client(http)

    source = await client.get_notebook("owner/slug")

    assert source == '{"cells": []}'


def _zip_notebook_source() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notebook.ipynb", '{"cells": []}')
    return buffer.getvalue()


async def test_get_notebook_unwraps_zip_archive() -> None:
    http = FakeHttp(_zip_notebook_source())
    client = _client(http)

    source = await client.get_notebook("owner/slug")

    assert source == '{"cells": []}'


async def test_list_discussions_uses_internal_endpoint_and_parses_threads() -> None:
    http = FakeHttp(
        json.dumps(
            [
                {
                    "ref": "123",
                    "title": "trick",
                    "author": {"name": "u"},
                    "totalVotes": 4,
                    "totalComments": 9,
                }
            ]
        ).encode()
    )
    client = _client(http)

    result = await client.list_discussions("titanic")

    assert result == [
        {
            "ref": "123",
            "title": "trick",
            "author": {"name": "u"},
            "totalVotes": 4,
            "totalComments": 9,
        }
    ]
    assert "api/i/competitions/titanic/discussions" in http.url
    assert "sortBy=hotness" in http.url


async def test_get_discussion_returns_joined_thread_text() -> None:
    http = FakeHttp(
        json.dumps(
            {
                "title": "How I improved",
                "body": "Feature engineering helped.",
                "comments": [{"author": "u2", "body": "Try log transform."}],
            }
        ).encode()
    )
    client = _client(http)

    source = await client.get_discussion("123")

    assert "# How I improved" in source
    assert "Feature engineering helped." in source
    assert "u2:" in source
    assert "Try log transform." in source
    assert "api/i/discussions/123" in http.url


async def test_non_2xx_raises_kaggle_api_error() -> None:
    http = FakeHttp(json.dumps({"message": "forbidden"}).encode(), status=403)
    client = _client(http)
    try:
        await client.list_competitions()
    except KaggleApiError as error:
        assert error.status == 403
        assert "forbidden" in error.message
    else:
        raise AssertionError("expected KaggleApiError")
