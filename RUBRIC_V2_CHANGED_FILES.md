# Rubric V2 Changed Files

## New implementation files

- `src/athena/agents/rubric_agent.py` — one shared factory for the two
  tool-free structured Rubric agents.
- `src/athena/core/agent/prompts/evaluation_rubric_agent.md` — Layer 1 policy
  rules and source precedence.
- `src/athena/core/agent/prompts/hypothesis_rubric_agent.md` — the four Layer 2
  dimensions and batch-only contract.
- `src/athena/research/rubrics/models.py` — strict policy, context, review, and
  resource contracts.
- `src/athena/research/rubrics/task.py` — deterministic task/data readiness.
- `src/athena/research/rubrics/evaluation.py` — metric capabilities,
  precedence, validation, retry, and fail-closed behavior.
- `src/athena/research/rubrics/ranking.py` — deterministic four-dimension and
  resource aggregation plus exact batch validation.
- `src/athena/research/rubrics/workflow.py` — compact runtime orchestration and
  artifact persistence for both Rubric layers.
- `src/athena/research/rubrics/__init__.py` — small public API.

## Minimal integration changes

- `src/athena/core/research_models.py` — shared `TaskUnderstanding`, metric
  provenance, missing-item contracts, and hypothesis Rubric score/ref fields.
- `src/athena/agents/supervisor_agent.py` and
  `src/athena/core/agent/prompts/supervisor_agent.md` — use the shared readiness
  contract and forbid guessed Accuracy.
- `src/athena/gui/service.py` — remove the duplicate schema and heuristic metric
  guess; retain the team's multi-turn Human clarification flow and reuse
  deterministic readiness.
- `src/athena/research/runtime.py` — task pause/resume, policy freeze/restore,
  and Rubric agent registration.
- `src/athena/research/agent_turn_runner.py` — thin delegates to
  `RubricWorkflow` and frozen-policy context for ideation while preserving the
  latest EDA/literature handoff flow.
- `src/athena/research/phase_runner.py` — pass the frozen policy into PREPARE.
- `src/athena/research/supervisor/prepare.py` and
  `src/athena/core/agent/prompts/evaluator_agent.md` — give the policy to the
  evaluator and reject mismatched `metric.json` output.
- `src/athena/research/supervisor/supervisor.py` — persist readiness/policy and
  invoke one Layer 2 batch before tree insertion.
- `src/athena/research/supervisor/state.py` — resume field for the policy ref.
- `src/athena/research/supervisor/ranker.py` — use stored AI priority when
  present and retain the original deterministic fallback.
- `src/athena/research/script_runner.py` — UTF-8 BOM-safe frozen headers.

## Verification files

- `test/unit/research/rubrics/` — readiness, precedence, failure handling,
  aggregation, exact-ID/evidence validation, and one-call workflow tests.
- `test/unit/research/test_data_scripts.py` — BOM compilation and idempotence.
- `test/unit/research/supervisor/` — policy persistence, evaluator enforcement,
  batch ordering, Selector use, and resume state.
- `test/unit/kaggle/test_supervisor_gate.py` — official metric provenance.
- `test/integration/research/test_task_seeding.py` and
  `test/unit/research/test_breakpoint_resume.py` — READY/NEEDS_INPUT and resume
  contract integration.

## Deliberately not changed

- No unrelated team module was broadly refactored.
- No V1/V2 ablation framework was added.
- No dataset or `.env` file is included.
- No commit, branch publication, or PR creation is included.
