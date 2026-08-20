# Title

Add Rubric V2 evaluation policy and hypothesis ranking

## Summary

This PR adds two scoped mechanisms to Athena:

1. A Research Evaluation Rubric generated after task understanding and frozen before PREPARE creates the Evaluator.
2. A batch Hypothesis Ranking Rubric generated after the existing Idea Generation Gate and before synchronous deterministic selection.

## Why

The previous task-understanding path could silently turn an unknown metric into `accuracy/maximize`, while hypothesis selection normally relied on a small source/specificity heuristic. Rubric V2 makes the scientific recommendation explicit and auditable while leaving final precedence, validation, aggregation, persistence and selection deterministic.

## Layer 1 changes

- Added strict context/draft/policy schemas.
- Enforced Human > Official > Protocol > AI precedence in code.
- Added metric capability and direction validation; unsupported/empty metrics fail safely.
- Removed unknown→accuracy fallback.
- Persisted `evaluation_policy_ref` and restored/applied direction on resume.
- Passed the frozen Policy into the Evaluator and required matching `metric.json` Primary/direction.
- Kept `selection_mode = primary`; secondary metrics, guardrails and weights do not change SOTA.

## Layer 2 changes

- Added a tool-free structured batch Rubric Agent after Gate PASS.
- Included Layer 1 Policy, EDA/evaluator handoff, baseline/SOTA/history, evidence, cost/environment and applicability-aware NLP/LLM risks.
- Enforced exact unique ID coverage and evidence-ref validity.
- Added transparent deterministic aggregation and persisted full review artifacts.
- Stored only `rubric_score` / `rubric_ref` on Hypothesis.
- Selector prefers `rubric_score`; unchanged `rubric_prior` remains the failure/no-model fallback.
- Selector makes no LLM call; unselected hypotheses remain `PROPOSED`.

## Tests

- [x] Targeted Rubric/unit/integration: `48 passed`
- [x] Research integration excluding the collection issue: `63 passed, 1 baseline-confirmed rolling-search failure`
- [x] Broad regression excluding four baseline-confirmed old failures: `1397 passed, 2 skipped, 3 deselected, 50 subtests passed`
- [x] Black check
- [x] Repository style checker (no blocking findings)
- [x] Compileall
- [ ] Unfiltered repository suite — blocked by pre-existing `test.unit` import collection errors and three unrelated existing expectation mismatches; see `RUBRIC_V2_TEST_REPORT.md`

## Real-data and Layer 2 validation

**PARTIAL — deterministic Layer 2 proof passed; full live E2E remains blocked before SEARCH**

- [x] Downloaded and prepared the official SMS Spam Collection (`5572` rows).
- [x] Prepared a small medical breast-cancer dataset (`569` rows, `30` numeric features).
- [x] Verified the configured DeepSeek provider with a real API probe.
- [x] Ran a deterministic production-code Layer 2 smoke: exact candidate IDs, artifact round-trip,
  transparent aggregation, leakage penalty, Selector ordering and no-fabricated-score fallback all passed.
- [x] Ran Layer 2 Rubric/wiring tests: `20 passed`.
- [ ] Complete live PREPARE→SEARCH→VALIDATE and observe the Layer 2 LLM review. Both real-data
  attempts were blocked before SEARCH by the baseline Windows BOM/script-prepending defect; the
  affected `script_runner.py` Git blob is unchanged on this branch and the failure was reproduced
  independently of Rubric V2.

This establishes deterministic Layer 2 functionality and integration wiring. It does not claim
that the complete real-data workflow, ranking quality, or model-generated Layer 2 review passed.

## Ablation

**NOT RUN — intentionally outside the current functional acceptance scope**

- [ ] Paired same-candidate V1 `rubric_prior` vs V2 LLM Rubric ranking
- [ ] Optional end-to-end Top-k comparison
- [ ] Evaluation Policy control comparison
- [ ] Record quality, risk detection, cost, latency and all seeds

Protocol: `RUBRIC_V2_ABLATION_GUIDE.md`.

## Known limitations

- Full Rubric details are stored in artifacts but not yet rendered in GUI/TUI.
- Real NLP and ablation results are pending.
- Metric capability registry must be extended alongside actual Evaluator support for new metrics.
- GUI intent preview has a legacy heuristic display path, but it does not affect Runtime Policy or SOTA.
- Secondary/guardrails/weights are report-only by design under the current single-Primary policy.

## Future integration

- Read-only GUI/TUI views for Policy and review artifacts.
- More Evaluator capabilities plus contract tests.
- Prompt calibration based on real NLP evidence.
- Separate projects for Dataset Role Review, full EDA Review and multi-objective/Pareto research.

## Changed files

See `RUBRIC_V2_CHANGED_FILES.md` for the per-file rationale.

Summary:

```text
Production files changed: 18
Test files changed: 6
Docs/delivery files changed: 10
```

## Reviewer checklist

- [ ] Unknown Primary never silently becomes Accuracy.
- [ ] Explicit/official/protocol Primary precedence is deterministic.
- [ ] Policy exists and direction is applied before Evaluator freeze.
- [ ] Only Primary changes SOTA.
- [ ] Ranking is after Gate and before registration/selection.
- [ ] Exact IDs/evidence refs and score bounds are enforced.
- [ ] Selector has no LLM call and old fallback is intact.
- [ ] Failures leave no fabricated AI score/explanation.
- [ ] Scope contains no Dataset Role/EDA/Pareto/GUI redesign or dataset-specific production hack.
