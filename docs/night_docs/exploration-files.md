# Durable Exploration Files

## Problem

Ideator Agents already receive workspace-bound `write_file` and `append_file`, but the
SEARCH request tells them not to modify files. Their evidence and discarded ideas live
only in one Agent turn, while later rounds see only hypotheses registered in the graph.

## Minimal design

- Each concurrent lane owns `exploration/<lane-id>/` under the EDA workspace.
- The lane may write diagnostic scripts, figures, and `evidence.md` there.
- PREPARE outputs, the frozen split/evaluator, and baseline files remain read-only.
- Athena always snapshots the accepted structured batch to the lane's `result.json`.
- Later Ideators inspect earlier lane results before repeating exploration.
- Each Plan worktree receives an append-only `EXPERIMENT_LOG.md` for trusted scores and
  failures. Markdown-only changes still fail the existing implementation-source gate.

No new persistence service is introduced. Ideator notes use normal workspace files;
Plan logs travel through the existing Git diff/commit and artifact pipeline.

## Boundary

The file tools reject absolute paths, `..`, symlink escapes, and `.athena/**` writes.
The shell runtime is process isolation, not a hostile filesystem sandbox; this design
does not claim otherwise.
