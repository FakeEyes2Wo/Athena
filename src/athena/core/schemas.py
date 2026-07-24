"""Athena shared fact models; large payloads stay behind artifact references."""

from typing import Annotated, Literal, Self, TypeAlias, List

from pydantic import BaseModel, Field, StringConstraints, model_validator

NonBlankText: TypeAlias = Annotated[str, StringConstraints(pattern=r"\S")]
ArtifactRef: TypeAlias = NonBlankText
CommitHash: TypeAlias = NonBlankText
ExecutionId: TypeAlias = NonBlankText

HypothesisStatus: TypeAlias = Literal[
    "PROPOSED",  # 刚刚提出假设
    "SUPPORTED",  # 支持假设
    "REFUTED",  # 不支持假设
    "REJECTED",  # 彻底拒绝
]


class MetricSpec(BaseModel):
    name: NonBlankText
    direction: Literal["maximize", "minimize"]


class TaskMetaData(BaseModel):
    task_type: str
    data_type: str
    target_vars: list[str] = Field(default_factory=list)
    primary_metric: MetricSpec
    constraints: list[str] = Field(default_factory=list)


class DataCard(BaseModel):
    dataset_ref: ArtifactRef
    fingerprint: str
    schema_ref: ArtifactRef
    split_manifest_ref: ArtifactRef | None = None


class Hypothesis(BaseModel):
    """一个可通过实验验证或证伪的机器学习假设。"""

    statement: NonBlankText = Field(description="为什么认为这个改动可能有效")
    intervention: NonBlankText = Field(description="实验中具体改变什么")
    expected_effect: NonBlankText = Field(description="预期指标如何变化")
    status: HypothesisStatus = "PROPOSED"
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    patience_grant: int = Field(default=0, ge=0)
    patience_evidence_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def _validate_patience(self) -> Self:
        if self.patience_grant and self.patience_evidence_ref is None:
            raise ValueError("positive patience grant requires an evidence reference")
        return self

    def to_prompt(self):
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


class ExperimentPlan(
    BaseModel
):  # 这里一定要注意，生成之前要说明工作目录。不过工作目录应该是通过程序进行处理的
    kind: str
    change: str = Field(
        description="根据假设，Plan应该如何改变目录从而完成我们的实验部分"
    )
    rubrics: List[str] = Field(
        description="我们建立Plan过后，这个Plan应该实现到什么程度的评价指标"
    )
    run_config_ref: ArtifactRef
    budget: dict
    acceptance_rule: str


class AthenaThread(BaseModel):
    thread_id: str
    session_id: str
    status: str
    context_ref: ArtifactRef


class AthenaTurn(BaseModel):
    turn_id: str
    thread_id: str
    request_ref: ArtifactRef
    status: str
    result_ref: ArtifactRef | None = None
