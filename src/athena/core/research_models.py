"""Models owned by Athena's core research tree."""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.contracts import ArtifactRef, NonBlankText

HypothesisStatus: TypeAlias = Literal[
    "PROPOSED",  # 刚刚提出假设
    "SUPPORTED",  # 支持假设
    "REFUTED",  # 不支持假设
    "INCONCLUSIVE",  # 实验无有效证据，无法支持或证伪
    "REJECTED",  # 彻底拒绝
]

MetricDirection: TypeAlias = Literal["maximize", "minimize"]
MetricSource: TypeAlias = Literal["human", "official", "protocol", "unresolved"]


class TaskMissingItem(BaseModel):
    """One actionable gap found before research is allowed to start."""

    model_config = ConfigDict(extra="forbid", strict=True)

    field: NonBlankText
    severity: Literal["critical", "warning"]
    reason: NonBlankText
    question: NonBlankText | None = None


class TaskUnderstanding(BaseModel):
    """Shared task-understanding and readiness contract for Supervisor and GUI."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = ""
    dataset: str = ""
    target: str = ""
    task_type: Literal[
        "classification", "regression", "vision", "generation", "other"
    ] = "other"
    primary_metric: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    direction: MetricDirection | None = None
    metric_source: MetricSource = "unresolved"
    human_primary_metric: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    human_direction: MetricDirection | None = None
    official_primary_metric: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    official_direction: MetricDirection | None = None
    protocol_primary_metric: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$"
    )
    protocol_direction: MetricDirection | None = None
    evaluation_plan: str = ""
    constraints: list[str] = Field(default_factory=list)
    readiness: Literal["READY", "NEEDS_INPUT"] = "NEEDS_INPUT"
    missing_items: list[TaskMissingItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_metric_provenance(self) -> "TaskUnderstanding":
        if self.metric_source == "unresolved":
            if self.primary_metric is not None or self.direction is not None:
                raise ValueError(
                    "unresolved task understanding cannot guess metric or direction"
                )
        else:
            if self.primary_metric is None:
                raise ValueError("a resolved metric source requires primary_metric")
            source_metric = getattr(self, f"{self.metric_source}_primary_metric")
            source_direction = getattr(self, f"{self.metric_source}_direction")
            if source_metric != self.primary_metric:
                raise ValueError(
                    f"{self.metric_source}_primary_metric must match primary_metric"
                )
            if source_direction != self.direction:
                raise ValueError(f"{self.metric_source}_direction must match direction")
        for source in ("human", "official", "protocol"):
            metric = getattr(self, f"{source}_primary_metric")
            source_direction = getattr(self, f"{source}_direction")
            if metric is None and source_direction is not None:
                raise ValueError(f"{source}_direction requires {source}_primary_metric")
        return self


class Hypothesis(BaseModel):
    """一个可通过实验验证或证伪的机器学习假设。"""

    statement: NonBlankText = Field(description="为什么认为这个改动可能有效")
    intervention: NonBlankText = Field(description="实验中具体改变什么")
    expected_effect: NonBlankText = Field(description="预期指标如何变化")
    status: HypothesisStatus = "PROPOSED"
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    id: str | None = Field(default=None, description="Unique hypothesis identifier")
    parent_id: str | None = Field(
        default=None, description="Parent experiment ID in ResearchTree"
    )
    supersedes: list[str] = Field(default_factory=list)
    priority: float = Field(default=1000.0, allow_inf_nan=False)
    order: int | None = Field(default=None, ge=0)
    patience: int = Field(default=0, ge=0)
    turn_limit: int | None = Field(default=None, ge=0)
    cost: float = Field(
        default=0.0,
        ge=0.0,
        description="Normalized expected cost of running this intervention",
    )
    sources: list[str] = Field(
        default_factory=list, description="Paper URLs or model repos"
    )
    rubric_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="Aggregated four-dimension hypothesis priority score",
    )
    rubric_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact containing the complete hypothesis priority review",
    )


class HypothesisBatch(BaseModel):
    """结构化 Ideator 输出：一组可验证/可证伪的假设（search.register_hypotheses 消费）。

    ``eda_request`` 是可选的自然语言补充 EDA 请求：Ideator 认为现有 EDA 不足以
    支撑可靠假设时填写，触发 SEARCH 阶段动态调用 Data Agent 把补充分析写回
    EDA 目录；为 null/空则不需要补充。
    """

    hypotheses: list[Hypothesis] = Field(default_factory=list)
    eda_request: str | None = Field(
        default=None, description="Optional additional-EDA request for the Data Agent"
    )


class EdaResult(BaseModel):
    """Data Agent 动态 EDA 的结构化输出：本次补充分析写了什么。"""

    summary: NonBlankText = Field(description="One-sentence summary of what was added")


class ExperimentPlan(BaseModel):
    """实验计划 — 描述如何修改代码目录以验证假设，含评价标准和资源预算。

    生成计划前需要明确工作目录（由程序控制而非 LLM 输出）。
    """

    kind: str
    change: str = Field(
        description="根据假设，Plan应该如何改变目录从而完成我们的实验部分"
    )
    rubrics: list[str] = Field(
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
