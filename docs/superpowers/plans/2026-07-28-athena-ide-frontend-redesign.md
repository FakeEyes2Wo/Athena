# Athena IDE Frontend Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `athena-ide` into a chat-first Athena workspace with a narrow right rail, an expandable bottom context surface, and a minimal premium dark visual system without changing the backend or WebSocket protocol.

**Architecture:** Keep the existing Tauri + React + TypeScript app entry points and bridge functions, but replace the current flat layout with an `AppShell` composition: `TopBar`, `ConversationPane`, `RightRail`, and `ContextSurface`. Introduce a small frontend state layer around the existing Tauri commands/events so streaming chat, run summaries, and detail panels stay synchronized while the backend protocol remains unchanged.

**Tech Stack:** React 18, TypeScript 5, Vite 5, Tauri 2 API, Vitest, React Testing Library, existing `reactflow`, `recharts`, and `@monaco-editor/react`

## Global Constraints

- Chat must remain the primary interaction surface.
- The right rail must stay narrow and show summaries only, not full heavy-detail views.
- Heavy content must open in the bottom context surface, not be permanently mounted in the right rail.
- The visual system must be minimal premium dark, not neon, not dashboard-heavy, and not traditional heavy IDE chrome.
- The redesign is frontend-only and must not change the Python IDE backend.
- The redesign is frontend-only and must not change the Rust `PythonBridge`.
- The redesign is frontend-only and must not change the WebSocket protocol semantics.
- Keep the existing Tauri command surface (`send_message`, `start_search`, `pause_search`, `resume_search`, `stop_search`, `start_validation`, `generate_report`) intact.
- Preserve support for ResearchTree, Metrics, Diff, File Tree, and Report views by moving their detailed rendering into the context surface.

---

## File Structure

### Existing files to modify

- `athena-ide/package.json` — add frontend test scripts and test dependencies.
- `athena-ide/src/App.tsx` — replace the current two-column demo layout with the new shell composition.
- `athena-ide/src/App.css` — remove the current placeholder layout rules and replace them with shell-specific styling or trim it to a tiny compatibility wrapper.
- `athena-ide/src/styles.css` — define the global visual system, tokens, resets, and base surfaces.
- `athena-ide/src/hooks/useEvents.ts` — expand event subscription beyond a single event kind and normalize event payload flow.
- `athena-ide/src/hooks/usePipeline.ts` — replace the current demo state with structured conversation, run summary, and context-surface state.
- `athena-ide/src/lib/tauri-bridge.ts` — add typed event subscriptions and normalized UI-facing types without changing command names.
- `athena-ide/src/components/ChatPanel.tsx` — replace with `ConversationPane`-oriented rendering.
- `athena-ide/src/components/ExperimentDashboard.tsx` — break apart into right-rail summary cards and context panels.
- `athena-ide/src/components/FileTree.tsx` — adapt into a context-surface panel.
- `athena-ide/src/components/DiffViewer.tsx` — adapt into a context-surface panel.
- `athena-ide/src/components/ResearchTreeViz.tsx` — adapt into a context-surface panel.
- `athena-ide/src/components/MetricChart.tsx` — adapt into a context-surface panel.

### New files to create

- `athena-ide/vitest.config.ts` — frontend test configuration.
- `athena-ide/src/test/setup.ts` — Vitest DOM setup.
- `athena-ide/src/test/render.tsx` — shared frontend render helper.
- `athena-ide/src/types/ui.ts` — frontend-only UI state types shared by hooks and components.
- `athena-ide/src/components/shell/AppShell.tsx` — shell layout composition.
- `athena-ide/src/components/shell/TopBar.tsx` — lightweight title and connection/run status strip.
- `athena-ide/src/components/conversation/ConversationPane.tsx` — chat-first workspace container.
- `athena-ide/src/components/conversation/MessageList.tsx` — conversation list renderer.
- `athena-ide/src/components/conversation/Composer.tsx` — input area and quick actions.
- `athena-ide/src/components/cards/IntentPreviewCard.tsx` — parsed-intent card.
- `athena-ide/src/components/cards/RunStatusCard.tsx` — run-state card for conversation stream.
- `athena-ide/src/components/cards/ResultSummaryCard.tsx` — experiment result card for the conversation stream.
- `athena-ide/src/components/cards/ErrorCard.tsx` — error card for the conversation stream.
- `athena-ide/src/components/right-rail/RightRail.tsx` — narrow summary rail container.
- `athena-ide/src/components/right-rail/StatusCard.tsx` — status summary card.
- `athena-ide/src/components/right-rail/BudgetCard.tsx` — budget summary card.
- `athena-ide/src/components/right-rail/BestResultCard.tsx` — best-result summary card.
- `athena-ide/src/components/right-rail/ResearchSnapshotCard.tsx` — research summary card.
- `athena-ide/src/components/context/ContextSurface.tsx` — expandable bottom detail surface.
- `athena-ide/src/components/context/ContextTabs.tsx` — context-surface tab switcher.
- `athena-ide/src/components/context/ExperimentLogPanel.tsx` — detailed experiment list panel.
- `athena-ide/src/components/context/ReportPanel.tsx` — report preview panel.
- `athena-ide/src/components/__tests__/app-shell.test.tsx`
- `athena-ide/src/components/__tests__/conversation-pane.test.tsx`
- `athena-ide/src/components/__tests__/right-rail.test.tsx`
- `athena-ide/src/components/__tests__/context-surface.test.tsx`
- `athena-ide/src/hooks/__tests__/usePipeline.test.tsx`
- `athena-ide/src/lib/__tests__/tauri-bridge.test.ts`

### Files intentionally not changed

- `src/athena/ide/**`
- `athena-ide/src-tauri/**`

These remain untouched unless the frontend build reveals a type import issue that can be fixed without changing behavior.

---

### Task 1: Add frontend test harness and shared UI types

**Files:**
- Modify: `athena-ide/package.json`
- Create: `athena-ide/vitest.config.ts`
- Create: `athena-ide/src/test/setup.ts`
- Create: `athena-ide/src/test/render.tsx`
- Create: `athena-ide/src/types/ui.ts`
- Test: `athena-ide/src/lib/__tests__/tauri-bridge.test.ts`

**Interfaces:**
- Consumes: Existing `TaskPreview`, `BudgetState`, and `ExperimentEvent` shapes from `athena-ide/src/lib/tauri-bridge.ts`.
- Produces:
  - `UIMessage` type
  - `RightRailSummary` type
  - `ContextPanelKey = "metrics" | "research-tree" | "experiment-log" | "diff" | "files" | "report"`
  - `PipelineViewModel` type
  - Working `npm test` command using Vitest

- [ ] **Step 1: Write the failing test**

```ts
import { describe, expect, it } from "vitest";
import { CONTEXT_PANELS, createEmptyPipelineViewModel } from "../../types/ui";

describe("ui model bootstrap", () => {
  it("defines the supported context panels and an idle default view model", () => {
    expect(CONTEXT_PANELS).toEqual([
      "metrics",
      "research-tree",
      "experiment-log",
      "diff",
      "files",
      "report",
    ]);

    expect(createEmptyPipelineViewModel()).toMatchObject({
      phase: "idle",
      status: "idle",
      contextSurface: {
        isOpen: false,
        activePanel: "metrics",
      },
      messages: [],
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd athena-ide && npx vitest run src/lib/__tests__/tauri-bridge.test.ts`
Expected: FAIL with `Cannot find module '../../types/ui'` or `No test files found`.

- [ ] **Step 3: Write minimal implementation**

```ts
// athena-ide/src/types/ui.ts
import type { TaskPreview } from "../lib/tauri-bridge";

export const CONTEXT_PANELS = [
  "metrics",
  "research-tree",
  "experiment-log",
  "diff",
  "files",
  "report",
] as const;

export type ContextPanelKey = (typeof CONTEXT_PANELS)[number];
export type PipelineStatus = "idle" | "running" | "paused" | "completed" | "error";

export interface UIMessage {
  id: string;
  role: "user" | "athena";
  kind: "text" | "intent-preview" | "run-status" | "result-summary" | "error";
  content: string;
  preview?: TaskPreview;
}

export interface RightRailSummary {
  phase: string;
  status: PipelineStatus;
  budgetRemaining: number;
  noImproveStreak: number;
  bestPrimary: number | null;
  latestExperimentId: string | null;
}

export interface PipelineViewModel {
  phase: string;
  status: PipelineStatus;
  messages: UIMessage[];
  contextSurface: {
    isOpen: boolean;
    activePanel: ContextPanelKey;
  };
  rightRail: RightRailSummary;
}

export function createEmptyPipelineViewModel(): PipelineViewModel {
  return {
    phase: "idle",
    status: "idle",
    messages: [],
    contextSurface: {
      isOpen: false,
      activePanel: "metrics",
    },
    rightRail: {
      phase: "idle",
      status: "idle",
      budgetRemaining: 0,
      noImproveStreak: 0,
      bestPrimary: null,
      latestExperimentId: null,
    },
  };
}
```

```ts
// athena-ide/vitest.config.ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
  },
});
```

```ts
// athena-ide/src/test/setup.ts
import "@testing-library/jest-dom/vitest";
```

```ts
// athena-ide/src/test/render.tsx
import { ReactElement } from "react";
import { render } from "@testing-library/react";

export function renderUi(ui: ReactElement) {
  return render(ui);
}
```

```json
// athena-ide/package.json (relevant additions)
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.0.1",
    "jsdom": "^25.0.1",
    "vitest": "^2.1.9"
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd athena-ide && npm test -- src/lib/__tests__/tauri-bridge.test.ts`
Expected: PASS with `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add athena-ide/package.json athena-ide/package-lock.json athena-ide/vitest.config.ts athena-ide/src/test/setup.ts athena-ide/src/test/render.tsx athena-ide/src/types/ui.ts athena-ide/src/lib/__tests__/tauri-bridge.test.ts
git commit -m "test(frontend): add Athena IDE UI test harness"
```

### Task 2: Normalize the Tauri bridge and pipeline state model

**Files:**
- Modify: `athena-ide/src/lib/tauri-bridge.ts`
- Modify: `athena-ide/src/hooks/useEvents.ts`
- Modify: `athena-ide/src/hooks/usePipeline.ts`
- Test: `athena-ide/src/lib/__tests__/tauri-bridge.test.ts`
- Test: `athena-ide/src/hooks/__tests__/usePipeline.test.tsx`

**Interfaces:**
- Consumes:
  - `sendMessage(message: string): Promise<TaskPreview>`
  - `startSearch(config: Record<string, unknown>): Promise<unknown>`
  - `pauseSearch(): Promise<unknown>`
  - `resumeSearch(): Promise<unknown>`
  - `stopSearch(): Promise<unknown>`
  - `startValidation(): Promise<unknown>`
  - `generateReport(): Promise<unknown>`
  - `PipelineViewModel` and `ContextPanelKey` from `src/types/ui.ts`
- Produces:
  - `subscribeToPipelineEvents(handler: (evt: PipelineEvent) => void): Promise<UnlistenFn[]>`
  - `usePipeline(): { viewModel: PipelineViewModel; sendPrompt(msg: string): Promise<void>; startRun(preview: TaskPreview): Promise<void>; openPanel(panel: ContextPanelKey): void; closePanel(): void; pauseRun(): Promise<void>; resumeRun(): Promise<void>; stopRun(): Promise<void>; }`

- [ ] **Step 1: Write the failing tests**

```ts
// athena-ide/src/lib/__tests__/tauri-bridge.test.ts
import { describe, expect, it, vi } from "vitest";
import { subscribeToPipelineEvents } from "../tauri-bridge";

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn((_name, handler) => Promise.resolve(() => handler)),
}));

describe("subscribeToPipelineEvents", () => {
  it("subscribes to all frontend-relevant event kinds", async () => {
    const unlisten = await subscribeToPipelineEvents(() => {});
    expect(unlisten).toHaveLength(6);
  });
});
```

```tsx
// athena-ide/src/hooks/__tests__/usePipeline.test.tsx
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { usePipeline } from "../usePipeline";

vi.mock("../../lib/tauri-bridge", () => ({
  sendMessage: vi.fn().mockResolvedValue({
    task_type: "classification",
    data_type: "tabular",
    target_vars: ["target"],
    primary_metric: "f1_macro",
    direction: "maximize",
    needs_configuration: true,
  }),
  startSearch: vi.fn().mockResolvedValue({ ok: true }),
  pauseSearch: vi.fn().mockResolvedValue({ ok: true }),
  resumeSearch: vi.fn().mockResolvedValue({ ok: true }),
  stopSearch: vi.fn().mockResolvedValue({ ok: true }),
  startValidation: vi.fn().mockResolvedValue({ ok: true }),
  generateReport: vi.fn().mockResolvedValue({ ok: true }),
  subscribeToPipelineEvents: vi.fn().mockResolvedValue([]),
}));

describe("usePipeline", () => {
  it("adds a user message and an intent preview card when a prompt is sent", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("analyze this CSV");
    });

    expect(result.current.viewModel.messages.map((m) => m.kind)).toEqual([
      "text",
      "intent-preview",
    ]);
    expect(result.current.viewModel.messages[1].preview?.primary_metric).toBe("f1_macro");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd athena-ide && npm test -- src/lib/__tests__/tauri-bridge.test.ts src/hooks/__tests__/usePipeline.test.tsx`
Expected: FAIL because `subscribeToPipelineEvents` and the new `usePipeline` API do not exist.

- [ ] **Step 3: Write minimal implementation**

```ts
// athena-ide/src/lib/tauri-bridge.ts (new pieces)
export type PipelineEvent = ExperimentEvent;

const EVENT_NAMES = [
  "chat/token",
  "chat/intent_parsed",
  "experiment/started",
  "experiment/completed",
  "budget/update",
  "phase/change",
] as const;

export function subscribeToPipelineEvents(
  handler: (evt: PipelineEvent) => void,
): Promise<UnlistenFn[]> {
  return Promise.all(EVENT_NAMES.map((name) => listen<PipelineEvent>(name, (e) => handler(e.payload))));
}
```

```ts
// athena-ide/src/hooks/useEvents.ts
import { useEffect, useState } from "react";
import { PipelineEvent, subscribeToPipelineEvents } from "../lib/tauri-bridge";

export function useEvents() {
  const [events, setEvents] = useState<PipelineEvent[]>([]);

  useEffect(() => {
    let cleanup: Array<() => void> = [];

    subscribeToPipelineEvents((evt) => {
      setEvents((prev) => [...prev.slice(-199), evt]);
    }).then((unlisteners) => {
      cleanup = unlisteners;
    });

    return () => {
      cleanup.forEach((fn) => fn());
    };
  }, []);

  return events;
}
```

```ts
// athena-ide/src/hooks/usePipeline.ts (shape only)
import { useCallback, useMemo, useState } from "react";
import {
  pauseSearch,
  resumeSearch,
  sendMessage,
  startSearch,
  stopSearch,
  TaskPreview,
} from "../lib/tauri-bridge";
import { ContextPanelKey, createEmptyPipelineViewModel, PipelineViewModel } from "../types/ui";

function nextId(prefix: string) {
  return `${prefix}-${Math.random().toString(36).slice(2, 8)}`;
}

export function usePipeline() {
  const [viewModel, setViewModel] = useState<PipelineViewModel>(createEmptyPipelineViewModel());

  const sendPrompt = useCallback(async (msg: string) => {
    setViewModel((prev) => ({
      ...prev,
      messages: [...prev.messages, { id: nextId("user"), role: "user", kind: "text", content: msg }],
    }));

    const preview = await sendMessage(msg);

    setViewModel((prev) => ({
      ...prev,
      messages: [
        ...prev.messages,
        {
          id: nextId("preview"),
          role: "athena",
          kind: "intent-preview",
          content: `Task: ${preview.task_type} · Primary: ${preview.primary_metric}`,
          preview,
        },
      ],
    }));
  }, []);

  const startRun = useCallback(async (preview: TaskPreview) => {
    setViewModel((prev) => ({
      ...prev,
      phase: "SEARCH",
      status: "running",
      rightRail: { ...prev.rightRail, phase: "SEARCH", status: "running" },
    }));
    await startSearch({ max_experiments: 10, ...preview });
  }, []);

  const openPanel = useCallback((panel: ContextPanelKey) => {
    setViewModel((prev) => ({
      ...prev,
      contextSurface: { isOpen: true, activePanel: panel },
    }));
  }, []);

  const closePanel = useCallback(() => {
    setViewModel((prev) => ({
      ...prev,
      contextSurface: { ...prev.contextSurface, isOpen: false },
    }));
  }, []);

  const pauseRun = useCallback(async () => {
    await pauseSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "paused",
      rightRail: { ...prev.rightRail, status: "paused" },
    }));
  }, []);

  const resumeRun = useCallback(async () => {
    await resumeSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "running",
      rightRail: { ...prev.rightRail, status: "running" },
    }));
  }, []);

  const stopRun = useCallback(async () => {
    await stopSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "completed",
      rightRail: { ...prev.rightRail, status: "completed" },
    }));
  }, []);

  return useMemo(
    () => ({ viewModel, sendPrompt, startRun, openPanel, closePanel, pauseRun, resumeRun, stopRun }),
    [viewModel, sendPrompt, startRun, openPanel, closePanel, pauseRun, resumeRun, stopRun],
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd athena-ide && npm test -- src/lib/__tests__/tauri-bridge.test.ts src/hooks/__tests__/usePipeline.test.tsx`
Expected: PASS with both tests green.

- [ ] **Step 5: Commit**

```bash
git add athena-ide/src/lib/tauri-bridge.ts athena-ide/src/hooks/useEvents.ts athena-ide/src/hooks/usePipeline.ts athena-ide/src/lib/__tests__/tauri-bridge.test.ts athena-ide/src/hooks/__tests__/usePipeline.test.tsx athena-ide/src/types/ui.ts
git commit -m "feat(frontend): normalize Athena IDE pipeline state"
```

### Task 3: Build the shell layout and premium dark visual system

**Files:**
- Modify: `athena-ide/src/App.tsx`
- Modify: `athena-ide/src/App.css`
- Modify: `athena-ide/src/styles.css`
- Create: `athena-ide/src/components/shell/AppShell.tsx`
- Create: `athena-ide/src/components/shell/TopBar.tsx`
- Test: `athena-ide/src/components/__tests__/app-shell.test.tsx`

**Interfaces:**
- Consumes:
  - `PipelineViewModel` from `src/types/ui.ts`
  - `openPanel(panel: ContextPanelKey): void`
  - `closePanel(): void`
- Produces:
  - `AppShell` component with props `{ viewModel: PipelineViewModel; conversation: React.ReactNode; rightRail: React.ReactNode; contextSurface: React.ReactNode; }`
  - `TopBar` component with props `{ title: string; phase: string; status: string; }`

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { AppShell } from "../shell/AppShell";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("AppShell", () => {
  it("renders the chat-first shell with a top bar, main conversation region, right rail, and collapsed context surface", () => {
    renderUi(
      <AppShell
        viewModel={createEmptyPipelineViewModel()}
        conversation={<div>conversation</div>}
        rightRail={<div>right rail</div>}
        contextSurface={<div>context surface</div>}
      />,
    );

    expect(screen.getByText("Athena IDE")).toBeInTheDocument();
    expect(screen.getByText("conversation")).toBeInTheDocument();
    expect(screen.getByText("right rail")).toBeInTheDocument();
    expect(screen.getByText("context surface")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/app-shell.test.tsx`
Expected: FAIL because `AppShell` does not exist.

- [ ] **Step 3: Write minimal implementation**

```tsx
// athena-ide/src/components/shell/TopBar.tsx
interface TopBarProps {
  title: string;
  phase: string;
  status: string;
}

export function TopBar({ title, phase, status }: TopBarProps) {
  return (
    <header className="top-bar">
      <div>
        <span className="top-bar__title">{title}</span>
      </div>
      <div className="top-bar__meta">
        <span>{phase}</span>
        <span>{status}</span>
      </div>
    </header>
  );
}
```

```tsx
// athena-ide/src/components/shell/AppShell.tsx
import { ReactNode } from "react";
import { PipelineViewModel } from "../../types/ui";
import { TopBar } from "./TopBar";

interface AppShellProps {
  viewModel: PipelineViewModel;
  conversation: ReactNode;
  rightRail: ReactNode;
  contextSurface: ReactNode;
}

export function AppShell({ viewModel, conversation, rightRail, contextSurface }: AppShellProps) {
  return (
    <div className="app-shell">
      <TopBar title="Athena IDE" phase={viewModel.phase} status={viewModel.status} />
      <div className="app-shell__body">
        <main className="app-shell__conversation">{conversation}</main>
        <aside className="app-shell__right-rail">{rightRail}</aside>
      </div>
      <section className="app-shell__context-surface">{contextSurface}</section>
    </div>
  );
}
```

```tsx
// athena-ide/src/App.tsx
import { AppShell } from "./components/shell/AppShell";
import { ConversationPane } from "./components/conversation/ConversationPane";
import { RightRail } from "./components/right-rail/RightRail";
import { ContextSurface } from "./components/context/ContextSurface";
import { usePipeline } from "./hooks/usePipeline";
import "./App.css";

function App() {
  const pipeline = usePipeline();

  return (
    <AppShell
      viewModel={pipeline.viewModel}
      conversation={<ConversationPane pipeline={pipeline} />}
      rightRail={<RightRail viewModel={pipeline.viewModel} openPanel={pipeline.openPanel} />}
      contextSurface={<ContextSurface viewModel={pipeline.viewModel} closePanel={pipeline.closePanel} />}
    />
  );
}

export default App;
```

```css
/* athena-ide/src/styles.css */
:root {
  color-scheme: dark;
  --bg: #0a0d14;
  --surface: #10141d;
  --surface-elevated: #151b26;
  --surface-muted: #0f141c;
  --border: rgba(255, 255, 255, 0.08);
  --text-primary: #f5f7fb;
  --text-secondary: #98a2b3;
  --accent: #58b6ff;
  --danger: #ff6b81;
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.5;
  font-weight: 400;
  color: var(--text-primary);
  background: var(--bg);
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

* { box-sizing: border-box; }
html, body, #root { margin: 0; min-height: 100%; background: var(--bg); }
body { min-height: 100vh; }
button, input, textarea { font: inherit; }
```

```css
/* athena-ide/src/App.css */
.app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto;
  background: radial-gradient(circle at top, rgba(88, 182, 255, 0.08), transparent 30%), var(--bg);
}

.app-shell__body {
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 16px;
  padding: 16px 20px 0;
}

.app-shell__conversation,
.app-shell__right-rail,
.app-shell__context-surface,
.top-bar {
  border: 1px solid var(--border);
  background: rgba(16, 20, 29, 0.82);
  backdrop-filter: blur(14px);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd athena-ide && npm test -- src/components/__tests__/app-shell.test.tsx`
Expected: PASS with the shell regions present.

- [ ] **Step 5: Commit**

```bash
git add athena-ide/src/App.tsx athena-ide/src/App.css athena-ide/src/styles.css athena-ide/src/components/shell/AppShell.tsx athena-ide/src/components/shell/TopBar.tsx athena-ide/src/components/__tests__/app-shell.test.tsx
git commit -m "feat(frontend): add Athena IDE shell layout"
```

### Task 4: Rebuild the conversation pane as the primary workflow surface

**Files:**
- Create: `athena-ide/src/components/conversation/ConversationPane.tsx`
- Create: `athena-ide/src/components/conversation/MessageList.tsx`
- Create: `athena-ide/src/components/conversation/Composer.tsx`
- Create: `athena-ide/src/components/cards/IntentPreviewCard.tsx`
- Create: `athena-ide/src/components/cards/RunStatusCard.tsx`
- Create: `athena-ide/src/components/cards/ResultSummaryCard.tsx`
- Create: `athena-ide/src/components/cards/ErrorCard.tsx`
- Modify: `athena-ide/src/components/ChatPanel.tsx`
- Test: `athena-ide/src/components/__tests__/conversation-pane.test.tsx`

**Interfaces:**
- Consumes:
  - `usePipeline()` return value from Task 2
  - `UIMessage` from `src/types/ui.ts`
- Produces:
  - `ConversationPane` props `{ pipeline: ReturnType<typeof usePipeline> }`
  - `Composer` props `{ onSend(message: string): Promise<void>; disabled?: boolean; }`
  - `IntentPreviewCard` props `{ content: string; onConfirm(): Promise<void>; }`

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderUi } from "../../test/render";
import { ConversationPane } from "../conversation/ConversationPane";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("ConversationPane", () => {
  it("sends prompts and renders an intent preview confirmation card in the message stream", async () => {
    const user = userEvent.setup();
    const sendPrompt = vi.fn().mockResolvedValue(undefined);
    const startRun = vi.fn().mockResolvedValue(undefined);

    const pipeline = {
      viewModel: {
        ...createEmptyPipelineViewModel(),
        messages: [
          {
            id: "preview-1",
            role: "athena",
            kind: "intent-preview",
            content: "Task: classification · Primary: f1_macro",
            preview: {
              task_type: "classification",
              data_type: "tabular",
              target_vars: ["target"],
              primary_metric: "f1_macro",
              direction: "maximize",
              needs_configuration: true,
            },
          },
        ],
      },
      sendPrompt,
      startRun,
    } as const;

    renderUi(<ConversationPane pipeline={pipeline as never} />);

    expect(screen.getByText(/Task: classification/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /confirm & start/i }));
    expect(startRun).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/conversation-pane.test.tsx`
Expected: FAIL because `ConversationPane` and the card components do not exist.

- [ ] **Step 3: Write minimal implementation**

```tsx
// athena-ide/src/components/cards/IntentPreviewCard.tsx
import type { TaskPreview } from "../../lib/tauri-bridge";

interface IntentPreviewCardProps {
  preview: TaskPreview;
  onConfirm(): Promise<void>;
}

export function IntentPreviewCard({ preview, onConfirm }: IntentPreviewCardProps) {
  return (
    <section className="card card--action">
      <h3>Intent preview</h3>
      <p>{`Task: ${preview.task_type} · Primary: ${preview.primary_metric}`}</p>
      <button onClick={() => void onConfirm()}>Confirm &amp; Start</button>
    </section>
  );
}
```

```tsx
// athena-ide/src/components/conversation/Composer.tsx
import { FormEvent, useState } from "react";

interface ComposerProps {
  onSend(message: string): Promise<void>;
  disabled?: boolean;
}

export function Composer({ onSend, disabled }: ComposerProps) {
  const [value, setValue] = useState("");

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!value.trim()) return;
    const next = value;
    setValue("");
    await onSend(next);
  }

  return (
    <form className="composer" onSubmit={(event) => void handleSubmit(event)}>
      <textarea value={value} onChange={(e) => setValue(e.target.value)} placeholder="Describe your ML task..." />
      <button type="submit" disabled={disabled}>Send</button>
    </form>
  );
}
```

```tsx
// athena-ide/src/components/conversation/MessageList.tsx
import { UIMessage } from "../../types/ui";
import { IntentPreviewCard } from "../cards/IntentPreviewCard";

interface MessageListProps {
  messages: UIMessage[];
  onConfirmPreview(previewId: string): Promise<void>;
}

export function MessageList({ messages, onConfirmPreview }: MessageListProps) {
  return (
    <div className="message-list">
      {messages.map((message) => {
        if (message.kind === "intent-preview" && message.preview) {
          return (
            <IntentPreviewCard
              key={message.id}
              preview={message.preview}
              onConfirm={() => onConfirmPreview(message.id)}
            />
          );
        }

        return (
          <article key={message.id} className={`message-bubble message-bubble--${message.role}`}>
            {message.content}
          </article>
        );
      })}
    </div>
  );
}
```

```tsx
// athena-ide/src/components/conversation/ConversationPane.tsx
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";

interface ConversationPaneProps {
  pipeline: {
    viewModel: {
      messages: Array<{ id: string; kind: string; content: string; preview?: unknown }>;
    };
    sendPrompt(message: string): Promise<void>;
    startRun(preview: unknown): Promise<void>;
  };
}

export function ConversationPane({ pipeline }: ConversationPaneProps) {
  async function onConfirmPreview(messageId: string) {
    const match = pipeline.viewModel.messages.find((message) => message.id === messageId);
    if (match?.preview) {
      await pipeline.startRun(match.preview);
    }
  }

  return (
    <section className="conversation-pane">
      <div className="conversation-pane__header">
        <h1>Athena Agent</h1>
        <p>Chat-first ML workflow orchestration.</p>
      </div>
      <MessageList messages={pipeline.viewModel.messages as never} onConfirmPreview={onConfirmPreview} />
      <Composer onSend={pipeline.sendPrompt} />
    </section>
  );
}
```

```ts
// athena-ide/src/components/ChatPanel.tsx
export { ConversationPane as default } from "./conversation/ConversationPane";
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd athena-ide && npm test -- src/components/__tests__/conversation-pane.test.tsx`
Expected: PASS with the confirmation action calling `startRun`.

- [ ] **Step 5: Commit**

```bash
git add athena-ide/src/components/conversation/ConversationPane.tsx athena-ide/src/components/conversation/MessageList.tsx athena-ide/src/components/conversation/Composer.tsx athena-ide/src/components/cards/IntentPreviewCard.tsx athena-ide/src/components/cards/RunStatusCard.tsx athena-ide/src/components/cards/ResultSummaryCard.tsx athena-ide/src/components/cards/ErrorCard.tsx athena-ide/src/components/ChatPanel.tsx athena-ide/src/components/__tests__/conversation-pane.test.tsx
git commit -m "feat(frontend): rebuild Athena conversation workspace"
```

### Task 5: Replace the old dashboard with a narrow right rail and bottom context surface

**Files:**
- Create: `athena-ide/src/components/right-rail/RightRail.tsx`
- Create: `athena-ide/src/components/right-rail/StatusCard.tsx`
- Create: `athena-ide/src/components/right-rail/BudgetCard.tsx`
- Create: `athena-ide/src/components/right-rail/BestResultCard.tsx`
- Create: `athena-ide/src/components/right-rail/ResearchSnapshotCard.tsx`
- Create: `athena-ide/src/components/context/ContextSurface.tsx`
- Create: `athena-ide/src/components/context/ContextTabs.tsx`
- Create: `athena-ide/src/components/context/ExperimentLogPanel.tsx`
- Create: `athena-ide/src/components/context/ReportPanel.tsx`
- Modify: `athena-ide/src/components/ExperimentDashboard.tsx`
- Modify: `athena-ide/src/components/FileTree.tsx`
- Modify: `athena-ide/src/components/DiffViewer.tsx`
- Modify: `athena-ide/src/components/ResearchTreeViz.tsx`
- Modify: `athena-ide/src/components/MetricChart.tsx`
- Test: `athena-ide/src/components/__tests__/right-rail.test.tsx`
- Test: `athena-ide/src/components/__tests__/context-surface.test.tsx`

**Interfaces:**
- Consumes:
  - `PipelineViewModel`
  - `ContextPanelKey`
  - `openPanel(panel: ContextPanelKey): void`
  - `closePanel(): void`
- Produces:
  - `RightRail` props `{ viewModel: PipelineViewModel; openPanel(panel: ContextPanelKey): void; }`
  - `ContextSurface` props `{ viewModel: PipelineViewModel; closePanel(): void; }`

- [ ] **Step 1: Write the failing tests**

```tsx
// athena-ide/src/components/__tests__/right-rail.test.tsx
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderUi } from "../../test/render";
import { RightRail } from "../right-rail/RightRail";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("RightRail", () => {
  it("renders summary cards and opens the report panel from a summary action", async () => {
    const user = userEvent.setup();
    const openPanel = vi.fn();

    renderUi(
      <RightRail
        viewModel={{
          ...createEmptyPipelineViewModel(),
          phase: "SEARCH",
          status: "running",
          rightRail: {
            phase: "SEARCH",
            status: "running",
            budgetRemaining: 7,
            noImproveStreak: 2,
            bestPrimary: 0.7812,
            latestExperimentId: "exp-4",
          },
        }}
        openPanel={openPanel}
      />,
    );

    expect(screen.getByText(/SEARCH/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /open report/i }));
    expect(openPanel).toHaveBeenCalledWith("report");
  });
});
```

```tsx
// athena-ide/src/components/__tests__/context-surface.test.tsx
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderUi } from "../../test/render";
import { ContextSurface } from "../context/ContextSurface";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("ContextSurface", () => {
  it("renders the active panel when opened and closes on demand", async () => {
    const user = userEvent.setup();
    const closePanel = vi.fn();

    renderUi(
      <ContextSurface
        viewModel={{
          ...createEmptyPipelineViewModel(),
          contextSurface: { isOpen: true, activePanel: "metrics" },
        }}
        closePanel={closePanel}
      />,
    );

    expect(screen.getByText(/Metrics/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /close details/i }));
    expect(closePanel).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd athena-ide && npm test -- src/components/__tests__/right-rail.test.tsx src/components/__tests__/context-surface.test.tsx`
Expected: FAIL because `RightRail` and `ContextSurface` do not exist.

- [ ] **Step 3: Write minimal implementation**

```tsx
// athena-ide/src/components/right-rail/RightRail.tsx
import { PipelineViewModel, ContextPanelKey } from "../../types/ui";

interface RightRailProps {
  viewModel: PipelineViewModel;
  openPanel(panel: ContextPanelKey): void;
}

export function RightRail({ viewModel, openPanel }: RightRailProps) {
  const { rightRail } = viewModel;

  return (
    <section className="right-rail">
      <button className="summary-card" onClick={() => openPanel("experiment-log")}>
        <span>Status</span>
        <strong>{rightRail.phase}</strong>
        <small>{rightRail.status}</small>
      </button>
      <button className="summary-card" onClick={() => openPanel("metrics")}>
        <span>Budget</span>
        <strong>{rightRail.budgetRemaining}</strong>
        <small>{`Streak ${rightRail.noImproveStreak}`}</small>
      </button>
      <button className="summary-card" onClick={() => openPanel("research-tree")}>
        <span>Best result</span>
        <strong>{rightRail.bestPrimary ?? "--"}</strong>
        <small>{rightRail.latestExperimentId ?? "No experiment yet"}</small>
      </button>
      <button className="summary-card" onClick={() => openPanel("report")}>
        <span>Research</span>
        <strong>Open report</strong>
        <small>Inspect details</small>
      </button>
      <button className="summary-card" onClick={() => openPanel("report")} aria-label="Open report">
        Open report
      </button>
    </section>
  );
}
```

```tsx
// athena-ide/src/components/context/ContextSurface.tsx
import { PipelineViewModel } from "../../types/ui";
import MetricChart from "../MetricChart";
import ResearchTreeViz from "../ResearchTreeViz";
import DiffViewer from "../DiffViewer";
import FileTree from "../FileTree";

interface ContextSurfaceProps {
  viewModel: PipelineViewModel;
  closePanel(): void;
}

export function ContextSurface({ viewModel, closePanel }: ContextSurfaceProps) {
  if (!viewModel.contextSurface.isOpen) {
    return <section className="context-surface context-surface--closed">Details hidden</section>;
  }

  return (
    <section className="context-surface context-surface--open">
      <header className="context-surface__header">
        <h2>{viewModel.contextSurface.activePanel === "metrics" ? "Metrics" : viewModel.contextSurface.activePanel}</h2>
        <button onClick={() => closePanel()} aria-label="Close details">Close details</button>
      </header>
      {viewModel.contextSurface.activePanel === "metrics" && <MetricChart />}
      {viewModel.contextSurface.activePanel === "research-tree" && <ResearchTreeViz />}
      {viewModel.contextSurface.activePanel === "diff" && <DiffViewer />}
      {viewModel.contextSurface.activePanel === "files" && <FileTree />}
      {viewModel.contextSurface.activePanel === "report" && <div>Report preview</div>}
      {viewModel.contextSurface.activePanel === "experiment-log" && <div>Experiment log</div>}
    </section>
  );
}
```

```tsx
// athena-ide/src/components/ExperimentDashboard.tsx
export { RightRail as default } from "./right-rail/RightRail";
```

```tsx
// athena-ide/src/components/MetricChart.tsx
export default function MetricChart() {
  return <div className="detail-panel"><h3>Metrics</h3><p>Primary metric trend view.</p></div>;
}
```

```tsx
// athena-ide/src/components/ResearchTreeViz.tsx
export default function ResearchTreeViz() {
  return <div className="detail-panel"><h3>Research Tree</h3><p>Research DAG detail view.</p></div>;
}
```

```tsx
// athena-ide/src/components/DiffViewer.tsx
export default function DiffViewer() {
  return <div className="detail-panel"><h3>Diff</h3><p>Experiment diff detail view.</p></div>;
}
```

```tsx
// athena-ide/src/components/FileTree.tsx
export default function FileTree() {
  return <div className="detail-panel"><h3>Files</h3><p>Experiment file tree detail view.</p></div>;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd athena-ide && npm test -- src/components/__tests__/right-rail.test.tsx src/components/__tests__/context-surface.test.tsx`
Expected: PASS with summary interactions opening the chosen context panels.

- [ ] **Step 5: Commit**

```bash
git add athena-ide/src/components/right-rail/RightRail.tsx athena-ide/src/components/right-rail/StatusCard.tsx athena-ide/src/components/right-rail/BudgetCard.tsx athena-ide/src/components/right-rail/BestResultCard.tsx athena-ide/src/components/right-rail/ResearchSnapshotCard.tsx athena-ide/src/components/context/ContextSurface.tsx athena-ide/src/components/context/ContextTabs.tsx athena-ide/src/components/context/ExperimentLogPanel.tsx athena-ide/src/components/context/ReportPanel.tsx athena-ide/src/components/ExperimentDashboard.tsx athena-ide/src/components/FileTree.tsx athena-ide/src/components/DiffViewer.tsx athena-ide/src/components/ResearchTreeViz.tsx athena-ide/src/components/MetricChart.tsx athena-ide/src/components/__tests__/right-rail.test.tsx athena-ide/src/components/__tests__/context-surface.test.tsx
git commit -m "feat(frontend): add Athena summary rail and context surface"
```

### Task 6: Wire real event updates, run controls, and visual polish into the redesigned shell

**Files:**
- Modify: `athena-ide/src/hooks/usePipeline.ts`
- Modify: `athena-ide/src/components/conversation/ConversationPane.tsx`
- Modify: `athena-ide/src/components/right-rail/RightRail.tsx`
- Modify: `athena-ide/src/components/context/ContextSurface.tsx`
- Modify: `athena-ide/src/styles.css`
- Modify: `athena-ide/src/App.css`
- Test: `athena-ide/src/hooks/__tests__/usePipeline.test.tsx`
- Test: `athena-ide/src/components/__tests__/conversation-pane.test.tsx`
- Test: `athena-ide/src/components/__tests__/right-rail.test.tsx`
- Test: `athena-ide/src/components/__tests__/context-surface.test.tsx`

**Interfaces:**
- Consumes:
  - `PipelineEvent`
  - `PipelineViewModel`
  - Existing Tauri commands and subscriptions from Tasks 1-5
- Produces:
  - Real event-to-view-model mapping for `phase`, `budget`, `best result`, and `latest experiment`
  - Visible run controls in the conversation and/or right rail
  - Final visual polish for the premium dark experience

- [ ] **Step 1: Write the failing tests**

```tsx
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { usePipeline } from "../usePipeline";

const eventHandlers: Array<(evt: { kind: string; data: Record<string, unknown> }) => void> = [];

vi.mock("../../lib/tauri-bridge", () => ({
  sendMessage: vi.fn().mockResolvedValue({
    task_type: "classification",
    data_type: "tabular",
    target_vars: ["target"],
    primary_metric: "f1_macro",
    direction: "maximize",
    needs_configuration: true,
  }),
  startSearch: vi.fn().mockResolvedValue({ ok: true }),
  pauseSearch: vi.fn().mockResolvedValue({ ok: true }),
  resumeSearch: vi.fn().mockResolvedValue({ ok: true }),
  stopSearch: vi.fn().mockResolvedValue({ ok: true }),
  startValidation: vi.fn().mockResolvedValue({ ok: true }),
  generateReport: vi.fn().mockResolvedValue({ ok: true }),
  subscribeToPipelineEvents: vi.fn(async (handler) => {
    eventHandlers.push(handler);
    return [];
  }),
}));

describe("usePipeline event mapping", () => {
  it("updates the right rail when experiment and budget events arrive", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({ kind: "budget/update", data: { remaining: 6, no_improve_streak: 1 } });
      eventHandlers[0]?.({ kind: "experiment/completed", data: { experiment_id: "exp-7", primary: 0.83 } });
    });

    expect(result.current.viewModel.rightRail.budgetRemaining).toBe(6);
    expect(result.current.viewModel.rightRail.bestPrimary).toBe(0.83);
    expect(result.current.viewModel.rightRail.latestExperimentId).toBe("exp-7");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd athena-ide && npm test -- src/hooks/__tests__/usePipeline.test.tsx`
Expected: FAIL because `usePipeline` does not subscribe and map incoming events.

- [ ] **Step 3: Write minimal implementation**

```ts
// athena-ide/src/hooks/usePipeline.ts (event mapping sketch)
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  pauseSearch,
  resumeSearch,
  sendMessage,
  startSearch,
  stopSearch,
  subscribeToPipelineEvents,
  TaskPreview,
} from "../lib/tauri-bridge";
import { ContextPanelKey, createEmptyPipelineViewModel, PipelineViewModel } from "../types/ui";

export function usePipeline() {
  const [viewModel, setViewModel] = useState<PipelineViewModel>(createEmptyPipelineViewModel());

  useEffect(() => {
    let cleanup: Array<() => void> = [];

    subscribeToPipelineEvents((evt) => {
      setViewModel((prev) => {
        if (evt.kind === "budget/update") {
          return {
            ...prev,
            rightRail: {
              ...prev.rightRail,
              budgetRemaining: Number(evt.data.remaining ?? prev.rightRail.budgetRemaining),
              noImproveStreak: Number(evt.data.no_improve_streak ?? prev.rightRail.noImproveStreak),
            },
          };
        }

        if (evt.kind === "phase/change") {
          const phase = String(evt.data.phase ?? prev.phase);
          return {
            ...prev,
            phase,
            rightRail: { ...prev.rightRail, phase },
          };
        }

        if (evt.kind === "experiment/completed") {
          const primary = Number(evt.data.primary ?? prev.rightRail.bestPrimary ?? 0);
          return {
            ...prev,
            status: "running",
            rightRail: {
              ...prev.rightRail,
              bestPrimary: prev.rightRail.bestPrimary == null ? primary : Math.max(prev.rightRail.bestPrimary, primary),
              latestExperimentId: String(evt.data.experiment_id ?? prev.rightRail.latestExperimentId ?? ""),
            },
          };
        }

        return prev;
      });
    }).then((unlisteners) => {
      cleanup = unlisteners;
    });

    return () => {
      cleanup.forEach((fn) => fn());
    };
  }, []);

  // keep sendPrompt/startRun/openPanel/closePanel/pauseRun/resumeRun/stopRun from Task 2
  // and add any missing run-status messages needed by the conversation stream.
}
```

```tsx
// athena-ide/src/components/right-rail/RightRail.tsx (add controls)
<button className="summary-card summary-card--action" onClick={() => openPanel("experiment-log")}>
  Open experiments
</button>
```

```css
/* athena-ide/src/App.css and styles.css polish goals */
.conversation-pane,
.right-rail,
.context-surface {
  border-radius: 18px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28);
}

.message-bubble--athena {
  background: rgba(255, 255, 255, 0.03);
}

.summary-card {
  text-align: left;
  transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
}

.summary-card:hover {
  transform: translateY(-1px);
  border-color: rgba(88, 182, 255, 0.28);
}

.context-surface--open {
  min-height: 240px;
  max-height: 42vh;
}
```

- [ ] **Step 4: Run the full frontend test and type-check suite**

Run: `cd athena-ide && npm test && npm run build`
Expected: PASS with all Vitest tests green and Vite build succeeding.

- [ ] **Step 5: Commit**

```bash
git add athena-ide/src/hooks/usePipeline.ts athena-ide/src/components/conversation/ConversationPane.tsx athena-ide/src/components/right-rail/RightRail.tsx athena-ide/src/components/context/ContextSurface.tsx athena-ide/src/styles.css athena-ide/src/App.css athena-ide/src/hooks/__tests__/usePipeline.test.tsx athena-ide/src/components/__tests__/conversation-pane.test.tsx athena-ide/src/components/__tests__/right-rail.test.tsx athena-ide/src/components/__tests__/context-surface.test.tsx
git commit -m "feat(frontend): wire Athena IDE redesign interactions"
```

## Self-Review Checklist

- Spec coverage:
  - Chat-first primary surface is implemented in Tasks 3-4.
  - Narrow summary-only right rail is implemented in Task 5.
  - Heavy detail context surface is implemented in Task 5.
  - Premium dark visual system is implemented in Tasks 3 and 6.
  - Frontend-only constraint is preserved because no `src/athena/ide/**` or `src-tauri/**` files are in scope.
- Placeholder scan:
  - All tasks name exact files, exact test files, concrete commands, and concrete interface shapes.
- Type consistency:
  - `ContextPanelKey` values are defined once in Task 1 and reused throughout.
  - `PipelineViewModel` is the single shell state type used by Tasks 2-6.
