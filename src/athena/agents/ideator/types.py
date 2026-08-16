from typing import Any, Literal

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef
from athena.core.research_models import Hypothesis


class _Draft(BaseModel):
    statement: str = Field(min_length=1, description="A falsifiable claim.")
    intervention: str = Field(min_length=1, description="One testable change.")
    expected_effect: str = Field(min_length=1, description="A measurable effect.")
    sources: list[str] = Field(min_length=1, description="Evidence references.")


class _Candidate(_Draft):
    key: str = Field(min_length=1)


class _ProposalBatch(BaseModel):
    hypotheses: list[_Draft] = Field(min_length=3, max_length=5)


class _Critique(BaseModel):
    key: str
    concerns: list[str] = Field(min_length=1)
    recommendation: Literal["retain", "revise", "reject"]


class _ReviewBatch(BaseModel):
    critiques: list[_Critique] = Field(min_length=1)


class _RevisionBatch(BaseModel):
    hypotheses: list[_Candidate] = Field(min_length=3, max_length=5)


class _JudgeDecision(BaseModel):
    candidate_keys: list[str] = Field(min_length=1)
    disposition: Literal["selected", "rejected", "merged"]
    reason: str = Field(min_length=1)


class _JudgedDraft(_Draft):
    candidate_keys: list[str] = Field(
        min_length=1,
        description="Candidate keys combined into this final hypothesis.",
    )


class _JudgeOutput(BaseModel):
    hypotheses: list[_JudgedDraft] = Field(min_length=3, max_length=5)
    decisions: list[_JudgeDecision] = Field(min_length=1)


class _TurnRequest(BaseModel):
    stage: Literal["proposal", "review", "revision", "judge"]
    role: Literal["debater", "judge"]
    agent_index: int = Field(ge=0)
    prompt: str = Field(min_length=1)


class DebateResult(BaseModel):
    """一轮 ideator 辩论的最终产出。"""

    hypotheses: list[Hypothesis] = Field(min_length=3, max_length=5)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    artifact_ref: ArtifactRef
