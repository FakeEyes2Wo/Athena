# Frontend Responsiveness Design

## Problem

The earlier workspace/session fixes are present on `main`, but two regressions
remain:

1. A workspace switch sets one global `switching` flag until the backend finishes
   rebuilding the runtime. Every workspace entry is disabled during that wait, so
   a slow request makes the UI look permanently unclickable.
2. The sidebar eagerly requests session lists for every recent workspace over the
   same sequential WebSocket used by interactive commands. Streaming output also
   copies and scans the full message list for every delta, so response time degrades
   as workspaces and messages accumulate.

The original six workspace/session requirements remain regression constraints.

## Chosen Approach

Use a frontend-only responsiveness fix:

- accept workspace navigation while a switch is pending and coalesce repeated
  selections to the latest requested workspace instead of issuing concurrent RPCs;
- render other-workspace session summaries from a local cache populated whenever
  that workspace is active, eliminating eager background `sessions_list_for` RPCs;
- batch streamed output updates per animation frame and reduce full-history work;
- narrow React effect dependencies and memoize unchanged trajectory rows.

Rejected alternatives:

- Merely removing `disabled` would enqueue multiple expensive runtime rebuilds and
  make latency worse.
- Making Python/WebSocket requests concurrent or cancellable would be broader than
  the requested frontend task and would require mutation serialization in the
  backend.

## Workspace Switch Coordinator

`useWorkspace.switchTo(path, sessionId)` remains the public interface. Internally,
the hook owns at most one active `setProjectRoot` call and one replaceable queued
intent. If another selection arrives while the active call is pending, it replaces
the queued intent. When the active call settles, the coordinator immediately runs
the latest queued intent and discards any superseded intermediate result from React
state.

The sidebar footer always opens the picker. Other-workspace headers/sessions and
picker choices remain selectable during switching so the user can revise the queued
target. The native directory dialog remains single-instance through `browsingRef`.
"Continue using current" stays disabled while a backend switch is pending because
the backend runtime may temporarily differ from the last committed frontend root.

Errors from superseded requests are ignored. Only the final selected intent may
update `currentRoot`, `requestedSessionId`, recent-root storage, picker visibility,
or the visible error. `switching` becomes false only when neither an active nor a
queued intent remains.

## Non-blocking Workspace Summaries

Remove `sessionsListFor` calls from `ContextSidebar`. `usePipeline.applySessions`
stores the authoritative current-workspace session IDs and resolved titles in a
small localStorage cache keyed by workspace root. The sidebar reads that cache for
other workspaces synchronously.

Visiting a workspace refreshes its cache. Deleting a session or receiving an empty
authoritative list removes stale cached rows, so empty workspaces render "暂无会话".
On first use after upgrade, an unvisited workspace may show no cached sessions; its
workspace header remains immediately clickable and visiting it loads the
authoritative list. The recent workspace order is never changed by cache reads.

## Streaming and Rendering Performance

The event subscription queues only `output` events and flushes them at most once per
animation frame. A single state update applies the batch using one copied message
array and an ID-to-index map, coalescing multiple deltas for the same message before
committing React state. Non-output events flush queued output first to preserve event
order.

History replay uses the same indexed batch primitive instead of repeatedly copying
the growing message list. Log entries for the flushed output batch are appended in
one bounded update while preserving their individual order and metadata.

`TrajectoryItem` is memoized so unchanged history rows do not rerender for each
streaming update. The AppShell session-refresh effect depends on the specific module,
session ID, status, and switch callback rather than the entire pipeline object.

## Error and Lifecycle Handling

- Queued workspace intents are cleared on unmount and may not update state later.
- Output animation frames are cancelled and their queue discarded on pipeline
  unmount, preventing stale workspace output from leaking into the next workspace.
- A rejected final workspace switch restores interactive state and exposes its
  normalized error; a subsequent click can retry.
- LocalStorage cache parse/write failures degrade to an empty summary without
  blocking workspace navigation.

## Verification

Tests must prove:

- the footer and picker remain usable while a switch Promise is pending;
- rapid A → B → C selection starts A, does not start B, then starts and commits C;
- superseded success/failure cannot overwrite the final target;
- sidebar mounting performs zero `sessionsListFor` RPCs and uses cached summaries;
- authoritative empty lists clear cached summaries;
- a burst of output deltas is committed in one scheduled flush and produces the
  same ordered messages/logs;
- history replay and all original six requirement tests still pass.

Final gates are the complete frontend test suite, production build, `cargo check`,
diff review, and merge back to `main` without touching unrelated backend work.
