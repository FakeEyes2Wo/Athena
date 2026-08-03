from unittest.mock import AsyncMock, MagicMock

import pytest

from athena.tools.mcp.client import McpClientError, McpClientManager
from athena.tools.mcp.config import McpServerConfig


def _fake_session():
    """构造实现 initialize/list_tools/call_tool 的伪造会话。"""
    session = MagicMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock()
    session.call_tool = AsyncMock()
    tool = MagicMock()
    tool.name = "search_competitions"
    tool.description = "search"
    tool.inputSchema = {"type": "object"}
    session.list_tools.return_value = MagicMock(tools=[tool])
    return session


def test_full_name_uses_prefix():
    mgr = McpClientManager(McpServerConfig(name="kaggle"))
    assert (
        mgr.full_name("download_competition_data_file")
        == "kaggle__download_competition_data_file"
    )


@pytest.mark.asyncio
async def test_connect_lists_and_caches_tools():
    session = _fake_session()
    mgr = McpClientManager(
        McpServerConfig(name="kaggle"), session_factory=lambda: session
    )
    await mgr.ensure_connected()
    session.initialize.assert_awaited_once()
    session.list_tools.assert_awaited_once()
    assert mgr.tool_defs()[0].name == "search_competitions"
    # 第二次 ensure_connected 不重复初始化
    await mgr.ensure_connected()
    assert session.initialize.await_count == 1


@pytest.mark.asyncio
async def test_call_tool_forwards_name_and_arguments():
    session = _fake_session()
    mgr = McpClientManager(
        McpServerConfig(name="kaggle"), session_factory=lambda: session
    )
    await mgr.call_tool("search_competitions", {"query": "titanic"})
    session.call_tool.assert_awaited_once_with(
        "search_competitions", arguments={"query": "titanic"}
    )


@pytest.mark.asyncio
async def test_call_tool_reconnects_on_failure():
    session = _fake_session()
    session.call_tool = AsyncMock(
        side_effect=[RuntimeError("connection lost"), MagicMock()]
    )
    mgr = McpClientManager(
        McpServerConfig(name="kaggle"), session_factory=lambda: session
    )
    await mgr.call_tool("x", {})
    assert session.call_tool.await_count == 2


def test_auth_headers_ok(monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "KGAT_test")
    mgr = McpClientManager(McpServerConfig(name="kaggle", auth_env="KAGGLE_API_TOKEN"))
    assert mgr._auth_headers() == {"Authorization": "Bearer KGAT_test"}


def test_auth_headers_missing_env(monkeypatch):
    monkeypatch.delenv("KAGGLE_API_TOKEN_MISSING", raising=False)
    mgr = McpClientManager(
        McpServerConfig(name="kaggle", auth_env="KAGGLE_API_TOKEN_MISSING")
    )
    with pytest.raises(McpClientError, match="KAGGLE_API_TOKEN_MISSING"):
        mgr._auth_headers()


def test_unsupported_transport_raises():
    mgr = McpClientManager(McpServerConfig(name="kaggle", transport="grpc"))
    with pytest.raises(McpClientError, match="不支持"):
        mgr._build_transport()
