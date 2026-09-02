# Rewrite Progress

## Completed production work

- Clarification is split into focused model, transition, generation, persistence,
  context, journal, handoff, controller, and confirmation modules.
- `ResearchRuntime` is a stable facade with four attributes: configuration, services,
  session state, and settings.
- PREPARE entry, confirmed-task preflight, phase execution, and agent turns have
  separate functional owners.
- Supervisor lifecycle, SEARCH state, output freshness, and evaluation are separated;
  duplicate lifecycle flags and forwarding paths were removed.
- All five clarification RPCs are present in the production Runtime. Domain error codes
  survive Python WebSocket, Rust/Tauri, and TypeScript boundaries.
- Cancellation returns to `IDLE`; critical missing fields are canonical unresolved
  items; the eighth answer produces a deterministic confirmation draft.
- Confirmation restores memory and disk on save failure, and a committed confirmation
  can retry lifecycle launch after PREPARE failure.
- Directory evaluation uses an exact `__athena_row_id` join and stable group-disjoint
  splits. EDA and evaluator prompts have bounded inputs.
- One-shot agent cleanup and Windows subprocess completion delivery are bounded, so a
  dead event consumer or pipe reader cannot hold a completed phase indefinitely.

## Production smoke completed

- Canonical project: `../task3/athena-jw-ssd-run-final`.
- CLI reached `COMPLETED / COMPLETED`; TUI reopened the same saved project and showed
  the unchanged terminal state.
- SEARCH used exactly two formal attempts. The compact histogram-gradient-boosting
  workflow won with frozen SEARCH macro-F1 `0.562185`.
- FINAL macro-F1 was `0.20987654320987656`; the recorded generalization gap was
  `0.35230845679012346`, with the warning enabled.
- PREPARE, selected SEARCH, and VALIDATE commits are recorded in the runbook.
- A redundant CNN diagnostic spawned during the first attempt was stopped to preserve
  the requested non-exhaustive two-attempt scope; the Athena CLI itself was not killed.

## Scope discipline

Focused leaf capabilities such as paper retrieval, survey, benchmark, and evaluation
were kept unless orchestration integration required a direct correction. Earlier failed
smoke projects and unrelated worktree changes remain untouched, and no files were
staged.
