"""Kaggle 取数流水线：connect → download → search notebooks，各段失败降级不中断。

EDA / SOTA 分析不在这里做：下载后的数据交给 agent 用 workspace + shell 的
代码生成流程自行分析（与本地数据集走同一条 PREPARE 流程）。
"""

import json
import time
from pathlib import Path

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import resolve_workspace_path
from athena.kaggle.client import KaggleApiClient, author_name, pick_first
from athena.kaggle.schemas import (
    Competition,
    KaggleRunReport,
    KaggleRunRequest,
    NotebookSummary,
)


class KagglePipeline:
    def __init__(
        self,
        client: KaggleApiClient,
        artifacts: LocalArtifactStore,
        download_root: Path,
        request: KaggleRunRequest,
    ) -> None:
        self.client = client
        self.artifacts = artifacts
        self.download_root = download_root
        self.request = request
        self.report = KaggleRunReport(competition_ref=request.competition)

    async def run(self) -> KaggleRunReport:
        started = time.monotonic()
        competition = await self._connect()
        self.report.competition = competition
        if competition is not None:
            if self.request.download_data:
                await self._download(competition)
            self.report.notebooks = await self._search()
        self.report.http_requests = self.client.http_request_count
        self.report.total_seconds = round(time.monotonic() - started, 3)
        self.report.status = self.final_status()
        return self.report

    def final_status(self) -> str:
        return "empty" if self.report.competition is None else "complete"

    def _target_dir(self, competition: Competition) -> Path:
        subdir = self.request.download_subdir or competition.ref or "kaggle-data"
        return resolve_workspace_path(self.download_root, subdir)

    async def _connect(self) -> Competition | None:
        try:
            raw = await self.client.get_competition(self.request.competition)
        except Exception as error:  # 连接失败 → 空报告
            self.report.warnings.append(
                f"connect failed: {type(error).__name__}: {error}"
            )
            return None
        if not raw:
            self.report.warnings.append("connect returned an empty competition object")
            return None
        return _competition_from_raw(self.request.competition, raw)

    async def _download(self, competition: Competition) -> None:
        try:
            paths = await self.client.download_competition(
                competition.ref, self._target_dir(competition)
            )
        except Exception as error:  # 下载失败 → 无本地文件
            self.report.warnings.append(
                f"download failed: {type(error).__name__}: {error}"
            )
            return
        self.report.data_manifest_ref = await self.artifacts.put_text(
            json.dumps(
                [{"name": p.name, "size": p.stat().st_size} for p in paths],
                ensure_ascii=False,
            )
        )
        self.report.downloaded_files = [str(p) for p in paths]
        if not paths:
            self.report.warnings.append("download produced no files")

    async def _search(self) -> list[NotebookSummary]:
        if self.request.max_notebooks <= 0:
            return []
        try:
            raw = await self.client.list_notebooks(
                self.request.competition, page=1, sort_by="hotness"
            )
            return _notebooks_from_raw(raw[: self.request.max_notebooks])
        except Exception as error:  # 检索失败只省略证据
            self.report.warnings.append(
                f"notebook search failed: {type(error).__name__}: {error}"
            )
            return []


def _competition_from_raw(ref: str, raw: dict) -> Competition:
    return Competition(
        ref=ref,
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        evaluation_metric=pick_first(raw, "evaluationMetric", "evaluation_metric"),
        reward=raw.get("reward", ""),
        deadline=raw.get("deadline", ""),
        category=raw.get("category", ""),
        organization_name=pick_first(raw, "organizationName", "organization_name"),
        url=raw.get("url", ""),
    )


def _notebooks_from_raw(raw: list[dict]) -> list[NotebookSummary]:
    notebooks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref", "")
        notebooks.append(
            NotebookSummary(
                ref=ref,
                title=item.get("title", ""),
                author=author_name(item.get("author", "")),
                total_votes=int(
                    pick_first(item, "totalVotes", "total_votes", default=0)
                ),
                language=item.get("language", ""),
                version=str(
                    pick_first(
                        item,
                        "currentVersion",
                        "current_version",
                        "version",
                        "versionNumber",
                        default="",
                    )
                ),
                url=item.get("url", "")
                or (f"https://www.kaggle.com/code/{ref}" if ref else ""),
            )
        )
    return notebooks


async def run_kaggle(
    client: KaggleApiClient,
    artifacts: LocalArtifactStore,
    download_root: Path,
    request: KaggleRunRequest,
) -> KaggleRunReport:
    return await KagglePipeline(client, artifacts, download_root, request).run()
