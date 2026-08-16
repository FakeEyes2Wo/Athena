"""研究域数据模型（原 ``athena.data`` 与 ``research.models`` 折叠迁入）。

原 ``src/athena/data/`` 包在 agent 子系统迁移中整体删除；其中仍被使用的代码
折入本模块。``MetricSpec``/``TaskMetaData`` 原在 ``research/models.py``，现与
其余研究域数据模型合并于此：

- ``DataCard``：数据集卡片，被 ``scripts/export_rust_contract_fixtures.py``
  用作跨语言契约固件；
- ``MetricSpec``/``TaskMetaData``：任务元数据与评估指标规格，同样被 Rust
  跨语言契约固件消费。

随原包弃用的代码（``SampleRef``/``create_analysis_samples`` 的 EDA 采样服务、
``operations.py`` 的 split/clean 等）消费方在 World A 迁移中一并删除，不再保留。
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef, NonBlankText


class DataCard(BaseModel):
    """数据集卡片 — 通过 artifact 引用描述数据集指纹、schema 和划分。"""

    dataset_ref: ArtifactRef
    fingerprint: str
    schema_ref: ArtifactRef
    split_manifest_ref: ArtifactRef | None = None


class MetricSpec(BaseModel):
    """评估指标规格 — 名称和优化方向。"""

    name: NonBlankText
    direction: Literal["maximize", "minimize"]


class TaskMetaData(BaseModel):
    """任务元数据 — 描述 ML 任务类型、数据格式和评估约束。"""

    task_type: str
    data_type: str
    target_vars: list[str] = Field(default_factory=list)
    primary_metric: MetricSpec
    constraints: list[str] = Field(default_factory=list)
