# EDA Workflow

This document describes how EDA files are generated and consumed.

## Flow

```text
PREPARE
  → create EDA worktree
  → prepare_eda (turn 1)
      → writes EDA_TODO.md
  → Python todo runner
      → spawns eda_worker subagents
      → each worker writes one EDA_REPORT_*.md
      → marks - [x] on success
  → prepare_eda (turn 2 / followup)
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
| `EDA_TODO.md` | prepare_eda | checkbox task list |
| `EDA_REPORT_*.md` | eda_worker | detailed reports |
| `EDA_INDEX.md` | prepare_eda | navigation index |
| `EDA_HANDOFF.md` | prepare_eda | concise handoff for baseline |
| `BASELINE_DESIGN.md` | baseline_ideator | baseline implementation plan |

## Finding files

All EDA files are written under the EDA worktree. The absolute path is printed
in the PREPARE logs as `EDA report: <absolute path>`. If you cannot find a file,
check the log output for the resolved absolute path rather than guessing from
`workspaces/eda`.
