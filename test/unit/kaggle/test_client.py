"""KaggleApiClient 的单元测试，用假传输避免触网。"""

import json
from unittest import mock

from athena.kaggle.auth import KaggleCredentials
from athena.kaggle.client import KaggleApiClient, KaggleApiError
from athena.research.paper_source.http import HttpResponse


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
