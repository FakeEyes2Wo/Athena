# Source repair commit, review and push

User order: commit the scoped source repair, review added/changed code, then push.
Follow docs/代码规范.md. Preserve unrelated worktree changes and partial-stage
phase_runner.py so older research changes are not included. No superpowers skills.

- [x] Read CURRENT, active task scopes, code conventions and review rubric.
- [x] Stage only this repair/documentation slice, inspect the staged diff and commit.
  Commit e3bf0969; commit hooks pass, phase_runner.py staged without old FINAL edits.
- [x] Review the actual commit using independent subagents plus primary inspection.
  RPC/lifecycle, GUI/Rust provider and fork/history reviewed independently.
- [x] Reproduce actionable findings, implement minimal corrections and verify the
  committed source separately from unrelated worktree changes.
  Atomic fork snapshots and checkpoint I/O handling approved after correction;
  isolated regression: 339 passed, 27 subtests passed; strict style check passed.
- [ ] Record review verdict/evidence, remove this plan and update CURRENT.
- [ ] Commit review corrections/report and push main; verify remote revision.

Older GUI/error-ledger/simplification plans stay open. Do not force-push or include
pre-existing source changes, deletions, submission materials or release artifacts.
