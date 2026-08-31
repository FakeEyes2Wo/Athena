"""``unfence_json`` 单元测试 —— 含 2026-08-31 VALIDATE 崩溃的回归。"""

import json

import pytest

from athena.core.fenced_json import unfence_json


def test_strips_json_tagged_fence() -> None:
    assert unfence_json('```json\n{"accepted": true}\n```') == '{"accepted": true}'


def test_strips_untagged_fence() -> None:
    assert unfence_json('```\n{"a": 1}\n```') == '{"a": 1}'


def test_bare_json_is_returned_unchanged() -> None:
    """没有围栏就不许动，包括前后空白——调用方可能靠原样比对。"""
    for text in ('{"a": 1}', '  {"a": 1}  ', "not json at all"):
        assert unfence_json(text) == text


def test_fence_with_surrounding_whitespace() -> None:
    assert unfence_json('\n\n```json\n{"a": 1}\n```\n\n') == '{"a": 1}'


def test_multiline_body_survives() -> None:
    body = '{\n  "accepted": false,\n  "reason": "changes the model"\n}'
    assert json.loads(unfence_json(f"```json\n{body}\n```"))["accepted"] is False


def test_prose_containing_a_fence_is_left_alone() -> None:
    """正文里碰巧有代码块的回答不该被截断——只在整段以围栏开头时才动手。

    否则一句「答案是 X，示例见 ```...```」会被截成那个代码块，把一个清晰的解析
    错误变成一个安静的错值。
    """
    text = 'The answer is {"a": 1}\n\n```python\nprint(1)\n```'
    assert unfence_json(text) == text


def test_unterminated_fence_falls_through_untouched() -> None:
    """只有开头没有结尾时保持原样，让解析错误照常暴露，而不是猜。"""
    text = '```json\n{"a": 1}'
    assert unfence_json(text) == text


def test_regression_the_payload_that_killed_a_five_hour_run() -> None:
    """2026-08-31 01:58 的真实回答。

    ``review_validation_diff`` 把它直接喂给 ``model_validate_json``，抛
    ``Invalid JSON: expected value at line 1 column 1``，``phase=VALIDATE
    status=FAILED``，整轮搜索结果作废。而内容本身是 accepted=true——
    验证本来是通过的。
    """
    answer = (
        '```json\n'
        '{"accepted": true, "reason": "Only runtime compatibility repairs; '
        'no changes to model, data, features, preprocessing, training, or '
        'final-label access."}\n'
        '```'
    )
    parsed = json.loads(unfence_json(answer))
    assert parsed["accepted"] is True


@pytest.mark.parametrize("payload", ['{"accepted": true}', '{"accepted": false}'])
def test_round_trips_through_json_loads(payload: str) -> None:
    assert json.loads(unfence_json(f"```json\n{payload}\n```")) == json.loads(payload)
