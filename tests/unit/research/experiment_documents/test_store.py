"""Tests for immutable and atomic experiment-document storage."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from athena.research.experiment_documents.models import (
    MetricRecord,
    ProvenanceRecord,
    ReasonRecord,
    StageRecord,
)
from athena.research.experiment_documents.store import (
    DocumentStore,
    ProjectionBatch,
    RunDocumentConflict,
    _render_stage_record,
)


@pytest.fixture
def record() -> StageRecord:
    return StageRecord(
        run_id="exp_search-1",
        stage="search",
        status="SUCCEEDED",
        metric=MetricRecord(
            name="score",
            direction="maximize",
            primary=0.8,
            reference=0.7,
            generalization_gap=0.1,
            secondary={"latency": 1.0},
        ),
        artifacts={"predictions": "artifact://predictions"},
        reason=ReasonRecord(kind="trusted_score", summary="scored"),
        provenance=ProvenanceRecord(phase="SEARCH", experiment_id="exp_search-1"),
    )


def make_batch(record: StageRecord) -> ProjectionBatch:
    return ProjectionBatch(
        kind="stage",
        records=(record,),
        aliases={record.stage: record.run_id},
        reports={"FINAL_REPORT.md": b"report", "OPTIMIZATION.md": b"optimization\n"},
    )


def changed_batch(batch: ProjectionBatch, primary: float) -> ProjectionBatch:
    record = batch.records[0]
    changed = record.model_copy(
        update={"metric": record.metric.model_copy(update={"primary": primary})}
    )
    return make_batch(changed)


def test_commit_creates_layout_and_digest_valid_latest(
    tmp_path: Path, record: StageRecord
) -> None:
    batch = make_batch(record)
    store = DocumentStore(tmp_path / ".athena" / "exp_docs")

    projection_id = store.commit(batch)

    root = store.root
    assert (root / "runs" / "exp_search-1.json").read_bytes() == _render_stage_record(
        batch.records[0]
    )
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["projection_id"] == projection_id
    for relative, digest in latest["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest


def test_same_semantics_preserve_existing_archive_bytes(
    tmp_path: Path, record: StageRecord
) -> None:
    batch = make_batch(record)
    store = DocumentStore(tmp_path / "docs")
    store.commit(batch)
    run_path = store.root / "runs" / "exp_search-1.json"
    reformatted = json.dumps(json.loads(run_path.read_text()), separators=(",", ":"))
    run_path.write_text(reformatted, encoding="utf-8")

    store.commit(batch)

    assert run_path.read_text(encoding="utf-8") == reformatted
    latest = json.loads((store.root / "latest.json").read_text())
    assert (
        latest["files"]["runs/exp_search-1.json"]
        == hashlib.sha256(reformatted.encode()).hexdigest()
    )


def test_unversioned_restored_shape_is_semantically_idempotent(
    tmp_path: Path, record: StageRecord
) -> None:
    record = record.model_copy(
        update={"metric": record.metric.model_copy(update={"primary": 1.0})}
    )
    batch = make_batch(record)
    store = DocumentStore(tmp_path / "docs")
    store.root.joinpath("runs").mkdir(parents=True)
    legacy = batch.records[0].model_dump(mode="json", exclude={"schema_version"})
    legacy["metric"]["primary"] = 1
    legacy_bytes = (json.dumps(legacy) + "\n").encode()
    (store.root / "runs" / "exp_search-1.json").write_bytes(legacy_bytes)

    store.commit(batch)

    assert (store.root / "runs" / "exp_search-1.json").read_bytes() == legacy_bytes
    latest = json.loads((store.root / "latest.json").read_text())
    assert (
        latest["files"]["runs/exp_search-1.json"]
        == hashlib.sha256(legacy_bytes).hexdigest()
    )


def test_same_run_with_different_content_never_overwrites(
    tmp_path: Path, record: StageRecord
) -> None:
    batch = make_batch(record)
    store = DocumentStore(tmp_path / "docs")
    store.commit(batch)
    before_run = (store.root / "runs" / "exp_search-1.json").read_bytes()
    before_latest = (store.root / "latest.json").read_bytes()

    with pytest.raises(RunDocumentConflict):
        store.commit(changed_batch(batch, 0.99))

    assert (store.root / "runs" / "exp_search-1.json").read_bytes() == before_run
    assert (store.root / "latest.json").read_bytes() == before_latest


def test_malformed_or_extra_field_existing_record_conflicts(
    tmp_path: Path, record: StageRecord
) -> None:
    store = DocumentStore(tmp_path / "docs")
    batch = make_batch(record)
    store.root.joinpath("runs").mkdir(parents=True)
    malformed = {"run_id": record.run_id, "extra": True}
    (store.root / "runs" / f"{record.run_id}.json").write_text(json.dumps(malformed))

    with pytest.raises(RunDocumentConflict):
        store.commit(batch)


def test_unknown_run_files_remain_untouched(
    tmp_path: Path, record: StageRecord
) -> None:
    store = DocumentStore(tmp_path / "docs")
    unknown = store.root / "runs" / "unknown.json"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"unknown bytes\n")

    store.commit(make_batch(record))

    assert unknown.read_bytes() == b"unknown bytes\n"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "runs\\escape"])
def test_unsafe_relative_paths_are_rejected(
    tmp_path: Path, name: str, record: StageRecord
) -> None:
    store = DocumentStore(tmp_path / "docs")
    with pytest.raises(ValueError):
        store._target(name)


def test_batch_rejects_bad_aliases_and_report_names(record: StageRecord) -> None:
    with pytest.raises(ValueError):
        ProjectionBatch(
            kind="stage",
            records=(record,),
            aliases={"baseline": "missing-run"},
            reports={"FINAL_REPORT.md": b"report", "OPTIMIZATION.md": b"opt"},
        )
    with pytest.raises(ValueError):
        ProjectionBatch(
            kind="stage",
            records=(record,),
            aliases={"search": record.run_id},
            reports={
                "FINAL_REPORT.md": b"report",
                "OPTIMIZATION.md": b"opt",
                "other.md": b"other",
            },
        )


def test_batch_defensively_copies_inputs(record: StageRecord) -> None:
    records = [record]
    aliases = {"search": record.run_id}
    reports = {"FINAL_REPORT.md": b"report", "OPTIMIZATION.md": b"opt"}
    batch = ProjectionBatch("stage", records, aliases, reports)
    records.clear()
    aliases.clear()
    reports.clear()

    assert len(batch.records) == 1
    assert dict(batch.aliases) == {"search": record.run_id}
    assert dict(batch.reports) == {
        "FINAL_REPORT.md": b"report",
        "OPTIMIZATION.md": b"opt",
    }


def test_commit_cleans_temps_after_replace_failure(
    tmp_path: Path, record: StageRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DocumentStore(tmp_path / "docs")
    batch = make_batch(record)
    store.commit(batch)
    old_latest = (store.root / "latest.json").read_bytes()
    calls = 0
    original = __import__(
        "athena.research.experiment_documents.store", fromlist=["os"]
    ).os.replace

    def fail_after_run(source: str | Path, target: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        original(source, target)

    monkeypatch.setattr(
        "athena.research.experiment_documents.store.os.replace", fail_after_run
    )
    new_record = record.model_copy(update={"run_id": "exp_search-2"})
    with pytest.raises(OSError):
        store.commit(make_batch(new_record))

    assert (store.root / "latest.json").read_bytes() == old_latest
    assert not list(store.root.rglob("*.tmp"))
    assert not list(store.root.rglob(".*.tmp"))


def test_replaces_are_serialized_and_latest_is_last(
    tmp_path: Path, record: StageRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DocumentStore(tmp_path / "docs")
    first = make_batch(record)
    second_record = record.model_copy(update={"run_id": "exp_search-2"})
    second = make_batch(second_record)
    replacements: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    original = __import__(
        "athena.research.experiment_documents.store", fromlist=["os"]
    ).os.replace

    def instrument(source: str | Path, target: str | Path) -> None:
        replacements.append(Path(target).relative_to(store.root).as_posix())
        if len(replacements) == 1:
            entered.set()
            assert release.wait(timeout=2)
        original(source, target)

    monkeypatch.setattr(
        "athena.research.experiment_documents.store.os.replace", instrument
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(store.commit, first)
        assert entered.wait(timeout=2)
        second_future = pool.submit(store.commit, second)
        release.set()
        first_future.result(timeout=2)
        final_id = second_future.result(timeout=2)

    assert replacements == [
        "runs/exp_search-1.json",
        "search.json",
        "FINAL_REPORT.md",
        "OPTIMIZATION.md",
        "latest.json",
        "runs/exp_search-2.json",
        "search.json",
        "FINAL_REPORT.md",
        "OPTIMIZATION.md",
        "latest.json",
    ]
    assert (
        json.loads((store.root / "latest.json").read_text())["projection_id"]
        == final_id
    )
