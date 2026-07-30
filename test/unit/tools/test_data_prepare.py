import json
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool


@pytest.fixture
def mock_llm():
    """Mock LLM returning a fixed EDA report."""
    with patch(
        "athena.tools.data_prepare.single_turn_chat"
    ) as mock:
        mock.return_value = json.dumps({
            "distributions": {"PassengerId": "uniform", "Survived": "binary"},
            "missing_values": {"Age": 177},
            "outliers": {"Fare": "right-skewed with extreme values"},
            "correlations": {"Pclass-Survived": -0.34},
            "class_balance": {"Survived": {"0": 549, "1": 342}},
        })
        yield mock


@pytest.mark.asyncio
async def test_data_analyze_returns_eda_report(mock_llm):
    tool = DataAnalyzeTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        data_card_refs=["sha256:aaaa"],
    )
    assert result.success is True
    assert "eda_report" in result.data


@pytest.mark.asyncio
async def test_data_analyze_empty_refs_errors():
    tool = DataAnalyzeTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, data_card_refs=[])
    assert result.success is False


@pytest.fixture
def mock_llm_clean():
    """Mock LLM returning a fixed cleaning script."""
    with patch(
        "athena.tools.data_prepare.single_turn_chat"
    ) as mock:
        mock.return_value = (
            "import pandas as pd\n"
            "df = pd.read_csv(INPUT_PATH)\n"
            "df.fillna(0, inplace=True)\n"
            "df.to_csv(OUTPUT_PATH, index=False)\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_data_clean_code_gen_returns_script(mock_llm_clean):
    tool = DataCleanCodeGenTool()
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        eda_report_ref="sha256:bbbb",
        data_card_refs=["sha256:aaaa"],
    )
    assert result.success is True
    assert "clean_script" in result.data
    assert result.data["status"] == "script_generated"
