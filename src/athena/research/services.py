"""Shared services and per-run session state for the research runtime.

The goal is to keep ``ResearchRuntime`` small: it composes these objects once
and stores only ``_config``, ``_services`` and ``_session``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.execution.runtime import ExecutionRuntime
from athena.kaggle import KaggleStack
from athena.research.evaluation import TrustedEvaluator
from athena.research.paper_rag.search import RetrievalSession
from athena.research.runtime_events import RuntimeEvents
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from athena.research.survey import SurveyStack

if TYPE_CHECKING:
    from athena.execution.compute_config import ComputeConfig
    from athena.execution.pool import GpuPool, Lease
    from athena.research.agent_turn_runner import AgentTurnRunner
    from athena.research.phase_runner import PhaseRunner


@dataclass
class ResearchServices:
    """Long-lived infrastructure shared by all research components."""

    store: LocalArtifactStore
    registry: AgentTypeRegistry
    agents: AgentRuntime
    execution: ExecutionRuntime
    git: LocalGitWorkspace
    events: RuntimeEvents
    scripts: DataScriptRunner
    evaluator: TrustedEvaluator
    tree: ResearchTree
    state: ResearchState
    supervisor: Supervisor | None = None
    agent_turns: AgentTurnRunner | None = None
    phase_runner: PhaseRunner | None = None


@dataclass
class ResearchSession:
    """Transient state that belongs to one running ResearchRuntime process."""

    provider: object | None = None
    reasoning_provider: object | None = None
    task: asyncio.Task | None = None
    started: bool = False
    survey_task: asyncio.Task | None = None
    survey_stack: SurveyStack | None = None
    corpus_sessions: list[RetrievalSession] = field(default_factory=list)
    task_text: str = ""
    kaggle_stack: KaggleStack | None = None
    data_root: Path | None = None
    compute: ComputeConfig | None = None
    pool: GpuPool | None = None
    leases: dict[str, Lease] = field(default_factory=dict)
