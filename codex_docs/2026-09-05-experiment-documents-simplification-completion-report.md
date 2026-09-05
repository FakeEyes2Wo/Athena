# Experiment Documents Simplification Completion Report

Date: 2026-09-05

## Delivered

The experiment-document subsystem was reduced from six production files to four while preserving its persisted JSON, Markdown, manifest, recovery, and sanitized-failure contracts.

- Deleted `experiment_documents/metric.py` and folded its single-use resolver into the projector.
- Deleted `experiment_documents/render.py`; canonical JSON/manifest encoding now belongs to the store, while both reports live in `research.report`.
- Deleted the redundant `MetricObservation`, `StageEvent`, and `RunCandidate` classes.
- `StageRecord.from_event()` now validates a raw mapping directly and rejects projector-owned fields supplied by callers.
- `ProjectionBatch` now stores four fields instead of six; stage/run identity and archive bytes are derived from validated records.
- The projector boundary is now `project(event, context)` and `rebuild(context)`, replacing five repeated state keywords. `ProjectionContext` itself stores only `tree`, `state`, and `direction`.
- PREPARE, SEARCH settlement, VALIDATE, skipped validation, recovery, test doubles, and integration callers use the new boundary.

Measured package size fell from 1,205 to 898 nonblank source lines (about 25%), and the change has 164 fewer production lines overall.

## Verification

- Experiment-document tests: **88 passed**.
- Focused report/projector/Supervisor/integration run: **172 passed**.
- Post-simplification model/store/Supervisor run: **129 passed**.
- Autonomous, recovery, and rolling integration files: **8 + 13 + 12 passed**.
- Ruff on the changed subsystem and focused tests: **passed**. The wider pre-existing `phases.py` unused `tb` finding is outside this task.
- Import/API smoke and `Athena-cli --help`: **passed**.
- `git diff --check`: **passed**.

Full suite: **2,852 passed, 2 failed, 1 warning, 53 subtests passed** in 264.65 seconds. Both failures are the unchanged Windows environment checks in `test/unit/execution/test_encoding.py`: this host has Windows PowerShell 5.1 but no `pwsh` (PowerShell 7), so the shell-selection assertion and the PowerShell-7 `&&` assertion fail. No product test failed.

## Delivery and cleanup contract

This task is not considered delivered until its feature commit is fast-forwarded into `main`, `origin/main` is pushed, the isolated worktree is removed, and the temporary local feature branch is deleted. Unrelated main-worktree changes must remain untouched throughout that sequence.
