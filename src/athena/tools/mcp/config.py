"""MCP server 配置 —— McpServerConfig 数据类与 JSON 加载器。"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class McpServerConfig:
    """单个 MCP server 的接入配置，与 mcp_servers.json 的 server 条目一一对应。"""

    name: str
    transport: str = "streamable_http"  # "streamable_http" | "stdio"
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    auth_env: str | None = None
    """bearer token 所在环境变量名；None 表示无需认证。"""
    tool_prefix: str = ""
    pinned_tools: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        """发现工具的全名前缀：显式 tool_prefix 或默认 ``{name}__``。"""
        return self.tool_prefix or f"{self.name}__"


def load_mcp_servers(path: str | Path | None = None) -> list[McpServerConfig]:
    """从 JSON 文件加载 MCP server 配置；文件缺失返回空列表。"""
    config_path = Path(path) if path else Path("mcp_servers.json")
    if not config_path.exists():
        return []
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{config_path} 不是合法 JSON: {exc}") from exc
    servers = raw.get("servers", []) if isinstance(raw, dict) else []
    return [_parse_server(s) for s in servers]


def _parse_server(raw: dict[str, Any]) -> McpServerConfig:
    """把 JSON 的 server 条目映射为 McpServerConfig（含 auth 对象扁平化）。"""
    if "name" not in raw:
        raise ValueError("server 条目缺少必填的 name 字段")
    auth = raw.get("auth") or {}
    return McpServerConfig(
        name=str(raw["name"]),
        transport=str(raw.get("transport", "streamable_http")),
        url=raw.get("url"),
        command=raw.get("command"),
        args=list(raw.get("args", [])),
        auth_env=auth.get("env") if isinstance(auth, dict) else None,
        tool_prefix=str(raw.get("tool_prefix", "")),
        pinned_tools=list(raw.get("pinned_tools", [])),
    )
