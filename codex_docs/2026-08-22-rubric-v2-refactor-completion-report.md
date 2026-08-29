# Rubric V2 Refactor Completion Report (2026-08-22)

## Outcome

Athena main commit `0fd803d` now has a compatible compact reimplementation of
the previous Rubric V2 feature plus the new task-completeness wrapper. The code
implementation, local automated acceptance suite, and a real DeepSeek-driven
medical workflow are complete. The isolated `search-limit=1` run reached
`phase=COMPLETED`, `status=COMPLETED`.

## Delivered behavior

- Task Understanding now records one shared, strict contract for task, dataset,
  target, metric provenance, constraints, confidence, missing items, warnings,
  questions, and readiness.
- Deterministic readiness checks validate local dataset existence, non-empty
  files, readable CSV/TSV headers, supervised targets, and task type. Critical
  gaps pause before PREPARE and ordinary Human replies trigger reassessment.
- Layer 1 runs after task understanding and before evaluator creation. It
  freezes one primary Evaluation Policy with deterministic precedence:
  Human > Official > Protocol > AI. Unknown metrics fail closed and never
  silently become Accuracy.
- The frozen policy is stored as an artifact, restored on resume, applied to
  Supervisor comparison direction, supplied to the evaluator, and checked
  against `metric.json` before the evaluator bundle can freeze.
- Layer 2 runs once for the complete post-Gate hypothesis batch. It reviews
  four dimensions with fixed weights: Evidence & Testability (30%), Scientific
  Value (30%), Execution Efficiency (20%), and Validity & Risk Control (20%).
- Execution Efficiency retains auditable latency, compute, memory, API-cost,
  and implementation-effort penalties. Aggregation is deterministic.
- Selector performs no LLM call. It consumes the stored Layer 2 score and keeps
  the existing deterministic prior as the no-model/error fallback.
- Frozen Python entrypoints retain a UTF-8 BOM at byte zero and insert the
  Athena marker after it, preventing the Windows `U+FEFF` syntax failure.

## Maintainability choices

- Supervisor and GUI use the same `TaskUnderstanding` model instead of
  maintaining duplicate schemas and metric guessing.
- Pure policy, readiness, aggregation, and validation logic lives in small
  `research/rubrics` modules and is testable without an LLM.
- Both structured Rubric agents share one registration factory.
- Runtime-facing calls are isolated in one `RubricWorkflow`; the existing
  `AgentTurnRunner`, Supervisor, PREPARE, and Selector receive only thin
  integration changes.
- The previous feature commit was used as a behavioral reference, not
  cherry-picked wholesale. Unrelated team code was not refactored.

## Acceptance evidence

- 169 latest-main targeted Rubric and merge-regression tests passed.
- 1,666 unit tests passed; 2 skipped; 3 known provider/environment assertions
  deselected; 50 subtests passed.
- 66 research integration tests passed; a transient rolling-search timeout
  passed in isolation on both clean main and the merged worktree. The remaining
  state-authority assertion was reproduced unchanged on clean `0fd803d`.
- Black check passed for 31 owned Rubric-related Python files and tests.
- Repository style check exited 0; its 42 R3 docstring messages refer to
  pre-existing GUI/runtime/Supervisor public methods and are non-blocking.
- Compileall and `git diff --check` passed.
- A real DeepSeek run on the public 569-row Wisconsin Breast Cancer dataset
  reached `READY`, froze a Human `roc_auc/maximize` policy, produced one Layer 2
  batch for six post-Gate hypotheses, selected a stored Rubric score, improved
  the trusted score from `0.9960317460` to `0.9993386243`, and independently
  validated the final score with generalization gap `0.0000`.

See `RUBRIC_V2_TEST_REPORT.md` for exact scope, baseline exclusions, and the
live-smoke limitation.

## Handoff

No feature commit, push, or pull request was created. Review the worktree and
generated patch, then create the team branch and commit through GitHub Desktop
or Git.

## Operational observations for team follow-up

- Evaluator labels contain no stable source-row identifiers. PREPARE had to
  reconstruct the evaluator's `random_state=42` unsorted split from label order
  before it could align predictions. This is an existing evaluator contract
  weakness, not introduced by Rubric V2.
- Very deep Windows project paths can prevent compiled scikit-learn extensions
  from loading even when their `.pyd` files exist. The same workflow succeeded
  from the short isolated path `C:\Users\15055\Documents\Codex\rv2_smoke_0822`.
- Agents initially emitted some Linux shell syntax on Windows, then recovered
  with PowerShell/cmd commands. A future platform prompt can reduce this noise.
