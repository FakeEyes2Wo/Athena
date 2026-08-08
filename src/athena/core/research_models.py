"""Models owned by Athena's core research tree."""

from typing import List, Literal, Self, TypeAlias

from pydantic import BaseModel, Field, model_validator

from athena.core.contracts import ArtifactRef, NonBlankText

HypothesisStatus: TypeAlias = Literal[
    "PROPOSED",  # 刚刚提出假设
    "SUPPORTED",  # 支持假设
    "REFUTED",  # 不支持假设
    "REJECTED",  # 彻底拒绝
]


class Hypothesis(BaseModel):
    """一个可通过实验验证或证伪的机器学习假设。"""

    statement: NonBlankText = Field(description="为什么认为这个改动可能有效")
    intervention: NonBlankText = Field(description="实验中具体改变什么")
    expected_effect: NonBlankText = Field(description="预期指标如何变化")
    status: HypothesisStatus = "PROPOSED"
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    patience_grant: int = Field(default=0, ge=0)
    patience_evidence_ref: ArtifactRef | None = None
    id: str | None = Field(default=None, description="Unique hypothesis identifier")
    parent_id: str | None = Field(
        default=None, description="Parent experiment ID in ResearchTree"
    )
    sources: list[str] = Field(
        default_factory=list, description="Paper URLs or model repos"
    )

    @model_validator(mode="after")
    def _validate_patience(self) -> Self:
        if self.patience_grant and self.patience_evidence_ref is None:
            raise ValueError("positive patience grant requires an evidence reference")
        return self


class ExperimentPlan(BaseModel):
    """实验计划 — 描述如何修改代码目录以验证假设，含评价标准和资源预算。

    生成计划前需要明确工作目录（由程序控制而非 LLM 输出）。
    """

    kind: str
    change: str = Field(
        description="根据假设，Plan应该如何改变目录从而完成我们的实验部分"
    )
    rubrics: List[str] = Field(
        default_factory=list,
        description="我们建立Plan过后，这个Plan应该实现到什么程度的评价指标",
    )
    run_config_ref: ArtifactRef
    budget: dict
    acceptance_rule: str


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
