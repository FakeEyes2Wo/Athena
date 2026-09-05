"""Athena 研究流程的领域模型与 Artifact payload 合同。

跨任务的数据结构在此冻结：任务元数据、DataScript Bundle、候选评估与 final-test。领域服务
（script_runner / evaluation / validation）与 Supervisor 只消费这些 Pydantic
模型；字段改动必须同步设计文档与所有消费者。

首版只冻结合同的形状与硬约束（enum/范围/非空），业务语义由 LLM 生成的脚本承担。
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from athena.core.contracts import ArtifactRef, NonBlankText


@dataclass(frozen=True)
class GeneralTurnOutcome:
    """One completed General Agent turn: stable agent id plus its structured result."""

    agent_id: str
    result: dict[str, object]


class DataCard(BaseModel):
    """Describe a dataset through immutable artifact references."""

    dataset_ref: ArtifactRef
    fingerprint: str
    schema_ref: ArtifactRef
    split_manifest_ref: ArtifactRef | None = None


class MetricSpec(BaseModel):
    """Name an evaluation metric and its optimization direction."""

    name: NonBlankText
    direction: Literal["maximize", "minimize"]


class TaskMetaData(BaseModel):
    """Describe task shape and evaluation constraints."""

    task_type: str
    data_type: str
    target_vars: list[str] = Field(default_factory=list)
    primary_metric: MetricSpec
    constraints: list[str] = Field(default_factory=list)


class DataScriptBundle(BaseModel):
    """冻结的 LLM 生成数据脚本 Bundle（python-uv，supervisor_imp_docs Task 4）。

    project_ref/lock_ref/tree_ref/python_version/environment_hash 使 bundle
    自包含：可重建完整冻结项目上下文（源码树 + pyproject + uv.lock + 解释器版本）
    后以 ``uv sync --frozen`` + ``uv run --frozen`` 执行，不依赖原 draft 目录。
    """

    bundle_id: NonBlankText
    entrypoint: str
    runtime: Literal["python-uv"] = "python-uv"
    lock_ref: ArtifactRef | None = None
    project_ref: ArtifactRef | None = None  # pyproject.toml 内容
    source_ref: ArtifactRef | None = None  # entrypoint 源码
    tree_ref: ArtifactRef | None = None  # 完整源码树 manifest {relpath: content_ref}
    python_version: str | None = None  # 解析出的解释器版本
    environment_hash: str | None = None  # sha256(pyproject + uv.lock + python_version)


class EvaluatorDescriptor(BaseModel):
    """README-only frozen evaluator descriptor.

    Replaces the old ``DataScriptBundle`` freeze: the evaluator stays in its
    working directory and README.md is the prompt-level freeze marker.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    dir_path: str
    readme_ref: ArtifactRef
    prediction_format: str = "tabular_csv"
    entrypoint: str = "evaluate.py"


class CandidateEvaluation(BaseModel):
    """单个候选实验的评估结果（trusted evaluator 产出）。"""

    candidate_id: NonBlankText
    test_score: float
    # Optional uncertainty evidence; when absent settlement must be conservative.
    test_se: float | None = None
    test_n: int | None = None
    kfold_mean: float | None = None
    kfold_std: float | None = None
    metrics_ref: ArtifactRef | None = None
    direction: Literal["maximize", "minimize"]


class ValidationResult(BaseModel):
    """VALIDATE 的最终结论（gap 超 tolerance 时 warning=true，仍 COMPLETED）。"""

    result_id: NonBlankText
    status: Literal["COMPLETED", "FAILED"]
    test_score: float | None = None
    final_test_score: float | None = None
    generalization_gap: float | None = None
    generalization_warning: bool = False
    sota_commit: str | None = None
    validation_commit: str | None = None
    predictions_ref: ArtifactRef | None = None
    predictions_path: str | None = None
    metrics_ref: ArtifactRef | None = None
    evidence_ref: ArtifactRef | None = None


__all__ = [
    "CandidateEvaluation",
    "DataCard",
    "DataScriptBundle",
    "EvaluatorDescriptor",
    "GeneralTurnOutcome",
    "MetricSpec",
    "TaskMetaData",
    "ValidationResult",
]
