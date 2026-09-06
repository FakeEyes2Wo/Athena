# GUI-first TESS repository closeout

User correction: run a short real GUI task on ../TESS_DATASET, tidy the original
repository for a GitHub link, prioritize a readable README. No source ZIP handoff.

- [x] Inspect data layout and GUI controls; use a new isolated demo workspace,
  keep dataset read-only, cap SEARCH at one attempt/concurrency one, skip FINAL.
- [ ] Launch the real task through visible GUI; verify progress and report
  actual terminal/result evidence or concrete blocker, never substitute old runs.
- [x] Diagnose EDA file-write failure for workspaces nested below an ancestor
  `.athena`; scope file and shell-workdir checks to the registered workspace,
  retaining private-directory and path-escape rejection. Nine focused tests pass.
  GUI resume initially reused stale permission conclusions without retrying tools.
  Added an explicit current write_file handoff instruction; partial-EDA tests:
  3 passed. Reloaded gateway and sent /resume through GUI. Fresh observation:
  RUNNING/PREPARE, EDA_TODO.md physically written (1189 bytes), EDA workers started.
  This verifies resumed EDA progress only, not complete reports or baseline success.
- [ ] Deploy an external authority capability before another full GUI run.
  Code now accepts a host-owned controller factory and preflights it before EDA;
  bounded environment repair is available only for transport failures. Actual
  service module/credentials remain a deployment owner responsibility; existing
  hardening design forbids a local fallback. See
  `codex_docs/2026-09-06-controller-repair-completion.md`.
- [x] Reconcile PREPARE's unconditional FINAL evaluator creation with this run's
  no-FINAL constraint before resuming. Previous logs include a final_evaluator
  run; skip_validate alone does not enforce no FINAL preparation. Do not claim
  that the prior GUI run proves FINAL was never accessed.
  Lightweight patch and synthetic lifecycle verification recorded in
  `codex_docs/2026-09-06-lightweight-final-completion.md`; live GUI rerun remains open.
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
