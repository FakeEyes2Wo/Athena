# Task 3 Completion Report

## Scope

Implemented the resume-only `validation_skipped` run marker, honest skipped-
validation disclosures in both report families, and GUI report regeneration
from the durable run marker.

## TDD evidence

RED command (with worktree-local `PYTHONPATH`):

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
..\..\.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_state.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py
```

Key output: `7 failed, 37 passed in 2.84s`. Failures were the expected missing
marker, resume ordering, report keyword, and experiment-document keyword
contracts.

GREEN and formatting/check command:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
..\..\.venv\Scripts\python.exe -m pytest -q test/unit/research/supervisor/test_state.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py
..\..\.venv\Scripts\python.exe -m black src/athena/research/supervisor/state.py src/athena/research/report.py src/athena/research/exp_docs.py src/athena/gui/service.py test/unit/research/supervisor/test_state.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py
git diff --check -- src/athena/research/supervisor/state.py src/athena/research/report.py src/athena/research/exp_docs.py src/athena/gui/service.py test/unit/research/supervisor/test_state.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py
```

Key output: `44 passed in 2.11s`; Black reformatted `exp_docs.py` and reported
all remaining files unchanged; `git diff --check` passed.

## Files changed

- `src/athena/research/supervisor/state.py`: resume-only marker and
  resume-first atomic save ordering.
- `src/athena/research/report.py`: skipped-validation notice, contradiction
  guard, and exported notice constant.
- `src/athena/research/exp_docs.py`: threaded keyword through final,
  optimization, and file writers; omitted validation metrics in skip mode.
- `src/athena/gui/service.py`: report regeneration reads only
  `runtime.state.validation_skipped`.
- Focused state, report, and experiment-document tests covering round trips,
  stale metadata, crash windows, disclosures, and contradictions.

## Self-review

- `validation_skipped` is excluded from legacy `state.json` by `RESUME_FIELDS`.
- Resume metadata is written before a matching core when non-empty; a failed
  resume write leaves the old core authoritative, and a failed core write leaves
  a digest-mismatched resume that is ignored.
- Missing and stale resume metadata defaults/loads as `None`.
- Skip reports contain the explicit `VALIDATE 已跳过` / `仅使用 SEARCH 结果`
  disclosure and do not render validation metric rows or generalization-gap
  guidance.
- Truthy validation plus the skip marker raises `ValueError`.
- Existing non-skip call signatures and output remain unchanged.
