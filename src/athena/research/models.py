"""Research workflow domain models."""

from typing import Literal

from pydantic import BaseModel, Field

from athena.core.contracts import NonBlankText


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
