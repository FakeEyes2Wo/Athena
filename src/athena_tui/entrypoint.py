"""entrypoint.py：参数解析、runtime 构造、非 TTY 检测与关闭。

``Athena-tui`` 命令与 ``python -m athena_tui`` 都从这里进入。非交互式终端不尝试
降级伪 TUI，而是给出指向 Athena-cli 的简明提示。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from athena.core.agent import settings
from athena.research import ResearchRuntime
from athena_tui.app import AthenaApp

# 非 TTY 时给自动化的提示。
_AUTOMATION_HINT = (
    "Athena TUI 需要交互式终端。自动化请使用 Athena-cli：\n"
    '  uv run Athena-cli run --project {project} --task "..." --data ...'
)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="Athena-tui")
    parser.add_argument("--project", default=".athena/tui-run", help="项目根目录")
    return parser.parse_args(argv)


def _is_interactive() -> bool:
    """终端可用：stdin 与 stdout 都是 TTY。"""
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


async def _run(args: argparse.Namespace) -> int:
    runtime = ResearchRuntime(
        project_root=Path(args.project),
        model=settings.model_name(),
        auto_seed_task=True,
    )
    app = AthenaApp(runtime, Path(args.project))
    # 不自动 start：第一条 Human 消息经 auto_seed_task 自动 seed 为 research task
    # 并从 PREPARE 启动，避免无任务直接进入 SEARCH（无 baseline/SOTA）。
    return await app.run()


def main(argv: list[str] | None = None) -> int:
    """Athena-tui 入口：解析参数并运行全屏 TUI。"""
    args = _parse(sys.argv[1:] if argv is None else argv)
    if not _is_interactive():
        print(_AUTOMATION_HINT.format(project=args.project), file=sys.stderr)
        return 1
    return asyncio.run(_run(args))
