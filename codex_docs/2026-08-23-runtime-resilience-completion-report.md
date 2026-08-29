# Runtime Resilience Completion Report (2026-08-23)

## Outcome

The stopped SUPPORT2 run exposed four runtime failures that are now addressed
without changing the Rubric V2 scientific contract:

- SEARCH/VALIDATE resume restores the exact content-addressed Evaluation Policy
  and refuses to regenerate a missing or invalid frozen policy.
- PlanAgent cannot stage or commit its own work; Athena retains diff review,
  trusted scoring, and commit ownership, and settlement records concrete turn
  failures when available.
- Layer 2 receives the real runtime/dependency context, while automatically
  generated hypotheses get a bounded four-turn default. The original four
  dimensions, fixed weights, one-call batch review, and deterministic Selector
  remain unchanged.
- `run_headless.py` configures Unicode-safe UTF-8 output and persists `STOPPED`
  when cancellation is received. The existing Python BOM-at-byte-zero freezing
  behavior is untouched.

## Preserved invariants

- Human > Official > Protocol > AI precedence is unchanged.
- One frozen primary Evaluation Policy remains authoritative across phases.
- Unknown metrics never silently become Accuracy.
- Layer 2 remains one LLM call for one complete post-Gate batch.
- Layer 2 aggregation remains 30% Evidence & Testability, 30% Scientific Value,
  20% Execution Efficiency, and 20% Validity & Risk Control.
- Selector makes no LLM call and uses stored scores; genuine Layer 2 provider
  failures retain the explicit deterministic fallback.
- Missing policy authority fails closed instead of fabricating scores or refs.

## Verification

- Direct modified-area unit suite: `73 passed`.
- Targeted acceptance suite: `122 passed, 1 deselected` (the deselected case is
  the pre-existing WAITING-loop assertion documented below).
- Concrete settlement integration regression: `1 passed`.
- Research integration run: `67 passed, 1 deselected`; the one remaining failure
  was the pre-existing single-writer assertion documented below and failed the
  same way on an isolated retry. The separate recovery file produced `12 passed`
  plus the pre-existing WAITING-loop assertion documented below.
- Broader unit suite: `1674 passed, 2 skipped, 3 deselected, 50 subtests`; its
  only failure was local process configuration where an existing higher-priority
  `LLM_API_KEY` overrode the test's temporary DeepSeek key. Re-running the
  settings module with a clean process-level override produced `5 passed`.
- Black: `12 files would be left unchanged`.
- `compileall`: passed.
- Repository style checker: exit 0; existing non-blocking R3 docstring notices
  remain.
- `git diff --check`: passed.

No paid DeepSeek workflow was re-run as part of this code-only repair.

## Pre-existing issues kept out of scope

1. `test_provider_failure_at_turn_limit_waits_without_consuming_patience`
   expects the SEARCH task to finish, while the current implementation
   intentionally remains in `WAITING` for `/resume`; this is the same old
   asynchronous test seen before this repair.
2. `test_all_phases_use_one_authority` expects only `Supervisor` to call
   `ResearchState.save`, while the existing `ResearchRuntime.start()` directly
   persists the initial `RUNNING` transition. That line is unchanged by this
   work.

Neither mismatch was hidden by altering unrelated team code.
