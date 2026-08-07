"""Data domain types (Codex types.rs pattern)."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from athena.core.contracts import ArtifactRef


class DataCard(BaseModel):
    """数据集卡片 — 通过 artifact 引用描述数据集指纹、schema 和划分。"""

    dataset_ref: ArtifactRef
    fingerprint: str
    schema_ref: ArtifactRef
    split_manifest_ref: ArtifactRef | None = None


class ColumnSummary(BaseModel):
    """Statistical summary of a single column in a dataset."""

    name: str
    dtype: str
    missing_rate: float = 0.0
    n_unique: int | None = None
    sample_values: list[str] = Field(default_factory=list)
    processing: str = ""


class DataProfile(BaseModel):
    """Profile of a dataset: row/column counts, column summaries, task hints."""

    row_count: int
    col_count: int
    columns: list[ColumnSummary] = Field(default_factory=list)
    missing_rate: float = 0.0
    task_type_hint: str = ""
    target_col: str | None = None
    issue_summary: str = ""


class ProcessingLog(BaseModel):
    """Record of data processing operations on columns and splits."""

    columns: dict[str, ColumnSummary] = Field(default_factory=dict)
    raw_copy: ArtifactRef
    cleaned_data: ArtifactRef
    splits: dict[str, ArtifactRef] = Field(default_factory=dict)


@dataclass
class ProcessingRecord:
    """Record of a single processing operation applied to a column."""

    col: str
    operation: str
    params: dict = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class SampleRef:
    seed: int
    artifact: ArtifactRef


@dataclass(frozen=True)
class SplitManifest:
    train: ArtifactRef
    validation: ArtifactRef
    test: ArtifactRef


if __name__ == "__main__":
    col = ColumnSummary(name="age", dtype="int64", missing_rate=0.05, n_unique=50)
    profile = DataProfile(row_count=1000, col_count=10, columns=[col])
    record = ProcessingRecord(col="age", operation="fillna", params={"value": 0})
    print(f"ColumnSummary: {col}")
    print(f"DataProfile: {profile}")
    print(f"ProcessingRecord: {record}")
