"""斜杠命令注册表 — 不 import 终端库，命令行为可脱离 TUI 单测。

每个 handler 吃一个 ``CommandContext``，吐 ``Block`` 列表。需要 TUI 主体配合的动作
（清屏、退出）通过 ``Block("control", {"action": ...})`` 上抛，由 ``app.py`` 执行。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from athena.tui.state import APPROVAL_MODES, AppState, Block

COMMAND_PREFIX = "/"
DEFAULT_EVENT_TAIL = 20


@dataclass(slots=True)
class CommandContext:
    """命令执行所需的一切。``session`` / ``agent_runtime`` 在单测里可以是 None。"""

    state: AppState
    args: str = ""
    session: Any = None
    agent_runtime: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    usage: str
    handler: Callable[[CommandContext], Awaitable[list[Block]]]


def is_command(text: str) -> bool:
    """以单个 ``/`` 开头且紧跟字母才算命令；``//`` 视为普通文本转义。"""
    stripped = text.lstrip()
    if not stripped.startswith(COMMAND_PREFIX):
        return False
    rest = stripped[1:]
    return bool(rest) and (rest[0].isalpha() or rest[0] == "?")


def split_command(text: str) -> tuple[str, str]:
    """``/switch abc def`` → ``("switch", "abc def")``。"""
    stripped = text.lstrip()[1:]
    name, _, args = stripped.partition(" ")
    return name.strip().lower(), args.strip()


async def dispatch(text: str, ctx: CommandContext) -> list[Block]:
    """执行一条斜杠命令。未知命令给出最接近的候选。"""
    name, args = split_command(text)
    command = COMMANDS.get(name)
    if command is None:
        return [_error(f"未知命令 /{name}。{_suggest(name)}")]
    ctx.args = args
    return await command.handler(ctx)


async def _help(ctx: CommandContext) -> list[Block]:
    lines = ["## 斜杠命令", "", "| 命令 | 说明 |", "| --- | --- |"]
    for command in _ordered():
        lines.append(f"| `{command.usage}` | {command.summary} |")
    lines += [
        "",
        "## 键位",
        "",
        "| 键 | 行为 |",
        "| --- | --- |",
        "| `Enter` | 提交；运行中则排队 |",
        "| `Alt+Enter` / `Ctrl+J` | 换行 |",
        "| `Esc` | 中断当前 Turn |",
        "| `Ctrl+C` | 清空输入 / 中断 / 连按两次退出 |",
        "| `Ctrl+D` | 输入为空时退出 |",
        "| `Ctrl+L` | 清屏 |",
        "| `Ctrl+R` | 切换 verbose |",
        "| `Shift+Tab` | 循环审批模式 |",
        "| `y` / `n` / `a` | 审批：批准 / 拒绝 / 始终批准 |",
    ]
    return [Block("markdown", {"text": "\n".join(lines)})]


async def _status(ctx: CommandContext) -> list[Block]:
    state = ctx.state
    thread = state.thread_id or "（未创建）"
    turn = state.turn.turn_id if state.turn else "（空闲）"
    rows = [
        "## 会话状态",
        "",
        f"- session: `{state.session_id}`",
        f"- thread: `{thread}`（共 {len(state.threads)} 条）",
        f"- turn: `{turn}`",
        f"- profile / model: `{state.profile}` / `{state.model}`",
        f"- 审批模式: `{state.approval_mode}`"
        + (f"，始终批准: {sorted(state.always_allow)}" if state.always_allow else ""),
        f"- verbose: `{state.verbose}`",
        f"- 已完成 Turn: {len(state.history)}",
        f"- 排队中的输入: {len(state.queued)}",
    ]
    return [Block("markdown", {"text": "\n".join(rows)})]


async def _threads(ctx: CommandContext) -> list[Block]:
    state = ctx.state
    if not state.threads:
        return [_info("还没有任何 Thread。")]
    lines = ["## Threads", ""]
    for tid in state.threads:
        marker = "●" if tid == state.thread_id else "○"
        lines.append(f"- {marker} `{tid}`")
    return [Block("markdown", {"text": "\n".join(lines)})]


async def _new_thread(ctx: CommandContext) -> list[Block]:
    if ctx.session is None:
        return [_error("当前没有可用会话。")]
    thread_id = await ctx.session.start_thread()
    ctx.state.thread_id = thread_id
    ctx.state.threads = list(ctx.session.threads)
    ctx.state.turn = None
    return [_success(f"已新建 Thread {_short(thread_id)}，上下文从零开始。")]


async def _switch(ctx: CommandContext) -> list[Block]:
    if ctx.session is None:
        return [_error("当前没有可用会话。")]
    if not ctx.args:
        return [_error("用法：/switch <thread_id 前缀>")]
    matches = [t for t in ctx.state.threads if t.startswith(ctx.args)]
    if not matches:
        return [_error(f"没有匹配 `{ctx.args}` 的 Thread。")]
    if len(matches) > 1:
        return [_error(f"前缀 `{ctx.args}` 匹配到 {len(matches)} 条，请写得更长。")]
    await ctx.session.switch(matches[0])
    ctx.state.thread_id = matches[0]
    ctx.state.turn = None
    return [_success(f"已切换到 Thread {_short(matches[0])}。")]


async def _fork(ctx: CommandContext) -> list[Block]:
    if ctx.session is None:
        return [_error("当前没有可用会话。")]
    child = await ctx.session.fork(ctx.args or None)
    ctx.state.thread_id = child
    ctx.state.threads = list(ctx.session.threads)
    ctx.state.turn = None
    return [
        _success(f"已分叉出 Thread {_short(child)}。"),
        _info(
            "注意：分叉继承的是 context_ref 快照，后端为新 Thread 新建了空的消息窗口，"
            "对话历史不会被复制。"
        ),
    ]


async def _interrupt(ctx: CommandContext) -> list[Block]:
    if ctx.session is None or ctx.state.turn is None:
        return [_info("当前没有正在运行的 Turn。")]
    await ctx.session.interrupt(ctx.state.turn.turn_id, "slash_command")
    return []


async def _verbose(ctx: CommandContext) -> list[Block]:
    ctx.state.verbose = not ctx.state.verbose
    return [_info(f"verbose = {ctx.state.verbose}")]


async def _mode(ctx: CommandContext) -> list[Block]:
    state = ctx.state
    if not ctx.args:
        state.cycle_approval_mode()
        return [_info(f"审批模式 → {state.approval_mode}")]
    if ctx.args not in APPROVAL_MODES:
        return [_error(f"模式只能是 {' / '.join(APPROVAL_MODES)}。")]
    state.approval_mode = ctx.args
    return [_info(f"审批模式 → {state.approval_mode}")]


async def _events(ctx: CommandContext) -> list[Block]:
    count = _to_int(ctx.args, DEFAULT_EVENT_TAIL)
    recent = list(ctx.state.recent_events)[-count:]
    if not recent:
        return [_info("还没有收到任何事件。")]
    lines = [f"最近 {len(recent)} 条事件："]
    lines += [f"  [{seq}] {kind}" for seq, kind in recent]
    return [Block("notice", {"text": "\n".join(lines), "level": "info"})]


async def _tools(ctx: CommandContext) -> list[Block]:
    if ctx.agent_runtime is None:
        return [_info("当前 runner 没有暴露工具列表（mock runner 无工具）。")]
    names = ctx.agent_runtime.tool_names
    if not names:
        return [_info("当前 agent 没有注册任何工具。")]
    lines = ["## 已注册工具", ""] + [f"- `{n}`" for n in names]
    return [Block("markdown", {"text": "\n".join(lines)})]


async def _model(ctx: CommandContext) -> list[Block]:
    if ctx.agent_runtime is None:
        return [_error("当前 runner 不支持切换模型。")]
    if not ctx.args:
        return [_info(f"当前模型：{ctx.agent_runtime.model}")]
    ctx.agent_runtime.set_model(ctx.args)
    ctx.state.model = ctx.args
    return [_success(f"模型 → {ctx.args}（下一个 Turn 生效）")]


async def _agent(ctx: CommandContext) -> list[Block]:
    if ctx.agent_runtime is None:
        return [_error("当前 runner 不支持切换 profile。")]
    names = ctx.agent_runtime.profile_names
    if not ctx.args:
        return [_info(f"当前 profile：{ctx.agent_runtime.profile}；可选：{names}")]
    if ctx.args not in names:
        return [_error(f"未知 profile `{ctx.args}`，可选：{names}")]
    ctx.agent_runtime.set_profile(ctx.args)
    ctx.state.profile = ctx.args
    ctx.state.model = ctx.agent_runtime.model
    return [_success(f"profile → {ctx.args}（下一个 Turn 生效）")]


async def _clear(ctx: CommandContext) -> list[Block]:
    return [Block("control", {"action": "clear"})]


async def _exit(ctx: CommandContext) -> list[Block]:
    ctx.state.exit_requested = True
    return [Block("control", {"action": "exit"})]


_DEFINITIONS = [
    ("help", "列出命令与键位", "/help", _help),
    ("status", "当前 session / thread / turn 状态", "/status", _status),
    ("threads", "列出本会话的所有 Thread", "/threads", _threads),
    ("new", "新建 Thread（上下文清零）", "/new", _new_thread),
    ("switch", "切换到指定 Thread", "/switch <id前缀>", _switch),
    ("fork", "从当前 Thread 分叉", "/fork [turn_id]", _fork),
    ("interrupt", "中断当前 Turn", "/interrupt", _interrupt),
    ("verbose", "切换原始事件流显示", "/verbose", _verbose),
    ("mode", "审批模式 ask / auto / deny", "/mode [模式]", _mode),
    ("events", "打印最近的事件", "/events [n]", _events),
    ("tools", "列出当前 agent 的工具", "/tools", _tools),
    ("model", "查看或切换模型", "/model [名称]", _model),
    ("agent", "查看或切换 agent profile", "/agent [profile]", _agent),
    ("clear", "清空屏幕", "/clear", _clear),
    ("exit", "退出", "/exit", _exit),
]

COMMANDS: dict[str, Command] = {
    name: Command(name, summary, usage, handler)
    for name, summary, usage, handler in _DEFINITIONS
}
COMMANDS["quit"] = Command("quit", "退出", "/quit", _exit)
COMMANDS["?"] = Command("?", "列出命令与键位", "/?", _help)


def command_names() -> list[str]:
    """按注册顺序返回可补全的命令名（别名排在最后）。"""
    return [name for name, *_ in _DEFINITIONS] + ["quit"]


def _ordered() -> list[Command]:
    """帮助表的行序 —— 含别名，用户看到什么就能补全什么。"""
    return [COMMANDS[name] for name in command_names()]


def _suggest(name: str) -> str:
    candidates = [n for n in command_names() if n.startswith(name[:2])]
    return f"你是想用 /{candidates[0]} 吗？" if candidates else "输入 /help 查看全部。"


def _short(thread_id: str) -> str:
    return thread_id[:8]


def _to_int(value: str, fallback: int) -> int:
    return int(value) if value.isdigit() else fallback


def _info(text: str) -> Block:
    return Block("notice", {"text": text, "level": "info"})


def _success(text: str) -> Block:
    return Block("notice", {"text": text, "level": "success"})


def _error(text: str) -> Block:
    return Block("notice", {"text": text, "level": "error"})
