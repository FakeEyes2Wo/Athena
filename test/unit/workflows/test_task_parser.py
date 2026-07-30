"""测试 task_parser.parse_competition_info 的解析逻辑。

注意：端到端测试（从 Kaggle 官网抓取到解析）需要等数据抓取工具实现后补充。
当前仅测试解析函数本身的输入-输出契约。
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from athena.workflows.prepare.task_parser import parse_competition_info

# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------
MOCK_KAGGLE_TEXT = """
# Titanic - Machine Learning from Disaster
Predict survival on the Titanic using passenger data.

## Evaluation
Submissions are evaluated on accuracy.

## Data
- train.csv: 891 rows, 12 columns including Survived (target)
- test.csv: 418 rows, 11 columns (no Survived)
"""

MOCK_KAGGLE_LLM_RESPONSE = json.dumps({
    "task_type": "binary_classification",
    "data_type": "tabular",
    "target_vars": ["Survived"],
    "primary_metric_name": "accuracy",
    "primary_metric_direction": "maximize",
    "constraints": [],
})

MOCK_UNKNOWN_LLM_RESPONSE = json.dumps({
    "task_type": "other",
    "data_type": "other",
    "target_vars": [],
    "primary_metric_name": "unknown",
    "primary_metric_direction": "maximize",
    "constraints": [],
})


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@patch("athena.workflows.prepare.task_parser.single_turn_chat", new_callable=AsyncMock)
async def test_parse_basic_competition_info(mock_chat):
    """验证标准 Kaggle 竞赛描述可以正确解析为结构化元数据。"""
    mock_chat.return_value = MOCK_KAGGLE_LLM_RESPONSE

    result = await parse_competition_info(
        MOCK_KAGGLE_TEXT, "https://kaggle.com/c/titanic"
    )

    assert result.task_type == "binary_classification"
    assert result.data_type == "tabular"
    assert result.target_vars == ["Survived"]
    assert result.primary_metric.name == "accuracy"
    assert result.primary_metric.direction == "maximize"


@pytest.mark.asyncio
@patch("athena.workflows.prepare.task_parser.single_turn_chat", new_callable=AsyncMock)
async def test_parse_handles_unknown_fields(mock_chat):
    """验证信息不足时仍能返回合法的结构化结果。"""
    mock_chat.return_value = MOCK_UNKNOWN_LLM_RESPONSE

    result = await parse_competition_info(
        "Some competition with no details.", "https://example.com"
    )

    assert isinstance(result.task_type, str) and len(result.task_type) > 0
    assert isinstance(result.primary_metric.name, str)


@pytest.mark.asyncio
@patch("athena.workflows.prepare.task_parser.single_turn_chat", new_callable=AsyncMock)
async def test_parse_handles_invalid_json(mock_chat):
    """验证 LLM 返回非 JSON 时回退到默认值，不会抛出异常。"""
    mock_chat.return_value = "not a valid json {{{"

    result = await parse_competition_info(
        "Some text", "https://example.com"
    )

    assert result.task_type == "other"
    assert result.data_type == "other"
    assert result.target_vars == []
    assert result.primary_metric.name == "unknown"
    assert result.primary_metric.direction == "maximize"
