"""mcp_search_tools —— 全局搜索入口，发现并激活 MCP 工具。"""

import json
from pathlib import Path
from typing import Any

from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

from athena.tools.mcp.adapter import McpToolAdapter
from athena.tools.mcp.client import McpClientManager


class McpSearchTools(BaseTool):
    """搜索已接入 MCP 服务上的可用工具并激活注册。

    命中工具会注册进 ToolRegistry，使它们在后续 LLM 轮次中可见可调。
    中文 query 无法子串匹配英文描述时，返回全部工具的目录兜底。
    """

    def __init__(
        self,
        managers: list[McpClientManager],
        registry: ToolRegistry,
        work_root: str,
        *,
        max_discovered: int = 30,
        top_k: int = 8,
    ) -> None:
        self._managers = managers
        self._registry = registry
        self._work_root = work_root
        self._max_discovered = max_discovered
        self._top_k = top_k
        self._discovered = 0
        self.output_dir = Path(work_root) / "mcp" / "search_tools"

    spec = ToolSpec(
        name="mcp_search_tools",
        description=(
            "Search available tools exposed by configured external MCP services "
            "(e.g. Kaggle, HuggingFace). Matches by name or description; returns "
            "matching tool names with JSON schemas, and matched tools become "
            "available to call directly in subsequent turns. Use English "
            "keywords for best results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "所需能力的描述或工具名，如 'download competition data'。",
                },
                "server": {
                    "type": "string",
                    "description": "可选，限定在某个 server 内搜索（默认全部）。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回的命中数上限。",
                    "default": 8,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        concurrency_safe=False,  # 会注册工具 + 可能触发网络连接，不可并行
    )

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """在接入的 MCP 服务上搜索工具；命中则注册进 ToolRegistry 并落盘结果。"""
        query = (input.get("query") or "").strip().lower()
        server = input.get("server")
        top_k = int(input.get("top_k", self._top_k))

        # 阶段 1：在各 server 的缓存在线清单上做 name/description 匹配
        matches: list[tuple[McpClientManager, Any, int]] = []
        errors: list[dict] = []
        for mgr in self._managers:
            if server and mgr.cfg.name != server:
                continue
            try:
                await mgr.ensure_connected()
            except Exception as exc:
                # 某 server 连接失败/超时 → 记录错误并继续搜索其他 server
                errors.append({"server": mgr.cfg.name, "error": str(exc)})
                continue
            for tool in mgr.tool_defs():  # 遍历工具清单
                score = 0
                # 按空格拆分 query，分别匹配工具名和描述，命中得分累加
                for word in query.split():
                    if word in tool.name.lower():
                        score += 2  # 单词命中工具名
                    elif word in (tool.description or "").lower():
                        score += 1  # 单词命中描述
                if score > 0:
                    matches.append((mgr, tool, score))

        matches.sort(key=lambda m: m[2], reverse=True)

        # 阶段 2：命中工具注册进 registry（重复跳过，受 max_discovered 上限约束）
        results: list[dict] = []
        activated: list[str] = []
        for mgr, tool, _score in matches[:top_k]:
            full = mgr.full_name(tool.name)
            if full not in self._registry:
                if self._discovered >= self._max_discovered:
                    # 达到单次运行发现上限 → 跳过，不返回未激活工具
                    continue
                self._registry.register(McpToolAdapter(mgr, tool, self._work_root))
                self._discovered += 1
            results.append(
                {
                    "server": mgr.cfg.name,
                    "tool": full,
                    "description": (tool.description or "")[:200],
                    "input_schema": _compact_schema(tool.inputSchema),
                }
            )
            activated.append(full)

        data: dict[str, Any] = {
            "query": input.get("query", ""),
            "matches": results,
            "activated": activated,
            "errors": errors,
        }
        # 阶段 3：无命中时补充目录兜底，并落盘搜索结果
        if not results:
            data["catalog"] = self._catalog(server)
            data["catalog_hint"] = (
                "以下目录中的工具尚未激活，请用工具名作为 query 调用 "
                "mcp_search_tools 搜索激活（如 query='get_competition'），"
                "激活后下一轮即可直接调用"
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return ToolResult(data={**data, "output_dir": str(self.output_dir)})

    def _catalog(self, server: str | None) -> list[dict]:
        """返回（过滤后的）全部工具目录。

        注意：目录项不含可直接调用的工具全名（防止 LLM 直接尝试调用），
        只提供 search_with 参数供 mcp_search_tools 二次搜索用。
        """
        catalog: list[dict] = []
        for mgr in self._managers:
            if server and mgr.cfg.name != server:
                continue
            for tool in mgr.tool_defs():
                catalog.append({
                    "server": mgr.cfg.name,
                    "search_with": tool.name,
                    "description": (tool.description or "")[:120],
                    "status": "未激活（使用 mcp_search_tools 搜索本名激活后可用）",
                })
        return catalog


def _compact_schema(schema: dict | None) -> dict:
    """把 MCP 工具 JSON Schema 压缩为属性名 + 类型的精简形式。"""
    if not isinstance(schema, dict):
        return {"type": "object"}
    props = schema.get("properties") or {}
    compact_props = {}
    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue
        compact_props[name] = {
            "type": prop.get("type", "any"),
            "description": (prop.get("description") or "")[:120],
        }
    return {
        "type": schema.get("type", "object"),
        "properties": compact_props,
        "required": schema.get("required", []),
    }
