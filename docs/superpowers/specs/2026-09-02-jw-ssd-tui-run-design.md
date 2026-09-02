# JW-SSD TUI Search-3 Run Design

Date: 2026-09-02
Status: approved approach, awaiting written-spec review

## Goal

Run Athena through its interactive TUI on the JW-SSD dataset under `../task3`, with a
SEARCH budget of three and automatic VALIDATE disabled. Store the complete reusable
task instruction in `../task3/init_prompt.md` before starting the TUI.

## Dataset contract

The dataset contains 520 observations. Each observation has paired continuum and
magnetogram inputs in PNG and FITS forms. Labels are encoded by directory name:

- `alpha`: 200
- `beta`: 200
- `beta-delta`: 25
- `beta-gamma`: 75
- `beta-gamma-delta`: 20

There are only 21 distinct HARPNUM activity regions. All model-selection splits must
therefore be grouped by HARPNUM. Frames from one HARPNUM must never appear in more than
one of train, SEARCH evaluation, or FINAL evaluation. Random frame-level splitting is
invalid because adjacent observations describe the same active region over time.

The initial implementation should use paired PNG continuum and magnetogram images.
FITS files remain available for later hypotheses but are not required for the first
trusted baseline.

## Task and metrics

The primary task is five-class Mount Wilson classification. The primary score is
macro-F1, because the three complex classes are much smaller than `alpha` and `beta`.
The evaluator also reports balanced accuracy, per-class recall, and a confusion matrix.

For compatibility with the competition templates, every prediction is also folded to
a binary complexity task:

- negative: `alpha`, `beta`
- positive: `beta-delta`, `beta-gamma`, `beta-gamma-delta`

The binary report includes TP, FP, TN, FN, TSS, HSS, precision, recall/POD, F1, FAR,
accuracy, ROC-AUC, and PR-AUC when probabilities are available. Thresholds and model
selection use only train/validation data, never FINAL labels.

## Reusable prompt

`../task3/init_prompt.md` will contain:

- the absolute dataset path and paired-file identity rule;
- the five-class label mapping;
- the HARPNUM split-isolation requirement;
- macro-F1 as the SEARCH and FINAL primary metric;
- the binary complexity fold and required secondary metrics;
- expected prediction and metrics filenames adapted from the submission templates;
- leakage prohibitions, reproducibility requirements, and a unique inference entry;
- baseline expectations: use a credible pretrained vision backbone with transfer
  learning or fine-tuning when practical, not a toy CNN;
- a requirement that exploration notes and experiment evidence are written to files.

Expected prediction schema:

```text
image_filename,pred_label,prob_alpha,prob_beta,prob_beta_delta,prob_beta_gamma,prob_beta_gamma_delta
```

Expected task id: `JWSSD_MW5`.

## TUI interface

Add only two command-line arguments to `Athena-tui`:

- `--search-limit N`, passed directly to `ResearchRuntime.search_limit`;
- `--validate`, a `store_true` flag passed to `ResearchRuntime.auto_validate`.

`--validate` defaults to false. No new configuration layer or mode abstraction is
introduced. Existing `--project` behavior remains unchanged.

The concrete run uses:

```powershell
.venv\Scripts\Athena-tui.exe `
  --project ..\task3\athena-jw-ssd-tui-search3 `
  --search-limit 3
```

The first TUI message is the complete content of `../task3/init_prompt.md`. VALIDATE is
not started automatically after SEARCH. The user may later request it explicitly.

## Isolation and persistence

The run project is `../task3/athena-jw-ssd-tui-search3`, separate from the dataset and
the three existing historical run directories. This prevents accidental resume or
workspace reuse. Athena artifacts, evaluators, experiments, worktrees, and logs remain
inside that run project.

## Error handling

- Refuse to start if `JW-SSD_Dataset` or its README is missing.
- Refuse invalid non-positive SEARCH budgets through argparse validation.
- Do not silently fall back to frame-level random splits.
- Do not start VALIDATE unless `--validate` is supplied or the user later requests it.
- If TUI startup fails, preserve the run directory and report the exact process error;
  do not delete partial artifacts.

## Verification

Before launch:

1. Test TUI argument parsing and runtime wiring for budget three and default-off
   validation.
2. Confirm `init_prompt.md` exists and names the actual dataset, five classes,
   HARPNUM isolation, macro-F1, binary fold, and output schema.
3. Confirm the fresh run directory does not reuse a prior state.

During launch, verify that the TUI opens against the requested project and accepts the
prompt. Completion of the full model search is monitored separately because it depends
on external model calls and experiment runtime.

## Out of scope

- Redesigning Athena's research algorithm or evaluator format.
- Adding automatic dataset-specific code to Athena.
- Running VALIDATE during this invocation.
- Reusing or modifying previous JW-SSD experiment directories.
