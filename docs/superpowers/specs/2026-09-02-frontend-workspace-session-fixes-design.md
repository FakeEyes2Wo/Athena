# Frontend Workspace and Session Fixes Design

## 1. Scope

This change fixes the six reported Athena GUI problems in the isolated
`fix/frontend-session-fixes` worktree. The scope is limited to the React GUI and
its Tauri desktop shell. Python research-runtime behavior and event production
remain unchanged.

The desktop app must work on Windows and Linux. Browser preview remains useful,
but browser security means it cannot return an unrestricted native absolute
directory path; the existing manual path entry remains the browser fallback.

## 2. Sidebar layout and workspace order

`ContextSidebar` will use three vertical regions in session mode:

1. a non-scrolling new-session action;
2. a `min-height: 0`, independently scrolling workspace/session list;
3. a non-scrolling footer containing `切换工作区`.

The outer sidebar will no longer scroll as one unit. This keeps the workspace
switch control against the visible bottom edge regardless of session count.

Workspace order is stable. The current workspace is marked in place instead of
being prepended. Selecting a known workspace does not promote it in recent-root
storage. A genuinely new path is added once without changing the relative order
of existing roots. The active root is inserted only when it is absent from the
stored list.

Clicking a session under another workspace carries both the target root and
session ID through the workspace switch. The newly mounted pipeline restores
that requested session when it exists; otherwise it falls back to the backend's
active session.

## 3. Native directory selection

The Tauri desktop shell will install and register the official dialog plugin.
The main-window capability grants only directory-open dialog access. A small
frontend boundary exposes:

```ts
selectWorkspaceDirectory(defaultPath?: string | null): Promise<string | null>
```

In Tauri it calls `open({ directory: true, multiple: false, defaultPath })` and
returns the chosen path or `null` on cancellation. In browser preview it returns
`null`, leaving manual absolute-path submission available.

`WorkspacePicker` gains a primary `浏览…` action. A selected directory flows
through the existing `setProjectRoot` RPC, so backend validation and runtime
switching are unchanged. Dialog errors are shown in the picker's existing error
area; cancellation is not an error.

## 4. Session lifecycle consistency

Workspace/session hydration becomes replacement-based rather than additive:
loading a session always replaces the conversation transcript, including when
the returned record list is empty. Results from a superseded workspace or
session request are ignored by an epoch/token check.

New-session creation is optimistic but tracked as an in-flight mutation. A
backend list response cannot remove a still-pending new session. Deleting that
session immediately waits for its creation/switch request, then calls
`sessionDelete`; on failure the visible row remains and an error is shown. On
success the session and title are removed and, when necessary, the clean default
session is restored.

These rules ensure an untouched workspace clears the main pane and a new
session always has a working delete action.

## 5. Task-understanding activity

While clarification status is `CLARIFYING` or `CONFIRMING`, the conversation
shows a live `任务理解过程` region sourced from the frontend's existing pipeline
`output` events. Agent/supervisor text is displayed directly, tool calls are
identified, and stdout/stderr remain collapsible. Streaming deltas continue to
merge by `message_id`, so partial output updates one row instead of producing
duplicates.

This frontend branch does not invent hidden chain-of-thought or add an LLM to
the deterministic clarification controller. It displays all task-understanding
output and reasoning-like explanatory text that the backend already emits. If a
provider/runtime sends no output event, the UI retains the `任务理解中…` placeholder.

## 6. Errors and accessibility

- Native dialog cancellation leaves the picker unchanged.
- Native dialog failure is rendered as a user-visible picker error.
- Workspace/session RPC failures never silently clear the currently valid view.
- New buttons have stable accessible names and keyboard focus styles.
- The activity region uses `role="log"` and `aria-live="polite"` without
  stealing focus.

## 7. Verification

Test-first coverage will prove:

- the sidebar has a dedicated scroll region and fixed footer;
- workspace order does not change when selection changes;
- cross-workspace session selection retains the requested session ID;
- the Tauri dialog is called with single-directory options and cancellation is
  inert;
- empty workspace/session hydration replaces old conversation content;
- a newly created session can be deleted before or after creation resolves;
- clarification-time output deltas and tool output are visibly rendered;
- all GUI Vitest tests, TypeScript/Vite build, and Tauri `cargo check` pass.

The final branch is merged into `main` only after confirming the main worktree's
parallel changes and completing fresh verification on the merged result.
