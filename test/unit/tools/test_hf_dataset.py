import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from athena.core.tool_types import ToolContext
from athena.tools import hf_dataset as hf_mod
from athena.tools.hf_dataset import HFDatasetDownloadTool, HFDatasetSearchTool


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
    """Mock HfApi 返回固定数据集列表。"""
    with patch("athena.tools.hf_dataset._get_hf_api") as mock_get:
        api = MagicMock()
        ds = MagicMock()
        ds.id = "user/test-dataset"
        ds.description = "A test dataset"
        ds.tags = ["tabular"]
        ds.downloads = 1000
        ds.likes = 50
        api.list_datasets.return_value = [ds]
        mock_get.return_value = api
        yield api


@pytest.mark.asyncio
async def test_dataset_search_writes_json(mock_hf_api, tmp_work_root):
    tool = HFDatasetSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-1", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_keywords="titanic survival", modality="tabular"
    )
    assert result.success is True
    assert result.data["count"] == 1

    # 验证落盘
    results_file = Path(result.data["output_dir"]) / "search_results.json"
    assert results_file.exists()
    on_disk = json.loads(results_file.read_text(encoding="utf-8"))
    assert on_disk["datasets"][0]["id"] == "user/test-dataset"


@pytest.mark.asyncio
async def test_dataset_search_appends_modality(mock_hf_api, tmp_work_root):
    """验证 modality 被追加到了搜索关键词中。"""
    tool = HFDatasetSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-2", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx,
        task_keywords="titanic passenger survival classification",
        modality="tabular",
        n_results=5,
    )
    assert result.success is True
    assert len(result.data["datasets"]) == 1
    assert result.data["datasets"][0]["id"] == "user/test-dataset"

    call_kwargs = mock_hf_api.list_datasets.call_args.kwargs
    assert "tabular" in call_kwargs["search"]


@pytest.mark.asyncio
async def test_dataset_search_empty_results(mock_hf_api, tmp_work_root):
    """空结果时返回 suggestion，且空结果同样落盘。"""
    mock_hf_api.list_datasets.return_value = []
    tool = HFDatasetSearchTool(work_root=tmp_work_root)
    ctx = ToolContext("test", "call-3", AsyncMock(), AsyncMock())

    result = await tool.ainvoke(
        ctx, task_keywords="nonexistent", modality="tabular", n_results=5
    )
    assert result.success is True
    assert len(result.data["datasets"]) == 0
    assert "suggestion" in result.data

    # 验证落盘（空结果也写盘）
    results_file = Path(result.data["output_dir"]) / "search_results.json"
    assert results_file.exists()
    on_disk = json.loads(results_file.read_text(encoding="utf-8"))
    assert on_disk["count"] == 0


@pytest.mark.asyncio
async def test_dataset_download_uses_managed_dir(tmp_work_root):
    """下载工具应使用构造器注入的目录，而非 LLM 传入的路径。"""
    with patch("athena.tools.hf_dataset.snapshot_download") as mock_snap:
        mock_snap.return_value = str(Path(tmp_work_root) / "hf_dataset_download")
        tool = HFDatasetDownloadTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-4", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            hf_dataset_id="user/test-dataset",
            output_dir="/unsafe/llm/path",  # 应被忽略
        )
        assert result.success is True
        assert result.data["output_dir"] == str(
            Path(tmp_work_root) / "hf_dataset_download"
        )
        # 验证 snapshot_download 使用了安全的路径
        mock_snap.assert_called_once()
        call_kwargs = mock_snap.call_args.kwargs
        assert call_kwargs["local_dir"] == str(
            Path(tmp_work_root) / "hf_dataset_download"
        )


@pytest.mark.asyncio
async def test_dataset_download_returns_local_path(tmp_work_root):
    """snapshot_download 被 mock 后返回固定路径，验证字段完整。"""
    fake_path = str(Path(tmp_work_root) / "hf_dataset_download" / "user" / "test-dataset")
    with patch("athena.tools.hf_dataset.snapshot_download", return_value=fake_path):
        tool = HFDatasetDownloadTool(work_root=tmp_work_root)
        ctx = ToolContext("test", "call-5", AsyncMock(), AsyncMock())

        result = await tool.ainvoke(
            ctx,
            hf_dataset_id="user/test-dataset",
            output_dir="/unsafe/llm/path",  # 应被忽略
        )

    assert result.success is True
    assert result.data["dataset_id"] == "user/test-dataset"
    assert result.data["local_path"] == fake_path
    assert result.data["status"] == "downloaded"
