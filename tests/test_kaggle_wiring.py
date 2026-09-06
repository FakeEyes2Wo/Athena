"""Kaggle URL → slug 提取 + 提交预测的两步流程。"""

import pytest

from athena.kaggle.auth import KaggleCredentials
from athena.kaggle.client import KaggleApiClient, _multipart_file
from athena.kaggle.tool import kaggle_slug_from_task
from athena.research.literature.paper_source.http import HttpResponse


def test_extracts_slug_from_competition_url() -> None:
    assert (
        kaggle_slug_from_task("https://www.kaggle.com/competitions/maze-crawler")
        == "maze-crawler"
    )


def test_extracts_slug_from_short_url() -> None:
    assert kaggle_slug_from_task("kaggle.com/c/titanic") == "titanic"


def test_returns_none_for_non_kaggle_task() -> None:
    assert kaggle_slug_from_task("predict survival on titanic") is None


def test_multipart_file_builds_file_field() -> None:
    body = _multipart_file("sub.csv", b"a,b\n1,2\n", "----boundary")
    assert b'name="file"' in body
    assert b'filename="sub.csv"' in body
    assert b"a,b\n1,2\n" in body
    assert body.endswith(b"--boundary--\r\n")


@pytest.mark.asyncio
async def test_submit_submission_uses_two_step_flow(tmp_path, monkeypatch) -> None:
    client = KaggleApiClient(KaggleCredentials(bearer_token="test-token"))
    posts: list[tuple[str, bytes]] = []

    async def fake_post(url: str, headers, data: bytes) -> HttpResponse:
        del headers
        posts.append((url, data))
        if "submissions/url" in url:
            return HttpResponse(
                status=200,
                url=url,
                body=b'{"createUrl": "https://storage.googleapis.com/signed"}',
                headers={},
            )
        return HttpResponse(status=200, url=url, body=b"{}", headers={})

    monkeypatch.setattr(client, "_post", fake_post)
    submission = tmp_path / "submission.csv"
    submission.write_text("id,pred\n1,0.5\n", encoding="utf-8")

    result = await client.submit_submission("maze-crawler", submission)

    assert result == {
        "competition": "maze-crawler",
        "file": "submission.csv",
        "status": "submitted",
    }
    assert len(posts) == 2
    # 第一步是 JSON 拿签名 URL，第二步是 multipart 上传文件。
    assert b'"fileName"' in posts[0][1]
    assert b'name="file"' in posts[1][1]
    assert posts[1][0] == "https://storage.googleapis.com/signed"
