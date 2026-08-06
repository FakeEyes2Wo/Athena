"""Athena GUI WebSocket gateway 的入口点。

Tauri 桌面应用启动此 Python 进程并从 stdout 的第一行读取端口号。

用法：
    python -m gui_gateway

提供 ``start_server(test_mode=False)`` 以支持可测试性。
"""

import asyncio
from typing import Any

from gui_gateway.handler import GuiRequestHandler
from gui_gateway.transport import WebSocketTransport
from athena.research import ResearchRuntime


async def start_server(
    test_mode: bool = False,
    runtime: ResearchRuntime | None = None,
) -> tuple[Any, int]:
    """启动 WebSocket 服务器并返回 (server, port) 元组。

    Args:
        test_mode: 如果为 True，则不向 stdout 打印端口号。

    Returns:
        ``(websockets.WebSocketServer, port)`` 元组。
    """
    handler = GuiRequestHandler(runtime or ResearchRuntime())
    transport = WebSocketTransport(handler)
    server = await transport.serve("127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    if not test_mode:
        # Tauri 读取 stdout 第一行以获取端口号
        print(port, flush=True)
    return server, port


async def main() -> None:
    """主入口点 — 启动服务器并持续运行。"""
    runtime = ResearchRuntime()
    server, _ = await start_server(test_mode=False, runtime=runtime)
    try:
        await asyncio.Future()  # 持续运行
    except KeyboardInterrupt:
        # 用户按下 Ctrl+C，优雅关闭服务器
        server.close()
        await server.wait_closed()
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
