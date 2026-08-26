"""Injected dependencies for the Supervisor facade."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.research_models import Hypothesis
from athena.core.workspace import GitWorkspace
from athena.research.contracts import GeneralTurnOutcome
from athena.research.supervisor.experiment import PlanTurnResult
from athena.research.supervisor.plans import PlanState
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler

PlanTurn = Callable[[str, PlanState], Awaitable[PlanTurnResult]]
SupervisorTurn = Callable[[str], Awaitable[str]]
Publish = Callable[[Literal["output", "state"], dict[str, object]], Awaitable[None]]
PreparePhase = Callable[[], Awaitable[PrepareResult]]
ValidationPhase = Callable[[CommitHash, float], Awaitable[object]]
IdeatorTurn = Callable[[int], Awaitable[list[Hypothesis]]]
GeneralTurn = Callable[[str, str | None], Awaitable[GeneralTurnOutcome]]
PublishAgentEvent = Callable[[str, str, str, dict | None], Awaitable[None] | None]


@dataclass
class SupervisorDeps:
    """All injected collaborators and callbacks owned by a Supervisor."""

    project_root: Path
    state_path: Path
    tree_path: Path
    store: ArtifactStore
    agents: AgentRuntime
    workspaces: GitWorkspace
    scheduler: Scheduler
    recovery: Recovery
    evaluator_ref: ArtifactRef | None
    direction: Literal["maximize", "minimize"]
    tolerance: float
    run_plan_turn: PlanTurn
    run_supervisor_turn: SupervisorTurn
    publish: Publish
    final_evaluator_ref: ArtifactRef | None = None
    auto_validate: bool = False
    run_ideator_turn: IdeatorTurn | None = None
    run_general_turn: GeneralTurn | None = None
    run_prepare_phase: PreparePhase | None = None
    run_validation_phase: ValidationPhase | None = None
    publish_agent_event: PublishAgentEvent | None = None


__all__ = [
    "GeneralTurn",
    "IdeatorTurn",
    "PlanTurn",
    "PreparePhase",
    "Publish",
    "PublishAgentEvent",
    "SupervisorDeps",
    "SupervisorTurn",
    "ValidationPhase",
]
