"""Regression tests for the frozen evaluator property probe's column choices."""

import csv
import io
import json
from types import SimpleNamespace

import pytest

from athena.research.supervisor.evaluator_plan import (
    _evaluator_layout,
    _resolve_prediction_column,
    _validate_frozen_evaluator,
)


class _ProbeScripts:
    """Score probe files using the same declared fields as the evaluator."""

    def __init__(self) -> None:
        self.headers: list[list[str]] = []

    async def run_dir(self, root, *, request, extra_files, output_schema):
        payload = next(iter(extra_files.values())).decode("utf-8")
        reader = csv.DictReader(io.StringIO(payload))
        self.headers.append(reader.fieldnames or [])
        predictions = {row["sample_key"]: row["pred_label"] for row in reader}
        labels = {"a": "0", "b": "1", "c": "0", "d": "1"}
        score = sum(predictions[key] == value for key, value in labels.items()) / 4
        return SimpleNamespace(outputs={"primary": score})


@pytest.mark.asyncio
async def test_frozen_probe_uses_metric_prediction_and_explicit_source_label(tmp_path):
    """Extra labels fields and misleading prose must not poison the probe."""
    metric = {
        "contract_version": 2,
        "task_id": "sample-classification",
        "task_type": "classification",
        "primary_metric": "accuracy",
        "class_labels": ["0", "1"],
        "prediction_file": "predictions__sample-classification.csv",
        "prediction_id_column": "sample_key",
        "prediction_column": "pred_label",
        "eval_script": "evaluate.py",
    }
    (tmp_path / "metric.json").write_text(json.dumps(metric), encoding="utf-8")
    (tmp_path / "labels.csv").write_text(
        "sample_key,sample_id,outcome\na,S-1,0\nb,S-2,1\nc,S-3,0\nd,S-4,1\n",
        encoding="utf-8",
    )
    (tmp_path / "HANDOFF.md").write_text(
        "The prediction column: misleading is mentioned in explanatory prose.\n",
        encoding="utf-8",
    )
    (tmp_path / "evaluate.py").write_text(
        "LABEL_COLUMN = 'outcome'\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='probe'\n", encoding="utf-8"
    )
    scripts = _ProbeScripts()

    await _validate_frozen_evaluator(
        root=tmp_path, scripts=scripts, layout=_evaluator_layout(tmp_path)
    )

    assert len(scripts.headers) == 3
    assert all(header == ["sample_key", "pred_label"] for header in scripts.headers)


@pytest.mark.asyncio
async def test_frozen_probe_rejects_ambiguous_target_with_repairable_message(tmp_path):
    metric = {
        "contract_version": 2,
        "task_id": "ambiguous-classification",
        "task_type": "classification",
        "primary_metric": "accuracy",
        "class_labels": ["0", "1"],
        "prediction_file": "predictions__ambiguous-classification.csv",
        "prediction_id_column": "sample_key",
        "prediction_column": "pred_label",
        "eval_script": "evaluate.py",
    }
    (tmp_path / "metric.json").write_text(json.dumps(metric), encoding="utf-8")
    (tmp_path / "labels.csv").write_text(
        "sample_key,sample_id,outcome\na,S-1,0\nb,S-2,1\n", encoding="utf-8"
    )
    (tmp_path / "HANDOFF.md").write_text("schema declared\n", encoding="utf-8")
    (tmp_path / "evaluate.py").write_text("def evaluate(): pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='probe'\n", encoding="utf-8"
    )

    with pytest.raises(
        ValueError, match="declare label_column in metric.json or LABEL_COLUMN"
    ):
        await _validate_frozen_evaluator(
            root=tmp_path,
            scripts=_ProbeScripts(),
            layout=_evaluator_layout(tmp_path),
        )


@pytest.mark.asyncio
async def test_frozen_evaluator_requires_label_ids_to_cover_its_feature_split(
    tmp_path,
):
    metric = {
        "contract_version": 2,
        "task_id": "sample-classification",
        "task_type": "classification",
        "primary_metric": "accuracy",
        "class_labels": ["0", "1"],
        "prediction_file": "predictions__sample-classification.csv",
        "prediction_id_column": "sample_key",
        "prediction_column": "pred_label",
        "label_column": "outcome",
        "eval_script": "evaluate.py",
    }
    (tmp_path / "metric.json").write_text(json.dumps(metric), encoding="utf-8")
    (tmp_path / "labels.csv").write_text(
        " sample_key ,outcome\na,0\nb,1\nc,0\nd,1\n", encoding="utf-8"
    )
    (tmp_path / "HANDOFF.md").write_text("schema declared\n", encoding="utf-8")
    (tmp_path / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='probe'\n", encoding="utf-8"
    )
    features = tmp_path / "search_features.csv"
    features.write_text(
        "feature, sample_key \n1,a\n2,b\n3,c\n4,unseen-row\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="do not exactly cover") as caught:
        await _validate_frozen_evaluator(
            root=tmp_path,
            scripts=_ProbeScripts(),
            layout=_evaluator_layout(tmp_path),
            expected_features=features,
        )
    assert "unseen-row" not in str(caught.value)


@pytest.mark.asyncio
async def test_nested_legacy_evaluator_coverage_uses_entrypoint_directory(tmp_path):
    evaluator_root = tmp_path / "evaluator"
    nested = evaluator_root / "bundle"
    nested.mkdir(parents=True)
    (evaluator_root / "metric.json").write_text(
        json.dumps({"eval_script": "bundle"}), encoding="utf-8"
    )
    (nested / "labels.csv").write_text(
        "__athena_row_id,outcome\na,0\nb,1\nc,0\nd,1\n", encoding="utf-8"
    )
    (nested / "evaluate.py").write_text("row['pred_label']\n", encoding="utf-8")
    (nested / "HANDOFF.md").write_text("schema declared\n", encoding="utf-8")
    (nested / "pyproject.toml").write_text(
        "[project]\nname='probe'\n", encoding="utf-8"
    )
    features = tmp_path / "search_features.csv"
    features.write_text(
        "__athena_row_id,feature\na,1\nb,2\nc,3\nd,4\n", encoding="utf-8"
    )

    class _NestedScripts:
        async def run_dir(self, root, *, request, extra_files, output_schema):
            del root, request, output_schema
            reader = csv.DictReader(
                io.StringIO(next(iter(extra_files.values())).decode("utf-8"))
            )
            values = {row["__athena_row_id"]: row["pred_label"] for row in reader}
            score = (
                sum(
                    values[key] == label
                    for key, label in {"a": "0", "b": "1", "c": "0", "d": "1"}.items()
                )
                / 4
            )
            return SimpleNamespace(outputs={"primary": score})

    await _validate_frozen_evaluator(
        root=evaluator_root,
        scripts=_NestedScripts(),
        layout=_evaluator_layout(evaluator_root),
        expected_features=features,
    )


@pytest.mark.asyncio
async def test_nested_v2_evaluator_uses_the_workspace_contract(tmp_path):
    """A nested entrypoint still uses the top-level v2 evaluator contract."""
    evaluator_root = tmp_path / "evaluator"
    nested = evaluator_root / "bundle"
    nested.mkdir(parents=True)
    (evaluator_root / "metric.json").write_text(
        json.dumps(
            {
                "contract_version": 2,
                "task_id": "nested-classification",
                "task_type": "classification",
                "primary_metric": "accuracy",
                "class_labels": ["0", "1"],
                "prediction_file": "predictions__nested-classification.csv",
                "prediction_id_column": "sample_key",
                "prediction_column": "pred_label",
                "label_column": "outcome",
                "eval_script": "bundle",
            }
        ),
        encoding="utf-8",
    )
    (nested / "labels.csv").write_text(
        "sample_key,outcome\na,0\nb,1\nc,0\nd,1\n", encoding="utf-8"
    )
    (nested / "evaluate.py").write_text("pass\n", encoding="utf-8")
    (nested / "HANDOFF.md").write_text("schema declared\n", encoding="utf-8")
    (nested / "pyproject.toml").write_text(
        "[project]\nname='probe'\n", encoding="utf-8"
    )
    features = tmp_path / "search_features.csv"
    features.write_text("sample_key,feature\na,1\nb,2\nc,3\nd,4\n", encoding="utf-8")

    await _validate_frozen_evaluator(
        root=evaluator_root,
        scripts=_ProbeScripts(),
        layout=_evaluator_layout(evaluator_root),
        expected_features=features,
    )


# The handoff prose below is the shape that broke a real run: the prose mentions
# an ordinary adverb while the machine-readable contract states the real column.
HANDOFF = """# FINAL evaluator

## Machine-readable declarations

```text
prediction_column: label
prediction_id_column: __athena_row_id
```

## Notes

The evaluator joins exactly on the identity column, so row order does not matter.
"""

SOURCE = """\
import pandas as pd

PREDICTION_FILE = "predictions__task.csv"
PREDICTION_ID_COLUMN = "__athena_row_id"


def load_predictions(path):
    frame = pd.read_csv(path)
    return frame["fault"].to_numpy()
"""


def _write(root, *, handoff: str | None, source: str | None) -> None:
    if handoff is not None:
        (root / "HANDOFF.md").write_text(handoff, encoding="utf-8")
    if source is not None:
        (root / "evaluate.py").write_text(source, encoding="utf-8")


def test_declared_contract_wins_over_handoff_prose(tmp_path) -> None:
    """A declared column must never be overridden by a word from the prose.

    Deriving the column from HANDOFF.md first let an arbitrary word override the
    declared name. The probe then wrote a prediction file the evaluator
    rejected, every scoring call returned the evaluator's error sentinel, and
    the value-permutation check saw an unchanged score — rejecting a correct
    evaluator and failing the whole run in PREPARE.
    """
    _write(tmp_path, handoff=HANDOFF, source=SOURCE)

    assert _resolve_prediction_column("label", tmp_path, "evaluate.py") == "label"


def test_falls_back_to_source_when_nothing_is_declared(tmp_path) -> None:
    """Legacy descriptors carry no column, so the source is still consulted."""
    _write(tmp_path, handoff=None, source=SOURCE)

    assert _resolve_prediction_column(None, tmp_path, "evaluate.py") == "fault"


def test_raises_when_no_column_can_be_resolved(tmp_path) -> None:
    _write(tmp_path, handoff="# Evaluator\n\nNo declarations here.\n", source=None)

    with pytest.raises(ValueError, match="cannot determine the tabular prediction"):
        _resolve_prediction_column(None, tmp_path, "evaluate.py")
