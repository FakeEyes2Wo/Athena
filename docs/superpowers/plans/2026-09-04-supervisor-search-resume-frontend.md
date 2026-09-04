# Supervisor Search Resume Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route messages from settled research sessions back to Supervisor so users can request more Search budget, while hiding zero-session workspace groups without deleting or rewriting any workspace data.

**Architecture:** The backend remains the authority for whether chat input belongs to task clarification or an existing research session and emits `interaction_mode` in every state projection. The React view model stores that field and uses it as the primary routing signal, with one narrow evidence-based fallback for legacy backends. `ContextSidebar` derives a presentation-only filtered group list; storage and backend session inventories remain untouched.

**Tech Stack:** TypeScript 5.5+, React 18, Vitest 2, Testing Library, Vite, existing Tauri bridge and Rust shell.

## Global Constraints

- Read `codex_docs/CURRENT.md`, the approved design, the backend completion report, and this plan before implementation; update each checkbox only after fresh evidence passes.
- Begin only after the backend plan has closed, `codex_docs/CURRENT.md` names this as the sole active implementation plan, and backend `interaction_mode` compatibility tests are green.
- Execute in an isolated worktree created with `using-git-worktrees`; preserve unrelated changes and stage only the files named by the current task.
- Do not add a frontend-owned phase/state machine, parse natural-language budget requests in React, or duplicate backend resume eligibility rules.
- `interaction_mode` has exactly two wire values: `"clarification"` and `"supervisor"`. Unknown or missing values project to `null`, not to an invented mode.
- A missing mode may use the compatibility fallback only when the loaded session contains research evidence. COMPLETED and WAITING are eligible; a stale RUNNING status with no evidence is not.
- Exact `/search +N` and natural-language requests both travel through the existing `sendControl(content)` bridge. The frontend never calculates or writes an absolute Search limit.
- Workspace filtering is render-only. Do not call session deletion, remove local-storage entries, rewrite recent roots, or trigger `sessionsListFor` merely because a group has zero sessions.
- Keep `New Session` and `Switch Workspace` controls visible even when every workspace group is hidden.
- Add no production dependency and expose no new user setting or constructor parameter.
- User-visible errors remain concise through the existing `errorMessage()` path; backend logs retain diagnostic detail.

---

## File map and ownership

### Modified production files

- `athena-gui/src/types/ui.ts`: owns `InteractionMode`, the `PipelineViewModel.interactionMode` field, and the empty-model default.
- `athena-gui/src/lib/tauri-bridge.ts`: types `interaction_mode` on state events and session-switch summaries without changing the RPC shape.
- `athena-gui/src/hooks/usePipeline.ts`: projects backend `interaction_mode` state events and chooses Supervisor versus clarification routing from the authoritative mode.
- `athena-gui/src/components/shell/ContextSidebar.tsx`: derives and renders only workspace groups that contain at least one session.

### Modified tests

- `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`: verifies state-event projection, session switching, unknown values, and resets.
- `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`: verifies completed-session prose and `/search +N` routing, legacy compatibility, empty-session safety, and concise error behavior.
- `athena-gui/src/components/__tests__/app-shell.test.tsx`: verifies presentation-only hiding, stable ordering of nonempty groups, retained global controls, and absence of destructive calls.

### Documentation and closeout

- `docs/athena-gui-design.md`: documents the backend-owned interaction contract and presentation-only workspace filtering.
- `docs/superpowers/specs/2026-09-04-supervisor-transactional-search-resume-design.md`: changes status to implemented only after both backend and frontend acceptance evidence is fresh.
- `codex_docs/2026-09-04-supervisor-search-resume-frontend-completion-report.md`: records exact commands, pass counts, build evidence, and the no-deletion assertion.
- `codex_docs/CURRENT.md`: advances the active pointer to the queued experiment-document projection plan without disturbing parallel or paused plans.

## Execution preflight

- [ ] Record `git status --short --branch`, `git diff --cached --name-status`, `git worktree list --porcelain`, and the current commit. Create `feat/supervisor-search-resume-frontend` in a new worktree; do not reuse `.worktrees/output-main-integration`.
- [ ] Verify `codex_docs/CURRENT.md` names this file as the sole active plan, queues `docs/superpowers/plans/2026-09-04-experiment-document-projection.md` next, the backend plan file is absent, and `codex_docs/2026-09-04-supervisor-transactional-search-resume-backend-completion-report.md` exists.
- [ ] Run the backend handoff gate and record its exact result: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_completed_search_resume.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/research/supervisor/test_events.py`.
- [ ] Run the frontend baseline and record its exact result: `npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.events.test.tsx src/components/__tests__/app-shell.test.tsx`.

---

### Task 1: Authoritative interaction-mode projection and message routing

**Files:**
- Modify: `athena-gui/src/types/ui.ts`
- Modify: `athena-gui/src/lib/tauri-bridge.ts`
- Modify: `athena-gui/src/hooks/usePipeline.ts`
- Test: `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`
- Test: `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`

**Interfaces:**
- Consumes: backend state-event field `interaction_mode: "clarification" | "supervisor"` and the existing bridge function `sendControl(command: string): Promise<unknown>`.
- Produces: `export type InteractionMode = "clarification" | "supervisor"`.
- Produces: `SessionSwitchResult { records: SessionRecord[]; sessions?: string[]; interaction_mode?: InteractionMode | null }` and a `sessionSwitch(sessionId: string): Promise<SessionSwitchResult>` return type.
- Produces: `PipelineViewModel.interactionMode: InteractionMode | null`.
- Produces: internal `normalizeInteractionMode(value: unknown): InteractionMode | null` and `hasResearchEvidence(viewModel: PipelineViewModel, runStarted: boolean): boolean` helpers.
- Preserves: `sendPrompt(message: string): Promise<void>` as the only UI input entry point; no new hook argument or public action is added.

- [ ] **Step 1: Add failing state-event projection tests**

Extend `usePipeline.events.test.tsx` with explicit events and assertions:

```tsx
it("projects the backend-owned interaction mode", async () => {
  const { result } = await renderHydratedPipeline();

  await act(async () => {
    eventHandlers[0]?.({
      kind: "state",
      data: {
        phase: "SEARCH",
        status: "COMPLETED",
        interaction_mode: "supervisor",
      },
    });
  });

  await waitFor(() => {
    expect(result.current.viewModel.interactionMode).toBe("supervisor");
    expect(result.current.viewModel.status).toBe("completed");
  });
});

it("treats an unknown interaction mode as unavailable", async () => {
  const { result } = await renderHydratedPipeline();

  await act(async () => {
    eventHandlers[0]?.({
      kind: "state",
      data: { interaction_mode: "future-mode" },
    });
  });

  await waitFor(() => expect(result.current.viewModel.interactionMode).toBeNull());
});
```

Add a session-switch assertion using the suite's existing `sessionSwitch` mock:

```tsx
it("replaces interaction mode from each session switch summary", async () => {
  const { result } = await renderHydratedPipeline();
  await act(async () => {
    eventHandlers[0]?.({
      kind: "state",
      data: { interaction_mode: "supervisor" },
    });
  });

  vi.mocked(sessionSwitch).mockResolvedValueOnce({
    records: [],
    sessions: ["next"],
    interaction_mode: "clarification",
  });
  await act(async () => result.current.switchSession("next"));
  expect(result.current.viewModel.interactionMode).toBe("clarification");

  vi.mocked(sessionSwitch).mockResolvedValueOnce({
    records: [],
    sessions: ["legacy"],
  });
  await act(async () => result.current.switchSession("legacy"));
  expect(result.current.viewModel.interactionMode).toBeNull();
});
```

This proves the prior session's mode cannot leak across navigation.

- [ ] **Step 2: Run projection tests and confirm the missing field causes failure**

Run:

```powershell
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.events.test.tsx
```

Expected: FAIL because `PipelineViewModel` has no `interactionMode` and state events do not project `interaction_mode`.

- [ ] **Step 3: Add failing routing tests for completed, waiting, legacy, and empty sessions**

Add this local hydration helper to `usePipeline.test.tsx` so state events cannot race
the mount-time session hydration:

```tsx
async function renderHydratedPipeline() {
  const hook = renderHook(() => usePipeline());
  await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));
  await waitFor(() => expect(pipelineEventHandler).not.toBeNull());
  await act(async () => undefined);
  return hook;
}
```

Then add these cases using the suite's existing bridge mocks and
`pipelineEventHandler`:

```tsx
it.each(["COMPLETED", "WAITING"])(
  "routes a settled %s session budget request to Supervisor",
  async (status) => {
    bridgeMocks.sendControl.mockResolvedValue({ response: "已增加 20 次 Search 预算。" });
    const { result } = await renderHydratedPipeline();

    act(() => pipelineEventHandler?.({
      kind: "state",
      data: {
        phase: "SEARCH",
        status,
        interaction_mode: "supervisor",
        search: { attempts: 12, limit: 12, successes: 8 },
      },
    }));
    await act(async () => result.current.sendPrompt("再 search 20 个"));

    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("再 search 20 个");
    expect(bridgeMocks.taskClarificationStart).not.toHaveBeenCalled();
  },
);

it("routes exact search syntax through the same control bridge", async () => {
  bridgeMocks.sendControl.mockResolvedValue({ response: "已增加 20 次 Search 预算。" });
  const { result } = await renderHydratedPipeline();

  act(() => pipelineEventHandler?.({
    kind: "state",
    data: { status: "COMPLETED", interaction_mode: "supervisor" },
  }));
  await act(async () => result.current.sendPrompt("/search +20"));

  expect(bridgeMocks.sendControl).toHaveBeenCalledWith("/search +20");
  expect(bridgeMocks.taskClarificationStart).not.toHaveBeenCalled();
});

it("keeps a backend-declared clarification session on the clarification path", async () => {
  const { result } = await renderHydratedPipeline();

  act(() => pipelineEventHandler?.({
    kind: "state",
    data: {
      status: "RUNNING",
      interaction_mode: "clarification",
      search: { attempts: 3, limit: 3 },
    },
  }));
  await act(async () => result.current.sendPrompt("新的数据分析任务"));

  expect(bridgeMocks.taskClarificationStart).toHaveBeenCalledWith("新的数据分析任务");
  expect(bridgeMocks.sendControl).not.toHaveBeenCalled();
});
```

Also add two compatibility cases:

```tsx
it("uses research evidence for a completed legacy state with no interaction mode", async () => {
  const { result } = await renderHydratedPipeline();
  act(() => pipelineEventHandler?.({
    kind: "state",
    data: { status: "COMPLETED", plans: [{ id: "plan-1" }] },
  }));

  await act(async () => result.current.sendPrompt("继续 search 5 个"));
  expect(bridgeMocks.sendControl).toHaveBeenCalledWith("继续 search 5 个");
});

it("does not trust a stale RUNNING status without research evidence", async () => {
  const { result } = await renderHydratedPipeline();
  act(() => pipelineEventHandler?.({ kind: "state", data: { status: "RUNNING" } }));

  await act(async () => result.current.sendPrompt("分析新任务"));
  expect(bridgeMocks.taskClarificationStart).toHaveBeenCalledWith("分析新任务");
  expect(bridgeMocks.sendControl).not.toHaveBeenCalled();
});
```

- [ ] **Step 4: Run routing tests and verify current status-gated logic fails the settled cases**

Run:

```powershell
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx
```

Expected: the COMPLETED case calls `taskClarificationStart`, and the new authoritative mode assertions cannot compile or fail at runtime.

- [ ] **Step 5: Implement the minimal typed projection and routing decision**

In `types/ui.ts`, add the type and field:

```ts
export type InteractionMode = "clarification" | "supervisor";

export interface PipelineViewModel {
  phase: string;
  status: PipelineStatus;
  interactionMode: InteractionMode | null;
  // retain all existing fields unchanged
}

export function createEmptyPipelineViewModel(): PipelineViewModel {
  return {
    phase: "idle",
    status: "idle",
    interactionMode: null,
    // retain all existing defaults unchanged
  };
}
```

In `tauri-bridge.ts`, import the UI type with a type-only import, add it to the
state-event DTO, and name the session-switch result instead of repeating anonymous
object types:

```ts
import type { InteractionMode } from "../types/ui";

export interface ExperimentEvent {
  kind: string;
  data: {
    interaction_mode?: InteractionMode | null;
    [key: string]: unknown;
  };
}

export interface SessionSwitchResult {
  records: SessionRecord[];
  sessions?: string[];
  interaction_mode?: InteractionMode | null;
}

export function sessionSwitch(sessionId: string): Promise<SessionSwitchResult> {
  if (hasTauri) return invoke<SessionSwitchResult>("session_switch", { sessionId });
  return wsBackend.call("session_switch", { session_id: sessionId }) as Promise<SessionSwitchResult>;
}
```

Retain every existing event-data property around the inserted `interaction_mode`
field; the abbreviated block above shows only the new member and existing index
signature.

In `usePipeline.ts`, normalize only exact wire values:

```ts
function normalizeInteractionMode(value: unknown): InteractionMode | null {
  return value === "clarification" || value === "supervisor" ? value : null;
}

function hasResearchEvidence(viewModel: PipelineViewModel, started: boolean): boolean {
  return started ||
    viewModel.plans.length > 0 ||
    viewModel.rightRail.searchAttempts > 0 ||
    viewModel.rightRail.latestExperimentId !== null;
}
```

Import `InteractionMode` with the existing UI types. In the `state` event branch, set `next.interactionMode = normalizeInteractionMode(data.interaction_mode)`. Because every new backend state projection contains the field, assigning `null` for missing/unknown values deliberately enables the compatibility path instead of retaining a stale mode from another session. In both initial hydration and explicit `switchSession`, apply `normalizeInteractionMode(result.interaction_mode)` immediately after restoring records. Apply the same hint to a successfully created optimistic session; missing legacy hints reset to `null`.

Replace `runInProgress` with one decision:

```ts
const researchEvidence = hasResearchEvidence(viewModel, runStarted.current);
const routesToSupervisor =
  viewModel.interactionMode === "supervisor" ||
  (viewModel.interactionMode === null && researchEvidence);

if (routesToSupervisor) {
  // retain the existing sendControl, response rendering, errorMessage, and rethrow block
  return;
}

await startClarification(content);
```

Do not special-case Chinese phrases or calculate `+20`; the same control bridge must receive the original trimmed string.

- [ ] **Step 6: Verify projection, route priority, error behavior, and no duplicate send**

Run:

```powershell
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.events.test.tsx src/hooks/__tests__/usePipeline.test.tsx
```

Expected: PASS. Confirm the settled-session tests observe one `sendControl` call, zero clarification calls, and the existing rejected-control test still produces one concise error message.

- [ ] **Step 7: Commit the routing slice**

```powershell
git add athena-gui/src/types/ui.ts athena-gui/src/lib/tauri-bridge.ts athena-gui/src/hooks/usePipeline.ts athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx athena-gui/src/hooks/__tests__/usePipeline.test.tsx
git diff --cached --check
git commit -m "feat(gui): route settled research input to supervisor"
```

---

### Task 2: Presentation-only hiding of zero-session workspaces

**Files:**
- Modify: `athena-gui/src/components/shell/ContextSidebar.tsx`
- Test: `athena-gui/src/components/__tests__/app-shell.test.tsx`

**Interfaces:**
- Consumes: existing `WorkspaceGroup { root: string; name: string; isCurrent: boolean; sessions: SessionItem[] }` and cached `loadWorkspaceSessions(root)` results.
- Produces: local derived `visibleGroups: WorkspaceGroup[]`, preserving the order of `groups` while excluding only `sessions.length === 0`.
- Preserves: all `ContextSidebarProps`, `onSelectWorkspace`, `onSelectSession`, `onNewSession`, `onDeleteSession`, recent-root storage, and session storage without mutation.

- [ ] **Step 1: Replace empty-placeholder expectations with failing render-only filter tests**

In `app-shell.test.tsx`, replace the two tests that currently expect `暂无会话` with these behaviors:

```tsx
it("hides a current workspace with zero sessions but keeps global controls", () => {
  const onSwitchWorkspace = vi.fn();
  const onSelectWorkspace = vi.fn();
  const deleteSession = vi.fn();
  const workspaceKey = "athena.workspace.sessions:C:/projects/titanic";
  const before = "[]";
  localStorage.setItem(workspaceKey, before);

  renderUi(
    <AppShell
      currentRoot="C:/projects/titanic"
      recentRoots={["C:/projects/titanic"]}
      switching={false}
      onSwitchWorkspace={onSwitchWorkspace}
      onSelectWorkspace={onSelectWorkspace}
      pipeline={makePipeline({ sessions: [], deleteSession }) as never}
    />,
  );

  expect(screen.queryByRole("button", { name: "titanic" })).not.toBeInTheDocument();
  expect(screen.queryByText("暂无会话")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "新会话" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "切换工作区" })).toBeInTheDocument();
  expect(onSelectWorkspace).not.toHaveBeenCalled();
  expect(deleteSession).not.toHaveBeenCalled();
  expect(bridgeMocks.sessionsListFor).not.toHaveBeenCalled();
  expect(localStorage.getItem(workspaceKey)).toBe(before);
});
```

Add a mixed-list test:

```tsx
it("renders only nonempty workspace groups in stable recent-root order", () => {
  localStorage.setItem(
    "athena.workspace.sessions:C:/alpha",
    JSON.stringify([{ id: "a-1", title: "Alpha session" }]),
  );
  localStorage.setItem(
    "athena.workspace.sessions:C:/gamma",
    JSON.stringify([{ id: "g-1", title: "Gamma session" }]),
  );
  const onSelectWorkspace = vi.fn();

  renderUi(
    <AppShell
      currentRoot="C:/beta"
      recentRoots={["C:/alpha", "C:/beta", "C:/empty", "C:/gamma"]}
      switching={false}
      onSwitchWorkspace={vi.fn()}
      onSelectWorkspace={onSelectWorkspace}
      pipeline={makePipeline({
        sessions: [{ id: "b-1", title: "Beta session" }],
        currentSessionId: "b-1",
      }) as never}
    />,
  );

  const headings = screen.getAllByRole("button")
    .filter((button) => ["alpha", "beta", "empty", "gamma"].includes(button.textContent ?? ""));
  expect(headings.map((button) => button.textContent)).toEqual(["alpha", "beta", "gamma"]);
  expect(screen.queryByRole("button", { name: "empty" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Alpha session" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Beta session" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Gamma session" })).toBeInTheDocument();
  expect(bridgeMocks.sessionsListFor).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Gamma session" }));
  expect(onSelectWorkspace).toHaveBeenCalledWith("C:/gamma", "g-1");
});
```

Keep the existing cached-session memoization and switching-interactivity tests.

- [ ] **Step 2: Run the sidebar suite and confirm current empty groups remain visible**

Run:

```powershell
npm --prefix athena-gui test -- --run src/components/__tests__/app-shell.test.tsx
```

Expected: FAIL because the current implementation renders workspace headers plus `暂无会话` for empty groups.

- [ ] **Step 3: Derive a nonempty render list without changing persistence**

In `SessionContext`, retain the existing `otherGroups`, `orderedRoots`, `otherGroupsByRoot`, and `groups` construction. Immediately after `groups`, add:

```ts
const visibleGroups = groups.filter((group) => group.sessions.length > 0);
```

Then render `visibleGroups.map(...)` and remove only this presentation branch:

```tsx
{group.sessions.length === 0 && <p className={styles.hint}>暂无会话</p>}
```

Do not change `loadWorkspaceSessions`, `recentRoots`, any callback, or the sidebar footer. Do not remove the shared `.hint` style because other sidebar states still use it.

- [ ] **Step 4: Verify hiding, ordering, interactions, and zero mutations**

Run:

```powershell
npm --prefix athena-gui test -- --run src/components/__tests__/app-shell.test.tsx src/lib/__tests__/workspaceStorage.test.ts
```

Expected: PASS. Confirm the suite proves all empty group headings and placeholder text are absent, nonempty order remains `alpha`, `beta`, `gamma`, global controls remain present, `sessionsListFor` is not invoked, and storage content is unchanged.

- [ ] **Step 5: Commit the presentation-only slice**

```powershell
git add athena-gui/src/components/shell/ContextSidebar.tsx athena-gui/src/components/__tests__/app-shell.test.tsx
git diff --cached --check
git commit -m "fix(gui): hide empty workspace groups"
```

---

### Task 3: Frontend regression, documentation, and closeout

**Files:**
- Modify: `docs/athena-gui-design.md`
- Modify: `docs/superpowers/specs/2026-09-04-supervisor-transactional-search-resume-design.md`
- Create: `codex_docs/2026-09-04-supervisor-search-resume-frontend-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete after acceptance: `docs/superpowers/plans/2026-09-04-supervisor-search-resume-frontend.md`

**Interfaces:**
- Consumes: Task 1's `PipelineViewModel.interactionMode` and Task 2's presentation-only filtering behavior.
- Produces: tested frontend artifact, Rust shell verification, final design status, exact completion evidence, and activation of the queued experiment-document projection work.
- Preserves: the parallel authoritative-baseline plan pointer and paused JW-SSD TUI plan pointer in `codex_docs/CURRENT.md`.

- [ ] **Step 1: Run formatting and diff safety gates**

Run:

```powershell
git diff --check
git status --short
git diff --name-only
```

Expected: no whitespace errors; changed paths are limited to the files owned by Tasks 1-3. The committed queued experiment-document design and implementation plan remain unstaged and unchanged.

- [ ] **Step 2: Run focused acceptance tests from a cold Vitest invocation**

Run:

```powershell
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.events.test.tsx src/components/__tests__/app-shell.test.tsx src/lib/__tests__/workspaceStorage.test.ts
```

Expected: PASS with zero failed tests. Record exact file/test counts and elapsed time.

- [ ] **Step 3: Run the full frontend suite and production build**

Run:

```powershell
npm --prefix athena-gui test
npm --prefix athena-gui run build
```

Expected: all Vitest files pass and TypeScript/Vite production build exits 0. Investigate every failure before classifying it; do not declare completion from focused tests alone.

- [ ] **Step 4: Verify the Tauri shell still compiles and passes tests**

Run:

```powershell
cargo test --manifest-path athena-gui/src-tauri/Cargo.toml
cargo check --manifest-path athena-gui/src-tauri/Cargo.toml
```

Expected: both commands exit 0. Record exact Rust test counts and warnings.

- [ ] **Step 5: Re-run the backend/frontend seam test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_completed_search_resume.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/research/supervisor/test_events.py
npm --prefix athena-gui test -- --run src/hooks/__tests__/usePipeline.test.tsx src/hooks/__tests__/usePipeline.events.test.tsx
```

Expected: PASS. The evidence must connect backend `interaction_mode="supervisor"` for eligible COMPLETED/WAITING sessions to exactly one frontend `sendControl("再 search 20 个")` call and zero clarification calls.

- [ ] **Step 6: Audit the zero-deletion boundary directly**

Run:

```powershell
git diff -- athena-gui/src/components/shell/ContextSidebar.tsx athena-gui/src/components/__tests__/app-shell.test.tsx
rg -n "removeItem|deleteSession|sessionsListFor|onDeleteSession" athena-gui/src/components/shell/ContextSidebar.tsx athena-gui/src/components/__tests__/app-shell.test.tsx
```

Expected: the production diff contains one derived `.filter(...)`, one `map` target change, and removal of the empty placeholder only. Existing delete controls for real current-session rows remain; no empty-group branch calls deletion, storage writes, or RPC hydration.

- [ ] **Step 7: Update documentation and write the evidence report**

Add these exact contract points to `docs/athena-gui-design.md`:

```markdown
- `interaction_mode` is backend-owned and accepts only `clarification` or `supervisor`.
- A missing mode uses research evidence only as a legacy compatibility fallback, including settled sessions.
- Search budget text is forwarded unchanged to `sendControl`; React does not parse or persist the budget.
- Workspace groups with zero sessions are filtered at render time only. Their roots and cached data are not deleted.
```

Change the design status to `implemented and verified` only after Steps 1-6 pass. Create the completion report with:

```markdown
# Supervisor Search Resume Frontend Completion Report

## Delivered behavior
- Settled-session input follows backend interaction mode to Supervisor.
- Missing-mode compatibility is evidence-gated; empty sessions still enter clarification.
- Empty workspace groups are hidden without persistence or backend mutations.

## Verification evidence
- Focused Vitest: <record exact command output>
- Full Vitest: <record exact command output>
- Production build: <record exact command output>
- Cargo test/check: <record exact command output>
- Backend/frontend seam: <record exact command output>

## Repository state
- Implementation commits: <record exact hashes>
- Unrelated files left untouched: <record exact paths or `none`>
```

Angle-bracket lines above are report fields: replace each with the observed output or `none`; never claim an unexecuted check.

- [ ] **Step 8: Close the plan only after every frontend acceptance criterion is green**

Delete this completed plan with `apply_patch`. Update `codex_docs/CURRENT.md` so the queued experiment-document projection plan becomes active, remove its now-empty queued sections, retain the parallel and paused sections verbatim, then stage only owned documentation:

```markdown
Active implementation plan:
- `docs/superpowers/plans/2026-09-04-experiment-document-projection.md`

Most recent completed work:
- `codex_docs/2026-09-04-supervisor-search-resume-frontend-completion-report.md`
- `codex_docs/2026-09-04-supervisor-transactional-search-resume-backend-completion-report.md`
```

Remove the `Queued subsequent implementation plan` and `Queued subsequent supporting design spec` sections after promoting their entries. Add the experiment-document design to the ordinary `Design spec` list if it is not already present.

```powershell
git add docs/athena-gui-design.md docs/superpowers/specs/2026-09-04-supervisor-transactional-search-resume-design.md codex_docs/2026-09-04-supervisor-search-resume-frontend-completion-report.md codex_docs/CURRENT.md
git add -u docs/superpowers/plans/2026-09-04-supervisor-search-resume-frontend.md
git diff --cached --check
git commit -m "docs: complete supervisor search resume frontend"
```

Do not stage the queued experiment-document plan or design, merge, or push without a separate user instruction. Hand off exact test evidence, commit hashes, and the newly active plan.
