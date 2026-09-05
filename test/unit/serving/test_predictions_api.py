"""``predictions_api`` 单元测试：校验、路由、阈值、缺失值。

用桩 booster，所以**不需要装 lightgbm**——这正是模块把 lightgbm 做成惰性导入
的意义：框架不依赖实验侧的包，测试也就不该依赖。
"""

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from athena.serving.model import (
    PredictionRequest,
    ID_COL,
    MAX_RECORDS,
    EXAMPLE_RECORDS,
    EXAMPLE_TRUTH,
    ModelBundle,
    ValidationError,
    create_features,
)
from athena.serving.predictions_api import build_server

FEATURES = [
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
    "Teff",
    "Rad",
    "snr_per_noise",
    "energy_density",
    "morphology_score",
    "teff_missing_flag",
    "rad_missing_flag",
    "log_max_snr",
    "strong_fraction",
    "consecutiveness_ratio",
]

PREPROC = {
    "feature_columns": FEATURES,
    "teff_median": 5126.0,
    "rad_median": 0.9361,
    "threshold": 0.5,
    "threshold_rule": "maximize F1 on the internal validation split",
    "thresholds": {"tss": {"threshold": 0.0035}, "f1": {"threshold": 0.5}},
    "versions": {"lightgbm": "4.7.0"},
}


class _SnrBooster:
    """把 max_snr 压到 (0,1)：单调、确定，断言可以写死。"""

    def __init__(self) -> None:
        self.seen: list[np.ndarray] = []

    def predict(self, data):
        self.seen.append(data)
        max_snr = np.asarray(data)[:, FEATURES.index("max_snr")].astype(float)
        return 1.0 / (1.0 + np.exp(-(max_snr - 10.0)))


def _bundle() -> ModelBundle:
    return ModelBundle(_SnrBooster(), dict(PREPROC), source="<test>")


def _record(**overrides) -> dict:
    rec = {
        "max_snr": 30.0,
        "n_above_3s": 8,
        "max_run_above_1s": 12,
        "sum_pos_resid": 0.5,
        "equiv_duration_s": 60.0,
        "n_above_1s": 20,
        "resid_skew": 3.0,
        "resid_kurt": 15.0,
        "slope_after_peak": -4.0,
        "scatter_ppm": 2000.0,
        "Tmag": 11.0,
        "Teff": 3400.0,
        "Rad": 0.4,
    }
    rec.update(overrides)
    return rec


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def test_accepts_records_envelope_bare_list_and_single_object() -> None:
    """三种写法都该收——试调的人随手发一条应该拿到预测，不是 400。"""
    bundle = _bundle()
    for payload in (
        {"records": [_record()]},
        [_record()],
        _record(),
    ):
        req = bundle.validate(payload)
        assert len(req.records) == 1
        assert req.threshold == bundle.default_threshold


def test_missing_required_field_names_every_one_of_them() -> None:
    """报错要指名缺了哪些字段，而不是只说"参数不对"。"""
    bundle = _bundle()
    rec = _record()
    del rec["max_snr"]
    del rec["resid_kurt"]
    with pytest.raises(ValidationError) as exc:
        bundle.validate(rec)
    assert "max_snr" in str(exc.value)
    assert "resid_kurt" in str(exc.value)


def test_nullable_fields_may_be_null_but_required_ones_may_not() -> None:
    bundle = _bundle()
    bundle.validate(_record(Teff=None, Rad=None))  # 不抛
    with pytest.raises(ValidationError, match="max_snr"):
        bundle.validate(_record(max_snr=None))


def test_non_numeric_required_field_is_rejected_with_its_type() -> None:
    bundle = _bundle()
    with pytest.raises(ValidationError) as exc:
        bundle.validate(_record(max_snr="high"))
    assert "max_snr" in str(exc.value) and "str" in str(exc.value)


def test_bool_is_not_accepted_as_a_number() -> None:
    """``isinstance(True, int)`` 是真，所以 bool 必须单独挡掉。"""
    with pytest.raises(ValidationError, match="bool"):
        _bundle().validate(_record(n_above_3s=True))


def test_empty_and_oversized_batches_are_rejected() -> None:
    bundle = _bundle()
    with pytest.raises(ValidationError, match="空"):
        bundle.validate({"records": []})
    with pytest.raises(ValidationError) as exc:
        bundle.validate({"records": [_record()] * (MAX_RECORDS + 1)})
    assert str(MAX_RECORDS) in str(exc.value)


def test_threshold_override_is_range_checked() -> None:
    bundle = _bundle()
    req = bundle.validate({"records": [_record()], "threshold": 0.9})
    assert req.threshold == 0.9
    for bad in (1.5, -0.1):
        with pytest.raises(ValidationError, match="0 到 1"):
            bundle.validate({"records": [_record()], "threshold": bad})
    with pytest.raises(ValidationError):
        bundle.validate({"records": [_record()], "threshold": "高"})


# --------------------------------------------------------------------------- #
# 预测
# --------------------------------------------------------------------------- #
def test_absent_nullable_column_is_imputed_not_crashed() -> None:
    """整批都省略 Teff 时，那一列压根不存在——不能 KeyError。"""
    bundle = _bundle()
    rec = _record()
    del rec["Teff"]
    del rec["Rad"]
    out = bundle.predict(PredictionRequest([rec], 0.5))
    assert len(out) == 1
    frame_values = bundle.booster.seen[-1]
    assert frame_values[0][FEATURES.index("Teff")] == PREPROC["teff_median"]
    assert frame_values[0][FEATURES.index("teff_missing_flag")] == 1


def test_present_nullable_value_sets_missing_flag_to_zero() -> None:
    bundle = _bundle()
    bundle.predict(PredictionRequest([_record(Teff=3400.0)], 0.5))
    values = bundle.booster.seen[-1]
    assert values[0][FEATURES.index("teff_missing_flag")] == 0


def test_prediction_flips_with_the_threshold() -> None:
    """同一条记录，只改阈值，判决必须跟着翻——阈值确实被用上了。"""
    bundle = _bundle()
    rec = _record(max_snr=11.0)  # sigmoid(1) ≈ 0.731
    assert bundle.predict(PredictionRequest([rec], 0.5))[0]["prediction"] == 1
    assert bundle.predict(PredictionRequest([rec], 0.9))[0]["prediction"] == 0


def test_id_is_echoed_or_null() -> None:
    bundle = _bundle()
    out = bundle.predict(
        PredictionRequest([_record(**{ID_COL: "TIC1_s0001_w00000"}), _record()], 0.5)
    )
    assert out[0][ID_COL] == "TIC1_s0001_w00000"
    assert out[1][ID_COL] is None


def test_row_order_is_preserved() -> None:
    """响应必须按请求顺序，否则调用方无法对齐。"""
    bundle = _bundle()
    snrs = [30.0, 1.0, 20.0, 2.0]
    out = bundle.predict(PredictionRequest([_record(max_snr=s) for s in snrs], 0.5))
    assert [p["prediction"] for p in out] == [1, 0, 1, 0]


def test_shipped_examples_match_their_declared_truth_shape() -> None:
    """示例记录必须自洽：每条都有 id，且 id 都在真值表里。"""
    bundle = _bundle()
    payload = bundle.example()
    ids = [r[ID_COL] for r in payload["records"]]
    assert set(ids) == set(payload["expected_label"])
    assert set(EXAMPLE_TRUTH) == {r[ID_COL] for r in EXAMPLE_RECORDS}
    bundle.validate(payload)  # /example 的输出必须能原样喂给 /predict


def test_create_features_adds_every_derived_column() -> None:
    import pandas as pd

    derived = create_features(pd.DataFrame([_record()]))
    for name in (
        "snr_per_noise",
        "energy_density",
        "morphology_score",
        "teff_missing_flag",
        "rad_missing_flag",
        "log_max_snr",
        "strong_fraction",
        "consecutiveness_ratio",
    ):
        assert name in derived.columns


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@pytest.fixture
def server():
    srv = build_server(_bundle(), "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(base: str, path: str, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=raw, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_get_routes_all_answer(server) -> None:
    for path in ("/", "/health", "/schema", "/example"):
        status, body = _get(server, path)
        assert status == 200, path
        assert isinstance(body, dict) and body


def test_unknown_route_lists_the_real_ones(server) -> None:
    try:
        _get(server, "/nope")
        raise AssertionError("应当 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
        body = json.loads(exc.read().decode("utf-8"))
        assert "/predict" in " ".join(body["routes"])


def test_example_body_posts_back_unchanged(server) -> None:
    """自述里承诺"原样 POST"，这里就照做一次，保证承诺不落空。"""
    _, example = _get(server, "/example")
    status, body = _post(server, "/predict", example)
    assert status == 200
    assert body["n"] == len(example["records"])
    returned = {p[ID_COL] for p in body["predictions"]}
    assert returned == set(example["expected_label"])


def test_predict_reports_the_threshold_it_used(server) -> None:
    status, body = _post(
        server, "/predict", {"records": [_record()], "threshold": 0.25}
    )
    assert status == 200 and body["threshold"] == 0.25


def test_bad_json_and_empty_body_are_400_not_500(server) -> None:
    status, body = _post(server, "/predict", b"{oops")
    assert status == 400 and "JSON" in body["error"]
    status, body = _post(server, "/predict", b"")
    assert status == 400 and "/example" in body["error"]


def test_validation_failure_is_400_with_the_field_name(server) -> None:
    bad = _record()
    del bad["scatter_ppm"]
    status, body = _post(server, "/predict", bad)
    assert status == 400 and "scatter_ppm" in body["error"]


def test_post_to_wrong_route_is_404(server) -> None:
    status, body = _post(server, "/health", {"records": [_record()]})
    assert status == 404 and "predict" in " ".join(body["routes"])


def test_cors_header_is_present_so_a_browser_can_call_it(server) -> None:
    with urllib.request.urlopen(server + "/health", timeout=10) as resp:
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
