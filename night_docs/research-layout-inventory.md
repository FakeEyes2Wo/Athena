# Research layout inventory

Inventory captured from the current working tree on 2026-09-02. This is a
Task 8 closeout input, not the final completion report.

## Counts

The count includes physical `.py` files only and excludes `__pycache__`.
Physical line count is the sum of `Get-Content` line counts, including blank
lines.

| Scope | Before layout goal | Current tree |
|---|---:|---:|
| Python files under `src/athena/research` | 129 | 142 |
| Python files directly in the research root | 26 | 10 |
| Physical Python lines | 30,146 | 30,394 |

The root navigation surface therefore lost 16 files. The total file count is
higher because the migration keeps independently testable parser, payload,
traversal, survey-stage, runtime-projection, and Supervisor lifecycle owners
instead of re-growing the old monoliths.

Current package counts:

| Package | Python files | Lines |
|---|---:|---:|
| root | 10 | 1,408 |
| `clarification` | 11 | 1,721 |
| `evaluation` | 5 | 579 |
| `idea_generation` | 10 | 1,453 |
| `literature` | 60 | 14,922 |
| `prepare` | 6 | 969 |
| `runtime` | 12 | 2,790 |
| `supervisor` | 22 | 5,549 |
| `turns` | 6 | 1,003 |
| **Total** | **142** | **30,394** |

## Functional tree

```text
src/athena/research/
├── __init__.py
├── config.py
├── contracts.py
├── data_models.py
├── exploration_files.py
├── fork.py
├── output_freshness.py
├── report.py
├── script_runner.py
├── splitter.py
├── clarification/
│   ├── __init__.py
│   ├── confirmation.py
│   ├── context.py
│   ├── controller.py
│   ├── errors.py
│   ├── generator.py
│   ├── handoff.py
│   ├── models.py
│   ├── persistence.py
│   ├── requirements.py
│   └── state.py
├── evaluation/
│   ├── __init__.py
│   ├── evaluator.py
│   ├── spec.py
│   ├── trust.py
│   └── validation.py
├── idea_generation/
│   ├── __init__.py
│   ├── citation_support.py
│   ├── gate.py
│   ├── gatekeeper.py
│   ├── idea_schemas.py
│   ├── pre_gate_checks.py
│   ├── prompts.py
│   ├── review_board.py
│   ├── structured_chat.py
│   └── validation.py
├── literature/
│   ├── __init__.py
│   ├── contracts.py
│   ├── bench/
│   │   ├── __init__.py
│   │   ├── health.py
│   │   ├── known_item.py
│   │   ├── query_sets.py
│   │   ├── recall.py
│   │   ├── reproducibility.py
│   │   └── schemas.py
│   ├── paper_scout/
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── backends.py
│   │   ├── pool.py
│   │   ├── prompts.py
│   │   ├── reranker.py
│   │   ├── schemas.py
│   │   ├── scorer.py
│   │   ├── selection.py
│   │   ├── session.py
│   │   └── tool.py
│   ├── paper_source/
│   │   ├── __init__.py
│   │   ├── arxiv.py
│   │   ├── fetcher.py
│   │   ├── http.py
│   │   ├── openalex.py
│   │   ├── payloads.py
│   │   ├── schemas.py
│   │   └── tool.py
│   ├── paper_markdown/
│   │   ├── __init__.py
│   │   ├── chunking.py
│   │   ├── document.py
│   │   ├── interfaces.py
│   │   ├── pdf_elements.py
│   │   ├── pdf_layout.py
│   │   ├── pdf_parser.py
│   │   ├── processor.py
│   │   ├── quality.py
│   │   ├── schemas.py
│   │   ├── tex_bibliography.py
│   │   ├── tex_parser.py
│   │   ├── tex_render.py
│   │   ├── tex_source.py
│   │   ├── tex_tables.py
│   │   ├── tool.py
│   │   └── visuals.py
│   ├── paper_rag/
│   │   ├── __init__.py
│   │   ├── index.py
│   │   ├── interfaces.py
│   │   ├── schemas.py
│   │   ├── search.py
│   │   ├── tool.py
│   │   └── traversal.py
│   └── survey/
│       ├── __init__.py
│       ├── library.py
│       ├── pipeline.py
│       ├── providers.py
│       ├── report.py
│       ├── stages.py
│       ├── tool.py
│       └── wiring.py
├── prepare/
│   ├── __init__.py
│   ├── baseline.py
│   ├── data.py
│   ├── eda.py
│   ├── evaluator.py
│   └── orchestrator.py
├── runtime/
│   ├── __init__.py
│   ├── bootstrap.py
│   ├── clarification.py
│   ├── control.py
│   ├── corpus.py
│   ├── event_projection.py
│   ├── events.py
│   ├── facade.py
│   ├── phase_runner.py
│   ├── services.py
│   ├── settings.py
│   └── survey.py
├── supervisor/
│   ├── __init__.py
│   ├── deps.py
│   ├── evaluator_plan.py
│   ├── events.py
│   ├── experiment.py
│   ├── manifest.py
│   ├── phases.py
│   ├── plan_lifecycle.py
│   ├── plan_runtime.py
│   ├── plans.py
│   ├── prepare.py
│   ├── prompt_context.py
│   ├── recovery.py
│   ├── run_state.py
│   ├── scheduling.py
│   ├── search_loop.py
│   ├── settlement.py
│   ├── state.py
│   ├── statistics.py
│   ├── supervisor.py
│   ├── validation.py
│   └── validation_contracts.py
└── turns/
    ├── __init__.py
    ├── common.py
    ├── general.py
    ├── ideator.py
    ├── runner.py
    └── support.py
```

## Added structural files and responsibility

These are the files introduced by the layout pass rather than simple moves of
an existing owner. Each has a cohesive boundary that can be tested without
sharing mutable facade locals.

| File | Independent responsibility |
|---|---|
| `research/exploration_files.py` | Durable naming and persistence helpers for exploration files. |
| `research/evaluation/__init__.py` | Minimal public evaluation package exports. |
| `research/evaluation/spec.py` | Dataset-specific evaluator specification parsing and validation. |
| `research/literature/__init__.py` | Literature package boundary without cross-domain exports. |
| `research/literature/contracts.py` | Conversion-boundary types shared by source and Markdown only. |
| `research/literature/{bench,paper_scout,paper_source,paper_markdown,paper_rag,survey}/__init__.py` | Minimal public boundaries for each literature capability. |
| `research/literature/paper_source/payloads.py` | Payload recognition, locator construction, and conversion-request construction. |
| `research/literature/paper_rag/traversal.py` | Citation, section, visual, and chunk graph traversal. |
| `research/literature/survey/pipeline.py` | Top-level survey request/report/state orchestration. |
| `research/literature/survey/providers.py` | Embedding and vision provider implementations. |
| `research/literature/survey/stages.py` | Scout, fetch, convert, and index stage execution. |
| `research/literature/paper_markdown/tex_render.py` | TeX rendering transformations. |
| `research/literature/paper_markdown/tex_tables.py` | TeX table extraction and normalization. |
| `research/literature/paper_markdown/tex_bibliography.py` | TeX bibliography and reference extraction. |
| `research/literature/paper_markdown/pdf_layout.py` | PDF reading-order and layout calculations. |
| `research/literature/paper_markdown/pdf_elements.py` | PDF element, table, and reference extraction. |
| `research/prepare/__init__.py` | Minimal PREPARE package boundary. |
| `research/prepare/eda.py` | EDA workspace setup, todo parsing, worker scheduling, and fallback artifacts. |
| `research/runtime/__init__.py` | Stable `ResearchRuntime` and runtime constants export. |
| `research/runtime/event_projection.py` | Pure Supervisor-event-to-output/state projection. |
| `research/supervisor/plan_runtime.py` | One Plan turn execution and recovery boundary. |
| `research/supervisor/scheduling.py` | Scheduler, policy, ranking, and selection decisions. |
| `research/supervisor/settlement.py` | Plan result settlement and SOTA update transformation. |
| `research/supervisor/validation_contracts.py` | Validation-specific request/result contracts. |
| `research/turns/__init__.py` | Minimal Agent-turn package boundary. |
| `research/turns/runner.py` | Role dispatch and turn lifecycle coordination. |

The moved files retain their existing responsibility; they are intentionally
not duplicated under their former root paths.

## Evidence commands

```powershell
$py = @(Get-ChildItem src/athena/research -Recurse -File -Filter '*.py')
$root = @(Get-ChildItem src/athena/research -File -Filter '*.py')
$lines = ($py | ForEach-Object { (Get-Content $_.FullName).Count } |
  Measure-Object -Sum).Sum
```

Result: `142` Python files, `10` root-level Python files, `30,394` physical
lines. A stale-path audit across the 25 current architecture/developer/user
documents changed in this pass returned `0` matches for deleted research
module paths.

Audit scope (25 files):

```text
docs/README.md
docs/academic_survey_ch.md
docs/architecture/2026-08-19-remote-gpu-execution-design.md
docs/athena-gui-design.md
docs/corpus_ideation_ch.md
docs/eda_workflow.md
docs/evaluator_contract_ch.md
docs/loop_failure_modes_ch.md
docs/paper_markdown_rag_output_ch.md
docs/paper_markdown_tool.md
docs/paper_markdown_tool_ch.md
docs/paper_rag_benchmarks_ch.md
docs/paper_rag_tool_ch.md
docs/research_core_mechanisms_ch.md
docs/survey_limits_ch.md
docs/survey_overhaul_ch.md
docs/task_readiness_ch.md
docs/athena-guide/README.md
docs/athena-guide/03-agent-infra.md
docs/athena-guide/06-workflow.md
docs/athena-guide/07-tree-search-and-tool.md
docs/athena-guide/08-idea-generation.md
docs/athena-guide/10-paper-research.md
docs/athena-guide/11-evidence-and-todo.md
docs/athena-guide/12-eda-system.md
```

The audit command was:

```powershell
rg -n "src/athena/research/(agent_turn_|prepare_|evaluation\\.py|evaluator_spec|evaluator_trust|validation\\.py|phase_runner\\.py|task_context\\.py|runtime\\.py|runtime_|services\\.py|paper_(markdown|rag|scout|source)/|survey/|bench/|project_runtime\\.py|search\\.py)|athena\\.research\\.(agent_turn_|prepare_|evaluation|evaluator_spec|evaluator_trust|validation|phase_runner|task_context)|src/athena/research/supervisor/(policy|ranker|scheduler)\\.py|src/athena/research/clarification/(store|journal)\\.py" <25-files-above>
```

Exit status was `1` (no matches); `git diff --check` over the same files and
this inventory also exited `0`.
