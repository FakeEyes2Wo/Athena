# Frontend Switch Integration Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add direct App-level regression evidence that a second workspace click remains usable while the first backend root rebuild is pending and is coalesced without concurrent RPCs.

**Architecture:** Extend the existing `App` integration harness with real `App`, `useWorkspace`, `ContextSidebar`, and `WorkspacePicker` behavior while mocking only backend RPCs and the unrelated pipeline hook. Deferred `setProjectRoot` Promises make request ordering and final UI state deterministic.

**Tech Stack:** React 18, TypeScript, Vitest, Testing Library.

## Global Constraints

- Change tests and completion documentation only; do not modify production behavior.
- Preserve all frontend fixes already merged through `4a04052`.
- Do not modify Python, Rust, backend protocols, dependencies, or lockfiles.
- Preserve unrelated dirty files in the main worktree.

---

### Task 1: Add the App-Level Coalescing Regression Test

**Files:**
- Modify: `athena-gui/src/__tests__/App.test.tsx`

**Interfaces:**
- Consumes: `App`, `setProjectRoot(path)`, stable recent-root storage, and the sidebar workspace buttons.
- Produces: deterministic end-to-end coverage of one active switch plus one replaceable latest intent.

- [x] **Step 1: Verify the existing App-test baseline**

  Run `npm test -- --run src/__tests__/App.test.tsx`.
  Expected: 1 test passes before the new coverage is added.

- [ ] **Step 2: Add the missing integration scenario**

  Preload recent roots `/a`, `/b`, `/c`; resolve `settingsGet` with `/a`; defer
  `setProjectRoot('/b')`; render the real `App`; click workspace `/b`, then click
  `/c` while `/b` is pending. Assert only `/b` has reached the backend. Resolve
  `/b`; assert `/c` starts next, `/b` never commits to the visible workspace, and
  resolving `/c` leaves `/c` as the only committed root. Keep picker/sidebar
  controls observable during the pending interval.

- [ ] **Step 3: Run the focused test**

  Run `npm test -- --run src/__tests__/App.test.tsx`.
  Expected: both App integration tests pass.

- [ ] **Step 4: Run the full frontend suite and build**

  Run `npm test -- --run` and `npm run build` from `athena-gui`.
  Expected: all tests and the production build pass.

- [ ] **Step 5: Commit the test**

  Stage only `athena-gui/src/__tests__/App.test.tsx` and this plan; commit as
  `test(gui): prove pending workspace switch coalescing`.

### Task 2: Review, Close, and Integrate

**Files:**
- Modify: `codex_docs/CURRENT.md`
- Modify: `codex_docs/2026-09-03-frontend-responsiveness-completion-report.md`
- Delete: `docs/superpowers/plans/2026-09-03-frontend-switch-integration-proof.md`

**Interfaces:**
- Consumes: Task 1 and the existing frontend completion report.
- Produces: independently reviewed evidence merged into `main`.

- [ ] **Step 1: Obtain independent code review**

  Review the exact `4a04052..HEAD` diff for behavioral value, mock boundaries,
  deterministic concurrency assertions, and scope. Require APPROVED.

- [ ] **Step 2: Close documentation**

  Add the App-level coalescing evidence to the existing completion report, restore
  `codex_docs/CURRENT.md` to the authoritative-baseline plan, and delete this plan.

- [ ] **Step 3: Commit closeout**

  Stage only the owned documentation and commit as
  `docs: record workspace switch integration proof`.

- [ ] **Step 4: Merge to main and verify**

  Merge `test/frontend-switch-integration` into `main`, rerun the focused App test,
  full frontend suite, build, `cargo check`, and `git diff --check`, then remove the
  isolated worktree/branch.
