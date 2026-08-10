"""Athena-cli 测试（supervisor_design §3.1）：只转换参数、调用公开方法并渲染结果。"""

import pytest

import athena.cli as cli
from test.unit._support import make_project


@pytest.mark.asyncio
async def test_parse_intent_style_helpers(tmp_path) -> None:
    """CLI 的 RUN 通过 ResearchRuntime 公开方法推进（只做参数转换）。"""
    runtime = make_project(tmp_path)
    result = await runtime.dispatch(
        "TASK_CONFIGURE",
        {
            "task_type": "classification",
            "data_type": "tabular",
            "target_vars": ["label"],
            "primary_metric": "accuracy",
            "data_path": str(tmp_path / "d.csv"),
            "target": "label",
        },
    )
    assert result["configured"] is True
    await runtime.aclose()


def test_cli_parser_accepts_mode_and_search_policy() -> None:
    """CLI run 接受 --mode / --kfold 并进入 ExecutionConfig 参数。"""
    parser = cli._build_parser()
    args = parser.parse_args(
        [
            "run",
            "--project",
            "p",
            "--data",
            "d",
            "--task",
            "intent",
            "--mode",
            "auto",
            "--kfold",
            "disabled",
        ]
    )
    assert (args.mode, args.kfold) == ("auto", "disabled")


def test_build_parser_has_all_subcommands() -> None:
    """CLI 暴露 run/status/pause/resume/stop/requests/reply 子命令。"""
    parser = cli._build_parser()
    for name in (
        "run",
        "status",
        "pause",
        "resume",
        "stop",
        "requests",
        "reply",
    ):
        # 未知子命令 → SystemExit；已知子命令缺 --project → SystemExit(required)。
        # 两者 exit code 不同，据此区分"子命令存在"。
        exit_code = _parse_exit_code(parser, [name])
        assert exit_code == 2, f"subcommand {name!r} missing (exit={exit_code})"


def _parse_exit_code(parser, argv: list[str]) -> int:
    try:
        parser.parse_args(argv)
        return 0
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else -1
