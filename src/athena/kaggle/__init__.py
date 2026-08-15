"""Kaggle 竞赛自动分析：连接竞赛 → 下载数据 → 自动 EDA → 检索 SOTA → 给出打法。"""

from athena.kaggle.pipeline import KaggleRunReport, KaggleRunRequest, run_kaggle
from athena.kaggle.tool import (
    KAGGLE_DOWNLOAD_DATA,
    KAGGLE_GET_COMPETITION,
    KAGGLE_LIST_COMPETITIONS,
    KAGGLE_LIST_NOTEBOOKS,
    KAGGLE_RUN,
    KAGGLE_SUBMIT,
    KaggleDownloadDataTool,
    KaggleGetCompetitionTool,
    KaggleListCompetitionsTool,
    KaggleListNotebooksTool,
    KaggleRunTool,
    KaggleSubmitTool,
)
from athena.kaggle.wiring import (
    AGENT_KAGGLE_TOOLS,
    KaggleStack,
    build_kaggle_stack,
    build_kaggle_tools,
    kaggle_slug_from_task,
)

__all__ = [
    "AGENT_KAGGLE_TOOLS",
    "KAGGLE_DOWNLOAD_DATA",
    "KAGGLE_GET_COMPETITION",
    "KAGGLE_LIST_COMPETITIONS",
    "KAGGLE_LIST_NOTEBOOKS",
    "KAGGLE_RUN",
    "KAGGLE_SUBMIT",
    "KaggleDownloadDataTool",
    "KaggleGetCompetitionTool",
    "KaggleListCompetitionsTool",
    "KaggleListNotebooksTool",
    "KaggleRunReport",
    "KaggleRunRequest",
    "KaggleRunTool",
    "KaggleSubmitTool",
    "KaggleStack",
    "build_kaggle_stack",
    "build_kaggle_tools",
    "kaggle_slug_from_task",
    "run_kaggle",
]
