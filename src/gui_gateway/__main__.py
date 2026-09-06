"""Athena GUI WebSocket gateway 的入口点。

Tauri 桌面应用启动此 Python 进程并从 stdout 的第一行读取端口号。

用法：
    python -m gui_gateway

提供 ``start_server(test_mode=False)`` 以支持可测试性。
"""

import asyncio
import importlib
import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from athena.core.agent import settings
from athena.research import ResearchRuntime
from athena.research.config import (
    ProviderConfig,
    ResearchOptions,
    ResearchPolicy,
    RuntimeDependencies,
    SessionConfig,
    TaskConfig,
)
from gui_gateway.controller import ControllerFactory, local_controller_capabilities
from gui_gateway.handler import GuiRequestHandler
from gui_gateway.human import HumanRequestBroker
from gui_gateway.state_store import GuiStateStore
from gui_gateway.transport import WebSocketTransport

RuntimeFactory = Callable[..., ResearchRuntime]


def _factory_accepts(factory: RuntimeFactory, keyword: str) -> bool:
    """Preserve custom test/embedding factories that predate a new option."""
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _controller_factory_from_environment() -> ControllerFactory | None:
    """Load the host-owned factory named by ``ATHENA_CONTROLLER_FACTORY``.

    The value is only a Python import reference (`package.module:factory`), not
    credentials. The imported host module owns credentials and its external
    authority client; agent tools and GUI requests never receive either.
    """
    reference = os.environ.get("ATHENA_CONTROLLER_FACTORY", "").strip()
    if not reference:
        return None
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise RuntimeError("ATHENA_CONTROLLER_FACTORY must be package.module:callable")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise TypeError("ATHENA_CONTROLLER_FACTORY must resolve to a callable")
    return factory


def _make_runtime(
    project_root: str | None = None,
    state_root: Path | None = None,
    ask_user: Any = None,
    session_id: str = "default",
    broker: Any = None,
    skip_validate: bool = False,
    controller_factory: ControllerFactory | None = None,
) -> ResearchRuntime:
    """按选定项目目录构造 research runtime；None 时沿用进程工作目录。

    ``auto_validate=True`` 使 SEARCH 结束后自动进入 VALIDATE 并发布最终报告
    （与 TUI/CLI/headless 入口一致），避免阶段机停在 WAITING 不再推进。
    GUI 路径显式启用确认门并关闭自动确认：raw task 文本必须先经过
    ``task_clarification_start`` 与 ``start_search(draft_id, revision)``。
    """
    root = Path(project_root or ".").resolve()
    capabilities = (
        controller_factory(root, session_id)
        if controller_factory is not None
        else local_controller_capabilities(root, session_id)
    )
    return ResearchRuntime(
        project_root=project_root,
        session=SessionConfig(state_root=state_root, session_id=session_id),
        research=ResearchOptions(
            task=TaskConfig(confirmation_gate=True, ask_user=ask_user),
            policy=ResearchPolicy(auto_validate=True, skip_validate=skip_validate),
        ),
        dependencies=RuntimeDependencies(
            provider=ProviderConfig(model=settings.model_name()),
            broker=broker,
            baseline_authority=capabilities.baseline_authority,
            environment_repair_actions=dict(capabilities.repair_actions),
        ),
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
    make_runtime: RuntimeFactory = _make_runtime,
    port: int | None = None,
    controller_factory: ControllerFactory | None = None,
) -> tuple[Any, int]:
    """启动 WebSocket 服务器并返回 (server, port) 元组。

    Args:
        test_mode: 如果为 True，则不向 stdout 打印端口号。
        make_runtime: 项目/session runtime factory; it receives the validated
            ``skip_validate`` keyword alongside the existing keyword options.
        port: 固定端口；None 时读 ``ATHENA_GUI_PORT`` 环境变量，再回退随机端口。
    Returns:
        ``(websockets.WebSocketServer, port)`` 元组。
    """
    controller_factory = controller_factory or _controller_factory_from_environment()
    broker = HumanRequestBroker()
    state_store = GuiStateStore()

    def _factory(
        project_root: str | None = None, state_root: Path | None = None
    ) -> ResearchRuntime:
        session_id = state_root.name if state_root is not None else "default"
        stored = state_store.load()
        controller_options = (
            {"controller_factory": controller_factory}
            if controller_factory is not None
            and _factory_accepts(make_runtime, "controller_factory")
            else {}
        )
        runtime = make_runtime(
            project_root,
            state_root,
            ask_user=broker.ask,
            session_id=session_id,
            broker=broker,
            skip_validate=stored.skip_validate_for(project_root),
            **controller_options,
        )
        broker.bind(session_id, "runtime", "runtime")
        return runtime

    stored_root = state_store.load().active_project_root
    handler = GuiRequestHandler(
        runtime or _factory(stored_root),
        _factory,
        broker,
        state_store=state_store,
    )
    transport = WebSocketTransport(handler)
    chosen_port = port if port is not None else _fixed_port()
    server = await transport.serve("127.0.0.1", chosen_port)
    bound_port: int = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    if not test_mode:
        # Tauri 读取 stdout 第一行以获取端口号
        print(bound_port, flush=True)
    return server, bound_port


async def main() -> None:
    """主入口点 — 启动服务器并持续运行。"""
    server, port = await start_server()
    print(f"Athena GUI gateway listening on ws://127.0.0.1:{port}", flush=True)
    try:
        await asyncio.Future()  # 持续运行
    except KeyboardInterrupt:
        # 用户按下 Ctrl+C，优雅关闭服务器
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
