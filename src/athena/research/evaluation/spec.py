"""Dataset-neutral metadata for a frozen evaluator directory.

The evaluator is intentionally described by data rather than by Athena's
knowledge of a particular dataset.  New evaluators use the flat fields in
``metric.json``; old README-only evaluators that only declare ``eval_script``
continue to be accepted by callers using :func:`load_evaluator_spec`.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_EVAL_SCRIPT = "eval_metrics.py"
DEFAULT_METRICS_FILE = "metrics_public_test.csv"
DEFAULT_PREDICTION_ID_COLUMN = "__athena_row_id"
DEFAULT_PREDICTION_COLUMN = "prediction"
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class EvaluatorSpec(BaseModel):
    """Serializable prediction and metric contract for one evaluator.

    ``extra='allow'`` deliberately permits dataset-specific declarations (for
    example a threshold rule or label mapping) without requiring a framework
    release.  The fields consumed by Athena remain strict and stable.
    """

    model_config = ConfigDict(extra="allow", strict=True)

    task_id: str = Field(min_length=1)
    prediction_file: str = Field(min_length=1)
    prediction_id_column: str = Field(min_length=1)
    prediction_column: str = Field(min_length=1)
    probability_columns: list[str] = Field(default_factory=list)
    metrics_file: str = Field(default=DEFAULT_METRICS_FILE, min_length=1)
    eval_script: str = Field(default=DEFAULT_EVAL_SCRIPT, min_length=1)
    prediction_format: str = Field(default="tabular_csv", min_length=1)

    @field_validator("prediction_file", "metrics_file", "eval_script")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        """Reject absolute or escaping paths in evaluator metadata."""
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("evaluator paths must be relative to evaluate/")
        return path.as_posix()

    @field_validator("prediction_id_column", "prediction_column")
    @classmethod
    def _column_name(cls, value: str) -> str:
        """Require non-empty CSV field names without imposing a dataset schema."""
        if not value.strip() or "," in value or "\n" in value:
            raise ValueError("prediction column names must be simple CSV fields")
        return value.strip()

    @field_validator("probability_columns", mode="before")
    @classmethod
    def _probability_names(cls, value: object) -> list[str]:
        """Normalize optional probability columns while preserving declaration order."""
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise TypeError("probability_columns must be a list of field names")
        names = [name.strip() for name in value]
        if any(not name or "," in name or "\n" in name for name in names):
            raise ValueError("probability column names must be simple CSV fields")
        if len(set(names)) != len(names):
            raise ValueError("probability_columns must be unique")
        return names

    @model_validator(mode="after")
    def _validate_contract(self) -> "EvaluatorSpec":
        """Ensure the task-specific prediction filename is deterministic."""
        if not _SAFE_TASK_ID.fullmatch(self.task_id):
            raise ValueError(
                "task_id must contain only letters, digits, '.', '-' or '_'"
            )
        expected = f"predictions__{self.task_id}.csv"
        if self.prediction_format == "tabular_csv" and self.prediction_file != expected:
            raise ValueError(
                f"prediction_file must be {expected!r} for task_id {self.task_id!r}"
            )
        all_columns = {
            self.prediction_id_column,
            self.prediction_column,
            *self.probability_columns,
        }
        declared_count = 2 + len(self.probability_columns)
        if len(all_columns) != declared_count:
            raise ValueError(
                "prediction_id_column, prediction_column, and probability_columns "
                "must be distinct"
            )
        return self


def prediction_filename(task_id: str) -> str:
    """Return the standard public prediction filename for a task identifier."""
    if not _SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    return f"predictions__{task_id}.csv"


def load_metric_json(root: Path) -> dict[str, Any]:
    """Read one evaluator's metric.json without applying legacy defaults."""
    path = Path(root) / "metric.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read evaluator metric.json: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError("metric.json must contain a JSON object")
    return payload


def load_evaluator_spec(root: Path, *, legacy_ok: bool = True) -> EvaluatorSpec | None:
    """Load the new flat evaluator contract, or ``None`` for a legacy bundle.

    Legacy descriptors are still valid during migration.  They are identified
    by the absence of ``task_id`` and retain the old ``eval_script`` behavior.
    A partially specified new contract is rejected instead of silently filling
    in dataset-specific values.
    """
    payload = load_metric_json(root)
    if "task_id" not in payload:
        if legacy_ok:
            return None
        raise ValueError("metric.json must declare task_id")
    try:
        return EvaluatorSpec.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid evaluator metric.json: {exc}") from exc


__all__ = [
    "DEFAULT_EVAL_SCRIPT",
    "DEFAULT_METRICS_FILE",
    "DEFAULT_PREDICTION_COLUMN",
    "DEFAULT_PREDICTION_ID_COLUMN",
    "EvaluatorSpec",
    "load_evaluator_spec",
    "load_metric_json",
    "prediction_filename",
]
