# GUI-first TESS repository closeout

User correction: run a short real GUI task on ../TESS_DATASET, tidy the original
repository for a GitHub link, prioritize a readable README. No source ZIP handoff.

- [x] Inspect data layout and GUI controls; use a new isolated demo workspace,
  keep dataset read-only, cap SEARCH at one attempt/concurrency one, skip FINAL.
- [ ] Launch the real task through visible GUI; verify progress and report
  actual terminal/result evidence or concrete blocker, never substitute old runs.
- [x] Fix discovered settings-save credential corruption: never submit a displayed
  masked API key as a new credential; reject masks on backend; verify unchanged
  settings preserve real keys, then recover local config through settings RPC.
  Existing provider credential passed health check; recovered locally without
  displaying it. Backend 21 tests, frontend 4 tests and build passed.
- [x] Rewrite README as a short GUI-first quickstart with actual config steps,
  workspace vs dataset distinction, limits and links to detailed docs.
- [x] Remove active ZIP-first handoff instructions; retain useful inference
  tools as optional material and preserve existing archives unless deletion asked.
  Removed the newly introduced packaging script; historical archives left alone.
- [ ] Focused verification, completion report, scoped main commit/push; no new
  branch needed, preserve unrelated changes. Delete plan only after acceptance.
