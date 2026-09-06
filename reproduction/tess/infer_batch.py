"""Offline, label-free TESS batch inference."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb
import yaml

if __package__ in {None, ""}:  # direct ``python reproduction/tess/infer_batch.py``
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reproduction.tess.bundle import load_verified_metadata
from reproduction.tess.features import CAND_FEATURES, ID_COL, prepare_features


def _config(config_path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read config: {config_path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("config must be a YAML object")
    for key in ("task_id", "input_file", "model_dir", "device"):
        if not isinstance(loaded.get(key), str) or not loaded[key].strip():
            raise ValueError(f"config requires non-empty {key}")
    if loaded["task_id"] != "TESS_flare_win120min":
        raise ValueError("config task_id is not TESS_flare_win120min")
    if loaded["device"] not in {"cpu", "cuda"}:
        raise ValueError("config device must be cpu or cuda")
    return loaded


def _inside(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside its root") from exc
    return candidate


def infer_batch(data_root: Path, config_path: Path, out_path: Path) -> pd.DataFrame:
    """Run one complete input partition through a verified local model bundle."""
    config = _config(config_path)
    root = data_root.resolve()
    input_path = _inside(root, config["input_file"], "input_file")
    model_dir = _inside(config_path.parent, config["model_dir"], "model_dir")
    metadata = load_verified_metadata(model_dir)
    try:
        frame = pd.read_csv(input_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read input CSV: {input_path}") from exc
    model = xgb.XGBClassifier(device=config["device"])
    model.load_model(model_dir / metadata["model_file"])
    model.set_params(device=config["device"])
    features, ids, input_order = prepare_features(frame)
    matrix = features.to_numpy(dtype=float, copy=True)
    statistics = np.asarray(metadata["imputer_statistics"], dtype=float)
    if not np.isfinite(statistics).all():
        raise ValueError("model bundle has non-finite imputer statistics")
    matrix = np.where(np.isnan(matrix), statistics, matrix)
    probabilities = model.predict_proba(matrix)[:, 1]
    output = pd.DataFrame(
        {
            "__athena_input_order": input_order.to_numpy(),
            ID_COL: ids.to_numpy(),
            "label": (probabilities >= 0.41).astype(int),
        }
    ).sort_values("__athena_input_order", kind="mergesort")
    output = output.drop(columns="__athena_input_order")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(out_path, index=False)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data_root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    infer_batch(args.data_root, args.config, args.out)


if __name__ == "__main__":
    main()
