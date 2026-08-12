"""Tests for the convenience ``src/main.py`` CLI wrapper."""

import importlib.util
from pathlib import Path

import pytest

_MAIN = Path(__file__).resolve().parents[2] / "src" / "main.py"


def _load_main():
    spec = importlib.util.spec_from_file_location("athena_main", _MAIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_args_defaults_to_titanic_run() -> None:
    mod = _load_main()
    argv = mod._run_argv([])
    assert argv[0] == "run"
    assert "--project" in argv
    assert "examples/titanic" in argv
    assert mod._DEFAULT_TASK in argv


def test_user_run_arguments_are_preserved() -> None:
    mod = _load_main()
    argv = mod._run_argv(
        ["run", "--project", "p", "--data", "mydata", "--task", "mine"]
    )
    assert argv == [
        "run",
        "--project",
        "p",
        "--data",
        "mydata",
        "--task",
        "mine",
    ]


@pytest.mark.parametrize("command", ["status", "pause", "resume", "stop"])
def test_control_commands_pass_through(command: str) -> None:
    mod = _load_main()
    argv = [command, "--project", "p"]
    assert mod._run_argv(argv) == argv


def test_help_passes_through() -> None:
    mod = _load_main()
    assert mod._run_argv(["--help"]) == ["--help"]


@pytest.mark.asyncio
async def test_all_commands_delegate_to_the_shared_cli(monkeypatch) -> None:
    mod = _load_main()
    seen = []

    async def fake_cli(args):
        seen.append(args.command)
        return 42

    monkeypatch.setattr(mod, "_cli_dispatch", fake_cli)
    args = mod._build_parser().parse_args(["run", "--project", "p", "--data", "d"])
    assert await mod._dispatch_command(args) == 42
    assert seen == ["run"]
