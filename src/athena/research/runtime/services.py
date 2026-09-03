"""Functional dependency groups composed by ``ResearchRuntime``."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.execution.runtime import ExecutionRuntime
from athena.kaggle import KaggleStack
from athena.research.evaluation import TrustedEvaluator
from athena.research.literature.paper_rag.search import RetrievalSession
from athena.research.literature.survey import SurveyStack
from athena.research.prepare.authority import BaselineAuthorityStore
from athena.research.runtime.events import RuntimeEvents
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor

if TYPE_CHECKING:
    from athena.execution.compute_config import ComputeConfig
    from athena.execution.pool import GpuPool, Lease
    from athena.research.runtime.phase_runner import PhaseRunner
    from athena.research.turns.runner import AgentTurnRunner


@dataclass
class ResearchInfrastructure:
    """Process-wide storage, execution, and Agent infrastructure."""

    store: LocalArtifactStore
    registry: AgentTypeRegistry
    agents: AgentRuntime
    execution: ExecutionRuntime
    git: LocalGitWorkspace
    events: RuntimeEvents
    scripts: DataScriptRunner
    evaluator: TrustedEvaluator
    baseline_authority: BaselineAuthorityStore | None = None


@dataclass
class DurableResearch:
    """Durable research state and experiment history."""

    tree: ResearchTree
    state: ResearchState


@dataclass
class ResearchWorkflow:
    """Connected lifecycle coordinators and clarification boundary."""

    supervisor: Supervisor | None = None
    agent_turns: "AgentTurnRunner | None" = None
    phases: "PhaseRunner | None" = None
    clarification: object | None = None


@dataclass
class ResearchServices:
    """Long-lived runtime dependencies grouped by responsibility."""

    infrastructure: ResearchInfrastructure
    durable: DurableResearch
    workflow: ResearchWorkflow = field(default_factory=ResearchWorkflow)


@dataclass
class LifecycleSession:
    """Mutable lifecycle values for one Runtime process."""

    task_text: str = ""
    provider: object | None = None
    task: asyncio.Task | None = None
    started: bool = False
    confirmation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class SurveySession:
    """Caches owned by optional literature survey work."""

    task: asyncio.Task | None = None
    stack: SurveyStack | None = None
    corpus_sessions: list[RetrievalSession] = field(default_factory=list)


@dataclass
class ComputeSession:
    """Mutable compute placement and active remote leases."""

    data_root: Path | None = None
    config: "ComputeConfig | None" = None
    pool: "GpuPool | None" = None
    leases: dict[str, "Lease"] = field(default_factory=dict)


@dataclass
class RuntimeOptions:
    """Runtime-changeable research policy and lazy Kaggle cache."""

    ideation: Literal["ideageneration", "baseline", "debate"] = "ideageneration"
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = 0.0
    auto_validate: bool = False
    kaggle: KaggleStack | None = None


@dataclass
class ResearchSession:
    """Transient values grouped by lifecycle, survey, compute, and policy."""

    lifecycle: LifecycleSession
    survey: SurveySession = field(default_factory=SurveySession)
    compute: ComputeSession = field(default_factory=ComputeSession)
    options: RuntimeOptions = field(default_factory=RuntimeOptions)


__all__ = [
    "ComputeSession",
    "DurableResearch",
    "LifecycleSession",
    "ResearchInfrastructure",
    "ResearchServices",
    "ResearchSession",
    "ResearchWorkflow",
    "RuntimeOptions",
    "SurveySession",
]
