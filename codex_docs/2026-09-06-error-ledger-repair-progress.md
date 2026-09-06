# Error-ledger repair progress

Implemented against the current Python source, not assumptions in older plans. ATHENA_ERRORS.md remains an append-only historical record. This report does not certify every historical error fixed.

## Delivered

- Errors 9/17: POSIX process-group identity retained at spawn; timeout/cancellation perform bounded process and pipe cleanup. Windows keeps taskkill tree termination. Live POSIX deployment acceptance remains outstanding.
- Errors 15/33: existing General turn timeout now bounds the interrupt request too.
- Error 18: `/cancel <plan_id> <reason>` calls Supervisor directly without starting SEARCH. Persist CANCELLED and a reason artifact before removing the active Plan. Recovery drops terminal experiments after a tree/state save interruption. Other Plans and SOTA are preserved; repeated cancellation can retry cleanup. Late results cannot settle removed Plans. This reuses existing tree-first saves, not the unimplemented transactional coordinator.
- Error 12: review diffs use the accepted checkpoint separately from HEAD. Agent-authored commits remain in history; Athena verifies HEAD/tree and records a recoverable review baseline. Accepted subsequent rounds advance the checkpoint.
- Errors 24/32: resolve CSV features from the existing durable data contract, including external split directories. Missing required CSV features fail explicitly before execution/workspace setup. Legacy local layouts and directory tasks retain their existing behavior; output schema remains evaluator-owned.
- Errors 1/5/20: explicit schema root and decision literal instructions; bounded/redacted diagnostics in repair and terminal output; an already published baseline exception is not published a second time by the phase machine.
- Error 16: pending persistent/next guidance survives restart in existing resume metadata. New Plans freeze guidance as before; old immutable PlanInput records are not rewritten.
- Error 19: reuse existing sequential EDA stages for relationships/leak analysis; prompts require producers before consumers. This mitigates generated task dependency mistakes, not an OS-level publication guarantee.
- Error 28: expected system clipboard backend failures fall back to in-memory storage; unrelated programming errors still propagate.

## Focused evidence

No paid model calls, real training, or FINAL dataset inspection was used. Existing tests were reused; additions cover failure chains rather than every ledger entry.

- Supervisor, scheduler, recovery, Human commands, state/guidance, phase preflight, baseline prompt selection: 128 passed.
- Execution runtime: 32 passed; two Windows Proactor pipe cleanup warnings remain.
- Baseline orchestration: 57 passed.
- Git workspace: 22 passed, 3 subtests.
- Prediction preflight and held-out prediction selection: 13 passed.
- Clipboard selection: 6 passed, 22 deselected.
- Existing General timeout selection: 1 passed.
- Experiment unit tests: 42 passed. Actual Plan execution/scoring integration: 1 passed outside the sandbox with PYTHONUTF8=1. The first sandbox run under default Windows encoding failed with scoring_failed and a subprocess reader UnicodeDecodeError; changing both environment conditions does not isolate the cause or certify default-encoding support.
- Earlier overlapping checks are not added together as a unique test count.
- Old pytest temporary directories had ACL failures. Successful retries used fresh basetemp directories. Formatting required execution outside the restricted sandbox; changed Python files passed Black.

## Unresolved work and evidence limits

- Errors 7/11/16/25/31: generic file tools confine paths, but local and SSH shells run without private evaluator mounts or a separate OS identity. Remote dataset staging can include labels. A filename denylist cannot enforce the stated no-read/no-metadata boundary. Deployment isolation must be selected and implemented before claiming this fixed; existing contaminated results remain contaminated.
- Error 10: per-Plan cancellation and source-change checks help recovery, but semantic duplicate-baseline scheduling needs a canonical implementation/configuration identity. Do not infer that identity from prose similarity or introduce a misleading fingerprint of unavailable provenance.
- Error 13: unexplained historical TUI termination still lacks a traceback or equivalent cause evidence.
- Errors 2/3/8/14/21/22/23/27: incident-specific generated scripts, monitoring harnesses, and remote interpreter configuration are not all present as reproducible repository code. Current source inspection does not establish that every historical deployment issue is fixed.
- Error 30: ledger omits the attribute name, object type and traceback; no targeted code correction is possible from that record alone.
- Error 29: current feature routing improves partition selection, but this is not a proof of arbitrary generated training code's partition fidelity.
- Errors 6/26: preserve honest provenance/access reports; later code changes cannot retroactively remove exposure or make historical claims true.
- Existing Plans retain their original frozen guidance. Cancel and replace an unsafe Plan instead of assuming new guidance repairs its history.

## Integration

User requires completed changes on main and pushed, with task-created temporary branches removed afterward. Work was performed on main; no task branch was created. Preserve the unrelated deletion of pr15_source.diff and stage only owned files. The repair plan remains linked from CURRENT because deployment isolation and missing incident evidence are unresolved; unrelated plans must not be closed.
