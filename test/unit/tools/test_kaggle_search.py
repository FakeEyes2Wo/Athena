from unittest.mock import AsyncMock, patch

import pytest
from athena.core.schemas import MetricSpec, TaskMetaData
from athena.core.tool_types import ToolContext
from athena.tools.kaggle_search import (
    KaggleCompetitionSearchTool,
    KaggleDatasetDownloadTool,
    KaggleDiscussionSearchTool,
)

# 模拟 Kaggle MCP 返回的比赛页面内容
MOCK_COMPETITION_HTML = """
<h1>Titanic - Machine Learning from Disaster</h1>
<p>Predict survival on the Titanic</p>
<h2>Evaluation</h2><p>Accuracy</p>
"""

MOCK_DISCUSSION_JSON = {
    "discussions": [
        {"title": "Top 3% solution", "author": "user1",
         "summary": "Used XGBoost with feature engineering", "score": 0.82,
         "url": "https://kaggle.com/c/titanic/discussion/1"}
    ]
}

MOCK_DOWNLOAD_RESULT = {
    "files": [
        "/tmp/titanic_data/train.csv",
        "/tmp/titanic_data/test.csv",
    ]
}

MOCK_TASK_METADATA = TaskMetaData(
    task_type="binary_classification",
    data_type="tabular",
    target_vars=["Survived"],
    primary_metric=MetricSpec(name="accuracy", direction="maximize"),
    constraints=[],
)


@pytest.fixture
def mock_mcp_client():
    """模拟 Kaggle MCP 客户端。"""
    with patch("athena.tools.kaggle_search._get_kaggle_mcp_client") as mock:
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=MOCK_COMPETITION_HTML)
        mock.return_value = client
        yield mock


@pytest.fixture
def mock_task_parser():
    """模拟 parse_competition_info，避免 LLM 调用。"""
    with patch("athena.tools.kaggle_search.parse_competition_info") as mock:
        mock.return_value = MOCK_TASK_METADATA
        yield mock


@pytest.mark.asyncio
async def test_competition_search_returns_task_metadata(
    mock_mcp_client, mock_task_parser
):
    tool = KaggleCompetitionSearchTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, competition_url="https://kaggle.com/c/titanic"
    )
    assert result.success is True
    assert "task_type" in str(result.data)


@pytest.mark.asyncio
async def test_discussion_search_returns_summaries(mock_mcp_client):
    mock_mcp_client.return_value.call_tool.return_value = MOCK_DISCUSSION_JSON

    tool = KaggleDiscussionSearchTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, competition_url="https://kaggle.com/c/titanic", top_k=5
    )
    assert result.success is True
    assert "discussions" in str(result.data).lower()


@pytest.mark.asyncio
async def test_dataset_download_returns_file_list(mock_mcp_client):
    mock_mcp_client.return_value.call_tool.return_value = MOCK_DOWNLOAD_RESULT

    tool = KaggleDatasetDownloadTool()
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        competition_ref="https://kaggle.com/c/titanic",
        output_dir="/tmp/titanic_data",
    )
    assert result.success is True
    assert "files" in result.data
    assert result.data["status"] == "downloaded"
