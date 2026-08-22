"""Shared services and per-run session state for the research runtime.

The goal is to keep ``ResearchRuntime`` small: it composes these objects once
and delegates work to runners that receive a narrow ``ResearchContext`` instead
of reaching into the runtime's private fields.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.core.git_workspace import LocalGitWorkspace
from athena.core.research_tree import ResearchTree
from athena.execution.runtime import ExecutionRuntime
from athena.kaggle import KaggleStack
from athena.research.config import ResearchConfig
from athena.research.evaluation import TrustedEvaluator
from athena.research.paper_rag.search import RetrievalSession
from athena.research.runtime_events import RuntimeEvents
from athena.research.script_runner import DataScriptRunner
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from athena.research.survey import SurveyStack

if TYPE_CHECKING:
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
    task: asyncio.Task | None = None
    started: bool = False
    survey_task: asyncio.Task | None = None
    survey_stack: SurveyStack | None = None
    corpus_sessions: list[RetrievalSession] = field(default_factory=list)
    task_text: str = ""
    kaggle_stack: KaggleStack | None = None


@dataclass
class ResearchContext:
    """Narrow read-only view passed to runners instead of the full runtime."""

    config: ResearchConfig
    services: ResearchServices
    session: ResearchSession

    @property
    def state(self) -> ResearchState:
        return self.services.state

    @property
    def tree(self) -> ResearchTree:
        return self.services.tree

    @property
    def supervisor(self) -> Supervisor:
        assert self.services.supervisor is not None
        return self.services.supervisor

    @property
    def store(self) -> LocalArtifactStore:
        return self.services.store

    @property
    def agents(self) -> AgentRuntime:
        return self.services.agents

    @property
    def registry(self) -> AgentTypeRegistry:
        return self.services.registry

    @property
    def execution(self) -> ExecutionRuntime:
        return self.services.execution

    @property
    def events(self) -> RuntimeEvents:
        return self.services.events
