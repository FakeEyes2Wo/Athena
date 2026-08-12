"""Athena 研究流程的 Artifact payload 合同（supervisor_imp_docs Task 1）。

跨任务的数据结构在此冻结：ExecutionConfig、数据集角色/清单、DataScript Bundle、
EvalSpec、EDA、baseline、候选评估、ranking、ablation 与 final-test。领域服务
（data_service / script_runner / evaluation / search / validation）与 Supervisor
只消费这些 Pydantic 模型；字段改动必须同步设计文档与所有消费者。

首版只冻结合同的形状与硬约束（enum/范围/非空），业务语义由 LLM 生成的脚本承担。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef, NonBlankText


class ExecutionConfig(BaseModel):
    """一次 execution 的冻结配置（interactive/auto 共享，supervisor_design §4.3）。

    IDEATION/搜索维度有设计上限；K-fold 只提供稳定性证据，唯一 SOTA 只按 test
    score 排序。
    """

    interaction_mode: Literal["interactive", "auto"] = "interactive"
    ideator_count: int = Field(default=3, ge=1, le=8)
    hypotheses_per_ideator: int = Field(default=2, ge=1, le=5)
    selected_hypotheses_per_round: int = Field(default=2, ge=1, le=4)
    kfold_policy: Literal["required", "auto", "disabled"] = "auto"
    k_folds: Literal[5] = 5
    ablation_mode: Literal["FULL_LINEAGE", "BASELINE_ONLY"] | None = None
    max_total_plans: int = Field(default=100, ge=1)
    max_search_experiments: int = Field(default=20, ge=1)
    max_repair_turns_per_agent: int = Field(default=8, ge=0)
    max_identical_failure_repeats: int = Field(default=3, ge=1)
    max_prepare_revisions: int = Field(default=2, ge=0)
    max_validation_environment_repairs: int = Field(default=2, ge=0)
    max_consecutive_no_improvement: int = Field(default=5, ge=1)
    max_execution_duration_seconds: int = Field(default=21600, ge=1)


class ServiceResult(BaseModel):
    """RUN_SERVICE 的返回值：结果 refs 与可提交 facts（Executor 消费）。"""

    result_refs: list[ArtifactRef] = Field(default_factory=list)
    facts: dict[str, object] = Field(default_factory=dict)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)


class SupervisorServices(Protocol):
    """Executor 的 RUN_SERVICE 静态服务注册契约（supervisor_design §5 白名单）。"""

    async def run(self, service: str, request: dict[str, object]) -> ServiceResult: ...


class DatasetRoleProposal(BaseModel):
    """DataAgent 对数据文件角色/目标列的提议（Task 5 消费）。"""

    role_proposal: str
    data_files: list[str] = Field(default_factory=list)
    target_column: str | None = None
    reasoning: str


class DatasetRoleReview(BaseModel):
    """ReflectionAgent 对角色提议的评审结论（独立 LLM，不读原始数据）。"""

    decision: Literal["ACCEPT", "REVISE"]
    findings: list[str] = Field(default_factory=list)


class DatasetManifest(BaseModel):
    """不可变原始数据 manifest：字节级 SHA-256 与列级结构不变量。

    ``columns``/``column_hashes``/``row_identity_hash`` 为结构性不变量记录
    （由实际读取数据的 LLM 脚本登记，平台本身不解析业务数据）。
    """

    manifest_id: NonBlankText
    source_root: str
    managed_root: str | None = (
        None  # 受管 raw snapshot 的绝对路径（worker 只读这份拷贝）
    )
    files: dict[str, str] = Field(default_factory=dict)  # 相对路径 -> sha256
    columns: dict[str, str] = Field(default_factory=dict)  # 列名 -> 类型
    column_hashes: dict[str, str] = Field(default_factory=dict)  # 列名 -> 值序列 sha256
    row_identity_hash: str | None = None  # 行身份序列 sha256
    split_boundaries: dict[str, object] = Field(default_factory=dict)


class DerivedDatasetManifest(BaseModel):
    """派生数据 manifest：只允许追加新列，原始列/值/行身份/split 完全不变。

    ``columns``/``column_hashes`` 为派生数据的全量列记录（含原始列），
    accept_derived 据此校验原始列未被改动。

    增量特征（supervisor-incremental-feature-dataset §One Data Concept）：
    ``files`` 记录每个已接受增量文件；``derived_columns`` 记录所有可用派生列；
    ``column_files`` 把派生列映射到其所在增量文件（跨文件可解析）；
    ``enabled_derived_columns`` 是唯一特征选择机制。禁用特征 = 新 manifest（不同 id），
    不删除/改写文件。既有 manifest 这两个字段空默认 → 视为"无 enabled 特征"视图。
    """

    manifest_id: NonBlankText
    parent_manifest_id: NonBlankText
    files: dict[str, str] = Field(default_factory=dict)
    columns: dict[str, str] = Field(default_factory=dict)
    column_hashes: dict[str, str] = Field(default_factory=dict)
    row_identity_hash: str | None = None
    split_boundaries: dict[str, object] = Field(default_factory=dict)
    derived_columns: list[str] = Field(default_factory=list)
    column_files: dict[str, str] = Field(
        default_factory=dict
    )  # 列名 -> 增量文件相对路径
    enabled_derived_columns: list[str] = Field(default_factory=list)


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


class EvalSpec(BaseModel):
    """评估合同：主指标、方向与 K-fold 策略（InitAgent 产出）。"""

    eval_spec_id: NonBlankText
    primary_metric: str
    direction: Literal["maximize", "minimize"]
    kfold_policy: Literal["required", "auto", "disabled"] = "auto"
    k_folds: int = Field(default=5, ge=1)
    target_test_score: float | None = None


class EDAReview(BaseModel):
    """ReflectionAgent 对 EDA 报告的评审结论（rubric 覆盖完整性/证据）。"""

    decision: Literal["ACCEPT", "REVISE"]
    rubric: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class EDARepairFailure(BaseModel):
    """EDA 脚本执行失败的紧凑记录（eda-auto-repair-design §Agent Outcome Contract）。

    ``failure_signature`` 是 (command, exit_code, stderr) 的规范化哈希，供
    Supervisor 识别重复失败并做有界退避。只保留最新一条失败记录。
    """

    attempt: int
    command: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    stderr: str = ""
    failure_signature: str


class EDAAttemptOutcome(BaseModel):
    """DataAgent 一次 EDA 尝试的结果（成功 turn，不是失败 run）。

    ``succeeded`` → ``bundle_ref`` 指向含 report.md/figures 的 DataAnalysis bundle；
    ``repairable_failure`` → ``failure_ref`` 指向 :class:`EDARepairFailure`。
    Supervisor 读此合同决定提升 EDA 或进入修复 follow-up（设计 §Plan State Machine）。
    """

    status: Literal["succeeded", "repairable_failure"]
    execution_id: str
    workspace: str
    repair_count: int = 0
    bundle_ref: ArtifactRef | None = None
    failure_ref: ArtifactRef | None = None
    failure_signature: str | None = None


class BaselinePlan(BaseModel):
    """Baseline Ideator 的方向与唯一推荐（CodeAgent 只实现 recommendation）。"""

    directions: list[str] = Field(default_factory=list)
    recommendation: str


class CandidateEvaluation(BaseModel):
    """单个候选实验的评估结果（trusted evaluator 产出）。"""

    candidate_id: NonBlankText
    test_score: float
    kfold_mean: float | None = None
    kfold_std: float | None = None
    direction: Literal["maximize", "minimize"]


class RankingRound(BaseModel):
    """一轮 SEARCH 的 append-only ranking 结果（唯一 SOTA 事务输入）。"""

    round_id: NonBlankText
    selected_ids: list[str] = Field(default_factory=list)
    deferred_ids: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    sota_experiment_id: str | None = None


class AblationSummary(BaseModel):
    """VALIDATE ablation 汇总（FULL_LINEAGE/BASELINE_ONLY 的 leave-one-out 结果）。"""

    summary_id: NonBlankText
    status: Literal["COMPLETE", "PARTIAL"]
    items: list[dict[str, object]] = Field(default_factory=list)


class FinalTestAttempt(BaseModel):
    """唯一 final-test 尝试的状态机记录（每 execution 最多一个逻辑 attempt）。

    创建 attempt 的同一事务冻结 SOTA/EvalSpec/dataset refs（design §2.5）；
    prediction/score Artifact 哈希在状态迁移时持久化，已有非空 score 不可被
    后续非空值覆盖（崩溃恢复只向前提交，不重训/重预测/重评分）。
    """

    attempt_id: NonBlankText
    execution_id: NonBlankText
    sota_experiment_id: NonBlankText
    eval_spec_version: NonBlankText
    status: Literal[
        "RESERVED",
        "RUNNING",
        "PREDICTIONS_WRITTEN",
        "SCORED",
        "COMMITTED",
        "FAILED_UNRECOVERABLE",
    ]
    sota_ref: ArtifactRef
    eval_spec_ref: ArtifactRef
    dataset_ref: ArtifactRef
    final_test_ref: ArtifactRef | None = None
    prediction_ref: ArtifactRef | None = None
    score_ref: ArtifactRef | None = None
    test_score: float | None = None
    final_test_score: float | None = None


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
    evidence_ref: ArtifactRef | None = None
