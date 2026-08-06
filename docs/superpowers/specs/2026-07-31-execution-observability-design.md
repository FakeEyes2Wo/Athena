# Execution Observability Design

Status: approved
Owner: Athena maintainers
Date: 2026-07-31

## Purpose

Refocus `athena.execution` on one cross-cutting responsibility: observing
long-running executions and detecting lack of progress or deadline overruns.
The design follows the `AgentMonitor` goal in `articles/AutoSOTA.md`: track
long-running work and prevent deadlock. In this phase, prevention means reliable
detection and health-state emission. A future handler may react to those states,
but this change does not define or implement a handler.

The work has two stages:

1. Clean ownership boundaries and remove duplicate implementations and obsolete
   import paths.
2. Introduce event-based Agent observability using the Core Agent and app-server
   event flow.

CodeEngine, Codex/Qoder backends, and incremental script output are not connected
in this phase. Their eventual integrations must use the same event contract.

## Current Problems

`athena.execution` currently combines three unrelated owners:

- `execution/handlers.py` duplicates the typed ThreadManager and submission
  runtime already owned and used by `athena.app_server`. Its string dispatch
  loop has no production consumer.
- `execution/agent_monitor.py` is an unused dictionary-returning monitor. The
  active `athena.code.monitor.AgentMonitor` only wraps a final awaitable and
  cannot observe real intermediate progress.
- `execution/supervisor.py` contains SEARCH decision policy rather than
  execution infrastructure. It duplicates `experiment/supervisor.py`.

The Core Agent already emits intermediate text and tool events, and app-server
already journals those events. The missing boundary is a source-independent
execution event contract and a passive state tracker that distinguishes
activity from actual progress.

## Chosen Approach

`athena.execution` defines a small typed observability contract and a passive
monitor. Event-source adapters explicitly identify whether an event advances
progress. The monitor consumes those events, maintains per-execution snapshots,
and emits health-state changes through an independent sink.

This approach is preferred over directly using Core Agent event classes because
Agent-specific types would make later CodeEngine and script integration depend
on Agent semantics. A full event bus is also rejected: subscription management,
delivery guarantees, persistence, and backpressure are unnecessary for the
first integration.

## Ownership And Target Structure

After cleanup, `athena.execution` contains only execution observability:

```text
src/athena/execution/
|- __init__.py
|- events.py
`- monitor.py
```

Responsibilities are assigned as follows:

- `execution/events.py` owns source-independent input events, health-state
  events, state values, snapshots, monitor limits, and the asynchronous health
  sink contract.
- `execution/monitor.py` owns event validation, state transitions, time
  accounting, periodic scans, snapshot lookup, and terminal-state cleanup.
- `app_server` owns ThreadManager, typed submissions, Agent-event adaptation,
  monitor lifecycle wiring, and journal persistence.
- `experiment/supervisor.py` is the only implementation of SEARCH acceptance,
  rejection, and stopping policy.

The following files are removed:

- `execution/handlers.py`
- `execution/agent_monitor.py`
- `execution/supervisor.py`

The duplicate handler tests are removed. Supervisor tests and every production,
test, and example import move to `athena.experiment.supervisor`. No compatibility
module or re-export preserves any deleted import path. The existing local note
that Supervisor policy is incomplete moves with the canonical implementation
rather than being discarded.

The existing `athena.code.monitor.AgentMonitor` remains temporarily because the
CodeEngine integration is explicitly deferred. It is not the new canonical
cross-source monitor and will be adapted or retired in the later CodeEngine
phase.

## Event Contract

An `ExecutionEvent` is immutable and contains at least:

- a non-empty `execution_id`;
- a typed lifecycle or activity kind;
- an aware UTC `occurred_at` timestamp for audit and journaling;
- `advances_progress`, set explicitly by the event source;
- optional JSON-safe metadata for diagnostics.

The contract supports start, activity, successful completion, and failed
completion. It does not carry arbitrary domain objects. Native event names may
be retained as metadata, but the monitor never branches on Core Agent,
app-server, CodeEngine, or CLI-specific classes.

`advances_progress` is authoritative. Log lines, telemetry, and heartbeats may
prove liveness while remaining `false`; they therefore cannot hide a stalled
execution by continuously resetting the progress timer.

Monitor limits contain two independent positive durations:

- `stalled_after`: maximum time without an event marked as progress;
- `timeout_after`: maximum total execution duration.

The monitor provides defaults and permits per-execution overrides. Both values
must be positive. They need not be ordered; when both conditions become true in
the same scan, `TIMEOUT` takes precedence.

## Monitor State And Time

For each registered execution, the monitor stores a read-only snapshot with:

- current state;
- start time;
- last observed event time;
- last progress time;
- optional stalled, timeout, completion, and failure times;
- the applied monitor limits.

UTC timestamps remain available for audit. Duration calculations use an
injected local monotonic clock so wall-clock adjustment and cross-process clock
skew cannot corrupt timeout decisions. Tests replace this clock with a fake.

The state flow is:

```text
STARTED -> RUNNING
RUNNING -- no progress for stalled_after --> STALLED
STALLED -- advances_progress=true --> RUNNING
RUNNING/STALLED -- total duration reaches timeout_after --> TIMEOUT
RUNNING/STALLED/TIMEOUT -- successful end --> COMPLETED
RUNNING/STALLED/TIMEOUT -- failed end --> FAILED
```

`TIMEOUT` is an observed deadline violation, not task cancellation. Because the
monitor is passive, a timed-out task may later report `COMPLETED` or `FAILED`.
The snapshot and emitted history retain its timeout timestamp. Progress received
after `TIMEOUT` does not restore `RUNNING`, because the absolute deadline remains
violated. `COMPLETED` and `FAILED` are final states.

Before accepting a terminal event, the monitor evaluates elapsed timers. This
ensures that a completion received after its deadline still emits `TIMEOUT`
before the final state even when the periodic scanner has not run at the exact
deadline.

## Data Flow

The first integration uses the existing Core Agent and app-server event path:

```text
Core Agent EmitEvent
        |
        v
app-server adapter/composition
        |
        v
ExecutionEvent -> ExecutionMonitor -> HealthEventSink -> EventJournal
```

The app-server composition layer imports both the native Agent event contract
and `athena.execution`; neither Core Agent nor `athena.execution` imports
app-server. This keeps adaptation with the source owner and avoids a reverse
dependency from the generic monitor to the serving runtime.

The initial adapter maps Agent task start, text deltas, tool begin/end/error,
and final result or failure. Each mapping sets `advances_progress` explicitly.
Journal-only messages and heartbeats do not advance progress. Existing Agent
event forwarding and Journal records remain intact; monitoring is an additional
observer of the same emissions.

Health changes use an independent asynchronous sink rather than being written
back into the monitor input stream. This prevents recursive observation and
keeps source events separate from derived health. App-server records a single
health event whenever the state changes; unchanged scans do not produce
duplicate Journal entries.

The monitor exposes a periodic asynchronous scan that the app-server owns and
schedules. Without a scan, a true deadlock that emits no events could never be
detected. The scan observes time and emits state only; it cannot cancel tasks,
submit work, retry operations, or mutate Thread/Turn state.

Near the health sink composition point, implementation leaves one concise
extension comment stating that a future handler may subscribe to
`STALLED/TIMEOUT/FAILED/COMPLETED`. No Handler class, protocol, factory, or
placeholder module is created in this phase.

## Failure And Edge-Case Behavior

- An ordinary event for an unregistered execution is rejected and logged; it
  never creates state implicitly.
- Repeated starts are idempotent only when their configuration is compatible.
  Conflicting limits are rejected and logged.
- Repeated terminal events and non-terminal events received after a final state
  are ignored and logged for diagnosis.
- Arrival order drives the state machine. `occurred_at` is audit data and cannot
  reorder state, because source clocks may differ.
- Adapter failures do not consume or replace the original execution exception.
  An execution failure is mapped to `FAILED`, then continues through the
  existing call chain.
- A health sink exception cannot fail the monitored execution or stop future
  scans. It is logged, and the latest snapshot remains available. This phase
  does not add a sink retry queue.
- Owners may explicitly remove final snapshots. The monitor also prunes final
  snapshots after a configurable, bounded retention period to prevent leaks in
  long-running servers.

## Explicit Non-Goals

This phase does not:

- create a Handler or perform automatic recovery;
- cancel, retry, pause, or repair an execution;
- infer progress from event names or payload text;
- add an event bus or durable delivery queue;
- stream Codex/Qoder backend output;
- stream subprocess stdout or stderr;
- replace the current CodeEngine awaitable monitor;
- preserve obsolete execution import paths.

## Migration Sequence

1. Record the relevant test baseline and all old import sites.
2. Move SEARCH policy ownership to `athena.experiment.supervisor`, update every
   caller, preserve its existing policy note, and remove the execution copy.
3. Remove the unused handler abstraction and duplicate handler tests, leaving
   the app-server ThreadManager as the sole runtime owner.
4. Add the typed execution event, state, snapshot, limits, and sink contracts.
5. Replace the duplicate execution Agent monitor with the passive event-based
   monitor.
6. Add app-server Agent-event adaptation, periodic monitor lifecycle wiring,
   and health Journal output without changing the original Journal path.
7. Remove obsolete files and exports, scan the repository for old imports, and
   run focused and complete validation.

Existing uncommitted user changes and comments in the files being migrated must
be incorporated rather than reverted. Unrelated worktree changes remain
untouched.

## Testing And Acceptance

Unit tests use a fake monotonic clock and cover:

- `RUNNING -> STALLED -> RUNNING` recovery;
- non-progress activity that does not delay `STALLED`;
- absolute timeout and timeout precedence;
- late `COMPLETED` and `FAILED` after `TIMEOUT`;
- deadline evaluation immediately before a late terminal event;
- duplicate start and terminal idempotency;
- unregistered, out-of-order, and post-terminal events;
- state-change deduplication at the health sink;
- sink failure isolation;
- explicit cleanup and bounded terminal retention.

Integration tests cover the Core Agent/app-server adapter, preservation of
existing Agent Journal behavior, progress mapping, derived health Journal
events, and monitor scan lifecycle. Supervisor tests run against the experiment
owner. The obsolete string-dispatch handler tests are deleted.

Repository searches must find no remaining imports of:

```text
athena.execution.handlers
athena.execution.agent_monitor
athena.execution.supervisor
```

Focused verification covers execution, core Agent, app-server, CodeEngine, and
SEARCH workflow tests. The complete project test suite then checks for
regressions. Acceptance requires:

- real app-server Agent executions can become `STALLED` or `TIMEOUT` from
  intermediate event timing;
- successful and failed terminal states are emitted exactly once;
- no monitor path controls or mutates the execution;
- no Handler implementation or compatibility import remains;
- CodeEngine and CLI integrations remain deferred behind the common contract;
- no unrelated worktree changes are included in the implementation commits.
