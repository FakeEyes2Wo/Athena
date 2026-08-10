"""ProjectStateStore — 权威项目事实与版本（supervisor_design §4/§9）。

研究阶段完全由已提交事实投影，不持久化可修改的 ``_phase`` 字段；控制状态与
研究阶段正交。事实与预算经 PlanJournal 持久化，state_version 由 Journal 维护。
"""

from pydantic import BaseModel

from athena.core.contracts import ArtifactRef
from athena.research.supervisor.journal import PlanJournal
from athena.research.supervisor.models import (
    ControlStatus,
    ResearchPhase,
)


class ProjectFacts(BaseModel):
    """项目权威事实 refs（每次读取时从 Journal 投影）。

    字段名即持久化 fact key；只有被接受（ACCEPT/COMMITTED）的 Artifact 才会提交
    为 ref 事实，评审拒绝或未批准不产生事实。
    """

    task_ref: ArtifactRef | None = None
    execution_config_ref: ArtifactRef | None = None
    dataset_role_proposal_ref: ArtifactRef | None = None
    dataset_role_review_ref: ArtifactRef | None = None
    dataset_manifest_ref: ArtifactRef | None = None
    eval_spec_ref: ArtifactRef | None = None
    eda_report_ref: ArtifactRef | None = None
    eda_review_ref: ArtifactRef | None = None
    baseline_experiment_ref: ArtifactRef | None = None
    graph_ref: ArtifactRef | None = None
    ranking_round_ref: ArtifactRef | None = None
    sota_experiment_ref: ArtifactRef | None = None
    search_stop_ref: ArtifactRef | None = None
    ablation_decision_ref: ArtifactRef | None = None
    ablation_summary_ref: ArtifactRef | None = None
    final_test_attempt_ref: ArtifactRef | None = None
    validation_result_ref: ArtifactRef | None = None


# PREPARE 被接受后进入 SEARCH 所需的事实（全部提交才视为 PREPARE 完成）。
_PREPARE_GATES = (
    "dataset_role_review_ref",
    "dataset_manifest_ref",
    "eval_spec_ref",
    "eda_review_ref",
    "baseline_experiment_ref",
)

# ProjectFacts 的持久化字段名（facts() 按此投影，顺序无关紧要）。
_REF_FIELDS = (
    "task_ref",
    "execution_config_ref",
    "dataset_role_proposal_ref",
    "dataset_role_review_ref",
    "dataset_manifest_ref",
    "eval_spec_ref",
    "eda_report_ref",
    "eda_review_ref",
    "baseline_experiment_ref",
    "graph_ref",
    "ranking_round_ref",
    "sota_experiment_ref",
    "search_stop_ref",
    "ablation_decision_ref",
    "ablation_summary_ref",
    "final_test_attempt_ref",
    "validation_result_ref",
)


class Budget(BaseModel):
    """多维硬预算（supervisor_design §4.3，首版取核心几项）。"""

    max_total_plans: int = 100
    plans_used: int = 0
    max_search_experiments: int = 20
    search_experiments_used: int = 0
    max_consecutive_no_improvement: int = 5
    consecutive_no_improvement: int = 0

    @property
    def plans_remaining(self) -> int:
        return self.max_total_plans - self.plans_used

    @property
    def search_experiments_remaining(self) -> int:
        return self.max_search_experiments - self.search_experiments_used

    def can_plan(self) -> bool:
        """是否还能创建新 Plan（不含已用次数）。"""
        return self.plans_used < self.max_total_plans

    def consume_plan(self) -> None:
        """消费一次 Plan 预算；耗尽后 Planner 不得继续同类返工。"""
        self.plans_used += 1

    def consume_search_experiment(self) -> None:
        self.search_experiments_used += 1

    def record_no_improvement(self, improved: bool) -> None:
        """按 SEARCH round 计数；本轮有候选成为新 SOTA 则归零。"""
        self.consecutive_no_improvement = (
            0 if improved else (self.consecutive_no_improvement + 1)
        )

    @property
    def search_exhausted(self) -> bool:
        """搜索预算或连续无改进上限耗尽。"""
        return (
            self.search_experiments_remaining <= 0
            or self.consecutive_no_improvement >= self.max_consecutive_no_improvement
        )


def project_phase(facts: ProjectFacts) -> ResearchPhase:
    """从已提交事实投影研究阶段（supervisor_design §4.1）。"""
    if facts.task_ref is None:
        return ResearchPhase.IDLE
    if not all(getattr(facts, gate) for gate in _PREPARE_GATES):
        return ResearchPhase.PREPARE
    if facts.sota_experiment_ref is None or facts.search_stop_ref is None:
        return ResearchPhase.SEARCH
    if facts.validation_result_ref is None:
        return ResearchPhase.VALIDATE
    return ResearchPhase.COMPLETED


class ProjectStateStore:
    """权威项目事实与预算的读写边界；阶段投影只读，控制状态正交。"""

    def __init__(self, journal: PlanJournal) -> None:
        self._journal = journal

    @property
    def journal(self) -> PlanJournal:
        return self._journal

    def facts(self) -> ProjectFacts:
        """从 Journal 重新投影权威事实（不含进程内缓存）。"""
        raw = self._journal.all_facts()
        return ProjectFacts(**{name: self._ref(raw, name) for name in _REF_FIELDS})

    @staticmethod
    def _ref(raw: dict[str, object], key: str) -> ArtifactRef | None:
        """取出字符串事实 ref；缺失或非字符串返回 None。"""
        value = raw.get(key)
        return value if isinstance(value, str) else None

    def control_status(self) -> ControlStatus:
        """控制状态投影：存储状态 + 开放 HumanRequest 投影 WAITING_FOR_HUMAN（§8.1）。

        运行中且有开放人工请求 → WAITING_FOR_HUMAN；用户主动设置的状态
        （PAUSED/CANCELLED）优先，不被请求覆盖。
        """
        execution_id = self._journal.active_execution_id()
        if execution_id is None:
            execution_id = self._journal.latest_execution_id()
        if execution_id is None:
            return ControlStatus.RUNNING
        stored = self._journal.execution_status(execution_id)
        if stored is None:
            return ControlStatus.RUNNING
        if stored is ControlStatus.RUNNING and self._journal.open_human_requests(
            execution_id
        ):
            return ControlStatus.WAITING_FOR_HUMAN
        return stored

    def set_control_status(self, status: ControlStatus) -> None:
        execution_id = self._journal.active_execution_id()
        if execution_id is not None:
            self._journal.set_execution_status(execution_id, status)

    def budget(self) -> Budget:
        raw = self._journal.get_fact("budget")
        return Budget.model_validate(raw) if raw else Budget()

    def save_budget(self, budget: Budget) -> None:
        self._journal.commit_facts(
            {"budget": budget.model_dump(mode="json")},
            self._journal.snapshot_version(),
        )

    def commit_facts(self, facts: dict[str, object]) -> int:
        """提交事实并推进 state_version（调用方需持有当前版本预期）。"""
        return self._journal.commit_facts(facts, self._journal.snapshot_version())
