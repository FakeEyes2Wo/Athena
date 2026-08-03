"""Application 级测试 — 用管道输入 + DummyOutput 真正跑一遍 TUI。

覆盖布局构建、按键分发、提交/排队/中断/退出这些只有跑起来才验证得到的路径。
"""

import asyncio
import contextlib

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from athena.tui.app import AthenaTUI
from athena.tui.runner import MockRunner
from athena.tui.session import TuiSession

RUN_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def in_memory_history(monkeypatch):
    """别让测试往用户家目录写历史文件。"""
    monkeypatch.setenv("ATHENA_TUI_HISTORY", "")


async def run_tui(script, *, delay: float = 0.001, mode: str = "auto"):
    """在管道输入下跑一次 TUI，``script`` 负责发按键。返回退出后的 TUI。

    脚本断言失败时立刻让 Application 退出，否则会一直挂到 ``RUN_TIMEOUT``，
    真正的失败原因就被超时盖掉了。
    """
    session = await TuiSession.create(MockRunner(delay=delay), session_id="app-test")
    failure: list[BaseException] = []
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            tui = AthenaTUI(session, verbose=False, approval_mode=mode)

            async def guarded() -> None:
                try:
                    await script(pipe, tui)
                except asyncio.CancelledError:
                    # run_tui 收尾时取消驱动任务 → 正常路径
                    raise
                except BaseException as exc:
                    failure.append(exc)
                    with contextlib.suppress(Exception):
                        tui._app.exit()
                    raise

            driver = asyncio.create_task(guarded())
            try:
                await asyncio.wait_for(tui.run(), timeout=RUN_TIMEOUT)
            finally:
                driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)
    if failure:
        raise failure[0]
    return tui


async def settle(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


async def test_submit_runs_a_turn_then_exits():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("看看仓库结构\r")
        await settle(lambda: len(tui.state.history) == 1)
        pipe.send_text("/exit\r")

    tui = await run_tui(script)
    assert len(tui.state.history) == 1
    turn = tui.state.history[0]
    assert turn.status == "completed"
    assert [t.name for t in turn.tools] == ["list_dir"]


async def test_second_input_is_queued_then_drained():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("第一问\r")
        await settle(lambda: tui.state.busy)
        pipe.send_text("第二问\r")
        await settle(lambda: len(tui.state.queued) == 1)
        await settle(lambda: len(tui.state.history) == 2, timeout=15)
        pipe.send_text("/exit\r")

    tui = await run_tui(script, delay=0.01)
    assert tui.state.queued == []
    assert len(tui.state.history) == 2


async def test_escape_interrupts_running_turn():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("慢慢来\r")
        await settle(lambda: tui.state.busy)
        pipe.send_text("\x1b")
        await settle(lambda: len(tui.state.history) == 1, timeout=15)
        pipe.send_text("/exit\r")

    tui = await run_tui(script, delay=0.2)
    assert tui.state.history[0].status == "interrupted"


async def test_slash_command_changes_state_without_a_turn():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("/mode deny\r")
        await settle(lambda: tui.state.approval_mode == "deny")
        pipe.send_text("/verbose\r")
        await settle(lambda: tui.state.verbose)
        pipe.send_text("/exit\r")

    tui = await run_tui(script)
    assert tui.state.history == []


async def test_shift_tab_cycles_approval_mode():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("\x1b[Z")
        await settle(lambda: tui.state.approval_mode == "auto")
        pipe.send_text("/exit\r")

    tui = await run_tui(script, mode="ask")
    assert tui.state.approval_mode == "auto"


async def test_ctrl_c_needs_two_presses():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("\x03")
        await asyncio.sleep(0.2)
        assert tui.state.exit_requested is False
        pipe.send_text("\x03")

    tui = await run_tui(script)
    assert tui.state.exit_requested is True


async def test_ctrl_c_clears_pending_input_first():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        pipe.send_text("草稿")
        await settle(lambda: tui._buffer.text == "草稿")
        pipe.send_text("\x03")
        await settle(lambda: tui._buffer.text == "")
        pipe.send_text("\x03")
        await asyncio.sleep(0.2)
        pipe.send_text("\x03")

    tui = await run_tui(script)
    assert tui.state.exit_requested is True


async def ask_approval(tui) -> asyncio.Task:
    """走真实通道发一次审批请求：server → transport → 事件泵 → 弹窗。"""
    return asyncio.create_task(
        tui._session.server.request_approval(
            tui.state.thread_id,
            "turn-1",
            "write_file(path=a.py)",
            payload={"tool": "write_file", "args": {"path": "a.py"}},
        )
    )


async def test_approval_can_be_granted():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        verdict = await ask_approval(tui)
        await settle(lambda: tui.state.pending_approval is not None)
        assert tui.state.pending_approval.tool == "write_file"
        pipe.send_text("y")
        await settle(verdict.done)
        assert verdict.result() is True
        pipe.send_text("/exit\r")

    tui = await run_tui(script, mode="ask")
    assert tui.state.pending_approval is None


async def test_approval_can_be_denied():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        verdict = await ask_approval(tui)
        await settle(lambda: tui.state.pending_approval is not None)
        pipe.send_text("n")
        await settle(verdict.done)
        assert verdict.result() is False
        pipe.send_text("/exit\r")

    await run_tui(script, mode="ask")


async def test_always_allow_skips_the_second_prompt():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        first = await ask_approval(tui)
        await settle(lambda: tui.state.pending_approval is not None)
        pipe.send_text("a")
        await settle(first.done)
        second = await ask_approval(tui)
        await settle(second.done)
        assert second.result() is True
        assert tui.state.pending_approval is None
        pipe.send_text("/exit\r")

    tui = await run_tui(script, mode="ask")
    assert "write_file" in tui.state.always_allow


async def test_new_thread_command_switches_subscription():
    async def script(pipe, tui):
        await settle(lambda: tui.state.thread_id is not None)
        first = tui.state.thread_id
        pipe.send_text("/new\r")
        await settle(lambda: tui.state.thread_id != first)
        pipe.send_text("/exit\r")

    tui = await run_tui(script)
    assert len(tui.state.threads) == 2
