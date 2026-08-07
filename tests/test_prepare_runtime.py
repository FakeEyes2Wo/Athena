from pathlib import Path

import pandas as pd
import pytest

from athena.research.models import MetricSpec, TaskMetaData
from athena.workflows.prepare.runtime import prepare_workflow_data


def _task() -> TaskMetaData:
    return TaskMetaData(
        task_type="classification",
        data_type="tabular",
        target_vars=["label"],
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )


@pytest.mark.asyncio
async def test_prepare_workflow_data_creates_phase_scoped_private_labels(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    frame = pd.DataFrame(
        {
            "number": [1.0, None, 100.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "category": ["a", None, "b", "a", "b", "a", "b", "a", "b", "a"],
            "label": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
        }
    )
    frame.to_csv(source, index=False)
    original = source.read_bytes()

    prepared = await prepare_workflow_data(
        source,
        target="label",
        task=_task(),
        output_dir=tmp_path / "run",
        seed=42,
        validation_ratio=0.2,
        test_ratio=0.2,
    )

    assert source.read_bytes() == original
    assert (
        Path(prepared.processing_log.raw_copy.removeprefix("artifact://")).read_bytes()
        == original
    )
    assert prepared.profile.row_count == 10
    assert prepared.profile.target_col == "label"
    assert prepared.eval_spec.eval_script
    train = pd.read_csv(prepared.validation_inputs.train_path)
    validation = prepared.validation_inputs
    test = prepared.test_inputs
    validation_features = pd.read_csv(validation.features_path)
    validation_labels = pd.read_csv(validation.labels_path)
    test_features = pd.read_csv(test.features_path)
    test_labels = pd.read_csv(test.labels_path)

    assert "__athena_row_id" in train.columns
    assert "label" in train.columns
    assert list(validation_labels.columns) == ["__athena_row_id", "label"]
    assert list(test_labels.columns) == ["__athena_row_id", "label"]
    assert "label" not in validation_features.columns
    assert "label" not in test_features.columns
    assert validation.row_id_column == "__athena_row_id"
    assert set(validation_features["__athena_row_id"]) == set(
        validation_labels["__athena_row_id"]
    )
    assert set(test_features["__athena_row_id"]) == set(test_labels["__athena_row_id"])
    split_ids = [
        set(train["__athena_row_id"]),
        set(validation_features["__athena_row_id"]),
        set(test_features["__athena_row_id"]),
    ]
    assert all(split_ids)
    assert not (split_ids[0] & split_ids[1])
    assert not (split_ids[0] & split_ids[2])
    assert not (split_ids[1] & split_ids[2])
    assert set.union(*split_ids) == set(range(10))
    assert all(
        not part.isna().any().any()
        for part in [train, validation_features, test_features]
    )


@pytest.mark.asyncio
async def test_prepare_workflow_data_rejects_missing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    pd.DataFrame({"feature": [1, 2]}).to_csv(source, index=False)

    with pytest.raises(ValueError, match="target column"):
        await prepare_workflow_data(
            source,
            target="label",
            task=_task(),
            output_dir=tmp_path / "run",
        )


@pytest.mark.asyncio
async def test_prepare_workflow_data_rejects_reserved_row_id(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    pd.DataFrame({"__athena_row_id": [1, 2], "label": [0, 1]}).to_csv(
        source, index=False
    )

    with pytest.raises(ValueError, match="__athena_row_id"):
        await prepare_workflow_data(
            source,
            target="label",
            task=_task(),
            output_dir=tmp_path / "run",
        )


@pytest.mark.asyncio
async def test_prepare_workflow_data_rejects_empty_split(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    pd.DataFrame({"feature": [1, 2, 3], "label": [0, 1, 0]}).to_csv(source, index=False)

    with pytest.raises(
        ValueError,
        match="dataset must produce non-empty train, validation, and test splits",
    ):
        await prepare_workflow_data(
            source,
            target="label",
            task=_task(),
            output_dir=tmp_path / "run",
        )
