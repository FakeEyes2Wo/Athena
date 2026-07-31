import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools import hf_model as hf_mod
from athena.tools.hf_model import HFModelDownloadTool, HFModelSearchTool


@pytest.fixture(autouse=True)
def _reset_hf_cache():
    """每个测试前重置 HF API 缓存，避免模块级单例跨测试污染。"""
    hf_mod._hf_api = None


@pytest.fixture
def tmp_work_root():
    """创建临时 work 目录，测试后清理。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def mock_hf_api():
    """Mock _get_hf_api 返回固定模型列表。"""
    with patch("athena.tools.hf_model._get_hf_api") as mock_get:
        api = MagicMock()
        m = MagicMock()
        m.modelId = "google/vit-base"
        m.pipeline_tag = "image-classification"
        m.tags = ["pytorch"]
        m.downloads = 50000
        m.likes = 200
        api.list_models.return_value = [m]
        mock_get.return_value = api
        yield api


# ── HFModelSearchTool ──


@pytest.mark.asyncio
async def test_model_search_writes_json(mock_hf_api, tmp_work_root):
    tool = HFModelSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_type="image_classification",
        modality="image",
        architecture_hint="vit",
    )
    assert result.success is True
    assert result.data["count"] == 1
    assert result.data["models"][0]["id"] == "google/vit-base"

    # 验证落盘
    results_file = Path(result.data["output_dir"]) / "search_results.json"
    assert results_file.exists()
    on_disk = json.loads(results_file.read_text(encoding="utf-8"))
    assert on_disk["models"][0]["id"] == "google/vit-base"


@pytest.mark.asyncio
async def test_model_search_empty_writes_json(mock_hf_api, tmp_work_root):
    """空结果时返回 suggestion，且空结果同样落盘。"""
    mock_hf_api.list_models.return_value = []
    tool = HFModelSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_type="nonexistent",
        modality="tabular",
        architecture_hint="",
        n_results=5,
    )
    assert result.success is True
    assert len(result.data["models"]) == 0
    assert "suggestion" in result.data

    # 验证落盘（空结果也写盘）
    results_file = Path(result.data["output_dir"]) / "search_results.json"
    assert results_file.exists()
    on_disk = json.loads(results_file.read_text(encoding="utf-8"))
    assert on_disk["count"] == 0


# ── HFModelDownloadTool ──


@pytest.mark.asyncio
async def test_model_download_uses_managed_dir(tmp_work_root):
    """下载工具应使用构造器注入的目录，而非 LLM 传入的路径。"""
    with patch("athena.tools.hf_model.snapshot_download") as mock_snap:
        mock_snap.return_value = str(Path(tmp_work_root) / "hf_model_download")
        tool = HFModelDownloadTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            hf_model_id="google/vit-base",
            output_dir="/unsafe/llm/path",  # 应被忽略
        )
        assert result.success is True
        assert result.data["output_dir"] == str(
            Path(tmp_work_root) / "hf_model_download"
        )
        # 验证 snapshot_download 使用了安全的路径
        mock_snap.assert_called_once()
        call_kwargs = mock_snap.call_args.kwargs
        assert call_kwargs["local_dir"] == str(
            Path(tmp_work_root) / "hf_model_download"
        )


@pytest.mark.asyncio
async def test_model_download_returns_local_path(tmp_work_root):
    """snapshot_download 被 mock 后返回固定路径，验证字段完整。"""
    fake_path = str(Path(tmp_work_root) / "hf_model_download")
    with patch("athena.tools.hf_model.snapshot_download", return_value=fake_path):
        tool = HFModelDownloadTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-4", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            hf_model_id="google/vit-base",
            output_dir="/unsafe/llm/path",  # 应被忽略
        )

    assert result.success is True
    assert result.data["model_id"] == "google/vit-base"
    assert result.data["model_path"] == fake_path
    assert result.data["status"] == "downloaded"
