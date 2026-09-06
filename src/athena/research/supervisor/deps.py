"""Focused dependency groups for research supervision."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from athena.core.agent.agent_runtime import AgentRuntime
    from athena.core.contracts import ArtifactStore, CommitHash
    from athena.core.research_models import Hypothesis
    from athena.core.workspace import GitWorkspace
    from athena.research.contracts import GeneralTurnOutcome
    from athena.research.experiment_documents import DocumentProjector
    from athena.research.supervisor.experiment import PlanTurnResult
    from athena.research.supervisor.plans import PlanState
    from athena.research.supervisor.prepare import PrepareResult
    from athena.core.research_tree import ResearchTree
    from athena.research.supervisor.scheduling import Scheduler

PlanTurn = Callable[[str, "PlanState"], Awaitable["PlanTurnResult"]]
SupervisorTurn = Callable[[str], Awaitable[str]]
Publish = Callable[[Literal["output", "state"], dict[str, object]], Awaitable[None]]
PreparePhase = Callable[[], Awaitable["PrepareResult"]]
_PrepareResumeIsAttested = Callable[[], Awaitable[bool]]
ValidationPhase = Callable[["CommitHash", float], Awaitable[object]]
IdeatorTurn = Callable[[int], Awaitable[list["Hypothesis"]]]
GeneralTurn = Callable[[str, str | None], Awaitable["GeneralTurnOutcome"]]
PublishAgentEvent = Callable[[str, str, str, dict | None], Awaitable[None] | None]
ValidationMode = Literal["manual", "auto", "skip"]


def resolve_validation_mode(
    auto_validate: bool,
    skip_validate: bool,
) -> ValidationMode:
    """Collapse public validation flags into one Supervisor policy."""
    if skip_validate:
        return "skip"
    return "auto" if auto_validate else "manual"


@dataclass(frozen=True, slots=True)
class SupervisorPaths:
    """Locate durable supervisor state and project workspaces."""

    project_root: Path
    state_path: Path
    tree_path: Path


@dataclass(frozen=True, slots=True)
class SupervisorRuntime:
    """Provide the artifact, Agent, and Git runtimes used by Plans."""

    store: "ArtifactStore"
    agents: "AgentRuntime"
    workspaces: "GitWorkspace"
    documents: "DocumentProjector"


@dataclass(frozen=True, slots=True)
class ResearchActions:
    """Provide model-backed research turns without owning their results."""

    plan: PlanTurn
    supervisor: SupervisorTurn
    ideator: IdeatorTurn | None = None
    general: GeneralTurn | None = None
    publish_agent_event: PublishAgentEvent | None = None


@dataclass(slots=True)
class PhaseActions:
    """Provide phase entry points and outward event callbacks."""

    publish: Publish
    prepare: PreparePhase | None = None
    validation: ValidationPhase | None = None
    prepare_resume_is_attested: _PrepareResumeIsAttested | None = None
    validation_mode: ValidationMode = "manual"


_PlanRecovery = Callable[
    ["ResearchState", "ResearchTree", Callable[[str], bool], Callable[[str], bool]],
    "ResearchState",
]


@dataclass(slots=True)
class SearchServices:
    """Own SEARCH scheduling and comparison policy services."""

    scheduler: "Scheduler"
    recovery: _PlanRecovery
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = 0.0
    on_plan_settled: Callable[[str], Awaitable[None]] | None = None


@dataclass(slots=True)
class SupervisorDeps:
    """Expose five focused dependency groups to supervisor collaborators."""

    paths: SupervisorPaths
    runtime: SupervisorRuntime
    research: ResearchActions
    phases: PhaseActions
    search: SearchServices
