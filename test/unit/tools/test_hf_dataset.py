from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.hf_dataset import HFDatasetSearchTool
from athena.tools import hf_dataset as hf_mod


# 模拟 HF API 返回的数据集搜索结果
MOCK_HF_RESULTS = [
    type("DsInfo", (), {
        "id": "user/titanic-similar",
        "description": "Similar passenger data",
        "tags": ["tabular", "classification"],
        "downloads": 500,
        "likes": 20,
        "lastModified": "2025-01-01",
    })()
]


@pytest.fixture(autouse=True)
def _reset_hf_cache():
    """每个测试前重置 HF API 缓存，避免模块级单例跨测试污染。"""
    hf_mod._hf_api = None


@pytest.fixture
def mock_hf_api():
    with patch("athena.tools.hf_dataset.HfApi") as mock:
        api = mock.return_value
        api.list_datasets.return_value = MOCK_HF_RESULTS
        yield mock


@pytest.mark.asyncio
async def test_dataset_search_returns_list(mock_hf_api):
    tool = HFDatasetSearchTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_keywords="titanic passenger survival classification",
        modality="tabular",
        n_results=5,
    )
    assert result.success is True
    assert len(result.data["datasets"]) == 1
    assert result.data["datasets"][0]["id"] == "user/titanic-similar"

    # 验证 modality 被追加到了搜索关键词中
    api = mock_hf_api.return_value
    call_kwargs = api.list_datasets.call_args.kwargs
    assert "tabular" in call_kwargs["search"]


@pytest.mark.asyncio
async def test_dataset_search_empty_results(mock_hf_api):
    mock_hf_api.return_value.list_datasets.return_value = []
    tool = HFDatasetSearchTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_keywords="nonexistent", modality="tabular", n_results=5
    )
    assert result.success is True
    assert len(result.data["datasets"]) == 0
    assert "suggestion" in result.data
