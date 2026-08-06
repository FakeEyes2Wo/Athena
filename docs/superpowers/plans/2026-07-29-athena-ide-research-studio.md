# Athena IDE Research Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the generic card-based Athena IDE frontend with the approved A2.2 “narrow ink spine” research workspace while preserving the existing pipeline ViewModel, Tauri bridge, event protocol, and backend behavior.

**Architecture:** Keep `usePipeline` as the only business-state controller. Add a pure presentation mapping module, keep `AppShell` slot-based, and rebuild the masthead, session spine, conversation, ledger, and context workspace as focused presentational components. Centralize design tokens and layout CSS in the existing `styles.css` and `App.css` pattern, then validate behavior with Vitest and target desktop layouts with Playwright.

**Tech Stack:** React 18, TypeScript 5.5, Vite 5, Vitest, Testing Library, Playwright, Tauri 2, Lucide React, Fontsource Geist, Fontsource Noto Serif SC.

## Global Constraints

- Preserve `usePipeline`, `PipelineViewModel`, Tauri bridge commands, WebSocket/Tauri event names, and backend fields.
- Do not add synthetic experiment counts, metric histories, trend charts, or connection indicators not backed by the current ViewModel.
- User-visible task states are exactly `就绪`, `运行中`, `已完成`, and `需要处理`.
- Target `1440x900`; support down to `1024x720`; below that use a non-overlapping vertical fallback.
- Use A2.2 colors exactly: canvas `#F0EEE7`, paper `#F7F5EF`, ink `#1D2925`, spine `#34443E`, active spine `#465A52`, ledger `#E4E8E2`, rule `#BEC6C1`, research `#356F63`, attention `#AD5143`, marker `#D0A84A`.
- Use 4px control radii, at most 6px for message/proposal surfaces, and at most 3px for micro labels.
- Do not add panel-wide shadows, gradients, glass effects, hover lift, decorative animation, or a dark theme.
- Use Geist for UI/body, Geist Mono for numbers/code, and locally packaged Noto Serif SC only for research and section titles.
- Use `lucide-react` for send, close, and new-research icons; every icon button needs an accessible name and tooltip.
- Do not revert or overwrite unrelated working-tree changes. Before editing `App.css` or `styles.css`, inspect their current diff and integrate it intentionally.
- Design reference: `docs/superpowers/specs/2026-07-29-athena-ide-research-studio-design.md`.

---

## File Structure

### New Files

- `athena-ide/src/lib/presentation.ts`: pure mapping from pipeline status/phase to the four user states and four research stages.
- `athena-ide/src/lib/__tests__/presentation.test.ts`: exhaustive mapping tests.
- `athena-ide/src/components/shell/ResearchMasthead.tsx`: brand, research title, and one task-state label.
- `athena-ide/src/components/__tests__/session-sidebar.test.tsx`: session spine semantics and new-research action.
- `athena-ide/playwright.config.ts`: Vite web server and Chromium configuration for visual checks.
- `athena-ide/tests/visual/workbench.spec.ts`: overflow, console, and screenshot checks at target sizes.

### Modified Files

- `athena-ide/package.json`, `athena-ide/package-lock.json`: add Noto Serif SC, Lucide React, Playwright, and the `test:visual` script.
- `athena-ide/src/main.tsx`: import the local Noto Serif SC weight.
- `athena-ide/src/styles.css`: replace generic blue/card tokens with the approved research-studio tokens and global accessibility defaults.
- `athena-ide/src/App.tsx`: compose the masthead and pass narrow props/callbacks to the visual components.
- `athena-ide/src/App.css`: implement continuous workbench layout, component visuals, responsive fallback, focus, and reduced-motion rules.
- `athena-ide/src/components/shell/AppShell.tsx`: add a masthead slot and grid the context workspace across the center and ledger columns.
- `athena-ide/src/components/shell/SessionSidebar.tsx`: semantic list buttons, active marker, Lucide new action, and tooltip.
- `athena-ide/src/components/conversation/ConversationPane.tsx`: narrow props, research heading, and phase progress.
- `athena-ide/src/components/conversation/MessageList.tsx`: empty state and research-record message semantics.
- `athena-ide/src/components/conversation/Composer.tsx`: compact horizontal composer and icon send action.
- `athena-ide/src/components/cards/IntentPreviewCard.tsx`: structured research proposal.
- `athena-ide/src/components/cards/ErrorCard.tsx`: concise alert record.
- `athena-ide/src/components/right-rail/RightRail.tsx`: continuous ledger using only existing summary fields.
- `athena-ide/src/components/context/ContextSurface.tsx`: null closed state, typed tabs, icon close, and focus restoration.
- `athena-ide/src/components/__tests__/app-shell.test.tsx`: masthead slot and shell regions.
- `athena-ide/src/components/__tests__/conversation-pane.test.tsx`: stage, empty, message, and proposal flows.
- `athena-ide/src/components/__tests__/right-rail.test.tsx`: four-state labels and panel callbacks.
- `athena-ide/src/components/__tests__/context-surface.test.tsx`: null closed state, tab switching, close, and focus restoration.

---

### Task 1: Presentation State And Stage Mapping

**Files:**
- Create: `athena-ide/src/lib/presentation.ts`
- Create: `athena-ide/src/lib/__tests__/presentation.test.ts`

**Interfaces:**
- Consumes: `PipelineStatus` from `src/types/ui.ts` and raw phase strings from `PipelineViewModel.phase`.
- Produces: `TASK_PRESENTATIONS`, `RESEARCH_STAGES`, `getTaskPresentation(status)`, `getResearchStage(phase)`, and `getStageState(phase, stageKey)`.

- [ ] **Step 1: Write failing exhaustive mapping tests**

```ts
import { describe, expect, it } from "vitest";
import {
  RESEARCH_STAGES,
  getResearchStage,
  getStageState,
  getTaskPresentation,
} from "../presentation";

describe("task presentation", () => {
  it("collapses pipeline statuses into four user states", () => {
    expect(getTaskPresentation("idle")).toMatchObject({ key: "ready", label: "就绪" });
    expect(getTaskPresentation("running")).toMatchObject({ key: "running", label: "运行中" });
    expect(getTaskPresentation("completed")).toMatchObject({ key: "completed", label: "已完成" });
    expect(getTaskPresentation("paused")).toMatchObject({ key: "attention", label: "需要处理" });
    expect(getTaskPresentation("error")).toMatchObject({ key: "attention", label: "需要处理" });
  });
});

describe("research phase presentation", () => {
  it("maps backend phases into four research stages", () => {
    expect(RESEARCH_STAGES.map((stage) => stage.label)).toEqual([
      "数据理解",
      "创意生成",
      "假设筛选",
      "验证报告",
    ]);
    expect(getResearchStage("PREPARE")).toBe("understand");
    expect(getResearchStage("data_analysis")).toBe("understand");
    expect(getResearchStage("IDEA_GENERATION")).toBe("ideate");
    expect(getResearchStage("SEARCH")).toBe("search");
    expect(getResearchStage("code_generation")).toBe("search");
    expect(getResearchStage("EVALUATE")).toBe("search");
    expect(getResearchStage("VALIDATE")).toBe("report");
    expect(getResearchStage("report")).toBe("report");
    expect(getResearchStage("unknown-phase")).toBeNull();
    expect(getResearchStage("idle")).toBeNull();
  });

  it("marks completed, current, and upcoming stages without guessing unknown phases", () => {
    expect(getStageState("SEARCH", "understand")).toBe("completed");
    expect(getStageState("SEARCH", "ideate")).toBe("completed");
    expect(getStageState("SEARCH", "search")).toBe("current");
    expect(getStageState("SEARCH", "report")).toBe("upcoming");
    expect(getStageState("unknown-phase", "understand")).toBe("upcoming");
  });
});
```

- [ ] **Step 2: Run the test and verify it fails because the module is absent**

Run: `cd athena-ide && npm test -- src/lib/__tests__/presentation.test.ts`

Expected: FAIL with `Cannot find module '../presentation'`.

- [ ] **Step 3: Implement the pure mapping module**

```ts
import type { PipelineStatus } from "../types/ui";

export type DisplayTaskState = "ready" | "running" | "completed" | "attention";
export type TaskTone = "neutral" | "active" | "complete" | "attention";

export interface TaskPresentation {
  key: DisplayTaskState;
  label: "就绪" | "运行中" | "已完成" | "需要处理";
  tone: TaskTone;
}

export const TASK_PRESENTATIONS: Record<PipelineStatus, TaskPresentation> = {
  idle: { key: "ready", label: "就绪", tone: "neutral" },
  running: { key: "running", label: "运行中", tone: "active" },
  paused: { key: "attention", label: "需要处理", tone: "attention" },
  completed: { key: "completed", label: "已完成", tone: "complete" },
  error: { key: "attention", label: "需要处理", tone: "attention" },
};

export type ResearchStageKey = "understand" | "ideate" | "search" | "report";
export type ResearchStageState = "completed" | "current" | "upcoming";

export const RESEARCH_STAGES: ReadonlyArray<{
  key: ResearchStageKey;
  label: string;
  number: string;
}> = [
  { key: "understand", label: "数据理解", number: "01" },
  { key: "ideate", label: "创意生成", number: "02" },
  { key: "search", label: "假设筛选", number: "03" },
  { key: "report", label: "验证报告", number: "04" },
];

const PHASE_TO_STAGE: Record<string, ResearchStageKey> = {
  PREPARE: "understand",
  DATA_ANALYSIS: "understand",
  IDEA_GENERATION: "ideate",
  SEARCH: "search",
  CODE_GENERATION: "search",
  EVALUATE: "search",
  VALIDATE: "report",
  REPORT: "report",
};

export function getTaskPresentation(status: PipelineStatus): TaskPresentation {
  return TASK_PRESENTATIONS[status];
}

export function getResearchStage(phase: string): ResearchStageKey | null {
  return PHASE_TO_STAGE[phase.trim().toUpperCase()] ?? null;
}

export function getStageState(
  phase: string,
  stageKey: ResearchStageKey,
): ResearchStageState {
  const current = getResearchStage(phase);
  if (!current) return "upcoming";
  const currentIndex = RESEARCH_STAGES.findIndex((stage) => stage.key === current);
  const stageIndex = RESEARCH_STAGES.findIndex((stage) => stage.key === stageKey);
  if (stageIndex < currentIndex) return "completed";
  return stageIndex === currentIndex ? "current" : "upcoming";
}
```

- [ ] **Step 4: Run the focused test and the existing UI model test**

Run: `cd athena-ide && npm test -- src/lib/__tests__/presentation.test.ts src/types/__tests__/ui.test.ts`

Expected: both test files PASS.

- [ ] **Step 5: Commit the presentation contract**

```bash
git add athena-ide/src/lib/presentation.ts athena-ide/src/lib/__tests__/presentation.test.ts
git commit -m "feat(ide): add research presentation mapping"
```

---

### Task 2: Research Masthead And Continuous App Shell

**Files:**
- Create: `athena-ide/src/components/shell/ResearchMasthead.tsx`
- Modify: `athena-ide/src/components/shell/AppShell.tsx`
- Modify: `athena-ide/src/components/__tests__/app-shell.test.tsx`
- Modify: `athena-ide/src/App.tsx`
- Modify: `athena-ide/src/main.tsx`
- Modify: `athena-ide/src/styles.css`
- Modify: `athena-ide/src/App.css`
- Modify: `athena-ide/package.json`
- Modify: `athena-ide/package-lock.json`

**Interfaces:**
- Consumes: `PipelineStatus`, raw phase string, and `getTaskPresentation` from Task 1.
- Produces: `ResearchMasthead({ status, phase })` and `AppShell({ masthead, sidebar, conversation, rightRail, contextSurface })`.

- [ ] **Step 1: Inspect overlapping user CSS changes before editing**

Run: `git diff -- athena-ide/src/App.css athena-ide/src/styles.css`

Expected: review the current user changes; preserve any compatible behavior and do not restore old blue/card styling merely to simplify the diff.

- [ ] **Step 2: Write failing shell and masthead tests**

Replace `app-shell.test.tsx` with tests that require the new slot and four-state label:

```tsx
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { AppShell } from "../shell/AppShell";
import { ResearchMasthead } from "../shell/ResearchMasthead";

describe("AppShell", () => {
  it("renders the masthead and continuous workbench regions", () => {
    renderUi(
      <AppShell
        masthead={<div>masthead</div>}
        sidebar={<div>sidebar</div>}
        conversation={<div>conversation</div>}
        rightRail={<div>right rail</div>}
        contextSurface={<div>context surface</div>}
      />,
    );

    expect(screen.getByText("masthead")).toBeInTheDocument();
    expect(screen.getByText("sidebar")).toBeInTheDocument();
    expect(screen.getByText("conversation")).toBeInTheDocument();
    expect(screen.getByText("right rail")).toBeInTheDocument();
    expect(screen.getByText("context surface")).toBeInTheDocument();
  });

  it("shows one simplified task state in the research masthead", () => {
    renderUi(<ResearchMasthead status="paused" phase="SEARCH" />);
    expect(screen.getByRole("banner")).toHaveTextContent("Athena");
    expect(screen.getByText("需要处理")).toBeInTheDocument();
    expect(screen.queryByText("paused")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run the shell test and verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/app-shell.test.tsx`

Expected: FAIL because `ResearchMasthead` and the `masthead` prop do not exist.

- [ ] **Step 4: Install the approved visual dependencies**

Run: `cd athena-ide && npm install lucide-react @fontsource/noto-serif-sc`

Expected: `package.json` and `package-lock.json` include both runtime dependencies.

- [ ] **Step 5: Add the local serif font import**

Add this import immediately after the existing Geist imports in `src/main.tsx`:

```ts
import "@fontsource/noto-serif-sc/600.css";
```

- [ ] **Step 6: Implement `ResearchMasthead`**

```tsx
import type { PipelineStatus } from "../../types/ui";
import { getResearchStage, getTaskPresentation, RESEARCH_STAGES } from "../../lib/presentation";

interface ResearchMastheadProps {
  status: PipelineStatus;
  phase: string;
}

export function ResearchMasthead({ status, phase }: ResearchMastheadProps) {
  const task = getTaskPresentation(status);
  const currentStage = getResearchStage(phase);
  const stageLabel = currentStage
    ? RESEARCH_STAGES.find((stage) => stage.key === currentStage)?.label
    : "研究工作台";

  return (
    <header className="research-masthead">
      <div className="research-masthead__brand" aria-label="Athena Research">
        <span className="research-masthead__mark" aria-hidden="true">A</span>
        <span>Athena</span>
        <span className="research-masthead__division">Research</span>
      </div>
      <div className="research-masthead__meta">
        <span>{stageLabel}</span>
        <span className={`task-state task-state--${task.tone}`}>{task.label}</span>
      </div>
    </header>
  );
}
```

- [ ] **Step 7: Add the masthead slot and continuous grid to `AppShell`**

```tsx
import type { ReactNode } from "react";

interface AppShellProps {
  masthead: ReactNode;
  sidebar: ReactNode;
  conversation: ReactNode;
  rightRail: ReactNode;
  contextSurface: ReactNode;
}

export function AppShell({
  masthead,
  sidebar,
  conversation,
  rightRail,
  contextSurface,
}: AppShellProps) {
  return (
    <div className="app-shell">
      {masthead}
      <div className="app-shell__workspace">
        <aside className="app-shell__sidebar">{sidebar}</aside>
        <main className="app-shell__conversation">{conversation}</main>
        <aside className="app-shell__right-rail">{rightRail}</aside>
        {contextSurface ? (
          <div className="app-shell__context-surface">{contextSurface}</div>
        ) : null}
      </div>
    </div>
  );
}
```

- [ ] **Step 8: Compose the masthead in `App.tsx`**

Import `ResearchMasthead` and add this slot without changing pipeline behavior:

```tsx
<AppShell
  masthead={(
    <ResearchMasthead
      status={pipeline.viewModel.status}
      phase={pipeline.viewModel.phase}
    />
  )}
  sidebar={<SessionSidebar />}
  conversation={<ConversationPane pipeline={pipeline} />}
  rightRail={<RightRail viewModel={pipeline.viewModel} openPanel={pipeline.openPanel} />}
  contextSurface={(
    <ContextSurface
      viewModel={pipeline.viewModel}
      closePanel={pipeline.closePanel}
    />
  )}
/>
```

- [ ] **Step 9: Replace global tokens and implement the shell foundation**

Define these exact tokens in `styles.css`, retain the existing reset, and replace old blue/card tokens:

```css
:root {
  color-scheme: light;
  --canvas: #f0eee7;
  --paper: #f7f5ef;
  --ink: #1d2925;
  --ink-muted: #68736f;
  --spine: #34443e;
  --spine-active: #465a52;
  --ledger: #e4e8e2;
  --rule: #bec6c1;
  --research: #356f63;
  --attention: #ad5143;
  --marker: #d0a84a;
  --focus: #176f9c;
  --font-sans: "Geist", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-serif: "Noto Serif SC", "Songti SC", serif;
  --font-mono: "Geist Mono", "Cascadia Mono", Consolas, monospace;
  --radius-control: 4px;
  --radius-surface: 6px;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
}

html,
body,
#root {
  margin: 0;
  min-height: 100%;
  background: var(--canvas);
  color: var(--ink);
  font-family: var(--font-sans);
  font-weight: 400;
  line-height: 1.5;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

button,
input,
textarea {
  font: inherit;
}

:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
```

Replace the shell section in `App.css` with:

```css
.app-shell {
  min-height: 100vh;
  height: 100vh;
  display: grid;
  grid-template-rows: 42px minmax(0, 1fr);
  overflow: hidden;
  background: var(--canvas);
}

.research-masthead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--space-5);
  border-bottom: 1px solid var(--rule);
  background: var(--paper);
}

.research-masthead__brand,
.research-masthead__meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.research-masthead__brand {
  font-size: 13px;
  font-weight: 700;
}

.research-masthead__mark {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  background: var(--attention);
  color: #fff;
  font-family: var(--font-serif);
  font-size: 12px;
}

.research-masthead__division,
.research-masthead__meta {
  color: var(--ink-muted);
  font-size: 12px;
}

.task-state {
  padding-left: var(--space-2);
  border-left: 1px solid var(--rule);
  color: var(--ink);
  font-weight: 600;
}

.task-state--active { color: var(--research); }
.task-state--attention { color: var(--attention); }

.app-shell__workspace {
  min-height: 0;
  display: grid;
  grid-template-columns: 176px minmax(0, 1fr) 240px;
  grid-template-rows: minmax(0, 1fr) auto;
}

.app-shell__sidebar {
  grid-column: 1;
  grid-row: 1 / -1;
  min-height: 0;
  background: var(--spine);
}

.app-shell__conversation {
  grid-column: 2;
  grid-row: 1;
  min-width: 0;
  min-height: 0;
  background: var(--paper);
}

.app-shell__right-rail {
  grid-column: 3;
  grid-row: 1;
  min-width: 0;
  min-height: 0;
  border-left: 1px solid var(--rule);
  background: var(--ledger);
}

.app-shell__context-surface {
  grid-column: 2 / 4;
  grid-row: 2;
  min-width: 0;
  background: var(--paper);
}
```

- [ ] **Step 10: Run focused tests and production build**

Run: `cd athena-ide && npm test -- src/components/__tests__/app-shell.test.tsx src/lib/__tests__/presentation.test.ts`

Expected: PASS.

Run: `cd athena-ide && npm run build`

Expected: production build succeeds and bundles the local serif font.

- [ ] **Step 11: Commit the shell foundation**

```bash
git add athena-ide/package.json athena-ide/package-lock.json athena-ide/src/main.tsx athena-ide/src/styles.css athena-ide/src/App.css athena-ide/src/App.tsx athena-ide/src/components/shell/AppShell.tsx athena-ide/src/components/shell/ResearchMasthead.tsx athena-ide/src/components/__tests__/app-shell.test.tsx
git commit -m "feat(ide): add research studio shell"
```

---

### Task 3: Accessible Session Spine

**Files:**
- Create: `athena-ide/src/components/__tests__/session-sidebar.test.tsx`
- Modify: `athena-ide/src/components/shell/SessionSidebar.tsx`
- Modify: `athena-ide/src/App.css`

**Interfaces:**
- Consumes: static session records currently owned by `SessionSidebar`.
- Produces: an accessible navigation region with active-session semantics and a Lucide new-research button.

- [ ] **Step 1: Write failing sidebar semantics tests**

```tsx
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import SessionSidebar from "../shell/SessionSidebar";

describe("SessionSidebar", () => {
  it("exposes research sessions and the active item semantically", () => {
    renderUi(<SessionSidebar />);
    expect(screen.getByRole("navigation", { name: "研究会话" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新 ML 任务" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "超参数搜索" })).not.toHaveAttribute("aria-current");
  });

  it("provides an icon command with an accessible name and tooltip", () => {
    renderUi(<SessionSidebar />);
    const action = screen.getByRole("button", { name: "新建研究" });
    expect(action).toHaveAttribute("title", "新建研究");
  });
});
```

- [ ] **Step 2: Run the sidebar test and verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/session-sidebar.test.tsx`

Expected: FAIL because sessions are list items rather than named buttons and the new action is text-only.

- [ ] **Step 3: Implement the semantic spine**

```tsx
import { Plus } from "lucide-react";

export default function SessionSidebar() {
  const sessions = [
    { id: "1", label: "新 ML 任务", active: true },
    { id: "2", label: "超参数搜索", active: false },
  ];

  return (
    <nav className="session-sidebar" aria-label="研究会话">
      <div className="session-sidebar__header">
        <span className="session-sidebar__title">研究空间</span>
        <button
          className="icon-button session-sidebar__new"
          type="button"
          aria-label="新建研究"
          title="新建研究"
        >
          <Plus size={16} aria-hidden="true" />
        </button>
      </div>
      <ul className="session-sidebar__list">
        {sessions.map((session) => (
          <li key={session.id}>
            <button
              className={`session-sidebar__item${session.active ? " session-sidebar__item--active" : ""}`}
              type="button"
              aria-current={session.active ? "page" : undefined}
              title={session.label}
            >
              <span className="session-sidebar__label">{session.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
```

- [ ] **Step 4: Replace the old sidebar card styling**

Use continuous spine styles in `App.css`:

```css
.session-sidebar {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: var(--space-4) var(--space-3);
  color: #f4f2eb;
}

.session-sidebar__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-4);
  padding: 0 var(--space-2);
}

.session-sidebar__title {
  color: #c8d2cd;
  font-size: 11px;
  font-weight: 600;
}

.session-sidebar__list {
  min-height: 0;
  flex: 1;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.session-sidebar__item {
  position: relative;
  width: 100%;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: #bdc9c3;
  text-align: left;
  cursor: pointer;
}

.session-sidebar__item:hover,
.session-sidebar__item--active {
  background: var(--spine-active);
  color: #fff;
}

.session-sidebar__item--active::before {
  content: "";
  position: absolute;
  inset: 7px auto 7px 0;
  width: 2px;
  background: var(--marker);
}

.session-sidebar__label {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.icon-button {
  width: 32px;
  height: 32px;
  display: inline-grid;
  place-items: center;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-control);
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.session-sidebar__new:hover {
  border-color: #75877f;
  background: var(--spine-active);
}
```

- [ ] **Step 5: Run focused tests**

Run: `cd athena-ide && npm test -- src/components/__tests__/session-sidebar.test.tsx src/components/__tests__/app-shell.test.tsx`

Expected: PASS.

- [ ] **Step 6: Commit the session spine**

```bash
git add athena-ide/src/components/shell/SessionSidebar.tsx athena-ide/src/components/__tests__/session-sidebar.test.tsx athena-ide/src/App.css
git commit -m "feat(ide): refine research session spine"
```

---

### Task 4: Research Conversation, Stage Progress, And Composer

**Files:**
- Create: `athena-ide/src/components/conversation/StageProgress.tsx`
- Modify: `athena-ide/src/components/conversation/ConversationPane.tsx`
- Modify: `athena-ide/src/components/conversation/MessageList.tsx`
- Modify: `athena-ide/src/components/conversation/Composer.tsx`
- Modify: `athena-ide/src/components/cards/IntentPreviewCard.tsx`
- Modify: `athena-ide/src/components/cards/ErrorCard.tsx`
- Modify: `athena-ide/src/components/__tests__/conversation-pane.test.tsx`
- Modify: `athena-ide/src/App.tsx`
- Modify: `athena-ide/src/App.css`

**Interfaces:**
- Consumes: `PipelineViewModel`, `TaskPreview`, `sendPrompt(message)`, `startRun(preview)`, `RESEARCH_STAGES`, and `getStageState`.
- Produces: `ConversationPane({ viewModel, onSend, onStartRun })` and `StageProgress({ phase })`.

- [ ] **Step 1: Replace conversation tests with the narrowed interface and required states**

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ConversationPane } from "../conversation/ConversationPane";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("ConversationPane", () => {
  it("shows the research title, current stage, and empty state", () => {
    renderUi(
      <ConversationPane
        viewModel={{ ...createEmptyPipelineViewModel(), phase: "SEARCH" }}
        onSend={vi.fn()}
        onStartRun={vi.fn()}
        onOpenPanel={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading", { name: "研究工作台" })).toBeInTheDocument();
    expect(screen.getByText("假设筛选").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("开始一项研究")).toBeInTheDocument();
  });

  it("renders user records and a structured intent preview", () => {
    const viewModel = createEmptyPipelineViewModel();
    viewModel.messages = [
      { id: "user-1", role: "user", kind: "text", content: "analyze this CSV" },
      {
        id: "preview-1",
        role: "athena",
        kind: "intent-preview",
        content: "classification",
        preview: {
          task_type: "classification",
          data_type: "tabular",
          target_vars: ["target"],
          primary_metric: "f1_macro",
          direction: "maximize",
          needs_configuration: true,
        },
      },
    ];
    renderUi(<ConversationPane viewModel={viewModel} onSend={vi.fn()} onStartRun={vi.fn()} onOpenPanel={vi.fn()} />);
    expect(screen.getByText("analyze this CSV")).toBeInTheDocument();
    expect(screen.getByText("classification")).toBeInTheDocument();
    expect(screen.getByText("f1_macro")).toBeInTheDocument();
    expect(screen.getByText("需要确认配置")).toBeInTheDocument();
  });

  it("starts a confirmed proposal and exposes an icon send command", () => {
    const onStartRun = vi.fn().mockResolvedValue(undefined);
    const viewModel = createEmptyPipelineViewModel();
    viewModel.messages = [{
      id: "preview-1",
      role: "athena",
      kind: "intent-preview",
      content: "classification",
      preview: {
        task_type: "classification",
        data_type: "tabular",
        target_vars: ["target"],
        primary_metric: "f1_macro",
        direction: "maximize",
        needs_configuration: false,
      },
    }];
    renderUi(<ConversationPane viewModel={viewModel} onSend={vi.fn()} onStartRun={onStartRun} onOpenPanel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "确认并启动" }));
    expect(onStartRun).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "发送消息" })).toHaveAttribute("title", "发送消息");
  });

  it("offers one clear log action for an error record", () => {
    const onOpenPanel = vi.fn();
    const viewModel = createEmptyPipelineViewModel();
    viewModel.messages = [{ id: "error-1", role: "athena", kind: "error", content: "模型运行失败" }];
    renderUi(
      <ConversationPane
        viewModel={viewModel}
        onSend={vi.fn()}
        onStartRun={vi.fn()}
        onOpenPanel={onOpenPanel}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "查看实验日志" }));
    expect(onOpenPanel).toHaveBeenCalledWith("experiment-log");
  });
});
```

- [ ] **Step 2: Run the conversation test and verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/conversation-pane.test.tsx`

Expected: FAIL because the narrowed props, stage progress, empty state, proposal fields, and icon command do not exist.

- [ ] **Step 3: Implement `StageProgress`**

```tsx
import { RESEARCH_STAGES, getStageState } from "../../lib/presentation";

interface StageProgressProps {
  phase: string;
}

export function StageProgress({ phase }: StageProgressProps) {
  return (
    <ol className="stage-progress" aria-label="研究阶段">
      {RESEARCH_STAGES.map((stage) => {
        const state = getStageState(phase, stage.key);
        return (
          <li
            key={stage.key}
            className={`stage-progress__item stage-progress__item--${state}`}
            aria-current={state === "current" ? "step" : undefined}
          >
            <span>{stage.number}</span>
            <strong>{stage.label}</strong>
          </li>
        );
      })}
    </ol>
  );
}
```

- [ ] **Step 4: Narrow and implement `ConversationPane`**

```tsx
import type { TaskPreview } from "../../lib/tauri-bridge";
import type { ContextPanelKey, PipelineViewModel } from "../../types/ui";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import { StageProgress } from "./StageProgress";

interface ConversationPaneProps {
  viewModel: PipelineViewModel;
  onSend(message: string): Promise<void>;
  onStartRun(preview: TaskPreview): Promise<void>;
  onOpenPanel(panel: ContextPanelKey): void;
}

export function ConversationPane({ viewModel, onSend, onStartRun, onOpenPanel }: ConversationPaneProps) {
  return (
    <section className="conversation-pane" aria-labelledby="research-workspace-title">
      <header className="conversation-pane__header">
        <p className="conversation-pane__eyebrow">Active investigation</p>
        <h1 id="research-workspace-title" className="conversation-pane__title">研究工作台</h1>
        <p className="conversation-pane__subtitle">对话驱动的自动化机器学习研究</p>
        <StageProgress phase={viewModel.phase} />
      </header>
      <MessageList messages={viewModel.messages} onStartRun={onStartRun} onOpenPanel={onOpenPanel} />
      <Composer onSend={onSend} />
    </section>
  );
}
```

Update the conversation slot in `App.tsx` to pass the narrow props and existing callbacks explicitly:

```tsx
conversation={(
  <ConversationPane
    viewModel={pipeline.viewModel}
    onSend={pipeline.sendPrompt}
    onStartRun={pipeline.startRun}
    onOpenPanel={pipeline.openPanel}
  />
)}
```

- [ ] **Step 5: Implement message, proposal, error, and composer semantics**

Use this empty and record structure in `MessageList`:

```tsx
if (messages.length === 0) {
  return (
    <div className="message-list message-list--empty">
      <div className="conversation-empty">
        <h2>开始一项研究</h2>
        <p>描述数据、目标与评价指标。</p>
      </div>
    </div>
  );
}

return (
  <div className="message-list" aria-live="polite">
    {messages.map((message) => {
      if (message.kind === "intent-preview" && message.preview) {
        return (
          <IntentPreviewCard
            key={message.id}
            preview={message.preview}
            onConfirm={() => onStartRun(message.preview!)}
          />
        );
      }
      if (message.kind === "error") {
        return (
          <ErrorCard
            key={message.id}
            content={message.content}
            onOpenLog={() => onOpenPanel("experiment-log")}
          />
        );
      }
      return (
        <article key={message.id} className={`research-record research-record--${message.role}`}>
          <span className="research-record__author">{message.role === "user" ? "你" : "Athena"}</span>
          <p>{message.content}</p>
        </article>
      );
    })}
  </div>
);
```

Use a definition list in `IntentPreviewCard`:

```tsx
<section className="proposal" aria-label="研究提案">
  <p className="proposal__eyebrow">Research proposal</p>
  <h2>研究提案</h2>
  <dl className="proposal__details">
    <div><dt>任务类型</dt><dd>{preview.task_type}</dd></div>
    <div><dt>主指标</dt><dd>{preview.primary_metric}</dd></div>
    <div><dt>配置</dt><dd>{preview.needs_configuration ? "需要确认配置" : "已就绪"}</dd></div>
  </dl>
  <button className="command-button command-button--primary" type="button" onClick={() => void onConfirm()}>
    确认并启动
  </button>
</section>
```

Use this exact `MessageListProps` contract:

```tsx
interface MessageListProps {
  messages: UIMessage[];
  onStartRun(preview: TaskPreview): Promise<void>;
  onOpenPanel(panel: ContextPanelKey): void;
}
```

Use `role="alert"`, one short heading, and one explicit existing-panel action in `ErrorCard`:

```tsx
interface ErrorCardProps {
  content: string;
  onOpenLog(): void;
}

export function ErrorCard({ content, onOpenLog }: ErrorCardProps) {
  return (
    <section className="research-alert" role="alert">
      <h2>需要处理</h2>
      <p>{content}</p>
      <button className="command-button" type="button" onClick={onOpenLog}>
        查看实验日志
      </button>
    </section>
  );
}
```

Use a Lucide send button in `Composer`:

```tsx
import { Send } from "lucide-react";

<form className="composer" onSubmit={(event) => void handleSubmit(event)}>
  <textarea
    className="composer__input"
    value={value}
    onChange={(event) => setValue(event.target.value)}
    placeholder="描述你的研究目标…"
    aria-label="研究消息"
    rows={2}
    disabled={disabled}
  />
  <button
    className="icon-button composer__send"
    type="submit"
    disabled={disabled || !value.trim()}
    aria-label="发送消息"
    title="发送消息"
  >
    <Send size={17} aria-hidden="true" />
  </button>
</form>
```

- [ ] **Step 6: Replace conversation/card styling in `App.css`**

Implement the approved hierarchy with these core rules, deleting old `.message-bubble`, `.card`, and blue action styles after their JSX is gone:

```css
.conversation-pane { height: 100%; min-height: 0; display: flex; flex-direction: column; }
.conversation-pane__header { padding: var(--space-6) var(--space-6) var(--space-4); border-bottom: 1px solid var(--rule); }
.conversation-pane__eyebrow,
.proposal__eyebrow { margin: 0 0 var(--space-1); color: var(--attention); font-size: 11px; font-weight: 700; }
.conversation-pane__title { margin: 0; font-family: var(--font-serif); font-size: 23px; font-weight: 600; }
.conversation-pane__subtitle { margin: var(--space-1) 0 0; color: var(--ink-muted); font-size: 13px; }
.stage-progress { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-2); margin: var(--space-5) 0 0; padding: 0; list-style: none; }
.stage-progress__item { padding-top: var(--space-2); border-top: 2px solid var(--rule); color: var(--ink-muted); }
.stage-progress__item span { display: block; font-family: var(--font-mono); font-size: 10px; }
.stage-progress__item strong { font-size: 11px; font-weight: 600; }
.stage-progress__item--completed { border-color: var(--research); color: var(--research); }
.stage-progress__item--current { border-color: var(--attention); color: var(--ink); }
.message-list { min-height: 0; flex: 1; overflow-y: auto; padding: var(--space-5) var(--space-6); }
.message-list--empty { display: grid; place-items: center; }
.conversation-empty { max-width: 320px; text-align: center; color: var(--ink-muted); }
.conversation-empty h2 { margin: 0 0 var(--space-2); color: var(--ink); font-family: var(--font-serif); font-size: 18px; }
.research-record { max-width: 82%; margin-bottom: var(--space-4); padding: var(--space-3) var(--space-4); border-left: 2px solid var(--research); background: #e8ece6; border-radius: 0 var(--radius-surface) var(--radius-surface) 0; }
.research-record--user { margin-left: auto; border-left: 0; background: #f0e3df; border-radius: var(--radius-surface); }
.research-record__author { display: block; margin-bottom: var(--space-1); color: var(--ink-muted); font-size: 11px; font-weight: 600; }
.research-record p { margin: 0; font-size: 14px; line-height: 1.6; }
.proposal,
.research-alert { margin-bottom: var(--space-4); padding: var(--space-4); border-left: 2px solid var(--research); background: #eef1ec; border-radius: 0 var(--radius-surface) var(--radius-surface) 0; }
.research-alert { border-left-color: var(--attention); background: #f4e9e5; }
.proposal h2,
.research-alert h2 { margin: 0 0 var(--space-3); font-family: var(--font-serif); font-size: 17px; }
.proposal__details { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-3); margin: 0 0 var(--space-4); }
.proposal__details dt { color: var(--ink-muted); font-size: 11px; }
.proposal__details dd { margin: var(--space-1) 0 0; font-size: 13px; font-weight: 600; }
.composer { display: grid; grid-template-columns: minmax(0, 1fr) 36px; gap: var(--space-2); margin: 0 var(--space-6) var(--space-5); padding: var(--space-2); border: 1px solid var(--rule); border-radius: var(--radius-control); background: #fff; }
.composer__input { width: 100%; min-height: 42px; padding: var(--space-2); border: 0; outline: 0; resize: none; background: transparent; color: var(--ink); }
.composer__send { align-self: end; background: var(--ink); color: #fff; }
.composer__send:disabled { opacity: .42; cursor: default; }
.command-button { min-height: 34px; padding: 0 var(--space-4); border: 1px solid var(--rule); border-radius: var(--radius-control); background: transparent; color: var(--ink); cursor: pointer; }
.command-button--primary { border-color: var(--ink); background: var(--ink); color: #fff; }
```

- [ ] **Step 7: Run focused tests and build**

Run: `cd athena-ide && npm test -- src/components/__tests__/conversation-pane.test.tsx src/lib/__tests__/presentation.test.ts`

Expected: PASS.

Run: `cd athena-ide && npm run build`

Expected: PASS with no TypeScript casts added to bypass the narrowed props.

- [ ] **Step 8: Commit the conversation workspace**

```bash
git add athena-ide/src/App.tsx athena-ide/src/App.css athena-ide/src/components/conversation athena-ide/src/components/cards athena-ide/src/components/__tests__/conversation-pane.test.tsx
git commit -m "feat(ide): redesign research conversation"
```

---

### Task 5: Continuous Research Ledger

**Files:**
- Modify: `athena-ide/src/components/right-rail/RightRail.tsx`
- Modify: `athena-ide/src/components/__tests__/right-rail.test.tsx`
- Modify: `athena-ide/src/App.css`

**Interfaces:**
- Consumes: only `PipelineViewModel.rightRail`, `getTaskPresentation`, `getResearchStage`, `RESEARCH_STAGES`, and existing `openPanel(panel)`.
- Produces: ledger rows for experiment log, metrics, research tree, and report. No new summary fields.

- [ ] **Step 1: Write failing ledger tests**

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { RightRail } from "../right-rail/RightRail";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("RightRail", () => {
  it("shows the simplified task state and only existing summary fields", () => {
    const viewModel = createEmptyPipelineViewModel();
    viewModel.phase = "SEARCH";
    viewModel.status = "paused";
    viewModel.rightRail = {
      phase: "SEARCH",
      status: "paused",
      budgetRemaining: 7,
      noImproveStreak: 2,
      bestPrimary: 0.7812,
      latestExperimentId: "exp-4",
    };
    renderUi(<RightRail viewModel={viewModel} openPanel={vi.fn()} />);
    expect(screen.getByText("研究台账")).toBeInTheDocument();
    expect(screen.getByText("需要处理")).toBeInTheDocument();
    expect(screen.getByText("假设筛选")).toBeInTheDocument();
    expect(screen.getByText("0.7812")).toBeInTheDocument();
    expect(screen.getByText("exp-4")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.queryByText(/趋势/)).not.toBeInTheDocument();
  });

  it("opens existing detail panels from semantic ledger rows", () => {
    const openPanel = vi.fn();
    renderUi(<RightRail viewModel={createEmptyPipelineViewModel()} openPanel={openPanel} />);
    fireEvent.click(screen.getByRole("button", { name: "打开实验日志" }));
    fireEvent.click(screen.getByRole("button", { name: "打开指标" }));
    fireEvent.click(screen.getByRole("button", { name: "打开研究树" }));
    fireEvent.click(screen.getByRole("button", { name: "打开报告" }));
    expect(openPanel.mock.calls).toEqual([
      ["experiment-log"],
      ["metrics"],
      ["research-tree"],
      ["report"],
    ]);
  });
});
```

- [ ] **Step 2: Run the ledger test and verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/right-rail.test.tsx`

Expected: FAIL because the current rail uses summary cards and exposes raw status/phase.

- [ ] **Step 3: Implement the ledger with existing fields only**

```tsx
import type { ContextPanelKey, PipelineViewModel } from "../../types/ui";
import { getResearchStage, getTaskPresentation, RESEARCH_STAGES } from "../../lib/presentation";

interface RightRailProps {
  viewModel: PipelineViewModel;
  openPanel(panel: ContextPanelKey): void;
}

export function RightRail({ viewModel, openPanel }: RightRailProps) {
  const { rightRail } = viewModel;
  const task = getTaskPresentation(rightRail.status);
  const stageKey = getResearchStage(rightRail.phase);
  const stageLabel = stageKey
    ? RESEARCH_STAGES.find((stage) => stage.key === stageKey)?.label
    : "研究进行中";

  return (
    <section className="right-rail" aria-labelledby="research-ledger-title">
      <header className="right-rail__header">
        <p>Evidence index</p>
        <h2 id="research-ledger-title">研究台账</h2>
      </header>
      <button className="ledger-row ledger-row--status" type="button" data-panel-trigger="experiment-log" aria-label="打开实验日志" onClick={() => openPanel("experiment-log")}>
        <span>当前阶段</span><strong>{stageLabel}</strong><small>{task.label}</small>
      </button>
      <button className="ledger-row" type="button" data-panel-trigger="metrics" aria-label="打开指标" onClick={() => openPanel("metrics")}>
        <span>最佳指标</span><strong>{rightRail.bestPrimary == null ? "--" : rightRail.bestPrimary.toFixed(4)}</strong><small>剩余预算 {rightRail.budgetRemaining}</small>
      </button>
      <button className="ledger-row" type="button" data-panel-trigger="research-tree" aria-label="打开研究树" onClick={() => openPanel("research-tree")}>
        <span>最近实验</span><strong>{rightRail.latestExperimentId ?? "--"}</strong><small>无提升轮次 {rightRail.noImproveStreak}</small>
      </button>
      <button className="ledger-row ledger-row--report" type="button" data-panel-trigger="report" aria-label="打开报告" onClick={() => openPanel("report")}>
        <span>研究输出</span><strong>报告</strong><small>查看详情</small>
      </button>
    </section>
  );
}
```

- [ ] **Step 4: Replace summary-card styling with ledger rules**

```css
.right-rail { height: 100%; min-height: 0; padding: var(--space-5) var(--space-4); overflow-y: auto; }
.right-rail__header { margin-bottom: var(--space-4); }
.right-rail__header p { margin: 0 0 var(--space-1); color: var(--ink-muted); font-size: 10px; font-weight: 700; }
.right-rail__header h2 { margin: 0; font-family: var(--font-serif); font-size: 17px; font-weight: 600; }
.ledger-row { width: 100%; display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-1); padding: var(--space-3) 0; border: 0; border-top: 1px solid var(--rule); border-radius: 0; background: transparent; color: var(--ink); text-align: left; cursor: pointer; }
.ledger-row:first-of-type { border-top-width: 2px; }
.ledger-row:hover { background: rgba(255, 255, 255, .34); }
.ledger-row span { color: var(--ink-muted); font-size: 10px; font-weight: 600; }
.ledger-row strong { font-family: var(--font-mono); font-size: 16px; font-variant-numeric: tabular-nums; }
.ledger-row--status strong,
.ledger-row--report strong { font-family: var(--font-sans); font-size: 14px; }
.ledger-row small { color: var(--ink-muted); font-size: 11px; }
```

Delete the unused `.summary-card` selectors.

- [ ] **Step 5: Run focused tests and build**

Run: `cd athena-ide && npm test -- src/components/__tests__/right-rail.test.tsx src/lib/__tests__/presentation.test.ts`

Expected: PASS.

Run: `cd athena-ide && npm run build`

Expected: PASS.

- [ ] **Step 6: Commit the ledger**

```bash
git add athena-ide/src/components/right-rail/RightRail.tsx athena-ide/src/components/__tests__/right-rail.test.tsx athena-ide/src/App.css
git commit -m "feat(ide): replace summary cards with research ledger"
```

---

### Task 6: Typed Context Workspace And Focus Restoration

**Files:**
- Modify: `athena-ide/src/components/context/ContextSurface.tsx`
- Modify: `athena-ide/src/components/__tests__/context-surface.test.tsx`
- Modify: `athena-ide/src/App.tsx`
- Modify: `athena-ide/src/App.css`

**Interfaces:**
- Consumes: `CONTEXT_PANELS`, `ContextPanelKey`, `PipelineViewModel.contextSurface`, `openPanel(panel)`, and `closePanel()`.
- Produces: a null closed state, typed tablist, icon close command, and focus return to the actual element that opened the workspace.

- [ ] **Step 1: Write failing context workspace tests**

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ContextSurface } from "../context/ContextSurface";
import { createEmptyPipelineViewModel } from "../../types/ui";

describe("ContextSurface", () => {
  it("renders nothing when closed", () => {
    const { container } = renderUi(
      <ContextSurface viewModel={createEmptyPipelineViewModel()} openPanel={vi.fn()} closePanel={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("switches typed tabs through openPanel", () => {
    const openPanel = vi.fn();
    renderUi(
      <ContextSurface
        viewModel={{ ...createEmptyPipelineViewModel(), contextSurface: { isOpen: true, activePanel: "metrics" } }}
        openPanel={openPanel}
        closePanel={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: "指标" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "研究树" }));
    expect(openPanel).toHaveBeenCalledWith("research-tree");
  });

  it("closes and restores focus to the active ledger trigger", () => {
    const closePanel = vi.fn();
    const closedViewModel = createEmptyPipelineViewModel();
    const openedViewModel = {
      ...closedViewModel,
      contextSurface: { isOpen: true as const, activePanel: "metrics" as const },
    };
    const { rerender } = renderUi(
      <>
        <button data-panel-trigger="metrics">metrics trigger</button>
        <ContextSurface
          viewModel={closedViewModel}
          openPanel={vi.fn()}
          closePanel={closePanel}
        />
      </>,
    );
    const trigger = screen.getByRole("button", { name: "metrics trigger" });
    trigger.focus();
    rerender(
      <>
        <button data-panel-trigger="metrics">metrics trigger</button>
        <ContextSurface
          viewModel={openedViewModel}
          openPanel={vi.fn()}
          closePanel={closePanel}
        />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
    expect(closePanel).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "metrics trigger" })).toHaveFocus();
  });
});
```

- [ ] **Step 2: Run the context test and verify it fails**

Run: `cd athena-ide && npm test -- src/components/__tests__/context-surface.test.tsx`

Expected: FAIL because closed state renders text, tabs/openPanel do not exist, and close does not restore focus.

- [ ] **Step 3: Implement the typed context workspace**

```tsx
import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { ContextPanelKey, PipelineViewModel } from "../../types/ui";
import { CONTEXT_PANELS } from "../../types/ui";
import MetricChart from "../MetricChart";
import ResearchTreeViz from "../ResearchTreeViz";
import DiffViewer from "../DiffViewer";
import FileTree from "../FileTree";

interface ContextSurfaceProps {
  viewModel: PipelineViewModel;
  openPanel(panel: ContextPanelKey): void;
  closePanel(): void;
}

const PANEL_TITLES: Record<ContextPanelKey, string> = {
  metrics: "指标",
  "research-tree": "研究树",
  "experiment-log": "实验日志",
  diff: "差异",
  files: "文件",
  report: "报告",
};

export function ContextSurface({ viewModel, openPanel, closePanel }: ContextSurfaceProps) {
  const { isOpen, activePanel: panel } = viewModel.contextSurface;
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (isOpen && document.activeElement instanceof HTMLElement) {
      returnFocusRef.current = document.activeElement;
    }
    if (!isOpen) returnFocusRef.current = null;
  }, [isOpen]);

  if (!isOpen) return null;

  function handleClose() {
    const returnTarget = returnFocusRef.current;
    closePanel();
    returnTarget?.focus();
  }

  return (
    <section className="context-surface" aria-labelledby="context-title">
      <header className="context-surface__header">
        <h2 id="context-title">{PANEL_TITLES[panel]}</h2>
        <button className="icon-button" type="button" aria-label="关闭详情" title="关闭详情" onClick={handleClose}>
          <X size={17} aria-hidden="true" />
        </button>
      </header>
      <div className="context-tabs" role="tablist" aria-label="研究详情">
        {CONTEXT_PANELS.map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={panel === key}
            className={panel === key ? "context-tabs__item context-tabs__item--active" : "context-tabs__item"}
            onClick={() => openPanel(key)}
          >
            {PANEL_TITLES[key]}
          </button>
        ))}
      </div>
      <div className="context-surface__body" role="tabpanel">
        {panel === "metrics" && <MetricChart />}
        {panel === "research-tree" && <ResearchTreeViz />}
        {panel === "diff" && <DiffViewer />}
        {panel === "files" && <FileTree />}
        {panel === "experiment-log" && <div className="detail-panel">实验日志</div>}
        {panel === "report" && <div className="detail-panel">报告预览</div>}
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Pass `openPanel` from `App.tsx`**

```tsx
contextSurface={
  pipeline.viewModel.contextSurface.isOpen ? (
    <ContextSurface
      viewModel={pipeline.viewModel}
      openPanel={pipeline.openPanel}
      closePanel={pipeline.closePanel}
    />
  ) : null
}
```

- [ ] **Step 5: Implement the integrated context styling**

```css
.context-surface {
  height: clamp(280px, 38vh, 420px);
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
  border-top: 1px solid var(--rule);
  background: var(--paper);
}

.context-surface__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-5) var(--space-2);
}

.context-surface__header h2 {
  margin: 0;
  font-family: var(--font-serif);
  font-size: 17px;
  font-weight: 600;
}

.context-tabs {
  display: flex;
  gap: var(--space-1);
  padding: 0 var(--space-5);
  border-bottom: 1px solid var(--rule);
  overflow-x: auto;
}

.context-tabs__item {
  padding: var(--space-2) var(--space-3);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--ink-muted);
  cursor: pointer;
}

.context-tabs__item--active {
  border-bottom-color: var(--attention);
  color: var(--ink);
}

.context-surface__body {
  min-height: 0;
  padding: var(--space-4) var(--space-5);
  overflow: auto;
}
```

Delete `.context-surface--open`, `.context-surface--closed`, and the old slide-up keyframes.

- [ ] **Step 6: Run focused tests and build**

Run: `cd athena-ide && npm test -- src/components/__tests__/context-surface.test.tsx src/components/__tests__/right-rail.test.tsx`

Expected: PASS.

Run: `cd athena-ide && npm run build`

Expected: PASS.

- [ ] **Step 7: Commit the context workspace**

```bash
git add athena-ide/src/App.tsx athena-ide/src/App.css athena-ide/src/components/context/ContextSurface.tsx athena-ide/src/components/__tests__/context-surface.test.tsx
git commit -m "feat(ide): integrate research detail workspace"
```

---

### Task 7: Responsive Visual QA And Accessibility Gate

**Files:**
- Create: `athena-ide/playwright.config.ts`
- Create: `athena-ide/tests/visual/workbench.spec.ts`
- Modify: `athena-ide/package.json`
- Modify: `athena-ide/package-lock.json`
- Modify: `athena-ide/src/App.css`

**Interfaces:**
- Consumes: the completed workbench at `/` served by Vite.
- Produces: `npm run test:visual`, target-size screenshots, overflow checks, console-error checks, responsive fallback, and reduced-motion rules.

- [ ] **Step 1: Install Playwright and add the visual test script**

Run: `cd athena-ide && npm install --save-dev @playwright/test`

Add to `package.json` scripts:

```json
"test:visual": "playwright test"
```

Install the local Chromium runtime:

Run: `cd athena-ide && npx playwright install chromium`

Expected: Chromium installation succeeds.

- [ ] **Step 2: Add Playwright configuration**

```ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/visual",
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:4173",
    browserName: "chromium",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
```

- [ ] **Step 3: Write visual tests before adding final responsive rules**

```ts
import { expect, test } from "@playwright/test";

const viewports = [
  { name: "desktop-1440", width: 1440, height: 900 },
  { name: "desktop-1024", width: 1024, height: 720 },
];

for (const viewport of viewports) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test("renders a stable research workspace", async ({ page }, testInfo) => {
      const consoleErrors: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });

      await page.goto("/");
      await expect(page.getByRole("banner")).toContainText("Athena");
      await expect(page.getByRole("navigation", { name: "研究会话" })).toBeVisible();
      await expect(page.getByRole("main")).toBeVisible();
      await expect(page.getByText("研究台账")).toBeVisible();

      const sidebar = await page.locator(".app-shell__sidebar").boundingBox();
      const ledger = await page.locator(".app-shell__right-rail").boundingBox();
      const expectedSidebarWidth = viewport.width === 1024 ? 148 : 176;
      const expectedLedgerWidth = viewport.width === 1024 ? 210 : 240;
      expect(Math.round(sidebar?.width ?? 0)).toBe(expectedSidebarWidth);
      expect(Math.round(ledger?.width ?? 0)).toBe(expectedLedgerWidth);

      const overflow = await page.evaluate(() => ({
        horizontal: document.documentElement.scrollWidth > window.innerWidth,
        vertical: document.documentElement.scrollHeight > window.innerHeight,
      }));
      expect(overflow).toEqual({ horizontal: false, vertical: false });
      expect(consoleErrors).toEqual([]);

      await page.screenshot({
        path: testInfo.outputPath(`${viewport.name}.png`),
        fullPage: true,
      });
    });
  });
}
```

- [ ] **Step 4: Run visual tests and record the responsive failure**

Run: `cd athena-ide && npm run test:visual`

Expected: FAIL in the `1024x720` case because the shell still uses 176px and 240px columns instead of the required 148px and 210px compact columns.

- [ ] **Step 5: Add responsive and reduced-motion rules**

```css
@media (max-width: 1100px) {
  .app-shell__workspace {
    grid-template-columns: 148px minmax(0, 1fr) 210px;
  }

  .conversation-pane__header,
  .message-list {
    padding-left: var(--space-4);
    padding-right: var(--space-4);
  }

  .composer {
    margin-left: var(--space-4);
    margin-right: var(--space-4);
  }

  .right-rail {
    padding-left: var(--space-3);
    padding-right: var(--space-3);
  }
}

@media (max-width: 899px) {
  .app-shell {
    height: auto;
    min-height: 100vh;
    overflow: visible;
  }

  .app-shell__workspace {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(560px, auto) auto auto;
  }

  .app-shell__sidebar,
  .app-shell__conversation,
  .app-shell__right-rail,
  .app-shell__context-surface {
    grid-column: 1;
    grid-row: auto;
  }

  .session-sidebar {
    min-height: 110px;
  }

  .session-sidebar__list {
    display: flex;
    gap: var(--space-2);
    overflow-x: auto;
  }

  .session-sidebar__list li {
    min-width: 160px;
  }

  .app-shell__right-rail {
    border-top: 1px solid var(--rule);
    border-left: 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: .01ms !important;
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
  }
}
```

Use transitions only for color/border changes at 120-180ms. Do not restore the deleted slide-up animation.

- [ ] **Step 6: Run the complete automated gate**

Run: `cd athena-ide && npm test`

Expected: all Vitest files PASS.

Run: `cd athena-ide && npm run build`

Expected: production build PASS.

Run: `cd athena-ide && npm run test:visual`

Expected: both target-size tests PASS, write `desktop-1440.png` and `desktop-1024.png` under Playwright test results, report no console errors, and report no document overflow.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 7: Inspect screenshots against the design acceptance criteria**

Open both generated PNG files and verify all of the following before committing:

- The warm central research paper is the visual center.
- The dark ink region is limited to the narrow session spine; the masthead remains warm white.
- The ledger reads as one continuous information surface, not four cards.
- No text, icon button, stage label, or metric overlaps another element.
- The context workspace uses the full center-plus-ledger width when opened in component tests; no decorative container constrains ResearchTree or metrics.
- No panel has 10-16px rounded corners, a generic blue accent, heavy shadow, gradient, glass effect, or hover lift.

- [ ] **Step 8: Commit the responsive visual gate**

```bash
git add athena-ide/package.json athena-ide/package-lock.json athena-ide/playwright.config.ts athena-ide/tests/visual/workbench.spec.ts athena-ide/src/App.css
git commit -m "test(ide): add responsive visual quality gate"
```

---

## Final Verification

After all seven tasks and their commits:

1. Run `git status --short` and confirm no task files remain unstaged.
2. Run `cd athena-ide && npm test` and report the exact passing test/file counts.
3. Run `cd athena-ide && npm run build` and report the generated CSS/JS bundle sizes.
4. Run `cd athena-ide && npm run test:visual` and report both viewport results and screenshot paths.
5. Run `git diff --check` and confirm no whitespace errors.
6. Compare the final file diff to `docs/superpowers/specs/2026-07-29-athena-ide-research-studio-design.md`; verify no backend, bridge, event protocol, or synthetic metric data changed.
