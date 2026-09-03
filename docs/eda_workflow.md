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
  → EDA_HANDOFF.md
      → baseline web/paper research
      → BASELINE_RESEARCH.json + BASELINE_DESIGN.md
      → platform Git/OpenAlex verification
      → BASELINE_RESEARCH_VERIFICATION.json
      → prepare implementation
      → trusted scoring
```

Before implementation, the baseline ideator records its search queries, candidate
decisions, and a modality-aware data-regime/training-strategy assessment. Athena then
qualifies the selected source independently. It tries a public HTTPS Git repository
first; if no repository qualifies, a matching OpenAlex work with at least 100
citations can satisfy the authority exception. Agent-reported citation counts are not
authoritative.

If the artifacts or source fail validation, the same ideator gets one structured
repair turn and must rewrite both complete artifacts. A second failure is terminal:
PREPARE stops before the prepare agent is registered, so there is no task-only
fallback. The prepare agent runs only after the verification artifact exists and the
three files agree on the selected candidate and training strategy.

Git qualification uses a disposable, no-checkout shallow clone to establish that the
repository is publicly retrievable and to pin the reachable commit. Cloneability
proves retrievability, not repository safety. Athena does not checkout, import,
install, copy, or execute third-party repository content during qualification.

## Generated files

| File | Owner | Purpose |
|---|---|---|
| `EDA_TODO.md` | `prepare/eda.py` | checkbox task list |
| `EDA_REPORT_*.md` | eda_worker | detailed reports |
| `EDA_INDEX.md` | `prepare/eda.py` | navigation index |
| `EDA_HANDOFF.md` | `prepare/eda.py` | concise handoff for baseline |
| `BASELINE_RESEARCH.json` | baseline_ideator | versioned research, candidate decisions, and data/strategy evidence |
| `BASELINE_DESIGN.md` | baseline_ideator | human-readable design matching the selected candidate and strategy |
| `BASELINE_RESEARCH_VERIFICATION.json` | Athena platform | Git commit or OpenAlex authority evidence bound to the research digest |

The three baseline files are durable workspace artifacts. On resume, Athena reuses a
verification only when its schema, selected candidate, and SHA-256 digest still match
the current research file; otherwise it validates and verifies the artifacts again.

## Finding files

All EDA files are written under the EDA worktree. The absolute path is printed
in the PREPARE logs as `EDA report: <absolute path>`. If you cannot find a file,
check the log output for the resolved absolute path rather than guessing from
`workspaces/eda`.
