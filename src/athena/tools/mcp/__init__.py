"""通用 MCP 接入层 —— 装配入口。"""

from athena.tools.mcp.config import McpServerConfig, load_mcp_servers
from athena.tools.mcp.adapter import McpToolAdapter
from athena.tools.mcp.client import McpClientError, McpClientManager
from athena.tools.mcp.search import McpSearchTools


async def register_mcp_tools(
    registry,
    servers: list[McpServerConfig],
    *,
    work_root: str,
    max_discovered: int = 30,
) -> list[McpClientManager]:
    """把配置的 MCP server 接入 registry：注册钉住工具 + 全局搜索工具。

    钉住工具需要构建期连接拉取 schema；未配置钉住工具时全程懒连接。
    """
    managers = [McpClientManager(cfg) for cfg in servers]
    for mgr in managers:
        for name in mgr.cfg.pinned_tools:  # 仅注册钉住工具，避免不必要的连接开销
            await mgr.ensure_connected()
            manifest = {t.name: t for t in mgr.tool_defs()}
            if name not in manifest:
                raise McpClientError(
                    f"server '{mgr.cfg.name}' 没有配置的钉住工具 '{name}'"
                )
            registry.register(McpToolAdapter(mgr, manifest[name], work_root))
    if managers:
        registry.register(
            McpSearchTools(
                managers, registry, work_root, max_discovered=max_discovered
            )
        )
    return managers
