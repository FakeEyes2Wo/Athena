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

| `.athena/exp_docs/runs/*.json` | Athena platform | one durable record for each baseline, SEARCH, or FINAL run |
| `.athena/exp_docs/FINAL_REPORT.md` | Athena platform | current scores and deterministic result causes |
| `.athena/exp_docs/OPTIMIZATION.md` | Athena platform | direction-aware optimization guidance |

## Finding files

All EDA files are written under the EDA worktree. The absolute path is printed
in the PREPARE logs as `EDA report: <absolute path>`. If you cannot find a file,
check the log output for the resolved absolute path rather than guessing from
`workspaces/eda`.

## Baseline evidence gate

The baseline ideator must write both research artifacts before implementation:

- `BASELINE_RESEARCH.json` records the dataset modality and regime, effective
  training units, at least two candidate methods when possible, search queries,
  decisions, and limitations. The recommended training strategy is assessed
  against the data regime; small image data normally uses a pretrained model
  with frozen or partially fine-tuned layers.
- `BASELINE_DESIGN.md` explains the selected architecture and repeats the
  selected candidate ID and training strategy in exact markers.

Athena independently verifies the selected source. A repository route uses a
public HTTPS shallow clone with no checkout. This establishes that the source
can be reached at a revision; it does not establish that the repository is
safe, authoritative, or suitable to execute. Repository code is never checked
out, imported, installed, copied, or run by the verifier. If no repository is
available, the fallback is a matching OpenAlex work with at least 100 citations;
the citation count claimed by the agent is not trusted. A completed Kaggle
winner write-up is useful supporting evidence, but must be paired with a
qualifying paper or public Git source when it is selected.

The platform writes `BASELINE_RESEARCH_VERIFICATION.json` only after this
independent check. It binds the result to the exact SHA-256 digest of
`BASELINE_RESEARCH.json` and the selected candidate, so a matching run can
resume without another ideator turn. A missing or stale digest requires
revalidation.

There is one repair turn for missing, malformed, or rejected research. If the
repaired response still fails validation, PREPARE stops with a terminal error;
it does not silently fall back to task-only baseline design. Only after the
gate succeeds is the prepare agent registered and trusted scoring started.

## Evaluator contract v2 and stage records

SEARCH and FINAL evaluators use the same contract v2: `task_type`,
`primary_metric`, and the complete ordered `class_labels` are declared in each
`metric.json` and must match between the two evaluators. Classification metrics
score the full declared class list; a class absent from one partition contributes
zero rather than being removed from the metric. The evaluators also share the
same label-free identity algorithm and reject missing, duplicate, or unexpected
prediction IDs.

TUI validation is optional and defaults to off. Use `--validate` to continue
automatically into VALIDATE; without it, the run can remain after SEARCH for
manual review or an explicit resume/validate command.

Every baseline, SEARCH, and FINAL settlement writes a JSON stage record under
`.athena/exp_docs/runs/`. The platform refreshes `.athena/exp_docs/FINAL_REPORT.md`
and `.athena/exp_docs/OPTIMIZATION.md` after each stage, so a resumed run has a
small auditable record of its latest scores, artifacts, and failure causes.
