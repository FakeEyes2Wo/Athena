"""Versioned baseline research artifacts and their deterministic contract."""

import hashlib
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
    """A deterministic artifact or verification contract failure."""

    def __init__(self, message: str, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class DatasetAssessment(BaseModel):
    """Assess local data scale and select an allowed training strategy."""

    model_config = ConfigDict(extra="forbid")

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
        """Require an adequate regime before allowing scratch training."""

        if (
            self.recommended_strategy == "train_from_scratch"
            and self.regime != "adequate"
        ):
            raise ValueError("train_from_scratch requires an adequate data regime")
        return self


class BaselineSource(BaseModel):
    """Describe one externally researched baseline candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    method: str = Field(min_length=1)
    source_url: HttpUrl
    source_kind: Literal["paper", "official_implementation", "technical_reference"]
    paper_locator: str | None = None
    repository_url: HttpUrl | None = None
    publication_year: int | None = None
    claimed_citation_count: int | None = Field(default=None, ge=0)
    relevance: str = Field(min_length=1)

    @field_validator("source_url", "repository_url")
    @classmethod
    def reject_url_userinfo(cls, value: HttpUrl | None) -> HttpUrl | None:
        """Reject credentials embedded in either source URL."""

        if value is not None:
            parsed = urlsplit(str(value))
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("URLs must not contain credentials")
        return value


class CandidateDecision(BaseModel):
    """Record whether a researched candidate was selected or rejected."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    decision: Literal["selected", "rejected"]
    reason: str = Field(min_length=1)


class BaselineResearch(BaseModel):
    """Version-one machine-readable baseline research artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dataset: DatasetAssessment
    candidates: list[BaselineSource] = Field(min_length=1)
    decisions: list[CandidateDecision] = Field(min_length=1)
    selected_candidate_id: str = Field(min_length=1)
    search_queries: list[str] = Field(min_length=1)
    limitations: list[str]

    @model_validator(mode="after")
    def validate_candidate_contract(self) -> "BaselineResearch":
        """Enforce complete, unique, and internally consistent decisions."""

        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("candidate IDs must be unique")

        decision_ids = [decision.candidate_id for decision in self.decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise ValueError("decision IDs must be unique")
        if set(decision_ids) != set(candidate_ids):
            raise ValueError("every candidate must have exactly one decision")

        selected = [
            decision.candidate_id
            for decision in self.decisions
            if decision.decision == "selected"
        ]
        if len(selected) != 1:
            raise ValueError("exactly one candidate decision must be selected")
        if selected[0] != self.selected_candidate_id:
            raise ValueError("selected_candidate_id must match the selected decision")

        if len(self.candidates) == 1 and (
            len(set(self.search_queries)) < 2 or not self.limitations
        ):
            raise ValueError(
                "one-candidate research requires two distinct queries and a limitation"
            )

        if self.dataset.recommended_strategy == "train_from_scratch":
            local_prefixes = ("eda:", "calculation:")
            if not any(
                item.startswith(local_prefixes) for item in self.dataset.evidence
            ):
                raise ValueError(
                    "train_from_scratch requires EDA or calculation evidence"
                )
            source_prefix = f"source:{self.selected_candidate_id}:"
            if not any(
                item.startswith(source_prefix) for item in self.dataset.evidence
            ):
                raise ValueError(
                    "train_from_scratch requires comparable selected-source evidence"
                )
        return self


class BaselineDesignSelection(BaseModel):
    """Immutable selection parsed from the human-readable design artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_candidate_id: str = Field(min_length=1)
    training_strategy: TrainingStrategy


@dataclass(frozen=True)
class BaselineArtifacts:
    """Validated research/design pair, retaining the exact research bytes."""

    root: Path
    raw_research: bytes
    research: BaselineResearch
    design_text: str
    design: BaselineDesignSelection
    selected: BaselineSource


class VerificationAttempt(BaseModel):
    """Record one platform source-verification route attempt."""

    model_config = ConfigDict(extra="forbid")

    route: Literal["git", "openalex"]
    success: bool
    diagnostic: str


class BaselineVerification(BaseModel):
    """Platform-owned digest-bound verification record."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    research_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_id: str = Field(min_length=1)
    route: Literal["git", "openalex"]
    verified_at: datetime
    repository_url: str | None = None
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    openalex_id: str | None = None
    title: str | None = None
    publication_year: int | None = None
    cited_by_count: int | None = Field(default=None, ge=0)
    attempts: list[VerificationAttempt]


@dataclass(frozen=True)
class VerifiedBaseline:
    """Immutable pair of validated artifacts and platform verification."""

    artifacts: BaselineArtifacts
    verification: BaselineVerification


def _parse_marker(text: str, pattern: re.Pattern[str], label: str) -> str:
    matches = pattern.findall(text)
    if len(matches) != 1:
        if not matches:
            detail = "missing"
        else:
            detail = "duplicate"
        raise BaselineResearchError(
            f"BASELINE_DESIGN.md must contain exactly one {label} marker ({detail})"
        )
    return matches[0]


def load_baseline_artifacts(root: Path) -> BaselineArtifacts:
    """Read and validate the research and matching design artifacts."""
    research_path = root / RESEARCH_FILENAME
    design_path = root / DESIGN_FILENAME
    try:
        raw_research = research_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise BaselineResearchError(
            f"unable to read {RESEARCH_FILENAME}", (str(exc),)
        ) from exc
    try:
        research = BaselineResearch.model_validate_json(raw_research)
    except (ValidationError, ValueError) as exc:
        raise BaselineResearchError(
            f"invalid {RESEARCH_FILENAME}", (str(exc),)
        ) from exc
    try:
        design_text = design_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BaselineResearchError(
            f"unable to read {DESIGN_FILENAME}", (str(exc),)
        ) from exc

    selected_candidate_id = _parse_marker(
        design_text, _SELECTED_RE, "Selected candidate"
    )
    training_strategy = _parse_marker(design_text, _STRATEGY_RE, "Training strategy")
    if selected_candidate_id != research.selected_candidate_id:
        raise BaselineResearchError(
            "design selected candidate does not match research artifact",
            (
                f"design={selected_candidate_id}",
                f"research={research.selected_candidate_id}",
            ),
        )
    if training_strategy != research.dataset.recommended_strategy:
        raise BaselineResearchError(
            "design training strategy does not match research artifact",
            (
                f"design={training_strategy}",
                f"research={research.dataset.recommended_strategy}",
            ),
        )

    selected = next(
        candidate
        for candidate in research.candidates
        if candidate.candidate_id == research.selected_candidate_id
    )
    design = BaselineDesignSelection(
        selected_candidate_id=selected_candidate_id,
        training_strategy=training_strategy,
    )
    return BaselineArtifacts(
        root=root,
        raw_research=raw_research,
        research=research,
        design_text=design_text,
        design=design,
        selected=selected,
    )


def research_sha256(raw: bytes) -> str:
    """Return the digest of the exact research bytes that were validated."""
    return hashlib.sha256(raw).hexdigest()


def write_verification(root: Path, verification: BaselineVerification) -> Path:
    """Persist platform verification atomically beside the research artifact."""
    target = root / VERIFICATION_FILENAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(verification.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def load_cached_verified_baseline(root: Path) -> VerifiedBaseline | None:
    """Load cache only when it still names and digests the current artifacts."""
    artifacts = load_baseline_artifacts(root)
    cache_path = root / VERIFICATION_FILENAME
    try:
        verification = BaselineVerification.model_validate_json(cache_path.read_bytes())
    except (OSError, UnicodeError, ValidationError, ValueError):
        return None
    if verification.research_sha256 != research_sha256(artifacts.raw_research):
        return None
    if verification.selected_candidate_id != artifacts.selected.candidate_id:
        return None
    return VerifiedBaseline(artifacts=artifacts, verification=verification)


def assert_verified_files(root: Path, verified: VerifiedBaseline) -> None:
    """Ensure a verified carrier still describes the files at ``root``."""
    artifacts = load_baseline_artifacts(root)
    expected_candidate = verified.artifacts.selected.candidate_id
    expected_strategy = verified.artifacts.design.training_strategy
    actual_digest = research_sha256(artifacts.raw_research)
    if actual_digest != verified.verification.research_sha256:
        raise BaselineResearchError(
            "research artifact digest does not match verification",
            (
                f"expected={verified.verification.research_sha256}",
                f"actual={actual_digest}",
            ),
        )
    if (
        artifacts.selected.candidate_id != expected_candidate
        or verified.verification.selected_candidate_id != expected_candidate
    ):
        raise BaselineResearchError("selected candidate does not match verification")
    if artifacts.design.training_strategy != expected_strategy:
        raise BaselineResearchError("training strategy does not match verification")


__all__ = [
    "AUTHORITY_CITATION_THRESHOLD",
    "BaselineArtifacts",
    "BaselineDesignSelection",
    "BaselineResearch",
    "BaselineResearchError",
    "BaselineSource",
    "BaselineVerification",
    "CandidateDecision",
    "DatasetAssessment",
    "DESIGN_FILENAME",
    "RESEARCH_FILENAME",
    "TrainingStrategy",
    "VerificationAttempt",
    "VERIFICATION_FILENAME",
    "VerifiedBaseline",
    "assert_verified_files",
    "load_baseline_artifacts",
    "load_cached_verified_baseline",
    "research_sha256",
    "write_verification",
]
