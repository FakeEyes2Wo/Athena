import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from athena.core.tool import ToolRegistry
from athena.core.tool_types import ToolContext
from athena.tools.mcp.client import McpClientManager
from athena.tools.mcp.config import McpServerConfig
from athena.tools.mcp.search import McpSearchTools, _compact_schema


def _tool(name, description):
    t = MagicMock()
    t.name = name
    t.description = description
    t.inputSchema = {"type": "object", "properties": {}}
    return t


def _manager_with(*tools):
    mgr = McpClientManager(McpServerConfig(name="kaggle"))
    mgr.ensure_connected = AsyncMock()
    mgr.tool_defs = MagicMock(return_value=list(tools))
    return mgr


@pytest.mark.asyncio
async def test_name_query_activates_tool(tmp_path):
    mgr = _manager_with(
        _tool("search_competitions", "search kaggle competitions"),
        _tool("submit_competition", "submit predictions to a competition"),
    )
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="submit_competition"
    )
    assert result.success is True
    assert "kaggle__submit_competition" in registry
    assert "kaggle__search_competitions" not in registry
    assert result.data["matches"][0]["tool"] == "kaggle__submit_competition"
    assert result.data["matches"][0]["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_no_match_returns_catalog(tmp_path):
    mgr = _manager_with(
        _tool("search_competitions", "search kaggle competitions"),
        _tool("submit_competition", "submit predictions"),
    )
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="随便什么中文"
    )
    assert result.success is True
    assert result.data["matches"] == []
    # 兜底目录列出全部可用工具（中文 query 无法子串匹配英文描述）
    assert {m["search_with"] for m in result.data["catalog"]} == {
        "search_competitions",
        "submit_competition",
    }
    assert "kaggle__submit_competition" not in registry


@pytest.mark.asyncio
async def test_server_filter_limits_search(tmp_path):
    mgr_a = _manager_with(_tool("download_data", "download data"))
    mgr_a.cfg.name = "kaggle"
    mgr_b = _manager_with(_tool("download_data", "download data"))
    mgr_b.cfg.name = "hf"
    registry = ToolRegistry()
    search = McpSearchTools([mgr_a, mgr_b], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()),
        query="download_data",
        server="kaggle",
    )
    assert result.data["matches"][0]["server"] == "kaggle"
    assert len(result.data["matches"]) == 1


@pytest.mark.asyncio
async def test_max_discovered_cap(tmp_path):
    mgr = _manager_with(
        _tool("tool_a", "alpha"),
        _tool("tool_b", "beta"),
        _tool("tool_c", "gamma"),
    )
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path), max_discovered=2)
    await search.ainvoke(ToolContext("t", "c", AsyncMock(), AsyncMock()), query="tool_")
    assert len(registry) == 2


def test_compact_schema():
    schema = {
        "type": "object",
        "properties": {
            "competition": {"type": "string", "description": "name" * 50},
            "top_k": {"type": "integer"},
        },
        "required": ["competition"],
    }
    compact = _compact_schema(schema)
    assert compact["properties"]["competition"]["type"] == "string"
    assert len(compact["properties"]["competition"]["description"]) <= 120
    assert compact["required"] == ["competition"]


@pytest.mark.asyncio
async def test_result_json_persisted(tmp_path):
    mgr = _manager_with(_tool("search_competitions", "search kaggle competitions"))
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="search_competitions"
    )
    out = Path(result.data["output_dir"])
    assert (out / "result.json").exists()
    record = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert record["query"] == "search_competitions"
    assert {m["tool"] for m in record["matches"]} == {"kaggle__search_competitions"}


@pytest.mark.asyncio
async def test_already_registered_match_reappears(tmp_path):
    mgr = _manager_with(_tool("submit_competition", "submit predictions"))
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path))
    await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="submit_competition"
    )
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="submit_competition"
    )
    # 工具已在 registry，二次搜索仍返回 matches/activated
    assert {m["tool"] for m in result.data["matches"]} == {"kaggle__submit_competition"}
    assert result.data["activated"] == ["kaggle__submit_competition"]


@pytest.mark.asyncio
async def test_top_k_override(tmp_path):
    mgr = _manager_with(
        _tool("tool_a", "alpha"),
        _tool("tool_b", "beta"),
        _tool("tool_c", "gamma"),
    )
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="tool_", top_k=1
    )
    assert len(result.data["matches"]) == 1


@pytest.mark.asyncio
async def test_connection_error_recorded(tmp_path):
    mgr_bad = _manager_with(_tool("bad_tool", "bad"))
    mgr_bad.cfg.name = "kaggle"
    mgr_bad.ensure_connected = AsyncMock(side_effect=RuntimeError("conn fail"))
    mgr_good = _manager_with(_tool("good_tool", "good"))
    mgr_good.cfg.name = "hf"
    registry = ToolRegistry()
    search = McpSearchTools([mgr_bad, mgr_good], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="good"
    )
    # 失败 server 记录错误，其它 server 仍被搜索
    assert any(
        e["server"] == "kaggle" and "conn fail" in e["error"]
        for e in result.data["errors"]
    )
    assert {m["tool"] for m in result.data["matches"]} == {"hf__good_tool"}


@pytest.mark.asyncio
async def test_max_discovered_excludes_from_results(tmp_path):
    mgr = _manager_with(
        _tool("tool_a", "alpha"),
        _tool("tool_b", "beta"),
        _tool("tool_c", "gamma"),
    )
    registry = ToolRegistry()
    search = McpSearchTools([mgr], registry, str(tmp_path), max_discovered=2)
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()), query="tool_"
    )
    assert len(registry) == 2
    assert len(result.data["matches"]) == 2


@pytest.mark.asyncio
async def test_server_filtered_catalog(tmp_path):
    mgr_kaggle = _manager_with(
        _tool("search_competitions", "search kaggle competitions")
    )
    mgr_kaggle.cfg.name = "kaggle"
    mgr_hf = _manager_with(_tool("list_models", "list hf models"))
    mgr_hf.cfg.name = "hf"
    registry = ToolRegistry()
    search = McpSearchTools([mgr_kaggle, mgr_hf], registry, str(tmp_path))
    result = await search.ainvoke(
        ToolContext("t", "c", AsyncMock(), AsyncMock()),
        query="不存在的词",
        server="kaggle",
    )
    assert result.data["matches"] == []
    assert {m["search_with"] for m in result.data["catalog"]} == {
        "search_competitions"
    }
    for m in result.data["catalog"]:
        assert "status" in m
