# Research Core Rewrite Completion Report

Completed: 2026-09-02

## Outcome

The previous `implementation complete` claim was withdrawn and the reported production
gaps were corrected. `src/athena/research` now has explicit functional boundaries for
runtime composition, lifecycle control, supervisor decisions, phase execution, and
clarification/confirmation. The public Runtime remains compatible with CLI, TUI, GUI,
and persisted checkpoints while owning only four grouped attributes.

## Corrected behavior

- Added all five clarification RPCs to the production Runtime.
- Preserved structured domain codes through Python WebSocket, Rust/Tauri, and
  TypeScript, including reachable `stale_revision` handling.
- Made cancellation return to `IDLE` and allowed clean restarts.
- Canonicalized missing critical task fields as unresolved confirmation items.
- Made confirmation rollback restore memory and disk, and made lifecycle launch
  retryable after commit.
- Made the eighth clarification answer produce a deterministic confirmation draft.
- Persisted cancelled pending requests during recovery.
- Made corrupt confirmed handoffs block phase side effects.
- Propagated the actual research session ID to typed human requests.
- Added exact row identity for directory evaluation and bounded EDA/evaluator inputs.
- Bounded one-shot agent and Windows subprocess completion cleanup.

## Structural result

Clarification models, transitions, generation, persistence, context, journal, handoff,
controller, and confirmation are separate modules. Runtime composition and control,
PREPARE/VALIDATE entry, task-context preflight, supervisor lifecycle, SEARCH state,
freshness, and evaluation each have a direct owner. No general-purpose framework or
dynamic forwarding layer was added.

## Fresh verification

- Research unit/integration: 710 passed.
- GUI gateway and real Runtime WebSocket coverage: 53 passed.
- Execution runtime: 33 passed.
- Frontend: 14 files and 78 tests passed; production build passed.
- Rust/Tauri: 7 tests passed; native bridge round trip passed.
- Python compile check and global `git diff --check`: passed.

The exact repeatable gate commands are recorded in `night_docs/verification.md`.

Known non-failing warnings are recorded in `night_docs/verification.md`.

## Production smoke

`../task3/athena-jw-ssd-run-final` completed through the production CLI with exactly two
SEARCH attempts, then reopened through the TUI without changing terminal state. The
selected compact classifier recorded SEARCH macro-F1 `0.562185`; FINAL macro-F1 was
`0.20987654320987656`, with a generalization warning. Reproduction commands, commits,
artifacts, dataset splits, and metric limitations are recorded under `night_docs`.

## Remaining packaging boundary

The checked-in `resources/gui_gateway.exe` is a zero-byte test placeholder. Native
protocol behavior is verified, but producing a distributable Windows sidecar remains a
packaging task rather than part of this research-core rewrite.
