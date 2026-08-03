"""转录渲染测试 — 渲染成字符串后断言，不需要真终端。"""

import pytest

from athena.tui.state import Block
from athena.tui.theme import ASCII_SYMBOLS, SPINNER_ASCII, Theme
from athena.tui.transcript import Transcript


@pytest.fixture
def plain() -> Transcript:
    theme = Theme(color=False, symbols=ASCII_SYMBOLS, spinner_frames=SPINNER_ASCII)
    return Transcript(theme, width=80)


def test_user_block_carries_marker(plain):
    out = plain.render(Block("user", {"text": "你好"}))
    assert out.startswith("> 你好")


def test_assistant_indents_continuation_lines(plain):
    out = plain.render(Block("assistant", {"text": "第一行\n\n第二段"}))
    lines = out.rstrip("\n").split("\n")
    assert lines[0].startswith("* ")
    assert all(line.startswith("  ") for line in lines[1:] if line)


def test_tool_begin_shows_args(plain):
    out = plain.render(
        Block("tool_begin", {"name": "read_file", "args": {"path": "a.py"}})
    )
    assert "read_file" in out and "path=a.py" in out


def test_tool_begin_flattens_multiline_args(plain):
    out = plain.render(Block("tool_begin", {"name": "run", "args": {"cmd": "a\nb"}}))
    assert "\n" not in out.rstrip("\n")


def test_tool_end_reports_duration_and_error(plain):
    out = plain.render(
        Block(
            "tool_end",
            {"name": "x", "ok": False, "error": "boom", "duration_ms": 1500},
        )
    )
    assert "1.5s" in out and "boom" in out


def test_tool_end_folds_long_preview(plain):
    preview = "\n".join(f"line{i}" for i in range(30))
    out = plain.render(Block("tool_end", {"name": "x", "ok": True, "preview": preview}))
    assert "另有 22 行" in out
    assert "line8" not in out


def test_turn_end_summarises(plain):
    out = plain.render(
        Block(
            "turn_end",
            {"status": "completed", "duration_s": 4.2, "tools": 2, "events": 30},
        )
    )
    assert "完成" in out and "4.2s" in out and "2 工具" in out


def test_turn_end_names_the_failure(plain):
    out = plain.render(
        Block(
            "turn_end",
            {
                "status": "failed",
                "duration_s": 1.0,
                "tools": 0,
                "events": 3,
                "exception_type": "RuntimeError",
            },
        )
    )
    assert "失败" in out and "RuntimeError" in out and "tui.log" in out


def test_no_trailing_padding(plain):
    out = plain.render(Block("assistant", {"text": "短句"}))
    for line in out.split("\n"):
        assert line == line.rstrip(" \t")


def test_control_block_renders_nothing(plain):
    assert plain.render(Block("control", {"action": "exit"})) == ""


def test_commit_writes_through_writer():
    sink = []
    theme = Theme(color=False, symbols=ASCII_SYMBOLS, spinner_frames=SPINNER_ASCII)
    transcript = Transcript(theme, writer=sink.append, width=60)
    transcript.commit([Block("notice", {"text": "hi", "level": "info"})])
    assert "hi" in "".join(sink)


def test_commit_ignores_empty_list():
    sink = []
    theme = Theme(color=False, symbols=ASCII_SYMBOLS, spinner_frames=SPINNER_ASCII)
    Transcript(theme, writer=sink.append, width=60).commit([])
    assert sink == []


def test_colour_theme_emits_ansi():
    theme = Theme(color=True, symbols=ASCII_SYMBOLS, spinner_frames=SPINNER_ASCII)
    out = Transcript(theme, width=60).render(Block("user", {"text": "hi"}))
    assert "\x1b[" in out
