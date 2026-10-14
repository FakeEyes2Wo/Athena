"""Regression cover for the prediction-column resolution used by the freeze probe."""

from __future__ import annotations

import pytest

from athena.research.supervisor.evaluator_plan import _resolve_prediction_column

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

SOURCE = '''\
import pandas as pd

PREDICTION_FILE = "predictions__task.csv"
PREDICTION_ID_COLUMN = "__athena_row_id"


def load_predictions(path):
    frame = pd.read_csv(path)
    return frame["fault"].to_numpy()
'''


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
