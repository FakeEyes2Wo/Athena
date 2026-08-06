# Execution Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mixed `athena.execution` package with a typed, passive execution monitor and connect Core Agent progress events to app-server health journaling.

**Architecture:** `athena.execution` owns immutable source-independent events and a multi-execution state machine driven by an injected monotonic clock. `athena.app_server` adapts existing turn and Agent events, owns the periodic scanner task, and writes derived state changes to the existing `EventJournal`; SEARCH policy remains under `athena.experiment`.

**Tech Stack:** Python 3.11, `asyncio`, frozen slotted dataclasses, `StrEnum`, pytest, pytest-asyncio, existing Athena Core Agent and app-server runtime.

## Global Constraints

- The monitor emits observations only; it never cancels, retries, pauses, repairs, or mutates a workflow.
- Do not create a Handler class, protocol, factory, or module. Leave one concise extension comment at the app-server health sink.
- `advances_progress` is supplied explicitly by each event-source adapter; the monitor never infers it from payload content.
- `STALLED` uses time since effective progress; `TIMEOUT` uses total execution duration.
- `TIMEOUT` is non-final and may be followed only by `COMPLETED` or `FAILED`; progress cannot restore it to `RUNNING`.
- `COMPLETED` and `FAILED` are final states.
- Delete obsolete execution imports immediately; do not add compatibility re-exports.
- Connect Core Agent/app-server now. Leave CodeEngine, Codex/Qoder streaming, and script output integration unchanged.
- Preserve all unrelated dirty-worktree changes. Incorporate the existing Supervisor and monitor comments when their old files are removed.
- Do not request external Agent or research help without user approval.

---

## File Structure

- Create `src/athena/execution/events.py`: event, state, limits, snapshot, health event, JSON metadata, and sink types.
- Create `src/athena/execution/monitor.py`: passive state machine, clock accounting, scan loop, error isolation, lookup, removal, and retention.
- Modify `src/athena/execution/__init__.py`: export only the new observability API.
- Create `src/athena/app_server/observability.py`: source-side Agent event mapping, scan-task ownership, and Journal sink.
- Modify `src/athena/app_server/thread_runtime.py`: forward turn lifecycle and Agent events to the observer without changing execution control.
- Modify `src/athena/app_server/thread_manager.py`: stop inheriting the deleted ABC and pass monitor timing configuration to runtimes.
- Modify `src/athena/experiment/supervisor.py`: retain the single SEARCH policy implementation and preserve the incomplete-policy note.
- Modify repository callers of `athena.execution.supervisor`: use `athena.experiment.supervisor`.
- Delete `src/athena/execution/agent_monitor.py`, `src/athena/execution/handlers.py`, `src/athena/execution/supervisor.py`, and `test/unit/test_handlers.py`.
- Create `test/unit/execution/test_events.py` and `test/unit/execution/test_monitor.py`.
- Create `test/unit/app_server/test_observability.py` and update `test/unit/app_server/test_thread_runtime.py` lifecycle expectations.

---

### Task 1: Consolidate Existing Owners

**Files:**
- Modify: `src/athena/experiment/supervisor.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_e2e_ai4ml.py`
- Modify: `examples/trial_run.py`
- Modify: `examples/ai4ml_pipeline.py`
- Modify: `src/athena/app_server/thread_manager.py`
- Delete: `src/athena/execution/supervisor.py`
- Delete: `src/athena/execution/handlers.py`
- Delete: `test/unit/test_handlers.py`

**Interfaces:**
- Consumes: current `Supervisor.decide(verdict: ComparisonVerdict, budget: BudgetSnapshot) -> Decision` behavior and app-server `RuntimeThreadManager` duck-typed API.
- Produces: canonical `athena.experiment.supervisor.Decision` and `Supervisor`; concrete `RuntimeThreadManager` with no obsolete ABC dependency.

- [ ] **Step 1: Move the focused Supervisor tests to the canonical import**

Change the test import in `tests/test_search_workflow.py`:

```python
from athena.experiment.supervisor import Supervisor
```

Keep the existing `test_supervisor_stops_on_streak` and
`test_supervisor_accepts_improvement` assertions unchanged so they protect the
policy while its owner changes.

- [ ] **Step 2: Run the canonical-owner test before production imports change**

Run:

```powershell
uv run pytest -q tests/test_search_workflow.py -k supervisor
```

Expected: the policy tests pass through `athena.experiment.supervisor`; any
failure is an existing dependency/import issue that must be fixed before the
old copy is removed.

- [ ] **Step 3: Update all callers and remove duplicate ownership**

In `src/athena/workflows/search/search_loop.py`, both examples, and both listed
tests, replace only this import:

```python
from athena.execution.supervisor import Supervisor
```

with:

```python
from athena.experiment.supervisor import Supervisor
```

Copy the existing incomplete-policy comment from the execution copy into
`experiment/supervisor.py` using an actionable marker:

```python
# NOTE(supervisor-policy): The current policy intentionally covers only the
# verdict, remaining-budget, and no-improvement rules.
```

Remove `ThreadManagerABC` from `app_server/thread_manager.py` and declare:

```python
class RuntimeThreadManager:
```

Delete the three obsolete files listed for this task. Do not re-export their
symbols from another execution module.

- [ ] **Step 4: Verify owner cleanup**

Run:

```powershell
uv run pytest -q tests/test_search_workflow.py test/unit/app_server/test_thread_manager.py
rg -n "athena\.execution\.(supervisor|handlers)" src tests test examples
```

Expected: tests pass and `rg` returns no matches.

- [ ] **Step 5: Commit the owner cleanup**

Review the exact paths first, then commit only them:

```powershell
git diff --check -- src/athena/experiment/supervisor.py src/athena/workflows/search/search_loop.py src/athena/app_server/thread_manager.py tests/test_search_workflow.py tests/test_e2e_ai4ml.py examples/trial_run.py examples/ai4ml_pipeline.py src/athena/execution/supervisor.py src/athena/execution/handlers.py test/unit/test_handlers.py
git add src/athena/experiment/supervisor.py src/athena/workflows/search/search_loop.py src/athena/app_server/thread_manager.py tests/test_search_workflow.py tests/test_e2e_ai4ml.py examples/trial_run.py examples/ai4ml_pipeline.py src/athena/execution/supervisor.py src/athena/execution/handlers.py test/unit/test_handlers.py
git commit --only -m "refactor(execution): consolidate runtime owners" -- src/athena/experiment/supervisor.py src/athena/workflows/search/search_loop.py src/athena/app_server/thread_manager.py tests/test_search_workflow.py tests/test_e2e_ai4ml.py examples/trial_run.py examples/ai4ml_pipeline.py src/athena/execution/supervisor.py src/athena/execution/handlers.py test/unit/test_handlers.py
```

Expected: one commit containing only owner migration paths.

---

### Task 2: Define The Execution Event Contract

**Files:**
- Create: `src/athena/execution/events.py`
- Modify: `src/athena/execution/__init__.py`
- Create: `test/unit/execution/__init__.py`
- Create: `test/unit/execution/test_events.py`

**Interfaces:**
- Consumes: Python standard-library datetime, dataclass, enum, and async callable types.
- Produces: `ExecutionEventKind`, `ExecutionState`, `MonitorLimits`, `ExecutionEvent`, `ExecutionSnapshot`, `HealthStateEvent`, and `HealthEventSink`.

- [ ] **Step 1: Write contract validation tests**

Create tests equivalent to:

```python
from datetime import datetime, timedelta, timezone

import pytest

from athena.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    MonitorLimits,
)


def test_monitor_limits_require_positive_durations() -> None:
    with pytest.raises(ValueError, match="stalled_after"):
        MonitorLimits(stalled_after=0, timeout_after=10)
    with pytest.raises(ValueError, match="timeout_after"):
        MonitorLimits(stalled_after=1, timeout_after=-1)


def test_execution_event_requires_id_and_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="execution_id"):
        ExecutionEvent(" ", ExecutionEventKind.STARTED)
    with pytest.raises(ValueError, match="UTC"):
        ExecutionEvent(
            "turn:1",
            ExecutionEventKind.ACTIVITY,
            occurred_at=datetime.now(),
        )
    event = ExecutionEvent(
        "turn:1",
        ExecutionEventKind.ACTIVITY,
        occurred_at=datetime.now(timezone.utc),
        advances_progress=True,
    )
    assert event.advances_progress is True
```

Also assert the public imports resolve from `athena.execution`.

- [ ] **Step 2: Run tests and confirm missing-contract failure**

Run:

```powershell
uv run pytest -q test/unit/execution/test_events.py
```

Expected: collection fails because the new contract does not exist.

- [ ] **Step 3: Implement the frozen contract types**

Implement these exact public shapes in `events.py`:

```python
class ExecutionEventKind(StrEnum):
    STARTED = "started"
    ACTIVITY = "activity"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionState(StrEnum):
    RUNNING = "RUNNING"
    STALLED = "STALLED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class MonitorLimits:
    stalled_after: float = 300.0
    timeout_after: float = 3600.0


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    execution_id: str
    kind: ExecutionEventKind
    occurred_at: datetime = field(default_factory=utc_now)
    advances_progress: bool = False
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
```

`ExecutionSnapshot` contains `execution_id`, `state`, `started_at`,
`last_event_at`, `last_progress_at`, optional `stalled_at`, `timeout_at`,
`completed_at`, `failed_at`, and `limits`. `HealthStateEvent` contains
`execution_id`, `state`, `changed_at`, and `snapshot`.

Define:

```python
HealthEventSink = Callable[[HealthStateEvent], Awaitable[None]]
```

Validation requires a non-blank ID, a timezone-aware zero-offset UTC timestamp,
positive finite limits, and a mapping for metadata. Export every public type
from `execution/__init__.py`.

- [ ] **Step 4: Run and format the contract tests**

Run:

```powershell
uv run pytest -q test/unit/execution/test_events.py
uv run black src/athena/execution/events.py src/athena/execution/__init__.py test/unit/execution
```

Expected: contract tests pass and formatting makes no semantic change.

- [ ] **Step 5: Commit the event contract**

```powershell
git add src/athena/execution/events.py src/athena/execution/__init__.py test/unit/execution/__init__.py test/unit/execution/test_events.py
git commit --only -m "feat(execution): define observability events" -- src/athena/execution/events.py src/athena/execution/__init__.py test/unit/execution/__init__.py test/unit/execution/test_events.py
```

Expected: one self-contained contract commit.

---

### Task 3: Implement The Passive Monitor State Machine

**Files:**
- Create: `src/athena/execution/monitor.py`
- Modify: `src/athena/execution/__init__.py`
- Create: `test/unit/execution/test_monitor.py`
- Delete: `src/athena/execution/agent_monitor.py`

**Interfaces:**
- Consumes: all Task 2 types and injected `Callable[[], float]` monotonic and `Callable[[], datetime]` UTC clocks.
- Produces: `ExecutionMonitor.observe()`, `sweep()`, `run()`, `snapshot()`, `remove()`, and `snapshots()`.

- [ ] **Step 1: Write fake-clock transition tests**

Use this test support:

```python
class FakeClock:
    def __init__(self) -> None:
        self.monotonic = 0.0
        self.utc = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.utc += timedelta(seconds=seconds)


async def collect_health(event: HealthStateEvent) -> None:
    emitted.append(event)
```

Cover these exact cases:

```python
await monitor.observe(started("turn:1"), limits=MonitorLimits(5, 20))
clock.advance(5)
await monitor.sweep()
assert states == [RUNNING, STALLED]

await monitor.observe(activity("turn:1", advances_progress=False))
assert monitor.snapshot("turn:1").state is STALLED

await monitor.observe(activity("turn:1", advances_progress=True))
assert states[-1] is RUNNING

clock.advance(20)
await monitor.sweep()
assert states[-1] is TIMEOUT
await monitor.observe(completed("turn:1"))
assert states[-1] is COMPLETED
assert monitor.snapshot("turn:1").timeout_at is not None
```

Add separate tests for timeout precedence, pre-terminal timer evaluation,
failed completion, duplicate starts with same/conflicting limits, unknown IDs,
post-final events, sink exceptions, `run(stop_event)`, explicit removal, and
terminal-retention pruning.

- [ ] **Step 2: Run tests and confirm the missing monitor failure**

Run:

```powershell
uv run pytest -q test/unit/execution/test_monitor.py
```

Expected: collection fails because `ExecutionMonitor` does not exist.

- [ ] **Step 3: Implement record keeping and transitions**

Use a private mutable `_ExecutionRecord` for monotonic timestamps and public
frozen snapshots. The constructor has this API:

```python
def __init__(
    self,
    sink: HealthEventSink,
    *,
    default_limits: MonitorLimits | None = None,
    scan_interval: float = 1.0,
    terminal_retention: float = 300.0,
    clock: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = utc_now,
) -> None:
```

Implement:

```python
async def observe(
    self,
    event: ExecutionEvent,
    *,
    limits: MonitorLimits | None = None,
) -> ExecutionSnapshot

async def sweep(self) -> tuple[ExecutionSnapshot, ...]
async def run(self, stop: asyncio.Event) -> None
def snapshot(self, execution_id: str) -> ExecutionSnapshot | None
def snapshots(self) -> tuple[ExecutionSnapshot, ...]
def remove(self, execution_id: str) -> bool
```

Before every terminal transition, evaluate elapsed time and emit any due
`STALLED` or `TIMEOUT` state first. `TIMEOUT` takes precedence when both are due.
Only effective progress restores `STALLED`; no event restores `TIMEOUT`.

Catch and log sink exceptions inside `_emit_state` after storing the state, so
the monitored execution and later scans continue. Unknown execution IDs raise
`KeyError` after logging. Conflicting repeated-start limits raise `ValueError`.
Final-state duplicates return the existing snapshot without another sink call.

The scan loop calls `sweep()`, then waits for either the stop event or
`scan_interval`. Final records are pruned when monotonic time since their final
transition reaches `terminal_retention`.

- [ ] **Step 4: Run focused state-machine validation**

Run:

```powershell
uv run pytest -q test/unit/execution/test_events.py test/unit/execution/test_monitor.py
uv run black src/athena/execution test/unit/execution
```

Expected: every event and monitor test passes without real sleeps.

- [ ] **Step 5: Remove the old awaitable monitor and commit**

Delete `execution/agent_monitor.py`, confirm no old imports, then commit:

```powershell
rg -n "execution\.agent_monitor" src tests test examples
git add src/athena/execution test/unit/execution
git commit --only -m "feat(execution): monitor progress events" -- src/athena/execution test/unit/execution
```

Expected: `rg` returns no matches and the commit contains the new monitor plus
deletion of the obsolete one.

---

### Task 4: Connect App-Server Agent Observability

**Files:**
- Create: `src/athena/app_server/observability.py`
- Modify: `src/athena/app_server/thread_runtime.py`
- Modify: `src/athena/app_server/thread_manager.py`
- Create: `test/unit/app_server/test_observability.py`
- Modify: `test/unit/app_server/test_thread_runtime.py`
- Modify: `test/unit/app_server/test_lifecycle.py`

**Interfaces:**
- Consumes: Task 3 `ExecutionMonitor`, existing Core `EmitEvent` kinds, `EventJournal`, and turn lifecycle methods.
- Produces: `ThreadExecutionObserver` with `start()`, `stop()`, `turn_started()`, `agent_event()`, `turn_completed()`, `turn_failed()`, and `turn_cancelled()`.

- [ ] **Step 1: Write adapter and Journal tests**

In `test_observability.py`, instantiate an `EventJournal` and observer with
small explicit limits. Assert:

```python
clock = FakeClock()
observer = ThreadExecutionObserver(
    journal,
    limits=MonitorLimits(stalled_after=5, timeout_after=20),
    scan_interval=1,
    terminal_retention=30,
    clock=lambda: clock.monotonic,
    utc_now=lambda: clock.utc,
)
await observer.turn_started("turn:1")
started_progress = observer.monitor.snapshot("turn:1").last_progress_at
await observer.agent_event("turn:1", "log", "event:1", None)
assert observer.monitor.snapshot("turn:1").last_progress_at == started_progress

clock.advance(1)
await observer.agent_event(
    "turn:1", "agent/text_delta", "event:2", {"delta": "x"}
)
assert observer.monitor.snapshot("turn:1").last_progress_at > started_progress

await observer.turn_completed("turn:1")
assert [event.data["state"] for event in journal._records] == [
    "RUNNING",
    "COMPLETED",
]
```

Use a fake clock to avoid waiting. Add mappings for `tool/begin`, `tool/end`, and
`tool/error`; assert unknown/log/heartbeat kinds use `advances_progress=False`.
Assert `turn_cancelled()` removes the record without emitting a false failure.

- [ ] **Step 2: Run the adapter test and confirm failure**

Run:

```powershell
uv run pytest -q test/unit/app_server/test_observability.py
```

Expected: collection fails because `ThreadExecutionObserver` does not exist.

- [ ] **Step 3: Implement the source-side adapter**

Define the explicit source mapping:

```python
AGENT_PROGRESS_EVENTS = frozenset(
    {"agent/text_delta", TOOL_BEGIN, TOOL_END, TOOL_ERROR}
)
```

Use this constructor so app-server tests and `RuntimeThreadManager` can provide
explicit timing policy without exposing monitor internals:

```python
def __init__(
    self,
    journal: EventJournal,
    *,
    limits: MonitorLimits | None = None,
    scan_interval: float = 1.0,
    terminal_retention: float = 300.0,
    clock: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = utc_now,
) -> None:
```

`ThreadExecutionObserver` owns one monitor and an app-server-created scanner
task named `execution-monitor-{thread_id}`. `start()` creates the task and
`stop()` sets its stop event and awaits it. Its health sink appends an
`Event(kind="execution/health", ...)` under the Journal condition with data:

```python
{
    "execution_id": event.execution_id,
    "state": event.state.value,
    "changed_at": event.changed_at.isoformat(),
}
```

Place this one implementation comment directly above the Journal append:

```python
# A future handler may subscribe to terminal and unhealthy state changes here.
```

Do not add any Handler symbol or callback.

- [ ] **Step 4: Wire turn and Agent events without controlling execution**

`ThreadRuntime` constructs the observer after its Journal. It starts/stops the
observer alongside its existing queue tasks. After the native Journal write,
forward:

```python
await self._execution_observer.turn_started(turn.turn_id)
await self._execution_observer.turn_completed(turn_id)
await self._execution_observer.turn_failed(turn_id, exception_type)
await self._execution_observer.turn_cancelled(turn_id)
```

In `_run_turn.emit`, append the original Agent event first, release the Journal
condition, then call:

```python
await runtime._execution_observer.agent_event(
    turn.turn_id, kind, event_ref, data
)
```

Never call `runner_task.cancel()`, submit a retry, or modify Thread/Turn state
from `observability.py`.

Add optional `monitor_limits`, `monitor_scan_interval`, and
`monitor_terminal_retention` configuration to `ThreadRuntime` and
`RuntimeThreadManager`, with the contract defaults when omitted. Pass them
through `_make_runtime()`.

- [ ] **Step 5: Update lifecycle expectations and task cleanup coverage**

Change exact Journal assertions to include derived events while preserving the
native subsequence:

```python
kinds = [event.kind for event in runtime.journal._records]
assert [kind for kind in kinds if kind != "execution/health"] == [
    "turn_started",
    "item",
    "turn_completed",
]
assert [
    event.data["state"]
    for event in runtime.journal._records
    if event.kind == "execution/health"
] == ["RUNNING", "COMPLETED"]
```

Add `"execution-monitor-"` to `OWNED_TASK_PREFIXES` in lifecycle tests and
retain the assertion that shutdown leaves no owned tasks.

- [ ] **Step 6: Run app-server and Agent integration tests**

Run:

```powershell
uv run pytest -q test/unit/app_server/test_observability.py test/unit/app_server/test_thread_runtime.py test/unit/app_server/test_lifecycle.py test/unit/test_agent.py
uv run black src/athena/app_server/observability.py src/athena/app_server/thread_runtime.py src/athena/app_server/thread_manager.py test/unit/app_server/test_observability.py test/unit/app_server/test_thread_runtime.py test/unit/app_server/test_lifecycle.py
```

Expected: native events remain ordered, health events are added once per state
change, and all monitor tasks stop during shutdown.

- [ ] **Step 7: Commit the app-server integration**

```powershell
git add src/athena/app_server/observability.py src/athena/app_server/thread_runtime.py src/athena/app_server/thread_manager.py test/unit/app_server/test_observability.py test/unit/app_server/test_thread_runtime.py test/unit/app_server/test_lifecycle.py
git commit --only -m "feat(app-server): journal execution health" -- src/athena/app_server/observability.py src/athena/app_server/thread_runtime.py src/athena/app_server/thread_manager.py test/unit/app_server/test_observability.py test/unit/app_server/test_thread_runtime.py test/unit/app_server/test_lifecycle.py
```

Expected: only the observer integration paths enter the commit.

---

### Task 5: Repository Cleanup And Verification

**Files:**
- Modify only files exposed by validation failures caused by Tasks 1-4.
- Verify: `src/athena/execution`, `src/athena/app_server`, `src/athena/experiment`, `src/athena/workflows/search`, tests, and examples.

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: a repository with no obsolete execution owner, no Handler implementation, and evidence-backed focused and full-suite results.

- [ ] **Step 1: Scan imports and forbidden control behavior**

Run:

```powershell
rg -n "athena\.execution\.(handlers|agent_monitor|supervisor)" src tests test examples
rg -n "class .*Handler|Protocol.*Handler|cancel\(|retry|repair" src/athena/execution src/athena/app_server/observability.py
```

Expected: the old-import search has no matches. The second search finds no
Handler declaration and no cancellation, retry, or repair behavior; the single
future-handler comment is allowed.

- [ ] **Step 2: Run focused behavioral gates**

Run:

```powershell
uv run pytest -q test/unit/execution test/unit/app_server tests/test_search_workflow.py tests/test_e2e_ai4ml.py tests/test_code_monitor.py test/unit/test_agent.py
```

Expected: all focused tests pass. `tests/test_code_monitor.py` confirms the
deferred CodeEngine monitor remains unchanged.

- [ ] **Step 3: Run formatting and static repository checks**

Run:

```powershell
uv run black --check src/athena/execution src/athena/app_server src/athena/experiment/supervisor.py src/athena/workflows/search/search_loop.py test/unit/execution test/unit/app_server tests/test_search_workflow.py tests/test_e2e_ai4ml.py examples/trial_run.py examples/ai4ml_pipeline.py
git diff --check
```

Expected: both commands exit zero.

- [ ] **Step 4: Run the complete test suite**

Run:

```powershell
uv run pytest -q test tests
```

Expected: the suite passes, or only failures already present in the recorded
baseline remain and are reported explicitly with evidence.

- [ ] **Step 5: Review final scope and commit validation fixes**

Inspect:

```powershell
git status --short
git diff --stat
git diff --check
```

If validation required task-scoped fixes, commit only their explicit paths with
`git commit --only`. Do not stage or commit unrelated files. Confirm the final
diff contains no Handler implementation, no compatibility module, and no
CodeEngine/CLI streaming work.
