"""Frozen model + preprocessing constants for the serving prediction API.

Kept free of HTTP concerns so it can be tested and reused without starting a
server. ``lightgbm`` is imported lazily: the framework does not depend on it,
and this module must import and test without it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

ID_COL = "image_filename"
MAX_RECORDS = 10_000

DEFAULT_REQUIRED_FIELDS = (
    "max_snr",
    "n_above_3s",
    "max_run_above_1s",
    "sum_pos_resid",
    "equiv_duration_s",
    "n_above_1s",
    "resid_skew",
    "resid_kurt",
    "slope_after_peak",
    "scatter_ppm",
    "Tmag",
)
DEFAULT_NULLABLE_FIELDS = ("Teff", "Rad")


class Booster(Protocol):
    """Anything with ``predict(ndarray) -> ndarray`` can be served."""

    def predict(self, data: Any) -> Any: ...


class ValidationError(ValueError):
    """A 400: the request was understood but is invalid."""


@dataclass(frozen=True, slots=True)
class PredictionRequest:
    """Validated record list plus the effective decision threshold."""

    records: list[dict]
    threshold: float


def create_features(df: pd.DataFrame) -> pd.DataFrame:
    """Derived features. Must stay byte-for-byte aligned with the training script."""
    df = df.copy()
    df["snr_per_noise"] = df["max_snr"] / (df["scatter_ppm"] + 1e-6)
    df["energy_density"] = df["sum_pos_resid"] / (df["n_above_1s"] + 1e-6)
    df["morphology_score"] = df["resid_skew"] * df["resid_kurt"].abs()
    df["teff_missing_flag"] = df["Teff"].isna().astype(int)
    df["rad_missing_flag"] = df["Rad"].isna().astype(int)
    df["log_max_snr"] = np.log1p(df["max_snr"])
    df["strong_fraction"] = df["n_above_3s"] / (df["n_above_1s"] + 1e-6)
    df["consecutiveness_ratio"] = df["max_run_above_1s"] / (df["n_above_1s"] + 1e-6)
    return df


class ModelBundle:
    """A booster plus the preprocessing constants it was trained with."""

    def __init__(self, booster: Booster, preproc: dict, source: str = "<memory>") -> None:
        self.booster = booster
        self.preproc = preproc
        self.source = source

    @property
    def default_threshold(self) -> float:
        return float(self.preproc["threshold"])

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(self.preproc.get("required_fields") or DEFAULT_REQUIRED_FIELDS)

    @property
    def nullable_fields(self) -> tuple[str, ...]:
        return tuple(self.preproc.get("nullable_fields") or DEFAULT_NULLABLE_FIELDS)

    @classmethod
    def load(cls, weights_dir: Path) -> "ModelBundle":
        preproc_path = weights_dir / "preproc.json"
        model_path = weights_dir / "model.txt"
        for path in (preproc_path, model_path):
            if not path.is_file():
                raise SystemExit(f"{path}: 不存在；用 --weights <dir> 指向权重目录")
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise SystemExit(
                "需要 lightgbm 才能装载模型：pip install lightgbm （或 uv sync --extra serve）"
            ) from exc
        preproc = json.loads(preproc_path.read_text(encoding="utf-8"))
        return cls(lgb.Booster(model_file=str(model_path)), preproc, str(weights_dir))

    def validate(self, payload: object) -> PredictionRequest:
        threshold: float | None = None
        if isinstance(payload, dict) and "records" in payload:
            records = payload["records"]
            raw = payload.get("threshold")
            if raw is not None:
                try:
                    threshold = float(raw)
                except (TypeError, ValueError):
                    raise ValidationError("threshold 必须是 0 到 1 之间的数")
                if not 0.0 <= threshold <= 1.0:
                    raise ValidationError("threshold 必须在 0 到 1 之间")
        elif isinstance(payload, dict):
            records = [payload]
        else:
            records = payload

        if not isinstance(records, list):
            raise ValidationError(
                '期望 {"records": [ {...}, ... ]}、一个记录列表，或单个记录对象'
            )
        if not records:
            raise ValidationError("records 是空的，至少要发一条")
        if len(records) > MAX_RECORDS:
            raise ValidationError(
                f"{len(records)} 条超出单次 {MAX_RECORDS} 条的上限；拆批发送，"
                "整份文件请走批量推理入口"
            )

        for index, rec in enumerate(records):
            if not isinstance(rec, dict):
                raise ValidationError(f"records[{index}] 不是一个对象")
            missing = [f for f in self.required_fields if rec.get(f) is None]
            if missing:
                raise ValidationError(
                    f"records[{index}] 缺必填字段：{', '.join(missing)}。"
                    f"只有 {' 和 '.join(self.nullable_fields)} 可以为 null。"
                )
            for field in self.required_fields:
                value = rec[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValidationError(
                        f"records[{index}].{field} 必须是数字，收到 {type(value).__name__}"
                    )
        return PredictionRequest(
            records=records,
            threshold=self.default_threshold if threshold is None else threshold,
        )

    def predict(self, request: PredictionRequest) -> list[dict]:
        frame = pd.DataFrame(request.records)
        for field in (*self.nullable_fields, *self.required_fields):
            if field not in frame.columns:
                frame[field] = np.nan
            frame[field] = pd.to_numeric(frame[field], errors="coerce")

        frame = create_features(frame)
        frame["Teff"] = frame["Teff"].fillna(self.preproc["teff_median"])
        frame["Rad"] = frame["Rad"].fillna(self.preproc["rad_median"])

        probs = np.asarray(
            self.booster.predict(frame[self.preproc["feature_columns"]].values)
        )
        ids = [
            str(rec[ID_COL]) if isinstance(rec.get(ID_COL), (str, int)) else None
            for rec in request.records
        ]
        return [
            {
                ID_COL: ids[i],
                "probability": round(float(probs[i]), 6),
                "prediction": int(probs[i] >= request.threshold),
            }
            for i in range(len(request.records))
        ]

    def describe(self, host: str) -> dict:
        return {
            "name": "TESS 恒星耀发检测 · 可调用测试 API",
            "task_id": "TESS_flare_win120min",
            "what_it_does": (
                "对 120 分钟 TESS 光变曲线窗口打分，判断窗口内是否有恒星耀发。"
                "只读推理，模型已冻结。"
            ),
            "positive_class": "1 = 窗口内有耀发峰值",
            "routes": {
                "GET /": "本说明",
                "GET /health": "存活与已加载的模型",
                "GET /schema": "接受的字段与判决阈值",
                "GET /example": "可原样 POST 的请求体",
                "POST /predict": f"给 1..{MAX_RECORDS} 条窗口记录打分",
            },
            "try_it": [
                f"curl http://{host}/schema",
                f"curl http://{host}/example",
                f"curl http://{host}/example | curl -X POST http://{host}/predict "
                "-H 'Content-Type: application/json' --data-binary @-",
            ],
        }

    def schema(self) -> dict:
        thresholds = self.preproc.get("thresholds", {})
        return {
            "request": {
                "records": f"1..{MAX_RECORDS} 条窗口对象",
                "threshold": "可选，[0,1] 的浮点数，覆盖默认阈值",
            },
            "fields": {
                "required": {name: "number" for name in self.required_fields},
                "nullable": {
                    name: "number 或 null —— 部分恒星的 FITS 头里就没有这个值，"
                    "传 null 是正确的，模型能处理"
                    for name in self.nullable_fields
                },
                "optional": {ID_COL: "string，原样回显，便于对齐"},
            },
            "response": {
                ID_COL: "你传的 id，或 null",
                "probability": "[0,1] 的浮点数",
                "prediction": "0 或 1，按下面的阈值判定",
            },
            "threshold": {
                "default": self.default_threshold,
                "rule": self.preproc.get("threshold_rule", ""),
                "alternatives": {
                    name: entry.get("threshold") for name, entry in thresholds.items()
                },
                "note": (
                    "正例率约 0.85%。按 TSS 调出来的阈值虚警远多于按 F1 调的，"
                    "选哪个要有意识。"
                ),
            },
            "feature_columns_used_by_model": self.preproc["feature_columns"],
        }

    def example(self) -> dict:
        records = self.preproc.get("example_records") or EXAMPLE_RECORDS
        truth = self.preproc.get("example_truth") or EXAMPLE_TRUTH
        return {
            "note": (
                "把这个 body 原样 POST 给 /predict。两条真实窗口：第一条含目录耀发，"
                "第二条没有。把返回的 prediction 和 expected_label 对一下。"
            ),
            "expected_label": truth,
            "records": records,
        }


EXAMPLE_RECORDS = [
    {
        ID_COL: "TIC207082763_s0001_w00299",
        "max_snr": 56.8091,
        "n_above_3s": 12,
        "max_run_above_1s": 18,
        "sum_pos_resid": 0.597448,
        "equiv_duration_s": 71.6938,
        "n_above_1s": 28,
        "resid_skew": 4.0873,
        "resid_kurt": 20.7263,
        "slope_after_peak": -7.8292,
        "scatter_ppm": 2599.84,
        "Tmag": 10.8088,
        "Teff": 3387.0,
        "Rad": 0.4762,
    },
    {
        ID_COL: "TIC5725904_s0002_w00209",
        "max_snr": 2.914,
        "n_above_3s": 0,
        "max_run_above_1s": 2,
        "sum_pos_resid": 0.376546,
        "equiv_duration_s": 45.1855,
        "n_above_1s": 11,
        "resid_skew": 0.2016,
        "resid_kurt": 4.1444,
        "slope_after_peak": 0.899,
        "scatter_ppm": 12729.16,
        "Tmag": 13.8295,
        "Teff": 3124.0,
        "Rad": 0.2343,
    },
]
EXAMPLE_TRUTH = {
    "TIC207082763_s0001_w00299": 1,
    "TIC5725904_s0002_w00209": 0,
}


__all__ = [
    "ID_COL",
    "MAX_RECORDS",
    "DEFAULT_REQUIRED_FIELDS",
    "DEFAULT_NULLABLE_FIELDS",
    "EXAMPLE_RECORDS",
    "EXAMPLE_TRUTH",
    "Booster",
    "ValidationError",
    "PredictionRequest",
    "create_features",
    "ModelBundle",
]
