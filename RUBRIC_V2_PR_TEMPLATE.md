# PR: Add maintainable Rubric V2 and task-completeness guard

## Summary

- Add a pre-PREPARE task/data completeness wrapper with Human clarification and
  resume support.
- Freeze one authoritative primary Evaluation Policy before evaluator creation
  using Human > Official > Protocol > AI precedence.
- Add one post-Gate, four-dimension batch priority review; Selector consumes the
  stored score and never calls an LLM.
- Preserve deterministic fallbacks and fix Windows UTF-8 BOM script freezing.
- Reimplement the prior Rubric V2 behavior in smaller shared modules without
  refactoring unrelated team code.

## Verification

- [x] Latest-main targeted/merge suite: 169 passed
- [x] Full unit suite under documented provider exclusions: 1666 passed,
      2 skipped, 3 deselected, 50 subtests
- [x] Research integration: 66 passed, one transient rolling-search test passed
      on isolated retry, and one state-authority failure reproduced on clean main
- [x] Black (31 files), compileall, repository style, and diff checks
- [x] Prior live DeepSeek medical smoke: `search-limit=1`, final score `0.9993386243`,
      `phase=COMPLETED`, `status=COMPLETED`

## Review focus

- Confirm task completeness blocks only critical gaps; warnings remain advisory.
- Confirm metric provenance and precedence match team policy.
- Confirm the four Layer 2 weights (30/30/20/20) and resource subdimensions.
- Confirm Layer 2 is one LLM call per batch and Selector has no LLM path.
- Confirm no unrelated team behavior was refactored.

## Known baseline/environment issues

- Windows rolling-search persisted-turn timing assertion is flaky on the team
  baseline and is unrelated to this change.
- Current main's state-authority assertion sees the initial state write from
  `ResearchRuntime`; the same failure reproduces without this patch.
- Two provider-kind unit assertions depend on the team's environment defaults.
- The evaluator label contract lacks stable row IDs; PREPARE reconstructed the
  frozen `random_state=42` split during the live smoke.
- Deep Windows project paths can break compiled extension imports; the accepted
  smoke used a short isolated project root.
