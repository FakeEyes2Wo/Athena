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


class EvalSpecChain:
    """只追加的评估协议版本链（设计 §5.5）。

    单个版本是不可变 :class:`EvalSpec` 快照；``append_metric`` 只能追加新
    secondary 指标，不能删除或改写已有条目。每次追加产生新版本号，历史评分
    必须绑定精确版本，不同版本的总分不能直接比较。首版内存实现。
    """

    def __init__(self, initial: EvalSpec | None = None) -> None:
        self._versions: list[EvalSpec] = [initial] if initial is not None else []

    @property
    def version(self) -> int:
        """当前版本号（1 起始；未冻结返回 0）。"""
        return len(self._versions)

    @property
    def current(self) -> EvalSpec:
        """当前版本快照（不可变）。"""
        if not self._versions:
            raise ValueError("eval protocol not frozen yet")
        return self._versions[-1]

    @property
    def versions(self) -> list[EvalSpec]:
        """全部版本快照（append-only，不可改写）。"""
        return list(self._versions)

    def version_at(self, version: int) -> EvalSpec:
        """按版本号取快照；越界抛 IndexError。"""
        return self._versions[version - 1]

    def freeze(self, spec: EvalSpec) -> int:
        """冻结第一个协议版本；已冻结时抛错（冻结后只能 append，不能改写）。"""
        if self._versions:
            raise ValueError("eval protocol already frozen")
        self._versions.append(spec)
        return len(self._versions)

    @classmethod
    def from_versions(cls, versions: list[EvalSpec]) -> "EvalSpecChain":
        """从持久化版本列表重建链（append-only 恢复）。"""
        chain = cls()
        chain._versions = list(versions)
        return chain

    def append_metric(self, metric: MetricDef) -> int:
        """追加一个新 secondary 指标，产生新版本；返回新版本号。"""
        existing = {m.name for m in self.current.secondary}
        if metric.name in existing:
            raise ValueError(f"metric already present: {metric.name}")
        new = self.current.model_copy(
            update={"secondary": self.current.secondary + [metric]}
        )
        self._versions.append(new)
        return len(self._versions)


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
    "EvalSpecChain",
    "MetricDef",
]
