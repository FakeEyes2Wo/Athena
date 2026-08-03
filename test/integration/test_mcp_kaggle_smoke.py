"""Kaggle 官方 MCP server 真连冒烟测试（需 KAGGLE_API_TOKEN 环境变量）。"""

import os

import pytest

from athena.tools.mcp.client import McpClientManager
from athena.tools.mcp.config import McpServerConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("KAGGLE_API_TOKEN"),
        reason="缺少 KAGGLE_API_TOKEN 环境变量，跳过 Kaggle 真连测试",
    ),
]


@pytest.mark.asyncio
async def test_kaggle_list_tools_smoke():
    """连接官方 server、初始化、拉取工具清单；不做任何副作用调用。"""
    mgr = McpClientManager(
        McpServerConfig(
            name="kaggle",
            transport="streamable_http",
            url="https://www.kaggle.com/mcp",
            auth_env="KAGGLE_API_TOKEN",
        )
    )
    await mgr.ensure_connected()
    try:
        assert len(mgr.tool_defs()) > 0, "Kaggle MCP server 未返回任何工具"
        names = [t.name for t in mgr.tool_defs()]
        print(f"Kaggle MCP 工具 ({len(names)}): {names}")
    finally:
        await mgr.close()
