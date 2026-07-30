from unittest.mock import AsyncMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools.hf_model import HFModelSearchTool
from athena.tools import hf_model as hf_mod


# 模拟 HF API 返回的模型搜索结果
MOCK_MODEL_RESULTS = [
    type("ModelInfo", (), {
        "modelId": "google/vit-base-patch16-224",
        "pipeline_tag": "image-classification",
        "tags": ["pytorch", "vision"],
        "downloads": 100000,
        "likes": 500,
        "lastModified": "2025-06-01",
    })()
]


@pytest.fixture(autouse=True)
def _reset_hf_cache():
    """每个测试前重置 HF API 缓存，避免模块级单例跨测试污染。"""
    hf_mod._hf_api = None


@pytest.fixture
def mock_hf_api():
    with patch("athena.tools.hf_model.HfApi") as mock:
        api = mock.return_value
        api.list_models.return_value = MOCK_MODEL_RESULTS
        yield mock


@pytest.mark.asyncio
async def test_model_search_returns_list(mock_hf_api):
    tool = HFModelSearchTool()
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_type="image_classification",
        modality="image",
        architecture_hint="vit transformer",
        n_results=5,
    )
    assert result.success is True
    assert len(result.data["models"]) == 1
    assert result.data["models"][0]["id"] == "google/vit-base-patch16-224"


@pytest.mark.asyncio
async def test_model_search_empty_is_ok(mock_hf_api):
    mock_hf_api.return_value.list_models.return_value = []
    tool = HFModelSearchTool()
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_type="nonexistent", modality="tabular",
        architecture_hint="", n_results=5
    )
    assert result.success is True
    assert len(result.data["models"]) == 0
    assert "suggestion" in result.data
