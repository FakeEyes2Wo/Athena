"""Explicit train-only exporter for a portable TESS XGBoost bundle."""

from __future__ import annotations

import argparse
import platform
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.impute import SimpleImputer

if __package__ in {None, ""}:  # direct ``python reproduction/tess/export_model.py``
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reproduction.tess.bundle import sha256_file, write_bundle_metadata
from reproduction.tess.features import ID_COL, TARGET, prepare_features

DEFAULT_XGB_PARAMS = {
    "tree_method": "hist",
    "learning_rate": 0.02,
    "n_estimators": 800,
    "max_depth": 6,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "reg_lambda": 1.0,
    "reg_alpha": 0.1,
    "scale_pos_weight": 1.0,
    "random_state": 0,
    "n_jobs": -1,
    "eval_metric": "logloss",
}


def export_model(
    train_csv: Path,
    model_dir: Path,
    *,
    device: str = "cpu",
    n_estimators: int | None = None,
) -> None:
    """Fit only on an explicitly named train.csv and export JSON artifacts."""
    if train_csv.name != "train.csv":
        raise ValueError("export requires an explicitly named train.csv")
    if device not in {"cpu", "cuda"}:
        raise ValueError("device must be cpu or cuda")
    model_dir.mkdir(parents=True, exist_ok=True)
    if any(
        (model_dir / filename).exists()
        for filename in ("model.json", "metadata.json", "checksums.txt")
    ):
        raise FileExistsError(
            f"refusing to overwrite existing model bundle: {model_dir}"
        )
    frame = pd.read_csv(train_csv)
    if TARGET not in frame.columns:
        raise ValueError("train.csv must contain label")
    # Training CSVs need no prediction ID; use a local index for ordering only.
    if ID_COL not in frame.columns:
        frame[ID_COL] = np.arange(len(frame))
    features, _, input_order = prepare_features(frame)
    labels = frame[TARGET].iloc[input_order.to_numpy()].to_numpy()
    imputer = SimpleImputer(strategy="median")
    matrix = imputer.fit_transform(features)
    if not np.isfinite(imputer.statistics_).all():
        raise ValueError("training data contains an entirely missing model feature")
    params = dict(DEFAULT_XGB_PARAMS)
    params["device"] = device
    if n_estimators is not None:
        params["n_estimators"] = n_estimators
    model = xgb.XGBClassifier(**params)
    model.fit(matrix, labels)
    model_path = model_dir / "model.json"
    model.save_model(model_path)
    write_bundle_metadata(
        model_dir,
        model_file="model.json",
        imputer_statistics=imputer.statistics_.tolist(),
        model_params=params,
        device=device,
        provenance={
            "kind": "new_fit_from_explicit_train_csv",
            "train_csv_sha256": sha256_file(train_csv),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": __import__("sklearn").__version__,
            "xgboost": xgb.__version__,
            "fixed_threshold": 0.41,
            "historical_score_guarantee": False,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train_csv", type=Path, required=True)
    parser.add_argument("--model_dir", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    export_model(args.train_csv, args.model_dir, device=args.device)


if __name__ == "__main__":
    main()
