# Athena IDE 视觉美化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以 Codex Canvas 浅色美学为基线，对 Athena IDE 前端进行全覆盖视觉美化——Geist 字体、三阶灰色正文、偶数圆角、两层阴影、克制动效。

**Architecture:** 从 `worktree-athena-ide-frontend-redesign` 分支合入组件架构（AppShell / ConversationPane / RightRail / ContextSurface），然后全局重写 CSS 设计 token 和组件样式。不改组件逻辑和数据流。

**Tech Stack:** React 18, TypeScript, Vite, Tauri v2, Geist (Vercel font), CSS Custom Properties

## Global Constraints

- 仅改 CSS 和设计 token（`styles.css` / `App.css`），不改组件 .tsx 文件
- 不改 `PythonBridge`、WebSocket 协议、后端逻辑
- 不改 Monaco Editor、ReactFlow、Recharts 第三方样式
- 强调色 `#2563EB` 仅出现在主按钮、链接、选中态、聚焦态（≤5% 场景）
- 面板圆角 16px、卡片 12px、按钮 10px、气泡 14px
- 阴影仅两层：`0 1px 3px rgba(0,0,0,0.04)` / `0 4px 16px rgba(0,0,0,0.06)`
- Geist Sans 用作主字体，Geist Mono 用于代码/Metrics
- 字体仅用 400 / 500 / 600 / 700 四个字重

---

### Task 1: 合入 Worktree 组件架构

**Files:**
- Copy all changed files from `worktree-athena-ide-frontend-redesign:athena-ide/src/` to `athena-ide/src/`

**Interfaces:**
- Produces: `AppShell`, `ConversationPane`, `RightRail`, `ContextSurface`, `SessionSidebar`, `TopBar`, `MessageList`, `Composer`, `IntentPreviewCard`, `ErrorCard` components; `usePipeline` (with viewModel), `useEvents` hooks; `types/ui.ts`; updated `lib/tauri-bridge.ts`

- [ ] **Step 1: 从 worktree 分支 checkout 所有源文件**

```bash
cd "C:\Users\80163\Desktop\挑战杯_2026\Athena"
git checkout worktree-athena-ide-frontend-redesign -- \
  athena-ide/src/App.tsx \
  athena-ide/src/App.css \
  athena-ide/src/styles.css \
  athena-ide/src/components/shell/AppShell.tsx \
  athena-ide/src/components/shell/TopBar.tsx \
  athena-ide/src/components/shell/SessionSidebar.tsx \
  athena-ide/src/components/conversation/ConversationPane.tsx \
  athena-ide/src/components/conversation/MessageList.tsx \
  athena-ide/src/components/conversation/Composer.tsx \
  athena-ide/src/components/right-rail/RightRail.tsx \
  athena-ide/src/components/context/ContextSurface.tsx \
  athena-ide/src/components/cards/IntentPreviewCard.tsx \
  athena-ide/src/components/cards/ErrorCard.tsx \
  athena-ide/src/hooks/usePipeline.ts \
  athena-ide/src/hooks/useEvents.ts \
  athena-ide/src/hooks/useResearchTree.ts \
  athena-ide/src/lib/tauri-bridge.ts \
  athena-ide/src/types/ui.ts
```

- [ ] **Step 2: 验证 TypeScript 编译通过**

```bash
cd athena-ide && npx tsc --noEmit
```
Expected: 无错误（可能需要处理少量类型不匹配，记下来以便后续修）

- [ ] **Step 3: 确保旧组件占位文件仍存在（DiffViewer, FileTree, ResearchTreeViz, MetricChart）**

```bash
ls athena-ide/src/components/DiffViewer.tsx athena-ide/src/components/FileTree.tsx athena-ide/src/components/ResearchTreeViz.tsx athena-ide/src/components/MetricChart.tsx
```
Expected: 四个文件均存在（ContextSurface 引用它们）

- [ ] **Step 4: Commit**

```bash
git add athena-ide/src/
git commit -m "feat(ide): merge AppShell architecture from frontend-redesign worktree"
```

---

### Task 2: 安装 Geist 字体

**Files:**
- Modify: `athena-ide/package.json`

**Interfaces:**
- Produces: Geist Sans + Geist Mono 字体可用，`@fontsource/geist` 和 `@fontsource/geist-mono` 已安装

- [ ] **Step 1: 安装字体 npm 包**

```bash
cd athena-ide && npm install @fontsource/geist @fontsource/geist-mono
```
Expected: 两个包安装到 `node_modules/`，`package.json` 自动更新

- [ ] **Step 2: 在 main.tsx 顶部导入字体**

修改 `athena-ide/src/main.tsx`，在现有 import 之前添加：

```tsx
import "@fontsource/geist";
import "@fontsource/geist-mono";
```

完整文件：

```tsx
import "@fontsource/geist";
import "@fontsource/geist-mono";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 3: 验证字体加载**

```bash
cd athena-ide && npm run dev
```
打开浏览器 DevTools → Network → Font，确认 `geist` 字体的 woff2 文件已请求。检查 Computed Styles：`body` 的 `font-family` 应显示 `Geist`。

- [ ] **Step 4: Commit**

```bash
git add athena-ide/package.json athena-ide/package-lock.json athena-ide/src/main.tsx
git commit -m "chore(ide): install Geist and Geist Mono fonts"
```

---

### Task 3: 重写设计 Token — styles.css

**Files:**
- Modify: `athena-ide/src/styles.css`（完全重写）

**Interfaces:**
- Produces: CSS 自定义属性（`--bg`, `--surface`, `--accent`, `--text-primary` 等），全局 reset，Geist 字体栈

- [ ] **Step 1: 替换 styles.css 为设计 token 系统**

```css
:root {
  color-scheme: light;

  /* background */
  --bg: #F7F8FA;
  --surface: #FFFFFF;
  --surface-elevated: #F1F3F6;
  --surface-muted: #F0F2F5;

  /* border */
  --border: rgba(0, 0, 0, 0.07);
  --border-hover: rgba(37, 99, 235, 0.18);
  --border-focus: rgba(37, 99, 235, 0.35);

  /* accent */
  --accent: #2563EB;
  --accent-hover: #1D4ED8;
  --accent-subtle: rgba(37, 99, 235, 0.06);

  /* text */
  --text-primary: #111318;
  --text-secondary: #6B7280;
  --text-tertiary: #9CA3AF;

  /* semantic */
  --success: #059669;
  --warning: #D97706;
  --danger: #DC2626;

  /* shadow */
  --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.06);

  /* font */
  --font-sans: "Geist", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "Geist Mono", "JetBrains Mono", "Fira Code", monospace;

  /* radius */
  --radius-panel: 16px;
  --radius-card: 12px;
  --radius-bubble: 14px;
  --radius-btn: 10px;
  --radius-tag: 6px;

  /* spacing */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 12px;
  --space-lg: 16px;
  --space-xl: 20px;
  --space-2xl: 24px;
  --space-3xl: 32px;
}

* {
  box-sizing: border-box;
}

html,
body,
#root {
  margin: 0;
  min-height: 100%;
  background: var(--bg);
  font-family: var(--font-sans);
  line-height: 1.5;
  font-weight: 400;
  color: var(--text-primary);
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body {
  min-height: 100vh;
}

button,
input,
textarea {
  font: inherit;
}

/* 等宽数值 */
.mono {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
```

- [ ] **Step 2: 验证 CSS 变量生效**

打开浏览器 DevTools → Elements → `:root`，检查 Computed Styles 中 `--accent` 应为 `#2563EB`，`--font-sans` 应包含 `Geist`。

- [ ] **Step 3: Commit**

```bash
git add athena-ide/src/styles.css
git commit -m "style(ide): rewrite CSS design tokens with Codex Canvas system"
```

---

### Task 4: 重写 App.css — 全局布局

**Files:**
- Modify: `athena-ide/src/App.css`（完全重写，替换现有 worktree 版本）

**Interfaces:**
- Consumes: CSS 变量 from Task 3
- Produces: `.app-shell`, `.top-bar`, `.app-shell__body`, `.app-shell__sidebar`, `.app-shell__conversation`, `.app-shell__right-rail`, `.app-shell__context-surface`

- [ ] **Step 1: 写入完整 App.css**

```css
/* ===== AppShell ===== */
.app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto;
  background: var(--bg);
}

.app-shell__body {
  min-height: 0;
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 280px;
  gap: var(--space-lg);
  padding: var(--space-lg) var(--space-xl) 0;
}

/* ===== shared panel surface ===== */
.app-shell__sidebar,
.app-shell__conversation,
.app-shell__right-rail,
.app-shell__context-surface,
.top-bar {
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: var(--radius-panel);
  box-shadow: var(--shadow-sm);
}

/* ===== TopBar ===== */
.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-sm) var(--space-xl);
  height: 44px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
  box-shadow: none;
}

.top-bar__title {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
}

.top-bar__meta {
  display: flex;
  gap: var(--space-md);
  font-size: 12px;
  color: var(--text-secondary);
}

.top-bar__dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--success);
  margin-right: var(--space-xs);
  vertical-align: middle;
}

/* ===== panels overflow ===== */
.app-shell__sidebar {
  min-height: 0;
  overflow-y: auto;
}

.app-shell__conversation {
  min-height: 0;
  overflow-y: auto;
}

.app-shell__right-rail {
  min-height: 0;
  overflow-y: auto;
}

.app-shell__context-surface {
  margin: var(--space-lg) var(--space-xl) var(--space-lg);
}
```

- [ ] **Step 2: 验证布局**

`npm run dev`，检查：
- 三列宽度为 220px / 1fr / 280px
- 面板白底、16px 圆角、0.07 边框
- TopBar 44px 高、底部细线
- 全局背景 `#F7F8FA`

- [ ] **Step 3: Commit**

```bash
git add athena-ide/src/App.css
git commit -m "style(ide): rewrite AppShell layout CSS with design tokens"
```

---

### Task 5: 组件样式 — 侧边栏 + 对话区

**Files:**
- Modify: `athena-ide/src/App.css`（追加样式）

**Interfaces:**
- Consumes: CSS 变量 from Task 3, layout from Task 4
- Produces: `.session-sidebar`, `.conversation-pane`, `.message-bubble`, `.composer` 样式

- [ ] **Step 1: 在 App.css 末尾追加侧边栏样式**

```css
/* ===== Session Sidebar ===== */
.session-sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  padding: var(--space-md);
}

.session-sidebar__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px var(--space-sm) 10px;
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--space-sm);
}

.session-sidebar__title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.05em;
}

.session-sidebar__list {
  flex: 1;
  list-style: none;
  margin: 0;
  padding: 0;
  min-height: 0;
  overflow-y: auto;
}

.session-sidebar__item {
  position: relative;
  padding: 10px var(--space-md);
  margin-bottom: var(--space-xs);
  border-radius: 10px;
  cursor: pointer;
  font-size: 13px;
  line-height: 1.4;
  color: var(--text-secondary);
  font-weight: 400;
  transition: background 160ms ease, color 160ms ease;
}

.session-sidebar__item::before {
  content: "";
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 0;
  border-radius: 0 3px 3px 0;
  background: var(--accent);
  transition: height 160ms ease;
}

.session-sidebar__item:hover {
  background: var(--surface-elevated);
  color: var(--text-primary);
}

.session-sidebar__item:hover::before {
  height: 16px;
}

.session-sidebar__item--active {
  background: var(--accent-subtle);
  color: var(--accent);
  font-weight: 500;
}

.session-sidebar__item--active::before {
  height: 20px;
}

.session-sidebar__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
}

.session-sidebar__footer {
  padding: var(--space-sm) 0 0;
  border-top: 1px solid var(--border);
  margin-top: var(--space-sm);
}

.session-sidebar__new {
  width: 100%;
  padding: var(--space-sm);
  border-radius: 10px;
  border: 1px dashed var(--border);
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: background 160ms ease, color 160ms ease;
}

.session-sidebar__new:hover {
  background: var(--surface-elevated);
  color: var(--text-primary);
}

/* ===== Conversation Pane ===== */
.conversation-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.conversation-pane__header {
  padding: var(--space-xl) 24px var(--space-md);
  border-bottom: 1px solid var(--border);
}

.conversation-pane__title {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  line-height: 1.3;
  color: var(--text-primary);
}

.conversation-pane__subtitle {
  margin: var(--space-xs) 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}

/* ===== Message List ===== */
.message-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-lg) 24px;
}

/* ===== Message Bubble ===== */
.message-bubble {
  padding: var(--space-md) var(--space-lg);
  margin-bottom: var(--space-md);
  border-radius: var(--radius-bubble);
  font-size: 14px;
  line-height: 1.55;
  font-weight: 400;
}

.message-bubble--user {
  background: rgba(37, 99, 235, 0.05);
  border: 1px solid rgba(37, 99, 235, 0.12);
}

.message-bubble--athena {
  background: var(--surface-muted);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

/* ===== Composer ===== */
.composer {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  padding: var(--space-md) 24px var(--space-xl);
  border-top: 1px solid var(--border);
}

.composer__input {
  width: 100%;
  padding: var(--space-md) 14px;
  border-radius: var(--radius-btn);
  border: 1px solid transparent;
  background: var(--surface-muted);
  color: var(--text-primary);
  resize: none;
  font-size: 14px;
  line-height: 1.5;
  font-family: var(--font-sans);
  transition: border-color 200ms ease;
}

.composer__input:focus {
  outline: none;
  border-color: var(--border-focus);
}

.composer__send {
  align-self: flex-end;
  padding: 6px var(--space-xl);
  height: 34px;
  border-radius: var(--radius-btn);
  border: none;
  background: var(--accent);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 160ms ease;
}

.composer__send:hover:not(:disabled) {
  background: var(--accent-hover);
}

.composer__send:disabled {
  opacity: 0.4;
  cursor: default;
}
```

- [ ] **Step 2: 验证侧边栏 + 对话区样式**

`npm run dev`，检查：
- 侧边栏激活项蓝字浅蓝底，左侧蓝色竖条
- 非激活项 hover 浅灰底 + 蓝色短竖条浮现
- 新建按钮 dashed 边框
- 用户气泡浅蓝底，Athena 气泡浅灰底
- 输入框默认无边框，聚焦蓝边框
- 发送按钮蓝色实心

- [ ] **Step 3: Commit**

```bash
git add athena-ide/src/App.css
git commit -m "style(ide): add sidebar and conversation pane styles"
```

---

### Task 6: 组件样式 — 右栏 + 卡片 + 详情层 + 按钮

**Files:**
- Modify: `athena-ide/src/App.css`（追加样式）

**Interfaces:**
- Consumes: CSS 变量 from Task 3
- Produces: `.right-rail`, `.summary-card`, `.card`, `.context-surface`, button system 样式

- [ ] **Step 1: 在 App.css 末尾追加右栏和卡片样式**

```css
/* ===== Right Rail ===== */
.right-rail {
  padding: var(--space-lg);
}

/* ===== Summary Card ===== */
.summary-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  padding: var(--space-md) 14px;
  margin-bottom: var(--space-sm);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  background: var(--surface);
  color: var(--text-primary);
  cursor: pointer;
  text-align: left;
  width: 100%;
  transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
}

.summary-card span {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.04em;
}

.summary-card strong {
  font-size: 15px;
  font-weight: 600;
}

.summary-card small {
  font-size: 12px;
  color: var(--text-secondary);
}

.summary-card:hover {
  transform: translateY(-1px);
  border-color: var(--border-hover);
  background: var(--surface-elevated);
}

/* ===== Generic Card ===== */
.card {
  padding: var(--space-lg);
  margin-bottom: var(--space-md);
  border-radius: var(--radius-card);
  border: 1px solid var(--border);
  font-size: 14px;
}

.card__title {
  margin: 0 0 var(--space-sm);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.04em;
}

.card__body {
  margin: 0 0 var(--space-sm);
}

.card__action {
  padding: 6px 18px;
  border-radius: var(--radius-btn);
  border: 1px solid rgba(37, 99, 235, 0.25);
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 160ms ease;
}

.card__action:hover {
  background: rgba(37, 99, 235, 0.16);
}

.card--action {
  border-color: rgba(37, 99, 235, 0.14);
}

.card--error {
  border-color: rgba(220, 38, 38, 0.22);
  background: rgba(220, 38, 38, 0.04);
}

/* ===== Context Surface ===== */
.context-surface--open {
  overflow-y: auto;
  padding: var(--space-lg) var(--space-xl);
  min-height: 240px;
  max-height: 42vh;
  border-top: 2px solid rgba(37, 99, 235, 0.10);
}

.context-surface__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-lg);
}

.context-surface__header h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}

.context-surface__header button {
  padding: 4px 14px;
  border-radius: var(--space-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: background 160ms ease, color 160ms ease;
}

.context-surface__header button:hover {
  background: var(--surface-elevated);
  color: var(--text-primary);
}

.context-surface--closed {
  padding: var(--space-md) var(--space-xl);
  font-size: 12px;
  color: var(--text-tertiary);
}

/* ===== Detail Panel ===== */
.detail-panel {
  padding: var(--space-sm) 0;
}

.detail-panel h3 {
  margin: 0 0 var(--space-xs);
  font-size: 14px;
  font-weight: 600;
}

.detail-panel p {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
}

/* ===== Button System ===== */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 34px;
  padding: 0 var(--space-lg);
  border-radius: var(--radius-btn);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
  border: none;
}

.btn--primary {
  background: var(--accent);
  color: #fff;
}

.btn--primary:hover {
  background: var(--accent-hover);
}

.btn--secondary {
  background: transparent;
  border: 1px solid rgba(0, 0, 0, 0.10);
  color: var(--text-primary);
}

.btn--secondary:hover {
  background: var(--surface-elevated);
}

.btn--ghost {
  background: transparent;
  color: var(--text-secondary);
}

.btn--ghost:hover {
  background: var(--surface-elevated);
  color: var(--text-primary);
}

.btn--danger {
  color: var(--danger);
  border: 1px solid rgba(220, 38, 38, 0.15);
  background: transparent;
}

.btn--danger:hover {
  background: rgba(220, 38, 38, 0.06);
}
```

- [ ] **Step 2: 验证右栏 + 卡片 + 详情层**

`npm run dev`，检查：
- 右栏卡三行信息：标签/数值/辅助
- 卡片 hover 上浮 1px + 边框变蓝 + 底变浅灰
- 意图预览卡蓝色边框 + 蓝色确认按钮
- 错误卡红色边框 + 浅红底
- 底部详情层展开/收起态正确
- 各按钮类型样式正确

- [ ] **Step 3: Commit**

```bash
git add athena-ide/src/App.css
git commit -m "style(ide): add right-rail, card, context-surface, and button styles"
```

---

### Task 7: 动效 + 侧边栏竖条 + 最后润色

**Files:**
- Modify: `athena-ide/src/App.css`（追加动效和微调）

**Interfaces:**
- Produces: 详情层展开/收起动画、hover 过渡、聚焦过渡

- [ ] **Step 1: 在 App.css 末尾追加动效关键帧**

```css
/* ===== Transitions & Animations ===== */
.context-surface--open {
  animation: context-slide-up 240ms cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes context-slide-up {
  from {
    opacity: 0;
    max-height: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    max-height: 42vh;
    transform: translateY(0);
  }
}

/* smooth scroll for message list */
.message-list {
  scroll-behavior: smooth;
}

/* focus ring — only visible on keyboard focus */
:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}

/* remove focus ring on mouse click */
:focus:not(:focus-visible) {
  outline: none;
}
```

- [ ] **Step 2: 在 styles.css 末尾追加图表色板变量**

```css
:root {
  /* Chart palette — Viridis-derived 6 colors */
  --chart-1: #2563EB;
  --chart-2: #059669;
  --chart-3: #D97706;
  --chart-4: #7C3AED;
  --chart-5: #DB2777;
  --chart-6: #0891B2;
}
```

- [ ] **Step 3: 最后验证 — 全功能走查**

`npm run dev`，逐项确认验收标准：

```bash
# 验收清单：
echo "□ 全局背景 #F7F8FA，面板白底浮在灰底上"
echo "□ Geist 字体已启用"
echo "□ 强调色仅出现在主按钮/链接/选中态/聚焦态"
echo "□ 面板 16px、卡片 12px、按钮 10px 圆角"
echo "□ 侧边栏激活项蓝字浅蓝底 + 蓝色竖条"
echo "□ 侧边栏 hover 灰色底 + 蓝色短竖条浮现"
echo "□ 右栏卡 hover 1px 上浮 + 边框微变色"
echo "□ 底部详情层 240ms 上滑展开"
echo "□ 无重阴影、无霓虹、无弹跳动效"
echo "□ 输入框聚焦蓝边框"
echo "□ 发送按钮蓝色实心"
echo "□ 用户气泡浅蓝底、Athena 气泡浅灰底"
echo "□ 气泡圆角 14px"
```

- [ ] **Step 4: Commit**

```bash
git add athena-ide/src/styles.css athena-ide/src/App.css
git commit -m "style(ide): add transitions, chart palette, and final polish"
```

---

### Task 8: 补充 — TopBar 连接指示灯

**Files:**
- Modify: `athena-ide/src/components/shell/TopBar.tsx`（小改动 — 在 span 中加 dot）

**⚠️ 这是唯一的 .tsx 改动——仅在现有 span 前插入一个 dot 元素。**

- [ ] **Step 1: 在 TopBar 的阶段指示前添加连接灯**

修改现有的 `<span>{phase}</span>` 前插入 dot：

```tsx
export function TopBar({ title, phase, status }: TopBarProps) {
  return (
    <header className="top-bar">
      <div>
        <span className="top-bar__title">{title}</span>
      </div>
      <div className="top-bar__meta">
        <span className="top-bar__dot" />
        <span>{status}</span>
        <span>{phase}</span>
      </div>
    </header>
  );
}
```

- [ ] **Step 2: 在 App.css 中确保 dot 样式存在**（已在 Task 4 中包含 `.top-bar__dot`）

- [ ] **Step 3: 验证连接灯显示为 6px 绿色圆点**

- [ ] **Step 4: Commit**

```bash
git add athena-ide/src/components/shell/TopBar.tsx
git commit -m "style(ide): add connection status dot to TopBar"
```

---

## 验收

全部任务完成后，Athena IDE 界面应呈现：

- **整体**：浅灰幕布 + 白色卡片，Geist 字体，克制冷蓝点缀
- **侧边栏**：带竖条的会话列表，dashed 新建按钮
- **对话区**：双色气泡，蓝色主按钮，聚焦蓝边框输入框
- **右栏**：三行摘要卡，hover 上浮 1px
- **详情层**：240ms 上滑展开，2px 蓝线分界
- **按钮**：Primary/Secondary/Ghost/Danger 四型齐全
- **图表区**：Viridis 6 色可用（通过 CSS 变量 `--chart-1` ~ `--chart-6`）

共 8 个 Task，预计 8-10 次 commit。
