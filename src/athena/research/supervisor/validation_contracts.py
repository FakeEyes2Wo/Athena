"""Data contracts shared by the validation workflow and its callers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.contracts import ArtifactRef, ArtifactStore, CommitHash
from athena.core.tool_types import EmitEvent
from athena.core.workspace import GitWorkBranch, GitWorkspace
from athena.execution.runtime import ExecutionRuntime
from athena.research.evaluation import TrustedEvaluator
from athena.research.supervisor.plans import DEFAULT_EXPERIMENT_TIMEOUT_S

CheckpointValidation = Callable[[ArtifactRef], Awaitable[None]]
ValidationRecoveryAction = Literal["run", "score", "commit"]


class ValidationInput(BaseModel):
    """Immutable inputs identifying one logical validation attempt."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sota_commit: CommitHash
    reference_metric: float = Field(allow_inf_nan=False)
    direction: Literal["maximize", "minimize"]
    final_evaluator_ref: ArtifactRef
    validation_key: str = Field(min_length=1)
    sota_context: dict[str, Any] | None = None
    task_context: str = ""


class ValidationDiffReview(BaseModel):
    """Independent decision on a proposed runtime-only repair."""

    model_config = ConfigDict(extra="forbid", strict=True)

    accepted: bool
    reason: str = Field(min_length=1)


@dataclass
class ValidationDeps:
    """All injected collaborators owned by one validation phase."""

    agents: AgentRuntime
    git: GitWorkspace
    workspace: GitWorkBranch
    execution: ExecutionRuntime
    evaluator: TrustedEvaluator
    store: ArtifactStore
    independent_review: Callable[[str], Awaitable[ValidationDiffReview]]
    checkpoint: CheckpointValidation
    publish: EmitEvent | None = None


@dataclass
class ValidationOptions:
    """Per-attempt validation knobs outside the logical input."""

    timeout_s: int = DEFAULT_EXPERIMENT_TIMEOUT_S
    predict_features: Path | None = None
    data_csv: Path | None = None


@dataclass(frozen=True)
class PredictionRun:
    """One re-run of the frozen experiment and its packed predictions."""

    predictions_ref: ArtifactRef
    predictions_path: str


__all__ = [
    "CheckpointValidation",
    "PredictionRun",
    "ValidationDeps",
    "ValidationDiffReview",
    "ValidationInput",
    "ValidationOptions",
    "ValidationRecoveryAction",
]
