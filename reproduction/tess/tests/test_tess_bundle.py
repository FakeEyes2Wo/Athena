from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from reproduction.tess.export_model import export_model
from reproduction.tess.infer_batch import infer_batch


def _rows() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for tic in (1001, 1002, 1003, 1004):
        for sector in (1, 2, 3):
            for index in range(3):
                rows.append(
                    {
                        "__athena_row_id": f"{tic}-{sector}-{index}",
                        "TIC": tic,
                        "sector": sector,
                        "w_index": index,
                        "sum_pos_resid": float(index + tic % 3),
                        "scatter_ppm": float(index * 2 + sector),
                        "max_snr": float(tic % 5 + index),
                        "n_above_1s": index + 1,
                        "n_above_3s": index,
                        "max_run_above_1s": index + sector,
                        "resid_skew": float(index - sector),
                        "resid_kurt": float(index + sector),
                        "slope_after_peak": float(tic % 7 - index),
                    }
                )
    return pd.DataFrame(rows)


def test_inference_is_label_free_preserves_ids_and_aligns_columns(
    tmp_path: Path,
) -> None:
    train_csv = tmp_path / "train.csv"
    features_csv = tmp_path / "search_features.csv"
    train = _rows()
    train["label"] = (train["sum_pos_resid"] > 1).astype(int)
    # Real TESS train.csv has labels but no prediction ID column.
    train.drop(columns="__athena_row_id").to_csv(train_csv, index=False)
    features = _rows().sample(frac=1, random_state=3)
    features = features[list(reversed(features.columns))]
    features.to_csv(features_csv, index=False)

    model_dir = tmp_path / "weights"
    export_model(train_csv, model_dir, device="cpu", n_estimators=4)
    config = tmp_path / "tess.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "task_id": "TESS_flare_win120min",
                "input_file": "search_features.csv",
                "model_dir": "weights",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "predictions.csv"

    result = infer_batch(tmp_path, config, out)

    assert list(result.columns) == ["__athena_row_id", "label"]
    assert result["__athena_row_id"].tolist() == features["__athena_row_id"].tolist()
    assert len(result) == len(features)
    assert set(result["label"].unique()) <= {0, 1}
    assert "label" not in features.columns


def test_inference_rejects_missing_or_tampered_weights(tmp_path: Path) -> None:
    train = _rows()
    train["label"] = (train["sum_pos_resid"] > 1).astype(int)
    train_csv = tmp_path / "train.csv"
    train.to_csv(train_csv, index=False)
    model_dir = tmp_path / "weights"
    export_model(train_csv, model_dir, device="cpu", n_estimators=2)
    (model_dir / "model.json").write_text("tampered", encoding="utf-8")
    config = tmp_path / "tess.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "task_id": "TESS_flare_win120min",
                "input_file": "train.csv",
                "model_dir": "weights",
                "device": "cpu",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="checksum"):
        infer_batch(tmp_path, config, tmp_path / "out.csv")
