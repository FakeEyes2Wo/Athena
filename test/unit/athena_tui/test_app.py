"""Tests for the single-composer prompt-toolkit application."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

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
async def test_prompt_toolkit_enter_submits_text_before_buffer_reset() -> None:
    """Catch scheduling submission after prompt-toolkit clears its buffer."""
    runtime = FakeRuntime()
    app = AthenaApp(
        runtime,
        Path("/tmp"),
        input=DummyInput(),
        output=DummyOutput(),
    )
    prompt_app = app._build()
    app._composer.text = "continue"

    app._composer.buffer.validate_and_handle()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runtime.messages == ["continue"]
    await prompt_app.cancel_and_wait_for_background_tasks()


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
    assert app._history_control.mouse_handler(click) is NotImplemented
    assert app._history_scroll == 3
    assert app._history_control.mouse_handler(wheel_down) is None
    assert app._history_scroll == 0
    assert app.state.history_follow_tail is True
    assert app._app.mouse_support() is True
    assert app._composer.control is not app._history_control


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
