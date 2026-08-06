"""Agent cognition schemas — PlanNode, Plan, Checkpoint, Reflection, GuardRule,
GuardResult, TrackState, ToolCategory."""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PlanNodeStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanNodeRelation(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    ALTERNATIVE = "alternative"


class PlanNode(BaseModel):
    """A single node in the execution plan tree.

    Children can be sequential steps, parallel tasks, or alternative approaches.
    The ``relation`` field determines how children relate to each other.
    """

    id: str = Field(description="Unique node identifier, e.g. 'plan-xxx#3b'")
    parent_id: str | None = Field(
        default=None, description="Parent node; None for root"
    )
    title: str = Field(description="Short goal description")
    description: str = Field(
        default="", description="What to do, why, and how to judge completion"
    )
    status: PlanNodeStatus = PlanNodeStatus.PENDING
    relation: PlanNodeRelation = PlanNodeRelation.SEQUENTIAL
    children: list["PlanNode"] = Field(default_factory=list)
    checkpoint_ref: str | None = Field(
        default=None, description="Associated Checkpoint artifact reference"
    )
    evidence: str = Field(
        default="",
        description="What was actually done; filled when marking completed/failed",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Plan(BaseModel):
    """Top-level plan container holding the full execution tree."""

    plan_id: str = Field(description="Unique plan identifier")
    session_id: str = ""
    goal: str = Field(description="Top-level goal statement")
    root: PlanNode = Field(description="Root of the plan tree")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Checkpoint(BaseModel):
    """A milestone checkpoint tied to a PlanNode."""

    checkpoint_id: str
    plan_id: str
    node_id: str
    summary: str = Field(description="What was accomplished and discovered")
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Reflection(BaseModel):
    """A reflective review entry recording what went wrong and what changed."""

    reflection_id: str
    plan_id: str
    trigger: str = Field(description="Why this reflection was triggered")
    observations: str = Field(description="What actually happened")
    adjustment: str = Field(default="", description="Proposed plan adjustment")
    plan_changes: list[dict] = Field(
        default_factory=list, description="Concrete tree structure changes made"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GuardRule(BaseModel):
    """A hard constraint rule for the guard tool."""

    rule_id: str
    description: str = Field(
        description="Rule description, e.g. 'do not modify eval scripts'"
    )
    pattern: str = Field(description="Match pattern for detecting violations")
    severity: Literal["error", "warning"] = "error"


class GuardResult(BaseModel):
    """Result of a guard check — advisory, not blocking."""

    allowed: bool = True
    violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TrackState(BaseModel):
    """Current execution progress snapshot."""

    plan_id: str
    current_node_id: str | None = None
    active_nodes: list[str] = Field(
        default_factory=list,
        description="All node IDs currently in_progress",
    )
    completed_count: int = 0
    failed_count: int = 0
    pending_count: int = 0
    path_to_root: list[str] = Field(
        default_factory=list,
        description="Node IDs from current node up to root (inclusive)",
    )


class ToolCategory(str, Enum):
    """Category label for Athena tools, used for migration detection in hints."""

    SEARCH = "search"
    DOWNLOAD = "download"
    INSPECT = "inspect"
    COMPUTE = "compute"
    ARTIFACT = "artifact"
    COGNITION = "cognition"
