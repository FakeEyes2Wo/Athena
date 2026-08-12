"""Single-composer prompt-toolkit application for Athena research conversations."""

import asyncio
from dataclasses import replace

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import DynamicContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from athena.research.supervisor.events import OutputEvent, StateEvent
from athena_tui.controller import TuiController
from athena_tui.render import (
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

_HELP = """快捷键
PageUp/PageDown  滚动输出    End  回到尾部
Esc              关闭帮助或取消确认
Ctrl+C           退出 TUI

命令
/help  /pause  /resume  /stop  /quit"""

_DEFAULT_SIZE = (24, 80)
_FIXED_ROWS = 7
# 鼠标滚轮每格回滚的行数。
_SCROLL_STEP = 3
# 历史区顶部行偏移 = header 高度(2)，滚动条鼠标映射用。改布局须同步。
_HISTORY_YPOS = 2
_STYLE = Style.from_dict(
    {
        "composer": "bg:#1f2937 #f9fafb",
        "status.running": "#22c55e",
        "status.waiting": "#f59e0b",
        "status.stopped": "#ef4444",
    }
)


def _join_lines(lines: tuple[StyleAndTextTuples, ...]) -> StyleAndTextTuples:
    output: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        if index:
            output.append(("", "\n"))
        output.extend(line)
    return output


class _HistoryControl(FormattedTextControl):
    """History text with wheel and draggable-scrollbar mouse handling."""

    def __init__(self, app: "AthenaApp") -> None:
        super().__init__(app._history_fragments)
        self._app = app
        self._dragging = False

    def mouse_handler(self, mouse_event):
        app = self._app
        et = mouse_event.event_type
        if et == MouseEventType.SCROLL_UP:
            app._scroll_history_up()
            return None
        if et == MouseEventType.SCROLL_DOWN:
            app._scroll_history_down()
            return None
        if et == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            if mouse_event.position.x != app._render_width() - 1:
                return NotImplemented  # 只响应最右侧滚动条列
            self._dragging = True
            app._jump_history_to(mouse_event.position.y)
            return True
        if et == MouseEventType.MOUSE_MOVE and self._dragging:
            app._jump_history_to(mouse_event.position.y)
            return True
        if et == MouseEventType.MOUSE_UP:
            self._dragging = False
            return True
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
        self._app: Application | None = None
        self._composer: TextArea | None = None
        self._composer_pane = None
        self._confirmation_pane = None
        self._overlay_pane = None
        self._bottom = None
        self._history_control = None
        self._history_scroll = 0
        self._confirm_action = ""
        self._exit_code = 0
        self._input = input
        self._output = output
        self._controller = TuiController(runtime, emit=self._on_event)

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
            if self.state.mode == COMPOSER and self.state.overlay is None:
                self._app.layout.focus(self._composer)
            self._app.invalidate()

    async def handle_key(self, token: str) -> None:
        """Handle normalized keys; tests use the same path as prompt-toolkit."""
        if token == "pageup":
            page = max(1, self._history_height() - 1)
            max_scroll = max(0, self._history_line_count() - self._history_height())
            self._history_scroll = min(max_scroll, self._history_scroll + page)
            self.state = set_history_follow(self.state, False)
            return
        if token == "pagedown":
            page = max(1, self._history_height() - 1)
            self._history_scroll = max(0, self._history_scroll - page)
            self.state = set_history_follow(self.state, self._history_scroll == 0)
            return
        if token == "end":
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
            self.state = open_overlay(self.state, _HELP)
            return
        if token == "c-c":
            await self._request_quit()
            return
        if token == "enter":
            await self._submit_composer()

    async def _submit_composer(self) -> None:
        buffer = self._composer.buffer
        original = buffer.document
        draft = original.text
        command = draft.strip()

        if not command:
            return

        if command == "/help":
            buffer.set_document(Document("", 0), bypass_readonly=True)
            self.state = open_overlay(self.state, _HELP)
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
        if self.state.control_status in {"STOPPED", "COMPLETED"}:
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
        return max(3, self._render_height() - _FIXED_ROWS)

    def _history_content_width(self) -> int:
        return max(1, self._render_width() - 1)

    def _history_line_count(self, width: int | None = None) -> int:
        render_width = self._history_content_width() if width is None else width
        return len(render_history_lines(self.state, render_width))

    def _clamp_history_scroll(self) -> None:
        max_scroll = max(0, self._history_line_count() - self._history_height())
        self._history_scroll = min(max_scroll, max(0, self._history_scroll))

    def _scroll_history_up(self, lines: int = _SCROLL_STEP) -> None:
        self._history_scroll += lines
        self._clamp_history_scroll()
        self.state = set_history_follow(self.state, self._history_scroll == 0)
        if self._app is not None:
            self._app.invalidate()

    def _scroll_history_down(self, lines: int = _SCROLL_STEP) -> None:
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
        visible = lines[start:end]
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
        def accept_input(_buffer) -> bool:
            """Schedule the shared Enter-key handler for accepted input."""
            if self._app is not None:
                self._app.create_background_task(self.handle_key("enter"))
            return True

        self._composer = TextArea(
            height=3,
            prompt="> ",
            multiline=False,
            accept_handler=accept_input,
            style="class:composer",
        )
        self._composer.buffer.on_text_changed += lambda _buffer: setattr(
            self, "state", replace(self.state, composer=self._composer.text)
        )
        header = Window(
            FormattedTextControl(
                lambda: render_header(self.state, self._render_width())
            ),
            height=2,
            dont_extend_height=True,
        )
        self._history_control = _HistoryControl(self)
        history = Window(self._history_control, wrap_lines=True)
        prompt = Window(
            FormattedTextControl(
                lambda: render_bottom_pane(self.state, self._render_width())
            ),
            dont_extend_height=True,
        )
        self._composer_pane = HSplit([prompt, self._composer])
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
            height=2,
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

        for token in (
            "pageup",
            "pagedown",
            "end",
            "escape",
            "c-c",
        ):
            bind(token, action="esc" if token == "escape" else token)
        bind("?", when=Condition(lambda: self.state.mode == COMPOSER))
        confirmation = Condition(lambda: self.state.mode == CONFIRMATION)
        bind("y", when=confirmation)
        bind("n", when=confirmation)
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
