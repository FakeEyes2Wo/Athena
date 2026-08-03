"""TUI 入口 — ``uv run python -m athena.tui``。

``--mock`` 不需要任何 API key，用内置 MockRunner 走完整的事件与渲染链路，
适合验证终端表现或在 CI 里冒烟。
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from athena.tui.app import AthenaTUI
from athena.tui.approvals import build_gate
from athena.tui.doctor import Doctor
from athena.tui.runner import (
    DEFAULT_PROFILE,
    AgentRuntime,
    MockRunner,
    default_profiles,
)
from athena.tui.session import TuiSession
from athena.tui.state import APPROVAL_MODES

DEFAULT_LOG_PATH = Path.home() / ".athena" / "tui.log"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def load_env() -> None:
    """先读 CWD 的 .env，再用项目根目录的 .env 兜底（先加载的优先）。

    从任意目录起 ``python -m athena.tui`` 都能拿到 ``OPENAI_API_KEY`` /
    ``OPENAI_BASE_URL`` / ``ATHENA_TUI_MODEL``。
    """
    load_dotenv()
    load_dotenv(PROJECT_ROOT / ".env")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m athena.tui", description="Athena 终端界面"
    )
    parser.add_argument(
        "--mock", action="store_true", help="用离线 MockRunner，不调用任何模型"
    )
    parser.add_argument("--model", default="", help="模型名，默认取 profile 的默认值")
    parser.add_argument(
        "--agent",
        default=DEFAULT_PROFILE,
        choices=sorted(default_profiles()),
        help="agent profile",
    )
    parser.add_argument(
        "--mode",
        default="ask",
        choices=list(APPROVAL_MODES),
        help="工具审批模式",
    )
    parser.add_argument(
        "--no-approval", action="store_true", help="关闭审批闸门（工具直接执行）"
    )
    parser.add_argument("--verbose", action="store_true", help="启动时显示原始事件流")
    parser.add_argument("--check", action="store_true", help="只做配置自检，不启动界面")
    parser.add_argument("--session", default="", help="自定义 session id")
    parser.add_argument(
        "--debug", action="store_true", help=f"把日志写到 {DEFAULT_LOG_PATH}"
    )
    return parser


def configure_logging(debug: bool) -> None:
    """日志必须落文件 —— stderr 会直接冲掉 TUI 的底部动态区。"""
    if not debug:
        logging.disable(logging.WARNING)
        return
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(DEFAULT_LOG_PATH),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    configure_logging(args.debug)

    if args.check:
        return await Doctor().run()

    agent_runtime = None
    if args.mock:
        runner = MockRunner()
    else:
        agent_runtime = AgentRuntime(profile=args.agent, model=args.model)
        runner = agent_runtime

    session = await TuiSession.create(runner, session_id=args.session or None)
    # 闸门需要 session 才能发 ServerRequest，session 又需要 runner —— 装配后回填
    if agent_runtime is not None and not args.no_approval:
        agent_runtime.gate = build_gate(session)
        agent_runtime.rebuild()

    tui = AthenaTUI(
        session,
        agent_runtime=agent_runtime,
        verbose=args.verbose,
        approval_mode=args.mode,
    )
    await tui.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except KeyboardInterrupt:
        # 事件循环外层收到 Ctrl+C（例如启动阶段）→ 静默退出
        return 130


if __name__ == "__main__":
    sys.exit(main())
