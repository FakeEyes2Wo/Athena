# Universal `continue` Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make exact plain-text `continue` resume Athena's current durable task across every official Python-runtime UI and caller without re-entering task clarification.

**Architecture:** A pure resume-contract module owns exact command matching and durable-state eligibility. `ResearchRuntime.resume_current_task()` becomes the single mutation path used by `/resume`, plain `continue`, legacy `start_task("continue")`, GUI RPC, TUI, and CLI. Additive state-event fields let React render resumability without guessing from local activity; browser and Tauri continue to share one bridge.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, pytest/pytest-asyncio, React 18, TypeScript 5.5, Vitest/Testing Library, Rust/Tauri protocol contract tests.

## Global Constraints

- Before each implementation task, read `codex_docs/CURRENT.md`, this plan, and `docs/superpowers/specs/2026-09-03-universal-continue-resume-design.md`.
- The previously active LLM task-understanding output plan is explicitly paused while this user-prioritized fix is active. Do not implement both plans in the same worktree.
- Preserve all unrelated staged and unstaged changes. Several runtime and gateway paths are already dirty under the current plan; re-read their live contents and stage only the paths owned by the active task.
- Exact continuation syntax is `text.strip().casefold() == "continue"`. `/resume` remains canonical. Do not match substrings or add other language aliases.
- `continue research`, `continue three more attempts`, and any other multiword phrase retain their current task/guidance behavior.
- Resume must preserve `task_text`, `task_understanding`, the clarification draft and revision, `TASK_CLARIFICATION.md`, and `handoff_refs["task_clarification"]`.
- STOPPED and COMPLETED are terminal. A fresh taskless session is not resumable.
- Repeated or concurrent resume requests must create at most one live Supervisor lifecycle task.
- A runtime-scoped `LifecycleSession.resume_lock` must serialize capability checks,
  lifecycle rearming, Supervisor resume, and lifecycle start for every resume request.
- A PREPARE failure caused by unavailable or mismatched baseline authority remains
  resumable. Resume re-enters the same authoritative PREPARE gate; it never rebuilds
  or trusts a local baseline to bypass authority. If authority is still unavailable,
  the original typed authority failure is surfaced again without clarification or
  checkpoint-schema mutation.
- Browser WebSocket and native Tauri must use the same `resume` RPC; do not add transport-specific state machines.
- State-event additions must be backward compatible and require no checkpoint migration.
- The independent TypeScript AutoResearch/DSH state machines receive compatibility verification only; do not redesign their persistence in this plan.
- Every production change follows test-driven development: write a focused failing test, observe the intended failure, implement the minimum change, then rerun focused and adjacent suites.

## File map

- Create `src/athena/research/runtime/resume_contract.py`: exact alias matcher, typed capability, pure durable-state classification, and stable unavailable error.
- Create `test/unit/research/test_resume_contract.py`: pure matching and state-matrix tests.
- Modify `src/athena/research/runtime/control.py`: one authoritative resume operation and message routing.
- Modify `src/athena/research/runtime/facade.py`: public `resume_current_task()` and early `start_task("continue")` routing.
- Modify `src/athena/research/runtime/services.py`: runtime-scoped resume serialization lock.
- Modify `src/athena/research/supervisor/events.py`: additive resume fields on `StateEvent`.
- Modify `src/athena/research/runtime/event_projection.py`: project the pure capability.
- Modify `test/unit/research/test_breakpoint_resume.py`, `test/integration/research/test_task_seeding.py`, and `test/integration/research/test_task_confirmation_gate.py`: runtime and clarification invariants.
- Modify `src/athena/gui/service.py`, `tests/test_gui_gateway_handler.py`, `tests/test_gui_gateway_transport.py`, and `tests/test_gui_protocol_contract.py`: RPC convergence and transport error/state contracts.
- Modify `src/gui_gateway/handler.py`: restored-session detection uses the public runtime state path.
- Modify `athena-gui/src/types/ui.ts`: frontend resume projection.
- Modify `athena-gui/src/hooks/usePipeline.ts`: exact alias routing and authoritative resume state.
- Modify `athena-gui/src/hooks/__tests__/usePipeline.test.tsx` and `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`: hook routing and restored-session tests.
- Modify `athena-gui/src/components/conversation/RunControls.tsx` and its component tests: enable Continue for resumable error/interrupted states.
- Modify `src/athena_tui/render.py`, `test/unit/athena_tui/test_render.py`, `test/unit/athena_tui/test_app.py`, and `test/integration/test_tui_protocol.py`: TUI help and end-to-end alias behavior.
- Modify `src/athena/cli.py` and `test/unit/test_cli.py`: `continue` CLI alias.
- Modify `README.md`, `docs/athena-guide/README.md`, `docs/athena-guide/06-workflow.md`, and `docs/athena-gui-design.md`: user and protocol documentation.
- Create `codex_docs/2026-09-03-universal-continue-resume-completion-report.md` during closeout.

## Execution preflight

- [x] Record `git status --short`, `git diff --cached --name-status`, and `git diff --name-only` before editing.
- [x] Confirm `codex_docs/CURRENT.md` points to this plan. If it points elsewhere, stop implementation without changing code.
- [x] Record overlap between current dirty files and the file map above. Preserve every pre-existing diff and do not stage it unless the active task explicitly owns the path.
- [x] Run the current focused baseline and record exact counts in plan notes:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/test_breakpoint_resume.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/athena_tui/test_app.py test/integration/test_tui_protocol.py test/unit/test_cli.py
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.events.test.tsx src/components/__tests__/conversation-pane.test.tsx src/lib/__tests__/tauri-bridge.test.ts
```

- [x] Save the observed reproduction: a failed confirmed run followed by React composer input `continue` calls `task_clarification_start("continue")` and returns `different_task`.

Preflight evidence (2026-09-04, latest `origin/main` `2713f53`): Python baseline
`145 passed in 15.35s`; React baseline `4 files / 62 tests passed in 3.28s`.
There were no tracked or staged implementation diffs. Six restored untracked paths
(`dialog.rs`, `docs/plans/`, `exp_docs.py`, `baseline_gate.py`, and their two tests)
do not overlap this plan's owned implementation paths and remain unstaged. Observed
reproduction: confirmed PREPARE failed with `baseline authority unavailable`; submitting
exact `continue` entered task understanding, called
`task_clarification_start("continue")`, and raised `different_task`.

---

### Task 1: Pure resume command and capability contract

**Files:**
- Create: `src/athena/research/runtime/resume_contract.py`
- Create: `test/unit/research/test_resume_contract.py`

**Interfaces:**
- Produces: `ResumeReason`, `ResumeCapability`, `ResearchControlError`, `is_continue_command(text: str) -> bool`, and `resume_capability(state: object) -> ResumeCapability`.
- Consumed by: runtime control, state-event projection, and focused tests in later tasks.

- [x] **Step 1: Write failing exact-match tests**

Add this table-driven test:

```python
import pytest

from athena.research.runtime.resume_contract import is_continue_command


@pytest.mark.parametrize("text", ["continue", " Continue ", "CONTINUE\n"])
def test_exact_continue_is_a_resume_command(text: str) -> None:
    assert is_continue_command(text) is True


@pytest.mark.parametrize(
    "text",
    ["", "/resume", "continue research", "continue three more attempts", "继续"],
)
def test_non_exact_continue_text_keeps_its_existing_meaning(text: str) -> None:
    assert is_continue_command(text) is False
```

- [x] **Step 2: Write failing durable-state matrix tests**

Use `ResearchState` instances and assert these exact results:

```python
@pytest.mark.parametrize(
    ("status", "phase", "has_task", "available", "reason"),
    [
        ("RUNNING", "SEARCH", True, False, "already_running"),
        ("WAITING", "PREPARE", True, True, "paused"),
        ("FAILED", "SEARCH", True, True, "failed"),
        ("FAILED", "SEARCH", False, False, "no_task"),
        ("IDLE", "PREPARE", True, True, "interrupted"),
        ("IDLE", "SEARCH", True, True, "interrupted"),
        ("IDLE", "VALIDATE", True, True, "interrupted"),
        ("IDLE", "PREPARE", False, False, "no_task"),
        ("RUNNING", "SEARCH", False, False, "no_task"),
        ("STOPPED", "SEARCH", True, False, "stopped"),
        ("COMPLETED", "COMPLETED", True, False, "completed"),
        ("RUNNING", "COMPLETED", True, False, "completed"),
    ],
)
def test_resume_capability_matrix(
    status: str,
    phase: str,
    has_task: bool,
    available: bool,
    reason: str,
) -> None:
    state = ResearchState(
        status=status,
        phase=phase,
        search_limit=3,
        concurrency=1,
    )
    if has_task:
        state.task_text = "original task"
    assert resume_capability(state) == ResumeCapability(available, reason)
```

Add a separate legacy test where `task_text=None` and
`task_understanding={"title": "legacy task"}` remains resumable.

- [x] **Step 3: Run the tests and confirm the missing-module failure**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/test_resume_contract.py
```

Expected: collection fails because `athena.research.runtime.resume_contract` does not
exist. Fix only accidental import or syntax errors until this is the failure.

- [x] **Step 4: Implement the minimal pure contract**

Implement immutable types and stable error metadata:

```python
from dataclasses import dataclass
from typing import Literal

ResumeReason = Literal[
    "already_running",
    "paused",
    "failed",
    "interrupted",
    "no_task",
    "stopped",
    "completed",
]


@dataclass(frozen=True)
class ResumeCapability:
    available: bool
    reason: ResumeReason


class ResearchControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.retryable = False
        super().__init__(f"{code}: {message}")


def is_continue_command(text: str) -> bool:
    return text.strip().casefold() == "continue"
```

`resume_capability` must check terminal states first, then durable task evidence, then
map WAITING/FAILED/IDLE. A RUNNING state with task evidence returns `already_running`;
process-local lifecycle identity is deliberately handled only by the mutating runtime
layer and is not an input to this pure projection.

- [x] **Step 5: Verify, format, and commit Task 1**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/test_resume_contract.py
.venv\Scripts\python.exe -m black src/athena/research/runtime/resume_contract.py test/unit/research/test_resume_contract.py
git diff --check -- src/athena/research/runtime/resume_contract.py test/unit/research/test_resume_contract.py
```

Expected: all contract tests pass. Stage only these two paths and commit:

```powershell
git add src/athena/research/runtime/resume_contract.py test/unit/research/test_resume_contract.py
git commit -m "feat: define durable task resume contract"
```

---

### Task 2: One authoritative Python runtime resume path

**Files:**
- Modify: `src/athena/research/runtime/control.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `src/athena/research/runtime/services.py`
- Modify: `test/unit/research/test_breakpoint_resume.py`
- Modify: `test/integration/research/test_task_seeding.py`
- Modify: `test/integration/research/test_task_confirmation_gate.py`

**Interfaces:**
- Consumes: `is_continue_command`, `resume_capability`, and `ResearchControlError` from Task 1.
- Produces: `resume_current_task(runtime: Any) -> Awaitable[str]` and `ResearchRuntime.resume_current_task() -> Awaitable[str]`.
- Preserves: public `message`, `start_task`, and `/resume` return values on successful control operations.

- [ ] **Step 1: Add failing runtime alias tests**

In `test_breakpoint_resume.py`, add a real lifecycle regression that fails its first
PREPARE attempt, then submits plain text:

```python
@pytest.mark.asyncio
async def test_plain_continue_restarts_failed_current_task_without_reseeding(tmp_path):
    runtime, restarted = failed_prepare_runtime(tmp_path)
    original_task = runtime.state.task_text
    original_understanding = dict(runtime.state.task_understanding or {})

    assert await runtime.message("  Continue  ") == "RUNNING"
    await asyncio.wait_for(restarted.wait(), timeout=1)

    assert runtime.state.task_text == original_task
    assert runtime.state.task_understanding == original_understanding
```

Add tests proving:

- `/resume`, `message("continue")`, and `start_task("continue")` call the same resume
  primitive;
- a reloaded `IDLE`/PREPARE checkpoint with task evidence starts the lifecycle;
- repeated `continue` while the restarted task is live is idempotent;
- two resume coroutines synchronized with a barrier create at most one new lifecycle
  task and both observe `RUNNING`;
- FAILED while the old lifecycle is still unwinding an actual exception waits for that
  task with `asyncio.gather(..., return_exceptions=True)`, then starts one replacement;
- STOPPED, COMPLETED, and taskless IDLE raise `ResearchControlError` with code
  `resume_unavailable` and do not start a lifecycle;
- `continue research` and `continue three more attempts` still reach
  `supervisor.message` unchanged.

- [ ] **Step 2: Add failing confirmed-contract immutability test**

In `test_task_confirmation_gate.py`, snapshot all confirmation-owned values before
resume:

```python
before = {
    "task_text": runtime.state.task_text,
    "understanding": dict(runtime.state.task_understanding or {}),
    "handoff_refs": dict(runtime.state.handoff_refs),
    "draft": runtime.clarification_path.read_bytes(),
    "handoff": runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes(),
}

await runtime.message("continue")

assert runtime.state.task_text == before["task_text"]
assert runtime.state.task_understanding == before["understanding"]
assert runtime.state.handoff_refs == before["handoff_refs"]
assert runtime.clarification_path.read_bytes() == before["draft"]
assert runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes() == before["handoff"]
```

Also instrument the clarification controller so any call to `start_or_resume` fails the
test. This proves the success is not merely compatible output from a regenerated draft.

Add an authoritative-baseline regression: fail confirmed PREPARE with
`BaselineAuthorityError`, then resume once after making authority available and once
while it remains unavailable. The successful attempt must re-enter PREPARE without
rebuilding the existing baseline; the persistent failure must surface the same typed
authority error. In both cases, task/draft/handoff bytes stay unchanged and
clarification is never invoked. `resume_current_task()` remains fire-and-forget and
returns `RUNNING`; the test awaits the replacement lifecycle task to assert
`BaselineAuthorityError`, while GUI tests assert the later state/error event.

- [ ] **Step 3: Run focused tests and observe the current misrouting**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/test_breakpoint_resume.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py
```

Expected: plain `continue` is forwarded as prose or reseeded, unavailable states are not
typed, and the new public method is absent.

- [ ] **Step 4: Implement the authoritative operation**

In `control.py`:

1. Export `resume_current_task` instead of keeping the operation private.
2. Acquire `runtime.session.lifecycle.resume_lock` before capability classification and
   hold it through any rearm, `supervisor.resume(...)`, and `runtime.start()` call.
3. Return `RUNNING` without mutation only when durable status is RUNNING and a lifecycle
   task is live. If status is FAILED but the old task has not finished unwinding, await
   its completion under the lock with
   `await asyncio.gather(old_task, return_exceptions=True)`, verify it is done, and then
   rearm. Do not directly await and re-raise the old phase exception.
4. Reject unavailable terminal/taskless states before touching Supervisor, agents, Git,
   or survey infrastructure.
5. For a done or absent lifecycle task, call `supervisor.resume(restarting=True)` and
   `runtime.start()` exactly once.
6. For a live paused lifecycle, call `supervisor.resume()` without spawning another
   phase task.
7. Recognize plain `continue` in `_control_command` alongside canonical `/resume`.

Add `resume_lock: asyncio.Lock = field(default_factory=asyncio.Lock)` to
`LifecycleSession`. The lock is process-local and requires no persisted-state migration.

The control branch must precede auto-seeding:

```python
async def message(runtime: Any, text: str) -> str:
    command_result = await _control_command(runtime, text.strip())
    if command_result is not None:
        return command_result
    if runtime.config.auto_seed_task and not runtime.session.lifecycle.started:
        return await start_task(runtime, text.strip())
    return await _send_guidance(runtime, text)
```

In `facade.py`, detect the alias before `seed_unconfirmed_task`:

```python
async def start_task(self, task: str) -> str:
    if is_continue_command(task):
        return await self.resume_current_task()
    if self.state.task_understanding is None:
        await seed_unconfirmed_task(self, task)
    return await start_task_impl(self, task)

async def resume_current_task(self) -> str:
    return await resume_current_task_impl(self)
```

- [ ] **Step 5: Verify all runtime cases and commit Task 2**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/test_resume_contract.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py test/integration/research/test_human_plan_boundary.py
.venv\Scripts\python.exe -m black src/athena/research/runtime/control.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py
git diff --check -- src/athena/research/runtime/control.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py
```

Expected: exact continuation cases pass and existing multiword guidance tests remain
green. Review the live diff carefully because `control.py` and `facade.py` may contain
pre-existing active-plan edits. Stage only Task 2 hunks, then commit:

```powershell
git add src/athena/research/runtime/control.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py
git commit -m "fix: resume current task from continue input"
```

---

### Task 3: Authoritative resume availability in state events and GUI RPC

**Files:**
- Modify: `src/athena/research/supervisor/events.py`
- Modify: `src/athena/research/runtime/event_projection.py`
- Modify: `src/athena/gui/service.py`
- Modify: `src/gui_gateway/handler.py`
- Modify: `test/unit/research/supervisor/test_events.py`
- Modify: `tests/test_gui_gateway_handler.py`
- Modify: `tests/test_gui_gateway_transport.py`
- Modify: `tests/test_gui_protocol_contract.py`

**Interfaces:**
- Consumes: `resume_capability(state)` and `ResearchRuntime.resume_current_task()`.
- Produces: `StateEvent.resume_available: bool`, `StateEvent.resume_reason: str | None`, and unchanged GUI method name `resume`.

- [ ] **Step 1: Add failing state-projection tests**

Add tests for these snapshots:

```python
event = supervisor_state(supervisor_with("IDLE", "PREPARE", task="original"), 0)
assert event.resume_available is True
assert event.resume_reason == "interrupted"

fresh = supervisor_state(supervisor_with("IDLE", "PREPARE", task=None), 0)
assert fresh.resume_available is False
assert fresh.resume_reason == "no_task"
```

Also cover WAITING, FAILED, RUNNING, STOPPED, COMPLETED, and a legacy understanding-only
checkpoint. Verify `StateEvent.model_validate` accepts an older payload that omits both
new fields and defaults them to `False`/`None`.

Add a handler restore test whose runtime exposes only the public `state_path` property.
It must resume the selected persisted session without depending on a fake-only
`_state_path` attribute.

- [ ] **Step 2: Add failing RPC convergence tests**

Extend gateway tests so:

```python
assert await handler.dispatch("resume", {}) == {"status": "RUNNING"}
assert await handler.dispatch("message", {"text": "continue"}) == {
    "response": "RUNNING"
}
```

Both calls must record the same runtime resume operation. Add a transport test where
`ResearchControlError("resume_unavailable", ...)` becomes RPC error data containing
`{"code": "resume_unavailable", "retryable": False}`. Keep the canonical protocol
method set unchanged.

`task_clarification_start` remains an explicit low-level clarification endpoint, not a
plain-text continuation entrypoint. Its genuine `different_task` behavior is unchanged.
Tests must prove every official free-text entrypoint intercepts exact `continue` before
that endpoint and that a real different task still reaches the protection.

- [ ] **Step 3: Run focused tests and confirm missing event fields/public operation**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_events.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_protocol_contract.py
```

Expected: state field assertions and direct public resume calls fail.

- [ ] **Step 4: Implement additive projection and service routing**

Add defaulted fields to `StateEvent`:

```python
resume_available: bool = False
resume_reason: str | None = None
```

In `supervisor_state`, calculate the capability once and project both fields. Change
`GuiService.resume()` to call `self._runtime.resume_current_task()` directly; keep
`GuiService.message()` unchanged so direct text callers exercise runtime parsing. Keep
the event annotation string-based and lock allowed values with tests; importing the
runtime contract type from `supervisor/events.py` would invert the package dependency
and risk a circular import during `ResearchRuntime` composition.

Change the gateway restore check to use `runtime.state_path`. Do not add a compatibility
fallback to the nonexistent private `_state_path`; tests must represent the real facade.

- [ ] **Step 5: Verify native/WebSocket protocol parity and commit Task 3**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_events.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_protocol_contract.py tests/test_gui_gateway_e2e.py
.venv\Scripts\python.exe -m black src/athena/research/supervisor/events.py src/athena/research/runtime/event_projection.py src/athena/gui/service.py src/gui_gateway/handler.py test/unit/research/supervisor/test_events.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py
git diff --check -- src/athena/research/supervisor/events.py src/athena/research/runtime/event_projection.py src/athena/gui/service.py src/gui_gateway/handler.py test/unit/research/supervisor/test_events.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_protocol_contract.py
```

Expected: old and new state payloads validate, and both transports retain the same
`resume` method. Commit only listed paths with:

```powershell
git add src/athena/research/supervisor/events.py src/athena/research/runtime/event_projection.py src/athena/gui/service.py src/gui_gateway/handler.py test/unit/research/supervisor/test_events.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_protocol_contract.py
git commit -m "feat: project durable resume availability"
```

---

### Task 4: React browser and Tauri continuation behavior

**Files:**
- Modify: `athena-gui/src/types/ui.ts`
- Modify: `athena-gui/src/hooks/usePipeline.ts`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`
- Modify: `athena-gui/src/components/conversation/RunControls.tsx`
- Modify: `athena-gui/src/components/__tests__/conversation-pane.test.tsx`
- Modify: `athena-gui/src/lib/__tests__/tauri-bridge.test.ts`

**Interfaces:**
- Consumes: state fields `resume_available`, `resume_reason`, and existing bridge method `resumeSearch()`.
- Produces: `PipelineViewModel.resumeAvailable`, `PipelineViewModel.resumeReason`, and one shared `continueCurrentRun()` action used by composer and button.

- [ ] **Step 1: Add failing composer-routing tests**

In `usePipeline.test.tsx`, hydrate a failed PREPARE snapshot with
`resume_available: true`, then assert:

```typescript
await act(async () => {
  await result.current.sendPrompt("  Continue  ");
});

expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(1);
expect(bridgeMocks.taskClarificationStart).not.toHaveBeenCalled();
expect(result.current.viewModel.messages.filter(
  (message) => message.kind === "intent-preview",
)).toHaveLength(0);
```

Repeat through the WebSocket fallback and native invoke bridge mocks. Add negative tests
for `continue research` and `continue three more attempts`. Those phrases must follow
the existing new-task or guidance branch according to current run state.

- [ ] **Step 2: Add failing restored-session and control-state tests**

Project `resume_available` and `resume_reason` in event tests. Verify:

- an error/failed PREPARE snapshot keeps `runActive` true and exposes an enabled Continue
  button;
- a reloaded `IDLE`/PREPARE interrupted snapshot does the same;
- a taskless fresh `IDLE`/PREPARE snapshot keeps controls inactive;
- a rejected resume appends one visible error, keeps prior history/preview intact, and
  does not set status to running;
- two rapid continuation submissions share the existing in-flight guard or backend
  lifecycle guard and do not create two clarification previews.

- [ ] **Step 3: Run frontend tests and observe current clarification call**

Run:

```powershell
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.events.test.tsx src/components/__tests__/conversation-pane.test.tsx src/lib/__tests__/tauri-bridge.test.ts
```

Expected: `resume_available` is ignored, failed runs are inactive, and exact `continue`
calls `taskClarificationStart`.

- [ ] **Step 4: Implement frontend projection and shared continuation action**

Add these defaults to `PipelineViewModel`:

```typescript
resumeAvailable: false,
resumeReason: null,
```

Map every complete state snapshot without local inference or inherited values:

```typescript
next.resumeAvailable = data.resume_available === true;
next.resumeReason = typeof data.resume_reason === "string" ? data.resume_reason : null;
```

Create a local exact matcher mirroring the backend and a shared action:

```typescript
function isContinueCommand(text: string): boolean {
  return text.trim().toLowerCase() === "continue";
}

const continueCurrentRun = useCallback(async () => {
  try {
    await bridge.resumeSearch();
    runStarted.current = true;
    setViewModel((prev) => ({ ...prev, status: "running", resumeAvailable: false }));
  } catch (error) {
    appendError(errorMessage(error));
    throw error;
  }
}, [appendError]);
```

Call `continueCurrentRun` before slash-command and run/new-task classification. Use it
from `resumeRun` as well. Include `resumeAvailable` in `runActive`; update RunControls so
Continue is enabled for `paused` or `resumeAvailable`, while Pause and Stop retain their
existing safety conditions.

Add an uppercase regression to lock locale-independent matching; do not use
`toLocaleLowerCase()` because UI locale must not change the command contract. Every
complete state snapshot must normalize missing resume fields to `false`/`null` so a
legacy or fresh session cannot inherit another session's resumability. Serialize initial
session selection before applying its state snapshot, or guard it with a session-aware
epoch, so a late default-session response cannot overwrite the selected session.

Add a sequential regression that applies a resumable state followed by a legacy/fresh
payload omitting both fields and asserts `resumeAvailable === false`,
`resumeReason === null`, and a disabled Continue control.

- [ ] **Step 5: Verify frontend tests/build and commit Task 4**

Run:

```powershell
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.events.test.tsx src/components/__tests__/conversation-pane.test.tsx src/lib/__tests__/tauri-bridge.test.ts
npm --prefix athena-gui run build
git diff --check -- athena-gui/src/types/ui.ts athena-gui/src/hooks/usePipeline.ts athena-gui/src/hooks/__tests__/usePipeline.test.tsx athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx athena-gui/src/components/conversation/RunControls.tsx athena-gui/src/components/__tests__/conversation-pane.test.tsx athena-gui/src/lib/__tests__/tauri-bridge.test.ts
```

Expected: focused tests and TypeScript/Vite build pass. Commit only Task 4 paths:

```powershell
git add athena-gui/src/types/ui.ts athena-gui/src/hooks/usePipeline.ts athena-gui/src/hooks/__tests__/usePipeline.test.tsx athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx athena-gui/src/components/conversation/RunControls.tsx athena-gui/src/components/__tests__/conversation-pane.test.tsx athena-gui/src/lib/__tests__/tauri-bridge.test.ts
git commit -m "fix(gui): continue interrupted research runs"
```

---

### Task 5: TUI, CLI, and direct-caller parity

**Files:**
- Modify: `src/athena_tui/render.py`
- Modify: `test/unit/athena_tui/test_render.py`
- Modify: `test/unit/athena_tui/test_app.py`
- Modify: `test/integration/test_tui_protocol.py`
- Modify: `src/athena/cli.py`
- Modify: `test/unit/test_cli.py`

**Interfaces:**
- Consumes: runtime plain-text alias and public `resume_current_task()` from Task 2.
- Produces: documented TUI alias and CLI subcommand `continue` mapped to canonical `resume`.

- [ ] **Step 1: Add failing real-runtime TUI test**

Create a failed PREPARE runtime fixture, submit `continue` through
`TuiController.send_message`, and assert the second PREPARE attempt starts. Capture
runtime events and assert no output contains `任务理解中` and no clarification draft
revision changes. Keep the existing UI echo and composer-clear assertions.

- [ ] **Step 2: Add failing CLI alias tests**

Extend parser and dispatch tests:

```python
@pytest.mark.asyncio
async def test_continue_cli_alias_uses_resume_control(monkeypatch, capsys) -> None:
    runtime = StubRuntime()
    monkeypatch.setattr(cli, "_runtime", lambda *_args, **_kwargs: runtime)
    args = cli._build_parser().parse_args(["continue", "--project", "p"])

    assert await cli._cmd_control(args) == 0
    assert runtime.messages == ["/resume"]
    assert capsys.readouterr().out.strip() == "status=RUNNING"
```

Retain the existing `resume` test unchanged. Add a parser inventory assertion that both
commands exist.

- [ ] **Step 3: Run tests and confirm the missing CLI alias**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/athena_tui/test_render.py test/unit/athena_tui/test_app.py test/integration/test_tui_protocol.py test/unit/test_cli.py
```

Expected: runtime-level TUI continuation passes only after Task 2, while CLI parsing
fails because `continue` is not registered.

- [ ] **Step 4: Implement thin UI/caller adapters**

Update the TUI help overlay to list `continue` as the plain-text synonym for `/resume`.
Do not add TUI-local resume state logic.

Register `continue` beside `resume` in the CLI parser and normalize only at dispatch:

```python
async def _cmd_control(args: argparse.Namespace) -> int:
    runtime = _runtime(args.project)
    command = "resume" if args.command == "continue" else args.command
    try:
        status = await runtime.message(f"/{command}")
        print(f"status={status}")
        return 0
    finally:
        await runtime.aclose()
```

- [ ] **Step 5: Verify TUI/CLI and commit Task 5**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/athena_tui/test_render.py test/unit/athena_tui/test_app.py test/integration/test_tui_protocol.py test/integration/test_tui_resume.py test/unit/test_cli.py
.venv\Scripts\python.exe -m black src/athena/cli.py test/unit/test_cli.py test/integration/test_tui_protocol.py
git diff --check -- src/athena_tui/render.py test/unit/athena_tui/test_render.py test/unit/athena_tui/test_app.py test/integration/test_tui_protocol.py src/athena/cli.py test/unit/test_cli.py
```

Expected: TUI and both CLI spellings pass. Commit only Task 5 paths:

```powershell
git add src/athena_tui/render.py test/unit/athena_tui/test_render.py test/unit/athena_tui/test_app.py test/integration/test_tui_protocol.py src/athena/cli.py test/unit/test_cli.py
git commit -m "feat: expose continue resume aliases"
```

---

### Task 6: Cross-surface failure/resume acceptance coverage

**Files:**
- Create: `test/integration/research/test_continue_resume_surfaces.py`
- Modify: `tests/test_gui_gateway_e2e.py`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`

**Interfaces:**
- Exercises: confirmed task -> phase failure -> exact continuation -> same phase and task contract.
- Verifies: direct runtime, GUI message RPC, GUI resume RPC, TUI, restored session, WebSocket frontend, and native bridge share the contract.

- [ ] **Step 1: Build a hermetic phase-failure harness**

The harness must accept phase `PREPARE`, `SEARCH`, or `VALIDATE`, fail the first phase
entry, block on the second, and record lifecycle task identities. It must create a real
confirmed clarification handoff before the first phase starts and make no network calls.

- [ ] **Step 2: Add parameterized cross-surface success tests**

For every phase, assert:

```python
assert second_task is not first_task
assert runtime.state.phase == phase
assert runtime.state.status == "RUNNING"
assert runtime.state.task_text == "original task"
assert runtime.clarification_path.read_bytes() == original_draft
assert runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes() == original_handoff
```

Exercise `runtime.message("continue")`, `runtime.start_task("continue")`,
`GuiService.message("continue")`, and `GuiService.resume()` against the same harness.

- [ ] **Step 3: Add end-to-end WebSocket and native bridge assertions**

Extend gateway E2E to submit a `message` request containing `continue` after a failed
state and observe `RUNNING` plus unchanged draft ID. In frontend event tests, execute the
same hook once with `window.__TAURI_INTERNALS__` absent and once with native invoke
mocked; both must call the canonical resume method and neither may call clarification.

- [ ] **Step 4: Add negative and concurrency acceptance tests**

Cover:

- fresh IDLE, STOPPED, and COMPLETED return `resume_unavailable`;
- `continue research` remains guidance;
- a genuinely new task continues to receive `different_task`;
- two concurrent `continue` requests return one live lifecycle task;
- a second phase failure is surfaced normally and is not automatically retried.

Use a barrier inside the old check-to-create window so the concurrency test would
deterministically create two tasks without `resume_lock`; do not rely only on final
state. Add in-process FAILED and reloaded FAILED-to-IDLE PREPARE cases for baseline
authority outage and mismatch. They must retry only the authoritative gate, preserve
the existing baseline tree and clarification bytes, and either succeed when authority
recovers or surface the original authority failure again.

- [ ] **Step 5: Run cross-surface and compatibility suites**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q test/integration/research/test_continue_resume_surfaces.py tests/test_gui_gateway_e2e.py test/integration/test_tui_protocol.py
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.events.test.tsx src/hooks/__tests__/usePipeline.test.tsx src/lib/__tests__/tauri-bridge.test.ts
cargo test --manifest-path athena-rust/Cargo.toml -p athena-protocol
npm --prefix athena_ts test -- --run packages/athena-dsh/test/index.test.ts packages/athena-autoresearch/test/runtime.test.ts
git diff --check -- test/integration/research/test_continue_resume_surfaces.py tests/test_gui_gateway_e2e.py athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx
```

Expected: every affected surface passes; Rust method parity and independent TypeScript
resume contracts remain unchanged. Commit only Task 6 paths:

```powershell
git add test/integration/research/test_continue_resume_surfaces.py tests/test_gui_gateway_e2e.py athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx
git commit -m "test: cover continue resume across surfaces"
```

---

### Task 7: Documentation, full verification, and plan closeout

**Files:**
- Modify: `README.md`
- Modify: `docs/athena-guide/README.md`
- Modify: `docs/athena-guide/06-workflow.md`
- Modify: `docs/athena-gui-design.md`
- Modify: `docs/superpowers/specs/2026-09-03-universal-continue-resume-design.md`
- Create: `codex_docs/2026-09-03-universal-continue-resume-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete after all acceptance evidence exists: `docs/superpowers/plans/2026-09-03-universal-continue-resume.md`

**Interfaces:**
- Documents: exact alias semantics, state-event fields, surface matrix, terminal-state behavior, and CLI usage.
- Produces: acceptance evidence and a closed `CURRENT.md` pointer.

- [ ] **Step 1: Update user and protocol documentation**

Document these commands exactly:

```text
Composer/TUI: continue
Explicit control: /resume
CLI: Athena-cli continue --project <path>
CLI compatibility: Athena-cli resume --project <path>
```

Explain exact matching, task preservation, terminal rejection, and that multiword
guidance is not treated as a command. Add `resume_available` and `resume_reason` to the
GUI state-event contract. Mark TypeScript AutoResearch/DSH as an independent runtime
with its existing explicit resume APIs. Update the README command synopsis itself from
`status|pause|resume|stop` to `status|pause|resume|continue|stop`, not only its examples.

- [ ] **Step 2: Run formatting, static, and build checks**

Run:

```powershell
.venv\Scripts\python.exe -m black --check src/athena/research/runtime/resume_contract.py src/athena/research/runtime/control.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py src/athena/research/supervisor/events.py src/athena/research/runtime/event_projection.py src/athena/gui/service.py src/gui_gateway/handler.py test/unit/research/test_resume_contract.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_continue_resume_surfaces.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/athena_tui/test_render.py test/integration/test_tui_protocol.py src/athena/cli.py test/unit/test_cli.py
.venv\Scripts\python.exe -m compileall -q src/athena src/athena_tui src/gui_gateway
npm --prefix athena-gui run build
cargo check --manifest-path athena-gui/src-tauri/Cargo.toml
git diff --check
```

If global `git diff --check` reports an unrelated pre-existing path, record it and rerun
against every task-owned path explicitly. Do not claim the global tree is clean.

- [ ] **Step 3: Run focused, subsystem, and repository suites**

Run in this order:

```powershell
.venv\Scripts\python.exe -m pytest -q test/unit/research/test_resume_contract.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_continue_resume_surfaces.py test/integration/research/test_task_seeding.py test/integration/research/test_task_confirmation_gate.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_protocol_contract.py test/unit/athena_tui/test_render.py test/unit/athena_tui/test_app.py test/integration/test_tui_protocol.py test/integration/test_tui_resume.py test/unit/test_cli.py
npm --prefix athena-gui test -- --run
cargo test --manifest-path athena-rust/Cargo.toml -p athena-protocol
npm --prefix athena_ts test -- --run packages/athena-dsh/test/index.test.ts packages/athena-autoresearch/test/runtime.test.ts
.venv\Scripts\python.exe -m pytest -q test/unit/research test/integration/research tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py tests/test_gui_protocol_contract.py test/unit/athena_tui test/integration/test_tui_protocol.py test/integration/test_tui_resume.py test/unit/test_cli.py
.venv\Scripts\python.exe -m pytest -q
```

Record timestamp, exit code, pass/fail/skip count, and duration for every command. Any
failure keeps the plan open and must be reported by exact test name.

- [ ] **Step 4: Audit every official caller from fresh source evidence**

Run:

```powershell
rg -n "start_task\(|\.message\(|resume_current_task|resume_search|resumeSearch|research_resume|async resume\(" src athena-gui athena-rust athena_ts examples
rg -n "taskClarificationStart\(content\)|task_clarification_start.*continue|isContinueCommand|is_continue_command" src athena-gui test tests
```

Build a completion-report table for React browser, React Tauri, TUI, CLI, GUI RPC,
direct Python, session restore, Rust protocol, TypeScript DSH, and TypeScript
AutoResearch. For each row record implementation path, test evidence, and whether it is
affected or compatibility-only.

- [ ] **Step 5: Write completion evidence and close the plan**

The completion report must contain:

```markdown
# Universal Continue Resume Completion Report

Date: 2026-09-03
Implementation commits:

## Delivered behavior
## Caller and UI coverage
## Acceptance criteria evidence
## Verification commands and results
## Persisted-state and clarification immutability audit
## Remaining known failures
## Unrelated worktree changes preserved
```

List actual commit hashes, mark the design spec `implemented and verified`, and delete
this plan only after all ten acceptance criteria have fresh evidence. Update
`codex_docs/CURRENT.md` so it no longer points at the deleted plan and names the new
completion report as most recent completed work. Preserve the separately paused JW-SSD
plan entry.

- [ ] **Step 6: Commit only documentation and closeout paths**

Review the staged path list before committing:

```powershell
git add README.md docs/athena-guide/README.md docs/athena-guide/06-workflow.md docs/athena-gui-design.md docs/superpowers/specs/2026-09-03-universal-continue-resume-design.md codex_docs/2026-09-03-universal-continue-resume-completion-report.md codex_docs/CURRENT.md docs/superpowers/plans/2026-09-03-universal-continue-resume.md
git diff --cached --name-status
git commit -m "docs: complete universal continue resume"
```

Expected: only documentation, completion report, `CURRENT.md`, and the plan deletion are
staged. Report the final hash and all fresh verification evidence.
