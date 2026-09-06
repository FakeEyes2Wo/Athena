"""Versioned baseline research artifacts and their deterministic contract."""

import hashlib
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal
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

from .repository_url import normalize_public_https_repository_url

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
_CALCULATION_RE = re.compile(
    r"(?P<left>[0-9]+)[ \t]*(?P<operator>[+*/-])[ \t]*"
    r"(?P<right>[0-9]+)[ \t]*=[ \t]*(?P<result>[0-9]+)"
)


def _substantive(value: str) -> str:
    """Normalize one free-text contract value and reject whitespace-only input."""
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must contain substantive text")
    return normalized


def _contains_matching_calculation(text: str, expected: int) -> bool:
    """Accept one full-field ASCII integer expression with the expected result."""
    match = _CALCULATION_RE.fullmatch(text.strip())
    if match is None:
        return False
    left = int(match["left"])
    right = int(match["right"])
    result = int(match["result"])
    if result != expected:
        return False
    operator = match["operator"]
    if operator == "+":
        return left + right == result
    if operator == "-":
        return left - right == result
    if operator == "*":
        return left * right == result
    return right != 0 and left % right == 0 and left // right == result


def _source_locator_key(value: str, *, http: bool) -> tuple[str, str] | None:
    """Return the field-appropriate comparison key for one source locator."""
    normalized = value.strip()
    if not http:
        return ("identifier", normalized)
    try:
        return ("http", str(HttpUrl(normalized)))
    except ValidationError:
        return None


class BaselineResearchError(RuntimeError):
    """A deterministic artifact or verification contract failure."""

    def __init__(self, message: str, diagnostics: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)
        self.published = False


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
        if self.evidence.kind == "calculation" and not any(
            _contains_matching_calculation(text, self.value)
            for text in (self.evidence.reference, self.evidence.claim)
        ):
            raise ValueError(
                "calculation evidence requires a true full-field ASCII integer expression equal to the supplied value"
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
        _validate_training_policy(self.dataset, self.training, selected_source)
        return self


_PRETRAINED_MODALITIES = frozenset({"image", "text", "audio", "video", "multimodal"})
_TRANSFER_STRATEGIES = frozenset(
    {"frozen_pretrained", "partial_finetune", "full_finetune"}
)


def _validate_training_policy(
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
        source_locators = (
            (str(selected.source_url), True),
            (
                str(selected.repository_url) if selected.repository_url else None,
                True,
            ),
            (selected.paper_locator, False),
        )
        if not any(
            locator is not None
            and _source_locator_key(comparison.source_locator, http=http)
            == _source_locator_key(locator, http=http)
            for locator, http in source_locators
        ):
            raise ValueError("scratch comparison must use a selected-source locator")
        local_fact = next(
            (fact for fact in dataset.facts if fact.field == comparison.local_fact),
            None,
        )
        if local_fact is None or local_fact.value != comparison.local_value:
            raise ValueError("scratch comparison must match a supplied local fact")
    elif training.scratch_scale is not None:
        raise ValueError("scratch scale evidence is unused by the selected strategy")


@dataclass(frozen=True, slots=True)
class BaselineArtifacts:
    """Validated research/design pair, retaining both exact byte sequences."""

    root: Path
    raw_research: bytes
    raw_design: bytes
    research: BaselineResearch
    training_strategy: TrainingStrategy

    @property
    def selected(self) -> BaselineSource:
        """Return the selected candidate from the validated research contract."""
        return next(
            candidate
            for candidate in self.research.candidates
            if candidate.candidate_id == self.research.selected_candidate_id
        )


class VerificationAttempt(BaseModel):
    """Record one platform source-verification route attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    route: Literal["git", "openalex"]
    success: bool
    diagnostic: str


class BaselineVerification(BaseModel):
    """Platform-owned digest-bound verification record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    research_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    design_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_id: str = Field(min_length=1)
    route: Literal["git", "openalex"]
    verified_at: datetime
    repository_url: str | None = None
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    paper_locator: str | None = None
    openalex_id: str | None = Field(default=None, pattern=r"^W[1-9][0-9]*$")
    title: str | None = None
    publication_year: int | None = None
    cited_by_count: int | None = Field(default=None, ge=0)
    attempts: tuple[VerificationAttempt, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_route_proof(self) -> "BaselineVerification":
        """Require route-specific proof and a successful matching attempt."""
        if not any(
            attempt.route == self.route and attempt.success for attempt in self.attempts
        ):
            raise ValueError("verification needs a successful attempt for its route")

        if self.route == "git":
            if self.paper_locator is not None:
                raise ValueError("Git verification must not contain a paper locator")
            if self.repository_url is None:
                raise ValueError(
                    "git verification requires a normalized public HTTPS repository URL"
                )
            try:
                normalized_repository = normalize_public_https_repository_url(
                    self.repository_url
                )
            except ValueError as exc:
                raise ValueError(
                    "git verification requires a normalized public HTTPS repository URL"
                ) from exc
            if normalized_repository != self.repository_url:
                raise ValueError(
                    "git verification requires a normalized public HTTPS repository URL"
                )
            if self.commit is None:
                raise ValueError("git verification requires a resolved commit")
        else:
            if not self.paper_locator or not self.paper_locator.strip():
                raise ValueError("OpenAlex verification requires a paper locator")
            if self.openalex_id is None:
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


@dataclass(frozen=True, slots=True)
class VerifiedBaseline:
    """Validated artifacts carried with canonical external-authority evidence."""

    artifacts: BaselineArtifacts
    verification: BaselineVerification
    verification_bytes: bytes
    authority_generation: int | None = None

    def __post_init__(self) -> None:
        raw, _ = _parse_canonical_verification(
            self.verification_bytes, self.verification
        )
        if self.authority_generation is not None and (
            not isinstance(self.authority_generation, int)
            or isinstance(self.authority_generation, bool)
            or self.authority_generation < 0
        ):
            raise BaselineResearchError(
                "authority generation must be a non-negative integer"
            )
        object.__setattr__(self, "verification_bytes", raw)


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


def _parse_baseline_artifacts(
    root: Path, raw_research: bytes, raw_design: bytes
) -> BaselineArtifacts:
    """Validate one exact research/design byte pair for ``root``."""
    try:
        research = BaselineResearch.model_validate_json(raw_research)
    except (ValidationError, ValueError) as exc:
        raise BaselineResearchError(
            f"invalid {RESEARCH_FILENAME}", (str(exc),)
        ) from exc
    try:
        design_text = raw_design.decode("utf-8", errors="strict")
    except (UnicodeError, ValueError) as exc:
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

    return BaselineArtifacts(
        root=root,
        raw_research=raw_research,
        raw_design=raw_design,
        research=research,
        training_strategy=training_strategy,
    )


def load_baseline_artifacts(root: Path) -> BaselineArtifacts:
    """Read and validate the research and matching design artifacts."""
    try:
        raw_research = (root / RESEARCH_FILENAME).read_bytes()
    except (OSError, ValueError) as exc:
        raise BaselineResearchError(
            f"unable to read {RESEARCH_FILENAME}", (str(exc),)
        ) from exc
    try:
        raw_design = (root / DESIGN_FILENAME).read_bytes()
    except (OSError, ValueError) as exc:
        raise BaselineResearchError(
            f"unable to read {DESIGN_FILENAME}", (str(exc),)
        ) from exc
    return _parse_baseline_artifacts(root, raw_research, raw_design)


def research_sha256(raw: bytes) -> str:
    """Return the digest of the exact research bytes that were validated."""
    return hashlib.sha256(raw).hexdigest()


def design_sha256(raw: bytes) -> str:
    """Return the digest of the exact design bytes that were validated."""
    return hashlib.sha256(raw).hexdigest()


def titles_match(reported: str, resolved: str) -> bool:
    """Compare titles after deterministic Unicode and punctuation normalization.

    A long title dropped at its subtitle still identifies the work. On
    2026-09-06 a candidate cited the right DOI and reported the title without
    its trailing "and Analysis of a New Flare Catalog"; the ratio fell to 0.85
    and PREPARE failed for a paper OpenAlex had resolved correctly. A prefix
    that long cannot pair a DOI with some other paper, which is what this check
    exists to catch.
    """
    normalized_reported = _normalize_title(reported)
    normalized_resolved = _normalize_title(resolved)
    if not normalized_reported or not normalized_resolved:
        return False
    if normalized_reported == normalized_resolved:
        return True
    shorter, longer = sorted((normalized_reported, normalized_resolved), key=len)
    if len(shorter) >= 40 and longer.startswith(shorter):
        return True
    if len(shorter) < 20:
        return False
    return (
        SequenceMatcher(None, normalized_reported, normalized_resolved).ratio() >= 0.90
    )


def _normalize_title(value: str) -> str:
    """Join Unicode-normalized alphanumeric title tokens."""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def verification_bytes(verification: BaselineVerification) -> bytes:
    """Serialize one verification record to its canonical audit representation."""
    return (verification.model_dump_json(indent=2) + "\n").encode("utf-8")


def _parse_canonical_verification(
    raw: bytes | bytearray | memoryview,
    expected: BaselineVerification,
) -> tuple[bytes, BaselineVerification]:
    """Validate one canonical byte representation against its parsed value."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise BaselineResearchError("verification bytes must be bytes-like")
    try:
        copied = bytes(raw)
        parsed = BaselineVerification.model_validate_json(copied)
    except (TypeError, ValueError) as exc:
        raise BaselineResearchError("verification bytes are invalid") from exc
    if copied != verification_bytes(parsed) or parsed != expected:
        raise BaselineResearchError(
            "verification bytes do not match the parsed verification"
        )
    return copied, parsed


def assert_verification_matches_artifacts(
    artifacts: BaselineArtifacts,
    verification: BaselineVerification,
) -> None:
    """Reject any verification proof that does not bind the complete artifacts."""
    actual_research_digest = research_sha256(artifacts.raw_research)
    if verification.research_sha256 != actual_research_digest:
        raise BaselineResearchError(
            "research artifact digest does not match verification",
            (
                f"expected={verification.research_sha256}",
                f"actual={actual_research_digest}",
            ),
        )

    actual_design_digest = design_sha256(artifacts.raw_design)
    if verification.design_sha256 != actual_design_digest:
        raise BaselineResearchError(
            "design artifact digest does not match verification",
            (
                f"expected={verification.design_sha256}",
                f"actual={actual_design_digest}",
            ),
        )

    if verification.selected_candidate_id != artifacts.selected.candidate_id:
        raise BaselineResearchError("selected candidate does not match verification")

    if verification.route == "git":
        if artifacts.selected.repository_url is None:
            raise BaselineResearchError(
                "Git verification requires a selected repository"
            )
        try:
            selected_repository = normalize_public_https_repository_url(
                str(artifacts.selected.repository_url)
            )
        except ValueError as exc:
            raise BaselineResearchError(
                "selected repository is not a public HTTPS repository", (str(exc),)
            ) from exc
        if verification.repository_url != selected_repository:
            raise BaselineResearchError(
                "Git verification proof does not match the selected repository"
            )
    else:
        if verification.paper_locator != artifacts.selected.paper_locator:
            raise BaselineResearchError(
                "OpenAlex verification proof does not match the selected paper locator"
            )
        if verification.title is None or not titles_match(
            artifacts.selected.title, verification.title
        ):
            raise BaselineResearchError(
                "OpenAlex verification title does not match the selected source"
            )


def _write_verification_bytes(root: Path, raw: bytes) -> Path:
    """Atomically replace the untrusted audit mirror with canonical bytes."""
    target = root / VERIFICATION_FILENAME
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def write_verification(root: Path, verification: BaselineVerification) -> Path:
    """Persist canonical platform verification as an untrusted audit mirror."""
    return _write_verification_bytes(root, verification_bytes(verification))
