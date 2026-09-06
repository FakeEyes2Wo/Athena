# Research Layout Simplification Completion Report

Completed: 2026-09-02

## Outcome

`src/athena/research` now uses functional package ownership instead of a wide root
module surface. The root was reduced from 26 to 10 Python files. Runtime remains the
stable composition root, and the public imports below remain valid:

- `from athena.research.runtime import ResearchRuntime, DEFAULT_SURVEY_PAPERS`
- `from athena.research.literature.survey import SurveyStack`

The implementation used five spawned subagents in total, below the stated limit of
ten. Editing ownership was separated by package; integration, tests, documentation,
and closeout remained root-owned. No `superpowers` skill was used.

## Before and after

| Measure | Before | After |
|---|---:|---:|
| Python files | 129 | 142 |
| Root-level Python files | 26 | 10 |
| Physical lines, repository PowerShell counting method | 30,146 | 30,394 |

The file count increased because independent parser, survey, runtime projection, Plan
runtime, and validation responsibilities were extracted. The root navigation surface
lost 16 files; the 248-line increase is the cost of keeping those cohesive,
independently testable owners instead of forwarding code back into the old monoliths.

Major move map:

- `agent_turn_*.py` -> `turns/{common,general,support,ideator,runner}.py`
- `prepare_*.py` plus `eda_todo.py` -> `prepare/{orchestrator,data,baseline,eda,evaluator}.py`
- `evaluation.py`, `evaluator_spec.py`, `evaluator_trust.py`, `validation.py` ->
  `evaluation/{evaluator,spec,trust,validation}.py`
- `runtime.py`, `runtime_*.py`, `services.py`, `phase_runner.py` -> `runtime/`
- `clarification/{store,journal}.py` plus root `task_context.py` ->
  `clarification/{persistence,context}.py`
- `supervisor/{policy,ranker,scheduler}.py` -> `supervisor/scheduling.py`; Plan
  execution/recovery and settlement moved to `plan_runtime.py` and `settlement.py`;
  validation data contracts moved to `validation_contracts.py`.
- Root `paper_*`, `survey`, and `bench` packages -> `literature/`; shared conversion
  types moved to `literature/contracts.py`.
- TeX/PDF parsing responsibilities moved behind `tex_render.py`, `tex_tables.py`,
  `tex_bibliography.py`, `pdf_layout.py`, and `pdf_elements.py`.
- Source payload classification, RAG traversal, survey stages, and provider adapters
  moved to `payloads.py`, `traversal.py`, `stages.py`, and `providers.py`.

The complete responsibility table for every added structural file is maintained in
[`night_docs/research-layout-inventory.md`](../night_docs/research-layout-inventory.md).

For the authoritative final tree, package counts, and independent responsibility table,
see [`night_docs/research-layout-inventory.md`](../night_docs/research-layout-inventory.md).

## Verification

Fresh final evidence:

- `python -m compileall -q src/athena/research`: passed.
- Stable runtime, survey, and literature imports: passed. Output: `ResearchRuntime 20`,
  `SurveyStack`, and `literature imports ok`.
- Required deleted-module and dynamic-import audits across `src`, `test`, and `tests`:
  only legitimate `athena.research.evaluation` package matches remained; all other
  legacy `agent_turn_`, root PREPARE/evaluator/validation/phase-runner/task-context,
  old Supervisor scheduling, and old clarification persistence paths returned zero.
- Static intra-package AST import graph: 142 modules, zero cycles.
- Empty-source audit: no zero-byte Python file.
- `black --check src/athena/research`: 142 files unchanged.
- `git diff --check`: passed; Git emitted only three CRLF-to-LF notices.
- Focused literature boundary suite: 477 passed.
- Focused runtime/core suite: 134 passed.
- Focused edge/clarification and migration suite: 81 passed.
- Focused Supervisor suite: 341 passed.
- Focused Task 4/5A suite: 86 passed.
- Focused lifecycle-cleanup suite: 82 passed.
- Full research suite: 735 passed, 6 failed in 73.64 seconds.
- Whole repository Python suite (`test tests`): 2,181 passed, 50 subtests passed,
  8 failed. Six failures are the retained research baseline nodes listed below; the
  other two are environment-only: `test_windows_shell_detection_finds_pwsh_on_path`
  and `test_chain_operator_works_under_powershell7`.
- Whole-research code-style check: passed.
- Black check over all 142 research Python files: passed.
- `git diff --check`: passed.
- `python -m compileall -q src/athena/research`: passed.

Compared with Task0, the final research run contains the same six retained baseline
nodes and zero new research failures. Four Task0 baseline groups disappeared during
directly owned boundary work:

- `test_prepare_agent_reap_is_bounded`
- the three `test_runtime_survey.py` failures recorded in the goal baseline

The six retained baseline failures are:

- `test/unit/research/test_search_budget_override.py::test_an_explicit_budget_replaces_the_persisted_one`
- `test/integration/research/test_fork_runtime.py::test_both_arms_inherit_one_evaluator_and_start_at_search`
- `test/integration/research/test_human_plan_boundary.py::test_persistent_guidance_is_frozen_into_every_later_plan`
- `test/integration/research/test_human_plan_boundary.py::test_next_guidance_is_frozen_once`
- `test/integration/research/test_human_plan_boundary.py::test_unlimited_waiting_plan_keeps_execution_safety_limits`
- `test/integration/research/test_human_plan_boundary.py::test_budget_extension_resumes_waiting_plan_before_new_plan`

The two non-research failures are in `test/unit/execution/test_encoding.py`:
`Get-Command pwsh` cannot find `pwsh`, so shell discovery selects Windows PowerShell
5.1 and its unsupported `&&` syntax fails. These are outside the research layout
scope.

## Remaining debt

- The six documented baseline failures remain behavior work, not structural-layout
  regressions.
- `tex_parser.py` and `pdf_parser.py` remain facades with shared parse context. Further
  splitting would increase parameter plumbing, so the goal stops at cohesive helpers.
- `docs/supervisor_imp_docs.md` and `docs/dsh_docs/athena-preset-tools-plan.md` are
  explicitly historical plans and intentionally retain historical paths. The current
  architecture and added-file responsibilities are documented in
  [`night_docs/research-layout-inventory.md`](../night_docs/research-layout-inventory.md).
- The repository as a whole is not claimed green: six unchanged research baseline
  failures and two machine-environment failures remain.
