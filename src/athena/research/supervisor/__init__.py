"""Supervisor 确定性协调子系统（supervisor_design §2.2）。

ResearchRuntime 是公开门面与 composition root；本包提供其确定性子组件：
Planner 生成一步式 Plan、Validator 校验、Executor 执行、Journal 持久化、
StateStore 持有权威事实、Coordinator 驱动协调循环。
"""

from athena.research.supervisor.coordinator import SupervisorCoordinator
from athena.research.supervisor.executor import PlanExecutor, WorkerRuntime
from athena.research.supervisor.journal import PlanJournal, VersionConflict
from athena.research.supervisor.models import (
    ControlStatus,
    HumanRequest,
    OperationStatus,
    OperationType,
    PlanStatus,
    ResearchPhase,
    SupervisorOperation,
    SupervisorPlan,
)
from athena.research.supervisor.planner import DeterministicSupervisorPlanner
from athena.research.supervisor.state import (
    Budget,
    ProjectFacts,
    ProjectStateStore,
    project_phase,
)
from athena.research.supervisor.validator import PlanValidator, ValidationError

__all__ = [
    "Budget",
    "ControlStatus",
    "DeterministicSupervisorPlanner",
    "HumanRequest",
    "OperationStatus",
    "OperationType",
    "PlanExecutor",
    "PlanJournal",
    "PlanStatus",
    "PlanValidator",
    "ProjectFacts",
    "ProjectStateStore",
    "ResearchPhase",
    "SupervisorCoordinator",
    "SupervisorOperation",
    "SupervisorPlan",
    "ValidationError",
    "VersionConflict",
    "WorkerRuntime",
    "project_phase",
]
