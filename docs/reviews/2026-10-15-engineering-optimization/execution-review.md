# Execution module review

Scope: all production Python files under `src/athena/execution/` and their
`tests/unit/execution/` suite. The older simplification inventory marks these
execution modules Reviewed; this pass rechecked current diffs and followed the
GPU lease path from research runtime callers through `GpuPool`, `SshBackend`,
`RemoteChannel`, and the local/SSH transports. No real SSH host or live
experiment was used.

## File decisions

| File | Decision |
| --- | --- |
| `src/athena/execution/__init__.py` | Retain the single public execution facade, as recorded in the old review. |
| `src/athena/execution/backend.py` | Retain the source-independent backend protocol and local implementation; current callers use the seam. |
| `src/athena/execution/check.py` | Retain the four-field host-check result and derived availability state; existing tests cover preflight reporting. |
| `src/athena/execution/compute_config.py` | Retain the six consumed config fields and separate file/mapping parse boundaries. |
| `src/athena/execution/monitor.py` | Retain event contracts with the observer lifecycle in one module. |
| `src/athena/execution/pool.py` | Change: on lease setup/staging failure or cancellation, close an acquired backend before returning its GPU reservation; on release cancellation during remote workspace discard, close the channel and still return accounting. Shared the two reservation-return paths in one policy helper. |
| `src/athena/execution/runtime.py` | Retain the concurrent existing command/encoding/worktree changes; added the required explanation at the existing local-process cancellation boundary. No runtime behavior was altered by this review. |
| `src/athena/execution/remote/__init__.py` | Retain the public remote/mirror facade. |
| `src/athena/execution/remote/agent.py` | Retain the single-file remote agent protocol implementation; operation errors map to protocol failures and are exercised by subprocess transport tests. |
| `src/athena/execution/remote/channel.py` | Change: clean up partial open across start/write/handshake/probe failures and cancellation; separate “closed to new work” from a shared idempotent resource-cleanup task; remove pending request/job state when sending fails or is cancelled; send best-effort remote cancellation when spawn is cancelled before acknowledgement. |
| `src/athena/execution/remote/dataset.py` | Retain mandatory content-manifest verification and staging boundary. |
| `src/athena/execution/remote/mirror.py` | Retain the workspace mirror and remote-only path ownership boundary. |
| `src/athena/execution/remote/ssh.py` | Retain five-parameter backend construction and per-run cwd ownership; clarified the cancellation catch's trigger and propagation. |

The dynamic runner and transport seams remain useful compatibility boundaries;
no forwarding layer was removed merely to reduce line count. The two new
private lifecycle methods each centralize a policy used by multiple paths:
`_return_gpus` updates host accounting for failed acquisition and normal
release, while `_close_resources` gives every close caller the same idempotent
cleanup operation.

## Regression evidence

Before changes, five deterministic tests failed: cancellation during dataset
staging stranded a lease; cancellation during channel bootstrap skipped
transport stop; EOF made later close skip transport stop; failed request send
left a pending future; and failed spawn send left both pending and job state.
After the repairs, ten focused lifecycle tests passed, also covering ordinary
staging failure, cancellation during workspace discard, cancellation during
probe, spawn cancellation/remote cancel ordering, and cancellation during
close followed by an idempotent second close.

The complete `tests/unit/execution` suite passed twice after the lifecycle
change: `178 passed, 1 skipped` (the skip is the existing platform-specific
case). Baseline before the new regressions was `168 passed, 1 skipped`.

`python scripts/check_code_style.py --enforce-docstrings src/athena/execution`,
`python -m black --check src/athena/execution tests/unit/execution`, and
`git diff --check` on owned changes all passed. Tests ran with `PYTHONPATH=src`.
