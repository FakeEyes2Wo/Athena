"""Tests for the single-composer prompt-toolkit application."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import Frame

from athena.research.supervisor.events import OutputEvent, StateEvent
from athena_tui.app import AthenaApp, apply_event
from athena_tui.state import COMPOSER, CONFIRMATION, HistoryEntry, TuiState


class FakeRuntime:
    def __init__(self) -> None:
        self.callback = None
        self.messages: list[str] = []
        self.closed = False

    def subscribe(self, callback) -> str:
        self.callback = callback
        callback(
            "state",
            StateEvent(
                status="RUNNING",
                phase="SEARCH",
                plans=[],
                search={"attempts": 0, "limit": 10, "successes": 0, "concurrency": 4},
                sota=None,
                waiting=None,
            ).model_dump(mode="json"),
        )
        return "sub_1"

    def unsubscribe(self, _subscription_id: str) -> None:
        self.callback = None

    async def message(self, text: str) -> str:
        self.messages.append(text)
        return "ok"

    async def aclose(self) -> None:
        self.closed = True


class BlockingRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.received = asyncio.Event()
        self.release = asyncio.Event()

    async def message(self, text: str) -> str:
        self.messages.append(text)
        self.received.set()
        await self.release.wait()
        return "ok"


class StubComposer:
    def __init__(self, text: str, cursor_position: int | None = None) -> None:
        cursor = len(text) if cursor_position is None else cursor_position
        self.buffer = Buffer(document=Document(text, cursor))

    @property
    def text(self) -> str:
        return self.buffer.text

    @text.setter
    def text(self, value: str) -> None:
        self.buffer.set_document(Document(value, len(value)), bypass_readonly=True)


class StubPromptApp:
    def __init__(self) -> None:
        self.result = None

    def exit(self, result: int = 0) -> None:
        self.result = result


def test_apply_event_accepts_only_output_and_state() -> None:
    state = apply_event(
        TuiState(),
        OutputEvent(seq=1, source="supervisor", channel="text", text="hello"),
    )
    state = apply_event(
        state,
        StateEvent(
            status="WAITING",
            phase="SEARCH",
            plans=[],
            search={"attempts": 1, "limit": 10, "successes": 0, "concurrency": 4},
            sota=None,
            waiting={"reason": "guidance"},
        ),
    )
    assert state.history == (
        HistoryEntry(
            kind="runtime",
            source="supervisor",
            channel="text",
            text="hello",
        ),
    )
    assert state.status == "WAITING"


@pytest.mark.asyncio
async def test_plain_text_uses_single_runtime_message_surface() -> None:
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    app._composer = StubComposer("continue")

    await app.handle_key("enter")

    assert runtime.messages == ["continue"]
    assert app._composer.text == ""
    assert app.state.history == (HistoryEntry(kind="user", text="continue"),)


@pytest.mark.asyncio
async def test_submit_is_visible_while_supervisor_turn_is_running() -> None:
    runtime = BlockingRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    app._composer = StubComposer("continue")

    submitting = asyncio.create_task(app.handle_key("enter"))
    await runtime.received.wait()

    assert app._composer.text == ""
    assert app.state.history == (HistoryEntry(kind="user", text="continue"),)

    runtime.release.set()
    await submitting


@pytest.mark.asyncio
async def test_real_keys_submit_cancel_confirmation_and_quit() -> None:
    runtime = FakeRuntime()

    async def wait_until(predicate) -> None:
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.01)

    with create_pipe_input() as pipe_input:
        app = AthenaApp(
            runtime,
            Path("/tmp"),
            input=pipe_input,
            output=DummyOutput(),
        )

        async def drive() -> None:
            pipe_input.send_text("continue\r")
            await wait_until(lambda: runtime.messages == ["continue"])
            pipe_input.send_text("/stop\r")
            await wait_until(lambda: app.state.mode == CONFIRMATION)
            pipe_input.send_text("n")
            await wait_until(lambda: app.state.mode == COMPOSER)
            pipe_input.send_text("\x03")
            await wait_until(lambda: app.state.mode == CONFIRMATION)
            pipe_input.send_text("y")

        driver = asyncio.create_task(drive())
        code, _ = await asyncio.wait_for(
            asyncio.gather(app.run(), driver),
            timeout=5,
        )

    assert code == 0
    assert runtime.messages == ["continue"]
    assert app.state.mode == COMPOSER
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_real_shift_enter_and_ctrl_j_insert_newlines_before_submit() -> None:
    runtime = FakeRuntime()

    async def wait_until(predicate) -> None:
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.01)

    with create_pipe_input() as pipe_input:
        app = AthenaApp(
            runtime,
            Path("/tmp"),
            input=pipe_input,
            output=DummyOutput(),
        )

        async def drive() -> None:
            await wait_until(lambda: app._composer is not None)
            pipe_input.send_text("line one")
            await wait_until(lambda: app._composer.text == "line one")
            pipe_input.send_text("\x1b[27;2;13~")
            await wait_until(lambda: app._composer.text == "line one\n")
            pipe_input.send_text("line two")
            await wait_until(lambda: app._composer.text == "line one\nline two")
            pipe_input.send_text("\n")
            await wait_until(lambda: app._composer.text == "line one\nline two\n")
            pipe_input.send_text("line three\r")
            await wait_until(lambda: runtime.messages)
            pipe_input.send_text("\x03")
            await wait_until(lambda: app.state.mode == CONFIRMATION)
            pipe_input.send_text("y")

        code, _ = await asyncio.wait_for(
            asyncio.gather(app.run(), asyncio.create_task(drive())), timeout=5
        )

    assert code == 0
    assert runtime.messages == ["line one\nline two\nline three"]


@pytest.mark.asyncio
async def test_modal_keys_do_not_change_composer_draft() -> None:
    runtime = FakeRuntime()

    async def wait_until(predicate) -> None:
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.01)

    with create_pipe_input() as pipe_input:
        app = AthenaApp(
            runtime,
            Path("/tmp"),
            input=pipe_input,
            output=DummyOutput(),
        )

        async def drive() -> None:
            pipe_input.send_text("?")
            await wait_until(lambda: app.state.overlay is not None)
            pipe_input.send_text("ignored")
            await asyncio.sleep(0.05)
            assert app._composer.text == ""
            pipe_input.send_text("\x1b")
            await wait_until(lambda: app.state.overlay is None)
            pipe_input.send_text("/stop\r")
            await wait_until(lambda: app.state.mode == CONFIRMATION)
            pipe_input.send_text("ignored")
            await asyncio.sleep(0.05)
            assert app._composer.text == ""
            pipe_input.send_text("n")
            await wait_until(lambda: app.state.mode == COMPOSER)
            pipe_input.send_text("\x03")
            await wait_until(lambda: app.state.mode == CONFIRMATION)
            pipe_input.send_text("y")

        await asyncio.wait_for(
            asyncio.gather(app.run(), asyncio.create_task(drive())), timeout=5
        )

    assert runtime.messages == []


@pytest.mark.asyncio
async def test_stop_requires_local_confirmation() -> None:
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    app._composer = StubComposer("/stop")

    await app.handle_key("enter")
    assert app.state.mode == CONFIRMATION
    assert runtime.messages == []

    await app.handle_key("y")
    assert app.state.mode == COMPOSER
    assert runtime.messages == ["/stop"]


@pytest.mark.asyncio
async def test_close_unsubscribes_and_closes_runtime() -> None:
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    await app._controller.aclose()
    assert runtime.callback is None
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_submit_moves_multiline_draft_to_upper_history_and_clears_composer() -> (
    None
):
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    app._composer = StubComposer("first line\nsecond line")

    await app.handle_key("enter")

    assert runtime.messages == ["first line\nsecond line"]
    assert app._composer.text == ""
    assert app.state.history[-1].kind == "user"
    assert app.state.history[-1].text == "first line\nsecond line"


def test_new_runtime_output_does_not_replace_existing_composer_draft() -> None:
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    app.state = replace(app.state, composer="keep draft")

    app._on_event(OutputEvent(seq=1, source="agent", channel="text", text="new output"))

    assert app.state.composer == "keep draft"
    assert app.state.history[-1].text == "new output"


@pytest.mark.asyncio
async def test_pageup_holds_visual_view_and_end_clears_unseen_output() -> None:
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"))

    for sequence in range(1, 30):
        app._on_event(
            OutputEvent(
                seq=sequence,
                source="supervisor",
                channel="text",
                text=f"line {sequence}",
            )
        )

    await app.handle_key("pageup")
    before = fragment_list_to_text(app._history_fragments())
    app._on_event(OutputEvent(seq=30, source="supervisor", channel="text", text="late"))

    assert app.state.history_follow_tail is False
    assert app.state.unseen_output_count == 1
    assert fragment_list_to_text(app._history_fragments()) == before
    await app.handle_key("end")
    assert app.state.history_follow_tail is True
    assert app.state.unseen_output_count == 0
    assert app._history_scroll == 0


@pytest.mark.asyncio
async def test_pagedown_reenables_follow_only_when_scroll_offset_reaches_zero() -> None:
    app = AthenaApp(FakeRuntime(), Path("/tmp"))
    for sequence in range(1, 50):
        app._on_event(
            OutputEvent(
                seq=sequence,
                source="supervisor",
                channel="text",
                text=f"line {sequence}",
            )
        )
    await app.handle_key("pageup")
    await app.handle_key("pageup")

    await app.handle_key("pagedown")
    assert app.state.history_follow_tail is False
    await app.handle_key("pagedown")
    assert app.state.history_follow_tail is True


def test_mouse_wheel_scrolls_only_output_viewport_and_resumes_tail() -> None:
    app = AthenaApp(
        FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
    )
    for sequence in range(1, 50):
        app._on_event(
            OutputEvent(
                seq=sequence,
                source="agent",
                channel="text",
                plan=f"ideator-{1 + sequence % 2}",
                text=f"line {sequence}\n",
            )
        )
    app._build()
    wheel_up = MouseEvent(
        position=Point(x=0, y=0),
        event_type=MouseEventType.SCROLL_UP,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )
    wheel_down = MouseEvent(
        position=Point(x=0, y=0),
        event_type=MouseEventType.SCROLL_DOWN,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )
    click = MouseEvent(
        position=Point(x=0, y=0),
        event_type=MouseEventType.MOUSE_DOWN,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )

    assert app._history_control.mouse_handler(wheel_up) is None
    assert app._history_scroll == 3
    assert app.state.history_follow_tail is False
    assert app._history_control.mouse_handler(click) is None
    assert app._history_scroll == 3
    assert app._history_control.mouse_handler(wheel_down) is None
    assert app._history_scroll == 0
    assert app.state.history_follow_tail is True
    assert app._app.mouse_support() is True
    assert app._composer.control is not app._history_control


def test_mouse_drag_selects_output_text_without_stealing_composer_focus() -> None:
    app = AthenaApp(
        FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
    )
    app._on_event(
        OutputEvent(seq=1, source="supervisor", channel="text", text="select me")
    )
    prompt_app = app._build()
    app._history_fragments()
    down = MouseEvent(
        position=Point(x=0, y=0),
        event_type=MouseEventType.MOUSE_DOWN,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )
    move = MouseEvent(
        position=Point(x=8, y=0),
        event_type=MouseEventType.MOUSE_MOVE,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )
    up = MouseEvent(
        position=Point(x=8, y=0),
        event_type=MouseEventType.MOUSE_UP,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )

    app._history_control.mouse_handler(down)
    app._history_control.mouse_handler(move)
    app._history_control.mouse_handler(up)
    selected = app._history_fragments()

    assert any("class:history.selection" in style for style, _text in selected)
    assert prompt_app.layout.current_control is app._composer.control


def test_streaming_output_preserves_selection_and_frozen_viewport() -> None:
    app = AthenaApp(
        FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
    )
    app._on_event(OutputEvent(seq=1, source="agent", channel="text", text="select me"))
    app._build()
    app._start_history_selection(Point(x=0, y=0))
    app._extend_history_selection(Point(x=8, y=0))
    anchor = app._history_selection_anchor
    cursor = app._history_selection_cursor

    app._on_event(OutputEvent(seq=2, source="agent", channel="text", text=" later"))

    assert app._history_selection_anchor == anchor
    assert app._history_selection_cursor == cursor
    assert app.state.history_follow_tail is False
    assert app.state.unseen_output_count == 1
    assert any(
        "class:history.selection" in style for style, _text in app._history_fragments()
    )


@pytest.mark.asyncio
async def test_ctrl_c_copies_selected_output_instead_of_requesting_quit() -> None:
    app = AthenaApp(
        FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
    )
    app._on_event(OutputEvent(seq=1, source="agent", channel="text", text="copy me"))
    prompt_app = app._build()
    prompt_app.clipboard = InMemoryClipboard()
    app._start_history_selection(Point(x=2, y=0))
    app._extend_history_selection(Point(x=6, y=0))

    await app.handle_key("c-c")

    assert prompt_app.clipboard.get_data().text == "copy"
    assert app.state.mode == COMPOSER


def test_composer_is_multiline_bounded_and_framed() -> None:
    app = AthenaApp(
        FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
    )
    prompt_app = app._build()

    assert app._composer.buffer.multiline() is True
    assert isinstance(app._composer_pane, Frame)
    assert app._composer_height() == 1
    app._composer.text = "1\n2\n3\n4\n5\n6\n7"
    assert app._composer_height() == 6
    assert prompt_app.layout.current_control is app._composer.control


@pytest.mark.asyncio
async def test_handle_key_inserts_newlines_without_submitting() -> None:
    runtime = FakeRuntime()
    app = AthenaApp(runtime, Path("/tmp"), input=DummyInput(), output=DummyOutput())
    app._build()
    app._composer.buffer.set_document(
        Document("line one", len("line one")), bypass_readonly=True
    )

    await app.handle_key("c-j")
    app._composer.buffer.insert_text("line two")
    await app.handle_key("s-enter")

    assert app._composer.text == "line one\nline two\n"
    assert runtime.messages == []


@pytest.mark.asyncio
async def test_question_mark_in_nonempty_draft_does_not_open_help() -> None:
    app = AthenaApp(
        FakeRuntime(), Path("/tmp"), input=DummyInput(), output=DummyOutput()
    )
    app._build()
    app._composer.text = "why"

    await app.handle_key("?")

    assert app.state.overlay is None
    assert app._composer.text == "why"


@pytest.mark.asyncio
async def test_send_failure_restores_exact_multiline_draft() -> None:
    class FailingRuntime(FakeRuntime):
        async def message(self, text: str) -> str:
            self.messages.append(text)
            raise RuntimeError("offline")

    runtime = FailingRuntime()
    app = AthenaApp(runtime, Path("/tmp"))
    draft = "first line\nsecond line"
    app._composer = StubComposer(draft, cursor_position=5)

    await app.handle_key("enter")

    assert runtime.messages == [draft]
    assert app._composer.text == draft
    assert app._composer.buffer.cursor_position == 5
    assert app.state.history[-1].text == draft
    assert "offline" in app.state.last_error
