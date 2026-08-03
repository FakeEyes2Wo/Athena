# 通用 MCP 接入层实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Athena 构建通用 MCP 接入层（配置驱动、懒连接、Codex 式"搜索激活"工具暴露），以 Kaggle 官方 MCP server（`https://www.kaggle.com/mcp`）为首个接入方，替换现有 3 个 Kaggle stub 工具。

**Architecture:** 新增 `src/athena/tools/mcp/` 子包：
- `config.py` — `McpServerConfig`（dataclass）+ `load_mcp_servers()`（JSON 加载，零代码接入）；
- `client.py` — `McpClientManager`（单 server：懒连接、`list_tools` 清单缓存、`call_tool` 转发、断线重连、bearer 认证）；
- `adapter.py` — `McpToolAdapter(BaseTool)`（MCP 工具→Athena 工具，schema 映射、结果归一化、统一落盘、下载处理）；
- `search.py` — `McpSearchTools(BaseTool)`（唯一常驻入口，搜索并激活注册命中工具）；
- `__init__.py` — `register_mcp_tools()` 装配入口。

工具暴露模型：所有 MCP 工具默认隐藏，每轮请求仅发送原生工具 + `mcp_search_tools`；命中即注册进 `ToolRegistry`，下一轮 LLM 请求即出现（`provider.py:47` 每轮从 `config.tools.specs` 重建工具定义）。`build_task_understand_agent` 改为 async 并接收 `mcp_servers`。

**Tech Stack:** Python ≥3.11、官方 `mcp` Python SDK（`streamable_http`/`stdio` 传输）、`httpx`（下载跟随重定向）、pytest（`asyncio_mode=auto`）。

## Global Constraints

- 开发过程文档（review / report / 计划 / commit 消息）一律使用**中文**；commit 采用 `feat:` / `fix:` / `docs:` 前缀 + 中文描述。
- 代码遵循 `docs/代码规范.md`：
  - 导入全部置文件开头（标准库 → 第三方 → 项目内）；不使用 `from __future__ import annotations`；`TYPE_CHECKING` 仅用于避免循环导入；
  - 公开函数/方法/类须有描述"做什么"的 docstring；超 15 行函数在关键阶段前加单行注释；**禁止 ASCII 装饰分隔线**（新代码不用 `# ──` 等）；
  - `except` 捕获特定异常须注释触发场景；禁止裸 `except:`；
  - 空行规范（模块级/代码块内各一个空行）；嵌套 ≤3 层；简单优于复杂；
  - 每个模块配有单元测试。
- Python ≥3.11，black line-length=88。
- pytest `asyncio_mode=auto`：测试可直接 `async def` + `await`。
- 新增依赖：`mcp`、`httpx`；**不引入** `kaggle` / `kagglehub`。
- `.env` 已配置 `KAGGLE_API_TOKEN=KGAT_...`（官方 MCP bearer token），冒烟测试以其为门控。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/athena/tools/mcp/__init__.py` | Create | 包入口 + `register_mcp_tools()` |
| `src/athena/tools/mcp/config.py` | Create | `McpServerConfig` / `load_mcp_servers` / `_parse_server` |
| `src/athena/tools/mcp/client.py` | Create | `McpClientManager` / `McpClientError` |
| `src/athena/tools/mcp/adapter.py` | Create | `McpToolAdapter` + 结果归一化/落盘/下载辅助函数 |
| `src/athena/tools/mcp/search.py` | Create | `McpSearchTools` + `_compact_schema` + 目录兜底 |
| `test/unit/tools/mcp/__init__.py` | Create | 空文件（子包结构，父目录已有包约定） |
| `test/unit/tools/mcp/test_config.py` | Create | 配置解析测试 |
| `test/unit/tools/mcp/test_client.py` | Create | 客户端连接/缓存/转发/重连/认证测试 |
| `test/unit/tools/mcp/test_adapter.py` | Create | 适配器映射/归一化/落盘/下载测试 |
| `test/unit/tools/mcp/test_search.py` | Create | 搜索匹配/激活注册/兜底目录/上限测试 |
| `test/integration/test_mcp_kaggle_smoke.py` | Create | Kaggle 真连冒烟（`KAGGLE_API_TOKEN` 门控） |
| `src/athena/tools/kaggle_search.py` | Delete | 3 个 stub 由 MCP 自动发现取代 |
| `test/unit/tools/test_kaggle_search.py` | Delete | stub 测试随模块删除 |
| `src/athena/agents/competition/task_understand_agent.py` | Modify | async build + `mcp_servers` 参数 + 删除 kaggle 注册 + 提示词 + 断言 13→10 |
| `test/integration/test_task_understand_agent.py` | Modify | `await` build、工具数 13→10、新增 MCP 注册断言 |
| `demo_taskunderstand_agent.py` | Modify | `await` build、seeded 提示词去除 kaggle 引用 |
| `src/athena/tools/data_prepare.py` | Modify | `_TOOL_OUTPUT_DIRS` 去 kaggle 条目；扫描器递归 |
| `test/unit/tools/test_data_prepare.py` | Modify | 新增递归扫描测试 |
| `pyproject.toml` | Modify | 加 `mcp`、`httpx` 依赖 |
| `mcp_servers.json.example` | Create | 零代码接入配置示例 |

---

### Task 1: `McpServerConfig` + `load_mcp_servers`

**Files:**
- Create: `src/athena/tools/mcp/__init__.py`（先建空包，Task 6 填充 `register_mcp_tools`）
- Create: `src/athena/tools/mcp/config.py`
- Test: `test/unit/tools/mcp/test_config.py`

**Interfaces:**
- Consumes: 无（纯 dataclass，不依赖 MCP SDK）
- Produces:
  - `McpServerConfig(name, transport="streamable_http", url=None, command=None, args=[], auth_env=None, tool_prefix="", pinned_tools=[])`（`slots=True`）
  - `McpServerConfig.prefix -> str`（`tool_prefix` 或 `f"{name}__"`）
  - `load_mcp_servers(path: str | Path | None = None) -> list[McpServerConfig]`（文件缺失返回 `[]`；JSON 的 `auth.env` 映射为 `auth_env`）

- [ ] **Step 0: 建包结构**

创建 `test/unit/tools/mcp/__init__.py`（空文件，保证 pytest 子包导入正常）：

```bash
mkdir -p test/unit/tools/mcp
touch test/unit/tools/mcp/__init__.py
```

- [ ] **Step 1: 写失败测试**

```python
# test/unit/tools/mcp/test_config.py
import json
import tempfile
from pathlib import Path

from athena.tools.mcp.config import McpServerConfig, load_mcp_servers


def test_load_parses_servers_and_auth_env():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "mcp_servers.json"
        cfg_path.write_text(
            json.dumps({
                "servers": [
                    {
                        "name": "kaggle",
                        "transport": "streamable_http",
                        "url": "https://www.kaggle.com/mcp",
                        "auth": {"type": "bearer", "env": "KAGGLE_API_TOKEN"},
                        "tool_prefix": "kaggle__",
                        "pinned_tools": [],
                    }
                ]
            }),
            encoding="utf-8",
        )
        servers = load_mcp_servers(cfg_path)
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "kaggle"
    assert s.transport == "streamable_http"
    assert s.url == "https://www.kaggle.com/mcp"
    assert s.auth_env == "KAGGLE_API_TOKEN"
    assert s.tool_prefix == "kaggle__"
    assert s.pinned_tools == []


def test_load_missing_file_returns_empty():
    assert load_mcp_servers(Path("/nonexistent/mcp_servers.json")) == []


def test_default_prefix_is_name_double_underscore():
    assert McpServerConfig(name="kaggle").prefix == "kaggle__"
    assert McpServerConfig(name="hf", tool_prefix="hf_").prefix == "hf_"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/tools/mcp/test_config.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'athena.tools.mcp'`

- [ ] **Step 3: 最小实现**

```python
# src/athena/tools/mcp/config.py
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
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    servers = raw.get("servers", []) if isinstance(raw, dict) else []
    return [_parse_server(s) for s in servers]


def _parse_server(raw: dict[str, Any]) -> McpServerConfig:
    """把 JSON 的 server 条目映射为 McpServerConfig（含 auth 对象扁平化）。"""
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/mcp/test_config.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/mcp/ test/unit/tools/mcp/test_config.py
git commit -m "feat: 新增 MCP server 配置模块（McpServerConfig + JSON 加载）"
```

---

### Task 2: 添加 `mcp` 与 `httpx` 依赖

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: 无
- Produces: 项目依赖含 `mcp>=1.0.0`、`httpx>=0.27.0`（Task 3-4 的 import 前提）

- [ ] **Step 1: 修改依赖声明**

在 `pyproject.toml` 的 `[project].dependencies` 末尾追加（保持字母序在 `openai` 前插入 `httpx`、`mcp`）：

```toml
dependencies = [
    "httpx>=0.27.0",
    "huggingface_hub>=0.20",
    "mcp>=1.0.0",
    "openai>=2.48.0",
    "pydantic>=2.0",
    "pydantic-ai>=2.17.0",
    "pylatexenc>=2.10",
    "pymupdf>=1.24",
    "python-dotenv>=1.0",
]
```

- [ ] **Step 2: 更新 lock 并验证可导入**

Run: `uv lock && uv sync`
Run: `uv run python -c "import mcp, httpx; print('mcp ok, httpx ok')"`
Expected: 输出 `mcp ok, httpx ok`

> 若安装的 mcp SDK 版本与计划中 API（`mcp.ClientSession` / `mcp.client.streamable_http` / `mcp.client.stdio` / `mcp.types`）有出入，以 `uv run python -c "import mcp; print(mcp.__version__)"` 的实际版本为准微调 import 路径。

- [ ] **Step 3: 提交**

注意：`uv.lock` 在 `.gitignore` 中（第 2 行），**不提交**，只提交 `pyproject.toml`：

```bash
git add pyproject.toml
git commit -m "build: 添加 mcp 与 httpx 依赖"
```

---

### Task 3: `McpClientManager`

**Files:**
- Create: `src/athena/tools/mcp/client.py`
- Test: `test/unit/tools/mcp/test_client.py`

**Interfaces:**
- Consumes: Task 1 的 `McpServerConfig`
- Produces:
  - `McpClientError(RuntimeError)`
  - `McpClientManager(cfg: McpServerConfig, *, session_factory: Any | None = None)`（`session_factory` 供测试注入伪造会话）
  - `mgr.prefix -> str`、`mgr.full_name(tool_name) -> str`
  - `await mgr.ensure_connected()`、`await mgr.connect()`、`await mgr.call_tool(tool_name, arguments) -> CallToolResult`、`mgr.tool_defs() -> list[Tool]`、`await mgr.close()`
  - 懒连接：首次调用才建会话并 `list_tools` 缓存清单

- [ ] **Step 1: 写失败测试**

```python
# test/unit/tools/mcp/test_client.py
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
    assert mgr.full_name("download_competition_data_file") == "kaggle__download_competition_data_file"


@pytest.mark.asyncio
async def test_connect_lists_and_caches_tools():
    session = _fake_session()
    mgr = McpClientManager(McpServerConfig(name="kaggle"), session_factory=lambda: session)
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
    mgr = McpClientManager(McpServerConfig(name="kaggle"), session_factory=lambda: session)
    await mgr.call_tool("search_competitions", {"query": "titanic"})
    session.call_tool.assert_awaited_once_with(
        "search_competitions", arguments={"query": "titanic"}
    )


@pytest.mark.asyncio
async def test_call_tool_reconnects_on_failure():
    session = _fake_session()
    session.call_tool = AsyncMock(side_effect=[RuntimeError("connection lost"), MagicMock()])
    mgr = McpClientManager(McpServerConfig(name="kaggle"), session_factory=lambda: session)
    await mgr.call_tool("x", {})
    assert session.call_tool.await_count == 2


def test_auth_headers_ok(monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "KGAT_test")
    mgr = McpClientManager(McpServerConfig(name="kaggle", auth_env="KAGGLE_API_TOKEN"))
    assert mgr._auth_headers() == {"Authorization": "Bearer KGAT_test"}


def test_auth_headers_missing_env(monkeypatch):
    monkeypatch.delenv("KAGGLE_API_TOKEN_MISSING", raising=False)
    mgr = McpClientManager(McpServerConfig(name="kaggle", auth_env="KAGGLE_API_TOKEN_MISSING"))
    with pytest.raises(McpClientError, match="KAGGLE_API_TOKEN_MISSING"):
        mgr._auth_headers()


def test_unsupported_transport_raises():
    mgr = McpClientManager(McpServerConfig(name="kaggle", transport="grpc"))
    with pytest.raises(McpClientError, match="不支持"):
        mgr._build_transport()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/tools/mcp/test_client.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'athena.tools.mcp.client'`

- [ ] **Step 3: 最小实现**

```python
# src/athena/tools/mcp/client.py
"""MCP 客户端管理 —— 连接、工具清单缓存、call_tool 转发。"""

import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from athena.tools.mcp.config import McpServerConfig


class McpClientError(RuntimeError):
    """MCP 连接或调用失败。"""


class McpClientManager:
    """管理单个 MCP server 的连接与调用。

    默认懒连接：首次调用 ensure_connected() 才建立会话并拉取工具清单。
    测试可通过 session_factory 注入伪造会话，避免真实网络。
    """

    def __init__(
        self,
        cfg: McpServerConfig,
        *,
        session_factory: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._manifest: list[Any] = []
        self._session_factory = session_factory

    @property
    def prefix(self) -> str:
        return self.cfg.prefix

    def full_name(self, tool_name: str) -> str:
        """拼接 Athena 侧工具全名（含前缀）。"""
        return f"{self.prefix}{tool_name}"

    async def connect(self) -> None:
        """建立连接、初始化会话并缓存工具清单。"""
        if self._session is not None:
            return
        if self._session_factory is not None:
            session = self._session_factory()
            await session.initialize()
            self._session = session
            self._manifest = list((await session.list_tools()).tools)
            return
        stack = AsyncExitStack()
        try:
            read, write, *_ = await stack.enter_async_context(
                self._build_transport()
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as exc:
            await stack.aclose()
            raise McpClientError(
                f"连接 MCP server '{self.cfg.name}' 失败: {exc}"
            ) from exc
        self._stack = stack
        self._session = session
        self._manifest = list((await session.list_tools()).tools)

    def _build_transport(self) -> Any:
        """按配置构建 streamable_http 或 stdio 传输的异步上下文管理器。"""
        headers = self._auth_headers()
        if self.cfg.transport == "streamable_http":
            if not self.cfg.url:
                raise McpClientError(f"server '{self.cfg.name}' 缺 url")
            return streamablehttp_client(self.cfg.url, headers=headers)
        if self.cfg.transport == "stdio":
            if not self.cfg.command:
                raise McpClientError(f"server '{self.cfg.name}' 缺 command")
            return stdio_client(
                StdioServerParameters(command=self.cfg.command, args=self.cfg.args)
            )
        raise McpClientError(
            f"server '{self.cfg.name}' 不支持的 transport: {self.cfg.transport}"
        )

    def _auth_headers(self) -> dict[str, str] | None:
        """从环境变量读取 bearer token 构造认证头；未配置则返回 None。"""
        if not self.cfg.auth_env:
            return None
        token = os.environ.get(self.cfg.auth_env)
        if not token:
            raise McpClientError(
                f"缺少环境变量 {self.cfg.auth_env}（MCP server '{self.cfg.name}' 认证）"
            )
        return {"Authorization": f"Bearer {token}"}

    async def ensure_connected(self) -> None:
        """确保已连接；未连接则建立。"""
        if self._session is None:
            await self.connect()

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """调用 MCP 工具，返回 CallToolResult（或其等价对象）。"""
        await self.ensure_connected()
        try:
            return await self._session.call_tool(tool_name, arguments=arguments)
        except Exception:
            # 连接可能已失效 → 重连一次后重试
            await self.close()
            await self.connect()
            return await self._session.call_tool(tool_name, arguments=arguments)

    def tool_defs(self) -> list[Any]:
        """已缓存的工具定义列表（name/description/inputSchema）。"""
        return list(self._manifest)

    async def close(self) -> None:
        """关闭连接并清空会话。"""
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._session = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/mcp/test_client.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/mcp/client.py test/unit/tools/mcp/test_client.py
git commit -m "feat: 新增 McpClientManager（懒连接/清单缓存/重连/认证）"
```

---

### Task 4: `McpToolAdapter`

**Files:**
- Create: `src/athena/tools/mcp/adapter.py`
- Test: `test/unit/tools/mcp/test_adapter.py`

**Interfaces:**
- Consumes: Task 3 的 `McpClientManager`；MCP SDK 类型 `mcp.types.CallToolResult` / `TextContent` / `ImageContent`
- Produces:
  - `McpToolAdapter(manager: McpClientManager, tool: Any, work_root: str) -> BaseTool`
  - spec 命名 `{manager.full_name(tool.name)}`，`concurrency_safe=False`
  - `output_dir = {work_root}/mcp/{server}/{tool.name}`；每次调用落盘 `result.json`
  - `execute()` 返回 `ToolResult(data={server, tool, text_preview, files, output_dir})`；失败返回 `ToolResult(success=False, error=...)` 且同样落盘

- [ ] **Step 1: 写失败测试**

```python
# test/unit/tools/mcp/test_adapter.py
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
    record = json.loads((Path(result.data["output_dir"]) / "result.json").read_text(encoding="utf-8"))
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
        return_value=CallToolResult(structuredContent={"url": "https://example.com/x"})
    )
    adapter = McpToolAdapter(manager, _tool_def(name="search_competitions"), str(tmp_path))
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/tools/mcp/test_adapter.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'athena.tools.mcp.adapter'`

- [ ] **Step 3: 最小实现**

```python
# src/athena/tools/mcp/adapter.py
"""MCP 工具适配器 —— 把发现的 MCP 工具包装成 Athena 的 BaseTool。"""

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from athena.core.tool import BaseTool
from athena.core.tool_types import ToolContext, ToolResult, ToolSpec

from athena.tools.mcp.client import McpClientManager

_DOWNLOAD_KEYWORDS = ("download", "get_file")
"""工具名包含这些关键字时，把结果里的 URL/资源块当作文件下载处理。"""

_DOWNLOAD_URL_KEYS = ("download_url", "redirect_url", "url")

_URL_RE = re.compile(r"https?://[^\s'\"]+")


class McpToolAdapter(BaseTool):
    """由一个 MCP 工具定义构建的通用工具。

    每个实例对应一个发现的 MCP 工具：execute() 转发 call_tool，结果归一化后
    统一落盘到 {work_root}/mcp/{server}/{tool}/result.json。
    """

    def __init__(self, manager: McpClientManager, tool: Any, work_root: str) -> None:
        self._manager = manager
        self._mcp_tool = tool
        self._is_downloader = any(
            kw in tool.name.lower() for kw in _DOWNLOAD_KEYWORDS
        )
        self.output_dir = Path(work_root) / "mcp" / manager.cfg.name / tool.name
        self.spec = ToolSpec(
            name=manager.full_name(tool.name),
            description=self._build_description(tool),
            input_schema=tool.inputSchema or {"type": "object", "properties": {}},
            concurrency_safe=False,  # 落盘 + 网络调用，不可并行
        )

    def _build_description(self, tool: Any) -> str:
        desc = (tool.description or "").strip()
        return desc or f"MCP 工具 {tool.name}（来源 {self._manager.cfg.name}）"

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._manager.call_tool(
                self._mcp_tool.name, arguments=input
            )
        except Exception as exc:
            return self._persist(
                {"error": f"{type(exc).__name__}: {exc}"},
                success=False,
                error=str(exc),
            )

        if getattr(result, "isError", False):
            text = _collect_text(result)
            return self._persist(
                {"error": text or "MCP 工具返回 isError"},
                success=False,
                error=text or "MCP 工具返回 isError",
            )

        payload, saved_files = await self._normalize(result, ctx)
        return self._persist({**payload, "files": saved_files}, success=True)

    async def _normalize(self, result: Any, ctx: ToolContext) -> tuple[dict, list[str]]:
        """把 MCP 结果归一化为 payload（文本/结构化/文件），返回保存的文件列表。"""
        payload: dict[str, Any] = {}
        text_parts: list[str] = []
        saved_files: list[str] = []

        structured = getattr(result, "structuredContent", None)
        if structured:
            payload["structured_content"] = structured

        for block in getattr(result, "content", []):
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype in ("image", "audio"):
                saved_files.append(
                    _save_binary_block(block, self.output_dir, ctx.call_id)
                )
            elif btype == "resource":
                saved_files.append(
                    _save_resource_block(block, self.output_dir, ctx.call_id)
                )

        if text_parts:
            payload["text"] = "\n".join(text_parts)

        if self._is_downloader:
            url = _extract_download_url(payload)
            if url:
                saved_files += await self._download_to_disk(url)
        return payload, saved_files

    async def _download_to_disk(self, url: str) -> list[str]:
        """跟随重定向下载文件到输出目录，返回保存的文件名。"""
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            fname = _infer_filename(resp, url)
            (self.output_dir / fname).write_bytes(resp.content)
            return [fname]

    def _persist(
        self,
        payload: dict,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> ToolResult:
        """把调用结果落盘到 result.json，并构造返回给 LLM 的 ToolResult。"""
        record = {
            "server": self._manager.cfg.name,
            "tool": self.spec.name,
            "success": success,
            "error": error,
            "output_dir": str(self.output_dir),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **payload,
        }
        (self.output_dir / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        data = {
            "server": self._manager.cfg.name,
            "tool": self.spec.name,
            "text_preview": _preview(payload),
            "files": payload.get("files", []),
            "output_dir": str(self.output_dir),
        }
        if not success:
            return ToolResult(success=False, error=error, data=data)
        return ToolResult(data=data)


def _collect_text(result: Any) -> str:
    """拼接 MCP 结果中的全部文本块。"""
    parts = [
        getattr(b, "text", "")
        for b in getattr(result, "content", [])
        if getattr(b, "type", "") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _save_binary_block(block: Any, output_dir: Path, call_id: str) -> str:
    """把 image/audio 二进制块解码保存，返回文件名。"""
    ext = "png" if getattr(block, "type", "") == "image" else "bin"
    fname = f"{_safe_call_id(call_id)}.{ext}"
    data = getattr(block, "data", "")
    (output_dir / fname).write_bytes(base64.b64decode(data))
    return fname


def _save_resource_block(block: Any, output_dir: Path, call_id: str) -> str:
    """把嵌入式资源块保存到磁盘，返回文件名。"""
    uri = getattr(block, "uri", "") or ""
    ext = (getattr(block, "mimeType", "") or "").split("/")[-1] or "bin"
    fname = f"{_safe_call_id(call_id)}-{Path(uri).name or 'resource'}.{ext}"
    data = getattr(block, "data", None)
    if data is None:
        # 无内联数据，仅记录文件名，不写盘
        return fname
    raw = base64.b64decode(data) if isinstance(data, str) else data
    (output_dir / fname).write_bytes(raw)
    return fname


def _safe_call_id(call_id: str) -> str:
    """把 call_id 规整为安全文件名。"""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in call_id)


def _extract_download_url(payload: dict) -> str | None:
    """从工具自身返回的结果里提取下载 URL；只在结果字段中查找。"""
    structured = payload.get("structured_content")
    candidates: list[Any] = []
    if isinstance(structured, dict):
        candidates.extend(structured.get(k) for k in _DOWNLOAD_URL_KEYS)
    text = payload.get("text", "")
    if text:
        m = _URL_RE.search(text)
        if m:
            candidates.append(m.group(0))
    for val in candidates:
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            return val
    return None


def _infer_filename(resp: Any, url: str) -> str:
    """从 Content-Disposition 或 URL 推断下载文件名。"""
    disposition = resp.headers.get("content-disposition", "")
    if "filename=" in disposition:
        candidate = disposition.split("filename=")[-1].strip('"').split(";")[0].strip()
        if candidate:
            return candidate
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return name or "download.bin"


def _preview(payload: dict) -> str:
    """生成返回给 LLM 的文本预览（最多 2000 字符）。"""
    text = payload.get("text", "")
    if text:
        return text[:2000]
    structured = payload.get("structured_content")
    if isinstance(structured, dict):
        return json.dumps(structured, ensure_ascii=False)[:2000]
    return ""
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/mcp/test_adapter.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/mcp/adapter.py test/unit/tools/mcp/test_adapter.py
git commit -m "feat: 新增 McpToolAdapter（schema 映射/结果归一化/落盘/下载）"
```

---

### Task 5: `McpSearchTools`

**Files:**
- Create: `src/athena/tools/mcp/search.py`
- Test: `test/unit/tools/mcp/test_search.py`

**Interfaces:**
- Consumes: Task 3 `McpClientManager`、Task 4 `McpToolAdapter`；`athena.core.tool.ToolRegistry`
- Produces:
  - `McpSearchTools(managers, registry, work_root, *, max_discovered=30, top_k=8) -> BaseTool`
  - spec.name = `mcp_search_tools`，`concurrency_safe=False`
  - 搜索在内存清单上匹配（name 命中优先）；命中工具注册进 registry（重复跳过，受 `max_discovered` 上限约束）；无命中时返回目录兜底
  - 每次调用落盘 `{work_root}/mcp/search_tools/result.json`

- [ ] **Step 1: 写失败测试**

```python
# test/unit/tools/mcp/test_search.py
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
    assert {m["tool"] for m in result.data["catalog"]} == {
        "kaggle__search_competitions",
        "kaggle__submit_competition",
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/tools/mcp/test_search.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'athena.tools.mcp.search'`

- [ ] **Step 3: 最小实现**

```python
# src/athena/tools/mcp/search.py
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
        query = input["query"].strip().lower()
        server = input.get("server")
        top_k = int(input.get("top_k", self._top_k))

        matches: list[tuple[McpClientManager, Any, int]] = []
        errors: list[dict] = []
        for mgr in self._managers:
            if server and mgr.cfg.name != server:
                continue
            try:
                await mgr.ensure_connected()
            except Exception as exc:
                errors.append({"server": mgr.cfg.name, "error": str(exc)})
                continue
            for tool in mgr.tool_defs():
                if query in tool.name.lower():
                    matches.append((mgr, tool, 2))
                elif query in f"{tool.name} {tool.description or ''}".lower():
                    matches.append((mgr, tool, 1))

        matches.sort(key=lambda m: m[2], reverse=True)

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
            results.append({
                "server": mgr.cfg.name,
                "tool": full,
                "description": (tool.description or "")[:200],
                "input_schema": _compact_schema(tool.inputSchema),
            })
            activated.append(full)

        data: dict[str, Any] = {
            "query": input["query"],
            "matches": results,
            "activated": activated,
            "errors": errors,
        }
        if not results:
            data["catalog"] = await self._catalog(server)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return ToolResult(data={**data, "output_dir": str(self.output_dir)})

    async def _catalog(self, server: str | None) -> list[dict]:
        """返回（过滤后的）全部工具的精简目录，供 LLM 选择后二次精确搜索。"""
        catalog: list[dict] = []
        for mgr in self._managers:
            if server and mgr.cfg.name != server:
                continue
            for tool in mgr.tool_defs():
                catalog.append({
                    "server": mgr.cfg.name,
                    "tool": mgr.full_name(tool.name),
                    "description": (tool.description or "")[:120],
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/mcp/test_search.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/mcp/search.py test/unit/tools/mcp/test_search.py
git commit -m "feat: 新增 mcp_search_tools（搜索匹配/激活注册/目录兜底）"
```

---

### Task 6: 接入 agent 构建 —— 删除 kaggle stub、async build

**Files:**
- Create: `src/athena/tools/mcp/__init__.py`（填充 `register_mcp_tools`）
- Delete: `src/athena/tools/kaggle_search.py`、`test/unit/tools/test_kaggle_search.py`
- Modify: `src/athena/agents/competition/task_understand_agent.py`
- Modify: `test/integration/test_task_understand_agent.py`
- Modify: `demo_taskunderstand_agent.py`

**Interfaces:**
- Consumes: Task 1/3/4/5 的 `McpServerConfig` / `McpClientManager` / `McpToolAdapter` / `McpSearchTools`
- Produces:
  - `await register_mcp_tools(registry, servers: list[McpServerConfig], *, work_root: str, max_discovered=30) -> list[McpClientManager]`：注册钉住工具（构建期连接）+ `mcp_search_tools`
  - `await build_task_understand_agent(model, client=None, *, work_root="work", max_turns=30, max_tokens=8192, temperature=0.1, mcp_servers: list[McpServerConfig] | None = None) -> Agent`：async 版本；`mcp_servers=None` 时从 `mcp_servers.json` 加载；原生工具 10 个

- [ ] **Step 1: 写失败测试（新增 MCP 注册断言）**

在 `test/integration/test_task_understand_agent.py` 中，把既有测试改为 async 并适配工具数；同时新增：

```python
async def test_mcp_servers_registers_search_tool(self):
    """配置 mcp_servers 时注册 mcp_search_tools，且构建期不联网。"""
    from athena.tools.mcp.config import McpServerConfig

    agent = await build_task_understand_agent(
        model="deepseek-v4-flash",
        client=None,
        mcp_servers=[McpServerConfig(name="kaggle")],
    )
    assert "mcp_search_tools" in agent.config.tools
    assert len(agent.config.tools) == 11  # 10 原生 + 1 搜索工具
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/integration/test_task_understand_agent.py -v`
Expected: FAIL —— `TypeError: object Agent can't be used in 'await' expression`（build 仍为同步）

- [ ] **Step 3: 实现**

**3a. 填充 `src/athena/tools/mcp/__init__.py`：**

```python
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
        for name in mgr.cfg.pinned_tools:
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
```

**3b. 修改 `task_understand_agent.py`：**

删除 kaggle 相关 import 与注册；import 增加：

```python
from athena.tools.mcp import register_mcp_tools
from athena.tools.mcp.config import McpServerConfig, load_mcp_servers
```

删除 `from athena.tools.kaggle_search import (...)` 整块。系统提示词规则 1 改为：

```python
1. When you need external data or services (e.g. Kaggle, HuggingFace),
   FIRST search for available tools with mcp_search_tools, then call the
   discovered tool by its full name in a later turn.
```

函数签名与注册块改为：

```python
async def build_task_understand_agent(
    model: str,
    client: "AsyncOpenAI | None" = None,
    *,
    work_root: str = "work",
    max_turns: int = 30,
    max_tokens: int = 8192,
    temperature: float = 0.1,
    mcp_servers: list[McpServerConfig] | None = None,
) -> Agent:
    """构建 TaskUnderstandAgent，注册全部原生工具与可选 MCP 工具。

    mcp_servers 为 None 时尝试从当前目录 mcp_servers.json 加载；
    未配置任何 server 时 MCP 接入层不生效。
    """
    tools = ToolRegistry()

    # 数据获取层 —— HF 数据集 (2 工具)
    tools.register(HFDatasetSearchTool(work_root=work_root))
    tools.register(HFDatasetDownloadTool(work_root=work_root))

    # 数据获取层 —— HF 模型 (2 工具)
    tools.register(HFModelSearchTool(work_root=work_root))
    tools.register(HFModelDownloadTool(work_root=work_root))

    # 数据准备层 (2 工具)
    tools.register(DataAnalyzeTool(work_root=work_root))
    tools.register(DataCleanCodeGenTool(work_root=work_root))

    # 建模 & 提交层 (4 工具)
    tools.register(SolutionDesignTool(work_root=work_root))
    tools.register(ProjectCodeGenTool(work_root=work_root))
    tools.register(CodeExecuteTool(work_root=work_root))
    tools.register(SubmissionBuildTool(work_root=work_root))

    # Guard：原生工具数量固定；MCP 工具在下方动态追加，不参与此断言
    assert len(tools) == 10, f"Expected 10 native tools, got {len(tools)}"

    # MCP 接入层（可选）：配置驱动，懒连接；未配置 server 时完全惰性
    servers = mcp_servers if mcp_servers is not None else load_mcp_servers()
    if servers:
        await register_mcp_tools(tools, servers, work_root=work_root)

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=_COMPETITION_SYSTEM_PROMPT,
        client=client,
        max_turns=max_turns,
        max_tokens=max_tokens,
        temperature=temperature,
        name="TaskUnderstandAgent",
        description=(
            "Autonomous Kaggle competition agent that researches, "
            "downloads data, builds baselines, and produces submissions."
        ),
    )
```

**3c. 修改 `test/integration/test_task_understand_agent.py`：**

- 4 个 sync 测试方法改为 `async def`，`build_task_understand_agent(...)` 前加 `await`；
- 工具数断言 13 → 10，`expected_names` 去掉 3 个 kaggle 名；
- `test_agent_config_defaults` 中 `"Kaggle" in system_prompt` 改为 `"mcp_search_tools" in system_prompt`；
- 被 skip 的 `test_agent_loads_all_tools`：`async def` + `await`，`len == 10`，去掉 `kaggle_competition_search` 断言；
- 追加 Step 1 中的 `test_mcp_servers_registers_search_tool`。

**3d. 修改 `demo_taskunderstand_agent.py`：**

- 第 369 行 `agent = build_task_understand_agent(` → `agent = await build_task_understand_agent(`；
- seeded 提示词第 78-80 行：

```python
CRITICAL RULES (follow strictly to avoid wasting turns on unavailable tools):
1. DO NOT call mcp_search_tools — external MCP tools are NOT needed in
   seeded mode.
2. DO NOT call code_execute — the sandbox is not ready. Skip training/inference
```

- `--mode` 的 help 文案中"绕过 Kaggle stub"改为"绕过 MCP 外部工具，预注入竞赛信息"。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/integration/test_task_understand_agent.py test/unit/tools/mcp -v`
Expected: PASS；同时确认 `uv run pytest -q` 全量通过（删除 kaggle stub 后无残留引用）

- [ ] **Step 5: 提交**

```bash
git add -A src/athena/tools/mcp/ src/athena/agents/competition/task_understand_agent.py test/integration/test_task_understand_agent.py demo_taskunderstand_agent.py
git rm src/athena/tools/kaggle_search.py test/unit/tools/test_kaggle_search.py
git commit -m "feat: MCP 接入层接入 agent 构建（async build + 搜索激活），删除 Kaggle stub"
```

---

### Task 7: `data_prepare` 扫描器支持 MCP 嵌套下载目录

**Files:**
- Modify: `src/athena/tools/data_prepare.py`
- Test: `test/unit/tools/test_data_prepare.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `_TOOL_OUTPUT_DIRS` 移除 `kaggle_competition_search` / `kaggle_discussion_search` 两个已删除条目
  - `_collect_data_files(work_root: Path, max_depth=4) -> dict[Path, list[Path]]`：递归收集数据文件，跳过工具输出目录与 `result.json`
  - `_scan_work_dir` 改用递归收集；一级目录输出格式保持不变（嵌套目录用相对路径）

**背景：** MCP 下载落在 `{work_root}/mcp/{server}/{tool}/`（三层），而原 `_scan_work_dir` 只扫 work_root 下一层，导致 data_analyze 看不到下载的数据。此任务修复该缺口。

- [ ] **Step 1: 写失败测试**

追加到 `test/unit/tools/test_data_prepare.py`：

```python
from athena.tools.data_prepare import _collect_data_files, _TOOL_OUTPUT_DIRS


def test_tool_output_dirs_removed_kaggle_stubs():
    assert "kaggle_competition_search" not in _TOOL_OUTPUT_DIRS
    assert "kaggle_discussion_search" not in _TOOL_OUTPUT_DIRS


def test_collect_data_files_recurses_into_mcp(tmp_path):
    data_dir = tmp_path / "mcp" / "kaggle" / "download_competition_data_file" / "files"
    data_dir.mkdir(parents=True)
    (data_dir / "train.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "mcp" / "kaggle" / "download_competition_data_file" / "result.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "data_analyze" / "eda_report.json").write_text("{}", encoding="utf-8")

    collected = _collect_data_files(tmp_path)
    all_files = [f for files in collected.values() for f in files]
    names = [f.name for f in all_files]
    # 递归找到深层数据文件
    assert "train.csv" in names
    # 跳过 MCP 产物与工具输出目录
    assert "result.json" not in names
    assert "eda_report.json" not in names
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest test/unit/tools/test_data_prepare.py -v`
Expected: FAIL —— `ImportError: cannot import name '_collect_data_files'`

- [ ] **Step 3: 最小实现**

修改 `data_prepare.py`：

```python
_TOOL_OUTPUT_DIRS = {
    "data_analyze", "data_clean_code_gen", "solution_design",
    "project_code_gen", "code_execute", "submission_build",
    "hf_dataset_search", "hf_model_search",
}

_ARTIFACT_FILES = {"result.json"}
"""扫描数据时跳过的工具产物文件名（MCP 统一落盘结果）。"""
```

新增递归收集函数（放在 `_scan_work_dir` 之前）：

```python
def _collect_data_files(work_root: Path, max_depth: int = 4) -> dict[Path, list[Path]]:
    """递归收集 work_root 下的数据文件，跳过工具输出目录与产物文件。

    Returns:
        {目录: [该目录下的数据文件]}。
    """
    collected: dict[Path, list[Path]] = {}

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        files = [
            f for f in sorted(d.iterdir())
            if f.is_file() and f.name not in _ARTIFACT_FILES
        ]
        if files:
            collected[d] = files
        for child in sorted(d.iterdir()):
            if child.is_dir() and child.name not in _TOOL_OUTPUT_DIRS:
                _walk(child, depth + 1)

    _walk(work_root, 0)
    return collected
```

替换 `_scan_work_dir` 主体（**逐字保留原 samples 格式与结尾守卫**，只改收集与汇总逻辑）：

```python
def _scan_work_dir(work_root: Path, max_samples: int = 8) -> tuple[str, str]:
    """Scan work_root for actual data files (recursively, excluding tool outputs).

    Returns:
        (tree_summary, samples_text) — both empty strings if no data found.
    """
    lines: list[str] = []
    samples: list[str] = []
    sample_count = 0

    # 递归收集数据文件（跳过工具输出目录与 MCP 产物）
    dirs = _collect_data_files(work_root)

    if not dirs:
        return "", ""

    for d, files in sorted(dirs.items()):
        if d == work_root:
            rel = "(root)"
        else:
            rel = str(d.relative_to(work_root))
        label = f"{rel}/ ({len(files)} files)"
        lines.append(label)

        for f in files:
            size_kb = f.stat().st_size / 1024
            lines.append(f"  {f.name}  ({size_kb:.1f} KB)")

            if sample_count < max_samples:
                sample = _read_sample(f)
                samples.append(f"--- {f.name} (in {rel}/) ---\n{sample}\n")
                sample_count += 1

    if not any(line.startswith("  ") for line in lines):
        return "", ""

    return "\n".join(lines), "\n".join(samples)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest test/unit/tools/test_data_prepare.py -v`
Expected: PASS（新增 2 个测试通过，原 4 个测试不受影响）

- [ ] **Step 5: 提交**

```bash
git add src/athena/tools/data_prepare.py test/unit/tools/test_data_prepare.py
git commit -m "fix: data_prepare 扫描器递归支持 MCP 嵌套下载目录"
```

---

### Task 8: Kaggle 真连冒烟测试 + 配置示例

**Files:**
- Create: `test/integration/test_mcp_kaggle_smoke.py`
- Create: `mcp_servers.json.example`

**Interfaces:**
- Consumes: Task 3 `McpClientManager` / `McpServerConfig`
- Produces: 门控的端到端验证（`KAGGLE_API_TOKEN` 存在才跑）；零代码接入示例文件

- [ ] **Step 1: 写测试**

```python
# test/integration/test_mcp_kaggle_smoke.py
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
```

- [ ] **Step 2: 运行测试确认跳过（无 token 时）**

Run: `uv run pytest test/integration/test_mcp_kaggle_smoke.py -v`
Expected: SKIPPED（当前有 token，则真实连接并打印工具列表）

- [ ] **Step 3: 创建配置示例**

```json
# mcp_servers.json.example
{
  "servers": [
    {
      "name": "kaggle",
      "transport": "streamable_http",
      "url": "https://www.kaggle.com/mcp",
      "auth": { "type": "bearer", "env": "KAGGLE_API_TOKEN" },
      "tool_prefix": "kaggle__",
      "pinned_tools": []
    }
  ]
}
```

使用方式：复制为 `mcp_servers.json` 放在工作目录即可零代码接入；token 从 `.env` 的 `KAGGLE_API_TOKEN` 读取。

- [ ] **Step 4: 运行全量测试确认通过**

Run: `uv run pytest -q`
Expected: 全量通过（冒烟测试按环境门控）

- [ ] **Step 5: 提交**

```bash
git add test/integration/test_mcp_kaggle_smoke.py mcp_servers.json.example
git commit -m "test: Kaggle MCP 真连冒烟测试（token 门控）+ 零代码接入配置示例"
```

---

## 收尾检查

完成全部任务后，跑一次 `uv run pytest -q`，并确认：
- 删除 `kaggle_search.py` 后无残留引用（`git grep -n kaggle_search src/ test/` 应只在 docs 中命中）；
- `build_task_understand_agent` 全部调用点已加 `await`（`git grep -n "build_task_understand_agent"` 检查 demo 与测试）；
- 新增 `mcp` 子包遵循代码规范（导入顺序、docstring、无装饰分隔线、异常注释）。
