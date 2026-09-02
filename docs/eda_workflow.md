# EDA Workflow

This document describes how EDA files are generated and consumed. The runtime
entry points live in `src/athena/research/prepare/eda.py`; phase ordering is
kept in `src/athena/research/prepare/orchestrator.py`.

## Flow

```text
PREPARE
  → create EDA worktree
  → `prepare/eda.py` (orchestrator turn 1)
      → writes EDA_TODO.md
  → Python todo runner
      → spawns eda_worker subagents
      → each worker writes one EDA_REPORT_*.md
      → marks - [x] on success
  → `prepare/eda.py` (orchestrator turn 2 / follow-up)
      → reads EDA_REPORT_*.md
      → writes EDA_INDEX.md
      → writes EDA_HANDOFF.md
  → baseline_ideator
      → reads EDA_HANDOFF.md
      → writes BASELINE_DESIGN.md
  → prepare agent
      → implements baseline from BASELINE_DESIGN.md
```

## Generated files

| File | Owner | Purpose |
|---|---|---|
| `EDA_TODO.md` | `prepare/eda.py` | checkbox task list |
| `EDA_REPORT_*.md` | eda_worker | detailed reports |
| `EDA_INDEX.md` | `prepare/eda.py` | navigation index |
| `EDA_HANDOFF.md` | `prepare/eda.py` | concise handoff for baseline |
| `BASELINE_DESIGN.md` | baseline_ideator | baseline implementation plan |

## Finding files

All EDA files are written under the EDA worktree. The absolute path is printed
in the PREPARE logs as `EDA report: <absolute path>`. If you cannot find a file,
check the log output for the resolved absolute path rather than guessing from
`workspaces/eda`.
