# Skip VALIDATE and Finalize After SEARCH Completion Report

Date: 2026-09-04
Design: `docs/superpowers/specs/2026-09-04-skip-validate-final-design.md`

Implementation and integration commits:

- Design, plan, and baseline: `9c88f99`, `0cf0580`, `c2e6571`
- Runtime policy and live settings: `4f3de3d`, `d3f9e56`
- Project preference persistence: `ec27c25`, `22b9eb0`
- Durable disclosure and reports: `91904c6`, `4db6463`
- SEARCH finalization and recovery boundaries: `517a08e`, `ca2212a`, `95d9a0a`
- GUI setting and accessibility coverage: `4f30cee`, `2acbd1b`
- Cross-layer proof: `fc95f94`
- Documentation and review fixes: `c356ebb`, `6597eb9`, `ef4ffcb`, `b938173`
- Integration with concurrent `main`: `96af25d`, `162d358`
- Merged-main evidence record: `80baf54`

## Delivered behavior

- Added the independent public boolean setting `skip_validate`, defaulting to
  `false` without changing `auto_validate` defaults or meaning.
- Added an accessible **跳过 VALIDATE** control to the GUI. Enabling it disables
  the automatic-validation checkbox visually and functionally while preserving
  that checkbox's stored value.
- Persisted the preference by resolved project root. It survives session changes
  and gateway restarts and remains isolated from other projects.
- Gave skip mode precedence after SEARCH. Once SEARCH has settled its Plan and
  SOTA, Athena writes Final/optimization documents and durably enters
  `phase="COMPLETED", status="COMPLETED"` without entering VALIDATE or invoking
  the held-out evaluator.
- Added the resume-only `validation_skipped=true` audit marker. Report
  regeneration uses that durable run fact rather than the project's current GUI
  preference.
- Kept the existing automatic VALIDATE, manual WAITING gate, entered-VALIDATE
  recovery, and completed-run no-op behaviors intact.

## Acceptance-criteria evidence

| Criterion | Evidence | Result |
| --- | --- | --- |
| Setting is separate, default-off, live, and strict-boolean | `test_runtime_settings.py` constructor, projection, update, and invalid-value cases | Passed |
| Preference is project-scoped and restart-persistent | `test_state_store.py`, `test_gui_gateway_main.py`, and gateway handler restart/isolation cases | Passed |
| WebSocket contract preserves exact `skip_validate` key | `test_transport_round_trips_skip_validate_with_exact_snake_case_key` | Passed |
| Skip path never calls validation | `test_skip_validate_completes_from_search_without_calling_validation` uses a raising validation adapter and call-count assertions | Passed |
| Final output is honest and contains no fabricated final metrics | `test_report_discloses_skipped_validate_without_final_metrics` and `test_write_reports_discloses_skipped_validation_without_final_metrics` | Passed |
| Durable completion is idempotent and recoverable | finalizer save-failure/notification tests and retry-without-duplication integration test | Passed |
| Resume matrix retains documented boundaries | SEARCH/RUNNING, SEARCH/WAITING, VALIDATE/RUNNING, and COMPLETED cases in `test_breakpoint_resume.py` and `test_human_plan_boundary.py` | Passed |
| UI disables but preserves `auto_validate` and explains the consequence | `SettingsPanel.test.tsx` loaded/off/on/save cases | Passed |
| Existing auto/manual validation paths remain compatible | focused and broad Python suites plus existing frontend/Rust suites | Passed |

## Verification commands and results

- Backend formatting/static checks: Black checked 26 changed Python files and
  `compileall` passed. Broad Ruff retained repository-baseline findings; the
  normalized changed-path signature set matched the feature baseline exactly,
  with no added finding.
- Focused Python suite on the feature/integration tree: 242 passed.
- Broad Python research/gateway suite on the integrated code tree: 1348 passed
  in 363.53 seconds, exit 0.
- Fresh merged-`main` focused Python suite: 242 passed in 107.07 seconds, exit 0.
- Fresh merged-`main` frontend suite: 19 files and 183 tests passed.
- Fresh merged-`main` production build: TypeScript and Vite passed; 2761 modules
  transformed.
- Fresh merged-`main` Rust verification: 10 tests passed and `cargo check`
  passed.
- `git diff --check` passed after integration and again on merged `main`.
- Independent final feature review and merge-specific review both reported zero
  Critical, Important, or Minor findings and approved integration.

## Persistence and recovery evidence

`GuiStateStore` tolerantly reads a project-keyed boolean map and ignores malformed
entries. The gateway restores the resolved project's value for each runtime and
persists only the validated value returned by `settings_set`, preserving the
active project and remembered sessions. Tests recreate both the store and runtime
factory to prove restart durability and use a second project to prove isolation.

For an actual run, only `resume.json` carries `validation_skipped`. Historical
core state remains compatible. A reopened completed run regenerates the same
skipped-validation disclosure even after the live project preference is turned
off. SEARCH/WAITING still requires explicit Continue; an already entered VALIDATE
run finishes real validation; COMPLETED remains a no-op.

## Honest-report audit

The skip finalizer requires a settled SEARCH result, passes `validation=None`, and
never calls `run_validation_phase`. Both Final and optimization documents state
that independent validation was skipped and omit final-test score and
generalization-gap fields. Completion is committed only after those documents are
available. Ordinary notification failures after the durable save cannot roll back
completion, while cancellation still propagates.

## Compatibility

With `skip_validate=false`, `auto_validate=true` still advances through VALIDATE
and `auto_validate=false` still uses the human WAITING gate. The new
`PhaseActions` field is appended with a default to preserve historical positional
construction. Old/corrupt GUI state falls back to `false`, and old run state with
no audit marker preserves its previous semantics.

The integration retained the reviewed feature implementation of `exp_docs.py`.
Current call signatures remain compatible, callers do not depend on its additive
return values, and the queued experiment-document projection plan explicitly
re-audits and replaces that module after its predecessor plans complete.

## Pre-existing warnings or unrelated failures

- The first isolated Cargo run could not fetch `hatchling` because a PyPI TLS
  handshake ended early. The identical `uv run` build/import boundary then
  succeeded without a code change, and Cargo passed all 10 tests on retry.
- Broad Python verification emitted the known Windows asyncio subprocess cleanup
  warning. Merged-main focused verification emitted the existing pytest-cache ACL
  warning. Both commands exited 0.
- Rust emitted six existing dead-code warnings. Gateway teardown logged the known
  Windows WebSocket connection-reset traceback; Rust tests still exited 0.
- Integration `npm ci` reported 8 dependency audit findings (1 low, 4 moderate,
  2 high, 1 critical). This feature changed no dependency manifest or lockfile.
- A running main-worktree Vite server locked `esbuild.exe`, so an attempted
  `npm ci` could not replace it. Missing dependency files were restored from the
  verified integration installation with the identical package-lock hash, without
  overwriting existing files or changing tracked dependency files. Standard
  merged-main frontend tests and build then passed.

## Unrelated worktree changes preserved

The final fast-forward overlap audit preserved these uncommitted main-worktree
paths exactly and excluded them from every feature/closeout commit:

- `src/athena/core/persistence.py`
- `src/athena/research/evaluation/trust.py`
- `test/unit/research/test_evaluator_trust.py`

No stash, reset, destructive checkout, safety commit, remote push, or dependency
lockfile change was performed.
