# JW-SSD Smoke Runbook

## Dataset contract

Path: `../task3/JW-SSD_Dataset`

The smoke uses the paired PNG tree. Labels are directory names under each modality:
`alpha`, `beta`, `beta-delta`, `beta-gamma`, and `beta-gamma-delta`.

- continuum: 520 images
- magnetogram: 520 images
- paired observations: 520 exact HARPNUM/timestamp pairs
- active-region groups: 21 unique HARPNUM values
- group-disjoint TRAIN/SEARCH/FINAL observations: 383/50/87

Every timestamp sharing a HARPNUM remains in one split. Random image-level splitting
would leak active-region history and is not used.

## Canonical command

Run from the Athena repository:

```powershell
$dataset = (Resolve-Path '..\task3\JW-SSD_Dataset\images').Path

uv run Athena-cli run `
  --project ..\task3\athena-jw-ssd-run-final `
  --data $dataset `
  --data-root $dataset `
  --data-type image `
  --task-type classification `
  --metric macro_f1 `
  --direction maximize `
  --max-search-experiments 2 `
  --mode auto `
  --timeout 3600 `
  --experiment-timeout 600 `
  --task "Build the best compact JW-SSD sunspot classifier that can be validated locally. Use directory names as labels and both modalities when useful. Prevent temporal/active-region leakage from filenames. Optimize macro-F1; report the selected workflow, confusion matrix, TP/FP/TN/FN, TSS, HSS, precision, POD/recall, F1, FAR, accuracy, and ROC-AUC/PR-AUC when probabilities are available."
```

The project directory is restart-safe. Re-running the same command against it resumes
durable state instead of creating extra formal SEARCH attempts.

## Recorded result

- PREPARE trusted baseline macro-F1: `0.364384`;
  commit `18ab485f72ad06cc1aa0b77b95f43f9300f916fa`.
- Attempt 1: two-branch normalized CNN, score `0.1600`, abandoned.
- Attempt 2: per-image z-score, downsampled 8x8 spatial features, compact histogram
  gradient boosting; frozen SEARCH score `0.562185`.
- Selected experiment: `exp_hyp_532038e014e8`;
  commit `57812ae9976203b39e1ff49e9f58ccb7ef80f9a6`.
- FINAL score: `0.20987654320987656`; gap `0.35230845679012346`;
  validation commit `fe7e98dc7ae53320fc613c624590037904caf10a`.
- Evidence/report/predictions refs are recorded in `verification.md` and pass their
  manifest SHA256 self-checks.

The project contains the frozen split/evaluator artifacts, experiment evidence,
selected classifier workflow, validation result, and durable runtime state. Earlier
failed smoke projects `athena-jw-ssd-run` and `athena-jw-ssd-run-2` are intentionally
preserved for diagnosis.

## Selected classifier files

The winning implementation is frozen at commit
`57812ae9976203b39e1ff49e9f58ccb7ef80f9a6` on branch
`hyp_532038e014e8` inside `.athena/repo`. The internal repository intentionally leaves
`main` at its initial commit; use the recorded commit instead of the working-tree HEAD:

```powershell
git -C ..\task3\athena-jw-ssd-run-final\.athena\repo show `
  57812ae9976203b39e1ff49e9f58ccb7ef80f9a6:solution/data.py
git -C ..\task3\athena-jw-ssd-run-final\.athena\repo show `
  57812ae9976203b39e1ff49e9f58ccb7ef80f9a6:solution/train_model.py
git -C ..\task3\athena-jw-ssd-run-final\.athena\repo show `
  57812ae9976203b39e1ff49e9f58ccb7ef80f9a6:solution/predict.py
```

`data.py` performs paired loading and per-image z-scored 8x8 morphology extraction;
`train_model.py` trains the group-aware histogram-gradient-boosting classifier; and
`predict.py` emits exact `__athena_row_id` predictions. The frozen commit also contains
`solution/models/model.joblib` and `solution/models/train_report.json`.

This frozen workflow is directly replayable on the machine that produced it. The
generated `data.py` embeds the current dataset's absolute path and uses unsorted
directory enumeration, so portability requires parameterizing the root and sorting
filenames before claiming bit-for-bit cross-machine reproduction.

## TUI resume check

```powershell
uv run Athena-cli status --project ..\task3\athena-jw-ssd-run-final
uv run Athena-tui --project ..\task3\athena-jw-ssd-run-final
```

The TUI must show `COMPLETED / COMPLETED`, 2/2 attempts, one success, and SOTA `0.5622`.
Exit without starting a new task, then run the status command again to confirm the
terminal state is unchanged.
