"""Data analysis tools exposed to DataAgent for dataset interaction."""

import pandas as pd

from athena.core.contracts import ArtifactRef
from athena.data.types import ColumnSummary, DataProfile, ProcessingLog


class DataTools:
    """暴露给 DataAgent 的数据集交互工具。避免将数据塞入上下文。"""

    def __init__(self, data_path: str):
        self._path = data_path

    def sample(self, seed: int, n: int = 10000) -> ArtifactRef:
        """Create a random sample of the dataset with the given seed."""
        df = pd.read_csv(self._path)
        sampled = df.sample(n=min(n, len(df)), random_state=seed)
        out = f"{self._path}.sample_{seed}.csv"
        sampled.to_csv(out, index=False)
        return f"artifact://{out}"

    def describe(self, data_ref: ArtifactRef) -> str:
        """Return df.describe() text for the artifact-referenced CSV."""
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        return df.describe(include="all").to_string()

    def head(self, data_ref: ArtifactRef, n: int = 5) -> str:
        """Return first n rows of the artifact-referenced CSV."""
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        return df.head(n).to_string()

    def missing_matrix(self, data_ref: ArtifactRef) -> ArtifactRef:
        """Create missing-value summary CSV and return artifact ref."""
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        missing = df.isnull().sum()
        out = f"{path}.missing.csv"
        missing.to_csv(out)
        return f"artifact://{out}"

    def correlation_matrix(self, data_ref: ArtifactRef) -> ArtifactRef:
        """Create correlation matrix CSV and return artifact ref."""
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        corr = df.corr(numeric_only=True)
        out = f"{path}.corr.csv"
        corr.to_csv(out)
        return f"artifact://{out}"
