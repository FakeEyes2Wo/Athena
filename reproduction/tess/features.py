"""The frozen 25-feature TESS feature construction used by the XGBoost model."""

from __future__ import annotations

import numpy as np
import pandas as pd

GROUP_COL = "TIC"
SECTOR_COL = "sector"
W_INDEX_COL = "w_index"
ID_COL = "__athena_row_id"
TARGET = "label"

WITHIN_STAR_FEATURES = ["sum_pos_resid", "scatter_ppm"]
CONTROL_FEATURES = [
    "scatter_ppm",
    "max_snr",
    "n_above_1s",
    "n_above_3s",
    "max_run_above_1s",
    "sum_pos_resid",
    "resid_skew",
    "resid_kurt",
    "slope_after_peak",
]
LAG_LEAD_SOURCE = ["max_snr", "n_above_3s", "max_run_above_1s", "resid_skew"]
RAW_FEATURES = tuple(dict.fromkeys(CONTROL_FEATURES))

CAND_FEATURES = tuple(
    [f"ws_{feature}" for feature in WITHIN_STAR_FEATURES]
    + [feature for feature in CONTROL_FEATURES if feature not in WITHIN_STAR_FEATURES]
    + [
        derived
        for feature in LAG_LEAD_SOURCE
        for derived in (f"lag1_{feature}", f"lead1_{feature}")
    ]
    + [
        derived
        for feature in LAG_LEAD_SOURCE
        for derived in (f"lag1_{feature}_missing", f"lead1_{feature}_missing")
    ]
)


def derive_neighbor_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add lag/lead features after the complete partition has been ordered."""
    out = frame.copy()
    grouped = out.groupby([GROUP_COL, SECTOR_COL], sort=False)
    for feature in LAG_LEAD_SOURCE:
        lag = grouped[feature].shift(1)
        lead = grouped[feature].shift(-1)
        out[f"lag1_{feature}"] = lag
        out[f"lead1_{feature}"] = lead
        out[f"lag1_{feature}_missing"] = lag.isna().astype(int)
        out[f"lead1_{feature}_missing"] = lead.isna().astype(int)
    return out


def add_within_star_standardized(frame: pd.DataFrame) -> pd.DataFrame:
    """Replace the two amplitude fields with per-TIC median/IQR values."""
    out = frame.copy()
    for feature in WITHIN_STAR_FEATURES:
        grouped = out.groupby(GROUP_COL, sort=False)[feature]
        median = grouped.transform("median")
        q75 = grouped.transform(lambda values: values.quantile(0.75))
        q25 = grouped.transform(lambda values: values.quantile(0.25))
        iqr = q75 - q25
        standardized = ((out[feature] - median) / iqr).where(iqr > 0, 0.0)
        out[f"ws_{feature}"] = standardized
        out = out.drop(columns=[feature])
    return out


def prepare_features(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Build model columns from one complete CSV and retain its input order."""
    required = {ID_COL, GROUP_COL, SECTOR_COL, W_INDEX_COL, *RAW_FEATURES}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"input is missing required columns: {', '.join(missing)}")
    if frame[ID_COL].isna().any() or frame[ID_COL].duplicated().any():
        raise ValueError(f"{ID_COL} must be non-null and unique")
    ordered = frame.copy()
    ordered["__athena_input_order"] = np.arange(len(ordered), dtype=np.int64)
    ordered = ordered.sort_values(
        [GROUP_COL, SECTOR_COL, W_INDEX_COL], kind="mergesort"
    ).reset_index(drop=True)
    ordered = derive_neighbor_features(ordered)
    ordered = add_within_star_standardized(ordered)
    ids = ordered[ID_COL].copy()
    input_order = ordered["__athena_input_order"].copy()
    return ordered.loc[:, list(CAND_FEATURES)], ids, input_order
