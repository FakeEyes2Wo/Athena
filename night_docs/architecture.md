# Research Core Architecture

## Design constraints

All rewritten code follows [docs/代码规范.md](../docs/代码规范.md): files own one functional area,
public APIs stay small, dependencies are explicit, nesting is shallow, and new
abstractions must delete more complexity than they add.

## Functional boundaries

- `runtime.py` is the stable facade used by CLI, TUI, GUI, and tests.
- `runtime_bootstrap.py` composes services and workflow objects.
- `runtime_control.py` owns lifecycle commands only; it does not implement
  clarification persistence or confirmation transactions.
- `clarification/` owns draft state, generation policy, persistence, handoff context,
  and atomic confirmation.
- `supervisor/` owns orchestration decisions and live lifecycle state.
- `prepare_phase.py`, `phase_runner.py`, and `agent_turn_runner.py` execute phase work.
- `task_context.py` is the single confirmed-context preflight policy.

`ResearchRuntime` owns only four objects: immutable config, grouped services, grouped
session state, and the settings controller. Services are grouped as infrastructure,
durable state, and workflow; transient state is grouped as lifecycle, survey, compute,
and run options. No dynamic attribute forwarding is used.

## Required invariants

1. Every production clarification RPC is an explicit `ResearchRuntime` method.
2. Domain error codes survive Python, WebSocket, Rust, Tauri, and TypeScript.
3. Cancelling clarification returns the session to `IDLE`.
4. Critical missing task fields are canonical unresolved items.
5. Confirmation restores both memory and disk when commit fails.
6. Lifecycle launch can be retried after a committed confirmation.
7. Confirmed handoff corruption fails before workspace or agent side effects.
8. Human requests use the real research session ID.
9. Directory evaluator predictions join labels by exact `__athena_row_id`, never by
   row order or a partial filename.
10. Completed commands and one-shot agent cleanup have bounded delivery/drain waits, so
    broken Windows pipe readers cannot hold a terminal phase open.
