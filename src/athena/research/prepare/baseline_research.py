"""Typed research evidence required before a PREPARE baseline can run."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

RESEARCH_FILENAME = "BASELINE_RESEARCH.json"
DESIGN_FILENAME = "BASELINE_DESIGN.md"
VERIFICATION_FILENAME = "BASELINE_RESEARCH_VERIFICATION.json"
AUTHORITY_CITATION_THRESHOLD = 100

TrainingStrategy = Literal[
    "classical",
    "frozen_pretrained",
    "partial_finetune",
    "full_finetune",
    "train_from_scratch",
]

_SELECTED_RE = re.compile(r"(?mi)^Selected candidate:\s*`([^`]+)`\s*$")
_STRATEGY_RE = re.compile(
    r"(?mi)^Training strategy:\s*`(classical|frozen_pretrained|partial_finetune|"
    r"full_finetune|train_from_scratch)`\s*$"
)


class BaselineResearchError(RuntimeError):
    """Raised when baseline research files cannot form a trusted contract."""

    def __init__(self, message: str, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class DatasetAssessment(BaseModel):
    """Describe the data regime and the training strategy it supports."""

    model_config = ConfigDict(extra="forbid", strict=True)

    modality: Literal[
        "tabular",
        "image",
        "text",
        "audio",
        "video",
        "time_series",
        "multimodal",
        "other",
    ]
    task_type: str = Field(min_length=1)
    labeled_samples: int | None = Field(default=None, ge=0)
    effective_training_units: int | None = Field(default=None, ge=0)
    group_count: int | None = Field(default=None, ge=0)
    class_count: int | None = Field(default=None, ge=1)
    minority_class_samples: int | None = Field(default=None, ge=0)
    input_scale: str = Field(min_length=1)
    regime: Literal["tiny", "small", "adequate", "unknown"]
    recommended_strategy: TrainingStrategy
    evidence: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_unsafe_scratch(self) -> "DatasetAssessment":
        """Reject scratch training without an adequate labeled regime."""
        if (
            self.recommended_strategy == "train_from_scratch"
            and self.regime != "adequate"
        ):
            raise ValueError("train_from_scratch requires an adequate data regime")
        return self


class BaselineSource(BaseModel):
    """Identify one externally researched baseline candidate."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    method: str = Field(min_length=1)
    source_url: HttpUrl
    source_kind: Literal["paper", "official_implementation", "technical_reference"]
    paper_locator: str | None = None
    repository_url: HttpUrl | None = None
    publication_year: int | None = Field(default=None, ge=1)
    claimed_citation_count: int | None = Field(default=None, ge=0)
    relevance: str = Field(min_length=1)

    @field_validator("source_url", "repository_url", mode="before")
    @classmethod
    def reject_url_userinfo(cls, value: object) -> object:
        """Reject credentials embedded in source URLs."""
        if value is not None and (url := urlsplit(str(value))).username is not None:
            raise ValueError("source URLs must not contain credentials")
        return value


class CandidateDecision(BaseModel):
    """Record why one researched candidate was selected or rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str = Field(min_length=1)
    decision: Literal["selected", "rejected"]
    reason: str = Field(min_length=1)


class BaselineResearch(BaseModel):
    """Machine-readable research record consumed by the PREPARE gate."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    dataset: DatasetAssessment
    candidates: list[BaselineSource] = Field(min_length=1)
    decisions: list[CandidateDecision] = Field(min_length=1)
    selected_candidate_id: str = Field(min_length=1)
    search_queries: list[str] = Field(min_length=1)
    limitations: list[str]

    @model_validator(mode="after")
    def validate_contract(self) -> "BaselineResearch":
        """Validate candidate decisions, research breadth, and scratch evidence."""
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")

        decisions = {decision.candidate_id: decision for decision in self.decisions}
        if len(decisions) != len(self.decisions):
            raise ValueError("candidate decisions must be unique")
        if set(decisions) != set(candidate_ids):
            raise ValueError("every candidate must have exactly one decision")
        selected = [
            decision for decision in self.decisions if decision.decision == "selected"
        ]
        if len(selected) != 1 or selected[0].candidate_id != self.selected_candidate_id:
            raise ValueError("selected candidate and selected decision must match")

        if len(candidate_ids) < 2:
            queries = {query.strip() for query in self.search_queries if query.strip()}
            if len(queries) < 2 or not any(item.strip() for item in self.limitations):
                raise ValueError(
                    "one candidate requires two distinct queries and a limitation"
                )

        numeric_fields = (
            self.dataset.labeled_samples,
            self.dataset.effective_training_units,
            self.dataset.group_count,
            self.dataset.class_count,
            self.dataset.minority_class_samples,
        )
        if any(value is not None for value in numeric_fields) and not any(
            item.startswith(("eda:", "data_contract:", "data:", "calculation:"))
            for item in self.dataset.evidence
        ):
            raise ValueError(
                "numeric dataset facts require EDA, data, or calculation evidence"
            )

        if self.dataset.recommended_strategy == "train_from_scratch":
            evidence = self.dataset.evidence
            if not any(
                item.startswith(("eda:", "data_contract:", "data:", "calculation:"))
                for item in evidence
            ):
                raise ValueError("scratch training requires local data evidence")
            prefix = f"source:{self.selected_candidate_id}:"
            if not any(item.startswith(prefix) for item in evidence):
                raise ValueError("scratch training requires comparable source evidence")
        return self


class BaselineDesignSelection(BaseModel):
    """The two values the human-readable design must agree with."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    selected_candidate_id: str
    training_strategy: TrainingStrategy


class VerificationAttempt(BaseModel):
    """Record one platform source-verification route."""

    model_config = ConfigDict(extra="forbid", strict=True)

    route: Literal["git", "openalex"]
    success: bool
    diagnostic: str


class BaselineVerification(BaseModel):
    """Platform-owned proof that the selected candidate was independently checked."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    research_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_id: str = Field(min_length=1)
    route: Literal["git", "openalex"]
    verified_at: datetime
    repository_url: str | None = None
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    openalex_id: str | None = None
    title: str | None = None
    publication_year: int | None = Field(default=None, ge=1)
    cited_by_count: int | None = Field(default=None, ge=0)
    attempts: list[VerificationAttempt]

    @model_validator(mode="after")
    def validate_route_evidence(self) -> "BaselineVerification":
        """Require evidence fields and a successful attempt for the chosen route."""
        if not any(
            attempt.route == self.route and attempt.success for attempt in self.attempts
        ):
            raise ValueError(f"verification has no successful {self.route} attempt")
        if self.route == "git" and (not self.repository_url or not self.commit):
            raise ValueError("git verification requires repository_url and commit")
        if self.route == "openalex" and (
            not self.openalex_id or not self.title or self.cited_by_count is None
        ):
            raise ValueError(
                "openalex verification requires id, title, and citation count"
            )
        return self


@dataclass(frozen=True)
class BaselineArtifacts:
    """Parsed baseline research and its matching Markdown selection."""

    root: Path
    raw_research: bytes
    research: BaselineResearch
    design_text: str
    design: BaselineDesignSelection
    selected: BaselineSource


@dataclass(frozen=True)
class VerifiedBaseline:
    """Baseline artifacts paired with platform verification evidence."""

    artifacts: BaselineArtifacts
    verification: BaselineVerification


def research_sha256(raw: bytes) -> str:
    """Return the SHA-256 digest of the exact research file bytes."""
    return hashlib.sha256(raw).hexdigest()


def _design_selection(text: str) -> BaselineDesignSelection:
    selected = _SELECTED_RE.findall(text)
    strategy = _STRATEGY_RE.findall(text)
    if len(selected) != 1 or len(strategy) != 1:
        raise BaselineResearchError(
            "BASELINE_DESIGN.md must contain each selection marker exactly once"
        )
    return BaselineDesignSelection(
        selected_candidate_id=selected[0], training_strategy=strategy[0]
    )


def load_baseline_artifacts(root: Path) -> BaselineArtifacts:
    """Load and cross-check the machine-readable and Markdown baseline artifacts."""
    root = Path(root)
    research_path = root / RESEARCH_FILENAME
    design_path = root / DESIGN_FILENAME
    try:
        raw_research = research_path.read_bytes()
        payload = json.loads(raw_research)
        research = BaselineResearch.model_validate(payload)
        design_text = design_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        # 研究文件或设计文件不存在 → 明确停止 PREPARE。
        raise BaselineResearchError(
            f"missing baseline artifact: {error.filename}"
        ) from error
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        # JSON、编码或 Pydantic 校验失败 → 返回带诊断的契约错误。
        raise BaselineResearchError(
            "invalid BASELINE_RESEARCH.json", (str(error),)
        ) from error

    design = _design_selection(design_text)
    if design.selected_candidate_id != research.selected_candidate_id:
        raise BaselineResearchError("design selected candidate does not match research")
    if design.training_strategy != research.dataset.recommended_strategy:
        raise BaselineResearchError("design training strategy does not match research")
    selected = next(
        candidate
        for candidate in research.candidates
        if candidate.candidate_id == research.selected_candidate_id
    )
    return BaselineArtifacts(
        root, raw_research, research, design_text, design, selected
    )


def load_cached_verified_baseline(root: Path) -> VerifiedBaseline | None:
    """Load a matching verification cache, returning ``None`` for stale cache data."""
    artifacts = load_baseline_artifacts(root)
    path = Path(root) / VERIFICATION_FILENAME
    if not path.exists():
        return None
    try:
        verification = BaselineVerification.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError):
        # 缓存不可读或格式非法 → 忽略并重新执行来源验证。
        return None
    if verification.selected_candidate_id != artifacts.selected.candidate_id:
        return None
    if verification.research_sha256 != research_sha256(artifacts.raw_research):
        return None
    return VerifiedBaseline(artifacts, verification)


def assert_verified_files(root: Path, verified: VerifiedBaseline) -> None:
    """Raise when current research files no longer match cached verification."""
    try:
        current = load_baseline_artifacts(root)
    except BaselineResearchError as error:
        raise BaselineResearchError(
            "research digest could not be revalidated",
            error.diagnostics or (str(error),),
        ) from error
    if research_sha256(current.raw_research) != verified.verification.research_sha256:
        raise BaselineResearchError(
            "research artifact digest changed after verification"
        )
    if current.selected.candidate_id != verified.verification.selected_candidate_id:
        raise BaselineResearchError("selected candidate changed after verification")
    if current.design != verified.artifacts.design:
        raise BaselineResearchError("design selection changed after verification")


def write_verification(root: Path, verification: BaselineVerification) -> Path:
    """Atomically write platform verification evidence beside the research files."""
    target = Path(root) / VERIFICATION_FILENAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(verification.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


if __name__ == "__main__":
    print("Baseline research files:", RESEARCH_FILENAME, DESIGN_FILENAME)
