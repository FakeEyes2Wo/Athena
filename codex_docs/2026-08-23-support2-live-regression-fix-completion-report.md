# SUPPORT2 Live Regression Fix Completion Report (2026-08-23)

## Outcome

The fresh SUPPORT2 run exposed three pre-SEARCH regressions.  All three now
have compact fixes and fresh local verification:

- Headless output matches the active Windows console code page before a
  PowerShell 5.1 `Tee-Object` boundary and uses `backslashreplace` only for
  characters that the selected code page cannot represent.
- Descriptive target text such as
  `mortality_180d (1 = death within 180 days)` resolves only to an exact table
  header followed by an explicit description separator.  No fuzzy scientific
  target guessing was added.
- Factory-created workers receive the same Runtime block as directly-created
  prompt agents.  Native Windows workers are additionally stopped before
  common POSIX-only inspection commands reach PowerShell and receive an
  actionable PowerShell replacement.

The frozen Evaluation Policy, Human > Official > Protocol > AI precedence,
single Layer 2 batch review, fixed 30/30/20/20 weights, stored Selector score,
and deterministic Rubric fallback contracts were not changed.

## Root causes

1. `run_headless.py` forced UTF-8 even when legacy PowerShell decoded native
   output with another active console code page, producing mojibake in both the
   terminal and `console.log`.
2. task readiness stripped colon descriptions but did not recognize a target
   header followed by a parenthetical label definition.
3. `register_prompt_agent` omitted runtime-summary injection in its factory
   path.  The dispatched General Agent therefore did not know it was using
   native Windows PowerShell before its first shell call.

## Verification

- Focused encoding/readiness/agent/execution suite: `52 passed`.
- Related Agent, execution, Rubric, breakpoint-resume, Kaggle wiring, and task
  seeding suite: `233 passed`.
- Broader unit suite: `1676 passed, 2 skipped, 3 deselected, 50 subtests`.
- Real Windows PowerShell 5.1 + `Tee-Object` probe preserved readable
  `任务理解中：check ✓` in both console and log.
- Real prepared SUPPORT2 CSV: `readiness=READY`, `missing_items=0`, 9,106 lines
  including the header (9,105 observations).
- Black: 8 changed Python files left unchanged.
- `compileall` passed for the changed runtime modules.
- Repository style checker exited successfully; six existing non-blocking R3
  docstring notices remain.
- `git diff --check` passed.

The broad suite intentionally excluded `test/unit/agent/test_settings.py`
because the developer's real repository `.env` overrides that test module's
synthetic credentials at import time.  The same unrelated local-environment
contamination was already present before this repair.  No credential value is
recorded here.

## Remaining live acceptance

No paid DeepSeek run was started after this repair.  The next acceptance step
is a new SUPPORT2 `search-limit=2` project.  Do not resume the WAITING project
created before these fixes, because its persisted task understanding contains
the already-generated false missing-target result.
