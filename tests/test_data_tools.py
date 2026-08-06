"""Test summary tools with real CSV data."""

import os
import tempfile
import pandas as pd
import pytest
from athena.data import tools as data_tools
from athena.data.tools import get_schema, get_summary, get_sample


@pytest.fixture
def sample_csv():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("age,income,city\n")
        f.write("25,50000,NYC\n")
        f.write("30,60000,LA\n")
        f.write("35,70000,NYC\n")
        f.write("40,80000,SF\n")
        f.write("45,90000,LA\n")
        path = f.name
    yield path
    os.unlink(path)


def test_get_schema(sample_csv):
    result = get_schema(sample_csv)
    assert "age" in result
    assert "income" in result
    assert "city" in result
    assert "int64" in result or "float" in result


def test_get_schema_size_limit(sample_csv):
    result = get_schema(sample_csv)
    assert len(result) <= 2048  # 2KB limit


def test_get_summary(sample_csv):
    result = get_summary(sample_csv)
    assert "mean" in result.lower() or "count" in result.lower()


def test_get_summary_size_limit(sample_csv):
    result = get_summary(sample_csv)
    assert len(result) <= 4096  # 4KB limit


def test_get_sample(sample_csv):
    result = get_sample(sample_csv, n=3)
    lines = result.strip().split("\n")
    assert len(lines) <= 4  # header + up to 3 rows


def test_get_sample_file_not_found():
    result = get_sample("/nonexistent/path.csv")
    assert "error" in result.lower() or result == ""


def test_large_dataset_uses_three_fixed_samples() -> None:
    frame = pd.DataFrame({"value": range(100_001)})
    stored: list[pd.DataFrame] = []

    def put_frame(sampled: pd.DataFrame) -> str:
        stored.append(sampled)
        return f"artifact://sample-{len(stored)}"

    refs = data_tools.create_analysis_samples(frame, put_frame=put_frame)

    assert [item.seed for item in refs] == [17, 42, 97]
    assert [item.artifact for item in refs] == [
        "artifact://sample-1",
        "artifact://sample-2",
        "artifact://sample-3",
    ]
    assert [len(sampled) for sampled in stored] == [10_000, 10_000, 10_000]


def test_small_dataset_uses_one_fixed_sample() -> None:
    frame = pd.DataFrame({"value": range(5)})
    first: list[pd.DataFrame] = []
    second: list[pd.DataFrame] = []

    first_refs = data_tools.create_analysis_samples(
        frame,
        put_frame=lambda sampled: first.append(sampled) or "artifact://first",
    )
    second_refs = data_tools.create_analysis_samples(
        frame,
        put_frame=lambda sampled: second.append(sampled) or "artifact://second",
    )

    assert [item.seed for item in first_refs] == [17]
    assert [item.seed for item in second_refs] == [17]
    assert set(first[0].index) == set(frame.index)
    assert first[0].index.tolist() == second[0].index.tolist()
