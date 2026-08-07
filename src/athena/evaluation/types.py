"""Evaluation protocol models."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef


class MetricDef(BaseModel):
    """CodeAgent 需要计算的指标规格。"""

    name: str
    direction: Literal["maximize", "minimize"]
    description: str  # prompt for CodeAgent


class EvalSpec(BaseModel):
    """冻结的评估协议。在 PREPARE 阶段构建，在 SEARCH 阶段不可变。"""

    model_config = {"frozen": True}

    primary: MetricDef
    secondary: list[MetricDef] = Field(default_factory=list)
    eval_script: str = ""
    split_seed: int = 42
    test_ratio: float = 0.2


class EvaluationInputs(BaseModel):
    """Host-owned files required to score one held-out phase."""

    model_config = {"frozen": True}

    phase: Literal["validation", "test"]
    train_path: Path
    features_path: Path
    labels_path: Path
    target: str
    row_id_column: str = "__athena_row_id"


class EvalResult(BaseModel):
    """在预测上运行 eval.py 的输出结果。"""

    experiment_id: str
    primary: float
    secondary: dict[str, float] = Field(default_factory=dict)
    per_sample: ArtifactRef


class ComparisonVerdict(BaseModel):
    """两个实验的两两比较。"""

    winner: Literal["baseline", "candidate", "tie"]
    p_value: float


__all__ = [
    "ComparisonVerdict",
    "EvaluationInputs",
    "EvalResult",
    "EvalSpec",
    "MetricDef",
]
