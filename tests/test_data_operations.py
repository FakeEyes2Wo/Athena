"""Test data operations: sample, clean, encode, split."""
import os
import tempfile
from dataclasses import fields
from datetime import datetime, timezone
import pandas as pd
import pytest
from athena.data import operations as data_operations
from athena.data.operations import sample, split_data, clean_column, apply_encoding, ProcessingRecord


def test_processing_record_is_owned_by_data_types() -> None:
    from athena.data.operations import ProcessingRecord as imported
    from athena.data.types import ProcessingRecord as owned

    assert imported is owned
    assert [field.name for field in fields(owned)] == [
        "col",
        "operation",
        "params",
        "timestamp",
    ]


def test_processing_record_keeps_existing_defaults() -> None:
    record = ProcessingRecord(col="age", operation="fillna")

    assert record.params == {}
    assert datetime.fromisoformat(record.timestamp).utcoffset() is not None


@pytest.fixture
def sample_csv():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("x,y,label\n")
        for i in range(100):
            f.write(f"{i},{i * 2},{i % 3}\n")
        path = f.name
    yield path
    os.unlink(path)


def test_sample_creates_file(sample_csv):
    ref = sample(sample_csv, seed=42, n=10)
    assert ref.startswith("artifact://")
    sampled_path = ref.replace("artifact://", "")
    assert os.path.exists(sampled_path)


def test_sample_deterministic(sample_csv):
    ref1 = sample(sample_csv, seed=42, n=10)
    ref2 = sample(sample_csv, seed=42, n=10)
    assert ref1 == ref2


def test_sample_respects_n(sample_csv):
    ref = sample(sample_csv, seed=42, n=3)
    sampled_path = ref.replace("artifact://", "")
    import pandas as pd
    df = pd.read_csv(sampled_path)
    assert len(df) == 3


def test_split_data(sample_csv):
    splits = split_data(sample_csv, test_ratio=0.3, seed=42)
    assert "train" in splits
    assert "test" in splits
    assert splits["train"].startswith("artifact://")
    assert splits["test"].startswith("artifact://")


def test_processing_record():
    r = ProcessingRecord(col="age", operation="fillna", params={"value": 0})
    assert r.col == "age"
    assert r.operation == "fillna"
    assert r.params == {"value": 0}


def test_clean_column_fillna(sample_csv):
    r = clean_column(sample_csv, "x", "fillna", {"value": 0})
    assert r.operation == "fillna"


def test_apply_encoding_label(sample_csv):
    r = apply_encoding(sample_csv, "label", "label")
    assert r.operation == "encode_label"


def test_apply_operation_copies_frame_and_records_trace() -> None:
    frame = pd.DataFrame({"age": [10.0, None, 30.0], "label": [0, 1, 0]})
    params = {"value": 20.0}
    instant = datetime(2026, 7, 29, 4, 5, tzinfo=timezone.utc)

    transformed, record = data_operations.apply_operation(
        frame,
        column="age",
        operation="fillna",
        params=params,
        now=lambda: instant,
    )

    assert frame["age"].isna().sum() == 1
    assert transformed["age"].tolist() == [10.0, 20.0, 30.0]
    assert record.col == "age"
    assert record.operation == "fillna"
    assert record.params == {"value": 20.0}
    assert record.params is not params
    assert record.timestamp == "2026-07-29T04:05:00+00:00"


def test_apply_operation_rejects_naive_timestamp() -> None:
    frame = pd.DataFrame({"age": [10.0, None]})

    with pytest.raises(ValueError, match="timezone-aware"):
        data_operations.apply_operation(
            frame,
            column="age",
            operation="fillna",
            params={"value": 20.0},
            now=lambda: datetime(2026, 7, 29, 4, 5),
        )


def test_apply_operation_rejects_uninferable_fill_value() -> None:
    frame = pd.DataFrame({"age": [float("nan"), float("nan")]})

    with pytest.raises(ValueError, match="cannot infer fill value"):
        data_operations.apply_operation(
            frame,
            column="age",
            operation="fillna",
            params={},
            now=lambda: datetime(2026, 7, 29, 4, 5, tzinfo=timezone.utc),
        )


def test_split_manifest_is_disjoint_and_complete() -> None:
    frame = pd.DataFrame({"value": range(20)})
    stored: dict[str, pd.DataFrame] = {}

    def put_frame(partition: pd.DataFrame) -> str:
        ref = f"artifact://partition-{len(stored)}"
        stored[ref] = partition
        return ref

    manifest = data_operations.split_dataset(
        frame,
        seed=42,
        validation_ratio=0.2,
        test_ratio=0.2,
        put_frame=put_frame,
    )
    train, validation, test = (
        set(stored[ref].index)
        for ref in (manifest.train, manifest.validation, manifest.test)
    )

    assert not (train & validation or train & test or validation & test)
    assert train | validation | test == set(range(len(frame)))
    assert [len(train), len(validation), len(test)] == [12, 4, 4]
