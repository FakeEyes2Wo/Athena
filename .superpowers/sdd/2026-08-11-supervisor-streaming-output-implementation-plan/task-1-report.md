# Task 1 Report: Supervisor Message Lifecycle

## Implementation

- Added `SupervisorMessageResponder` in `src/athena/research/supervisor/messaging.py`.
  It keeps `single_turn_chat` internal to the Supervisor package, gives it a fixed
  status-only system prompt, and maps `agent/text_delta` callbacks to
  `(delta, accumulated)` reply callbacks.
- Added `SupervisorCoordinator.set_message_responder()` and
  `handle_user_message()`.
  The coordinator trims and validates user input, resolves the execution from
  bound then active then latest, supplies the required status context, permits
  one active reply, and returns `{message_id, reply}`.
- Durable `message/started`, `message/completed`, and `message/failed` events
  are appended before being published. `message/delta` uses `_emit_transient()`
  and is never appended to the journal. Cancellation persists a failed terminal
  event with `error="cancelled"` and is re-raised.

## Tests And Evidence

Focused command:

```powershell
$env:PYTHONPATH='C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\supervisor-streaming-output\src;C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\supervisor-streaming-output'
& 'C:\Users\80163\Desktop\挑战杯_2026\Athena\.venv\Scripts\python.exe' -m pytest test/unit/research/test_supervisor_core.py -k "user_message or message_stream" -q --basetemp .superpowers/pytest-task1
```

RED, before production implementation:

```text
ModuleNotFoundError: No module named 'athena.research.supervisor.messaging'
1 error during collection
```

GREEN, after implementation (fresh final run):

```text
....                                                                     [100%]
4 passed, 36 deselected in 1.36s
```

The final verification command also ran `py_compile` for the changed Python
files and `git diff --check`; both returned exit code 0.

The focused tests cover ordered callback events, durable-event filtering,
message-id consistency, overlap rejection, cancellation failure persistence,
required reply context, and `agent/text_delta` forwarding.

## Files

- `src/athena/research/supervisor/messaging.py`
- `src/athena/research/supervisor/coordinator.py`
- `test/unit/research/test_supervisor_core.py`
- `.superpowers/sdd/2026-08-11-supervisor-streaming-output-implementation-plan/task-1-report.md`

## Self-Review

- Confirmed Supervisor is the only new owner of user-message handling; Worker
  code and rollout behavior are unchanged.
- Confirmed each accepted message has one started event and one terminal event;
  deltas are callback-only.
- Confirmed terminal events are persisted before callback publication, including
  the cancellation path.
- No deferred background queue was added.

## Concerns

The full supervisor-core file was not used as a completion gate because the
task brief identifies 11 known unrelated failures in the `uv lock`/GBK paths.
They were not modified or investigated by this task.

## Review Fix Round 1

### Root Cause And Implementation

- The shared `_emit()` method deliberately treats plan-event journal writes as
  best effort. Reusing it for messages swallowed `append_event()` failures and
  published an unpersisted lifecycle.
- `message/completed` was emitted inside the responder `try`, so an observer
  exception or cancellation after persistence was classified as a responder
  failure and appended a second `message/failed` terminal event.
- Added `_emit_message_durable()`, a message-only strict emitter which appends
  before calling `_emit_transient()` and propagates journal errors. Existing
  plan-event `_emit()` behavior remains unchanged.
- `handle_user_message()` now classifies only `responder.stream()` outcomes.
  It selects one terminal event, persists and publishes it after classification,
  then propagates the responder error or cancellation. An observer failure
  while publishing that terminal cannot create another journal record.

### RED And GREEN Evidence

Command:

```powershell
$env:PYTHONPATH='C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\supervisor-streaming-output\src;C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\supervisor-streaming-output'
& 'C:\Users\80163\Desktop\挑战杯_2026\Athena\.venv\Scripts\python.exe' -m pytest test/unit/research/test_supervisor_core.py -k "user_message or message_stream" -q --basetemp .superpowers/pytest-task1
```

RED before the fix:

```text
.FFF...
3 failed, 4 passed, 36 deselected in 1.75s
```

The three failures showed: journal append failure did not raise; completed
callback failure added `message/failed`; and completion callback cancellation
also added `message/failed`.

GREEN after the fix:

```text
.......
7 passed, 36 deselected in 1.39s
```

`py_compile src/athena/research/supervisor/coordinator.py` also returned exit
code 0 before the GREEN test run.

### Fix-Round Files And Self-Review

- Modified `src/athena/research/supervisor/coordinator.py`.
- Modified `test/unit/research/test_supervisor_core.py`.
- Modified this report.
- Added regression tests for strict started persistence, completed observer
  exception, and completed observer cancellation.
- Verified a journal failure occurs before callback publication, and verified
  both success-terminal publication failure paths leave exactly
  `message/started` plus `message/completed` in the journal.
- The full supervisor-core file remains outside this gate because of the 11
  known unrelated `uv lock`/GBK baseline failures; they remain untouched.
