"""把训练好的模型开成一个可调用的 HTTP 推理端点。

    python -m athena.serving.predictions_api --weights <dir>
    python -m athena.serving.predictions_api --weights <dir> --port 8080

只做推理。不执行 shell、不落盘、请求间无状态，因此可以长期开着给人试调，
而不会像 ``gui_gateway`` 那样把研究运行时（会执行模型现写的任意命令）暴露出去。

``--weights`` 目录需要两个文件：

* ``model.txt``     —— LightGBM booster
* ``preproc.json``  —— ``feature_columns``、缺失值填充中位数、判决阈值

``preproc.json`` 可以选填 ``required_fields`` / ``nullable_fields`` 覆盖默认的
输入字段表；不填则用下面的 TESS 耀发窗口默认值。

端点：

    GET  /          自述，含可直接复制运行的 curl
    GET  /health    存活 + 加载了哪个模型
    GET  /schema    接受的字段、类型、阈值
    GET  /example   可原样 POST 的请求体
    POST /predict   打分，单次上限 MAX_RECORDS 条

lightgbm 是**惰性导入**的：它不是框架依赖（见 pyproject 的说明——实验侧依赖由
Agent 自己装进实验环境），所以本模块在没装 lightgbm 时也能导入和被测试。
"""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

ID_COL = "image_filename"
MAX_RECORDS = 10_000

# TESS 耀发窗口任务的默认输入字段。这些列在窗口表里从不缺失，所以缺一个就报错
# 而不是悄悄填 0——0 在这里是一个物理上不同的窗口，不是"没测到"。
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
# 这两列确实有一部分恒星没有（FITS 头里就没写）。传 null 或整个省略都是正确表达：
# 模型会看到中位数加一个缺失标志，和训练时一致。
DEFAULT_NULLABLE_FIELDS = ("Teff", "Rad")


class Booster(Protocol):
    """只要能 ``predict(ndarray) -> ndarray``，就能被这个端点服务。

    写成 Protocol 而不是直接依赖 ``lgb.Booster``，测试才能不装 lightgbm。
    """

    def predict(self, data: Any) -> Any: ...


def create_features(df: pd.DataFrame) -> pd.DataFrame:
    """派生特征。**必须与训练脚本里的同名函数逐字一致。**

    这里是刻意复制而不是从训练脚本 import 的：推理要在训练脚本和它的数据都不在
    的机器上照常工作。任何一边改了，另一边必须跟着改。
    """
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


class ValidationError(ValueError):
    """一个 400：请求读懂了，但它是错的，且错误信息要指名是哪个字段。"""


class ModelBundle:
    """一个 booster 加上它训练时用的那套预处理常量。"""

    def __init__(self, booster: Booster, preproc: dict, source: str = "<memory>") -> None:
        self.booster = booster
        self.preproc = preproc
        self.source = source
        self.default_threshold = float(preproc["threshold"])
        self.required_fields = tuple(
            preproc.get("required_fields") or DEFAULT_REQUIRED_FIELDS
        )
        self.nullable_fields = tuple(
            preproc.get("nullable_fields") or DEFAULT_NULLABLE_FIELDS
        )

    @classmethod
    def load(cls, weights_dir: Path) -> ModelBundle:
        """从磁盘装载。lightgbm 在这里才导入。"""
        preproc_path = weights_dir / "preproc.json"
        model_path = weights_dir / "model.txt"
        for path in (preproc_path, model_path):
            if not path.is_file():
                raise SystemExit(f"{path}: 不存在；用 --weights <dir> 指向权重目录")
        try:
            import lightgbm as lgb
        except ImportError as exc:  # 框架不依赖它，装的时候才需要
            raise SystemExit(
                "需要 lightgbm 才能装载模型：pip install lightgbm （或 uv sync --extra serve）"
            ) from exc
        preproc = json.loads(preproc_path.read_text(encoding="utf-8"))
        return cls(lgb.Booster(model_file=str(model_path)), preproc, str(weights_dir))

    def validate(self, payload: object) -> tuple[list[dict], float | None]:
        """返回 (records, 阈值覆盖) 或抛 ValidationError。

        ``{"records": [...]}``、裸列表、单个对象都接受——试调的人随手发一个记录
        应该拿到预测，而不是 400。
        """
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
        return records, threshold

    def predict(self, records: list[dict], threshold: float) -> list[dict]:
        frame = pd.DataFrame(records)
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
            for rec in records
        ]
        return [
            {
                ID_COL: ids[i],
                "probability": round(float(probs[i]), 6),
                "prediction": int(probs[i] >= threshold),
            }
            for i in range(len(records))
        ]

    # ---------- 自述 ----------

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
        """两条真实窗口——一条有耀发一条没有——外加真值，便于对答案。

        允许 ``preproc.json`` 用 ``example_records`` / ``example_truth`` 覆盖，
        换任务时不必改代码。
        """
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


# 取自公开窗口表的两条真实记录，不是编的数。都拿冻结模型核对过：两条都判对，
# 所以试调的人 POST 之后可以直接和下面的真值比。
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


class Handler(BaseHTTPRequestHandler):
    bundle: ModelBundle
    server_version = "athena-predictions-api/1.0"
    sys_version = ""

    # ---------- 管道 ----------

    def _send(self, status: HTTPStatus, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        # 只读、无状态、不带凭据，所以可以放心从浏览器里调。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(HTTPStatus.NO_CONTENT, {})

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        bundle = self.bundle
        if route == "/":
            self._send(HTTPStatus.OK, bundle.describe(self.headers.get("Host", "127.0.0.1:8000")))
        elif route == "/health":
            self._send(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "model": "LightGBM booster, frozen",
                    "weights": bundle.source,
                    "n_features": len(bundle.preproc["feature_columns"]),
                    "trained_with": bundle.preproc.get("versions", {}),
                },
            )
        elif route == "/schema":
            self._send(HTTPStatus.OK, bundle.schema())
        elif route == "/example":
            self._send(HTTPStatus.OK, bundle.example())
        else:
            self._send(
                HTTPStatus.NOT_FOUND,
                {
                    "error": f"没有这个路由 {route}",
                    "routes": ["/", "/health", "/schema", "/example", "POST /predict"],
                },
            )

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route != "/predict":
            self._send(
                HTTPStatus.NOT_FOUND,
                {"error": f"没有这个路由 POST {route}", "routes": ["POST /predict"]},
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": "body 是空的；GET /example 可以拿到一个能直接 POST 的 body"},
            )
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": f"body 不是合法 JSON：{exc}"})
            return

        try:
            records, override = self.bundle.validate(payload)
        except ValidationError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        threshold = self.bundle.default_threshold if override is None else override
        try:
            predictions = self.bundle.predict(records, threshold)
        except Exception as exc:  # 校验没拦住的畸形记录
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"打分失败：{type(exc).__name__}: {exc}"},
            )
            return

        self._send(
            HTTPStatus.OK,
            {
                "n": len(predictions),
                "threshold": threshold,
                "threshold_rule": self.bundle.preproc.get("threshold_rule", ""),
                "predictions": predictions,
            },
        )


def build_server(bundle: ModelBundle, host: str, port: int) -> ThreadingHTTPServer:
    """装配好 server 但不 serve——测试可以拿它的真实端口自己发请求。"""
    handler = type("BoundHandler", (Handler,), {"bundle": bundle})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True, help="含 model.txt 与 preproc.json 的目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    server = build_server(ModelBundle.load(args.weights), args.host, args.port)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"listening on http://{shown}:{args.port}  (试试 GET / )", file=sys.stderr)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"注意：绑在 {args.host}，别的主机能连上来。这个端点只读、无状态，"
            "但没有限流——面向公网时请放在反向代理后面。",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
