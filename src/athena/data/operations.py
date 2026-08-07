"""Data operations: sample, clean, encode, split. Deterministic, no LLM."""

import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

import pandas as pd

from athena.core.contracts import ArtifactRef
from athena.data.types import ProcessingRecord, SplitManifest

OperationHandler = Callable[[pd.DataFrame, str, Mapping[str, object]], pd.DataFrame]


def _fillna(
    frame: pd.DataFrame, column: str, params: Mapping[str, object]
) -> pd.DataFrame:
    fill_value = params.get("value", frame[column].median())
    if "value" not in params and pd.isna(fill_value):
        raise ValueError("cannot infer fill value for all-NaN column")
    frame[column] = frame[column].fillna(fill_value)
    return frame


def _clip(
    frame: pd.DataFrame, column: str, params: Mapping[str, object]
) -> pd.DataFrame:
    frame[column] = frame[column].clip(
        lower=params.get("lower"), upper=params.get("upper")
    )
    return frame


def _replace(
    frame: pd.DataFrame, column: str, params: Mapping[str, object]
) -> pd.DataFrame:
    frame[column] = frame[column].replace(params.get("to_replace"), params.get("value"))
    return frame


def _onehot(
    frame: pd.DataFrame, column: str, params: Mapping[str, object]
) -> pd.DataFrame:
    del params
    dummies = pd.get_dummies(frame[column], prefix=column)
    return pd.concat([frame.drop(columns=[column]), dummies], axis=1)


def _label(
    frame: pd.DataFrame, column: str, params: Mapping[str, object]
) -> pd.DataFrame:
    del params
    frame[column] = frame[column].astype("category").cat.codes
    return frame


OPERATION_HANDLERS: dict[str, OperationHandler] = {
    "fillna": _fillna,
    "clip": _clip,
    "replace": _replace,
    "onehot": _onehot,
    "label": _label,
    "encode_onehot": _onehot,
    "encode_label": _label,
}


def apply_operation(
    frame: pd.DataFrame,
    *,
    column: str,
    operation: str,
    params: Mapping[str, object],
    now: Callable[[], datetime],
) -> tuple[pd.DataFrame, ProcessingRecord]:
    if column not in frame.columns:
        raise ValueError(f"Column '{column}' not found")
    instant = now()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must return a timezone-aware datetime")
    transformed = OPERATION_HANDLERS[operation](frame.copy(), column, params)
    return transformed, ProcessingRecord(
        col=column,
        operation=operation,
        params=dict(params),
        timestamp=instant.astimezone(timezone.utc).isoformat(),
    )


def deterministic_split(
    frame: pd.DataFrame,
    seed: int,
    validation_ratio: float,
    test_ratio: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if validation_ratio < 0 or test_ratio < 0 or validation_ratio + test_ratio >= 1:
        raise ValueError(
            "validation_ratio and test_ratio must be non-negative and sum below 1"
        )
    if not frame.index.is_unique:
        raise ValueError("frame index must be unique")
    shuffled = frame.sample(frac=1, random_state=seed)
    test_count = int(len(frame) * test_ratio)
    validation_count = int(len(frame) * validation_ratio)
    test = shuffled.iloc[:test_count].copy()
    validation = shuffled.iloc[test_count : test_count + validation_count].copy()
    train = shuffled.iloc[test_count + validation_count :].copy()
    return train, validation, test


def validate_disjoint_indices(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    *,
    expected: set[object],
) -> None:
    train_indices, validation_indices, test_indices = map(
        set, (train.index, validation.index, test.index)
    )
    if (
        train_indices & validation_indices
        or train_indices & test_indices
        or validation_indices & test_indices
        or train_indices | validation_indices | test_indices != expected
    ):
        raise ValueError("split indices must be disjoint and complete")


def split_dataset(
    frame: pd.DataFrame,
    *,
    seed: int,
    validation_ratio: float,
    test_ratio: float,
    put_frame: Callable[[pd.DataFrame], ArtifactRef],
) -> SplitManifest:
    train, validation, test = deterministic_split(
        frame, seed, validation_ratio, test_ratio
    )
    validate_disjoint_indices(train, validation, test, expected=set(frame.index))
    return SplitManifest(
        train=put_frame(train),
        validation=put_frame(validation),
        test=put_frame(test),
    )


def sample(data_path: str, seed: int, n: int = 10000) -> ArtifactRef:
    """Random sample with seed. Returns ArtifactRef to sampled CSV."""
    df = pd.read_csv(data_path)
    sampled = df.sample(n=min(n, len(df)), random_state=seed)
    stem = os.path.splitext(data_path)[0]
    out = f"{stem}.sample_{seed}.csv"
    sampled.to_csv(out, index=False)
    return f"artifact://{out}"


def clean_column(
    data_path: str, col: str, method: str, params: dict | None = None
) -> ProcessingRecord:
    """Apply cleaning to a column. Methods: fillna, clip, replace."""
    params = params or {}
    df = pd.read_csv(data_path)
    if col not in df.columns:
        raise ValueError(f"Column '{col}' not found")
    if method == "fillna":
        df[col] = df[col].fillna(params.get("value", df[col].median()))
    elif method == "clip":
        df[col] = df[col].clip(lower=params.get("lower"), upper=params.get("upper"))
    elif method == "replace":
        df[col] = df[col].replace(params.get("to_replace"), params.get("value"))
    df.to_csv(data_path, index=False)
    return ProcessingRecord(col=col, operation=method, params=params)


def apply_encoding(data_path: str, col: str, method: str) -> ProcessingRecord:
    """Apply encoding to a column. Methods: onehot, label."""
    df = pd.read_csv(data_path)
    if col not in df.columns:
        raise ValueError(f"Column '{col}' not found")
    if method == "onehot":
        dummies = pd.get_dummies(df[col], prefix=col)
        df = pd.concat([df.drop(columns=[col]), dummies], axis=1)
    elif method == "label":
        df[col] = df[col].astype("category").cat.codes
    df.to_csv(data_path, index=False)
    return ProcessingRecord(col=col, operation=f"encode_{method}")


def split_data(
    data_path: str, test_ratio: float = 0.2, seed: int = 42
) -> dict[str, ArtifactRef]:
    """Split data into train/test. Returns ArtifactRefs."""
    df = pd.read_csv(data_path)
    test = df.sample(frac=test_ratio, random_state=seed)
    train = df.drop(test.index)
    stem = os.path.splitext(data_path)[0]
    train_path = f"{stem}_train.csv"
    test_path = f"{stem}_test.csv"
    train.to_csv(train_path, index=False)
    test.to_csv(test_path, index=False)
    return {"train": f"artifact://{train_path}", "test": f"artifact://{test_path}"}
