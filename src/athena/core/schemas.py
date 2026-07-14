"""Athena 的共享事实模型。

设计约束：运行过程中仅在状态对象中保存必要的结构化字段；数据、日志、
模型、预测、配置和报告等大对象必须以不可变 ``ArtifactRef`` 表示。所有涉及
评分、排序、通过、拒绝、风险、质量、新颖性或可行性的结果，都应关联版本化
rubric，并保存逐项证据，而非只保存总分。
"""

from typing import Literal, TypeAlias

from pydantic import BaseModel, Field


ArtifactRef: TypeAlias = str
CommitHash: TypeAlias = str
ExecutionId: TypeAlias = str


class MetricSpec(BaseModel):
    """主指标的规范名称与优化方向；扩展指标由运行配置 artifact 记录。"""
    name: str = Field(description="Canonical metric name.")
    direction: Literal["maximize", "minimize"] = Field(description="Optimization direction.")


class TaskMetaData(BaseModel):
    """任务解析阶段产生的最小任务事实。

    后续实验必须遵守 ``constraints``，并以 ``primary_metric`` 作为主要选择
    指标；分类、回归、聚类、异常检测、预测、降维、排序和非结构化任务均由
    ``task_type`` 与 ``data_type`` 共同描述。
    """
    task_type: str = Field(description="Task type, such as classification or regression.")
    data_type: str = Field(description="Primary data modality.")
    target_vars: list[str] = Field(default_factory=list, description="Target variables. Empty for unsupervised tasks.")
    primary_metric: MetricSpec = Field(description="Primary selection metric.")
    constraints: list[str] = Field(default_factory=list, description="Hard constraints that every experiment must preserve.")


class DataCard(BaseModel):
    """不可变原始数据集及其轻量画像的索引。

    数据分析前须保存原始副本；数据切分、schema 与抽样/清洗产物不应覆盖原始
    数据，而应各自以 artifact 引用和内容指纹关联。
    """
    dataset_ref: ArtifactRef = Field(description="Immutable raw dataset reference.")
    fingerprint: str = Field(description="Content fingerprint.")
    schema_ref: ArtifactRef = Field(description="Schema and lightweight profile reference.")
    split_manifest_ref: ArtifactRef | None = Field(default=None, description="Immutable split manifest reference.")


class RecordNode(BaseModel):
    """记录树节点的公共最小字段。

    假设树和实验检查点树应复用这一父子关系与 artifact 载荷模式；树实现负责
    异步持久化、路径查询和分支，而模型本身不保存大载荷。
    """
    node_id: str = Field(description="Unique node identifier.")
    parent_ids: list[str] = Field(default_factory=list, description="Parent node identifiers.")
    status: str = Field(description="Node lifecycle status.")
    payload_ref: ArtifactRef = Field(description="Immutable node payload reference.")


class Hypothesis(RecordNode):
    """可证伪的研究假设及支持与反对证据引用。

    假设应说明最小干预和可测量预期效果，供纯算法 Elo 排序、实验选择、复现和
    消融使用；任何可行性或新颖性判断仍需绑定 rubric 与逐项证据。
    """
    statement: str = Field(description="Falsifiable hypothesis statement.")
    intervention: str = Field(description="Minimal change to test.")
    expected_effect: str = Field(description="Expected measurable effect.")
    evidence_refs: list[ArtifactRef] = Field(default_factory=list, description="Supporting and opposing evidence references.")


class ExperimentPlan(BaseModel):
    """单次实验相对父运行的最小变更与验收约定。

    ``acceptance_rule`` 必须引用冻结评估器和版本化 rubric 所定义的规则；资源
    预算限制运行时间、硬件和重试范围，具体完整配置保存在 ``run_config_ref``。
    """
    hypothesis_id: str | None = Field(default=None, description="Hypothesis tested by this experiment.")
    kind: str = Field(description="Prototype, optimize, ablate, replicate, aggregate, or debug.")
    change: str = Field(description="Minimal intended change from the parent run.")
    run_config_ref: ArtifactRef = Field(description="Immutable run configuration reference.")
    budget: dict = Field(description="Runtime and resource limits.")
    acceptance_rule: str = Field(description="Rule used to accept the experiment result.")


class AthenaThread(BaseModel):
    """可恢复的 Agent 上下文边界，而非工作流调度器。

    线程历史由 StateStore 保存，过长上下文压缩为 ``context_ref`` artifact；并发
    通过多个线程实现，同一线程同时只能运行一个 Turn。
    """
    thread_id: str = Field(description="Unique thread identifier.")
    session_id: str = Field(description="Owning session identifier.")
    status: str = Field(description="Thread lifecycle status.")
    context_ref: ArtifactRef = Field(description="Resumable context artifact reference.")


class AthenaTurn(BaseModel):
    """线程内一次明确任务的异步执行记录。

    请求、事件和结构化结果均通过 artifact 交接；子线程只能返回结果 artifact，
    不得直接改写父线程历史。
    """
    turn_id: str = Field(description="Unique turn identifier.")
    thread_id: str = Field(description="Owning thread identifier.")
    request_ref: ArtifactRef = Field(description="Request artifact reference.")
    status: str = Field(description="Turn lifecycle status.")
    result_ref: ArtifactRef | None = Field(default=None, description="Structured result artifact reference.")
