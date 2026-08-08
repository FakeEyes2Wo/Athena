"""MCP 客户端管理 —— 连接、工具清单缓存、call_tool 转发。"""

import asyncio
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
        """返回配置的 MCP 工具名前缀。"""
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
            read, write, *_ = await stack.enter_async_context(self._build_transport())
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
        """调用 MCP 工具，失败时重连并指数退避重试（最多 3 次）。"""
        await self.ensure_connected()
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return await self._session.call_tool(tool_name, arguments=arguments)
            except Exception as exc:
                last_exc = exc
                if attempt == 2:  # 最后一次尝试仍失败
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, 4.0)
                await self.close()
                await self.connect()
        raise McpClientError(
            f"调用工具 '{tool_name}' 失败（已重连重试）: {last_exc}"
        ) from last_exc

    def tool_defs(self) -> list[Any]:
        """已缓存的工具定义列表（name/description/inputSchema）。"""
        return list(self._manifest)

    async def close(self) -> None:
        """关闭连接并清空会话。

        吞掉 anyio/Python 3.14 兼容性导致的 teardown 异常
        （cancel scope / async generator cleanup），不影响功能。
        """
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except (RuntimeError, ExceptionGroup) as exc:
                # Python 3.14 + anyio 兼容性问题：
                # - "Attempted to exit cancel scope in a different task"
                # - "athrow(): asynchronous generator is already running"
                msg = str(exc)
                if "cancel scope" not in msg and "already running" not in msg:
                    raise
            except GeneratorExit:
                pass
            self._stack = None
        self._session = None
