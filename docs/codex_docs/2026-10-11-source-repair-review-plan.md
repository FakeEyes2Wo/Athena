# Source repair commit, review and push

User order: commit the scoped source repair, review added/changed code, then push.
Follow docs/代码规范.md. Preserve unrelated worktree changes and partial-stage
phase_runner.py so older research changes are not included. No superpowers skills.

- [x] Read CURRENT, active task scopes, code conventions and review rubric.
- [ ] Stage only this repair/documentation slice, inspect the staged diff and commit.
- [ ] Review the actual commit using independent subagents plus primary inspection.
- [ ] Reproduce actionable findings, implement minimal corrections and verify the
  committed source separately from unrelated worktree changes.
- [ ] Record review verdict/evidence, remove this plan and update CURRENT.
- [ ] Commit review corrections/report and push main; verify remote revision.

Older GUI/error-ledger/simplification plans stay open. Do not force-push or include
pre-existing source changes, deletions, submission materials or release artifacts.
