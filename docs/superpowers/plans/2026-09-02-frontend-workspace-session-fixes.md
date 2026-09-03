# Frontend Workspace and Session Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix sidebar anchoring and ordering, native desktop directory selection, empty/new session lifecycle, and task-understanding output visibility in the Athena GUI.

**Architecture:** Keep all behavior inside the React/Tauri GUI boundary. Split the sidebar into fixed and scrolling regions, carry an optional target session through workspace remounts, isolate native dialog access behind one TypeScript function, serialize session mutations in `usePipeline`, and group already-received output events under a live task-understanding activity region.

**Tech Stack:** React 18, TypeScript, Vitest/Testing Library, Tauri 2, Rust.

## Global Constraints

- Work only in `.worktrees/frontend-session-fixes` on `fix/frontend-session-fixes`.
- Do not modify Python research-runtime code or event-production contracts.
- Preserve the main worktree and its uncommitted authoritative-baseline changes.
- Desktop directory selection must use a native Tauri dialog on Windows and Linux.
- Browser preview must retain manual absolute-path entry and must not invent an absolute path from browser file APIs.
- Workspace selection must not reorder previously known workspace groups.
- Empty transcript hydration must replace, not append to, the visible conversation.
- A newly created session must remain deletable even when deletion is requested before creation finishes.
- Task-understanding UI may render only events already supplied by the backend; it must not synthesize hidden reasoning.
- Every production behavior change follows RED -> GREEN with the focused command recorded below.

---

### Task 1: Stable sidebar layout and workspace/session targeting

**Files:**
- Modify: `athena-gui/src/lib/workspaceStorage.ts`
- Modify: `athena-gui/src/lib/__tests__/workspaceStorage.test.ts`
- Modify: `athena-gui/src/components/shell/ContextSidebar.tsx`
- Modify: `athena-gui/src/components/shell/ContextSidebar.module.css`
- Modify: `athena-gui/src/components/shell/AppShell.tsx`
- Modify: `athena-gui/src/components/__tests__/app-shell.test.tsx`
- Modify: `athena-gui/src/hooks/useWorkspace.ts`
- Create: `athena-gui/src/hooks/__tests__/useWorkspace.test.tsx`
- Modify: `athena-gui/src/App.tsx`

**Interfaces:**
- `addRecentRoot(root, existing)` keeps an already-known root in its existing position and prepends only a new root.
- `WorkspaceActions.switchTo(path: string, sessionId?: string): Promise<void>` records an optional requested session.
- `WorkspaceState.requestedSessionId: string | null` is passed to `usePipeline` after the keyed workspace remount.
- `ContextSidebarProps.onSelectWorkspace(path: string, sessionId?: string): void` carries the clicked cross-workspace session.

- [x] **Step 1: Write failing storage and hook tests**

Change the existing storage expectation and add hook coverage equivalent to:

```ts
expect(addRecentRoot("/b", ["/b", "/a"])).toEqual(["/b", "/a"]);
expect(addRecentRoot("/b", ["/a", "/b"])).toEqual(["/a", "/b"]);

await act(async () => result.current.switchTo("/b", "s-2"));
expect(result.current.requestedSessionId).toBe("s-2");
expect(result.current.recentRoots).toEqual(["/a", "/b"]);
```

Mock `settingsGet`, `setProjectRoot`, and local storage. Assert that a first-run backend cwd not present in recent roots leaves `pickerOpen === true`, while a remembered root closes it.

- [x] **Step 2: Write failing sidebar structure and order tests**

Render roots `C:/alpha`, `C:/beta`, `C:/gamma` with `beta` current. Assert their headings remain alpha/beta/gamma, clicking session `s-gamma` calls `onSelectWorkspace("C:/gamma", "s-gamma")`, and `切换工作区` is contained by a footer outside the element marked as the scroll region.

- [x] **Step 3: Run the focused RED tests**

Run:

```powershell
npm test -- --run src/lib/__tests__/workspaceStorage.test.ts src/hooks/__tests__/useWorkspace.test.tsx src/components/__tests__/app-shell.test.tsx
```

Expected: failures show existing-root promotion, missing requested-session state, current-root prepending, and absent fixed/scrolling structure.

- [x] **Step 4: Implement stable order and three-region sidebar**

Make `addRecentRoot` return the existing order when the trimmed root already exists. In `SessionContext`, derive ordered roots from `recentRoots`, append `currentRoot` only when absent, place loaded groups into that order, and render:

```tsx
<div className={styles.sessionContext}>
  <button className={styles.newSession}>...</button>
  <div className={styles.workspaceScroll} data-testid="workspace-scroll">...</div>
  <footer className={styles.sidebarFooter}>
    <button className={styles.switchWorkspace}>...</button>
  </footer>
</div>
```

Use `min-height: 0` and `overflow-y: auto` only on `workspaceScroll`; the outer sidebar and footer do not scroll. Carry `sessionId` through `AppShell`, `App`, and `useWorkspace`.

- [x] **Step 5: Run GREEN tests and commit Task 1**

Run the Step 3 command and `npm run build`. Commit only Task 1 paths:

```powershell
git add athena-gui/src/App.tsx athena-gui/src/components/shell/ContextSidebar.tsx athena-gui/src/components/shell/ContextSidebar.module.css athena-gui/src/components/shell/AppShell.tsx athena-gui/src/components/__tests__/app-shell.test.tsx athena-gui/src/hooks/useWorkspace.ts athena-gui/src/hooks/__tests__/useWorkspace.test.tsx athena-gui/src/lib/workspaceStorage.ts athena-gui/src/lib/__tests__/workspaceStorage.test.ts
git commit -m "fix(gui): stabilize workspace sidebar"
```

---

### Task 2: Windows and Linux native directory picker

**Files:**
- Modify: `athena-gui/package.json`
- Modify: `athena-gui/package-lock.json`
- Modify: `athena-gui/src-tauri/Cargo.toml`
- Modify: `athena-gui/src-tauri/Cargo.lock`
- Modify: `athena-gui/src-tauri/src/lib.rs`
- Modify: `athena-gui/src-tauri/capabilities/default.json`
- Create: `athena-gui/src/lib/workspaceDialog.ts`
- Create: `athena-gui/src/lib/__tests__/workspaceDialog.test.ts`
- Modify: `athena-gui/src/hooks/useWorkspace.ts`
- Modify: `athena-gui/src/hooks/__tests__/useWorkspace.test.tsx`
- Modify: `athena-gui/src/components/shell/WorkspacePicker.tsx`
- Create: `athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx`

**Interfaces:**
- `selectWorkspaceDirectory(defaultPath?: string | null): Promise<string | null>` returns one native path or `null`.
- `WorkspaceActions.browse(): Promise<void>` opens the dialog and reuses `switchTo` for a chosen path.
- `WorkspaceState.browsing: boolean` prevents duplicate dialog opens.

- [ ] **Step 1: Write failing dialog-boundary tests**

Mock `@tauri-apps/plugin-dialog` and Tauri detection. Assert:

```ts
await selectWorkspaceDirectory("C:/work");
expect(open).toHaveBeenCalledWith({
  directory: true,
  multiple: false,
  defaultPath: "C:/work",
});
```

Also assert cancellation returns `null`, browser mode does not invoke `open`, a selected path reaches `setProjectRoot`, and a thrown dialog error appears in workspace state.

- [ ] **Step 2: Write failing picker component tests**

Assert `浏览目录…` invokes `onBrowse`, is disabled while browsing/switching, cancellation leaves manual input intact, and manual path submission still calls `onSelect`.

- [ ] **Step 3: Run focused RED tests**

Run:

```powershell
npm test -- --run src/lib/__tests__/workspaceDialog.test.ts src/hooks/__tests__/useWorkspace.test.tsx src/components/shell/__tests__/WorkspacePicker.test.tsx
```

Expected: missing module, prop, action, and dependency failures.

- [ ] **Step 4: Install and register the Tauri dialog plugin**

Run `npm run tauri add dialog` from `athena-gui`. Confirm it adds the JS/Rust dependencies, `.plugin(tauri_plugin_dialog::init())`, and the narrow dialog-open permission in `capabilities/default.json`. Do not add filesystem read/write permissions.

Implement the dialog boundary and `browse`; keep the manual input visible in all modes. Pass `browsing` and `onBrowse` through `App.tsx` to `WorkspacePicker`.

- [ ] **Step 5: Run GREEN tests, desktop checks, and commit Task 2**

Run the Step 3 command, `npm run build`, and from `athena-gui/src-tauri` run `cargo check`. Commit only Task 2 paths with:

```powershell
git add athena-gui/package.json athena-gui/package-lock.json athena-gui/src-tauri/Cargo.toml athena-gui/src-tauri/Cargo.lock athena-gui/src-tauri/src/lib.rs athena-gui/src-tauri/capabilities/default.json athena-gui/src/lib/workspaceDialog.ts athena-gui/src/lib/__tests__/workspaceDialog.test.ts athena-gui/src/hooks/useWorkspace.ts athena-gui/src/hooks/__tests__/useWorkspace.test.tsx athena-gui/src/components/shell/WorkspacePicker.tsx athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx athena-gui/src/App.tsx
git commit -m "fix(gui): open native workspace dialog"
```

---

### Task 3: Empty-workspace reset and reliable new-session deletion

**Files:**
- Modify: `athena-gui/src/hooks/usePipeline.ts`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`
- Modify: `athena-gui/src/components/__tests__/app-shell.test.tsx`

**Interfaces:**
- `usePipeline(workspaceRoot?: string | null, requestedSessionId?: string | null)` uses the requested ID during initial restore when available.
- Session mutations use a monotonically increasing request epoch plus a map of in-flight creation promises.

- [ ] **Step 1: Write failing replacement and stale-result tests**

Seed a non-empty transcript, resolve a later session switch with `records: []`, and assert `viewModel.messages` becomes empty. Resolve an older switch after a newer switch and assert the older records never replace the newer transcript.

Mount with `requestedSessionId="s-2"`, return it in `sessionsList`, and assert the first `sessionSwitch` targets `s-2` rather than the remembered active ID.

- [ ] **Step 2: Write failing immediate-delete test**

Hold the new session's `sessionSwitch` promise unresolved, call `newSession()`, then call `deleteSession(newId)`. Assert the delete RPC waits; resolve creation and assert `sessionDelete(newId)` is called and the row disappears.

- [ ] **Step 3: Run focused RED tests**

Run:

```powershell
npm test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.identity.test.tsx src/components/__tests__/app-shell.test.tsx
```

Expected: empty restore preserves old messages, stale completion wins, requested ID is ignored, and immediate deletion races creation.

- [ ] **Step 4: Implement request epochs and serialized mutation**

Initial hydration and every explicit switch capture a new epoch. Apply records/list/current ID only if the epoch is still current. Always call `restoreRecords(records, true)` for a completed session hydration, including empty records.

Track each new session's creation promise in a ref map. Keep its optimistic row until the authoritative response arrives or creation fails. `deleteSession` awaits a matching creation promise before deleting. Remove its title and optimistic marker only after successful deletion.

- [ ] **Step 5: Run GREEN tests and commit Task 3**

Run the Step 3 command and `npm run build`. Commit:

```powershell
git add athena-gui/src/hooks/usePipeline.ts athena-gui/src/hooks/__tests__/usePipeline.test.tsx athena-gui/src/components/__tests__/app-shell.test.tsx
git commit -m "fix(gui): serialize session lifecycle"
```

---

### Task 4: Visible task-understanding output trajectory

**Files:**
- Modify: `athena-gui/src/components/conversation/MessageList.tsx`
- Modify: `athena-gui/src/components/conversation/MessageList.module.css`
- Modify: `athena-gui/src/components/__tests__/conversation-pane.test.tsx`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`

**Interfaces:**
- `segmentMessages` recognizes the messages following the active `CLARIFYING`/`CONFIRMING` preview as a task-understanding activity segment.
- The activity segment renders as `role="log"`, `aria-label="任务理解过程"`, and reuses `TrajectoryItem` for agent, supervisor, tool-call, stdout, and stderr presentation.

- [ ] **Step 1: Write failing live-output component tests**

Render a clarifying preview followed by two deltas sharing `message_id`, an agent tool call, and tool stdout. Assert the reducer merges the deltas and the component shows their resulting text/tool output inside the `任务理解过程` live region while the placeholder remains visible.

Also assert a ready/running preview does not relabel later research output as task understanding.

- [ ] **Step 2: Run focused RED tests**

Run:

```powershell
npm test -- --run src/hooks/__tests__/usePipeline.events.test.tsx src/components/__tests__/conversation-pane.test.tsx
```

Expected: no task-understanding activity region exists.

- [ ] **Step 3: Implement clarification activity grouping**

Extend message segmentation with a `kind: "ordinary" | "ideator" | "clarification"` discriminator. When the latest intent-preview is `CLARIFYING` or `CONFIRMING`, group subsequent non-user output messages into one labeled live section. Render every item through the existing `TrajectoryItem`; do not duplicate message content and do not add polling or synthetic text.

- [ ] **Step 4: Run GREEN tests and commit Task 4**

Run the Step 2 command and the complete GUI suite. Commit:

```powershell
git add athena-gui/src/components/conversation/MessageList.tsx athena-gui/src/components/conversation/MessageList.module.css athena-gui/src/components/__tests__/conversation-pane.test.tsx athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx
git commit -m "fix(gui): show task understanding activity"
```

---

### Task 5: Full verification, closeout, and merge preparation

**Files:**
- Create: `codex_docs/2026-09-02-frontend-workspace-session-fixes-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete: `docs/superpowers/plans/2026-09-02-frontend-workspace-session-fixes.md`

- [ ] **Step 1: Run fresh complete verification**

Run:

```powershell
cd athena-gui
npm test -- --run
npm run build
cd src-tauri
cargo check
cd ..\..
git diff --check main...HEAD
git status --short
```

Record exact counts, exit codes, and any warnings. The pre-existing React `act(...)` warning must either be fixed in its owning test or explicitly shown not to originate from changed behavior; no new warnings are accepted.

- [ ] **Step 2: Audit each reported problem against evidence**

Map each of the six user requirements to its focused test and implementation path. Confirm Windows/Linux are supported by the selected Tauri plugin configuration and that item 4 is explicitly limited to backend-supplied events.

- [ ] **Step 3: Write completion report and close the worktree plan**

Write the completion report with commits, changed files, six-item evidence map, commands, exact results, and residual limitations. Delete this plan and update `codex_docs/CURRENT.md` so it no longer points to it. Commit only closeout paths:

```powershell
git add codex_docs/CURRENT.md codex_docs/2026-09-02-frontend-workspace-session-fixes-completion-report.md docs/superpowers/plans/2026-09-02-frontend-workspace-session-fixes.md
git commit -m "docs: close frontend workspace fixes"
```

- [ ] **Step 4: Merge into main without overwriting parallel work**

Fetch the latest `main` state from the primary worktree, ensure its index/worktree changes do not overlap merge targets, and compute `git merge-tree` evidence before mutation. Merge `fix/frontend-session-fixes` into `main` with a normal non-destructive merge commit. If main has overlapping uncommitted files or merge conflicts, stop and report exact paths rather than stashing, resetting, or overwriting them.

After merge, rerun `npm test -- --run`, `npm run build`, and `cargo check` from the main worktree. Only then report completion.
