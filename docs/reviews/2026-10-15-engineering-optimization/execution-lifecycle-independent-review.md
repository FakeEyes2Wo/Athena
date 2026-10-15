# Execution lifecycle cancellation review (2026-10-15)

## Review scope

Reviewed the current `pool.py` and `remote/channel.py` against the ignored pre-change copies under `submission/reproduction/2026-10-15-engineering-optimization/baseline/`. Traced the lease path `GpuPool.acquire/release -> Lease.backend.aclose -> MirroredBackend/SshBackend -> RemoteChannel.close` and the channel path `open/_read_loop/spawn/_send`. Existing execution-review findings remain unchanged outside cancellation/close ownership. No real SSH host or experiment was used.

## Reproduced defects and changes

Four controlled regressions were red before the fixes:

- Three cancellation requests during `GpuPool.acquire` cleanup could cancel the raw `await cleanup`; the `finally` path then freed the GPU while the fake backend's close gate was still blocked.
- Cancellation during `GpuPool.release`'s workspace discard, followed by two more cancellations while backend close was shielded/joined, could likewise cancel close and return the slot early.
- `RemoteChannel.close` returned to its caller immediately on the first cancellation, even though shield kept the cleanup task alive. That allowed an owner to proceed to GPU accounting before the channel/transport was actually stopped.
- On cancelled `spawn`, the best-effort secondary `_send(cancel)` had no bound; a blocked write kept the original cancellation pending indefinitely.

The pool now routes acquire/release cleanup through one narrow `_close_lease_backend` policy: it shields and continues joining the close task across repeated caller cancellations, reports close failures, and lets reservation accounting run only after that task finishes. It returns whether release itself was cancelled during close so cancellation still propagates after accounting. The helper does not promise recovery from an independently self-cancelled or non-terminating backend close.

`RemoteChannel.close` continues joining its single idempotent cleanup task across repeated caller cancellations, then propagates cancellation after cleanup. This also preserves the EOF case: `_read_loop` may set `_closed` first, but a later `close()` still creates/joins cleanup and calls `transport.stop()` exactly once.

The secondary remote cancel send now runs as a task with a one-second best-effort deadline (`SPAWN_CANCEL_TIMEOUT_S`). On timeout or another cancellation, the send is cancelled and the original spawn failure/cancellation remains the caller-visible outcome.

## Regression and verification evidence

Added deterministic tests for repeated cancellation in acquire cleanup, repeated cancellation in release cleanup, repeated cancellation during channel close, and a stalled secondary spawn-cancel write. They assert externally visible lease accounting, backend-close completion, idempotent transport stop, cleared pending/job state, and bounded propagation of the original cancellation.

Before implementation, all four tests failed; after implementation, the focused set passed. Final checks:

- `pytest -q tests/unit/execution` with `PYTHONPATH=src`: **181 passed, 1 skipped**.
- `python -m black --check src/athena/execution/pool.py src/athena/execution/remote/channel.py tests/unit/execution/test_pool.py tests/unit/execution/test_remote_channel.py`: passed.
- `python scripts/check_code_style.py --enforce-docstrings src/athena/execution`: passed.
- `git diff --check` for the four owned source/test files: passed.

The best-effort cancel test lowers the timeout to 20 ms; the production default is one second. The local subprocess and fake transports cover channel lifecycle behavior, but the SSH command/remote host behavior is not exercised here.

## Manual regression addendum (2026-10-15)

- Re-reviewed the detached best-effort cancel send created after `spawn` failure/cancellation. Its deadline bounds the caller, but a cancellation-resistant `_send` may fail after detachment; cancelling the task alone does not retrieve a later exception. A done callback now retrieves any eventual exception without extending the caller's wait or changing the original spawn failure/cancellation. Deterministic regression holds the cleanup send past its deadline, then makes it raise after cancellation; no loop-level unhandled-task report is emitted.
- `RemoteChannel.close` and pool close-owner ordering were not changed in this pass. Existing contract remains bounded only for the secondary spawn-cancel send; channel/backend cleanup joins its task before lease accounting and does not promise recovery from a permanently hung backend.
- `src/athena/execution/monitor.py`'s nested transition notification remains as-is: `None` means no state transition, so the append guard directly preserves monitor state/event ordering.
- Focused channel/trace tests passed before a concurrent out-of-scope UTF-8 corruption blocked subsequent pytest collection through the eager research runtime import. Strict code-style gate and Black check passed for the changed/scoped modules before final recovery of that external test blocker.
- After the GUI collection blocker was identified, the isolated remote-channel suite ran independently: `pytest -q tests/unit/execution/test_remote_channel.py` — 34 passed. GUI trace/TUI/gateway-dependent tests remain deferred until the root restores the malformed out-of-scope research source.
- After the out-of-scope test collection blocker was restored, combined gateway/state-store/execution lifecycle regression passed: 135 passed. Pytest emitted a Windows `BaseSubprocessTransport.__del__` / closed-loop unraisable warning after tests; test exit status was successful. The separate remote channel suite also passed 34 tests.
