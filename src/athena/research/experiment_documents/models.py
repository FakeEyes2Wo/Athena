"""Strict contracts used by the experiment-document projection layer."""

import math
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Direction = Literal["maximize", "minimize"]
StageName = Literal["baseline", "search", "final"]
ProjectionKind = Literal["stage", "rebuild"]

DOCUMENTS_STALE_CODE = "experiment_documents_stale"
DOCUMENTS_STALE_MESSAGE = (
    "Experiment documents could not be refreshed; canonical research state is "
    "safe and the documents will be rebuilt on recovery."
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_STATUS = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")
_REASON_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _text(value: str, *, limit: int, label: str) -> str:
    if (
        not value
        or len(value) > limit
        or value != value.strip()
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError(f"{label} must be nonblank, single-line, and bounded")
    return value


def _reference(value: str | None, *, label: str = "reference") -> str | None:
    return None if value is None else _text(value, limit=4096, label=label)


def _finite(value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError("metric values must be finite")
    return value


def _validate_run_id(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not _RUN_ID.fullmatch(value)
        or ".." in value
        or value.endswith(".")
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    ):
        raise ValueError("run_id is not a safe identifier")
    return value


class MetricRecord(_StrictModel):
    """Observed metrics plus the authoritative metric contract."""

    name: str
    direction: Direction
    primary: float | None = None
    reference: float | None = None
    generalization_gap: float | None = None
    secondary: dict[str, float] = Field(default_factory=dict)

    _finite_primary = field_validator("primary", "reference", "generalization_gap")(
        _finite
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Reject blank or unsafe metric names."""
        return _text(value, limit=128, label="metric name")

    @field_validator("secondary")
    @classmethod
    def validate_secondary(cls, value: dict[str, float]) -> dict[str, float]:
        """Validate secondary metric names and finite values."""
        for name, metric in value.items():
            _text(name, limit=128, label="secondary metric name")
            if not math.isfinite(metric):
                raise ValueError("metric values must be finite")
        return value


class ReasonRecord(_StrictModel):
    """Human-readable, machine-classifiable reason for a stage result."""

    kind: str
    summary: str

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        """Validate the stable lowercase reason identifier."""
        if not _REASON_KIND.fullmatch(value):
            raise ValueError("reason kind is not a safe identifier")
        return value

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        """Validate the bounded, single-line reason summary."""
        return _text(value, limit=1000, label="reason summary")


class ProvenanceRecord(_StrictModel):
    """References that explain which canonical inputs produced a record."""

    phase: Literal["PREPARE", "SEARCH", "VALIDATE"] | None = None
    experiment_id: str | None = None
    hypothesis_id: str | None = None
    commit: str | None = None
    sota_experiment_id: str | None = None
    sota_commit: str | None = None
    validation_commit: str | None = None

    _validate_values = field_validator(
        "experiment_id",
        "hypothesis_id",
        "commit",
        "sota_experiment_id",
        "sota_commit",
        "validation_commit",
    )(_reference)

    @model_validator(mode="after")
    def require_provenance(self) -> "ProvenanceRecord":
        """Require at least one provenance value on every record."""
        if not any(
            getattr(self, field)
            for field in (
                "phase",
                "experiment_id",
                "hypothesis_id",
                "commit",
                "sota_experiment_id",
                "sota_commit",
                "validation_commit",
            )
        ):
            raise ValueError("at least one provenance field is required")
        return self


class StageRecord(_StrictModel):
    """Persisted stage record with a resolved metric name and direction."""

    schema_version: Literal[1] = 1
    run_id: str
    stage: StageName
    status: str
    metric: MetricRecord
    artifacts: dict[str, str] = Field(default_factory=dict)
    reason: ReasonRecord
    provenance: ProvenanceRecord

    _validate_run_id_field = field_validator("run_id")(_validate_run_id)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """Validate the uppercase stage status identifier."""
        if not _STATUS.fullmatch(value):
            raise ValueError("status is not a valid uppercase identifier")
        return value

    @field_validator("artifacts")
    @classmethod
    def validate_artifacts(cls, value: dict[str, str]) -> dict[str, str]:
        """Validate artifact keys and bounded references."""
        for key, reference in value.items():
            _text(key, limit=128, label="artifact key")
            _reference(reference, label="artifact reference")
        return value

    @classmethod
    def from_event(
        cls, event: Mapping[str, object], *, name: str, direction: Direction
    ) -> "StageRecord":
        """Validate a raw event after adding projector-owned metric facts."""
        if "schema_version" in event:
            raise ValueError("event cannot provide schema_version")
        metric = event.get("metric")
        if isinstance(metric, Mapping) and ({"name", "direction"} & metric.keys()):
            raise ValueError("event cannot provide metric name or direction")
        payload = dict(event)
        payload["schema_version"] = 1
        if isinstance(metric, Mapping):
            payload["metric"] = {**metric, "name": name, "direction": direction}
        return cls.model_validate(payload)


def _safe_manifest_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError("manifest path must be a safe relative POSIX path")
    return value


class LatestManifest(_StrictModel):
    """Digest manifest identifying one complete projection batch."""

    schema_version: Literal[1] = 1
    projection_id: str
    kind: ProjectionKind
    stage: StageName | None = None
    run_id: str | None = None
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("projection_id")
    @classmethod
    def validate_projection_id(cls, value: str) -> str:
        """Require a lowercase SHA-256 projection digest."""
        if not _DIGEST.fullmatch(value):
            raise ValueError("projection_id must be a lowercase SHA-256 digest")
        return value

    _validate_run_id_field = field_validator("run_id")(_validate_run_id)

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        """Validate relative manifest paths and their content digests."""
        for path, digest in value.items():
            _safe_manifest_path(path)
            if not _DIGEST.fullmatch(digest):
                raise ValueError("manifest file digests must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> "LatestManifest":
        """Enforce the identity fields required by each manifest kind."""
        if self.kind == "stage" and (self.stage is None or self.run_id is None):
            raise ValueError("stage manifests require stage and run_id")
        if self.kind == "rebuild" and (
            self.stage is not None or self.run_id is not None
        ):
            raise ValueError("rebuild manifests cannot carry stage identity")
        return self


class ProjectionOutcome(_StrictModel):
    """Sanitized public result of a projection attempt."""

    ok: bool
    projection_id: str | None = None
    warning_code: Literal[DOCUMENTS_STALE_CODE] | None = None
    warning_message: str | None = None

    @field_validator("projection_id")
    @classmethod
    def validate_projection_id(cls, value: str | None) -> str | None:
        """Validate an optional lowercase SHA-256 projection digest."""
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("projection_id must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProjectionOutcome":
        """Enforce mutually exclusive success and stale-warning fields."""
        if self.ok:
            if self.projection_id is None or self.warning_code is not None:
                raise ValueError("successful outcome requires only a projection ID")
            if self.warning_message is not None:
                raise ValueError("successful outcome cannot carry a warning")
        elif (
            self.projection_id is not None
            or self.warning_code != DOCUMENTS_STALE_CODE
            or self.warning_message != DOCUMENTS_STALE_MESSAGE
        ):
            raise ValueError("stale outcome must carry the fixed warning")
        return self

    @classmethod
    def success(cls, projection_id: str) -> "ProjectionOutcome":
        """Build a successful outcome for a committed projection."""
        return cls(ok=True, projection_id=projection_id)

    @classmethod
    def stale(cls) -> "ProjectionOutcome":
        """Build the fixed sanitized outcome for a stale projection."""
        return cls(
            ok=False,
            warning_code=DOCUMENTS_STALE_CODE,
            warning_message=DOCUMENTS_STALE_MESSAGE,
        )


__all__ = [
    "DOCUMENTS_STALE_CODE",
    "DOCUMENTS_STALE_MESSAGE",
    "Direction",
    "LatestManifest",
    "MetricRecord",
    "ProjectionOutcome",
    "ProvenanceRecord",
    "ReasonRecord",
    "StageName",
    "StageRecord",
]
