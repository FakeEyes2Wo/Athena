from pydantic import BaseModel, Field
from athena.core.schemas import ArtifactRef


class ColumnSummary(BaseModel):
    name: str
    dtype: str
    missing_rate: float = 0.0
    n_unique: int | None = None
    sample_values: list[str] = Field(default_factory=list)
    processing: str = ""


class DataProfile(BaseModel):
    row_count: int
    col_count: int
    columns: list[ColumnSummary] = Field(default_factory=list)
    missing_rate: float = 0.0
    task_type_hint: str = ""
    target_col: str | None = None
    issue_summary: str = ""


class ProcessingLog(BaseModel):
    columns: dict[str, ColumnSummary] = Field(default_factory=dict)
    raw_copy: ArtifactRef = ""
    cleaned_data: ArtifactRef = ""
    splits: dict[str, ArtifactRef] = Field(default_factory=dict)


class DataTools:
    """Tools exposed to DataAgent for dataset interaction. Avoid stuffing data into context."""

    def __init__(self, data_path: str):
        self._path = data_path

    def sample(self, seed: int, n: int = 10000) -> ArtifactRef:
        import pandas as pd

        df = pd.read_csv(self._path)
        sampled = df.sample(n=min(n, len(df)), random_state=seed)
        out = f"{self._path}.sample_{seed}.csv"
        sampled.to_csv(out, index=False)
        return f"artifact://{out}"

    def describe(self, data_ref: ArtifactRef) -> str:
        import pandas as pd

        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        return df.describe(include="all").to_string()

    def head(self, data_ref: ArtifactRef, n: int = 5) -> str:
        import pandas as pd

        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        return df.head(n).to_string()

    def missing_matrix(self, data_ref: ArtifactRef) -> ArtifactRef:
        import pandas as pd

        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        missing = df.isnull().sum()
        out = f"{path}.missing.csv"
        missing.to_csv(out)
        return f"artifact://{out}"

    def correlation_matrix(self, data_ref: ArtifactRef) -> ArtifactRef:
        import pandas as pd

        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        corr = df.corr(numeric_only=True)
        out = f"{path}.corr.csv"
        corr.to_csv(out)
        return f"artifact://{out}"
