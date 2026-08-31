"""PREPARE evaluator 修复循环的三处失明。

2026-08-30 真机：evaluator 与 final_evaluator 一共提交 19 轮，全部被打回，最后以
``evaluator turn budget exhausted without an accepted evaluator`` 结束整个 PREPARE。
逐轮读回灌内容后发现 18 轮是被框架自己浪费的：

- 10 轮：模型把终态 JSON 写成"散文 + ```json 围栏"，``_unfenced`` 只认整段以围栏
  开头的形态，于是原样返回、解析失败（见 test_agent_unfenced.py）。
- 8 轮：评估器进程失败的反馈被头部截断在 1000 字符，而 ``uv`` 的告警与评估器自己的
  DEBUG 行把预算吃光，真正的 Traceback 一个字都没进去——agent 在盲修。
- 贯穿全程：``extract_prediction_column_from_source`` 把 Python **变量名** ``ID_COL``
  当成列名（正则的引号是可选的），而它的值恰恰是行 id 列，平台于是按一个不存在的
  列名造探针预测。
"""

import json

from athena.research.evaluator_trust import extract_prediction_column_from_source
from athena.research.supervisor.prepare import (
    _feedback_text,
    declared_prediction_column,
)


def test_source_column_ignores_unquoted_identifiers() -> None:
    """``row[ID_COL]`` 里的 ID_COL 是变量，不是列名。"""
    source = "\n".join(
        [
            'ID_COL = "__athena_row_id"',
            "def load(row):",
            "    rid = int(row[ID_COL])",
            '    return row["Survived"]',
        ]
    )

    assert extract_prediction_column_from_source(source) == "Survived"


def test_source_column_still_reads_quoted_literals() -> None:
    """带引号的字面量仍要认出来（原有能力不能退化）。"""
    assert extract_prediction_column_from_source('v = df["pred_label"]') == "pred_label"
    assert extract_prediction_column_from_source("v = row['Survived']") == "Survived"


def test_source_column_returns_none_without_any_literal() -> None:
    """全是变量索引时宁可返回 None，交给调用方拒绝，也不要瞎猜。"""
    source = 'COL = "x"\nv = row[COL]\nw = df[OTHER]'

    assert extract_prediction_column_from_source(source) is None


def test_feedback_keeps_the_tail_where_the_traceback_lives() -> None:
    """反馈超预算时保留尾部：Traceback 在最后，头部是 uv 告警与 DEBUG 噪声。"""
    noise = "warning: VIRTUAL_ENV does not match the project environment. " * 40
    exc = RuntimeError(
        noise + "Traceback (most recent call last): KeyError: 'Survived'"
    )

    text = _feedback_text(exc)

    assert len(text) <= 1000
    assert "KeyError: 'Survived'" in text
    assert "Traceback" in text


def test_short_feedback_is_passed_through_whole() -> None:
    """没超预算的反馈原样传，不该被截断标记污染。"""
    text = _feedback_text(ValueError("labels.csv must carry __athena_row_id"))

    assert text == "labels.csv must carry __athena_row_id"
    assert "TRUNCATED" not in text


def test_metric_json_declares_the_prediction_column(tmp_path) -> None:
    """预测列名是结构化事实，应当从 metric.json 读，而不是刮散文。

    真机：agent 在 HANDOFF.md 里用最清楚的英语写了 "The prediction value column is
    named `prediction`"，正则只认冒号语法于是返回 None，平台连续 10 轮回灌"请声明"，
    却从不说要用什么语法——contract 两端都无法满足，PREPARE 以 turn budget exhausted
    收场。metric.json 本就是被结构化解析的契约载体（已含 eval_script /
    prediction_format），列名归它。
    """
    (tmp_path / "metric.json").write_text(
        json.dumps(
            {
                "eval_script": "evaluate.py",
                "prediction_format": "tabular_csv",
                "prediction_column": "prediction",
            }
        ),
        encoding="utf-8",
    )

    assert declared_prediction_column(tmp_path) == "prediction"


def test_metric_json_without_the_field_returns_none(tmp_path) -> None:
    """没声明就返回 None，交给散文兜底，不猜。"""
    (tmp_path / "metric.json").write_text(
        json.dumps({"eval_script": "evaluate.py"}), encoding="utf-8"
    )

    assert declared_prediction_column(tmp_path) is None


def test_missing_metric_json_returns_none(tmp_path) -> None:
    """metric.json 缺失由 _evaluator_layout 负责报错，这里不抛。"""
    assert declared_prediction_column(tmp_path) is None


def test_handoff_accepts_natural_phrasing() -> None:
    """散文兜底要认 agent 真实写出来的说法，不只认冒号语法。"""
    from athena.research.evaluator_trust import extract_prediction_column

    assert (
        extract_prediction_column(
            "The **prediction value column is named `prediction`** (exactly, lowercase)."
        )
        == "prediction"
    )
    assert (
        extract_prediction_column("The prediction column is `Survived`.") == "Survived"
    )
