"""Models owned by Athena's core research tree."""

from typing import List, Literal, Self, TypeAlias

from pydantic import BaseModel, Field, model_validator

from athena.core.contracts import ArtifactRef, NonBlankText
from athena.evaluation.types import ComparisonVerdict, EvalResult

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

    def to_prompt(self):
        """将假设转为自然语言提示字符串，包含 statement、intervention、预期效果和验证状态。"""
        status_text = {
            "PROPOSED": "该假设尚未验证，需要后续实验进行评估。",
            "SUPPORTED": "实验结果支持该假设，该假设较大概率成立。",
            "REFUTED": "实验结果不支持该假设，但仍可在调整后继续验证。",
            "REJECTED": "该假设已被彻底拒绝，不应继续沿此方向实验。",
        }[self.status]

        return (
            f"假设：{self.statement}\n"
            f"实验改动：{self.intervention}\n"
            f"预期效果：{self.expected_effect}\n"
            f"验证状态：{self.status}\n"
            f"状态结论：{status_text}"
        )


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


class ExperimentOutcome(BaseModel):
    """已完成实验的结果，存储在 ResearchTree 中。"""

    eval: EvalResult
    verdict: ComparisonVerdict | None = None
    is_sota: bool = False
