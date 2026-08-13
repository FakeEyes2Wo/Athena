"""Single-composer prompt-toolkit application for Athena research conversations."""

import asyncio
from dataclasses import replace

from prompt_toolkit.application import Application
from prompt_toolkit.clipboard import Clipboard, InMemoryClipboard
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
from prompt_toolkit.layout.containers import DynamicContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from athena.research.supervisor.events import OutputEvent, StateEvent
from athena_tui.controller import TuiController
from athena_tui.render import (
    HELP_TEXT,
    composer_height,
    render_bottom_pane,
    render_header,
    render_history_lines,
    render_status,
)
from athena_tui.state import (
    COMPOSER,
    CONFIRMATION,
    TuiState,
    append_user_message,
    apply_output,
    apply_snapshot,
    close_confirmation,
    close_overlay,
    open_confirmation,
    open_overlay,
    set_error,
    set_history_follow,
)

_DEFAULT_SIZE = (24, 80)
_FIXED_ROWS = 5
_SCROLL_STEP = 3
_SHIFT_ENTER_SEQUENCES = {"\x1b[27;2;13~"}
_STYLE = Style.from_dict(
    {
        "header.brand": "bold",
        "header.path": "#8b949e",
        "phase.running": "#2dd4bf bold",
        "phase.waiting": "#fbbf24 bold",
        "phase.done": "#22c55e bold",
        "phase.stopped": "#f87171 bold",
        "history.user.marker": "#7dd3fc bold",
        "history.user": "#7dd3fc",
        "history.supervisor.marker": "#fbbf24 bold",
        "history.supervisor": "#fbbf24",
        "history.agent.marker": "#2dd4bf bold",
        "history.agent": "#2dd4bf",
        "history.tool": "#22c55e",
        "history.error": "#f87171",
        "history.selection": "reverse",
        "history.ideator.title": "#7dd3fc bold",
        "history.ideator.separator": "#475569",
        "status.primary": "bold",
        "status.secondary": "#8b949e",
        "status.warning": "#fbbf24",
        "status.error": "#f87171",
        "status.unseen": "#7dd3fc",
        "scrollbar": "#475569",
        "composer": "",
        "text-area.prompt": "#7dd3fc bold",
        "frame.border": "#475569",
        "frame.label": "#8b949e",
        "composer-frame.focused frame.border": "#94a3b8",
        "confirmation": "#fbbf24 bold",
    }
)


def _join_lines(lines: tuple[StyleAndTextTuples, ...]) -> StyleAndTextTuples:
    output: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        if index:
            output.append(("", "\n"))
        output.extend(line)
    return output


def _create_system_clipboard() -> Clipboard:
    try:
        from prompt_toolkit.clipboard.pyperclip import (
            PyperclipClipboard,
        )  # 延迟导入避免循环依赖
    except ImportError:
        return InMemoryClipboard()
    return PyperclipClipboard()


class _HistoryControl(FormattedTextControl):
    """History text with selection, wheel, and scrollbar mouse handling."""

    def __init__(self, app: "AthenaApp") -> None:
        super().__init__(app._history_fragments)
        self._app = app
        self._dragging_scrollbar = False
        self._selecting = False

    def mouse_handler(self, mouse_event):
        """Handle scroll/selection mouse events on the history view."""
        app = self._app
        et = mouse_event.event_type
        if et == MouseEventType.SCROLL_UP:
            app._scroll_history_up()
            return None
        if et == MouseEventType.SCROLL_DOWN:
            app._scroll_history_down()
            return None
        if et == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            if mouse_event.position.x == app._render_width() - 1:
                self._dragging_scrollbar = True
                app._clear_history_selection()
                app._jump_history_to(mouse_event.position.y)
                return None
            self._selecting = True
            app._start_history_selection(mouse_event.position)
            return None
        if et == MouseEventType.MOUSE_MOVE:
            if self._dragging_scrollbar:
                app._jump_history_to(mouse_event.position.y)
                return None
            if self._selecting and mouse_event.button != MouseButton.NONE:
                app._extend_history_selection(mouse_event.position)
                return None
        if et == MouseEventType.MOUSE_UP:
            if self._dragging_scrollbar:
                self._dragging_scrollbar = False
                return None
            if self._selecting:
                self._selecting = False
                app._extend_history_selection(mouse_event.position)
                return None
        return super().mouse_handler(mouse_event)


def apply_event(state: TuiState, event: object, _width: int = 80) -> TuiState:
    """Apply one validated runtime event to immutable TUI state."""
    if isinstance(event, StateEvent):
        return apply_snapshot(state, event)
    if isinstance(event, OutputEvent):
        return apply_output(state, event)
    return state


class AthenaApp:
    """Full-screen TUI with one focus target and one runtime command surface."""

    def __init__(
        self, runtime: object, project_root, *, input=None, output=None
    ) -> None:
        self.state = TuiState(project_root=str(project_root))
        self._runtime = runtime
        self._restore_history(runtime)
        self._app: Application | None = None
        self._composer: TextArea | None = None
        self._composer_pane = None
        self._confirmation_pane = None
        self._overlay_pane = None
        self._bottom = None
        self._history_control = None
        self._history_scroll = 0
        self._history_selection_anchor = None
        self._history_selection_cursor = None
        self._history_selection_lines = None
        self._confirm_action = ""
        self._exit_code = 0
        self._input = input
        self._output = output
        self._controller = TuiController(runtime, emit=self._on_event)

    def _restore_history(self, runtime: object) -> None:
        """重放持久化的会话记录重建历史，实现断点续传（codex-like resume）。

        runtime 无 ``replay_output_events``（测试 stub）或历史缺失时静默跳过。
        记录按 ``type`` 区分：``user`` 还原为用户消息，其余按 OutputEvent 还原。
        """
        replay = getattr(runtime, "replay_output_events", None)
        if replay is None:
            return
        try:
            records = replay()
        except Exception:
            return
        for record in records:
            if record.get("type") == "user":
                self.state = append_user_message(
                    self.state, str(record.get("text", ""))
                )
                continue
            try:
                event = OutputEvent.model_validate(record)
            except Exception:
                continue
            self.state = apply_output(self.state, event)

    def _on_event(self, event: OutputEvent | StateEvent) -> None:
        width = self._history_content_width()
        before = self._history_line_count(width)
        was_following = self.state.history_follow_tail
        self.state = apply_event(self.state, event, width)
        after = self._history_line_count(width)

        if isinstance(event, OutputEvent) and not was_following:
            self._history_scroll += max(0, after - before)
            self._clamp_history_scroll()

        if self._app is not None:
            if self._history_selection_lines is None:
                self._app.invalidate()

    async def handle_key(self, token: str) -> None:
        """Handle normalized keys; tests use the same path as prompt-toolkit."""
        if token == "pageup":
            self._clear_history_selection(invalidate=False)
            page = max(1, self._history_height() - 1)
            max_scroll = max(0, self._history_line_count() - self._history_height())
            self._history_scroll = min(max_scroll, self._history_scroll + page)
            self.state = set_history_follow(self.state, False)
            return
        if token == "pagedown":
            self._clear_history_selection(invalidate=False)
            page = max(1, self._history_height() - 1)
            self._history_scroll = max(0, self._history_scroll - page)
            self.state = set_history_follow(self.state, self._history_scroll == 0)
            return
        if token == "end":
            self._clear_history_selection(invalidate=False)
            self._history_scroll = 0
            self.state = set_history_follow(self.state, True)
            return
        if token == "esc":
            self.state = (
                close_confirmation(self.state)
                if self.state.mode == CONFIRMATION
                else close_overlay(self.state)
            )
            return
        if self.state.mode == CONFIRMATION:
            if token == "y":
                await self._apply_confirmation()
            elif token == "n":
                self.state = close_confirmation(self.state)
            return
        if token == "?":
            if self._composer is None or not self._composer.text:
                self.state = open_overlay(self.state, HELP_TEXT)
            return
        if token == "s-tab":
            await self._controller.toggle_manual_mode()
            return
        if token == "c-c":
            if not self._copy_history_selection():
                await self._request_quit()
            return
        if token == "enter":
            await self._submit_composer()
            return
        if token in {"c-j", "s-enter"} and self._composer is not None:
            self._composer.buffer.insert_text("\n")

    async def _submit_composer(self) -> None:
        buffer = self._composer.buffer
        original = buffer.document
        draft = original.text
        command = draft.strip()

        if not command:
            return

        if command == "/help":
            buffer.set_document(Document("", 0), bypass_readonly=True)
            self.state = open_overlay(self.state, HELP_TEXT)
            return
        if command == "/quit":
            buffer.set_document(Document("", 0), bypass_readonly=True)
            await self._request_quit()
            return
        if command == "/stop":
            buffer.set_document(Document("", 0), bypass_readonly=True)
            self._confirm_action = "stop"
            self.state = open_confirmation(self.state, "停止当前研究执行？")
            return

        buffer.set_document(Document("", 0), bypass_readonly=True)
        self._history_scroll = 0
        self.state = set_history_follow(self.state, True)
        self.state = set_error(append_user_message(self.state, draft), None)
        persist = getattr(self._runtime, "persist_user_message", None)
        if persist is not None:
            try:
                persist(draft)
            except Exception:
                pass
        if self._app is not None:
            self._app.invalidate()

        try:
            await self._controller.send_message(draft)
        except Exception as exc:
            buffer.set_document(original, bypass_readonly=True)
            self.state = replace(self.state, composer=original.text)
            self.state = set_error(self.state, f"发送失败：{exc}")
            if self._app is not None:
                self._app.invalidate()

    async def _request_quit(self) -> None:
        if self.state.control_status in {"STOPPED", "COMPLETED", "FAILED"}:
            if self._app is not None:
                self._app.exit(result=0)
            return
        self._confirm_action = "quit"
        self.state = open_confirmation(self.state, "退出 TUI？研究执行将继续。")

    async def _apply_confirmation(self) -> None:
        action = self._confirm_action
        self._confirm_action = ""
        self.state = close_confirmation(self.state)
        if action == "stop":
            try:
                await self._controller.send_message("/stop")
            except Exception as exc:
                self.state = set_error(self.state, f"停止失败: {exc}")
        elif action == "quit" and self._app is not None:
            self._app.exit(result=0)

    def _render_width(self) -> int:
        if self._app is not None:
            return max(20, self._app.output.get_size().columns)
        if self._output is not None:
            return max(20, self._output.get_size().columns)
        return _DEFAULT_SIZE[1]

    def _render_height(self) -> int:
        if self._app is not None:
            return self._app.output.get_size().rows
        if self._output is not None:
            return self._output.get_size().rows
        return _DEFAULT_SIZE[0]

    def _history_height(self) -> int:
        return max(3, self._render_height() - _FIXED_ROWS - self._composer_height())

    def _composer_height(self) -> int:
        text = (
            self._composer.text if self._composer is not None else self.state.composer
        )
        return composer_height(text, self._render_width())

    def _history_content_width(self) -> int:
        return max(1, self._render_width() - 1)

    def _history_line_count(self, width: int | None = None) -> int:
        render_width = self._history_content_width() if width is None else width
        return len(render_history_lines(self.state, render_width))

    def _clamp_history_scroll(self) -> None:
        max_scroll = max(0, self._history_line_count() - self._history_height())
        self._history_scroll = min(max_scroll, max(0, self._history_scroll))

    def _scroll_history_up(self, lines: int = _SCROLL_STEP) -> None:
        self._clear_history_selection(invalidate=False)
        self._history_scroll += lines
        self._clamp_history_scroll()
        self.state = set_history_follow(self.state, self._history_scroll == 0)
        if self._app is not None:
            self._app.invalidate()

    def _scroll_history_down(self, lines: int = _SCROLL_STEP) -> None:
        self._clear_history_selection(invalidate=False)
        self._history_scroll = max(0, self._history_scroll - lines)
        self.state = set_history_follow(self.state, self._history_scroll == 0)
        if self._app is not None:
            self._app.invalidate()

    def _history_fragments(self) -> StyleAndTextTuples:
        lines = render_history_lines(self.state, self._history_content_width())
        height = self._history_height()
        total = len(lines)
        end = (
            total
            if self.state.history_follow_tail
            else max(0, total - self._history_scroll)
        )
        start = max(0, end - height)
        if self._history_selection_lines is not None:
            lines = self._history_selection_lines
            total = len(lines)
            start = 0
            end = total
        visible = tuple(
            self._apply_history_selection(line, row)
            for row, line in enumerate(lines[start:end])
        )
        if total <= height:
            # 内容不足一屏也显示轨道，让滚动条可见。
            return _join_lines(
                tuple([*line, ("class:scrollbar", "│")] for line in visible)
            )
        scroll = 0 if self.state.history_follow_tail else self._history_scroll
        thumb_h = max(1, round(height * height / total))
        max_scroll = max(1, total - height)
        top = round((1 - scroll / max_scroll) * (height - thumb_h))
        rows = []
        for index, line in enumerate(visible):
            in_thumb = top <= index < top + thumb_h
            rows.append([*line, ("class:scrollbar", "█" if in_thumb else "│")])
        return _join_lines(tuple(rows))

    def _start_history_selection(self, point) -> None:
        lines = render_history_lines(self.state, self._history_content_width())
        height = self._history_height()
        total = len(lines)
        end = (
            total
            if self.state.history_follow_tail
            else max(0, total - self._history_scroll)
        )
        start = max(0, end - height)
        self._history_selection_lines = tuple(lines[start:end])
        self.state = set_history_follow(self.state, False)
        self._history_selection_anchor = point
        self._history_selection_cursor = point
        if self._app is not None:
            self._app.invalidate()

    def _extend_history_selection(self, point) -> None:
        if self._history_selection_anchor is None:
            return
        self._history_selection_cursor = point
        if self._app is not None:
            self._app.invalidate()

    def _clear_history_selection(self, *, invalidate: bool = True) -> None:
        self._history_selection_anchor = None
        self._history_selection_cursor = None
        self._history_selection_lines = None
        if invalidate and self._app is not None:
            self._app.invalidate()

    def _copy_history_selection(self) -> bool:
        text = self._selected_history_text()
        if not text or self._app is None:
            return False
        try:
            self._app.clipboard.set_text(text)
        except Exception:
            self._app.clipboard = InMemoryClipboard()
            self._app.clipboard.set_text(text)
        return True

    def _selected_history_text(self) -> str:
        anchor = self._history_selection_anchor
        cursor = self._history_selection_cursor
        lines = self._history_selection_lines
        if anchor is None or cursor is None or lines is None or anchor == cursor:
            return ""

        first, last = sorted(((anchor.y, anchor.x), (cursor.y, cursor.x)))
        selected_lines: list[str] = []
        for row in range(first[0], min(last[0], len(lines) - 1) + 1):
            start = first[1] if row == first[0] else 0
            end = last[1] if row == last[0] else self._history_content_width()
            column = 0
            selected = ""
            for _style, fragment in lines[row]:
                for char in fragment:
                    width = max(0, get_cwidth(char))
                    char_end = column + width
                    if char_end > start and column < end:
                        selected += char
                    column = char_end
            selected_lines.append(selected.rstrip())
        return "\n".join(selected_lines)

    def _apply_history_selection(
        self, line: StyleAndTextTuples, row: int
    ) -> StyleAndTextTuples:
        anchor = self._history_selection_anchor
        cursor = self._history_selection_cursor
        if anchor is None or cursor is None or anchor == cursor:
            return line

        first, last = sorted(((anchor.y, anchor.x), (cursor.y, cursor.x)))
        if row < first[0] or row > last[0]:
            return line
        start = first[1] if row == first[0] else 0
        end = last[1] if row == last[0] else self._history_content_width()
        if end <= start:
            return line

        selected: StyleAndTextTuples = []
        column = 0
        for style, text in line:
            for char in text:
                width = max(0, get_cwidth(char))
                char_end = column + width
                in_selection = char_end > start and column < end
                selected_style = (
                    f"{style} class:history.selection" if in_selection else style
                )
                if selected and selected[-1][0] == selected_style:
                    previous_style, previous_text = selected[-1]
                    selected[-1] = (previous_style, previous_text + char)
                else:
                    selected.append((selected_style, char))
                column = char_end
        return selected

    def _jump_history_to(self, row: int) -> None:
        """把历史视图跳到滚动条上 ``row`` 行（内容相对坐标）对应的位置。"""
        height = self._history_height()
        total = self._history_line_count()
        max_scroll = max(0, total - height)
        if max_scroll <= 0:
            return
        fraction = min(1.0, max(0.0, row / max(1, height - 1)))
        self._history_scroll = min(max_scroll, int(max_scroll * (1 - fraction)))
        self._clamp_history_scroll()
        self.state = set_history_follow(self.state, self._history_scroll == 0)
        if self._app is not None:
            self._app.invalidate()

    def _bottom_container(self):
        if self.state.mode == CONFIRMATION:
            return self._confirmation_pane
        if self.state.overlay:
            return self._overlay_pane
        return self._composer_pane

    def _build(self) -> Application:
        self._composer = TextArea(
            height=lambda: Dimension.exact(self._composer_height()),
            prompt="› ",
            multiline=True,
            wrap_lines=True,
            scrollbar=True,
            dont_extend_height=True,
            style="class:composer",
        )
        self._composer.buffer.on_text_changed += lambda _buffer: setattr(
            self, "state", replace(self.state, composer=self._composer.text)
        )
        header = Window(
            FormattedTextControl(
                lambda: render_header(self.state, self._render_width())
            ),
            height=1,
            dont_extend_height=True,
        )
        self._history_control = _HistoryControl(self)
        history = Window(self._history_control, wrap_lines=True)
        self._composer_pane = Frame(
            self._composer,
            title=lambda: render_bottom_pane(self.state, self._render_width()),
            style="class:composer-frame.focused",
        )
        self._confirmation_pane = Window(
            FormattedTextControl(
                lambda: render_bottom_pane(self.state, self._render_width()),
                focusable=True,
            )
        )
        self._overlay_pane = Window(
            FormattedTextControl(
                lambda: render_bottom_pane(self.state, self._render_width()),
                focusable=True,
            )
        )
        self._bottom = DynamicContainer(self._bottom_container)
        status = Window(
            FormattedTextControl(
                lambda: render_status(self.state, self._render_width())
            ),
            height=1,
            dont_extend_height=True,
            style=lambda: f"class:status.{self.state.control_status.lower()}",
        )
        self._app = Application(
            layout=Layout(
                HSplit([header, history, self._bottom, status]), self._composer
            ),
            key_bindings=self._key_bindings(),
            full_screen=True,
            mouse_support=True,
            input=self._input,
            output=self._output,
            clipboard=_create_system_clipboard(),
            style=_STYLE,
        )
        return self._app

    def _key_bindings(self) -> KeyBindings:
        keys = KeyBindings()

        def bind(key: str, *, action: str | None = None, when=None) -> None:
            """Bind one prompt-toolkit key token to the shared key handler."""

            async def handler(_event) -> None:
                """Forward a prompt-toolkit key event to the normalized handler."""
                await self.handle_key(action or key)

            add = keys.add(key) if when is None else keys.add(key, filter=when)
            add(handler)

        for token in ("pageup", "pagedown", "end", "c-c"):
            bind(token)
        bind(
            "?",
            when=Condition(
                lambda: self.state.mode == COMPOSER
                and self.state.overlay is None
                and self._composer is not None
                and not self._composer.text
            ),
        )
        confirmation = Condition(lambda: self.state.mode == CONFIRMATION)
        modal = Condition(
            lambda: self.state.mode == CONFIRMATION or self.state.overlay is not None
        )

        @keys.add(Keys.Any, filter=modal)
        def ignore_modal_text(_event) -> None:
            """Swallow arbitrary keys while a modal overlay is open."""

        bind("escape", action="esc", when=modal)
        bind("y", when=confirmation)
        bind("n", when=confirmation)

        composer = Condition(
            lambda: self.state.mode == COMPOSER and self.state.overlay is None
        )

        bind("s-tab", when=composer)

        @keys.add("c-m", filter=composer, eager=True)
        async def submit_or_newline(event) -> None:
            """Submit on plain Enter, insert a newline on Shift+Enter."""
            if event.key_sequence[-1].data in _SHIFT_ENTER_SEQUENCES:
                self._composer.buffer.insert_text("\n")
            else:
                await self.handle_key("enter")

        @keys.add("c-j", filter=composer, eager=True)
        def insert_newline(_event) -> None:
            """Insert a newline at the composer cursor (Ctrl+J fallback)."""
            self._composer.buffer.insert_text("\n")

        return keys

    async def run(self) -> int:
        """Run the full-screen application and always close the controller."""
        try:
            self._build()
            await self._app.run_async()
        finally:
            await self._controller.aclose()
        return self._exit_code


__all__ = ["AthenaApp", "apply_event"]
