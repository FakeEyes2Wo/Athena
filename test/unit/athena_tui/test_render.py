"""Tests for codex-inspired responsive TUI rendering."""

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.utils import get_cwidth

from athena_tui.render import (
    HELP_TEXT,
    composer_height,
    render_bottom_pane,
    render_header,
    render_history,
    render_history_lines,
    render_status,
)
from athena_tui.state import (
    HistoryEntry,
    TuiState,
    close_overlay,
    open_confirmation,
    open_overlay,
)


def plain(value) -> str:
    return fragment_list_to_text(value) if not isinstance(value, str) else value


def test_header_prioritizes_product_phase_and_status_at_narrow_width() -> None:
    state = TuiState(
        project_root="C:/very/long/project/path/athena-experiment",
        phase="SEARCH",
        status="RUNNING",
    )

    wide = plain(render_header(state, 100))
    narrow = plain(render_header(state, 40))

    assert "Athena" in wide
    assert "C:/very/long/project/path/athena-experiment" in wide
    assert "SEARCH" in narrow
    assert "RUNNING" in narrow
    assert get_cwidth(narrow) <= 40


def test_history_renders_user_runtime_tool_and_error_with_distinct_markers() -> None:
    state = TuiState(
        history=(
            HistoryEntry(kind="user", text="try a forest"),
            HistoryEntry(kind="runtime", source="agent", text="working"),
            HistoryEntry(kind="runtime", source="tool", channel="stdout", text="0.84"),
            HistoryEntry(kind="runtime", source="tool", channel="error", text="failed"),
        )
    )

    output = plain(render_history(state, 50))

    assert "› try a forest" in output
    assert "● working" in output
    assert "tool · stdout" in output
    assert "! failed" in output


def test_consecutive_text_from_the_same_source_suppresses_repeat_marker() -> None:
    state = TuiState(
        history=(
            HistoryEntry(kind="runtime", source="agent", text="first"),
            HistoryEntry(kind="runtime", source="agent", text="second"),
        )
    )

    output = plain(render_history(state, 50))

    assert output.count("●") == 1
    assert "  second" in output


def test_wrapping_preserves_styles_and_terminal_cell_width() -> None:
    state = TuiState(history=(HistoryEntry(kind="user", text="中文中文中文"),))

    lines = render_history_lines(state, 8)

    assert len(lines) >= 2
    assert all(get_cwidth(fragment_list_to_text(line)) <= 8 for line in lines)
    assert any(style == "class:history.user.marker" for style, _ in lines[0])
    assert any(style == "class:history.user" for line in lines for style, _ in line)


def test_dynamic_text_is_not_interpreted_as_style_markup() -> None:
    state = TuiState(
        history=(HistoryEntry(kind="runtime", source="agent", text="[red]literal"),)
    )

    output = render_history(state, 40)

    assert "[red]literal" in fragment_list_to_text(output)


@pytest.mark.parametrize(("count", "expected_titles"), [(1, 1), (2, 2), (3, 3)])
def test_ideator_debate_uses_exact_actual_lane_count(count, expected_titles) -> None:
    state = TuiState(
        history=tuple(
            HistoryEntry(
                kind="runtime",
                source="agent",
                plan=f"ideator-{index}",
                text=f"proposal {index}",
            )
            for index in range(1, count + 1)
        )
    )

    lines = render_history_lines(state, 120)
    output = "\n".join(plain(line) for line in lines)

    assert output.count("Ideator ") == expected_titles
    for index in range(1, count + 1):
        assert f"Ideator {index}" in output
        assert f"proposal {index}" in output
    assert all(get_cwidth(plain(line)) <= 120 for line in lines)


def test_ideator_debate_caps_display_at_first_three_lanes() -> None:
    state = TuiState(
        history=tuple(
            HistoryEntry(
                kind="runtime",
                source="agent",
                plan=f"ideator-{index}",
                text=f"proposal {index}",
            )
            for index in range(1, 5)
        )
    )

    output = "\n".join(plain(line) for line in render_history_lines(state, 120))

    assert output.count("Ideator ") == 3
    assert "Ideator 1" in output
    assert "Ideator 2" in output
    assert "Ideator 3" in output
    assert "Ideator 4" not in output
    assert "proposal 4" not in output


def test_narrow_ideator_debate_stacks_actual_lanes_and_keeps_ordinary_output() -> None:
    state = TuiState(
        history=(
            HistoryEntry(kind="runtime", source="supervisor", text="keep ordinary"),
            HistoryEntry(
                kind="runtime",
                source="agent",
                plan="ideator-1",
                text="first proposal",
            ),
            HistoryEntry(
                kind="runtime",
                source="agent",
                plan="ideator-2",
                text="second proposal",
            ),
        )
    )

    lines = render_history_lines(state, 40)
    output = "\n".join(plain(line) for line in lines)

    assert output.index("keep ordinary") < output.index("Ideator 1")
    assert output.index("Ideator 1") < output.index("Ideator 2")
    assert output.count("Ideator ") == 2
    assert all(get_cwidth(plain(line)) <= 40 for line in lines)


def test_output_after_debate_remains_after_board_in_history_order() -> None:
    state = TuiState(
        history=(
            HistoryEntry(kind="runtime", source="supervisor", text="before"),
            HistoryEntry(
                kind="runtime",
                source="agent",
                plan="ideator-1",
                text="proposal",
            ),
            HistoryEntry(kind="runtime", source="supervisor", text="after"),
        )
    )

    output = "\n".join(plain(line) for line in render_history_lines(state, 80))

    assert output.index("before") < output.index("Ideator 1")
    assert output.index("Ideator 1") < output.index("after")


def test_separate_debate_rounds_render_as_separate_boards() -> None:
    state = TuiState(
        history=(
            HistoryEntry(
                kind="runtime", source="agent", plan="ideator-1", text="round one"
            ),
            HistoryEntry(kind="runtime", source="supervisor", text="between"),
            HistoryEntry(
                kind="runtime", source="agent", plan="ideator-1", text="round two"
            ),
        )
    )

    output = "\n".join(plain(line) for line in render_history_lines(state, 80))

    assert output.count("Ideator 1") == 2
    assert output.index("round one") < output.index("between")
    assert output.index("between") < output.index("round two")


def test_status_prioritizes_error_waiting_and_unseen_output() -> None:
    assert "boom" in plain(render_status(TuiState(last_error="boom"), 50))
    assert "需要指导" in plain(
        render_status(TuiState(status="WAITING", waiting={"reason": "需要指导"}), 50)
    )
    assert "3 条新输出" in plain(
        render_status(TuiState(history_follow_tail=False, unseen_output_count=3), 50)
    )


def test_wide_status_shows_search_and_sota_but_narrow_status_keeps_core_state() -> None:
    state = TuiState(
        phase="SEARCH",
        status="RUNNING",
        search={"attempts": 3, "limit": 10, "successes": 2, "concurrency": 4},
        sota={"metric": 0.8421},
    )

    wide = plain(render_status(state, 100))
    narrow = plain(render_status(state, 38))

    assert "3/10" in wide
    assert "2 successful" in wide
    assert "4 workers" in wide
    assert "SOTA 0.8421" in wide
    assert "SEARCH" in narrow
    assert "RUNNING" in narrow
    assert get_cwidth(narrow) <= 38


@pytest.mark.parametrize(
    ("text", "width", "expected"),
    [
        ("", 80, 1),
        ("one line", 80, 1),
        ("one\ntwo\nthree", 80, 3),
        ("x" * 500, 20, 6),
    ],
)
def test_composer_height_is_bounded_by_content(text, width, expected) -> None:
    assert composer_height(text, width) == expected


def test_bottom_pane_renders_composer_help_overlay_and_confirmation() -> None:
    composer = plain(render_bottom_pane(TuiState(), 80))
    assert "Enter 发送" in composer
    assert "Shift+Enter 换行" in composer
    assert "Ctrl+J" in composer

    help_state = open_overlay(TuiState(composer="keep"), HELP_TEXT)
    help_output = plain(render_bottom_pane(help_state, 80))
    assert "输入" in help_output
    assert "导航" in help_output
    assert "控制" in help_output
    assert close_overlay(help_state).composer == "keep"

    confirmation = open_confirmation(TuiState(), "停止当前研究执行？")
    confirm_output = plain(render_bottom_pane(confirmation, 80))
    assert "停止当前研究执行？" in confirm_output
    assert "Y 确认" in confirm_output
    assert "N/Esc 取消" in confirm_output
