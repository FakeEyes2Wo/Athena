"""Deterministic task/data readiness checks around Supervisor understanding."""

import csv
import re
from pathlib import Path

from athena.core.research_models import TaskMissingItem, TaskUnderstanding

_PATH_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".parquet", ".xlsx"}


def _missing(field: str, reason: str, question: str) -> TaskMissingItem:
    return TaskMissingItem(
        field=field, severity="critical", reason=reason, question=question
    )


def _looks_like_path(value: str) -> bool:
    candidate = value.strip().strip("\"'")
    return (
        "/" in candidate
        or "\\" in candidate
        or Path(candidate).suffix.lower() in _PATH_SUFFIXES
    )


def _target_name(value: str) -> str:
    return re.split(r"\s*[:：]\s*", value.strip(), maxsplit=1)[0].strip()


def _table_columns(path: Path) -> list[str] | None:
    if path.suffix.lower() not in {".csv", ".tsv"}:
        return None
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle, delimiter=delimiter), [])


def assess_task_readiness(
    understanding: TaskUnderstanding, *, project_root: Path
) -> TaskUnderstanding:
    """Return a readiness-normalized copy without guessing scientific choices."""
    critical: list[TaskMissingItem] = [
        item for item in understanding.missing_items if item.severity == "critical"
    ]
    warnings = list(understanding.warnings)
    if not understanding.dataset.strip():
        critical.append(
            _missing(
                "dataset",
                "No dataset name, location, or access method was provided.",
                "Which dataset should Athena use, and where can it be accessed?",
            )
        )
    elif _looks_like_path(understanding.dataset):
        raw = understanding.dataset.strip().strip("\"'")
        dataset_path = Path(raw)
        if not dataset_path.is_absolute():
            dataset_path = project_root / dataset_path
        if not dataset_path.exists():
            critical.append(
                _missing(
                    "dataset",
                    f"The declared local dataset does not exist: {raw}",
                    "Please provide a valid dataset path or place the file at the declared path.",
                )
            )
        elif dataset_path.is_file() and dataset_path.stat().st_size == 0:
            critical.append(
                _missing(
                    "dataset",
                    f"The declared dataset is empty: {raw}",
                    "Please provide a non-empty dataset file.",
                )
            )
        elif dataset_path.is_file() and understanding.target.strip():
            try:
                columns = _table_columns(dataset_path)
            except (OSError, UnicodeError, csv.Error) as error:
                critical.append(
                    _missing(
                        "dataset",
                        f"The declared dataset header cannot be read: {error}",
                        "Please provide a readable UTF-8 CSV/TSV file or describe its format.",
                    )
                )
                columns = None
            target = _target_name(understanding.target)
            if columns is not None and target and target not in columns:
                critical.append(
                    _missing(
                        "target",
                        f"Target column {target!r} is absent from the dataset header.",
                        f"Which column is the prediction target? Available columns include: "
                        f"{', '.join(columns[:12])}",
                    )
                )
    if understanding.task_type == "other":
        critical.append(
            _missing(
                "task_type",
                "The scientific or machine-learning task type is unresolved.",
                "Is this classification, regression, vision, generation, or another task?",
            )
        )
    if (
        understanding.task_type in {"classification", "regression"}
        and not understanding.target.strip()
    ):
        critical.append(
            _missing(
                "target",
                "Supervised learning requires a target variable.",
                "What target column or outcome should the model predict?",
            )
        )
    if understanding.primary_metric is None:
        warnings.append(
            "No higher-priority primary metric is locked; Layer 1 must select one."
        )
    if not understanding.evaluation_plan.strip():
        warnings.append(
            "Evaluation details are incomplete and must be resolved by Layer 1."
        )
    if not understanding.constraints:
        warnings.append("No explicit runtime or resource constraints were supplied.")

    unique: dict[tuple[str, str], TaskMissingItem] = {}
    for item in critical:
        unique[(item.field, item.reason)] = item
    missing_items = list(unique.values())
    questions = list(
        dict.fromkeys(item.question for item in missing_items if item.question)
    )
    return understanding.model_copy(
        update={
            "readiness": "NEEDS_INPUT" if missing_items else "READY",
            "missing_items": missing_items,
            "clarification_questions": questions,
            "warnings": list(dict.fromkeys(warnings)),
        }
    )


__all__ = ["assess_task_readiness"]
