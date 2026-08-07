"""Deterministic artifact-backed dataset preparation for the application runtime."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from pandas.api.types import is_numeric_dtype

from athena.data.operations import deterministic_split
from athena.data.types import ColumnSummary, DataProfile, ProcessingLog
from athena.evaluation.types import EvalSpec
from athena.research.models import TaskMetaData
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory


@dataclass(frozen=True)
class PreparedWorkflowData:
    """Frozen PREPARE outputs consumed by baseline and SEARCH composition."""

    profile: DataProfile
    processing_log: ProcessingLog
    eval_spec: EvalSpec
    split_manifest_path: Path
    eval_spec_path: Path
    evaluator_path: Path


def _artifact_ref(path: Path) -> str:
    return f"artifact://{path.resolve()}"


def _write_bytes(root: Path, data: bytes, suffix: str) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    path = root / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return path


def _write_frame(root: Path, frame: pd.DataFrame) -> str:
    data = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return _artifact_ref(_write_bytes(root, data, ".csv"))


def _clean_partitions(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    *,
    target: str,
) -> tuple[list[pd.DataFrame], dict[str, str]]:
    partitions = [train.copy(), validation.copy(), test.copy()]
    processing: dict[str, str] = {}
    for column in train.columns:
        if column == target:
            processing[column] = (
                "Preserved target values; rows with missing targets removed"
            )
            continue
        if is_numeric_dtype(train[column]):
            value = train[column].median()
            if pd.isna(value):
                value = 0.0
            processing[column] = f"Filled missing values with training median {value!r}"
        else:
            value = "__MISSING__"
            processing[column] = "Filled missing values with __MISSING__"
        for partition in partitions:
            partition[column] = partition[column].fillna(value)
    return partitions, processing


def _profile(
    frame: pd.DataFrame,
    *,
    target: str,
    task: TaskMetaData,
    processing: dict[str, str],
) -> DataProfile:
    columns = [
        ColumnSummary(
            name=str(name),
            dtype=str(series.dtype),
            missing_rate=float(series.isna().mean()),
            n_unique=int(series.nunique(dropna=True)),
            sample_values=[str(value) for value in series.dropna().head(5).tolist()],
            processing=processing.get(str(name), ""),
        )
        for name, series in frame.items()
    ]
    return DataProfile(
        row_count=len(frame),
        col_count=len(frame.columns),
        columns=columns,
        missing_rate=float(frame.isna().mean().mean()) if columns else 0.0,
        task_type_hint=task.task_type,
        target_col=target,
        issue_summary="",
    )


def _prepare(
    data_path: Path,
    *,
    target: str,
    task: TaskMetaData,
    output_dir: Path,
    seed: int,
    validation_ratio: float,
    test_ratio: float,
) -> PreparedWorkflowData:
    source = data_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"dataset does not exist: {source}")
    output = output_dir.resolve()
    artifacts = output / "artifacts" / "data"
    config = output / "config"
    artifacts.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)

    raw_bytes = source.read_bytes()
    raw_copy = _artifact_ref(
        _write_bytes(artifacts, raw_bytes, source.suffix or ".csv")
    )
    frame = pd.read_csv(source)
    if target not in frame.columns:
        raise ValueError(f"target column not found: {target}")
    frame = frame.loc[frame[target].notna()].copy()
    if frame.empty:
        raise ValueError("dataset has no rows after removing missing targets")
    train, validation, test = deterministic_split(
        frame,
        seed=seed,
        validation_ratio=validation_ratio,
        test_ratio=test_ratio,
    )
    partitions, processing = _clean_partitions(train, validation, test, target=target)
    train, validation, test = partitions
    cleaned = pd.concat(partitions).sort_index()
    split_refs = {
        "train": _write_frame(artifacts, train),
        "validation": _write_frame(artifacts, validation),
        "test": _write_frame(artifacts, test),
    }
    processing_log = ProcessingLog(
        columns={
            summary.name: summary
            for summary in _profile(
                cleaned,
                target=target,
                task=task,
                processing=processing,
            ).columns
        },
        raw_copy=raw_copy,
        cleaned_data=_write_frame(artifacts, cleaned),
        splits=split_refs,
    )
    profile = _profile(
        cleaned,
        target=target,
        task=task,
        processing=processing,
    )
    eval_spec = EvaluatorFactory.build(task, profile)
    split_manifest_path = config / "splits.json"
    split_manifest_path.write_text(
        json.dumps(
            {"target": target, "splits": split_refs},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    eval_spec_path = config / "eval_spec.json"
    eval_spec_path.write_text(
        eval_spec.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    evaluator_path = config / "eval.py"
    evaluator_path.write_text(eval_spec.eval_script, encoding="utf-8")
    return PreparedWorkflowData(
        profile=profile,
        processing_log=processing_log,
        eval_spec=eval_spec,
        split_manifest_path=split_manifest_path,
        eval_spec_path=eval_spec_path,
        evaluator_path=evaluator_path,
    )


async def prepare_workflow_data(
    data_path: str | Path,
    *,
    target: str,
    task: TaskMetaData,
    output_dir: str | Path,
    seed: int = 42,
    validation_ratio: float = 0.2,
    test_ratio: float = 0.2,
) -> PreparedWorkflowData:
    """Prepare a CSV without blocking the async workflow control plane."""
    return await asyncio.to_thread(
        _prepare,
        Path(data_path),
        target=target,
        task=task,
        output_dir=Path(output_dir),
        seed=seed,
        validation_ratio=validation_ratio,
        test_ratio=test_ratio,
    )
