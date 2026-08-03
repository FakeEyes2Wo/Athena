"""斜杠命令测试 — session 与 agent_runtime 都用替身。"""

import pytest

from athena.tui.commands import (
    CommandContext,
    command_names,
    dispatch,
    is_command,
    split_command,
)
from athena.tui.state import AppState


class FakeSession:
    def __init__(self) -> None:
        self.threads = ["thread-aaa", "thread-bbb"]
        self.switched: list[str] = []
        self.forked: list[str | None] = []
        self.interrupted: list[str] = []

    async def start_thread(self) -> str:
        self.threads.append("thread-ccc")
        return "thread-ccc"

    async def switch(self, thread_id: str) -> None:
        self.switched.append(thread_id)

    async def fork(self, after_turn_id=None) -> str:
        self.forked.append(after_turn_id)
        self.threads.append("thread-fork")
        return "thread-fork"

    async def interrupt(self, turn_id: str, reason: str) -> None:
        self.interrupted.append(turn_id)


class FakeRuntime:
    def __init__(self) -> None:
        self.model = "m1"
        self.profile = "demo"
        self.profile_names = ["demo"]
        self.tool_names = ["list_dir", "read_file"]

    def set_model(self, model: str) -> None:
        self.model = model

    def set_profile(self, profile: str) -> None:
        self.profile = profile


@pytest.fixture
def ctx() -> CommandContext:
    state = AppState(session_id="s", model="m1", profile="demo")
    state.threads = ["thread-aaa", "thread-bbb"]
    return CommandContext(
        state=state, session=FakeSession(), agent_runtime=FakeRuntime()
    )


def texts(blocks) -> str:
    return "\n".join(str(b.payload.get("text", "")) for b in blocks)


@pytest.mark.parametrize(
    "text,expected",
    [("/help", True), ("/?", True), ("  /status", True), ("//not", False)],
)
def test_is_command(text, expected):
    assert is_command(text) is expected


def test_plain_text_is_not_command():
    assert is_command("帮我读一下 /etc/hosts") is False


def test_split_command():
    assert split_command("/switch  abc def") == ("switch", "abc def")


async def test_unknown_command_suggests(ctx):
    blocks = await dispatch("/thre", ctx)
    assert "未知命令" in texts(blocks) and "threads" in texts(blocks)


async def test_help_lists_every_command(ctx):
    body = texts(await dispatch("/help", ctx))
    for name in command_names():
        assert f"/{name}" in body


async def test_status_reports_mode_and_model(ctx):
    body = texts(await dispatch("/status", ctx))
    assert "ask" in body and "m1" in body


async def test_verbose_toggles(ctx):
    await dispatch("/verbose", ctx)
    assert ctx.state.verbose is True


async def test_mode_accepts_argument_and_rejects_junk(ctx):
    await dispatch("/mode deny", ctx)
    assert ctx.state.approval_mode == "deny"
    assert "只能是" in texts(await dispatch("/mode wat", ctx))
    assert ctx.state.approval_mode == "deny"


async def test_mode_without_argument_cycles(ctx):
    await dispatch("/mode", ctx)
    assert ctx.state.approval_mode == "auto"


async def test_new_thread_updates_state(ctx):
    await dispatch("/new", ctx)
    assert ctx.state.thread_id == "thread-ccc"
    assert "thread-ccc" in ctx.state.threads


async def test_switch_requires_unique_prefix(ctx):
    assert "匹配到 2 条" in texts(await dispatch("/switch thread-", ctx))
    await dispatch("/switch thread-bbb", ctx)
    assert ctx.session.switched == ["thread-bbb"]
    assert ctx.state.thread_id == "thread-bbb"


async def test_switch_reports_missing(ctx):
    assert "没有匹配" in texts(await dispatch("/switch zzz", ctx))


async def test_fork_warns_about_context(ctx):
    body = texts(await dispatch("/fork turn-9", ctx))
    assert ctx.session.forked == ["turn-9"]
    assert "不会被复制" in body


async def test_interrupt_without_turn_is_noop(ctx):
    await dispatch("/interrupt", ctx)
    assert ctx.session.interrupted == []


async def test_model_switch(ctx):
    await dispatch("/model gpt-x", ctx)
    assert ctx.agent_runtime.model == "gpt-x" and ctx.state.model == "gpt-x"


async def test_agent_rejects_unknown_profile(ctx):
    assert "未知 profile" in texts(await dispatch("/agent nope", ctx))


async def test_tools_lists_registry(ctx):
    assert "list_dir" in texts(await dispatch("/tools", ctx))


async def test_events_tail(ctx):
    for i in range(5):
        ctx.state.recent_events.append((i, f"k{i}"))
    body = texts(await dispatch("/events 2", ctx))
    assert "k4" in body and "k1" not in body


async def test_exit_sets_control_block(ctx):
    blocks = await dispatch("/quit", ctx)
    assert ctx.state.exit_requested is True
    assert blocks[0].kind == "control" and blocks[0].payload["action"] == "exit"


async def test_clear_emits_control(ctx):
    blocks = await dispatch("/clear", ctx)
    assert blocks[0].payload["action"] == "clear"


async def test_commands_work_without_session():
    ctx = CommandContext(state=AppState(session_id="s"))
    assert "没有可用会话" in texts(await dispatch("/new", ctx))
