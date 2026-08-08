"""DataAgent 领域模型与 EDA 采样服务（由 athena.data 折叠迁入）。

原 ``src/athena/data/`` 包在 agent 子系统迁移中整体删除；其中仍被 agent
路径使用的代码折入本模块：

- ``DataProfile``/``ColumnSummary``：真实 Ideator 的输入类型
  （``agents/ideator/ideator.py``），描述数据集画像；
- ``DataCard``：数据集卡片，被 ``scripts/export_rust_contract_fixtures.py``
  用作跨语言契约固件；
- ``SampleRef`` + ``create_analysis_samples``：DataAgent 的 EDA 采样服务
  （data-analysis §7.1）：大数据集按项目 EDA 上限生成恰好三项可复现样本。

随原包弃用的代码（``operations.py`` 的 split/clean、``ProcessingLog``/
``ProcessingRecord``/``SplitManifest`` 等）消费方在 World A 迁移中一并删除，
不再保留。
"""

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd
from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef


class DataCard(BaseModel):
    """数据集卡片 — 通过 artifact 引用描述数据集指纹、schema 和划分。"""

    dataset_ref: ArtifactRef
    fingerprint: str
    schema_ref: ArtifactRef
    split_manifest_ref: ArtifactRef | None = None


class ColumnSummary(BaseModel):
    """Statistical summary of a single column in a dataset."""

    name: str
    dtype: str
    missing_rate: float = 0.0
    n_unique: int | None = None
    sample_values: list[str] = Field(default_factory=list)
    processing: str = ""


class DataProfile(BaseModel):
    """Profile of a dataset: row/column counts, column summaries, task hints."""

    row_count: int
    col_count: int
    columns: list[ColumnSummary] = Field(default_factory=list)
    missing_rate: float = 0.0
    task_type_hint: str = ""
    target_col: str | None = None
    issue_summary: str = ""


@dataclass(frozen=True)
class SampleRef:
    seed: int
    artifact: ArtifactRef


ANALYSIS_SEEDS = (17, 42, 97)


def create_analysis_samples(
    frame: pd.DataFrame,
    *,
    put_frame: Callable[[pd.DataFrame], ArtifactRef],
    sample_size: int = 10_000,
    eda_cap: int = 100_000,
) -> list[SampleRef]:
    """按项目 EDA 上限生成确定性样本（data-analysis §7.1）。

    超过 ``eda_cap`` 时用三个已记录且互不相同的 seed 生成恰好三项等规模样本；
    否则使用单一固定 seed。采样只服务 EDA，不改变 train/validation/test。
    """
    seeds = ANALYSIS_SEEDS if len(frame) > eda_cap else (ANALYSIS_SEEDS[0],)
    return [
        SampleRef(
            seed=seed,
            artifact=put_frame(
                frame.sample(min(sample_size, len(frame)), random_state=seed)
            ),
        )
        for seed in seeds
    ]
