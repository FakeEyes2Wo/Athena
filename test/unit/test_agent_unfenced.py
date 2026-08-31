"""结构化输出的围栏剥离：模型常在 JSON 前面写一段散文。

带工具时不能发 ``response_format``（见 ``docs/agent_structured_output_ch.md``），
模型于是自由地把终态 JSON 包进 ```json 围栏——而且经常先写一句"All files are in
place and verified."再给围栏。``_unfenced`` 原本只在**整段以围栏开头**时才剥，
散文开头就原样返回，于是 ``model_validate_json`` 报 "expected value at line 1
column 1"，白白吃掉结构化重试预算。2026-08-30 真机：evaluator 19 轮回灌里有 10 轮
是这个形态。
"""

import json

from athena.core.agent.runtime import _unfenced

_PAYLOAD = '{"decision": "submit", "reason": "ready", "suggestions": []}'
_FENCE = "```"


def _parses(text: str) -> bool:
    try:
        json.loads(_unfenced(text))
    except ValueError:
        return False
    return True


def test_bare_json_is_untouched() -> None:
    assert json.loads(_unfenced(_PAYLOAD)) == json.loads(_PAYLOAD)


def test_fence_only_output() -> None:
    assert _parses(f"{_FENCE}json\n{_PAYLOAD}\n{_FENCE}")


def test_prose_before_a_fenced_block() -> None:
    """真机上最常见的形态。"""
    assert _parses(
        f"All files are in place and verified.\n\n{_FENCE}json\n{_PAYLOAD}\n{_FENCE}"
    )


def test_prose_before_bare_json() -> None:
    """没有围栏、直接跟在散文后面的 JSON 也要捞出来。"""
    assert _parses(f"The evaluator works in manual mode.\n{_PAYLOAD}")


def test_prose_on_both_sides() -> None:
    assert _parses(
        f"Here is the result.\n\n{_FENCE}json\n{_PAYLOAD}\n{_FENCE}\n\nLet me know."
    )


def test_text_without_any_json_is_returned_unchanged() -> None:
    """没有 JSON 就原样返回，让上层拿到模型的真实措辞去报错。"""
    assert _unfenced("I could not finish the task.") == "I could not finish the task."
