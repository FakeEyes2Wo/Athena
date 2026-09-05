"""Atomic persistence for immutable experiment-document projections."""

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
from athena.research.experiment_documents.render import (
    build_latest_manifest,
    render_latest_manifest,
    render_stage_record,
)

_REPORT_NAMES = ("FINAL_REPORT.md", "OPTIMIZATION.md")
_STAGE_ORDER = ("baseline", "search", "final")


@dataclass(frozen=True)
class RunCandidate:
    """One validated run archive proposed by a projection."""

    record: StageRecord
    content: bytes
    match_mode: Literal["exact", "recoverable"] = "exact"

    def __post_init__(self) -> None:
        if not isinstance(self.record, StageRecord):
            raise TypeError("record must be a StageRecord")
        if not isinstance(self.content, bytes):
            raise TypeError("content must be bytes")
        if self.match_mode not in {"exact", "recoverable"}:
            raise ValueError("match_mode must be exact or recoverable")


@dataclass(frozen=True)
class ProjectionBatch:
    """Immutable set of files committed as one projection."""

    kind: Literal["stage", "rebuild"]
    stage: StageName | None
    run_id: str | None
    runs: Sequence[RunCandidate]
    aliases: Mapping[StageName, str]
    reports: Mapping[Literal["FINAL_REPORT.md", "OPTIMIZATION.md"], bytes]

    def __post_init__(self) -> None:
        if self.kind not in {"stage", "rebuild"}:
            raise ValueError("kind must be stage or rebuild")
        if self.stage is not None and self.stage not in _STAGE_ORDER:
            raise ValueError("invalid stage")
        if self.kind == "stage" and (self.stage is None or self.run_id is None):
            raise ValueError("stage batches require stage and run_id")
        if self.kind == "rebuild" and (
            self.stage is not None or self.run_id is not None
        ):
            raise ValueError("rebuild batches cannot carry stage identity")

        runs = tuple(self.runs)
        if any(not isinstance(run, RunCandidate) for run in runs):
            raise TypeError("runs must contain RunCandidate values")
        run_ids = [run.record.run_id for run in runs]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run IDs must be unique")
        if self.kind == "stage" and self.run_id not in run_ids:
            raise ValueError("stage run_id must identify a run in the batch")

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

        object.__setattr__(self, "runs", runs)
        object.__setattr__(self, "aliases", MappingProxyType(aliases))
        object.__setattr__(self, "reports", MappingProxyType(reports))


class RunDocumentConflict(ValueError):
    """Raised when an existing run archive cannot be safely reused."""


def _record_payload(content: bytes, *, legacy: bool = False) -> StageRecord:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunDocumentConflict(
            "existing run archive is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RunDocumentConflict("existing run archive must contain an object")
    if legacy:
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


def _existing_record(content: bytes) -> StageRecord:
    """Parse an existing archive and select its explicit legacy compatibility mode."""
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunDocumentConflict(
            "existing run archive is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RunDocumentConflict("existing run archive must contain an object")
    return _record_payload(content, legacy="schema_version" not in payload)


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

        for candidate in sorted(batch.runs, key=lambda item: item.record.run_id):
            relative = f"runs/{candidate.record.run_id}.json"
            target = self._target(relative)
            expected = _record_payload(candidate.content)
            if expected != candidate.record:
                raise RunDocumentConflict("candidate content does not match its record")
            if target.exists():
                if not target.is_file():
                    raise RunDocumentConflict("run archive target is not a file")
                existing_bytes = target.read_bytes()
                existing = _existing_record(existing_bytes)
                if not _records_match(existing, candidate.record, candidate.match_mode):
                    raise RunDocumentConflict(
                        f"run archive {candidate.record.run_id} conflicts with existing bytes"
                    )
                selected[candidate.record.run_id] = existing_bytes
            else:
                selected[candidate.record.run_id] = candidate.content
                replacements.append((relative, candidate.content))
            effective[relative] = selected[candidate.record.run_id]

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
            manifest = build_latest_manifest(
                kind=batch.kind,
                stage=batch.stage,
                run_id=batch.run_id,
                files=effective,
            )
            latest_bytes = render_latest_manifest(manifest)
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
    "RunCandidate",
    "RunDocumentConflict",
]
