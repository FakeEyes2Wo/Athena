"""Convenience entrypoint for the shared Athena CLI."""

import asyncio
import sys
import tempfile

from athena.cli import _build_parser
from athena.cli import _dispatch_command as _cli_dispatch

_DEFAULT_DATA = "examples/titanic"
_DEFAULT_TASK = (
    "discover a valid supervised prediction task and build the best model "
    "supported by the data"
)
_COMMANDS = frozenset({"run", "status", "pause", "resume", "stop"})


def _run_argv(argv: list[str]) -> list[str]:
    """Inject the repository demonstration defaults only for ``run``."""
    if not argv or argv[0] not in _COMMANDS:
        if argv and argv[0] in {"-h", "--help"}:
            return argv
        argv = ["run", *argv]
    if argv[0] != "run":
        return argv

    def provided(name: str) -> bool:
        return any(arg == name or arg.startswith(f"{name}=") for arg in argv)

    extra: list[str] = []
    if not provided("--project"):
        extra += ["--project", tempfile.mkdtemp(prefix="athena-")]
    if not provided("--data"):
        extra += ["--data", _DEFAULT_DATA]
    if not provided("--task"):
        extra += ["--task", _DEFAULT_TASK]
    return argv + extra


async def _dispatch_command(args) -> int:
    return await _cli_dispatch(args)


def main(argv: list[str] | None = None) -> int:
    normalized = _run_argv(list(sys.argv[1:] if argv is None else argv))
    args = _build_parser().parse_args(normalized)
    return asyncio.run(_dispatch_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
