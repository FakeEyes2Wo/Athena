"""Kaggle 取数端到端单元测试：假客户端 + 真实 artifact 存储，不触网。"""

from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.kaggle.pipeline import run_kaggle
from athena.kaggle.schemas import KaggleRunRequest
from athena.kaggle.tool import KaggleStack


class FakeKaggleClient:
    """按流水线契约返回最小数据的假客户端。"""

    def __init__(self, failure: str = "") -> None:
        self.requests = 0
        self.failure = failure

    @property
    def http_request_count(self) -> int:
        return self.requests

    async def get_competition(self, ref: str) -> dict:
        self.requests += 1
        if self.failure == "connect":
            raise RuntimeError("boom")
        return {"ref": ref, "title": "Titanic", "evaluationMetric": "accuracy"}

    async def download_competition(self, ref: str, dest_dir) -> list[Path]:
        self.requests += 1
        if self.failure == "download":
            raise RuntimeError("boom")
        target = Path(dest_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "train.csv").write_text(
            "x,y,target\n1,2,0\n2,3,1\n3,4,1\n", encoding="utf-8"
        )
        return [target / "train.csv"]

    async def list_notebooks(
        self, competition: str, *, page: int = 1, sort_by: str = "votes"
    ) -> list[dict]:
        self.requests += 1
        if self.failure == "search":
            raise RuntimeError("boom")
        return [{"ref": "n1", "title": "top", "totalVotes": 5, "url": "http://x"}]


def _stack(tmp_path, failure: str = "") -> KaggleStack:
    return KaggleStack(
        client=FakeKaggleClient(failure),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        download_root=tmp_path / "download",
    )


async def test_run_fetches_data_and_notebooks(tmp_path) -> None:
    report = await run_kaggle(
        _stack(tmp_path),
        KaggleRunRequest(competition="titanic"),
    )
    assert report.status == "complete"
    assert report.competition is not None
    assert report.competition.title == "Titanic"
    assert len(report.downloaded_files) == 1
    assert report.data_manifest_ref
    assert len(report.notebooks) == 1
    assert report.notebooks[0].title == "top"


async def test_run_download_only_skips_notebooks(tmp_path) -> None:
    report = await run_kaggle(
        _stack(tmp_path),
        KaggleRunRequest(competition="titanic", max_notebooks=0),
    )
    assert report.status == "complete"
    assert report.downloaded_files
    assert report.notebooks == []


async def test_run_without_download(tmp_path) -> None:
    report = await run_kaggle(
        _stack(tmp_path),
        KaggleRunRequest(competition="titanic", download_data=False),
    )
    assert report.status == "complete"
    assert report.downloaded_files == []
    assert report.data_manifest_ref == ""
    assert len(report.notebooks) == 1


@pytest.mark.parametrize(
    ("failure", "warning", "status"),
    [
        ("connect", "connect failed", "empty"),
        ("download", "download failed", "complete"),
        ("search", "notebook search failed", "complete"),
    ],
)
async def test_stage_failures_degrade_to_partial_reports(
    tmp_path, failure: str, warning: str, status: str
) -> None:
    report = await run_kaggle(
        _stack(tmp_path, failure),
        KaggleRunRequest(competition="titanic"),
    )

    assert report.status == status
    assert warning in report.warnings[0]
