"""Athena GUI WebSocket gateway 的入口点。

Tauri 桌面应用启动此 Python 进程并从 stdout 的第一行读取端口号。

用法：
    python -m gui_gateway

提供 ``start_server(test_mode=False)`` 以支持可测试性。
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from gui_gateway.handler import GuiRequestHandler
from gui_gateway.human import HumanRequestBroker
from gui_gateway.state_store import GuiStateStore, validate_project_root
from gui_gateway.transport import WebSocketTransport
from athena.core.agent import settings
from athena.research import ResearchRuntime


def _make_runtime(
    project_root: str | None = None,
    state_root: Path | None = None,
    ask_user: Any = None,
    session_id: str = "default",
    broker: Any = None,
    skip_validate: bool = False,
) -> ResearchRuntime:
    """按选定项目目录构造 research runtime；None 时沿用进程工作目录。

    ``auto_validate=True`` 使 SEARCH 结束后自动进入 VALIDATE 并发布最终报告
    （与 TUI/CLI/headless 入口一致），避免阶段机停在 WAITING 不再推进。
    GUI 路径显式启用确认门并关闭自动确认：raw task 文本必须先经过
    ``task_clarification_start`` 与 ``start_search(draft_id, revision)``。
    """
    return ResearchRuntime(
        project_root=project_root,
        state_root=state_root,
        session_id=session_id,
        model=settings.model_name(),
        auto_validate=True,
        skip_validate=skip_validate,
        task_confirmation_gate=True,
        auto_confirm=False,
        ask_user=ask_user,
        broker=broker,
    )


DEFAULT_GUI_PORT = 17601


def _fixed_port() -> int:
    """读 ``ATHENA_GUI_PORT`` 作为固定端口。

    未设置时默认 ``17601``（与浏览器预览前端 ``ws-backend.ts`` 一致）；
    显式传 ``0`` 仍表示随机端口（Tauri 桌面端从 stdout 读端口时可用）。
    """
    raw = os.environ.get("ATHENA_GUI_PORT", "")
    try:
        return int(raw) if raw else DEFAULT_GUI_PORT
    except ValueError:
        return DEFAULT_GUI_PORT


async def start_server(
    test_mode: bool = False,
    runtime: ResearchRuntime | None = None,
    make_runtime: Any = _make_runtime,
    port: int | None = None,
) -> tuple[Any, int]:
    """启动 WebSocket 服务器并返回 (server, port) 元组。

    Args:
        test_mode: 如果为 True，则不向 stdout 打印端口号。
        make_runtime: 给定项目目录构造 runtime 的工厂（供 GUI 运行时切换项目）。
        port: 固定端口；None 时读 ``ATHENA_GUI_PORT`` 环境变量，再回退随机端口。

    Returns:
        ``(websockets.WebSocketServer, port)`` 元组。
    """
    broker = HumanRequestBroker()
    state_store = GuiStateStore()

    def factory(
        project_root: str | None = None, state_root: Path | None = None
    ) -> ResearchRuntime:
        session_id = state_root.name if state_root is not None else "default"
        stored = state_store.load()
        runtime = make_runtime(
            project_root,
            state_root,
            ask_user=broker.ask,
            session_id=session_id,
            broker=broker,
            skip_validate=stored.skip_validate_for(project_root),
        )
        broker.bind(session_id, "runtime", "runtime")
        return runtime

    stored_root = validate_project_root(state_store.load().active_project_root)
    handler = GuiRequestHandler(
        runtime or factory(stored_root),
        factory,
        broker,
        state_store=state_store,
    )
    transport = WebSocketTransport(handler)
    chosen_port = port if port is not None else _fixed_port()
    server = await transport.serve("127.0.0.1", chosen_port)
    port: int = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    if not test_mode:
        # Tauri 读取 stdout 第一行以获取端口号
        print(port, flush=True)
    return server, port


async def main() -> None:
    """主入口点 — 启动服务器并持续运行。"""
    server, port = await start_server(test_mode=False, port=_fixed_port())
    print(f"Athena GUI gateway listening on ws://127.0.0.1:{port}", flush=True)
    try:
        await asyncio.Future()  # 持续运行
    except KeyboardInterrupt:
        # 用户按下 Ctrl+C，优雅关闭服务器
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
