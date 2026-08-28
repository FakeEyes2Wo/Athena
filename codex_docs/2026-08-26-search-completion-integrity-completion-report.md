# Search Completion Integrity Completion Report (2026-08-26)

## Outcome

The failures exposed by the fresh SUPPORT2 `search-limit=3` attempt have been
repaired without running another paid SUPPORT2 workflow.  SEARCH now fails
closed when candidate generation, Agent dispatch, structured output, or final
evaluation integrity is unavailable.  A small synthetic DeepSeek probe passed
for the normal/pro routes and the three structured boundaries requested for
live verification.

The frozen scientific contracts remain authoritative: task completeness is
checked before PREPARE; one Evaluation Policy is frozen before evaluator
creation; metric precedence is Human > Official > Protocol > AI; unknown
metrics fail closed; Layer 2 reviews one complete batch; and Selector consumes
stored scores without calling an LLM.

## Delivered behavior

### Bounded structured correction

- Deterministic JSON extraction and format-only repair remain the first two
  recovery steps.
- A semantically incomplete result receives at most two content-correction
  attempts from the original provider with the exact validation error and
  schema.
- Correction has no tools and cannot repeat an experiment or shell command.
- Exhausted correction raises an explicit failure and cannot fabricate a
  hypothesis, score, scientific value, or success state.

### Auditable SEARCH completion

- Total Ideator-lane failure and zero valid hypotheses now raise explicit
  errors; successful lanes are retained when only part of a batch fails.
- Automatic VALIDATE is refused until the requested number of technically
  completed SEARCH experiments is present.
- A failed Plan turn now persists one consumed turn and parks the run in
  `WAITING`.  It no longer immediately relaunches the same Plan and silently
  spends all remaining turns/API calls.
- Resuming creates a new scheduling task, while WAITING/STOPPED loops exit
  cleanly rather than leaving headless execution apparently hung.
- Terminal Agent failures append a safe JSONL summary containing run status and
  the public exception message, without private reasoning or credentials.

### Independent final evaluation

- PREPARE freezes two evaluator bundles under the same Evaluation Policy:
  `search/` for SEARCH and `final/` for VALIDATE.
- Both bundles must contain non-empty `labels.csv` files with exact
  `__athena_row_id` keys, and the two ID sets must be disjoint.
- Both artifact references are persisted and restored on resume.  An older
  single-evaluator checkpoint is treated as incomplete and reruns evaluator
  preparation instead of reusing SEARCH labels as a final score.
- VALIDATE requires the distinct final evaluator and fails closed when it is
  absent, identical to SEARCH, unreadable, or overlapping.

### Runtime and Windows hardening

- Durable state has one runtime-owned persistence path for initial start and
  resume-sensitive transitions.
- EDA completion reuses usable reports, bounds worker retries/timeouts, and
  cancels a timed-out worker before falling back.
- Native Windows workers receive PowerShell commands rather than Unix-only
  `head`/`wc` guidance.
- Headless output remains UTF-8/PowerShell pipe safe and records effective
  fresh/resume configuration.

## Fresh verification

- Full unit suite before the final scheduler guard:
  `1652 passed, 2 skipped, 50 subtests passed`.
- Full research integration after the scheduler guard:
  `82 passed` (one non-fatal Windows asyncio subprocess finalizer warning).
- Final broad unit run after all implementation/format changes:
  `1649 passed, 2 skipped, 50 subtests passed`; three unrelated, wall-clock
  threshold tests timed out under load.  Those exact three tests passed on an
  immediate isolated rerun: `3 passed`.
- Failed-dispatch/no-retry-storm focused regression: `2 passed`.
- Black: `46 files would be left unchanged`.
- Repository style checker: exit code 0; 53 non-blocking R3 docstring notices
  remain and no hard-rule violations remain.
- `compileall`: exit code 0.
- `git diff --check`: exit code 0.

## Synthetic DeepSeek verification

`scripts/probe_deepseek_contracts.py` made three bounded calls using synthetic
text only; it did not read or upload SUPPORT2 or another research dataset.

- reasoning model (`deepseek-v4-pro`): semantic correction converted an
  intentionally invalid empty Ideator batch into one valid hypothesis;
- reasoning model (`deepseek-v4-pro`): Layer 2 returned exactly two reviews for
  two requested IDs and emitted private thinking content;
- normal model (`deepseek-v4-flash`): VALIDATE returned a valid
  `ValidationRepair` object without thinking mode;
- aggregate result: `PASSED`.

The process emitted a non-fatal `httpcore2` asynchronous-generator shutdown
warning after all three results completed; exit code remained 0.

## Remaining live acceptance

No fresh paid SUPPORT2 run was started by this task.  The remaining human
acceptance step is a brand-new full-cohort SUPPORT2 project with
`search-limit=3`, `ideator-count=3`, `hypotheses-per-ideator=5`, and
`--pro-reasoning`.  It should be run only when the user chooses to spend the
external API budget.
