# Skip VALIDATE and Finalize After SEARCH Design

Date: 2026-09-04
Status: approved for implementation

## Problem

The GUI already exposes `auto_validate`, but that option does not disable the
VALIDATE stage. It selects between automatic validation and the existing human
gate:

- `auto_validate=true` advances from SEARCH to VALIDATE;
- `auto_validate=false` parks SEARCH at `WAITING` so a human can validate later.

The requested behavior is a third policy. A user must be able to finish SEARCH,
skip the independent VALIDATE evaluator entirely, generate a final report from
the SEARCH SOTA, and terminate the run as `COMPLETED`.

The backend has no separate `FINAL` phase. Its durable phase enum is PREPARE,
SEARCH, VALIDATE, or COMPLETED. Therefore this feature must not invent a fake
FINAL phase merely to match UI wording.

## Product decision

Add a separate `skip_validate` boolean. It defaults to `false`, takes precedence
over `auto_validate`, and preserves the current automatic and human-gated
validation behaviors when disabled.

The three supported post-SEARCH behaviors are:

| `skip_validate` | `auto_validate` | Result after SEARCH |
| --- | --- | --- |
| `true` | either value | Generate an explicitly unvalidated final report and enter `COMPLETED` |
| `false` | `true` | Run VALIDATE and then enter `COMPLETED` |
| `false` | `false` | Enter/remain at the human `WAITING` gate |

The preference is project-scoped for the GUI: every session in one project
shares it, session switches and GUI restarts retain it, and another project has
an independent value. CLI, TUI, library, and headless callers retain
`skip_validate=false` unless they explicitly opt in through the runtime
constructor.

## Alternatives considered

1. **Separate `skip_validate` policy (selected).** This preserves all existing
   workflows, exposes the requested third behavior directly, and requires no
   phase-enum migration.
2. **Replace the booleans with a `post_search_action` enum.** This is a tidy
   model, but it widens migration and compatibility work across GUI, CLI, TUI,
   tests, and external runtime callers without improving the requested result.
3. **Redefine `auto_validate=false` as skip.** This removes the current manual
   validation gate and silently changes existing callers, so it is rejected.

## Frontend behavior

The existing Settings panel gains a switch labelled `跳过 VALIDATE` with a
short explanation: SEARCH will proceed directly to Final, no independent final
evaluation will run, and no final-test or generalization metrics will exist.

`GuiSettings` and `DEFAULT_GUI_SETTINGS` gain `skip_validate: boolean`, defaulting
to `false`. The setting is included in the existing `settings_get` /
`settings_set` snapshot and patch; no new RPC method or transport DTO is added.

When `skip_validate` is selected, the existing `自动验证` input is visibly
disabled because its value has no immediate effect. Its stored value is not
changed. Turning skip off restores that prior choice, avoiding a surprising
switch from automatic validation to the human gate.

Saving the setting updates a running SEARCH immediately, so its next natural
completion uses the latest policy. Saving settings is not itself a terminal
workflow command: a run already parked at `SEARCH/WAITING` remains parked until
the user chooses Continue, after which the normal phase machine can finalize it.
An already entered VALIDATE phase is never cancelled or reclassified.

## Project-scoped GUI persistence

Extend the existing corrupt-tolerant `GuiStateStore` with a mapping from resolved
project roots to booleans, named `skip_validate_by_project`. This follows the
store's existing `last_sessions` project-keyed pattern while keeping the policy
outside user research checkpoints and outside `.env`.

The persistence rules are:

1. Missing files, missing fields, malformed mappings, non-string keys, and
   non-boolean values degrade entry-by-entry to the safe default `false`.
2. The gateway runtime factory resolves the active project root, reads its
   preference, and passes it to every runtime created for that project's
   default or named sessions.
3. A successful `settings_set` containing `skip_validate` updates the active
   runtime and then atomically writes the project mapping. Unsupported or
   non-boolean values are rejected by the runtime settings boundary and are not
   persisted.
4. Existing active-project and last-session fields remain intact on every
   preference write. Project/session switching likewise preserves the new map.
5. A persistence error is surfaced to the settings UI. The current in-memory
   runtime may already contain the accepted value, matching the existing
   settings controller's non-transactional multi-field behavior; a restart
   continues to use the last successfully persisted value.

The stored value is non-secret. Model credentials remain in the existing `.env`
path and are unrelated to this feature.

## Runtime configuration

Thread `skip_validate: bool = false` through the existing configuration layers:

```text
ResearchRuntime
  -> ResearchConfig
  -> RuntimeOptions
  -> PhaseActions
  -> PhaseMachine / SearchLoop
```

`SettingsController.snapshot()` returns it, the whitelist accepts it,
`SettingsController.apply()` validates that it is a real boolean, and
`Supervisor.configure_options()` updates the active `PhaseActions` value.

`auto_validate` keeps its current meaning. Wherever SEARCH currently uses
`auto_validate` to auto-settle an exhausted Plan rather than park for missing
human input, the effective auto-advance condition becomes
`auto_validate or skip_validate`. This lets skip mode actually finish SEARCH;
it does not bypass `manual_mode` hypothesis selection or other explicit human
controls.

## Phase transition and direct finalization

After `SearchLoop.run_search()` returns normally and the run is not stopped,
`PhaseMachine.continue_phase()` applies this order:

1. if `skip_validate`, finalize without validation and return;
2. otherwise, if `auto_validate`, persist the VALIDATE phase and run it;
3. otherwise retain the existing SEARCH budget/human gate behavior.

The direct finalizer has one responsibility: turn a completed SEARCH result into
an honest terminal result without invoking the validation adapter. It:

1. verifies the run is still in SEARCH, has no locally running Plan, and has a
   trusted SOTA experiment with a SEARCH evaluation;
2. writes `FINAL_REPORT.md` and `OPTIMIZATION.md` from the research tree with
   `validation=None` and an explicit validation-skipped notice;
3. writes the stable `final-skipped` experiment-stage record with
   `stage="final"`, `status="SKIPPED"`, no final primary score, the SEARCH SOTA
   score as a reference only, and provenance containing the SOTA id and commit;
4. records `validation_skipped=true` in backward-compatible per-run resume
   metadata, while leaving `state.validation=None`;
5. persists `phase="COMPLETED"` and `status="COMPLETED"` through the existing
   single state writer;
6. publishes an explicit human-facing completion message followed by the normal
   state snapshot.

The report text must say that no independent VALIDATE result was recorded and
that all shown scores came from SEARCH. It must not render `final_test_score`,
`generalization_gap`, or a successful-validation claim.

`validation_skipped` is an optional field stored in `resume.json`, not the
legacy core `state.json`. Older checkpoints therefore load with its default,
and older binaries ignore the additional resume metadata. Report generation
uses this run marker, rather than the user's current preference, so reopening an
old completed session remains historically accurate even if the setting later
changes.

## Ordering, failures, and recovery

Final-report and final-stage documents are derived, atomically replaced, and use
stable paths/ids. They are written before the terminal state commit. Repeating
the direct finalizer after a crash overwrites the same derived files and does not
add an experiment or duplicate a completion record.

If report or stage-document generation fails, terminal state is not committed.
The normal phase error boundary leaves a retryable failed SEARCH, and Continue
may retry finalization. If the state save fails after in-memory fields were
prepared, the helper restores their previous values before propagating the
error. Once terminal state is successfully persisted, output/subscriber failure
is logged and does not roll the run back.

Because the audit marker lives in digest-bound `resume.json` while terminal
phase/status live in `state.json`, a save with resume metadata writes the new
resume payload first and the matching core payload second. A failed resume write
therefore leaves the old core authoritative; a failed core write leaves a stale
new resume payload that the existing digest check ignores. A durable COMPLETED
core can never be intentionally committed before its skipped-validation marker.

Recovery follows these rules:

- persisted `SEARCH/RUNNING` resumes SEARCH and applies the currently persisted
  project policy when the search loop finishes;
- `SEARCH/WAITING` remains waiting until an explicit Continue/control action;
- `VALIDATE/RUNNING` continues the existing validation checkpoint path even if
  skip is now enabled;
- `COMPLETED` remains terminal and does not regenerate or rerun validation;
- missing/corrupt GUI preference data defaults to skip disabled
  (`skip_validate=false`), after which the existing `auto_validate` behavior is
  authoritative.

## Testing strategy

Use test-driven development and fake phase adapters; no network provider is
required.

Frontend tests cover:

- default and loaded switch states;
- the exact `settings_set` payload;
- disabling and restoring the `auto_validate` control without changing its
  value;
- the explanatory text that validation metrics will be unavailable.

Gateway/settings tests cover:

- project-keyed persistence across session recreation and gateway restart;
- isolation between two project roots;
- preservation of active-project and last-session state;
- missing, legacy, partially malformed, and corrupt state files;
- strict boolean validation and settings snapshot/live update behavior.

Supervisor/runtime tests cover:

- SEARCH goes directly to COMPLETED and the validation adapter is never called;
- `state.validation` stays `None` and `validation_skipped` survives reload;
- the final report and final-stage record disclose the skip and contain no final
  validation metrics;
- completion output/state ordering and idempotent retry after write failure;
- no completion while a Plan remains active;
- SEARCH/RUNNING, SEARCH/WAITING, VALIDATE/RUNNING, and COMPLETED recovery cases;
- `auto_validate=true` still validates and `auto_validate=false` still reaches
  the human gate when skip is disabled.

Verification includes focused Python and Vitest tests, the relevant research
and GUI gateway suites, the complete frontend suite and production build, Rust
tests/checks for the unchanged generic settings transport, formatting/static
checks, and scoped diff checks.

## Acceptance criteria

1. The GUI exposes a project-persistent `跳过 VALIDATE` option that defaults to
   off and survives session switches and GUI restarts within the same project.
2. With skip enabled, a normally completed SEARCH never calls the validation
   adapter and reaches `COMPLETED` without entering the VALIDATE phase.
3. The final report and final stage record explicitly identify the missing
   independent validation and never present SEARCH metrics as final-test data.
4. The actual run records its skip decision independently of the later GUI
   preference, while `state.validation` remains `None`.
5. Existing automatic validation, human-gated validation, manual SEARCH,
   pause/resume, and in-progress validation recovery retain their behavior.
6. Failed direct finalization remains retryable and cannot leave a durable
   `COMPLETED` claim without the derived final documents.
7. Focused and relevant full test/build/static-check gates pass with no new
   warnings in changed files.

## Out of scope

- Adding a new durable FINAL phase or changing the existing phase literals.
- Fabricating held-out scores, generalization gaps, or a validation result.
- Cancelling a validation run that has already started.
- Removing the existing human validation gate or changing `auto_validate`
  semantics.
- Persisting every existing Settings-panel field or redesigning the settings
  subsystem.
- Exposing the new GUI preference in CLI/TUI flags in this change.
