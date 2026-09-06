"""Immutable configuration and path layout for the research runtime."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from athena.execution.compute_config import ComputeConfig
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class ResearchPaths:
    """Filesystem layout derived from project/state roots."""

    root: Path
    athena: Path
    workspaces: Path

    @property
    def state(self) -> Path:
        """Return the durable research state path."""
        return self.athena / "state.json"

    @property
    def tree(self) -> Path:
        """Return the durable research tree path."""
        return self.athena / "research_tree.json"

    @property
    def sessions(self) -> Path:
        """Return the agent session-log directory."""
        return self.athena / "logs" / "sessions"

    @property
    def clarification(self) -> Path:
        """Return the canonical clarification draft path."""
        return self.athena / "clarification.json"

    @property
    def handoffs(self) -> Path:
        """Return the named handoff directory."""
        return self.athena / "handoffs"


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Search budget knobs owned by the durable ResearchState."""

    search_limit: int = 10
    concurrency: int = 1
    ideator_count: int = 3
    hypotheses_per_ideator: int = 2


@dataclass(frozen=True, slots=True)
class SurveyConfig:
    """Optional background literature-survey knobs."""

    enabled: bool = False
    query: str = ""
    max_papers: int = 20
    search_top_k: int = 0
    max_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Optional model provider selection and client override."""

    model: str | None = None
    client: Any = None


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """Task seeding, confirmation, and human-interaction policy."""

    text: str = ""
    auto_seed: bool = False
    confirmation_gate: bool = False
    auto_confirm: bool = False
    ask_user: Any = None


@dataclass(frozen=True, slots=True)
class ResearchPolicy:
    """Initial search/validation policy copied into mutable session state."""

    auto_validate: bool = False
    skip_validate: bool = False
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = 0.0
    ideation: Literal["ideageneration", "baseline", "debate"] = "ideageneration"


@dataclass(frozen=True, slots=True)
class DatasetConfig:
    """Optional platform-owned dataset split contract."""

    path: Path | None = None
    target_column: str | None = None
    split_seed: int = 0
    group_column: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Local/remote execution roots, limits, and compute placement."""

    data_root: Path | None = None
    experiment_timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S
    compute: ComputeConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAdapters:
    """Provider-less test adapters for phase and Plan execution."""

    prepare: Any = None
    validation: Any = None
    plan: Any = None


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """State location and durable session identity."""

    state_root: str | Path | None = None
    session_id: str = "default"


@dataclass(frozen=True, slots=True)
class ResearchOptions:
    """Research behavior selected for a runtime."""

    task: TaskConfig = field(default_factory=TaskConfig)
    search: SearchLimits = field(default_factory=SearchLimits)
    survey: SurveyConfig = field(default_factory=SurveyConfig)
    policy: ResearchPolicy = field(default_factory=ResearchPolicy)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Provider, execution, adapters, and optional integration boundaries."""

    provider: ProviderConfig = field(default_factory=ProviderConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    adapters: RuntimeAdapters = field(default_factory=RuntimeAdapters)
    broker: Any = None
    baseline_authority: Any = None
    environment_repair_actions: Any = None


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    """Everything ResearchRuntime needs to know that does not change per run."""

    paths: ResearchPaths
    session_id: str = "default"
    research: ResearchOptions = field(default_factory=ResearchOptions)
    dependencies: RuntimeDependencies = field(default_factory=RuntimeDependencies)
