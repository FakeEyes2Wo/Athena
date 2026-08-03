"""键位绑定 — 只做分发，具体行为都在 ``AthenaTUI`` 上。

按键处理器是同步的，凡是需要 await 的动作都通过
``Application.create_background_task`` 丢到事件循环里。
"""

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

CTRL_C_EXIT_WINDOW = 2.0


def build_keybindings(tui) -> KeyBindings:
    """把键位绑到 ``tui``（``app.py`` 里的 AthenaTUI）的动作方法上。"""
    kb = KeyBindings()
    pending = Condition(lambda: tui.state.pending_approval is not None)
    idle = ~pending

    @kb.add("enter", filter=idle)
    def _submit(event):
        buffer = event.app.current_buffer
        completion = buffer.complete_state and buffer.complete_state.current_completion
        if completion is not None:
            buffer.apply_completion(completion)
            return
        tui.on_submit()

    @kb.add("escape", "enter", filter=idle)
    @kb.add("c-j", filter=idle)
    def _newline(event):
        event.app.current_buffer.insert_text("\n")

    @kb.add("tab", filter=idle)
    def _complete(event):
        buffer = event.app.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=False)

    @kb.add("escape", filter=idle, eager=True)
    def _interrupt(event):
        tui.on_interrupt()

    @kb.add("c-c")
    def _cancel(event):
        buffer = event.app.current_buffer
        if buffer.text:
            buffer.reset()
            return
        if tui.state.pending_approval is not None:
            tui.on_approval("deny")
            return
        if tui.state.busy:
            tui.on_interrupt()
            return
        tui.on_exit_request()

    @kb.add("c-d", filter=idle)
    def _eof(event):
        if not event.app.current_buffer.text:
            tui.on_exit_request(force=True)

    @kb.add("c-l")
    def _clear(event):
        tui.on_clear()

    @kb.add("c-r", filter=idle)
    def _verbose(event):
        tui.on_toggle_verbose()

    @kb.add("s-tab", filter=idle)
    def _mode(event):
        tui.on_cycle_mode()

    @kb.add("y", filter=pending, eager=True)
    def _approve(event):
        tui.on_approval("approve")

    @kb.add("n", filter=pending, eager=True)
    def _deny(event):
        tui.on_approval("deny")

    @kb.add("a", filter=pending, eager=True)
    def _always(event):
        tui.on_approval("always")

    return kb
