import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.data_prepare import DataAnalyzeTool, DataCleanCodeGenTool


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_llm():
    """Mock LLM returning a fixed EDA report."""
    with patch("athena.tools.data_prepare.single_turn_chat") as mock:
        mock.return_value = json.dumps({
            "distributions": {"PassengerId": "uniform", "Survived": "binary"},
            "missing_values": {"Age": 177},
            "outliers": {"Fare": "right-skewed with extreme values"},
            "correlations": {"Pclass-Survived": -0.34},
            "class_balance": {"Survived": {"0": 549, "1": 342}},
        })
        yield mock


@pytest.mark.asyncio
async def test_data_analyze_returns_eda_report(mock_llm, tmp_work_root):
    tool = DataAnalyzeTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, data_card_refs=["sha256:aaaa"])
    assert result.success is True
    assert "eda_report" in result.data

    # 验证落盘
    report_file = Path(result.data["output_dir"]) / "eda_report.json"
    assert report_file.exists()
    on_disk = json.loads(report_file.read_text(encoding="utf-8"))
    assert on_disk["class_balance"]["Survived"]["0"] == 549


@pytest.mark.asyncio
async def test_data_analyze_empty_refs_errors(tmp_work_root):
    tool = DataAnalyzeTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, data_card_refs=[])
    assert result.success is False


@pytest.fixture
def mock_llm_clean():
    """Mock LLM returning a cleaning script inside a code block."""
    with patch("athena.tools.data_prepare.single_turn_chat") as mock:
        mock.return_value = (
            "```python\n"
            "import pandas as pd\n"
            "df = pd.read_csv(INPUT_PATH)\n"
            "df.fillna(0, inplace=True)\n"
            "df.to_csv(OUTPUT_PATH, index=False)\n"
            "```\n"
        )
        yield mock


@pytest.mark.asyncio
async def test_data_clean_code_gen_writes_script(mock_llm_clean, tmp_work_root):
    tool = DataCleanCodeGenTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        eda_report_ref="sha256:bbbb",
        data_card_refs=["sha256:aaaa"],
    )
    assert result.success is True
    assert result.data["status"] == "script_generated"

    # 验证脚本落盘（不含 markdown 标记）
    script_file = Path(result.data["output_dir"]) / "clean_script.py"
    assert script_file.exists()
    content = script_file.read_text(encoding="utf-8")
    assert "import pandas as pd" in content
    assert "```" not in content


@pytest.mark.asyncio
async def test_data_clean_code_gen_no_code_block_fails(tmp_work_root):
    """LLM 响应无代码块时返回 success=False。"""
    with patch("athena.tools.data_prepare.single_turn_chat") as mock:
        mock.return_value = "Here is some text without any code block."
        tool = DataCleanCodeGenTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-4", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            eda_report_ref="sha256:bbbb",
            data_card_refs=["sha256:aaaa"],
        )
        assert result.success is False
        assert "No code block" in result.error
