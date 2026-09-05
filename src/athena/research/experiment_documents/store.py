"""Atomic persistence for immutable experiment-document projections."""

import hashlib
import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from athena.research.experiment_documents.models import (
    LatestManifest,
    StageName,
    StageRecord,
)

_REPORT_NAMES = ("FINAL_REPORT.md", "OPTIMIZATION.md")
_STAGE_ORDER = ("baseline", "search", "final")


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return f"{text}\n".encode()


def _render_stage_record(record: StageRecord) -> bytes:
    return _json_bytes(record.model_dump(mode="json"))


def _build_manifest(
    batch: "ProjectionBatch", files: Mapping[str, bytes]
) -> LatestManifest:
    digests = {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(files.items())
    }
    identity = _json_bytes(
        {
            "schema_version": 1,
            "kind": batch.kind,
            "stage": batch.stage,
            "run_id": batch.run_id,
            "files": digests,
        }
    )
    return LatestManifest(
        projection_id=hashlib.sha256(identity).hexdigest(),
        kind=batch.kind,
        stage=batch.stage,
        run_id=batch.run_id,
        files=digests,
    )


@dataclass(frozen=True)
class ProjectionBatch:
    """Immutable set of files committed as one projection."""

    kind: Literal["stage", "rebuild"]
    records: Sequence[StageRecord]
    aliases: Mapping[StageName, str]
    reports: Mapping[Literal["FINAL_REPORT.md", "OPTIMIZATION.md"], bytes]

    def __post_init__(self) -> None:
        if self.kind not in {"stage", "rebuild"}:
            raise ValueError("kind must be stage or rebuild")
        records = tuple(self.records)
        if any(not isinstance(record, StageRecord) for record in records):
            raise TypeError("records must contain StageRecord values")
        if self.kind == "stage" and len(records) != 1:
            raise ValueError("stage batches require exactly one record")
        run_ids = [record.run_id for record in records]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run IDs must be unique")

        aliases = dict(self.aliases)
        for stage, run_id in aliases.items():
            if stage not in _STAGE_ORDER:
                raise ValueError("invalid alias stage")
            if run_id not in run_ids:
                raise ValueError("aliases must reference a run in the batch")

        reports = dict(self.reports)
        if set(reports) != set(_REPORT_NAMES):
            raise ValueError("reports must contain exactly the required report names")
        if any(not isinstance(content, bytes) for content in reports.values()):
            raise TypeError("report content must be bytes")

        object.__setattr__(self, "records", records)
        object.__setattr__(self, "aliases", MappingProxyType(aliases))
        object.__setattr__(self, "reports", MappingProxyType(reports))

    @property
    def stage(self) -> StageName | None:
        """Derive stage identity for a one-record stage projection."""
        return self.records[0].stage if self.kind == "stage" else None

    @property
    def run_id(self) -> str | None:
        """Derive run identity for a one-record stage projection."""
        return self.records[0].run_id if self.kind == "stage" else None


class RunDocumentConflict(ValueError):
    """Raised when an existing run archive cannot be safely reused."""


def _existing_record(content: bytes) -> StageRecord:
    """Parse a strict archive, normalizing only the supported legacy shape."""
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunDocumentConflict(
            "existing run archive is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RunDocumentConflict("existing run archive must contain an object")
    if "schema_version" not in payload:
        payload = dict(payload)
        payload["schema_version"] = 1
        metric = payload.get("metric")
        if isinstance(metric, dict):
            metric = dict(metric)
            for key in ("primary", "reference", "generalization_gap"):
                value = metric.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    metric[key] = float(value)
            secondary = metric.get("secondary")
            if isinstance(secondary, dict):
                metric["secondary"] = {
                    key: (
                        float(value)
                        if isinstance(value, int) and not isinstance(value, bool)
                        else value
                    )
                    for key, value in secondary.items()
                }
            payload["metric"] = metric
    try:
        return StageRecord.model_validate(payload)
    except (TypeError, ValidationError) as exc:
        raise RunDocumentConflict(
            "existing run archive does not match the schema"
        ) from exc


def _records_match(existing: StageRecord, candidate: StageRecord, mode: str) -> bool:
    if mode == "exact":
        return existing.model_dump(mode="json") == candidate.model_dump(mode="json")
    if mode != "recoverable":
        raise ValueError("unknown matching mode")
    if (
        existing.run_id != candidate.run_id
        or existing.stage != candidate.stage
        or existing.status != candidate.status
        or existing.metric.primary != candidate.metric.primary
        or existing.metric.secondary != candidate.metric.secondary
        or existing.artifacts != candidate.artifacts
    ):
        return False
    for field in ("reference", "generalization_gap"):
        value = getattr(candidate.metric, field)
        if value is not None and getattr(existing.metric, field) != value:
            return False
    for field in (
        "phase",
        "experiment_id",
        "hypothesis_id",
        "commit",
        "sota_experiment_id",
        "sota_commit",
        "validation_commit",
    ):
        value = getattr(candidate.provenance, field)
        if value is not None and getattr(existing.provenance, field) != value:
            return False
    return True


class DocumentStore:
    """Persist projection batches with one-process serialized replacement."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        """Return the resolved projection root."""
        return self._root

    def _target(self, relative: str) -> Path:
        if (
            not relative
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or Path(relative).is_absolute()
        ):
            raise ValueError("projection path must be a safe relative POSIX path")
        target = (self._root / Path(relative)).resolve(strict=False)
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("projection path escapes the store root") from exc
        return target

    def _write_temp(self, relative: str, content: bytes) -> Path:
        target = self._target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _preflight(
        self, batch: ProjectionBatch
    ) -> tuple[dict[str, bytes], list[tuple[str, bytes]]]:
        self._root.mkdir(parents=True, exist_ok=True)
        effective: dict[str, bytes] = {}
        replacements: list[tuple[str, bytes]] = []
        selected: dict[str, bytes] = {}

        mode = "exact" if batch.kind == "stage" else "recoverable"
        for record in sorted(batch.records, key=lambda item: item.run_id):
            relative = f"runs/{record.run_id}.json"
            target = self._target(relative)
            content = _render_stage_record(record)
            if target.exists():
                if not target.is_file():
                    raise RunDocumentConflict("run archive target is not a file")
                existing_bytes = target.read_bytes()
                existing = _existing_record(existing_bytes)
                if not _records_match(existing, record, mode):
                    raise RunDocumentConflict(
                        f"run archive {record.run_id} conflicts with existing bytes"
                    )
                selected[record.run_id] = existing_bytes
            else:
                selected[record.run_id] = content
                replacements.append((relative, content))
            effective[relative] = selected[record.run_id]

        for stage in _STAGE_ORDER:
            if stage not in batch.aliases:
                continue
            run_id = batch.aliases[stage]
            content = selected[run_id]
            relative = f"{stage}.json"
            self._target(relative)
            effective[relative] = content
            replacements.append((relative, content))

        for name in _REPORT_NAMES:
            self._target(name)
            content = batch.reports[name]
            effective[name] = content
            replacements.append((name, content))

        return effective, replacements

    def commit(self, batch: ProjectionBatch) -> str:
        """Preflight, stage, and atomically replace one projection batch."""
        with self._lock:
            effective, replacements = self._preflight(batch)
            manifest = _build_manifest(batch, effective)
            latest_bytes = _json_bytes(manifest.model_dump(mode="json"))
            ordered = [*replacements, ("latest.json", latest_bytes)]
            temporary: dict[str, Path] = {}
            try:
                for relative, content in ordered:
                    temporary[relative] = self._write_temp(relative, content)
                for relative, _content in ordered:
                    os.replace(temporary[relative], self._target(relative))
            finally:
                for path in temporary.values():
                    path.unlink(missing_ok=True)
            return manifest.projection_id


__all__ = [
    "DocumentStore",
    "ProjectionBatch",
    "RunDocumentConflict",
]
