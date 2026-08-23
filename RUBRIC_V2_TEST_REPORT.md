# Rubric V2 Test Report

Date: 2026-08-23

## Result summary

| Check | Result | Meaning |
|---|---:|---|
| Latest-main targeted and merge-regression suite | 169 passed | Rubric behavior plus current GUI clarification, EDA handoff, Ideator, Survey, and resume boundaries pass on `0fd803d` |
| Full unit suite with known provider exclusions | 1666 passed, 2 skipped, 3 deselected, 50 subtests | Broad Python regression is green on current main plus this patch |
| Research integration suite | 66 passed, 1 deselected, 3 warnings; transient rolling-search retry passed | Selected lifecycle assertions pass; one additional authority assertion is a current-main baseline failure documented below |
| Black | 31 files unchanged | Owned Python changes and tests are formatted |
| Repository style script | Exit 0 | 42 pre-existing/non-blocking R3 docstring notices in current GUI/runtime/Supervisor files |
| Compileall | Exit 0 | New/modified core Python modules compile |
| `git diff --check` | Exit 0 | No whitespace errors or conflict markers |
| Prior live DeepSeek medical smoke | COMPLETED | Real Task Understanding, both Rubric layers, Selector, one SEARCH experiment, and VALIDATE passed before the final latest-main rebase; post-rebase automated regressions are green |

## Focused acceptance coverage

- Critical task gaps produce actionable questions and `NEEDS_INPUT`.
- A valid local CSV, target, and task type produce `READY` without guessing a
  primary metric.
- Human/Official/Protocol precedence is deterministic.
- Unsupported or invalid AI metrics cannot fall back to Accuracy.
- A locked primary survives AI enrichment failure; an unlocked policy fails
  closed.
- Layer 2 uses exactly one agent call for a whole hypothesis batch.
- Missing, extra, or duplicate hypothesis IDs and fabricated evidence refs are
  rejected.
- Higher resource penalties reduce Execution Efficiency deterministically.
- Full reviews are artifacts; hypotheses retain only the score and ref.
- Selector uses a Rubric score when present and makes no model call.
- Evaluator freeze rejects a primary metric or direction that conflicts with
  the frozen policy.
- UTF-8 BOM scripts retain the BOM at byte zero, receive one frozen marker, and
  compile on Windows.

## Documented exclusions and warnings

- The full unit command deselected
  `create_provider_routes_by_llm_provider_env` and
  `settings_provider_kind_default_and_validation`. These are team provider
  environment expectations and were already part of the broader-test guidance.
- The current-main integration command ignored
  `test/integration/research/test_search_recovery.py` and deselected
  `test_turn_is_persisted_before_dispatch`. The latter was reproduced on the
  baseline flow as a Windows async timing race (`turns_used` advances before the
  assertion reads state), independent of Rubric V2.
- `test_all_phases_use_one_authority` fails because current main writes the
  initial `RUNNING` state from `ResearchRuntime`; the identical failure was
  reproduced on the untouched `0fd803d` checkout. Rubric V2 preserves that team
  behavior rather than refactoring unrelated ownership code.
- `test_generated_hypothesis_fills_the_requested_slot` timed out once in the
  full Windows async suite and passed immediately in isolation on both untouched
  main and the merged Rubric worktree.
- Three integration warnings are existing asyncio/coroutine cleanup warnings.
- The repository style checker reports 42 non-enforced R3 docstring notices on
  existing GUI/runtime/Supervisor public methods and returns exit code 0.

## Live smoke acceptance

The headless runner used the public 569-row Wisconsin breast-cancer CSV, an
explicit Human `roc_auc/maximize` policy, CPU constraints, and
`search-limit=1`. The successful isolated run used the short project path
`C:\Users\15055\Documents\Codex\rv2_smoke_0822` and ended with
`phase=COMPLETED`, `status=COMPLETED`.

Verified persisted evidence:

1. Task Understanding reached `READY`, with `primary_metric=roc_auc` and
   `metric_source=human`.
2. The Evaluation Policy artifact froze `roc_auc/maximize`, `source=human`,
   `locked=true`, `confidence=0.9`.
3. Evaluator `metric.json` matched the policy and the trusted PREPARE baseline
   scored `0.996031746031746`.
4. Exactly one Evaluation Rubric agent run and one Hypothesis Rubric agent run
   appear in agent logs.
5. Six post-Gate hypotheses store `rubric_score` and `rubric_ref`. The selected
   hypothesis `hyp_4ded2a392cc4` stored score `0.82`; its review contains
   Evidence/Testability `0.9`, Scientific Value `0.7`, Validity/Risk Control
   `0.8`, and the full latency/compute/memory/API/implementation breakdown.
6. Selector dispatched that hypothesis without a Selector-time model call. Its
   L1-sparse Logistic Regression experiment scored `0.9993386243386244` and was
   marked `SUPPORTED`.
7. Independent VALIDATE reproduced `0.9993`, reported generalization gap
   `0.0000`, and completed the workflow.

Two abandoned setup attempts are retained outside the source package as
diagnostic evidence: one showed the corrected fail-closed HTTP 401 path, and
one exposed a Windows deep-path binary-extension limitation. Neither is part of
the successful result or the deliverable ZIP.

## Follow-up findings outside Rubric scope

- The evaluator's validation labels should include stable row IDs or an
  explicit split-index artifact; label order alone forced PREPARE to reconstruct
  the split seed.
- Windows agents should receive stronger platform-specific shell guidance to
  avoid recoverable `&&`, `ls`, and `head` failures.
- Real research projects should use a short project root on Windows when
  compiled Python packages are required.
