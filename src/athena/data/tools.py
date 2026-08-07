"""Summary tools for DataAgent — eyes, not brain."""

from collections.abc import Callable

import pandas as pd

from athena.core.contracts import ArtifactRef
from athena.data.types import SampleRef

ANALYSIS_SEEDS = (17, 42, 97)


def create_analysis_samples(
    frame: pd.DataFrame,
    *,
    put_frame: Callable[[pd.DataFrame], ArtifactRef],
    sample_size: int = 10_000,
) -> list[SampleRef]:
    seeds = ANALYSIS_SEEDS if len(frame) > 100_000 else (ANALYSIS_SEEDS[0],)
    return [
        SampleRef(
            seed=seed,
            artifact=put_frame(
                frame.sample(min(sample_size, len(frame)), random_state=seed)
            ),
        )
        for seed in seeds
    ]


def _read_safe(data_path: str) -> pd.DataFrame | None:
    """Read CSV safely, return None on failure."""
    try:
        return pd.read_csv(data_path)
    except Exception:
        return None


def get_schema(data_path: str) -> str:
    """Return column names + dtypes, max 2KB."""
    df = _read_safe(data_path)
    if df is None:
        return ""
    lines = [f"{col}: {str(dtype)}" for col, dtype in df.dtypes.items()]
    result = "\n".join(lines)
    if len(result) > 2048:
        result = result[:2045] + "..."
    return result


def get_summary(data_path: str) -> str:
    """Return df.describe() text, max 4KB."""
    df = _read_safe(data_path)
    if df is None:
        return ""
    result = df.describe(include="all").to_string()
    if len(result) > 4096:
        result = result[:4093] + "..."
    return result


def get_sample(data_path: str, n: int = 5) -> str:
    """Return first n rows as text."""
    df = _read_safe(data_path)
    if df is None:
        return ""
    return df.head(n).to_string()
