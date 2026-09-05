"""Kaggle 竞赛自动分析：连接竞赛 → 下载数据 → 自动 EDA → 检索 SOTA → 给出打法。"""

from athena.kaggle.pipeline import run_kaggle
from athena.kaggle.schemas import KaggleRunRequest
from athena.kaggle.wiring import (
    AGENT_KAGGLE_TOOLS,
    KaggleStack,
    build_kaggle_stack,
    build_kaggle_tools,
)

__all__ = [
    "AGENT_KAGGLE_TOOLS",
    "KaggleRunRequest",
    "KaggleStack",
    "build_kaggle_stack",
    "build_kaggle_tools",
    "run_kaggle",
]
