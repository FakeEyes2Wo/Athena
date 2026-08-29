# SUPPORT2 Post-Success Hardening Completion Report

## Outcome

The completed full-data SUPPORT2 result was preserved before modification, and
four local runtime weaknesses observed in that run were hardened without
changing any scientific contract.

Preserved contracts:

- full SUPPORT2 dataset: 9,105 rows, no sampling;
- frozen Evaluation Policy: `roc_auc`, `maximize`;
- Rubric weights and exact-ID validation;
- Selector precedence and deterministic fallback;
- `search-limit=3`, Ideator budgeting, breakpoint resume, and final validation;
- no commit, push, PR, or paid DeepSeek rerun.

## Backup

The pre-hardening successful version is stored at:

`C:\Users\15055\Documents\Codex\2026-08-17\1\outputs\support2-success-backup-20260828`

It contains the recorded branch/HEAD, binary Git patches, all 61 modified or
untracked files, restoration instructions, and the completed run's state,
research tree, and console log. All 61 copied files were SHA-256 verified with
zero mismatches.

## Changes

### Layer 2 Rubric completeness

- `HypothesisPriorityBatch.reviews` now requires at least one item, so an empty
  model batch reaches the existing bounded semantic correction instead of
  passing the schema boundary and failing later.
- Exact candidate-ID coverage remains strict. Missing or unexpected IDs still
  use the existing deterministic fallback.
- Success and fallback output now carry stable `rubric_status` markers.
- Fallback reasons and expected IDs are persisted as an artifact when storage
  is available; artifact persistence itself is best-effort so it cannot disable
  fallback.

### DeepSeek structured correction

- DSML filtering now covers the observed doubled ASCII/full-width bar framing,
  including `<｜｜DSML｜｜tool_calls>` across stream chunk boundaries.
- The single bounded semantic correction pass receives system/user text,
  assistant prose, and bounded quoted tool evidence, but no `ThinkingPart`,
  `ToolCallPart`, or `ToolReturnPart` protocol objects.
- Retry count and paid-call behavior were not expanded; no hypothesis is
  fabricated.

### Formal experiment diagnostics

- Formal experiment failures now preserve `CommandResult.error`, so a command
  killed by the existing bounded timeout reports `timeout` instead of the
  ambiguous `command failed (exit -1):` message.
- No global timeout was increased.

### Redirected UTF-8 output

- `PYTHONIOENCODING` and `PYTHONUTF8` now take precedence over a detected Windows
  console code page.
- Sessions without an explicit Python encoding retain the existing PowerShell
  5.1 console-compatible behavior.

## Verification

Fresh offline evidence:

- focused regression selection: `27 passed, 94 deselected`;
- complete affected-file suite: `121 passed` (run twice, including after Black);
- adjacent frozen-contract suite: `106 passed`;
- final fallback workflow check: `4 passed`;
- Black check: 12 affected files unchanged after formatting;
- `python -m compileall -q src/athena scripts/run_headless.py`: passed;
- `git diff --check`: passed.

No DeepSeek request or full SUPPORT2 run was used for this implementation pass.
A future paid run should use a new project directory so the preserved completed
run remains immutable evidence.
