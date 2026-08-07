"""Sandbox MCP 集成测试 —— 启动真实 sandbox server 并通过 MCP 协议调用。

已知问题:
- Python 3.14 + anyio scope 不兼容导致 teardown 报 RuntimeError
- 首次 CallToolRequest（python_inspect/python_execute）在 MCP stdio 层偶发超时
  （SandboxExecutor 直接调用正常，问题限于 MCP stdio transport 层）
"""

import json
import tempfile

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


@pytest.fixture
async def sandbox_session():
    """启动 sandbox MCP server 并返回已初始化的 ClientSession。"""
    with tempfile.TemporaryDirectory() as tmp:
        server_params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "athena.sandbox", "--work-root", tmp],
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session, tmp


@pytest.mark.asyncio
async def test_mcp_discovery(sandbox_session):
    """list_tools 返回 3 个工具。"""
    session, _ = sandbox_session
    tools = await session.list_tools()
    tool_names = {t.name for t in tools.tools}
    assert tool_names == {"python_inspect", "python_execute", "sandbox_config"}


@pytest.mark.asyncio
async def test_mcp_sandbox_config(sandbox_session):
    """sandbox_config 返回白名单和限制信息。"""
    session, _ = sandbox_session
    result = await session.call_tool("sandbox_config", arguments={})
    data = json.loads(result.content[0].text)
    assert "whitelist" in data
    assert "pandas" in data["whitelist"]
    assert data["memory_limit_mb"] == 2048


@pytest.mark.asyncio
@pytest.mark.skip(reason="Python 3.14 + MCP stdio 层 CallToolRequest 超时，待环境升级后验证")
async def test_mcp_python_inspect(sandbox_session):
    """通过 MCP 调用 python_inspect 返回正确结果。"""
    session, _ = sandbox_session
    result = await session.call_tool(
        "python_inspect", arguments={"expr": "1 + 2", "timeout": 30}
    )
    data = json.loads(result.content[0].text)
    assert data["ok"] is True
    assert data["value"] == "3"


@pytest.mark.asyncio
@pytest.mark.skip(reason="Python 3.14 + MCP stdio 层 CallToolRequest 超时，待环境升级后验证")
async def test_mcp_python_execute(sandbox_session):
    """通过 MCP 调用 python_execute 执行脚本。"""
    session, _ = sandbox_session
    result = await session.call_tool(
        "python_execute",
        arguments={
            "script": (
                "import os\n"
                "with open('mcp_test.txt', 'w') as f:\n"
                "    f.write('mcp ok')\n"
            ),
            "timeout": 30,
        },
    )
    data = json.loads(result.content[0].text)
    assert data["ok"] is True
    assert "mcp_test.txt" in data["output_files"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
