"""Immutable configuration and path layout for the research runtime."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from athena.execution.compute_config import ComputeConfig
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S


@dataclass(frozen=True)
class ResearchPaths:
    """Filesystem layout derived from project/state roots."""

    root: Path
    athena: Path
    workspaces: Path
    state: Path
    tree: Path
    sessions: Path


@dataclass(frozen=True)
class SearchLimits:
    """Search budget knobs owned by the durable ResearchState."""

    search_limit: int = 10
    concurrency: int = 1
    ideator_count: int = 3
    hypotheses_per_ideator: int = 2


@dataclass(frozen=True)
class SurveyConfig:
    """Optional background literature-survey knobs."""

    enabled: bool = False
    query: str = ""
    max_papers: int = 20
    search_top_k: int = 0
    max_seconds: float = 0.0


@dataclass(frozen=True)
class ResearchConfig:
    """Everything ResearchRuntime needs to know that does not change per run."""

    paths: ResearchPaths
    model: str | None = None
    client: Any = None
    task: str = ""
    auto_seed_task: bool = False
    search: SearchLimits = field(default_factory=SearchLimits)
    survey: SurveyConfig = field(default_factory=SurveyConfig)
    auto_validate: bool = False
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = 0.0
    ideation: Literal["ideageneration", "baseline", "debate"] = "ideageneration"
    # Optional platform-owned local CSV dataset contract. When set, PREPARE
    # materializes train/search/final splits instead of letting the evaluator
    # agent create the split itself.
    dataset_path: Path | None = None
    target_column: str | None = None
    split_seed: int = 0
    # Compute resources / remote GPU execution.
    data_root: Path | None = None
    experiment_timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S
    compute: ComputeConfig | None = None
    prepare_phase: Any = None
    validation_phase: Any = None
    plan_turn: Any = None
    ask_user: Any = None
