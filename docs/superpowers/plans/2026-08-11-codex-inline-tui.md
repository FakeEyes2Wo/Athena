# Athena Codex-Inline TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refine `src/tui.py` into a quiet, readable Codex-style inline terminal workspace (session header, `›` task prompts, phase track, agent activity blocks, consistent command vocabulary) while preserving all existing Supervisor behavior and terminal scrollback.

**Architecture:** Keep `src/tui.py` as the entry point and split pure rendering from I/O. Module-level theme constants (semantic Rich styles) and pure render helpers (accepting plain dicts/scalars) build every renderable; the existing async functions keep stdin ownership, runtime dispatch, polling, and lifecycle control unchanged. Message-part summarization moves to a new `part_summary` in `athena/agent_messages.py`; `tui.py` owns only terminal composition.

**Tech Stack:** Python 3.11, Rich (already a transitive dependency of `pydantic-ai`), `asyncio`, `pytest` + `pytest-asyncio`. Tests run via `uv run pytest`.

## Global Constraints

- Presentation-focused only. Do NOT alter: `TASK_CONFIGURE -> RUN` lifecycle, status polling, persisted rollout format, `HumanRequest` contract, or slash-command semantics.
- No new terminal UI dependencies. No alternate-screen, no Textual, no continuously redrawn dashboard.
- Pure render helpers MUST accept plain dictionaries or scalar values so they can be tested without a real terminal or runtime.
- Never encode status by color alone — phase labels, status words, and symbols must remain understandable when color is unavailable.
- Respect `Console` color-system fallback and non-interactive captured output. Use `rich.markup.escape` on ALL user/dynamic content so a stray `[` cannot break rendering.
- Rendering failures must not stop research execution. Missing fields use stable fallbacks: phase → `IDLE`, status → `-`, version → `-`.
- Rely on Rich wrapping; do not truncate task descriptions or failure reasons. Fixed-width constructs limited to short phase labels and symbols.
- In scope: `src/tui.py`, `test/unit/test_tui.py`, and the small additive `part_summary` in `src/athena/agent_messages.py` (+ its tests in `test/unit/test_main.py`). Out of scope: Supervisor state machine, retry policy, rollout storage format, `athena-gui`.
- Commits are scoped to ONLY the touched files (`src/tui.py`, `src/athena/agent_messages.py`, `test/unit/test_tui.py`, `test/unit/test_main.py`). The working tree has unrelated pre-existing modifications — never `git add -A` or `git add .`.

**Phase values:** the Supervisor's `ResearchPhase` enum is `IDLE / PREPARE / SEARCH / VALIDATE / COMPLETED`. The display track is `PREPARE -> SEARCH -> VALIDATE -> DONE`. `COMPLETED` maps to the `DONE` slot (index 3). `ControlStatus` values are `RUNNING / PAUSED / WAITING_FOR_HUMAN / COMPLETED / FAILED / CANCELLED`.

**Command vocabulary (single source of truth):**
- `✓` (OK) success · `●` (ACTIVE) in progress · `!` (WARN) recoverable warning · `×` (ERR) failure.

---

### Task 1: Add `part_summary` to `src/athena/agent_messages.py`

**Files:**
- Modify: `src/athena/agent_messages.py` (insert `part_summary` after `read_new_messages`, before `render_parts`)
- Test: `test/unit/test_main.py` (append two tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `part_summary(part: dict, *, expand: bool = False, clip: int = 120) -> tuple[str, str] | None` — used by Task 4's `render_activity_block`. Returns `(kind, text)` where `kind ∈ {"user-prompt", "text", "tool-call", "tool-return"}`; `text` is bare (no role/tool prefixes, tool-call is `"<name> <compact-args>"` with no parens). Returns `None` for non-displayable parts (e.g. `system-prompt`). This function is additive — `render_parts`/`render_message`/`print_new_messages` are untouched and their existing tests stay green.

- [ ] **Step 1: Write the failing tests**

Append to `test/unit/test_main.py`:

```python
def test_part_summary_renders_kind_and_text() -> None:
    """part_summary 把各 part 变成 (kind, 裸文本)，工具调用无括号前缀。"""
    from athena.agent_messages import part_summary

    kind, text = part_summary({"part_kind": "user-prompt", "content": "read data"})
    assert kind == "user-prompt"
    assert text == "read data"

    kind, text = part_summary({"part_kind": "text", "content": "done"})
    assert kind == "text"
    assert text == "done"

    kind, text = part_summary(
        {
            "part_kind": "tool-call",
            "tool_name": "write_file",
            "args": {"path": "analysis.py"},
        }
    )
    assert kind == "tool-call"
    assert text.startswith("write_file ")
    assert '"path"' in text  # args 被 json 序列化为紧凑文本

    kind, text = part_summary(
        {"part_kind": "tool-return", "content": {"path": "analysis.py"}}
    )
    assert kind == "tool-return"
    assert "analysis.py" in text


def test_part_summary_clips_and_skips() -> None:
    """折叠模式截断长文本；system-prompt 等不可展示 part 返回 None。"""
    from athena.agent_messages import part_summary

    long_text = "x" * 500
    _, folded = part_summary({"part_kind": "text", "content": long_text})
    assert len(folded) < 200
    assert "…" in folded
    _, expanded = part_summary({"part_kind": "text", "content": long_text}, expand=True)
    assert expanded == long_text

    assert part_summary({"part_kind": "system-prompt", "content": "sys"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/test_main.py::test_part_summary_renders_kind_and_text test/unit/test_main.py::test_part_summary_clips_and_skips -q`
Expected: FAIL with `ImportError: cannot import name 'part_summary'`.

- [ ] **Step 3: Write minimal implementation**

In `src/athena/agent_messages.py`, after the `read_new_messages` function and before `render_parts`, insert:

```python
def part_summary(
    part: dict, *, expand: bool = False, clip: int = 120
) -> tuple[str, str] | None:
    """渲染单个 part 为 ``(kind, 文本)`` 供 TUI 活动块组合；返回 None 跳过。

    kind ∈ {user-prompt, text, tool-call, tool-return}。文本不含角色/工具前缀，
    由调用方（render_parts / TUI）决定样式。tool-call 为 ``name args``（紧凑、
    无括号）。system-prompt 等不可展示 part 返回 None。
    """
    pk = part.get("part_kind", "")
    if pk == "user-prompt":
        return ("user-prompt", _clip(str(part.get("content", "")), None if expand else clip))
    if pk == "text":
        return ("text", _clip(str(part.get("content", "")), None if expand else clip))
    if pk == "tool-call":
        args = part.get("args", "")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        compact = _clip(str(args), None if expand else 60)
        return ("tool-call", f"{part.get('tool_name', '')} {compact}")
    if pk == "tool-return":
        return ("tool-return", _clip(str(part.get("content", "")), None if expand else clip))
    return None
```

Note: `json` is already imported at the top of `agent_messages.py` (line 9), and `_clip` already exists (line 111).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test/unit/test_main.py -q`
Expected: PASS (all existing `test_main.py` tests + the two new ones).

- [ ] **Step 5: Commit**

```bash
git add src/athena/agent_messages.py test/unit/test_main.py
git commit -m "feat: add part_summary for TUI activity-block message parts"
```

---

### Task 2: Pure render helpers — header, phase track, status line (`src/tui.py`)

**Files:**
- Modify: `src/tui.py` (add theme constants + `_esc`, `render_header`, `render_phase_track`, `render_status_line`; remove `_describe_progress`)
- Test: `test/unit/test_tui.py`

**Interfaces:**
- Consumes: `_execution_of` / `_runtime` from `athena.cli` (already imported, unchanged).
- Produces: `render_header(project_root: str) -> str`, `render_phase_track(current_phase: str) -> str`, `render_status_line(phase: str, status: str, version: object) -> str`, module constants `PHASES`, `OK/ACTIVE/WARN/ERR`, `STYLE`, and helper `_esc`. These are used by Tasks 3, 5, 6, 7.

- [ ] **Step 1: Write the failing tests**

In `test/unit/test_tui.py`, replace `test_describe_progress_renders_snapshot` (line 284) with:

```python
def test_render_header_contains_product_project_and_supporting_text() -> None:
    """会话头部含产品名、项目路径与克制的支持文案。"""
    tui = _load_tui()
    header = tui.render_header("/tmp/athena-project")
    assert "Athena" in header
    assert "/tmp/athena-project" in header
    assert "对话式研究工作区" in header


def test_render_phase_track_marks_past_current_future() -> None:
    """SEARCH 阶段：PREPARE=subdued-success，SEARCH=emphasized，后续=dim。"""
    tui = _load_tui()
    track = tui.render_phase_track("SEARCH")
    assert "[green]PREPARE[/green]" in track
    assert "[bold]SEARCH[/bold]" in track
    assert "[dim]VALIDATE[/dim]" in track
    assert "[dim]DONE[/dim]" in track


def test_render_phase_track_completed_marks_done_current() -> None:
    """系统相位 COMPLETED 映射到展示轨道 DONE 槽位（全部完成）。"""
    tui = _load_tui()
    track = tui.render_phase_track("COMPLETED")
    assert "[green]PREPARE[/green]" in track
    assert "[green]SEARCH[/green]" in track
    assert "[green]VALIDATE[/green]" in track
    assert "[bold]DONE[/bold]" in track


def test_render_phase_track_idle_all_dim() -> None:
    """IDLE/未知阶段：整轨统一 dim，无强调或完成态。"""
    tui = _load_tui()
    track = tui.render_phase_track("IDLE")
    assert "[bold]" not in track
    assert "[green]" not in track


def test_render_status_line_has_semantic_text_and_plain_output() -> None:
    """状态过渡行含轨道、控制状态与版本，纯文本（去样式）仍可读。"""
    from rich.text import Text

    tui = _load_tui()
    line = tui.render_status_line("SEARCH", "RUNNING", 3)
    plain = Text.from_markup(line).plain
    assert "PREPARE  →  SEARCH  →  VALIDATE  →  DONE" in plain
    assert "RUNNING" in plain
    assert "v3" in plain
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'render_header'` (and for the others).

- [ ] **Step 3: Write minimal implementation**

In `src/tui.py`:
1. Add `from rich.markup import escape as _markup_escape` to the imports (alphabetical: after `rich.console` import).
2. After the `_reader_paused = threading.Event()` block, insert the theme + symbol constants:

```python
# ---------- 主题（语义化样式；颜色只是补充，文本本身可读） ----------

# 确定性生命周期展示轨道（呈现层把系统相位 COMPLETED 映射到 DONE 槽位）。
PHASES = ("PREPARE", "SEARCH", "VALIDATE", "DONE")
_INDEX_BY_PHASE = {"PREPARE": 0, "SEARCH": 1, "VALIDATE": 2, "COMPLETED": 3}

# 命令反馈统一词汇。
OK = "✓"  # 成功
ACTIVE = "●"  # 进行中
WARN = "!"  # 可恢复告警
ERR = "×"  # 失败

_STATUS_SYMBOLS = {
    "RUNNING": ACTIVE,
    "WAITING_FOR_HUMAN": ACTIVE,
    "PAUSED": WARN,
    "COMPLETED": OK,
    "FAILED": ERR,
    "CANCELLED": ERR,
}

STYLE = {
    "brand": "bold",
    "muted": "dim",
    "phase_past": "green",
    "phase_current": "bold",
    "phase_future": "dim",
    "ok": "green",
    "warn": "yellow",
    "err": "bold red",
    "info": "cyan",
    "activity_header": "bold",
    "tool": "dim magenta",
    "tool_error": "red",
    "role": "dim",
}


def _esc(text: object) -> str:
    """转义富文本标记，避免用户内容里的 ``[`` 被当作样式解析。"""
    return _markup_escape(str(text))
```

3. Replace the entire `_describe_progress` function (lines 47-53) with the three render helpers:

```python
def render_header(project_root: str) -> str:
    """会话头部：产品名 + 克制的支持文案 + 项目路径元数据。"""
    return (
        f"[{STYLE['brand']}]Athena[/{STYLE['brand']}] "
        f"[{STYLE['muted']}]对话式研究工作区[/{STYLE['muted']}]\n"
        f"[{STYLE['muted']}]项目: {_esc(project_root)}[/{STYLE['muted']}]"
    )


def render_phase_track(current_phase: str) -> str:
    """内联相位轨道：past=subdued-success，current=emphasized，future=dim。

    ``COMPLETED`` 映射到 ``DONE`` 槽位；IDLE/未知阶段整轨统一 dim。
    """
    idx = _INDEX_BY_PHASE.get((current_phase or "IDLE").strip())
    if idx is None:
        return "  ".join(
            f"[{STYLE['phase_future']}]{p}[/{STYLE['phase_future']}]" for p in PHASES
        )
    out: list[str] = []
    for i, phase in enumerate(PHASES):
        if i:
            out.append("→")
        if i < idx:
            style = STYLE["phase_past"]
        elif i == idx:
            style = STYLE["phase_current"]
        else:
            style = STYLE["phase_future"]
        out.append(f"[{style}]{phase}[/{style}]")
    return "  ".join(out)


def render_status_line(phase: str, status: str, version: object) -> str:
    """状态过渡行：相位轨道 + 控制状态/版本元数据（不依赖颜色）。"""
    track = render_phase_track(phase)
    status_word = _esc(status or "-")
    symbol = _STATUS_SYMBOLS.get(status, "")
    status_text = f"{symbol} {status_word}".strip()
    ver = "-" if version is None else str(version)
    return f"{track}   [{STYLE['muted']}]{status_text} · v{_esc(ver)}[/{STYLE['muted']}]"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: PASS (the 4 replaced/added tests + all other existing TUI tests still green — nothing else in `test_tui.py` calls `_describe_progress` after the replacement).

- [ ] **Step 5: Commit**

```bash
git add src/tui.py test/unit/test_tui.py
git commit -m "feat: tui theme constants + header/phase-track/status-line renderers"
```

---

### Task 3: Execution summary + `_run` / `_collect_task` wiring

**Files:**
- Modify: `src/tui.py` (add `render_execution_summary`, `_short_execution_id`; rewire `_run` and `_collect_task`)
- Test: `test/unit/test_tui.py`

**Interfaces:**
- Consumes: `render_header` (Task 2), `STYLE`, `ACTIVE`, `_esc`.
- Produces: `render_execution_summary(execution_id: str, interaction_mode: str, phase: str) -> str` — used by `_run`.

- [ ] **Step 1: Write the failing test**

Append to `test/unit/test_tui.py`:

```python
def test_render_execution_summary_replaces_protocol_lines() -> None:
    """执行启动摘要含短 id/交互模式/初始阶段，不含原始协议行。"""
    tui = _load_tui()
    summary = tui.render_execution_summary(
        "exec_ab12cd34ef56", "interactive", "PREPARE"
    )
    assert "ab12cd34ef56" in summary  # exec_ 前缀被压缩
    assert "interactive" in summary
    assert "PREPARE" in summary
    assert "configured phase=" not in summary
    assert "execution=" not in summary


def test_short_execution_id_compresses_id() -> None:
    """exec_ 前缀后的随机 hex 保留最多 12 位作为紧凑显示。"""
    tui = _load_tui()
    assert tui._short_execution_id("exec_ab12cd34ef56") == "ab12cd34ef56"
    assert tui._short_execution_id("") == "-"
    assert len(tui._short_execution_id("exec_0123456789abcdef")) <= 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/test_tui.py::test_render_execution_summary_replaces_protocol_lines test/unit/test_tui.py::test_short_execution_id_compresses_id -q`
Expected: FAIL with `AttributeError: ... no attribute 'render_execution_summary'`.

- [ ] **Step 3: Write minimal implementation**

In `src/tui.py`:

1. Add these two functions right after `render_status_line` (from Task 2):

```python
def render_execution_summary(
    execution_id: str, interaction_mode: str, phase: str
) -> str:
    """执行启动摘要：短 execution id + 交互模式 + 初始阶段（替换原始协议行）。"""
    short = _esc(_short_execution_id(execution_id))
    mode = _esc(interaction_mode or "interactive")
    phase = _esc(phase or "IDLE")
    return f"[{STYLE['info']}]{ACTIVE}[/{STYLE['info']}] 执行 {short} · {mode} · 初始阶段 {phase}"


def _short_execution_id(execution_id: str) -> str:
    """从完整 execution id 派生紧凑显示：``exec_ab12cd34ef56`` → ``ab12cd34ef56``。"""
    if not execution_id:
        return "-"
    return execution_id.rsplit("_", 1)[-1][:12]
```

2. Rewrite `_collect_task` (replace the `_console.rule(...)` banner + example + prompt messages):

```python
async def _collect_task() -> tuple[str, str]:
    """通过对话获取任务描述与数据路径（不预置任何信息）。

    对话期间暂停后台命令 reader，避免 rich Prompt 与命令输入竞争 stdin。
    ``›`` 提示语 + 一行 dim 示例；保留空任务校验与默认数据路径。
    """
    _reader_paused.set()
    try:
        _console.print(
            f"[{STYLE['muted']}]例如: 分析 examples/titanic 预测幸存[/{STYLE['muted']}]"
        )
        task = await _ask("› 任务描述", default="")
        while not task.strip():
            _console.print(
                f"[{STYLE['warn']}]{WARN} 任务描述不能为空，请描述你的研究目标。[/{STYLE['warn']}]"
            )
            task = await _ask("› 任务描述", default="")
        data_path = await _ask(
            "› 数据路径（默认 examples/titanic）", default="examples/titanic"
        )
        return task.strip(), data_path.strip()
    finally:
        _reader_paused.clear()
```

3. Rewrite `_run` (print header before collection; drop the two raw protocol `print` lines; print the single execution summary):

```python
async def _run(args: argparse.Namespace) -> int:
    project_root = Path(args.project)
    _console.print(render_header(str(project_root)))
    task, data_path = await _collect_task()
    runtime = _runtime(str(project_root))
    _start_reader()
    try:
        configured = await runtime.dispatch(
            "TASK_CONFIGURE",
            {
                "interaction_mode": "interactive",
                "kfold_policy": "auto",
                "task": task,
                "data_path": data_path,
            },
        )
        run = await runtime.dispatch("RUN", {})
        _console.print(
            render_execution_summary(
                run.get("execution_id", ""),
                run.get("interaction_mode")
                or configured.get("interaction_mode")
                or "interactive",
                run.get("phase") or configured.get("phase") or "IDLE",
            )
        )
        return await _watch(runtime, project_root)
    finally:
        await runtime.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: PASS. In particular `test_collect_task_gets_intent_and_data_path`, `test_collect_task_defaults_data_path` (message still contains `"数据路径"` and default is still `"examples/titanic"`), and `test_collect_task_pauses_reader_during_conversation` must still pass.

- [ ] **Step 5: Commit**

```bash
git add src/tui.py test/unit/test_tui.py
git commit -m "feat: tui execution summary + header and › task-collection prompts"
```

---

### Task 4: Agent activity blocks (`render_activity_block`, `_print_new_messages`, `_cmd_expand`)

**Files:**
- Modify: `src/tui.py` (add `render_activity_block`, `_looks_like_error`; rewrite `_print_new_messages`, `_cmd_expand`; change imports)
- Test: `test/unit/test_tui.py`

**Interfaces:**
- Consumes: `part_summary` (Task 1), `read_new_messages` (already imported), `STYLE`, `OK`, `ERR`, `_esc`.
- Produces: `render_activity_block(agent_id: str, seq: int, msg: dict, *, expand: bool = False) -> str`. `_print_new_messages(sessions_dir, offset_by_agent, history, next_seq) -> int` keeps its existing signature. `_cmd_expand(args, history) -> tuple[bool, str | None]` keeps its existing signature but returns an expanded activity block.

- [ ] **Step 1: Write the failing tests**

In `test/unit/test_tui.py`, replace `test_render_message_user_agent_and_tool` (line 83) and `test_render_message_folds_long_text` (line 104) with:

```python
def test_render_activity_block_has_header_and_tool_line() -> None:
    """活动块含 ◆ agent #n 头与 dim ↳ 工具调用行，user 文本带角色标签。"""
    tui = _load_tui()
    msg = {
        "kind": "request",
        "parts": [
            {"part_kind": "user-prompt", "content": "read data"},
            {
                "part_kind": "tool-call",
                "tool_name": "write_file",
                "args": {"path": "analysis.py"},
            },
        ],
    }
    block = tui.render_activity_block("agent_1", 7, msg)
    assert "◆ agent_1  #7" in block
    assert "↳ write_file" in block
    assert "read data" in block


def test_render_activity_block_folds_long_text_and_expands() -> None:
    """折叠模式截断超长 LLM 文本；展开模式输出完整内容。"""
    tui = _load_tui()
    long_text = "x" * 500
    msg = {"kind": "response", "parts": [{"part_kind": "text", "content": long_text}]}
    folded = tui.render_activity_block("a", 0, msg, expand=False)
    assert len(folded) < 200
    assert "…" in folded
    expanded = tui.render_activity_block("a", 0, msg, expand=True)
    assert long_text in expanded


def test_render_activity_block_skips_non_displayable() -> None:
    """只有 system-prompt 的消息不渲染活动块（保持 /msg 序号语义）。"""
    tui = _load_tui()
    msg = {"kind": "request", "parts": [{"part_kind": "system-prompt", "content": "sys"}]}
    assert tui.render_activity_block("a", 0, msg) == ""


def test_print_new_messages_prints_activity_blocks(tmp_path) -> None:
    """增量打印按 agent 的活动块，并登记 /msg 展开历史序号。"""
    tui = _load_tui()
    sessions = tmp_path / ".athena" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "agent_1.jsonl").write_text(
        json.dumps(
            {
                "msg": [
                    {
                        "kind": "response",
                        "parts": [{"part_kind": "text", "content": "hello"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    printed: list[str] = []

    with mock.patch.object(
        tui._console, "print", side_effect=lambda *a, **k: printed.append(str(a[0]))
    ):
        next_seq = tui._print_new_messages(sessions, {}, [], 0)

    assert next_seq == 1
    joined = " ".join(printed)
    assert "◆ agent_1  #0" in joined
    assert "hello" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: FAIL with `AttributeError: ... no attribute 'render_activity_block'`.

- [ ] **Step 3: Write minimal implementation**

In `src/tui.py`:

1. Change the import line for message content (currently line 23) from:
```python
from athena.agent_messages import read_new_messages, render_message as _render_message
```
to:
```python
from athena.agent_messages import part_summary, read_new_messages
```

2. Add `render_activity_block` and `_looks_like_error` right after `render_execution_summary`:

```python
def render_activity_block(
    agent_id: str, seq: int, msg: dict, *, expand: bool = False
) -> str:
    """渲染一条 rollout 消息为内联活动块：``◆ <short-agent-id>  #<n>`` + 各 part 行。

    tool-call/tool-return 用 dim ``↳`` 行；工具返回含错误标记时用红色区分。
    折叠模式仍为默认；``expand=True`` 输出完整内容。无可展示 part 返回空串。
    """
    lines = [
        f"[{STYLE['activity_header']}]◆ {_esc(agent_id[:8])}  #{seq}[/{STYLE['activity_header']}]"
    ]
    shown = False
    for part in msg.get("parts") or []:
        summary = part_summary(part, expand=expand)
        if summary is None:
            continue
        shown = True
        kind, text = summary
        if kind == "tool-call":
            lines.append(f"[{STYLE['tool']}]↳ {_esc(text)}[/{STYLE['tool']}]")
        elif kind == "tool-return":
            is_err = _looks_like_error(text)
            style = STYLE["tool_error"] if is_err else STYLE["tool"]
            lines.append(f"[{style}]↳ {ERR if is_err else OK} {_esc(text)}[/{style}]")
        elif kind == "user-prompt":
            lines.append(f"[{STYLE['role']}]user[/{STYLE['role']}] {_esc(text)}")
        else:  # text
            lines.append(_esc(text))
    return "\n".join(lines) if shown else ""


def _looks_like_error(text: str) -> bool:
    """启发式判断工具返回是否含错误标记（仅补充样式，文本仍可读）。"""
    low = text.lower()
    return any(
        marker in low
        for marker in ("error", "exception", "traceback", "failed", "失败", "错误")
    )
```

3. Rewrite `_print_new_messages` (replace the `_render_message` summary with the activity block):

```python
def _print_new_messages(
    sessions_dir: Path,
    offset_by_agent: dict[str, int],
    history: list[tuple[int, str, dict]],
    next_seq: int,
) -> int:
    """打印各 agent rollout 的增量消息（折叠活动块），并登记展开用历史。"""
    for agent_id, msg in read_new_messages(sessions_dir, offset_by_agent):
        block = render_activity_block(agent_id, next_seq, msg, expand=False)
        if not block:
            continue
        history.append((next_seq, agent_id, msg))
        _console.print(block)
        next_seq += 1
    return next_seq
```

4. Rewrite `_cmd_expand` to return the expanded block:

```python
def _cmd_expand(
    args: list[str], history: list[tuple[int, str, dict]]
) -> tuple[bool, str | None]:
    """``/msg <序号>`` 展开历史消息；返回 (命令已处理, 完整内容或 None)。"""
    if len(args) != 2 or not args[1].isdigit():
        return True, "用法: /msg <序号>（如 /msg 3）"
    seq = int(args[1])
    for s, agent_id, msg in history:
        if s == seq:
            return True, render_activity_block(agent_id, seq, msg, expand=True)
    return True, f"序号 {seq} 不在历史中（可用 /help 查看范围）"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: PASS. In particular `test_cmd_expand_returns_full_message` ("complete analysis result" now inside the expanded block) and `test_cmd_expand_missing_sequence` must still pass.

- [ ] **Step 5: Commit**

```bash
git add src/tui.py test/unit/test_tui.py
git commit -m "feat: tui agent activity blocks with ◆ headers and ↳ tool lines"
```

---

### Task 5: Command feedback vocabulary + aligned help (`_consume_commands`)

**Files:**
- Modify: `src/tui.py` (add `render_help`; rewrite `_consume_commands` feedback lines)
- Test: `test/unit/test_tui.py`

**Interfaces:**
- Consumes: `_cmd_expand` (Task 4), `STYLE`, `OK`, `WARN`, `ERR`, `_esc`.
- Produces: `render_help() -> str`. `_consume_commands(runtime, history) -> bool` keeps its signature and dispatch semantics.

- [ ] **Step 1: Write the failing test**

Append to `test/unit/test_tui.py`:

```python
def test_render_help_lists_every_command() -> None:
    """帮助输出为对齐的小列表，列出全部支持命令。"""
    tui = _load_tui()
    help_text = tui.render_help()
    for cmd in ("/msg", "/retry", "/quit", "/help"):
        assert cmd in help_text
    assert "/msg <序号>" in help_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/test_tui.py::test_render_help_lists_every_command -q`
Expected: FAIL with `AttributeError: ... no attribute 'render_help'`.

- [ ] **Step 3: Write minimal implementation**

In `src/tui.py`:

1. Add `render_help` after the `render_activity_block` function (module-level; `render_completed_line` does not exist until Task 6, and function order does not matter at module scope):

```python
def render_help() -> str:
    """命令帮助：对齐的小列表（每行一条，不含分号）。"""
    lines = [
        "/msg <序号>  展开完整消息",
        "/retry       重试失败的 execution",
        "/quit        退出",
        "/help        显示帮助",
    ]
    return "\n".join(f"[{STYLE['info']}]{line}[/{STYLE['info']}]" for line in lines)
```

2. Rewrite `_consume_commands` body to use the vocabulary:

```python
async def _consume_commands(
    runtime, history: list[tuple[int, str, dict]]
) -> bool:
    """从全局命令队列取命令并处理；``/quit`` 返回 True 请求退出。

    处理 ``/msg`` 展开、``/retry``（dispatch RETRY 恢复 FAILED execution）与
    ``/help``。反馈统一用 ✓ / ● / ! / × 词汇。
    """
    while not _command_queue.empty():
        line = _command_queue.get_nowait().strip()
        if not line or not line.startswith("/"):
            continue
        parts = line.split()
        if parts[0] == "/msg":
            _, text = _cmd_expand(parts, history)
            if text:
                _console.print(text)
        elif parts[0] == "/retry":
            try:
                result = await runtime.dispatch("RETRY", {})
            except Exception as exc:
                # 并发/日志错误不应击穿 _watch 循环
                _console.print(
                    f"[{STYLE['warn']}]{WARN} 重试失败: "
                    f"{_esc(type(exc).__name__)}: {_esc(exc)}（继续轮询）[/{STYLE['warn']}]"
                )
            else:
                if result.get("retried"):
                    _console.print(
                        f"[{STYLE['ok']}]{OK} 已发起重试（execution 恢复 RUNNING）[/{STYLE['ok']}]"
                    )
                else:
                    _console.print(
                        f"[{STYLE['err']}]{ERR} 重试失败: "
                        f"{_esc(result.get('error', '未知原因'))}[/{STYLE['err']}]"
                    )
        elif parts[0] == "/quit":
            return True
        elif parts[0] == "/help":
            _console.print(render_help())
        else:
            _console.print(
                f"[{STYLE['err']}]{ERR} 未知命令: {_esc(parts[0])}（/help 查看）[/{STYLE['err']}]"
            )
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: PASS. Existing command tests must still pass: `test_consume_commands_processes_msg_and_help` ("expanded content", "/msg <序号>", "未知命令"), `test_consume_commands_retry_dispatches` (calls == ["RETRY"]), `test_consume_commands_retry_error_not_failed` ("重试失败" + "not FAILED"), `test_consume_commands_retry_dispatch_exception` ("重试失败" + "journal busy"), `test_consume_commands_quit_requests_exit`.

- [ ] **Step 5: Commit**

```bash
git add src/tui.py test/unit/test_tui.py
git commit -m "feat: tui command feedback vocabulary and aligned /help list"
```

---

### Task 6: Terminal states + HumanRequest confirmation block (`_watch`, `_handle_human_request`)

**Files:**
- Modify: `src/tui.py` (add `render_failed_line`, `render_cancelled_line`, `render_completed_line`; rewrite `_watch` terminal branches; rewrite `_handle_human_request`; add `Panel` import)
- Test: `test/unit/test_tui.py`

**Interfaces:**
- Consumes: `render_status_line`, `render_phase_track`, `STYLE`, `OK`, `ERR`, `_esc`, `_handle_human_request` runtime dispatch contract (unchanged).
- Produces: `render_failed_line(error: object) -> str`, `render_cancelled_line() -> str`, `render_completed_line(phase: str) -> str`.

- [ ] **Step 1: Write the failing tests**

In `test/unit/test_tui.py`, update `test_watch_failed_prints_retry_hint` assertions (lines 279-281) from `"[执行失败]"` to the new plain-text failure line:

```python
    assert code == 0
    out = capsys.readouterr().out
    assert "执行失败" in out
    assert "/retry" in out
    assert "/quit" in out
```

Append terminal-state renderer tests:

```python
def test_render_failed_line_has_error_and_actions() -> None:
    """FAILED 行含失败词、可用错误与 /retry、/quit 行动；无错误时也能渲染。"""
    tui = _load_tui()
    line = tui.render_failed_line("boom")
    assert "执行失败" in line
    assert "boom" in line
    assert "/retry" in line
    assert "/quit" in line
    no_err = tui.render_failed_line(None)
    assert "执行失败" in no_err
    assert "/retry" in no_err


def test_render_completed_and_cancelled_lines() -> None:
    """完成行含 ✓ 完成与最终 DONE 轨道；取消行单独一行。"""
    tui = _load_tui()
    done = tui.render_completed_line("COMPLETED")
    assert "完成" in done
    assert "DONE" in done
    assert "已取消" in tui.render_cancelled_line()


def test_render_completed_line_track_marks_done() -> None:
    """完成行的相位轨道把 DONE 标为当前完成态。"""
    tui = _load_tui()
    assert "[bold]DONE[/bold]" in tui.render_completed_line("COMPLETED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: FAIL with `AttributeError: ... no attribute 'render_failed_line'`.

- [ ] **Step 3: Write minimal implementation**

In `src/tui.py`:

1. Add `from rich.panel import Panel` to the imports (after `from rich.markup import escape as _markup_escape`).

2. Add the three terminal-state renderers after `render_help`:

```python
def render_failed_line(error: object) -> str:
    """FAILED 终态：强失败行 + 可用错误 + /retry 与 /quit 行动。"""
    text = f"{ERR} 执行失败"
    if error:
        text += f": {_esc(error)}"
    return (
        f"[{STYLE['err']}]{text}[/{STYLE['err']}]  "
        f"[{STYLE['muted']}](/retry 重试 · /quit 退出)[/{STYLE['muted']}]"
    )


def render_cancelled_line() -> str:
    """CANCELLED 终态：一行终止说明，随后成功退出。"""
    return f"[{STYLE['warn']}]{ERR} 已取消[/{STYLE['warn']}]"


def render_completed_line(phase: str) -> str:
    """COMPLETED 终态：完成行 + 最终相位轨道。"""
    return f"[{STYLE['ok']}]{OK} 完成[/{STYLE['ok']}]  {render_phase_track(phase)}"
```

3. Rewrite `_handle_human_request` to render a bounded confirmation panel:

```python
async def _handle_human_request(runtime, request: dict) -> None:
    """把 Supervisor 的 HumanRequest 呈现为有边界的确认块并收集回答。

    沿用 rich Prompt 与 stdin 暂停机制；不改 reply payload 与默认回答行为。
    """
    _reader_paused.set()
    try:
        options = request.get("options") or []
        body_lines = [_esc(request.get("question", ""))]
        body_lines.extend(f"  {i + 1}. {_esc(opt)}" for i, opt in enumerate(options))
        panel = Panel(
            "\n".join(body_lines),
            title="需要人工确认",
            border_style="yellow",
            padding=(0, 1),
        )
        _console.print(panel)
        answer = await _ask(
            "回答", default=options[0] if options else "", choices=options
        )
        await runtime.dispatch(
            "HUMAN_REPLY",
            {"request_id": request["request_id"], "answer": answer},
        )
    finally:
        _reader_paused.clear()
```

4. Rewrite `_watch`'s state-change and terminal branches:

```python
async def _watch(runtime, project_root: Path) -> int:
    """附着轮询 STATUS + agent 消息；HumanRequest 时暂停询问。

    消息默认折叠展示（工具调用与 LLM 长文本截断）。运行中可随时输入命令：
    ``/msg <序号>`` 展开完整消息，``/help`` 查看命令（对齐 Claude Code）。
    命令经后台读线程进入 ``input_queue``（由 ``_run`` 提供），不打断轮询。
    """
    last_state: tuple[str, str, object] | None = None
    offset_by_agent: dict[str, int] = {}
    history: list[tuple[int, str, dict]] = []  # (seq, agent_id, msg)
    next_seq = 0
    sessions_dir = project_root / ".athena" / "sessions"
    while True:
        if await _consume_commands(runtime, history):
            return 0  # /quit
        status = await runtime.dispatch("STATUS", {})
        execution = _execution_of(status)
        phase = execution.get("phase", "IDLE") if execution else "IDLE"
        exec_status = execution.get("status", "-") if execution else "-"
        version = status.get("state_version")
        state = (phase, exec_status, version)
        if state != last_state:
            _console.print(render_status_line(phase, exec_status, version))
            last_state = state
            if exec_status == "FAILED":
                # FAILED 是终态但不退出：允许 /retry 重试同一 execution
                _console.print(render_failed_line(status.get("error")))
        next_seq = _print_new_messages(sessions_dir, offset_by_agent, history, next_seq)
        request = status.get("human_request")
        if isinstance(request, dict) and request.get("request_id"):
            await _handle_human_request(runtime, request)
            continue
        if execution and execution.get("status") == "CANCELLED":
            _console.print(render_cancelled_line())
            return 0
        if execution and execution.get("phase") == "COMPLETED":
            _console.print(render_completed_line(execution.get("phase")))
            return 0
        await asyncio.sleep(2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: PASS. `test_watch_failed_prints_retry_hint` and `test_watch_handles_human_request_then_completes` must still pass.

- [ ] **Step 5: Commit**

```bash
git add src/tui.py test/unit/test_tui.py
git commit -m "feat: tui terminal-state lines and HumanRequest confirmation panel"
```

---

### Task 7: Full verification suite

**Files:**
- Verify: `src/tui.py`, `src/athena/agent_messages.py`, `test/unit/test_tui.py`, `test/unit/test_main.py`

- [ ] **Step 1: Run the focused TUI module**

Run: `uv run pytest test/unit/test_tui.py -q`
Expected: PASS (all TUI tests, old + new).

- [ ] **Step 2: Run the shared-message and shared-terminal modules**

Run: `uv run pytest test/unit/test_main.py test/unit/test_cli.py -q`
Expected: PASS. These cover `athena.agent_messages` (`render_message`, `print_new_messages`, new `part_summary`) and `athena.cli._attach_until_terminal` which shares terminal behavior. No CLI/main behavior changed, so all should stay green.

- [ ] **Step 3: Broader regression sweep**

Run: `uv run pytest test/unit -q`
Expected: PASS. If a failure appears, it must be from a test that depended on the removed `_describe_progress` or the old TUI strings; fix only that test to match the new rendering and re-run.

- [ ] **Step 4: Commit any fixes**

If a fix was needed, commit it with a descriptive message scoped to the touched files:

```bash
git add src/tui.py src/athena/agent_messages.py test/unit/test_tui.py test/unit/test_main.py
git commit -m "fix: align remaining tests with codex-inline TUI rendering"
```

(If no fixes were needed, skip this step.)
