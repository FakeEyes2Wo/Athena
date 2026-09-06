# Documentation cleanup completion

Date: 2026-09-06

## Scope

- Removed the internal `docs/superpowers/` plans and design specs from the
  distributable documentation tree.
- Preserved user-facing guides, architecture notes, operations/testing docs,
  and the historical `docs/dsh_docs/` material.
- Updated `codex_docs/CURRENT.md` so it no longer points at removed plans or
  specifications.

## Verification

- Confirmed no active documentation index points at the removed directory;
  historical completion reports retain their original provenance references.
- Ran `git diff --check` successfully.
- No source code or unrelated worktree files were changed by this cleanup.
