"""Kaggle 组合根：装配凭据、客户端与 artifact 存储（纯取数，无 LLM）。"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from athena.core.artifact_store import LocalArtifactStore
from athena.core.tool import ToolRegistry
from athena.kaggle.auth import KAGGLE_TOKEN_ENV, KaggleCredentials, resolve_credentials
from athena.kaggle.client import KaggleApiClient
from athena.kaggle.tool import (
    KAGGLE_DOWNLOAD_DATA,
    KAGGLE_GET_COMPETITION,
    KAGGLE_GET_DISCUSSION,
    KAGGLE_GET_NOTEBOOK,
    KAGGLE_LIST_COMPETITIONS,
    KAGGLE_LIST_DISCUSSIONS,
    KAGGLE_LIST_NOTEBOOKS,
    KAGGLE_RUN,
    KAGGLE_SUBMIT,
    KaggleDownloadDataTool,
    KaggleGetCompetitionTool,
    KaggleGetDiscussionTool,
    KaggleGetNotebookTool,
    KaggleListCompetitionsTool,
    KaggleListDiscussionsTool,
    KaggleListNotebooksTool,
    KaggleRunTool,
    KaggleSubmitTool,
)

DOWNLOAD_ROOT_ENV = "ATHENA_KAGGLE_DOWNLOAD_ROOT"
DEFAULT_DOWNLOAD_ROOT = Path.cwd() / "kaggle-data"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".athena" / "artifacts"

_KAGGLE_SLUG_RE = re.compile(
    r"kaggle\.com/(?:competitions|c|t)/([a-z0-9][a-z0-9-]*)", re.IGNORECASE
)


def kaggle_slug_from_task(task: str) -> str | None:
    """从任务文本提取 Kaggle 竞赛 slug（如 URL 里的 ``maze-crawler``）。"""
    match = _KAGGLE_SLUG_RE.search(task or "")
    return match.group(1) if match else None


@dataclass(slots=True)
class KaggleStack:
    client: KaggleApiClient
    artifacts: LocalArtifactStore
    download_root: Path
    download: bool = True


def build_kaggle_stack(
    *,
    download_root: str | Path = "",
    credentials: KaggleCredentials | None = None,
    artifacts: LocalArtifactStore | None = None,
    download: bool = True,
) -> KaggleStack:
    resolved = credentials or resolve_credentials(
        KaggleCredentials(bearer_token=os.environ.get(KAGGLE_TOKEN_ENV, ""))
    )
    root = Path(
        download_root or os.environ.get(DOWNLOAD_ROOT_ENV, "") or DEFAULT_DOWNLOAD_ROOT
    ).resolve()
    store = artifacts or LocalArtifactStore(
        os.environ.get("ATHENA_ARTIFACT_ROOT", "") or DEFAULT_ARTIFACT_ROOT
    )
    return KaggleStack(
        client=KaggleApiClient(resolved),
        artifacts=store,
        download_root=root,
        download=download,
    )


_TOOL_FACTORIES = {
    KAGGLE_LIST_COMPETITIONS: KaggleListCompetitionsTool,
    KAGGLE_GET_COMPETITION: KaggleGetCompetitionTool,
    KAGGLE_LIST_NOTEBOOKS: KaggleListNotebooksTool,
    KAGGLE_GET_NOTEBOOK: KaggleGetNotebookTool,
    KAGGLE_LIST_DISCUSSIONS: KaggleListDiscussionsTool,
    KAGGLE_GET_DISCUSSION: KaggleGetDiscussionTool,
    KAGGLE_DOWNLOAD_DATA: KaggleDownloadDataTool,
    KAGGLE_RUN: KaggleRunTool,
    KAGGLE_SUBMIT: KaggleSubmitTool,
}

# 每个 agent 按其职责注入的最少 Kaggle 工具；data 做补充 EDA 无需 Kaggle 工具。
AGENT_KAGGLE_TOOLS: dict[str, tuple[str, ...]] = {
    "evaluator": (KAGGLE_GET_COMPETITION, KAGGLE_DOWNLOAD_DATA),
    "prepare": (KAGGLE_RUN,),
    "ideator": (
        KAGGLE_LIST_NOTEBOOKS,
        KAGGLE_GET_NOTEBOOK,
        KAGGLE_LIST_DISCUSSIONS,
        KAGGLE_GET_DISCUSSION,
    ),
    "plan": (
        KAGGLE_LIST_NOTEBOOKS,
        KAGGLE_GET_NOTEBOOK,
        KAGGLE_LIST_DISCUSSIONS,
        KAGGLE_GET_DISCUSSION,
    ),
    "kaggle_handoff": (
        KAGGLE_LIST_NOTEBOOKS,
        KAGGLE_GET_NOTEBOOK,
        KAGGLE_LIST_DISCUSSIONS,
        KAGGLE_GET_DISCUSSION,
    ),
    "general": tuple(_TOOL_FACTORIES),
}


def build_kaggle_tools(
    stack: KaggleStack, names: tuple[str, ...] | None = None
) -> ToolRegistry:
    tools = ToolRegistry()
    for name in names or tuple(_TOOL_FACTORIES):
        tools.register(_TOOL_FACTORIES[name](stack))
    return tools
