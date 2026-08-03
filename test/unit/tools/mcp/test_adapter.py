import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from athena.core.tool_types import ToolContext
from athena.tools.mcp.adapter import McpToolAdapter
from athena.tools.mcp.client import McpClientManager
from athena.tools.mcp.config import McpServerConfig


def _make_manager() -> McpClientManager:
    return McpClientManager(McpServerConfig(name="kaggle"))


def _tool_def(name="search_competitions", description="search competitions"):
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = {"type": "object", "properties": {}}
    return tool


def _ctx():
    return ToolContext("t", "call-1", AsyncMock(), AsyncMock())


@pytest.mark.asyncio
async def test_schema_mapping():
    adapter = McpToolAdapter(_make_manager(), _tool_def(), "tmp")
    assert adapter.spec.name == "kaggle__search_competitions"
    assert adapter.spec.concurrency_safe is False
    assert adapter.spec.input_schema["type"] == "object"


@pytest.mark.asyncio
async def test_text_result_persisted(tmp_path):
    manager = _make_manager()
    manager.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="hello")])
    )
    adapter = McpToolAdapter(manager, _tool_def(), str(tmp_path))
    result = await adapter.ainvoke(_ctx(), query="x")
    assert result.success is True
    out = Path(result.data["output_dir"])
    record = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert record["text"] == "hello"
    assert record["tool"] == "kaggle__search_competitions"
    assert result.data["text_preview"] == "hello"


@pytest.mark.asyncio
async def test_structured_content_preferred(tmp_path):
    manager = _make_manager()
    manager.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="raw")],
            structuredContent={"competition": "titanic", "metric": "accuracy"},
        )
    )
    adapter = McpToolAdapter(manager, _tool_def(), str(tmp_path))
    result = await adapter.ainvoke(_ctx(), q=1)
    record = json.loads(
        (Path(result.data["output_dir"]) / "result.json").read_text(encoding="utf-8")
    )
    assert record["structured_content"]["competition"] == "titanic"
    assert record["text"] == "raw"


@pytest.mark.asyncio
async def test_iserror_returns_failure(tmp_path):
    manager = _make_manager()
    manager.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="boom")], isError=True
        )
    )
    adapter = McpToolAdapter(manager, _tool_def(), str(tmp_path))
    result = await adapter.ainvoke(_ctx(), q=1)
    assert result.success is False
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_downloader_follows_url(tmp_path, monkeypatch):
    manager = _make_manager()
    manager.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="data ready")],
            structuredContent={"download_url": "https://example.com/train.csv"},
        )
    )
    adapter = McpToolAdapter(
        manager, _tool_def(name="download_competition_data_file"), str(tmp_path)
    )

    class FakeResponse:
        headers = {}
        content = b"a,b\n1,2\n"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("athena.tools.mcp.adapter.httpx.AsyncClient", FakeClient)
    result = await adapter.ainvoke(_ctx(), q=1)
    out = Path(result.data["output_dir"])
    assert (out / "train.csv").exists()
    assert result.data["files"] == ["train.csv"]


@pytest.mark.asyncio
async def test_non_downloader_ignores_url(tmp_path, monkeypatch):
    manager = _make_manager()
    manager.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[], structuredContent={"url": "https://example.com/x"}
        )
    )
    adapter = McpToolAdapter(
        manager, _tool_def(name="search_competitions"), str(tmp_path)
    )
    result = await adapter.ainvoke(_ctx(), q=1)
    assert result.success is True
    assert result.data["files"] == []


@pytest.mark.asyncio
async def test_image_block_saved(tmp_path):
    manager = _make_manager()
    manager.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")]
        )
    )
    adapter = McpToolAdapter(manager, _tool_def(), str(tmp_path))
    result = await adapter.ainvoke(_ctx(), q=1)
    assert result.data["files"] == ["call-1.png"]
    out = Path(result.data["output_dir"])
    assert (out / "call-1.png").read_bytes() == b"hello"
