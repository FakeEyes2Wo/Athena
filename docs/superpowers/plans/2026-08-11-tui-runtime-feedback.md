# Athena TUI Runtime Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Codex-inline TUI truthful and quieter while allowing DataAgent to repair a generated analysis script after a deterministic runtime failure.

**Architecture:** Preserve Supervisor state transitions and rollout storage. Fix command environment construction at the generic-tool boundary, add a bounded generate-run-repair loop inside DataAgent, carry full worker failure text through ThreadRuntime and STATUS, and render compact TUI activity based on semantic outcomes rather than raw message boundaries.

**Tech Stack:** Python 3.11+, asyncio subprocesses, Rich, SQLite, Pydantic, pytest.

## Global Constraints

- Do not change Supervisor phase gates, persisted rollout format, or slash-command semantics.
- Do not hard-code a Matplotlib-specific patch; feed the actual stderr back to the same DataAgent so other deterministic script errors can be repaired.
- Script repair is bounded to two repair turns after the initial generation.
- Full failure reasons may be truncated only at the persistence/display boundary if needed for safety; the exception type and actionable tail must remain visible.
- TUI color is supplemental. Non-zero return codes must display `×` even when stderr does not contain the word `error`.
- Preserve terminal scrollback and inline rendering; no new dependencies.
- Work with existing user changes and stage only files touched by each task.

---

### Task 1: Preserve the Windows command environment

**Files:**
- Modify: `src/athena/agents/tools/generic_tools.py`
- Test: `test/unit/agent/test_generic_tools.py`

**Interfaces:**
- Consumes: host `os.environ`, `sys.executable`.
- Produces: `_shell_env() -> dict[str, str]` containing uppercase allowlisted keys and a `PATH` prefixed by the active interpreter directory.

- [ ] **Step 1: Write the failing test**

```python
def test_shell_env_accepts_windows_mixed_case_path(monkeypatch) -> None:
    host = {"Path": r"C:\\Windows\\System32", "SystemRoot": r"C:\\Windows"}
    monkeypatch.setattr(generic_tools.os, "environ", host)
    env = generic_tools._shell_env()
    assert env["SYSTEMROOT"] == r"C:\\Windows"
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).resolve().parent)
    assert r"C:\\Windows\\System32" in env["PATH"]
```

- [ ] **Step 2: Run it and verify RED**

Run: `uv run pytest test/unit/agent/test_generic_tools.py::test_shell_env_accepts_windows_mixed_case_path -q`

Expected: FAIL because `_shell_env()` omits mixed-case `Path` and `SystemRoot`.

- [ ] **Step 3: Implement case-insensitive environment normalization**

```python
def _shell_env() -> dict[str, str]:
    allowed = {name.upper() for name in _HOST_ALLOW}
    env = {key.upper(): value for key, value in os.environ.items() if key.upper() in allowed}
    scripts = str(Path(sys.executable).resolve().parent)
    inherited = env.get("PATH", "")
    entries = inherited.split(os.pathsep) if inherited else []
    if scripts not in entries:
        env["PATH"] = os.pathsep.join([scripts, *entries])
    return env
```

- [ ] **Step 4: Run focused generic-tool tests**

Run: `uv run pytest test/unit/agent/test_generic_tools.py -q`

- [ ] **Step 5: Commit only the two task files**

```powershell
git add src/athena/agents/tools/generic_tools.py test/unit/agent/test_generic_tools.py
git commit -m "fix: preserve mixed-case Windows shell environment"
```

---

### Task 2: Add a bounded DataAgent script-repair loop

**Files:**
- Modify: `src/athena/agents/data_agent.py`
- Modify: `src/athena/core/agent/prompts/data_agent.md`
- Test: `test/unit/agent/test_data_agent.py`

**Interfaces:**
- Produces: `_run_script(workspace: Path, data_path: str, target: str) -> tuple[int, str, str]` and `_repair_prompt(returncode: int, stdout: str, stderr: str) -> str`.
- Behavior: initial inner run plus at most `_MAX_SCRIPT_REPAIRS = 2` repair turns on non-zero exit.

- [ ] **Step 1: Write a failing repair-success test**

Create a lightweight fake inner agent with a real `tools` attribute. Its first call leaves an `analysis.py` that exits with `TypeError: labels`; its second call asserts the repair prompt contains that stderr and rewrites the script to create `report.md` and `figures/plot.png`. Assert `DataAgent.run()` completes and the inner agent was called twice.

```python
class RepairingInner:
    def __init__(self, workspace: Path) -> None:
        self.tools = ToolRegistry()
        self.workspace = workspace
        self.inputs: list[str] = []

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self.inputs.append(ctx.input_text or "")
        if len(self.inputs) == 2:
            assert "TypeError: labels" in self.inputs[-1]
            (self.workspace / "analysis.py").write_text(
                "from pathlib import Path\n"
                "Path('report.md').write_text('# EDA Report')\n"
                "Path('figures').mkdir(exist_ok=True)\n"
                "Path('figures/plot.png').write_bytes(b'png')\n",
                encoding="utf-8",
            )
        return AgentOutcome(result_ref="inner://done")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest test/unit/agent/test_data_agent.py::test_data_agent_repairs_failed_generated_script -q`

Expected: FAIL with `analysis.py failed` after one execution.

- [ ] **Step 3: Implement the minimal repair loop**

Build the inner agent once, run the initial generation, then execute the script. For each non-zero result while repairs remain, call the same inner agent with a new `AgentContext` sharing thread, turn, emit, tools, cancel, and memory but using this input:

```text
The generated analysis.py failed when the framework executed it.
Exit code: <code>
stdout:
<stdout>
stderr:
<stderr>
Edit analysis.py in the existing workspace to fix this exact failure. Do not only explain the fix and do not run the script yourself.
```

After the final failed attempt, raise `RuntimeError` containing the final exit code and stderr.

- [ ] **Step 4: Add and verify an exhaustion test**

Use an always-failing script and a no-op inner agent. Assert three total script executions (initial plus two repairs), three inner calls, and the final exception includes the script stderr.

Run: `uv run pytest test/unit/agent/test_data_agent.py -q`

- [ ] **Step 5: Update the DataAgent prompt**

Replace “the framework runs it exactly once” with the bounded contract: the framework runs the script; if it fails, it may return exact stdout/stderr and request an in-place repair. The agent must edit `analysis.py`, not merely explain or invoke it itself.

- [ ] **Step 6: Commit only DataAgent, its prompt, and tests**

```powershell
git add src/athena/agents/data_agent.py src/athena/core/agent/prompts/data_agent.md test/unit/agent/test_data_agent.py
git commit -m "fix: let DataAgent repair failed analysis scripts"
```

---

### Task 3: Carry actionable worker failures into STATUS

**Files:**
- Modify: `src/athena/app_server/submissions.py`
- Modify: `src/athena/app_server/thread_runtime.py`
- Modify: `src/athena/core/agent/agent_runtime.py`
- Modify: `src/athena/research/supervisor/journal.py`
- Modify: `src/athena/research/runtime.py`
- Test: `test/unit/app_server/test_thread_runtime.py`
- Test: `test/unit/agent/test_agent_runtime.py`
- Test: `test/unit/research/test_supervisor_journal.py`
- Test: `tests/test_research_runtime.py`

**Interfaces:**
- Extend `RunnerFailed` and `TurnTerminalState` with `error_message: str | None = None`.
- Add `PlanJournal.latest_failure(execution_id: str) -> str | None`.
- STATUS `error` equals the latest failed operation error for a failed execution, otherwise `None`.

- [ ] **Step 1: Write failing tests for terminal error preservation**

Use a runner that raises `RuntimeError("analysis.py failed: TypeError: labels")`. Assert `wait_turn()` and `AgentRuntime.wait_run()` expose both `RuntimeError` and `TypeError: labels`, not only the exception class.

- [ ] **Step 2: Verify terminal tests RED**

Run: `uv run pytest test/unit/app_server/test_thread_runtime.py test/unit/agent/test_agent_runtime.py -q`

- [ ] **Step 3: Preserve type and message across runtime signals**

In the runner exception branch, emit:

```python
RunnerFailed(
    turn_id=turn.turn_id,
    exception_type=type(exc).__name__,
    error_message=str(exc),
)
```

Pass both fields through `commit_failed` and `TurnTerminalState`. Format the public error as `<exception_type>: <error_message>` while keeping observability metadata for the exception type.

- [ ] **Step 4: Write and verify RED tests for journal/STATUS error projection**

Persist a failed plan operation with `error="RuntimeError: analysis.py failed"`. Assert `latest_failure(execution_id)` returns it. In a runtime STATUS test, assert a FAILED execution returns the same string under `error` and a RUNNING execution returns `None`.

- [ ] **Step 5: Implement latest failure projection**

Query the newest failed operation joined through plans for the requested execution, ordered by plan sequence and operation rowid descending. Pass that value into `_status_fields` only when control status is `FAILED`.

- [ ] **Step 6: Run the focused runtime suites**

Run: `uv run pytest test/unit/app_server/test_thread_runtime.py test/unit/agent/test_agent_runtime.py test/unit/research/test_supervisor_journal.py tests/test_research_runtime.py -q`

- [ ] **Step 7: Commit only the runtime error-propagation files**

```powershell
git add src/athena/app_server/submissions.py src/athena/app_server/thread_runtime.py src/athena/core/agent/agent_runtime.py src/athena/research/supervisor/journal.py src/athena/research/runtime.py test/unit/app_server/test_thread_runtime.py test/unit/agent/test_agent_runtime.py test/unit/research/test_supervisor_journal.py tests/test_research_runtime.py
git commit -m "fix: surface worker failure details in research status"
```

---

### Task 4: Reduce TUI noise and render tool outcomes truthfully

**Files:**
- Modify: `src/tui.py`
- Modify: `src/athena/agent_messages.py`
- Test: `test/unit/test_tui.py`
- Test: `test/unit/test_main.py`

**Interfaces:**
- Extend `part_summary` to preserve structured tool-return content or add `tool_return_failed(content: object) -> bool`.
- `render_activity_block(..., show_header: bool | None = None)` omits the large `◆` header for tool-only messages but retains `#<seq>` on the compact tool line.
- `_watch` prints status only when `(phase, control_status)` changes, not for version-only changes.

- [ ] **Step 1: Write failing tests for non-zero tool results**

Assert tool returns with `{"returncode": 127, ...}` and `{"returncode": 1, ...}` render `↳ ×`; a zero return code renders `↳ ✓`.

- [ ] **Step 2: Write a failing compact-tool-message test**

Assert a message containing only a tool call or tool return does not begin with `◆`, still contains `#7`, and remains expandable via `/msg 7`.

- [ ] **Step 3: Write a failing status-deduplication test**

Feed STATUS snapshots `PREPARE/RUNNING/v1`, `PREPARE/RUNNING/v3`, then `SEARCH/RUNNING/v4`. Assert only two phase-track lines are printed and SEARCH is the second.

- [ ] **Step 4: Run the focused tests and verify RED**

Run: `uv run pytest test/unit/test_tui.py test/unit/test_main.py -q`

- [ ] **Step 5: Implement semantic tool-result classification and compact blocks**

Treat a structured integer `returncode != 0` as failure before string heuristics. Tool-only messages render as `↳ <symbol> ...  [#<seq>]`; user/model messages retain the `◆ <agent> #<seq>` header.

- [ ] **Step 6: Deduplicate status by phase/control status**

Track `(phase, exec_status)` in `_watch`. Keep version in the rendered transition line but do not emit a new line for a version-only snapshot.

- [ ] **Step 7: Run TUI and shared CLI tests**

Run: `uv run pytest test/unit/test_tui.py test/unit/test_main.py test/unit/test_cli.py -q`

- [ ] **Step 8: Commit only TUI/message files and tests**

```powershell
git add src/tui.py src/athena/agent_messages.py test/unit/test_tui.py test/unit/test_main.py
git commit -m "fix: make inline TUI quieter and outcome-aware"
```

---

### Task 5: Verification

**Files:** No production changes expected.

- [ ] **Step 1: Run formatting and focused tests**

Run: `uv run black --check src/athena/agents/tools/generic_tools.py src/athena/agents/data_agent.py src/athena/app_server/submissions.py src/athena/app_server/thread_runtime.py src/athena/core/agent/agent_runtime.py src/athena/research/supervisor/journal.py src/athena/research/runtime.py src/athena/agent_messages.py src/tui.py`

Run: `uv run pytest test/unit/agent/test_generic_tools.py test/unit/agent/test_data_agent.py test/unit/app_server/test_thread_runtime.py test/unit/agent/test_agent_runtime.py test/unit/research/test_supervisor_journal.py tests/test_research_runtime.py test/unit/test_tui.py test/unit/test_main.py test/unit/test_cli.py -q`

- [ ] **Step 2: Run broader regression tests**

Run: `uv run pytest test/unit/agent test/unit/app_server test/unit/research test/unit/test_tui.py test/unit/test_main.py test/unit/test_cli.py tests/test_research_runtime.py -q`

- [ ] **Step 3: Inspect the diff and working tree**

Run: `git diff --check` and `git status --short`. Confirm unrelated user changes remain untouched.
