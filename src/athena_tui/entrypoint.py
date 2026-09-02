"""Athena TUI argument parsing, runtime composition, and TTY checks."""

import argparse
import asyncio
import sys
from pathlib import Path

from athena.core.agent import settings
from athena.research import ResearchRuntime
from athena_tui.app import AthenaApp

_AUTOMATION_HINT = (
    "Athena TUI 需要交互式终端。自动化请使用 Athena-cli：\n"
    '  uv run Athena-cli run --project {project} --task "..." --data ...'
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("search limit must be at least 1")
    return parsed


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="Athena-tui")
    parser.add_argument("--project", default=".athena/tui-run", help="项目根目录")
    parser.add_argument("--search-limit", type=_positive_int, default=10)
    parser.add_argument("--validate", action="store_true", default=False)
    return parser.parse_args(argv)


def _is_interactive() -> bool:
    """Return whether stdin and stdout are both interactive terminals."""
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


async def _run(args: argparse.Namespace) -> int:
    runtime = ResearchRuntime(
        project_root=Path(args.project),
        model=settings.model_name(),
        auto_seed_task=True,
        search_limit=args.search_limit,
        auto_validate=args.validate,
        task_confirmation_gate=False,
        auto_confirm=True,
    )
    # Resume: a prior run with a trusted baseline/SOTA auto-recovers and
    # continues; otherwise the first Human message seeds the task.
    if runtime.tree.best_experiment_id() is not None:
        await runtime.start()
    app = AthenaApp(runtime, Path(args.project))
    return await app.run()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the full-screen TUI."""
    args = _parse(sys.argv[1:] if argv is None else argv)
    if not _is_interactive():
        print(_AUTOMATION_HINT.format(project=args.project), file=sys.stderr)
        return 1
    return asyncio.run(_run(args))
