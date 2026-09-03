# Frontend Responsiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep workspace navigation responsive during slow runtime swaps and make long streamed conversations remain responsive without regressing the six completed frontend fixes.

**Architecture:** `useWorkspace` serializes backend swaps while coalescing pending user intent to the latest target. The sidebar reads cached session summaries instead of issuing background RPCs, and `usePipeline` batches output deltas into one indexed update per animation frame while memoized rows avoid repeated history rendering.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, Tauri 2, browser/Tauri WebSocket bridge.

## Global Constraints

- Keep the implementation frontend-only; do not change Python or Rust backend protocols.
- Preserve the six workspace/session fixes already merged in `b802820`.
- Keep `switchTo(path: string, sessionId?: string): Promise<void>` as the public workspace action.
- Never issue more than one `setProjectRoot` request at a time; retain only the latest queued intent.
- Preserve output message order, IDs, content, log metadata, and clarification grouping.
- Add no dependencies and do not touch unrelated dirty main files.

---

### Task 1: Establish Baseline and Commit Plan

**Files:**
- Modify: `codex_docs/CURRENT.md`
- Test: all existing `athena-gui/src/**/__tests__/*`

**Interfaces:**
- Consumes: merged frontend behavior at `8b5f996`.
- Produces: fresh baseline evidence and an active plan pointer for this worktree.

- [x] **Step 1: Install locked frontend dependencies if absent**

  Run `npm ci` from `athena-gui` only when `node_modules` is absent.

- [x] **Step 2: Run the complete frontend baseline**

  Run `npm test -- --run` from `athena-gui`.
  Expected: 17 test files and 115 tests pass.

- [x] **Step 3: Point CURRENT at this plan**

  Set the active plan/spec in `codex_docs/CURRENT.md` to this plan and
  `docs/superpowers/specs/2026-09-03-frontend-responsiveness-design.md`, retaining
  the authoritative-baseline work as a parallel plan.

- [x] **Step 4: Commit plan metadata**

  Stage only this plan and `codex_docs/CURRENT.md`; commit as
  `docs: plan frontend responsiveness fix`.

### Task 2: Coalesce Workspace Switch Intent

**Files:**
- Modify: `athena-gui/src/hooks/useWorkspace.ts`
- Modify: `athena-gui/src/hooks/__tests__/useWorkspace.test.tsx`
- Modify: `athena-gui/src/components/shell/ContextSidebar.tsx`
- Modify: `athena-gui/src/components/shell/WorkspacePicker.tsx`
- Modify: `athena-gui/src/components/__tests__/app-shell.test.tsx`
- Modify: `athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx`

**Interfaces:**
- Consumes: `setProjectRoot(path)` and `switchTo(path, sessionId?)`.
- Produces: one active switch Promise plus one replaceable queued `WorkspaceIntent`.

- [ ] **Step 1: Add failing coordinator tests**

  Add a `WorkspaceIntent` scenario to `useWorkspace.test.tsx` using deferred
  `setProjectRoot` Promises. Start `/a -> /b`, then request `/c` and `/d` before
  `/b` resolves. Assert only `/b` starts initially; after `/b` resolves only `/d`
  starts; `/c` is never sent; only `/d` updates `currentRoot` and
  `requestedSessionId`.

  Add a rejection variant proving a superseded `/b` error is not displayed and
  the queued `/d` still starts. Add a final rejection/retry variant proving
  `switching` clears and another call can run.

- [ ] **Step 2: Add failing interaction tests**

  Change the `app-shell.test.tsx` switching-state expectation so the footer and
  other workspace/session buttons remain enabled and call their handlers.
  Change `WorkspacePicker.test.tsx` so recent roots, browse, manual path input,
  and Open remain usable while `switching`; only Continue remains disabled.

- [ ] **Step 3: Run the focused tests and verify RED**

  Run:

  ```powershell
  npm test -- --run src/hooks/__tests__/useWorkspace.test.tsx src/components/__tests__/app-shell.test.tsx src/components/shell/__tests__/WorkspacePicker.test.tsx
  ```

  Expected: failures show concurrent `setProjectRoot` calls and disabled controls.

- [ ] **Step 4: Implement the latest-intent coordinator**

  In `useWorkspace.ts`, add:

  ```ts
  interface WorkspaceIntent { path: string; sessionId: string | null }
  const activeSwitchRef = useRef<Promise<void> | null>(null);
  const queuedIntentRef = useRef<WorkspaceIntent | null>(null);
  ```

  `switchTo` stores the normalized intent. If a processor already exists, return
  that Promise. Otherwise, run a loop that clears and processes the current queued
  intent, awaits `setProjectRoot`, commits state only when no newer intent exists,
  ignores superseded failures, and clears `switching`/the active ref in `finally`.
  Guard state writes after unmount.

- [ ] **Step 5: Keep navigation inputs interactive**

  Remove `switching` from disabled conditions on the sidebar footer,
  other-workspace headers/session rows, picker recent roots, Browse, manual input,
  and Open. Keep Browse protected only by `browsing`; keep Continue disabled while
  switching.

- [ ] **Step 6: Run focused tests and verify GREEN**

  Run the command from Step 3 and require PASS.

- [ ] **Step 7: Commit the switch fix**

  Stage only the six task files and commit as
  `fix(gui): keep workspace switching responsive`.

### Task 3: Remove Background Workspace RPCs

**Files:**
- Modify: `athena-gui/src/lib/workspaceStorage.ts`
- Modify: `athena-gui/src/lib/__tests__/workspaceStorage.test.ts`
- Modify: `athena-gui/src/hooks/usePipeline.ts`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`
- Modify: `athena-gui/src/components/shell/ContextSidebar.tsx`
- Modify: `athena-gui/src/components/__tests__/app-shell.test.tsx`

**Interfaces:**
- Produces: `loadWorkspaceSessions(root): SessionSummary[]` and
  `persistWorkspaceSessions(root, sessions): void`, where `SessionSummary` is
  `{ id: string; title: string }`.
- Consumes: authoritative session arrays built by `usePipeline.applySessions`.

- [ ] **Step 1: Add failing cache tests**

  In `workspaceStorage.test.ts`, assert cache keys are workspace-namespaced,
  malformed data returns `[]`, duplicate IDs are normalized, and persisting `[]`
  clears stale entries.

  In `app-shell.test.tsx`, prepopulate summaries for another workspace, render the
  sidebar, assert cached sessions appear synchronously, and assert
  `sessionsListFor` is never called. Add an empty-cache case showing "暂无会话".

  In `usePipeline.test.tsx`, hydrate with an empty authoritative list after a stale
  summary exists and assert storage becomes empty.

- [ ] **Step 2: Run focused tests and verify RED**

  Run:

  ```powershell
  npm test -- --run src/lib/__tests__/workspaceStorage.test.ts src/components/__tests__/app-shell.test.tsx src/hooks/__tests__/usePipeline.test.tsx
  ```

  Expected: missing cache API and eager `sessionsListFor` calls fail assertions.

- [ ] **Step 3: Implement session-summary storage**

  Add the typed load/persist helpers to `workspaceStorage.ts`. Normalize non-empty
  string IDs/titles, deduplicate by ID, cap storage to a conservative 200 sessions,
  and catch localStorage errors.

- [ ] **Step 4: Populate cache from authoritative state**

  In `usePipeline.applySessions`, build the resolved authoritative summaries once,
  persist them for `workspaceRoot`, and then merge pending optimistic sessions for
  React state. Persist only authoritative rows so failed optimistic creations are
  not advertised in other workspaces.

- [ ] **Step 5: Make the sidebar synchronous**

  Remove `useEffect`, `useState`, and `sessionsListFor` from `ContextSidebar`.
  Build other workspace groups from `loadWorkspaceSessions(root)` during render.
  Preserve recent-root order and current-workspace authoritative sessions.

- [ ] **Step 6: Run focused tests and verify GREEN**

  Run the command from Step 2 and require PASS.

- [ ] **Step 7: Commit the RPC removal**

  Stage only the six task files and commit as
  `perf(gui): cache workspace session summaries`.

### Task 4: Batch Streamed Output and Avoid History Rerenders

**Files:**
- Modify: `athena-gui/src/hooks/usePipeline.ts`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`
- Modify: `athena-gui/src/components/conversation/MessageList.tsx`
- Modify: `athena-gui/src/components/shell/AppShell.tsx`

**Interfaces:**
- Produces: pure `applyOutputEvents(current, events, replay)` batch behavior and an
  animation-frame queue owned by `usePipeline`.
- Consumes: existing `PipelineEvent`, `UIMessage`, and `LogEntry` shapes.

- [ ] **Step 1: Add failing burst and lifecycle tests**

  In `usePipeline.events.test.tsx`, stub `requestAnimationFrame`, emit 100 deltas
  for one `message_id` plus ordered tool/stdout events, assert the visible state is
  unchanged before the scheduled callback, invoke one callback, and assert one
  coalesced message plus ordered individual log entries. Unmount before a scheduled
  callback and assert it is cancelled.

  In `usePipeline.test.tsx`, replay a large mixed transcript with repeated message
  IDs and assert content/order equals live reducer semantics.

- [ ] **Step 2: Run focused tests and verify RED**

  Run:

  ```powershell
  npm test -- --run src/hooks/__tests__/usePipeline.events.test.tsx src/hooks/__tests__/usePipeline.test.tsx
  ```

  Expected: current output state updates immediately for every event and no frame
  cancellation occurs.

- [ ] **Step 3: Implement indexed output batches**

  Extract output classification into a helper that mutates a single copied
  messages array and uses `Map<string, number>` for ID lookup. Accumulate delta
  strings per message within the batch before assigning the final content. Reuse
  this primitive from history replay so it copies the base array once.

- [ ] **Step 4: Implement animation-frame flushing**

  Queue live `output` events in refs. Schedule at most one animation frame. On
  flush, atomically drain the queue, call one `setViewModel`, and append the batch's
  log entries in one bounded `setLogs`. Flush pending output synchronously before
  handling a non-output event to preserve ordering. Cancel the frame and discard
  the queue on unmount.

- [ ] **Step 5: Reduce component work**

  Wrap `TrajectoryItem` with `memo` and keep unchanged `UIMessage` object identities.
  Narrow `AppShell`'s session-refresh effect dependencies to `module`,
  `pipeline.currentSessionId`, `pipeline.viewModel.status`, and
  `pipeline.switchSession`.

- [ ] **Step 6: Run focused tests and verify GREEN**

  Run the command from Step 2 and require PASS.

- [ ] **Step 7: Commit the streaming fix**

  Stage only the five task files and commit as
  `perf(gui): batch streamed conversation output`.

### Task 5: Full Verification and Integration

**Files:**
- Create: `codex_docs/2026-09-03-frontend-responsiveness-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete: `docs/superpowers/plans/2026-09-03-frontend-responsiveness.md`

**Interfaces:**
- Consumes: Tasks 2-4 and the original frontend completion report.
- Produces: verified merge on `main` with completion evidence.

- [ ] **Step 1: Run all frontend tests and build**

  Run `npm test -- --run` and `npm run build` from `athena-gui`.

- [ ] **Step 2: Run desktop integration checks**

  Run `cargo check` from `athena-gui/src-tauri` and `git diff --check`.

- [ ] **Step 3: Review scope and original requirements**

  Review `git diff main...HEAD` and prove each original workspace/session behavior,
  switch responsiveness, zero sidebar background RPCs, and batched output behavior
  from current tests and implementation.

- [ ] **Step 4: Close documentation**

  Write the completion report with exact verification results, delete this plan,
  and restore CURRENT to the authoritative-baseline parallel plan while listing the
  new report as most recent completed work.

- [ ] **Step 5: Commit closeout and merge**

  Commit only owned documentation, verify no overlap with dirty main paths, merge
  `fix/frontend-responsiveness` into `main` with a merge commit, and rerun the full
  frontend test/build plus `cargo check` on merged main.
