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
_INTEGER_TOKEN_RE = re.compile(r"(?<![\w.])(?P<value>[+-]?\d+)(?!\w|\.\d)")
_CALCULATION_RE = re.compile(
    r"(?<![\w.])(?P<left>\d+)\s*(?P<operator>[+*/-])\s*"
    r"(?P<right>\d+)\s*=\s*(?P<result>\d+)(?!\w|\.\d)"
)


def _substantive(value: str) -> str:
    """Normalize one free-text contract value and reject whitespace-only input."""
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must contain substantive text")
    return normalized


def _contains_exact_integer(text: str, expected: int) -> bool:
    """Return whether text contains an integer token equal to ``expected``."""
    return any(
        int(match["value"]) == expected for match in _INTEGER_TOKEN_RE.finditer(text)
    )


def _contains_matching_calculation(text: str, expected: int) -> bool:
    """Accept one true nonnegative-integer binary expression with the expected result."""
    for match in _CALCULATION_RE.finditer(text):
        left = int(match["left"])
        right = int(match["right"])
        result = int(match["result"])
        if result != expected:
            continue
        operator = match["operator"]
        if operator == "+" and left + right == result:
            return True
        if operator == "-" and left - right == result:
            return True
        if operator == "*" and left * right == result:
            return True
        if (
            operator == "/"
            and right != 0
            and left % right == 0
            and left // right == result
        ):
            return True
    return False


class BaselineResearchError(RuntimeError):
    """A deterministic artifact or verification contract failure."""

    def __init__(self, message: str, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


Modality = Literal[
    "tabular",
    "image",
    "text",
    "audio",
    "video",
    "time_series",
    "multimodal",
    "other",
]
DataRegime = Literal["tiny", "small", "adequate", "unknown"]
DatasetFactName = Literal[
    "labeled_samples",
    "effective_training_units",
    "group_count",
    "class_count",
    "minority_class_samples",
]


class EvidenceRef(BaseModel):
    """One typed, traceable statement supporting a research claim."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["eda", "data_contract", "calculation", "source"]
    reference: str
    claim: str

    @field_validator("reference", "claim")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _substantive(value)


class DatasetFact(BaseModel):
    """One nonnegative local-data fact with evidence bound to its value."""

    model_config = ConfigDict(extra="forbid")

    field: DatasetFactName
    value: int = Field(ge=0)
    evidence: EvidenceRef

    @model_validator(mode="after")
    def bind_local_evidence(self) -> "DatasetFact":
        if self.evidence.kind == "source":
            raise ValueError("dataset facts require local evidence")
        evidence_text = f"{self.evidence.reference} {self.evidence.claim}"
        if not _contains_exact_integer(evidence_text, self.value):
            raise ValueError("dataset fact evidence must name its supplied value")
        if self.evidence.kind == "calculation" and not any(
            _contains_matching_calculation(text, self.value)
            for text in (self.evidence.reference, self.evidence.claim)
        ):
            raise ValueError(
                "calculation evidence requires a true integer expression ending in the supplied value"
            )
        return self


class DatasetProfile(BaseModel):
    """Compact modality and data-regime assessment."""

    model_config = ConfigDict(extra="forbid")

    modality: Modality
    task_type: str
    input_scale: str
    regime: DataRegime
    facts: list[DatasetFact]
    rationale: str

    @field_validator("task_type", "input_scale", "rationale")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _substantive(value)

    @model_validator(mode="after")
    def reject_duplicate_facts(self) -> "DatasetProfile":
        names = [fact.field for fact in self.facts]
        if len(names) != len(set(names)):
            raise ValueError("dataset fact names must be unique")
        return self


class PretrainedAssessment(BaseModel):
    """Availability assessment for a relevant pretrained representation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "unavailable", "unknown"]
    representation: str | None
    evidence: EvidenceRef

    @field_validator("representation")
    @classmethod
    def normalize_representation(cls, value: str | None) -> str | None:
        return None if value is None else _substantive(value)

    @model_validator(mode="after")
    def bind_representation_to_status(self) -> "PretrainedAssessment":
        if self.status == "available" and self.representation is None:
            raise ValueError("available pretrained weights require a representation")
        if self.status != "available" and self.representation is not None:
            raise ValueError(
                "unavailable pretrained weights cannot name a representation"
            )
        return self


class FineTuneSafeguards(BaseModel):
    """The three evidence-bearing controls required for full fine-tuning."""

    model_config = ConfigDict(extra="forbid")

    augmentation: EvidenceRef
    regularization: EvidenceRef
    validation: EvidenceRef


class ScratchScaleComparison(BaseModel):
    """Comparable-scale proof connecting local data to the selected source."""

    model_config = ConfigDict(extra="forbid")

    selected_candidate_id: str
    source_locator: str
    local_fact: DatasetFactName
    local_value: int = Field(ge=0)
    source_value: int = Field(ge=0)
    unit: str
    relationship: Literal["comparable", "local_at_least_source"]
    rationale: str

    @field_validator("selected_candidate_id", "source_locator", "unit", "rationale")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _substantive(value)

    @model_validator(mode="after")
    def validate_relationship(self) -> "ScratchScaleComparison":
        if (
            self.relationship == "local_at_least_source"
            and self.local_value < self.source_value
        ):
            raise ValueError("local scale must be at least the cited source scale")
        return self


class TrainingPolicy(BaseModel):
    """Strategy and only the evidence structures relevant to that strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy: TrainingStrategy
    pretrained: PretrainedAssessment | None
    safeguards: FineTuneSafeguards | None
    scratch_scale: ScratchScaleComparison | None


class OneCandidateException(BaseModel):
    """Search evidence explaining why only one candidate was retained."""

    model_config = ConfigDict(extra="forbid")

    query_indices: list[int] = Field(min_length=2)
    scope: str
    limitation: str

    @field_validator("scope", "limitation")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _substantive(value)

    @field_validator("query_indices")
    @classmethod
    def reject_duplicate_indices(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("one-candidate query indices must be unique")
        return value


class SearchRecord(BaseModel):
    """Normalized search queries and the optional one-candidate exception."""

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(min_length=2)
    one_candidate: OneCandidateException | None

    @field_validator("queries")
    @classmethod
    def normalize_queries(cls, value: list[str]) -> list[str]:
        normalized = [_substantive(query) for query in value]
        if len({query.casefold() for query in normalized}) != len(normalized):
            raise ValueError("search queries must be distinct")
        return normalized

    @model_validator(mode="after")
    def bind_exception_queries(self) -> "SearchRecord":
        if self.one_candidate is not None and any(
            index < 0 or index >= len(self.queries)
            for index in self.one_candidate.query_indices
        ):
            raise ValueError("one-candidate query index is out of range")
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

    @field_validator("candidate_id", "title", "method", "paper_locator", "relevance")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return None if value is None else _substantive(value)

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

    @field_validator("candidate_id", "reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _substantive(value)


class BaselineResearch(BaseModel):
    """Version-two machine-readable baseline research artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    dataset: DatasetProfile
    training: TrainingPolicy
    candidates: list[BaselineSource] = Field(min_length=1)
    decisions: list[CandidateDecision] = Field(min_length=1)
    selected_candidate_id: str = Field(min_length=1)
    search: SearchRecord
    limitations: list[str]

    @field_validator("selected_candidate_id")
    @classmethod
    def normalize_selected_candidate(cls, value: str) -> str:
        return _substantive(value)

    @field_validator("limitations")
    @classmethod
    def normalize_limitations(cls, value: list[str]) -> list[str]:
        return [_substantive(limitation) for limitation in value]

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

        if len(self.candidates) == 1 and self.search.one_candidate is None:
            raise ValueError("one-candidate research requires a structured exception")
        if len(self.candidates) != 1 and self.search.one_candidate is not None:
            raise ValueError("one-candidate exception is valid only for one candidate")

        selected_source = next(
            candidate
            for candidate in self.candidates
            if candidate.candidate_id == self.selected_candidate_id
        )
        validate_training_policy(self.dataset, self.training, selected_source)
        return self


_PRETRAINED_MODALITIES = frozenset({"image", "text", "audio", "video", "multimodal"})
_TRANSFER_STRATEGIES = frozenset(
    {"frozen_pretrained", "partial_finetune", "full_finetune"}
)


def validate_training_policy(
    dataset: DatasetProfile,
    training: TrainingPolicy,
    selected: BaselineSource,
) -> None:
    """Raise ValueError when v2 evidence does not justify the strategy."""

    strategy = training.strategy
    pretrained = training.pretrained
    if dataset.modality in _PRETRAINED_MODALITIES and pretrained is None:
        raise ValueError("this modality requires a pretrained availability assessment")
    if strategy in _TRANSFER_STRATEGIES and (
        pretrained is None or pretrained.status != "available"
    ):
        raise ValueError("transfer strategies require an available representation")

    if dataset.modality in _PRETRAINED_MODALITIES:
        if dataset.regime == "tiny":
            allowed = (
                {"frozen_pretrained"}
                if pretrained is not None and pretrained.status == "available"
                else {"classical"}
            )
        elif dataset.regime == "small":
            allowed = {
                "frozen_pretrained",
                "partial_finetune",
                "full_finetune",
            }
        elif dataset.regime == "unknown":
            allowed = {"classical", "frozen_pretrained"}
        else:
            allowed = set(TrainingStrategy.__args__)
        if strategy not in allowed:
            raise ValueError(
                f"{dataset.regime} {dataset.modality} data does not permit {strategy}"
            )

    if strategy == "full_finetune":
        if training.safeguards is None:
            raise ValueError("full_finetune requires all safeguard evidence")
    elif training.safeguards is not None:
        raise ValueError("fine-tune safeguards are unused by the selected strategy")

    if strategy == "train_from_scratch":
        if dataset.regime != "adequate":
            raise ValueError("train_from_scratch requires an adequate data regime")
        comparison = training.scratch_scale
        if comparison is None:
            raise ValueError("train_from_scratch requires a source scale comparison")
        if comparison.selected_candidate_id != selected.candidate_id:
            raise ValueError("scratch comparison must name the selected candidate")
        source_locators = {
            value
            for value in (
                str(selected.source_url),
                str(selected.repository_url) if selected.repository_url else None,
                selected.paper_locator,
            )
            if value is not None
        }
        if comparison.source_locator not in source_locators:
            raise ValueError("scratch comparison must use a selected-source locator")
        local_fact = next(
            (fact for fact in dataset.facts if fact.field == comparison.local_fact),
            None,
        )
        if local_fact is None or local_fact.value != comparison.local_value:
            raise ValueError("scratch comparison must match a supplied local fact")
    elif training.scratch_scale is not None:
        raise ValueError("scratch scale evidence is unused by the selected strategy")


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
    attempts: list[VerificationAttempt] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_route_proof(self) -> "BaselineVerification":
        """Require route-specific proof and a successful matching attempt."""
        if not any(
            attempt.route == self.route and attempt.success for attempt in self.attempts
        ):
            raise ValueError("verification needs a successful attempt for its route")

        if self.route == "git":
            if not _is_public_https_repository(self.repository_url):
                raise ValueError(
                    "git verification requires a normalized public HTTPS repository URL"
                )
            if self.commit is None:
                raise ValueError("git verification requires a resolved commit")
        else:
            if not self.openalex_id or not self.openalex_id.strip():
                raise ValueError("OpenAlex verification requires a work ID")
            if not self.title or not self.title.strip():
                raise ValueError("OpenAlex verification requires a title")
            if (
                self.cited_by_count is None
                or self.cited_by_count < AUTHORITY_CITATION_THRESHOLD
            ):
                raise ValueError(
                    "OpenAlex verification requires the authority citation threshold"
                )
        return self


@dataclass(frozen=True)
class VerifiedBaseline:
    """Immutable pair of validated artifacts and platform verification."""

    artifacts: BaselineArtifacts
    verification: BaselineVerification


def _is_public_https_repository(value: str | None) -> bool:
    """Return whether a cache repository proof has a normalized HTTPS URL."""
    if not value or value != value.strip() or value.endswith("/"):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path not in ("", "/")
    )


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
    if training_strategy != research.training.strategy:
        raise BaselineResearchError(
            "design training strategy does not match research artifact",
            (
                f"design={training_strategy}",
                f"research={research.training.strategy}",
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
    "DatasetFact",
    "DatasetProfile",
    "DESIGN_FILENAME",
    "EvidenceRef",
    "FineTuneSafeguards",
    "OneCandidateException",
    "PretrainedAssessment",
    "RESEARCH_FILENAME",
    "ScratchScaleComparison",
    "SearchRecord",
    "TrainingPolicy",
    "TrainingStrategy",
    "VerificationAttempt",
    "VERIFICATION_FILENAME",
    "VerifiedBaseline",
    "assert_verified_files",
    "load_baseline_artifacts",
    "load_cached_verified_baseline",
    "research_sha256",
    "validate_training_policy",
    "write_verification",
]
