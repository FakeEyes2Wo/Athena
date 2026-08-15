"""KaggleRunTool 的单元测试：假客户端 + 真实 artifact 存储，验证取数内联。"""

import asyncio
from pathlib import Path

from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool_types import ToolContext
from athena.kaggle.tool import KaggleRunTool
from athena.kaggle.wiring import KaggleStack


class FakeClient:
    """按流水线契约返回最小数据的假客户端。"""

    def __init__(self) -> None:
        self.requests = 0

    @property
    def http_request_count(self) -> int:
        return self.requests

    async def get_competition(self, ref: str) -> dict:
        self.requests += 1
        return {"ref": ref, "title": "Titanic", "evaluationMetric": "accuracy"}

    async def download_competition(self, ref: str, dest_dir) -> list[Path]:
        self.requests += 1
        target = Path(dest_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "train.csv").write_text(
            "x,y,target\n1,2,0\n2,3,1\n3,4,1\n", encoding="utf-8"
        )
        return [target / "train.csv"]

    async def list_notebooks(
        self, competition: str, *, page: int = 1, sort_by: str = "hotness"
    ) -> list[dict]:
        self.requests += 1
        return [{"ref": "a/top", "title": "top", "totalVotes": 5}]


async def _noop_emit(kind: str, ref: str, data=None) -> None:
    del kind, ref, data


def _stack(tmp_path) -> KaggleStack:
    return KaggleStack(
        client=FakeClient(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        download_root=tmp_path / "download",
    )


async def test_kaggle_run_fetches_data_and_notebooks(tmp_path) -> None:
    tool = KaggleRunTool(_stack(tmp_path))
    ctx = ToolContext("kaggle_run", "c1", _noop_emit, asyncio.Event())
    result = await tool.execute({"competition": "titanic"}, ctx)
    assert result.success
    assert result.data["status"] == "complete"
    assert result.data["competition"]["title"] == "Titanic"
    assert result.data["downloaded_files"]
    assert result.data["notebooks"][0]["title"] == "top"


async def test_kaggle_run_skips_download_when_disabled(tmp_path) -> None:
    stack = _stack(tmp_path)
    stack.download = False
    tool = KaggleRunTool(stack)
    ctx = ToolContext("kaggle_run", "c2", _noop_emit, asyncio.Event())
    result = await tool.execute({"competition": "titanic"}, ctx)
    assert result.success
    assert result.data["downloaded_files"] == []
    assert result.data["data_manifest_ref"] == ""
    assert result.data["notebooks"]  # 不下载仍检索 notebook 证据
