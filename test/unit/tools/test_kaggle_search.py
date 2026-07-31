import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDatasetDownloadTool,
    KaggleDiscussionSearchTool,
)


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_mcp():
    """Mock Kaggle MCP 客户端 + parse_competition_info。"""
    mcp = MagicMock()
    mcp.call_tool = AsyncMock()
    with patch(
        "athena.tools.kaggle_search._get_kaggle_mcp_client", return_value=mcp
    ), patch(
        "athena.tools.kaggle_search.parse_competition_info", new_callable=AsyncMock
    ) as mock_parse:
        # 设置 parse_competition_info 返回一个简单的 metadata
        mock_meta = MagicMock()
        mock_meta.task_type = "binary_classification"
        mock_meta.data_type = "tabular"
        mock_meta.target_vars = ["target"]
        mock_meta.primary_metric.name = "accuracy"
        mock_meta.primary_metric.direction = "maximize"
        mock_meta.constraints = {}
        mock_parse.return_value = mock_meta
        yield mcp


@pytest.mark.asyncio
async def test_competition_search_writes_json(mock_mcp, tmp_work_root):
    mock_mcp.call_tool.return_value = "<html>competition page</html>"
    tool = KaggleCompetitionSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, competition_url="https://kaggle.com/c/test")
    assert result.success is True

    # 验证落盘
    info_file = Path(result.data["output_dir"]) / "competition_info.json"
    assert info_file.exists()
    on_disk = json.loads(info_file.read_text(encoding="utf-8"))
    assert on_disk["task_type"] == "binary_classification"


@pytest.mark.asyncio
async def test_discussion_search_writes_json(mock_mcp, tmp_work_root):
    mock_mcp.call_tool.return_value = json.dumps({
        "discussions": [
            {"title": "Top solution", "author": "user1",
             "summary": "Used XGBoost", "score": 0.95, "url": "https://..."}
        ]
    })
    tool = KaggleDiscussionSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(ctx, competition_url="https://kaggle.com/c/test")
    assert result.success is True

    # 验证落盘
    disc_file = Path(result.data["output_dir"]) / "discussions.json"
    assert disc_file.exists()
    on_disk = json.loads(disc_file.read_text(encoding="utf-8"))
    assert on_disk["discussion_count"] == 1


@pytest.mark.asyncio
async def test_dataset_download_writes_to_managed_dir(mock_mcp, tmp_work_root):
    mock_mcp.call_tool.return_value = {"files": ["/tmp/dataset/train.csv"]}
    tool = KaggleDatasetDownloadTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        competition_ref="test-comp",
        output_dir="/unsafe/llm/path",  # LLM 传入的路径应被忽略
    )
    assert result.success is True
    # 验证实际输出目录使用了构造器注入的路径，而非 LLM 传入的
    assert result.data["output_dir"] == str(
        Path(tmp_work_root) / "kaggle_dataset_download"
    )
