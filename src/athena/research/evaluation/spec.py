"""Dataset-neutral metadata for a frozen evaluator directory.

The evaluator is intentionally described by data rather than by Athena's
knowledge of a particular dataset.  New evaluators use the flat fields in
``metric.json``; old README-only evaluators that only declare ``eval_script``
continue to be accepted by callers using :func:`load_evaluator_spec`.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.research.contracts import EvaluatorDescriptor

DEFAULT_PREDICTION_ID_COLUMN = "__athena_row_id"
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class EvaluatorSpec(BaseModel):
    """Serializable prediction and metric contract for one evaluator.

    ``extra='allow'`` deliberately permits dataset-specific declarations (for
    example a threshold rule or label mapping) without requiring a framework
    release.  The fields consumed by Athena remain strict and stable.
    """

    model_config = ConfigDict(extra="allow", strict=True)

    contract_version: Literal[2]
    task_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    primary_metric: str = Field(min_length=1)
    class_labels: list[str] = Field(default_factory=list)
    prediction_file: str = Field(min_length=1)
    prediction_id_column: str = Field(min_length=1)
    prediction_column: str = Field(min_length=1)
    probability_columns: list[str] = Field(default_factory=list)
    metrics_file: str = Field(default="metrics_public_test.csv", min_length=1)
    eval_script: str = Field(default="eval_metrics.py", min_length=1)
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

    @field_validator("class_labels", mode="before")
    @classmethod
    def _class_names(cls, value: object) -> list[str]:
        """Normalize the complete classification label universe."""
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise TypeError("class_labels must be a list")
        labels = [str(label).strip() for label in value]
        if any(not label for label in labels) or len(labels) != len(set(labels)):
            raise ValueError("class_labels must contain unique non-empty values")
        return labels

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
        if self.task_type.endswith("classification") and len(self.class_labels) < 2:
            raise ValueError(
                "classification evaluators require the complete class_labels list"
            )
        return self


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


async def read_eval_handoff(
    store: ArtifactStore, evaluator_ref: ArtifactRef | None
) -> str:
    """Read the model-visible contract from a frozen evaluator."""
    if evaluator_ref is None:
        return ""
    try:
        descriptor = EvaluatorDescriptor.model_validate_json(
            await store.get_text(evaluator_ref)
        )
        return (Path(descriptor.dir_path) / "HANDOFF.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
