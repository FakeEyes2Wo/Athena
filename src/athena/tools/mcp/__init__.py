"""通用 MCP 接入层 —— 装配入口。"""

import sys
from dataclasses import replace

from athena.tools.mcp.config import McpServerConfig, load_mcp_servers
from athena.tools.mcp.adapter import McpToolAdapter
from athena.tools.mcp.client import McpClientError, McpClientManager
from athena.tools.mcp.search import McpSearchTools


def _expand_vars(value: str, work_root: str) -> str:
    """展开字符串中的占位符：``${WORK_ROOT}``、``${PYTHON_EXECUTABLE}``。"""
    return value.replace("${WORK_ROOT}", work_root).replace("${PYTHON_EXECUTABLE}", sys.executable)


def _expand_args(args: list[str], work_root: str) -> list[str]:
    """展开 args 列表中每个元素的占位符。"""
    return [_expand_vars(a, work_root) for a in args]


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
    # 展开 command/args 中的 ${WORK_ROOT} / ${PYTHON_EXECUTABLE} 占位符
    expanded = []
    for cfg in servers:
        patch: dict = {}
        new_command = _expand_vars(cfg.command or "", work_root)
        if new_command != cfg.command:
            patch["command"] = new_command
        new_args = _expand_args(cfg.args, work_root)
        if new_args != cfg.args:
            patch["args"] = new_args
        if patch:
            cfg = replace(cfg, **patch)
        expanded.append(cfg)
    managers = [McpClientManager(cfg) for cfg in expanded]
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
