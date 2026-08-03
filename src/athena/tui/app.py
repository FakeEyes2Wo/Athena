"""AthenaTUI — 组装 prompt_toolkit Application，跑事件泵，编排提交与中断。

布局自上而下：流式区 / 审批框 / 状态行 / 输入区 / 提示行。
这五块是**唯一**会被重绘的区域；定稿内容由 ``Transcript`` 打进 scrollback，
再也不动。``patch_stdout(raw=True)`` 保证两者不会互相踩。
"""

import asyncio
import time

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from athena.app_server.protocol import ServerRequest
from athena.tui import commands, composer, live
from athena.tui.approvals import ApprovalCoordinator
from athena.tui.completion import AthenaCompleter
from athena.tui.keys import CTRL_C_EXIT_WINDOW, build_keybindings
from athena.tui.state import AppState, Block, TurnView, reduce_event
from athena.tui.theme import PT_STYLE_RULES, Theme, load_theme
from athena.tui.transcript import Transcript

SPINNER_INTERVAL = 0.12
MAX_STREAM_HEIGHT = 16


class AthenaTUI:
    """TUI 主体。持有状态、会话、渲染器，并把按键分发到具体动作。"""

    def __init__(
        self,
        session,
        *,
        agent_runtime=None,
        theme: Theme | None = None,
        verbose: bool = False,
        approval_mode: str = "ask",
    ) -> None:
        self._session = session
        self._agent_runtime = agent_runtime
        self.theme = theme or load_theme()
        self.state = AppState(
            session_id=session.session_id,
            verbose=verbose,
            approval_mode=approval_mode,
            model=getattr(agent_runtime, "model", "mock"),
            profile=getattr(agent_runtime, "profile", "mock"),
        )
        self._transcript = Transcript(self.theme)
        self._approvals = ApprovalCoordinator(session, self.state)
        self._buffer = composer.create_buffer(AthenaCompleter(), self._accept)
        self._app = self._build_application()
        self._tasks: list[asyncio.Task] = []
        self._last_ctrl_c = 0.0
        self._spinner_index = 0

    async def run(self) -> None:
        """建线程、开事件泵、跑 Application，退出时清理。"""
        thread_id = await self._session.start_thread()
        self.state.thread_id = thread_id
        self.state.threads = list(self._session.threads)
        self._transcript.commit(
            [Block("markdown", {"text": live.banner(self.state, self.theme)})]
        )
        with patch_stdout(raw=True):
            self._tasks = [
                asyncio.create_task(self._pump(), name="tui-pump"),
                asyncio.create_task(self._tick(), name="tui-tick"),
            ]
            try:
                await self._app.run_async()
            finally:
                await self._teardown()

    def _build_application(self) -> Application:
        stream_window = Window(
            FormattedTextControl(self._stream_fragments),
            wrap_lines=True,
            dont_extend_height=True,
            height=Dimension(min=0, max=MAX_STREAM_HEIGHT),
        )
        approval_window = Window(
            FormattedTextControl(self._approval_fragments),
            wrap_lines=True,
            dont_extend_height=True,
            height=Dimension(min=0, max=8),
        )
        input_window = Window(
            BufferControl(buffer=self._buffer),
            wrap_lines=True,
            dont_extend_height=True,
            height=Dimension(min=1),
            get_line_prefix=self._line_prefix,
        )
        root = FloatContainer(
            content=HSplit(
                [
                    ConditionalContainer(
                        stream_window, filter=Condition(self._has_stream)
                    ),
                    ConditionalContainer(
                        approval_window,
                        filter=Condition(
                            lambda: self.state.pending_approval is not None
                        ),
                    ),
                    Window(
                        FormattedTextControl(self._status_fragments),
                        height=1,
                        align=WindowAlign.LEFT,
                    ),
                    input_window,
                    Window(FormattedTextControl(self._hint_fragments), height=1),
                ]
            ),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                )
            ],
        )
        return Application(
            layout=Layout(root, focused_element=input_window),
            key_bindings=build_keybindings(self),
            style=Style(PT_STYLE_RULES),
            full_screen=False,
            mouse_support=False,
            erase_when_done=True,
        )

    def _stream_fragments(self):
        return live.stream_fragments(self.state, self.theme)

    def _approval_fragments(self):
        return live.approval_fragments(self.state, self.theme, self._width())

    def _status_fragments(self):
        frame = self.theme.spinner_frames[
            self._spinner_index % len(self.theme.spinner_frames)
        ]
        return live.status_fragments(self.state, self.theme, frame, self._width())

    def _hint_fragments(self):
        return live.hint_fragments(self.state, self.theme)

    def _line_prefix(self, line_number: int, wrap_count: int):
        if line_number == 0 and wrap_count == 0:
            return [("class:prompt", f"{self.theme.symbols.user} ")]
        return [("", "  ")]

    def _has_stream(self) -> bool:
        turn = self.state.turn
        if turn is None:
            return False
        return bool(turn.text.strip() or turn.running_tools)

    def _width(self) -> int:
        return self._app.output.get_size().columns

    async def _pump(self) -> None:
        """事件泵 —— 协议事件与服务端请求的唯一入口。"""
        while True:
            event = await self._session.next_event()
            if event is None:
                await asyncio.sleep(0.05)
                continue
            if isinstance(event, ServerRequest):
                self._commit(await self._approvals.handle(event))
            else:
                self._commit(
                    reduce_event(
                        self.state,
                        event.kind,
                        event.data,
                        event.event_ref,
                        event.sequence,
                        event.turn_id or "",
                    )
                )
            await self._drain_queue()
            self._app.invalidate()

    async def _tick(self) -> None:
        """只在忙时驱动 spinner 与计时器重绘，空闲时不产生重绘。"""
        while True:
            await asyncio.sleep(SPINNER_INTERVAL)
            if self.state.busy or self.state.pending_approval is not None:
                self._spinner_index += 1
                self._app.invalidate()

    async def _drain_queue(self) -> None:
        if self.state.busy or not self.state.queued:
            return
        text = self.state.queued.pop(0)
        self._commit([Block("user", {"text": text})])
        await self._start_turn(text)

    def _accept(self, buffer) -> bool:
        """Buffer 的 accept_handler —— 返回 False 表示不保留文本。"""
        return False

    def on_submit(self) -> None:
        text = composer.take(self._buffer)
        if text:
            self._app.create_background_task(self._handle_input(text))

    async def _handle_input(self, text: str) -> None:
        """一条用户输入：斜杠命令走命令表，其余作为 Turn 提交或排队。"""
        if commands.is_command(text):
            self._commit([Block("user", {"text": text})])
            ctx = commands.CommandContext(
                state=self.state,
                session=self._session,
                agent_runtime=self._agent_runtime,
            )
            blocks = await self._safely(commands.dispatch(text, ctx))
            self._apply_control(blocks)
            self._commit([b for b in blocks if b.kind != "control"])
            self._app.invalidate()
            return
        if self.state.busy:
            self.state.queued.append(text)
            self._commit(
                [
                    Block("user", {"text": text}),
                    Block(
                        "notice",
                        {
                            "text": f"当前 Turn 未结束，已排队（第 {len(self.state.queued)} 条）。",
                            "level": "info",
                        },
                    ),
                ]
            )
            self._app.invalidate()
            return
        self._commit([Block("user", {"text": text})])
        await self._start_turn(text)

    async def _start_turn(self, text: str) -> None:
        try:
            turn_id = await self._session.submit(text)
        except Exception as exc:
            self._commit(
                [
                    Block(
                        "notice",
                        {
                            "text": f"提交失败：{type(exc).__name__}: {exc}",
                            "level": "error",
                        },
                    )
                ]
            )
            self._app.invalidate()
            return
        # 乐观占位：让状态行立刻进入"运行中"，turn_started 到达时不会覆盖同 id 的视图
        if self.state.turn is None:
            self.state.turn = TurnView(turn_id=turn_id, prompt=text)
        self._app.invalidate()

    def on_interrupt(self) -> None:
        turn = self.state.turn
        if turn is None:
            return
        self._app.create_background_task(
            self._safely(self._session.interrupt(turn.turn_id, "user_interrupt"))
        )

    def on_toggle_verbose(self) -> None:
        self.state.verbose = not self.state.verbose
        self._commit(
            [
                Block(
                    "notice",
                    {"text": f"verbose = {self.state.verbose}", "level": "info"},
                )
            ]
        )
        self._app.invalidate()

    def on_cycle_mode(self) -> None:
        mode = self.state.cycle_approval_mode()
        self._commit([Block("notice", {"text": f"审批模式 → {mode}", "level": "info"})])
        self._app.invalidate()

    def on_clear(self) -> None:
        self._app.renderer.clear()
        self._app.invalidate()

    def on_approval(self, decision: str) -> None:
        self._app.create_background_task(self._resolve_approval(decision))

    async def _resolve_approval(self, decision: str) -> None:
        self._commit(await self._safely(self._approvals.resolve(decision)))
        self._app.invalidate()

    def on_exit_request(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_ctrl_c < CTRL_C_EXIT_WINDOW:
            self.state.exit_requested = True
            self._app.exit()
            return
        self._last_ctrl_c = now
        self._commit(
            [Block("notice", {"text": "再按一次 Ctrl+C 退出。", "level": "warn"})]
        )
        self._app.invalidate()

    def _apply_control(self, blocks: list[Block]) -> None:
        for block in blocks:
            if block.kind != "control":
                continue
            action = block.payload.get("action")
            if action == "clear":
                self.on_clear()
            elif action == "exit":
                self._app.exit()

    def _commit(self, blocks: list[Block]) -> None:
        self._transcript.commit([b for b in blocks if b.kind != "control"])

    async def _safely(self, awaitable):
        """把后台协程的异常收敛成一条通知，不让它冲掉整个 Application。"""
        try:
            return await awaitable
        except asyncio.CancelledError:
            # TUI 退出时后台任务被取消 → 正常路径，向上传播
            raise
        except Exception as exc:
            self._transcript.commit(
                [
                    Block(
                        "notice",
                        {"text": f"{type(exc).__name__}: {exc}", "level": "error"},
                    )
                ]
            )
            return []

    async def _teardown(self) -> None:
        await self._approvals.flush_denied()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._session.shutdown()
