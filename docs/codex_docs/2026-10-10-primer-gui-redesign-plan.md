# Athena GUI 迁移到 GitHub Primer 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `athena-gui` 的界面从自建设计系统整体迁移到 GitHub Primer（`@primer/react` 组件 + `@primer/primitives` token），亮色 Primer Light、暗色 Primer Dark，字体 Mona Sans。

**Architecture:** 根部由 `AppProviders`（亮/暗偏好上下文 + Primer `ThemeProvider` + `BaseStyles`）包裹 `App`。迁移期在 `styles.css` 保留一个「旧 token → Primer 变量」的兼容层，让尚未迁移的旧样式自动使用 Primer 配色；各界面逐个换成 Primer 组件，最后删除兼容层与旧设计系统。外壳改为 GitHub 仓库页结构（顶栏 + `UnderlineNav` 模块标签 + 左侧栏 / 主区 / 非模态右侧栏）。

**Tech Stack:** React 18.3、Vite 5、Vitest 2.1 + jsdom 25 + Testing Library、`@primer/react` 38.40、`@primer/primitives` 11.10、`@primer/octicons-react` 19.40、`@fontsource-variable/mona-sans` 5.3；保留 ReactFlow 11、Recharts 3、react-markdown 10。

**Spec:** `docs/architecture/2026-10-10-primer-gui-redesign-design.md`

## Global Constraints

- 工作目录：所有 `npm` 命令在 `athena-gui/` 下执行（或用 `npm --prefix athena-gui`）；不改 Python、Rust、网关协议、hooks 与 `lib/` 业务逻辑。
- 只用 `@primer/react` 主入口导出的组件；**不得**从 `@primer/react/experimental`、`@primer/react/deprecated` 导入（`DataTable`、`Blankslate`、`InlineMessage` 仅在 experimental，按本计划的替代实现处理）。
- 组件与 CSS Module 中不得出现十六进制或 `rgba(` 字面色值；颜色、尺寸、圆角、字号一律引用 Primer CSS 变量（`--bgColor-*`、`--fgColor-*`、`--borderColor-*`、`--borderWidth-thin`、`--borderRadius-*`、`--base-size-*`、`--text-*`、`--fontStack-*`、`--base-text-weight-*`、`--shadow-*`、`--data-*-color-emphasis`）。
- 纯图标按钮一律用 `IconButton` 并提供 `aria-label`。
- localStorage 主题 key 保持 `athena-theme`，值 `light` / `dark`。
- 测试：新行为同时写正常路径与异常/降级路径测试；不依赖网络与真实模型；`npm test` 必须零失败、零跳过。
- 代码风格：匹配所在文件；注释与 docstring 以中文为主；新增导出函数/组件带一行说明「做什么」的注释。
- 提交：Conventional Commits，scope 用 `gui`，提交说明写中文，例如 `feat(gui): 接入 Primer 主题与图标`；**不加** `Co-Authored-By` 等协作者行（用户规则）。每个任务只暂存本任务列出的文件。

## Review Focus

1. 后端返回未知的假设/实验状态字符串 → 显示为 `secondary` 灰色标签、原文照显，不抛错（Task 2 测试 `hypothesisVariant` 与 `StatusBadge` 的未知值）。
2. 面板请求失败（RPC 抛错）→ 面板内出现 `Flash` 错误条，已渲染的数据不被清空，刷新按钮仍可用（Task 8 `ExperimentLog` 测试）。
3. 超长工作区路径或会话标题 → 顶栏面包屑截断显示，完整文本通过 `title` 可见（Task 3 测试面包屑 `title`）。
4. 两个弹窗的 Esc 语义不同：设置弹窗 Esc 关闭；人工确认弹窗 Esc 只在 `allow_skip` 为真时等同 Skip，否则不做任何事（Task 6、Task 7 测试）。
5. 窄窗口下模块标签放不下 → `UnderlineNav` 收进「More items」菜单，所有模块仍可到达（jsdom 无布局，列入 Task 12 实机核对第 4 项）。

## 实测依据（2026-10-10，隔离项目中以 jsdom 渲染 Primer 38.40.1 得到）

写测试与实现时以此为准，不要凭记忆假设 Primer 的 DOM：

| 组件 | jsdom 中的实际表现 |
|---|---|
| `@primer/react` 在 Vitest 中 | ESM 产物直接 import `.css`，必须配置 `test.server.deps.inline: [/@primer\/react/]`，否则报 `Unknown file extension ".css"` |
| `UnderlineNav.Item as="button"` | 渲染 `<button aria-current>`；带 `counter` 时可访问名为「研究树5 (5)」，按名查找要用正则 `/^研究树/`；另有一个「More items」按钮 |
| `NavList.Item as="button"` | `<button aria-current="page">`；`disabled` 为原生属性 |
| `Button` / `IconButton` | `disabled` 为原生属性（`toBeDisabled()` 可用）；`IconButton` 可访问名来自 `aria-label` |
| `SegmentedControl.Button` | `<button aria-pressed>` |
| `Checkbox` + `FormControl` | `role=checkbox`；`FormControl disabled` 传递给 Checkbox；`FormControl.Caption` 自动写入 `aria-describedby`，id 为 `<FormControl id>-caption` |
| `ActionList.Item` | `li > button`，点击触发 `onSelect`；`disabled` 为 `aria-disabled="true"`（不是原生 disabled）；`title` 透传到按钮 |
| `ActionList.TrailingAction` | 独立按钮，可访问名为 `label` |
| `ActionList role="listbox"` + `Item role="option"` | 选项是 `li[role=option]`，roving tabindex，ArrowDown 移动焦点；`LeadingVisual` 内 `aria-hidden` 文本不进入可访问名 |
| `Dialog` | `div[role=dialog]`，可访问名为标题；Esc 调用 `onClose("escape")`；**不传 `initialFocusRef` 时焦点不进入弹窗**；关闭后焦点归还是异步的（测试用 `await waitFor`）；弹窗经 Portal 渲染，`render()` 返回的 `container` 里查不到弹窗内容，要用 `screen` 或 `document` |
| `ThemeProvider` | 在包裹 div 上写 `data-color-mode` / `data-light-theme` / `data-dark-theme`，不写 `<html>`；Portal 渲染在 `BaseStyles` 内 |
| `PageHeader.Title as="h3"` | 渲染三级标题 |
| `@fontsource-variable/mona-sans` | 注册的字体族名是 `"Mona Sans Variable"`，而 Primer 字体栈第一项是 `"Mona Sans VF"`，需在 `styles.css` 覆盖 `--fontStack-sansSerif` |
| `BaseStyles` 字体 | `font-family: var(--BaseStyles-fontFamily, var(--fontStack-system, …))`，默认是系统字体栈，需设置 `--BaseStyles-fontFamily` |

## 文件结构

新建（`athena-gui/src/` 下）：

| 文件 | 职责 |
|---|---|
| `hooks/useTheme.tsx`（替换 `useTheme.ts`） | 亮/暗偏好：读取/持久化、写 `<html>` 上的 Primer 属性、`ThemeModeProvider` 上下文 |
| `components/common/AppProviders.tsx` | 根部 Provider：`ThemeModeProvider` + Primer `ThemeProvider` + `BaseStyles` |
| `components/common/hypothesisStatus.ts` | 假设状态 → Primer `Label` variant 与图节点边框色（唯一映射点） |
| `components/common/Panel.tsx` + `.module.css` | 数据面板容器：`PageHeader` 标题区 + 操作区 + 内容 |
| `components/common/KeyValueList.tsx` + `.module.css` | 键值列表 `<dl>` |
| `components/common/Section.tsx` + `.module.css` | 带边框的分组容器（替代 `.card`） |
| `components/common/CodeBlock.tsx` + `.module.css` | 等宽代码块（替代 `.code-block`） |
| `components/common/EmptyState.module.css` | Blankslate 规格的空状态样式 |
| `components/common/FlowNodeCard.tsx` + `.module.css` | 研究树/假设图共用的 ReactFlow 节点卡片与图容器 |
| `components/shell/ModuleNav.tsx` + `.module.css` | `UnderlineNav` 模块标签栏（替代 `FunctionRail`） |
| 若干 `*.module.css` | 各组件改用 Primer 变量后的样式 |

删除：`hooks/useTheme.ts`、`components/shell/FunctionRail.tsx` / `.module.css`、`components/shell/AthenaWordmark.tsx`、`components/shell/WorkspaceSwitcher.tsx` / `.module.css`（无引用的死组件）、`components/__tests__/function-rail.test.tsx`（由 `ModuleNav` 测试替代）。

保持接口不变：`components/common/Icon.tsx` 的 `IconName` / `<Icon name size className />`；`useTheme()` 的 `{ mode, cycleMode }`；所有组件的 props。

## Pre-flight（执行前一次性完成）

- [ ] **P1：确认工作区基线。** 当前 worktree 有两组未提交改动：(a) 早先 Tokyo Night 配色对 8 个文件的修改（`athena-gui/src/styles.css`、`SettingsPanel.module.css`、`conversation/Composer.module.css`、`conversation/MessageList.module.css`、`conversation/WelcomeHero.module.css`、`shell/AppShell.module.css`、`shell/ContextSidebar.module.css`、`shell/HumanRequestDialog.module.css`）；(b) 根 `.gitignore` 新增 `CLAUDE.md`。按用户决定处理：Tokyo Night 改动已被本方案取代——**经用户在对话中明确同意后**执行 `git restore` 丢弃 (a)，否则先单独提交 (a)；(b) 与本计划无关，不要暂存。本计划所有步骤对两种基线都成立。

Run: `git status --short`
Expected: 只剩用户决定保留的改动，以及本计划文档与 spec。

- [ ] **P2：基线测试。**

Run: `npm --prefix athena-gui test`
Expected: 191 个测试通过；唯一失败的测试文件是 `scripts/dev.test.cjs`（「No test suite found」，它是 `node:test` 文件，Task 1 会把它排除出 Vitest）。

---

### Task 1: 接入 Primer（依赖、测试环境、主题上下文、字体、图标）

**Files:**
- Modify: `athena-gui/package.json`、`athena-gui/package-lock.json`（经 `npm install` 生成）
- Modify: `athena-gui/vitest.config.ts`
- Modify: `athena-gui/src/test/setup.ts`
- Modify: `athena-gui/src/test/render.tsx`
- Delete: `athena-gui/src/hooks/useTheme.ts`
- Create: `athena-gui/src/hooks/useTheme.tsx`
- Create: `athena-gui/src/components/common/AppProviders.tsx`
- Modify: `athena-gui/src/App.tsx`
- Modify: `athena-gui/src/main.tsx`
- Modify: `athena-gui/src/styles.css`（只替换文件开头的 token 区块）
- Modify: `athena-gui/src/components/common/Icon.tsx`
- Modify: `athena-gui/src/components/shell/ThemeToggle.tsx`
- Create: `athena-gui/src/hooks/__tests__/useTheme.test.tsx`
- Create: `athena-gui/src/components/common/__tests__/Icon.test.tsx`
- Modify: `athena-gui/src/components/__tests__/common-contracts.test.tsx`

**Interfaces:**
- Produces: `useTheme(): { mode: "light" | "dark"; cycleMode(): void }`（必须在 `ThemeModeProvider` 内调用，否则抛出含 `ThemeModeProvider` 字样的错误）；`getStoredTheme(): Theme`；`applyTheme(theme: Theme): void`；`ThemeModeProvider`；`AppProviders({ children })`；`Icon` 与 `IconName` 接口不变，另导出 `ICON_NAMES: IconName[]`；`renderUi(ui)` 现在会用 `AppProviders` 包裹。
- Produces（CSS）：`styles.css` 中的兼容层让旧 token（`--surface`、`--accent`、`--text-primary` 等）全部指向 Primer 变量，后续任务的旧样式无需改动即可跟随主题。

- [ ] **Step 1: 安装依赖**

Run（在仓库根目录）:
```bash
npm --prefix athena-gui install @primer/react@^38.40.1 @primer/primitives@^11.10.0 @primer/octicons-react@^19.40.0 @fontsource-variable/mona-sans@^5.3.0 react-is@^18.3.1
```
Expected: 成功退出；`athena-gui/package.json` 的 `dependencies` 出现这 5 项。`react-is` 是 `@primer/react` 的必需 peer 依赖。此步不移除 `lucide-react` 与 Geist（Task 11 处理）。

- [ ] **Step 2: 配置 Vitest**

把 `athena-gui/vitest.config.ts` 整体替换为：

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // scripts/dev.test.cjs 是 node:test 用例，不归 Vitest 运行。
    include: ["src/**/*.test.{ts,tsx}"],
    // @primer/react 的 ESM 产物直接 import .css，需交给 Vite 转换，不能交给 Node 加载。
    server: { deps: { inline: [/@primer\/react/] } },
  },
});
```

- [ ] **Step 3: 补齐 jsdom 缺失的浏览器 API**

把 `athena-gui/src/test/setup.ts` 整体替换为：

```ts
import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Simulate the Tauri runtime for all tests so invoke/listen work.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(window as any).__TAURI_INTERNALS__ = {};

// Primer React 依赖的浏览器 API 在 jsdom 中缺失。内容与 @primer/react/test-helpers
// 等价，但那份文件使用 Jest 全局变量，无法在 Vitest 中导入。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class IntersectionObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const globals = globalThis as any;
globals.ResizeObserver ??= ResizeObserverStub;
globals.IntersectionObserver ??= IntersectionObserverStub;
globals.CSS ??= {};
globals.CSS.supports ??= () => false;
globals.CSS.escape ??= (value: string) => value;
// Primer v38 的 Dialog 渲染为 div[role=dialog]，不需要 HTMLDialogElement 补丁。
globals.Element.prototype.scrollIntoView ??= function scrollIntoView() {};
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}
```

- [ ] **Step 4: 写主题上下文的失败测试**

Create `athena-gui/src/hooks/__tests__/useTheme.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AppProviders } from "../../components/common/AppProviders";
import { ThemeToggle } from "../../components/shell/ThemeToggle";
import { getStoredTheme, useTheme } from "../useTheme";

function stubSystemDark(matches: boolean) {
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe("useTheme", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("syncs Primer color-mode attributes on <html> and the ThemeProvider, and persists the choice", () => {
    localStorage.setItem("athena-theme", "light");
    render(
      <AppProviders>
        <ThemeToggle />
      </AppProviders>,
    );

    const root = document.documentElement;
    expect(root.getAttribute("data-color-mode")).toBe("light");
    expect(root.getAttribute("data-light-theme")).toBe("light");
    expect(root.getAttribute("data-dark-theme")).toBe("dark");

    fireEvent.click(screen.getByRole("button", { name: "主题：深色" }));

    expect(root.getAttribute("data-color-mode")).toBe("dark");
    expect(localStorage.getItem("athena-theme")).toBe("dark");
    expect(document.querySelector('[data-component="ThemeProvider"]')).toHaveAttribute("data-color-mode", "dark");
    expect(screen.getByRole("button", { name: "主题：浅色" })).toBeInTheDocument();
  });

  it("falls back to the system preference when the stored value is missing or invalid", () => {
    localStorage.setItem("athena-theme", "purple");
    stubSystemDark(true);
    expect(getStoredTheme()).toBe("dark");

    localStorage.removeItem("athena-theme");
    stubSystemDark(false);
    expect(getStoredTheme()).toBe("light");
  });

  it("throws a clear error when used outside ThemeModeProvider", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    function Orphan() {
      useTheme();
      return null;
    }
    expect(() => render(<Orphan />)).toThrow(/ThemeModeProvider/);
  });
});
```

- [ ] **Step 5: 运行，确认失败**

Run: `npm --prefix athena-gui test -- src/hooks/__tests__/useTheme.test.tsx`
Expected: FAIL，报找不到 `../../components/common/AppProviders` 或 `getStoredTheme` 未导出。

- [ ] **Step 6: 实现主题上下文**

删除 `athena-gui/src/hooks/useTheme.ts`，新建 `athena-gui/src/hooks/useTheme.tsx`：

```tsx
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

const STORAGE_KEY = "athena-theme";

export type Theme = "light" | "dark";

interface ThemeContextValue {
  mode: Theme;
  cycleMode(): void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function systemPrefersDark(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

/** 读取已保存的主题；缺失或非法时回落到系统偏好。 */
export function getStoredTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return systemPrefersDark() ? "dark" : "light";
}

/** 把主题写到 <html>：Primer 主题 CSS 按这三个属性生效，页面背景随之切换。 */
export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.setAttribute("data-color-mode", theme);
  root.setAttribute("data-light-theme", "light");
  root.setAttribute("data-dark-theme", "dark");
}

// 在 React 挂载前应用已保存/系统主题，避免首屏闪烁。
applyTheme(getStoredTheme());

/** 亮/暗偏好上下文：持久化到 localStorage 并同步到 <html>。 */
export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Theme>(getStoredTheme);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, mode);
    applyTheme(mode);
  }, [mode]);

  const cycleMode = useCallback(() => {
    setMode((prev) => (prev === "light" ? "dark" : "light"));
  }, []);

  return <ThemeContext.Provider value={{ mode, cycleMode }}>{children}</ThemeContext.Provider>;
}

/** 当前亮/暗模式与切换函数；必须在 ThemeModeProvider 内使用。 */
export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) {
    throw new Error("useTheme 必须在 ThemeModeProvider 内使用");
  }
  return value;
}
```

Create `athena-gui/src/components/common/AppProviders.tsx`:

```tsx
import type { ReactNode } from "react";
import { BaseStyles, ThemeProvider } from "@primer/react";
import { ThemeModeProvider, useTheme } from "../../hooks/useTheme";

function PrimerTheme({ children }: { children: ReactNode }) {
  const { mode } = useTheme();
  return (
    <ThemeProvider colorMode={mode === "dark" ? "night" : "day"} dayScheme="light" nightScheme="dark">
      <BaseStyles>{children}</BaseStyles>
    </ThemeProvider>
  );
}

/** 应用根部 Provider：亮/暗偏好 + Primer ThemeProvider + BaseStyles。 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeModeProvider>
      <PrimerTheme>{children}</PrimerTheme>
    </ThemeModeProvider>
  );
}
```

把 `athena-gui/src/components/shell/ThemeToggle.tsx` 整体替换为：

```tsx
import { IconButton } from "@primer/react";
import { MoonIcon, SunIcon } from "@primer/octicons-react";
import { useTheme } from "../../hooks/useTheme";

/** 顶栏亮/暗主题切换按钮。 */
export function ThemeToggle() {
  const { mode, cycleMode } = useTheme();
  const toDark = mode === "light";
  return (
    <IconButton
      icon={toDark ? MoonIcon : SunIcon}
      variant="invisible"
      aria-label={toDark ? "主题：深色" : "主题：浅色"}
      onClick={cycleMode}
    />
  );
}
```

`App.tsx`：把现有的 `function App() { ... }` 改名为 `function AppContent()`（函数体不变），并在其后新增：

```tsx
/** 根应用：主题 Provider + 工作区选择门 + 统一研究工作区。 */
function App() {
  return (
    <AppProviders>
      <AppContent />
    </AppProviders>
  );
}
```

同时在 `App.tsx` 顶部加 `import { AppProviders } from "./components/common/AppProviders";`，并把 `AppContent` 上方原来的注释改为 `/** 工作区选择门 + 统一研究工作区。 */`。`export default App;` 保持不变。

把 `athena-gui/src/test/render.tsx` 整体替换为：

```tsx
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { AppProviders } from "../components/common/AppProviders";

/** 在应用根 Provider（主题 + Primer）内渲染组件。 */
export function renderUi(ui: ReactElement) {
  return render(ui, { wrapper: AppProviders });
}
```

- [ ] **Step 7: 运行主题测试，确认通过**

Run: `npm --prefix athena-gui test -- src/hooks/__tests__/useTheme.test.tsx`
Expected: 3 passed。

- [ ] **Step 8: 写图标映射的失败测试**

Create `athena-gui/src/components/common/__tests__/Icon.test.tsx`:

```tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { ICON_NAMES, Icon } from "../Icon";

afterEach(() => vi.restoreAllMocks());

describe("Icon", () => {
  it("renders an SVG for every registered name", () => {
    expect(ICON_NAMES.length).toBeGreaterThan(0);
    for (const name of ICON_NAMES) {
      const { container, unmount } = render(<Icon name={name} size={16} />);
      expect(container.querySelector("svg"), name).not.toBeNull();
      unmount();
    }
  });

  it("rejects unknown names at the type level and at runtime", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    // @ts-expect-error 未注册的图标名必须在类型层面被拒绝
    expect(() => render(<Icon name="not-an-icon" />)).toThrow();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/common/__tests__/Icon.test.tsx`
Expected: FAIL，`ICON_NAMES` 未导出。

- [ ] **Step 9: 把 Icon 换成 Octicons**

把 `athena-gui/src/components/common/Icon.tsx` 整体替换为：

```tsx
import {
  BeakerIcon,
  BookIcon,
  BugIcon,
  ChecklistIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  CodeIcon,
  ColumnsIcon,
  CommentDiscussionIcon,
  CommentIcon,
  CpuIcon,
  CreditCardIcon,
  DatabaseIcon,
  DiffIcon,
  DownloadIcon,
  EyeIcon,
  FileCodeIcon,
  FileDirectoryIcon,
  FileDirectoryOpenFillIcon,
  FileIcon,
  FilterIcon,
  GearIcon,
  GitBranchIcon,
  GlobeIcon,
  GoalIcon,
  GraphIcon,
  HistoryIcon,
  HubotIcon,
  InfoIcon,
  KeyIcon,
  LightBulbIcon,
  LinkExternalIcon,
  ListUnorderedIcon,
  LockIcon,
  LogIcon,
  MeterIcon,
  MoonIcon,
  NumberIcon,
  PackageIcon,
  PaperAirplaneIcon,
  PauseIcon,
  PeopleIcon,
  PlayIcon,
  PulseIcon,
  RocketIcon,
  SearchIcon,
  ShieldCheckIcon,
  SidebarCollapseIcon,
  SidebarExpandIcon,
  SparkleIcon,
  StackIcon,
  StarIcon,
  SunIcon,
  SyncIcon,
  TableIcon,
  TelescopeIcon,
  TerminalIcon,
  ToolsIcon,
  TrashIcon,
  TrophyIcon,
  UploadIcon,
  WebhookIcon,
  WorkflowIcon,
  XIcon,
  ZapIcon,
} from "@primer/octicons-react";

/** 语义图标注册表：名称 → Octicon 组件。没有一一对应的名称选用最接近的 Octicon。 */
const ICONS = {
  metrics: GraphIcon,
  tree: GitBranchIcon,
  network: WorkflowIcon,
  algorithms: NumberIcon,
  experiments: BeakerIcon,
  llm: CommentDiscussionIcon,
  settings: GearIcon,
  search: SearchIcon,
  wallet: CreditCardIcon,
  trophy: TrophyIcon,
  report: FileIcon,
  sun: SunIcon,
  moon: MoonIcon,
  sparkles: SparkleIcon,
  wrench: ToolsIcon,
  diff: DiffIcon,
  files: FileDirectoryIcon,
  folder: FileDirectoryOpenFillIcon,
  log: LogIcon,
  send: PaperAirplaneIcon,
  loader: SyncIcon,
  chat: CommentIcon,
  chart: TelescopeIcon,
  clock: ClockIcon,
  close: XIcon,
  chevronDown: ChevronDownIcon,
  chevronLeft: ChevronLeftIcon,
  chevronRight: ChevronRightIcon,
  panelLeft: SidebarExpandIcon,
  panelLeftClose: SidebarCollapseIcon,
  trash: TrashIcon,
  // 扩展图标：数据、工具、状态与通用语义
  activity: PulseIcon,
  atom: CpuIcon,
  book: BookIcon,
  box: PackageIcon,
  brain: HubotIcon,
  bug: BugIcon,
  clipboard: ChecklistIcon,
  columns: ColumnsIcon,
  database: DatabaseIcon,
  download: DownloadIcon,
  external: LinkExternalIcon,
  eye: EyeIcon,
  fileCode: FileCodeIcon,
  filter: FilterIcon,
  gauge: MeterIcon,
  globe: GlobeIcon,
  hammer: ToolsIcon,
  history: HistoryIcon,
  info: InfoIcon,
  key: KeyIcon,
  layers: StackIcon,
  lightbulb: LightBulbIcon,
  list: ListUnorderedIcon,
  lock: LockIcon,
  microscope: TelescopeIcon,
  pause: PauseIcon,
  play: PlayIcon,
  refresh: SyncIcon,
  rocket: RocketIcon,
  shield: ShieldCheckIcon,
  sigma: NumberIcon,
  squareTerminal: TerminalIcon,
  star: StarIcon,
  table: TableIcon,
  target: GoalIcon,
  upload: UploadIcon,
  users: PeopleIcon,
  variable: CodeIcon,
  webhook: WebhookIcon,
  workflow: WorkflowIcon,
  zap: ZapIcon,
} as const;

export type IconName = keyof typeof ICONS;

/** 全部已注册的图标名（测试用于逐个校验可渲染）。 */
export const ICON_NAMES = Object.keys(ICONS) as IconName[];

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
}

/** 渲染一个已注册的 Octicon。 */
export function Icon({ name, size = 16, className }: IconProps) {
  const Component = ICONS[name];
  return <Component size={size} className={className} />;
}
```

Run: `npm --prefix athena-gui test -- src/components/common/__tests__/Icon.test.tsx`
Expected: 2 passed。

- [ ] **Step 10: 引入 Primer 样式与字体，建立兼容层**

把 `athena-gui/src/main.tsx` 整体替换为：

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import "@primer/primitives/dist/css/primitives.css";
import "@primer/primitives/dist/css/functional/themes/light.css";
import "@primer/primitives/dist/css/functional/themes/dark.css";
import "@fontsource-variable/mona-sans";
import "./hooks/useTheme"; // apply persisted/system theme before first paint
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

在 `athena-gui/src/styles.css` 中，把**从文件第一行开始、直到 `:root[data-theme="dark"] { ... }` 区块的右花括号为止**的全部内容（文件头注释、`:root` token 区块、暗色 token 区块）替换为下面的内容；文件其余部分保持不变：

```css
/* Athena GUI 全局样式。颜色、尺寸、字体全部来自 GitHub Primer（@primer/primitives）。 */

:root {
  /* fontsource 注册的族名是 "Mona Sans Variable"，Primer 字体栈第一项是 "Mona Sans VF"，此处前置补齐。 */
  --fontStack-sansSerif: "Mona Sans Variable", "Mona Sans VF", -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans Backtick Fix", "Noto Sans", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  --BaseStyles-fontFamily: var(--fontStack-sansSerif);
}

/* 迁移期兼容层：旧 token 指向 Primer 变量，尚未迁移的旧样式自动跟随亮/暗主题。
   各界面迁移完成后由 Task 11 整体删除。 */
:root {
  --bg: var(--bgColor-default);
  --surface: var(--bgColor-default);
  --surface-elevated: var(--bgColor-muted);
  --surface-muted: var(--bgColor-muted);
  --border: var(--borderColor-muted);
  --border-strong: var(--borderColor-default);
  --border-hover: var(--borderColor-accent-muted);
  --border-focus: var(--borderColor-accent-emphasis);
  --border-input: var(--control-borderColor-rest);
  --accent: var(--fgColor-accent);
  --accent-hover: var(--fgColor-accent);
  --accent-subtle: var(--bgColor-accent-muted);
  --accent-soft: var(--borderColor-accent-muted);
  --accent-subtle-hover: var(--bgColor-accent-muted);
  --on-accent: var(--fgColor-onEmphasis);
  --text-primary: var(--fgColor-default);
  --text-secondary: var(--fgColor-muted);
  --text-tertiary: var(--fgColor-muted);
  --text-placeholder: var(--control-fgColor-placeholder);
  --success: var(--fgColor-success);
  --success-subtle: var(--bgColor-success-muted);
  --warning: var(--fgColor-attention);
  --warning-subtle: var(--bgColor-attention-muted);
  --danger: var(--fgColor-danger);
  --danger-subtle: var(--bgColor-danger-muted);
  --danger-border: var(--borderColor-danger-muted);
  --info: var(--fgColor-accent);
  --info-subtle: var(--bgColor-accent-muted);
  --selection: var(--selection-bgColor);
  --overlay: var(--overlay-backdrop-bgColor);
  --code-bg: var(--bgColor-muted);
  --switch-thumb: var(--bgColor-default);
  --scrollbar-thumb: var(--borderColor-default);
  --scrollbar-thumb-hover: var(--fgColor-muted);
  --shadow-xs: var(--shadow-resting-xsmall);
  --shadow-sm: var(--shadow-resting-small);
  --shadow-md: var(--shadow-floating-small);
  --shadow-lg: var(--shadow-floating-large);
  --font-sans: var(--fontStack-sansSerif);
  --font-mono: var(--fontStack-monospace);
  --radius-xs: var(--borderRadius-small);
  --radius-sm: var(--borderRadius-medium);
  --radius-md: var(--borderRadius-medium);
  --radius-card: var(--borderRadius-medium);
  --radius-panel: var(--borderRadius-large);
  --radius-bubble: var(--borderRadius-medium);
  --radius-btn: var(--borderRadius-medium);
  --radius-dialog: var(--borderRadius-large);
  --space-xs: var(--base-size-4);
  --space-sm: var(--base-size-8);
  --space-md: var(--base-size-12);
  --space-lg: var(--base-size-16);
  --space-xl: var(--base-size-20);
  --space-2xl: var(--base-size-28);
  --ease: cubic-bezier(0.4, 0, 0.2, 1);
  --dur: 160ms;
  --chart-1: var(--data-blue-color-emphasis);
  --chart-2: var(--data-green-color-emphasis);
  --chart-3: var(--data-orange-color-emphasis);
  --chart-4: var(--data-purple-color-emphasis);
  --chart-5: var(--data-pink-color-emphasis);
  --chart-6: var(--data-teal-color-emphasis);
  --status-supported-bg: var(--bgColor-success-muted);
  --status-supported-border: var(--borderColor-success-emphasis);
  --status-supported-text: var(--fgColor-success);
  --status-refuted-bg: var(--bgColor-danger-muted);
  --status-refuted-border: var(--borderColor-danger-emphasis);
  --status-refuted-text: var(--fgColor-danger);
  --status-rejected-bg: var(--bgColor-neutral-muted);
  --status-rejected-border: var(--borderColor-emphasis);
  --status-rejected-text: var(--fgColor-muted);
  --status-proposed-bg: var(--bgColor-accent-muted);
  --status-proposed-border: var(--borderColor-accent-emphasis);
  --status-proposed-text: var(--fgColor-accent);
}
```

确认 `data-theme` 已无引用：

Run: `grep -rn "data-theme\|dataset.theme" athena-gui/src athena-gui/index.html`
Expected: 只剩 `src/components/__tests__/common-contracts.test.tsx` 中的两行（下一步修改）。

- [ ] **Step 11: 更新共享契约测试**

在 `athena-gui/src/components/__tests__/common-contracts.test.tsx` 中，把 `it("uses the shared icon style and persists both theme transitions", ...)` 整个测试替换为：

```tsx
  it("toggles the theme from the shared icon button and persists both transitions", () => {
    localStorage.setItem("athena-theme", "light");
    renderUi(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button", { name: "主题：深色" }));
    expect(document.documentElement.getAttribute("data-color-mode")).toBe("dark");
    expect(localStorage.getItem("athena-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "主题：浅色" }));
    expect(document.documentElement.getAttribute("data-color-mode")).toBe("light");
    expect(localStorage.getItem("athena-theme")).toBe("light");
  });
```

- [ ] **Step 12: 全量测试与构建**

Run: `npm --prefix athena-gui test`
Expected: 全部通过（原 191 个 + 本任务新增 5 个），不再出现 `scripts/dev.test.cjs` 失败。

Run: `npm --prefix athena-gui run build`
Expected: `tsc` 与 `vite build` 均成功。

- [ ] **Step 13: 提交**

```bash
git add athena-gui/package.json athena-gui/package-lock.json athena-gui/vitest.config.ts athena-gui/src/test/setup.ts athena-gui/src/test/render.tsx athena-gui/src/hooks/useTheme.ts athena-gui/src/hooks/useTheme.tsx athena-gui/src/hooks/__tests__/useTheme.test.tsx athena-gui/src/components/common/AppProviders.tsx athena-gui/src/App.tsx athena-gui/src/main.tsx athena-gui/src/styles.css athena-gui/src/components/common/Icon.tsx athena-gui/src/components/common/__tests__/Icon.test.tsx athena-gui/src/components/shell/ThemeToggle.tsx athena-gui/src/components/__tests__/common-contracts.test.tsx
git commit -m "feat(gui): 接入 Primer 主题、字体与 Octicons 图标"
```

---

### Task 2: 共享部件（状态映射、标签、空状态、面板、键值列表、分组、代码块、错误边界）

**Files:**
- Create: `athena-gui/src/components/common/hypothesisStatus.ts`
- Modify: `athena-gui/src/components/common/StatusBadge.tsx`
- Modify: `athena-gui/src/components/common/EmptyState.tsx`
- Create: `athena-gui/src/components/common/EmptyState.module.css`
- Create: `athena-gui/src/components/common/Panel.tsx`、`Panel.module.css`
- Create: `athena-gui/src/components/common/KeyValueList.tsx`、`KeyValueList.module.css`
- Create: `athena-gui/src/components/common/Section.tsx`、`Section.module.css`
- Create: `athena-gui/src/components/common/CodeBlock.tsx`、`CodeBlock.module.css`
- Modify: `athena-gui/src/components/common/ErrorBoundary.tsx`
- Create: `athena-gui/src/components/common/ErrorBoundary.module.css`
- Create: `athena-gui/src/components/common/__tests__/building-blocks.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `renderUi`。
- Produces:
  - `HYPOTHESIS_STATUS_VARIANT: Record<string, LabelColorOptions>`；`hypothesisVariant(status: string): LabelColorOptions`；`hypothesisBorderColor(status: string): string`（返回 `var(--borderColor-…)`）。
  - `StatusBadge({ status: string })`（实验生命周期状态 → `Label`）。
  - `EmptyState({ icon: IconName; message: string })`（接口不变）。
  - `Panel({ title: ReactNode; actions?: ReactNode; fill?: boolean; children?: ReactNode })`。
  - `KeyValueList({ items: KeyValueItem[]; columns?: 1 | 2 })`，`KeyValueItem = { label: string; value: ReactNode; mono?: boolean }`。
  - `Section({ title?: ReactNode; children: ReactNode })`。
  - `CodeBlock({ children: ReactNode; className?: string })`。

- [ ] **Step 1: 写失败测试**

Create `athena-gui/src/components/common/__tests__/building-blocks.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { Button } from "@primer/react";
import { renderUi } from "../../../test/render";
import { hypothesisBorderColor, hypothesisVariant } from "../hypothesisStatus";
import { StatusBadge } from "../StatusBadge";
import { Panel } from "../Panel";
import { KeyValueList } from "../KeyValueList";
import { EmptyState } from "../EmptyState";
import { CodeBlock } from "../CodeBlock";

describe("hypothesisStatus", () => {
  it("maps each known hypothesis status to its Primer Label variant", () => {
    expect(hypothesisVariant("SUPPORTED")).toBe("success");
    expect(hypothesisVariant("REFUTED")).toBe("danger");
    expect(hypothesisVariant("PROPOSED")).toBe("accent");
    expect(hypothesisVariant("REJECTED")).toBe("secondary");
    expect(hypothesisVariant("INCONCLUSIVE")).toBe("attention");
  });

  it("uses Primer emphasis border colors for graph nodes", () => {
    expect(hypothesisBorderColor("SUPPORTED")).toBe("var(--borderColor-success-emphasis)");
    expect(hypothesisBorderColor("REFUTED")).toBe("var(--borderColor-danger-emphasis)");
  });

  it("falls back to secondary for statuses the frontend does not know", () => {
    expect(hypothesisVariant("SOMETHING_NEW")).toBe("secondary");
    expect(hypothesisBorderColor("SOMETHING_NEW")).toBe("var(--borderColor-emphasis)");
  });
});

describe("StatusBadge", () => {
  it("renders a Primer Label with the lifecycle variant", () => {
    renderUi(<StatusBadge status="FAILED" />);
    expect(screen.getByText("FAILED")).toHaveAttribute("data-variant", "danger");
  });

  it("renders unknown statuses as a secondary label without failing", () => {
    renderUi(<StatusBadge status="ARCHIVED" />);
    expect(screen.getByText("ARCHIVED")).toHaveAttribute("data-variant", "secondary");
  });
});

describe("Panel", () => {
  it("renders an h3 title and its actions", () => {
    renderUi(<Panel title="实验日志 (2)" actions={<Button size="small">刷新</Button>}>内容</Panel>);
    expect(screen.getByRole("heading", { level: 3, name: "实验日志 (2)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
    expect(screen.getByText("内容")).toBeInTheDocument();
  });
});

describe("KeyValueList", () => {
  it("renders labels and values and shows a dash for empty values", () => {
    renderUi(
      <KeyValueList
        items={[
          { label: "路径", value: "/work/exp-1", mono: true },
          { label: "分支", value: "" },
        ]}
      />,
    );
    expect(screen.getByText("路径")).toBeInTheDocument();
    expect(screen.getByText("/work/exp-1")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("EmptyState and CodeBlock", () => {
  it("shows the guidance message and preformatted code", () => {
    renderUi(
      <>
        <EmptyState icon="log" message="暂无实验记录。" />
        <CodeBlock>{"line 1\nline 2"}</CodeBlock>
      </>,
    );
    expect(screen.getByText("暂无实验记录。")).toBeInTheDocument();
    expect(screen.getByText(/line 1/).tagName).toBe("PRE");
  });
});
```

- [ ] **Step 2: 运行，确认失败**

Run: `npm --prefix athena-gui test -- src/components/common/__tests__/building-blocks.test.tsx`
Expected: FAIL，找不到 `../hypothesisStatus` 等模块。

- [ ] **Step 3: 实现状态映射与 StatusBadge**

Create `athena-gui/src/components/common/hypothesisStatus.ts`:

```ts
import type { LabelColorOptions } from "@primer/react";

/** 假设状态 → Primer Label 配色；研究树、假设图、待选假设、实验列表共用。 */
export const HYPOTHESIS_STATUS_VARIANT: Record<string, LabelColorOptions> = {
  SUPPORTED: "success",
  REFUTED: "danger",
  PROPOSED: "accent",
  REJECTED: "secondary",
  INCONCLUSIVE: "attention",
};

const BORDER_COLOR: Partial<Record<LabelColorOptions, string>> = {
  success: "var(--borderColor-success-emphasis)",
  danger: "var(--borderColor-danger-emphasis)",
  accent: "var(--borderColor-accent-emphasis)",
  attention: "var(--borderColor-attention-emphasis)",
  secondary: "var(--borderColor-emphasis)",
};

/** 假设状态对应的 Label variant；未知状态回落 secondary。 */
export function hypothesisVariant(status: string): LabelColorOptions {
  return HYPOTHESIS_STATUS_VARIANT[status] ?? "secondary";
}

/** 图节点边框与小地图用的状态色（Primer 变量）。 */
export function hypothesisBorderColor(status: string): string {
  return BORDER_COLOR[hypothesisVariant(status)] ?? "var(--borderColor-emphasis)";
}
```

把 `athena-gui/src/components/common/StatusBadge.tsx` 整体替换为：

```tsx
import { Label, type LabelColorOptions } from "@primer/react";

/** 实验生命周期状态 → Primer Label 配色。 */
const STATUS_VARIANT: Record<string, LabelColorOptions> = {
  PENDING: "accent",
  RUNNING: "attention",
  SUCCEEDED: "success",
  FAILED: "danger",
  CANCELLED: "secondary",
};

/** 实验生命周期状态标签；未知状态显示为 secondary。 */
export function StatusBadge({ status }: { status: string }) {
  return <Label variant={STATUS_VARIANT[status] ?? "secondary"}>{status}</Label>;
}
```

- [ ] **Step 4: 实现空状态、面板、键值列表、分组、代码块**

把 `athena-gui/src/components/common/EmptyState.tsx` 整体替换为：

```tsx
import { Icon, type IconName } from "./Icon";
import styles from "./EmptyState.module.css";

interface EmptyStateProps {
  icon: IconName;
  message: string;
}

/** 空状态：按 Primer Blankslate 规格的图标 + 说明文字。 */
export function EmptyState({ icon, message }: EmptyStateProps) {
  return (
    <div className={styles.blankslate}>
      <span className={styles.visual} aria-hidden="true">
        <Icon name={icon} size={24} />
      </span>
      <p className={styles.message}>{message}</p>
    </div>
  );
}
```

Create `athena-gui/src/components/common/EmptyState.module.css`:

```css
.blankslate {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--base-size-8);
  padding: var(--base-size-32) var(--base-size-16);
  text-align: center;
}

.visual {
  color: var(--fgColor-muted);
}

.message {
  margin: 0;
  max-width: 360px;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-muted);
}
```

Create `athena-gui/src/components/common/Panel.tsx`:

```tsx
import type { ReactNode } from "react";
import { PageHeader } from "@primer/react";
import styles from "./Panel.module.css";

interface PanelProps {
  title: ReactNode;
  actions?: ReactNode;
  /** 撑满父容器高度（图类面板需要确定高度）。 */
  fill?: boolean;
  children?: ReactNode;
}

/** 数据面板：PageHeader 标题区 + 右侧操作 + 内容区。 */
export function Panel({ title, actions, fill = false, children }: PanelProps) {
  return (
    <div className={fill ? `${styles.panel} ${styles.fill}` : styles.panel}>
      <PageHeader className={styles.header}>
        <PageHeader.TitleArea variant="medium">
          <PageHeader.Title as="h3">{title}</PageHeader.Title>
        </PageHeader.TitleArea>
        {actions && <PageHeader.Actions>{actions}</PageHeader.Actions>}
      </PageHeader>
      {children}
    </div>
  );
}
```

Create `athena-gui/src/components/common/Panel.module.css`:

```css
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-12);
  min-height: 0;
}

.fill {
  height: 100%;
}

.header {
  flex-shrink: 0;
}
```

Create `athena-gui/src/components/common/KeyValueList.tsx`:

```tsx
import type { ReactNode } from "react";
import styles from "./KeyValueList.module.css";

export interface KeyValueItem {
  label: string;
  value: ReactNode;
  /** 用等宽字体显示（路径、提交号、ID）。 */
  mono?: boolean;
}

/** 键值列表；空值显示为「—」。 */
export function KeyValueList({ items, columns = 1 }: { items: KeyValueItem[]; columns?: 1 | 2 }) {
  return (
    <dl className={columns === 2 ? `${styles.list} ${styles.twoColumns}` : styles.list}>
      {items.map((item) => (
        <div key={item.label} className={styles.row}>
          <dt className={styles.label}>{item.label}</dt>
          <dd className={item.mono ? `${styles.value} ${styles.mono}` : styles.value}>
            {item.value === "" || item.value == null ? "—" : item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
```

Create `athena-gui/src/components/common/KeyValueList.module.css`:

```css
.list {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--base-size-4) var(--base-size-24);
  margin: 0;
}

.twoColumns {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.row {
  display: flex;
  gap: var(--base-size-8);
  min-width: 0;
  font-size: var(--text-body-size-medium);
}

.label {
  flex-shrink: 0;
  min-width: 96px;
  color: var(--fgColor-muted);
}

.value {
  margin: 0;
  min-width: 0;
  word-break: break-word;
  color: var(--fgColor-default);
}

.mono {
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
}
```

Create `athena-gui/src/components/common/Section.tsx`:

```tsx
import type { ReactNode } from "react";
import styles from "./Section.module.css";

/** 带边框的分组容器（对应 GitHub 的 Box），可选标题栏。 */
export function Section({ title, children }: { title?: ReactNode; children: ReactNode }) {
  return (
    <section className={styles.section}>
      {title && <h4 className={styles.title}>{title}</h4>}
      <div className={styles.body}>{children}</div>
    </section>
  );
}
```

Create `athena-gui/src/components/common/Section.module.css`:

```css
.section {
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  overflow: hidden;
}

.title {
  margin: 0;
  padding: var(--base-size-8) var(--base-size-16);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-muted);
  font-size: var(--text-body-size-medium);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-default);
}

.body {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
  padding: var(--base-size-12) var(--base-size-16);
}
```

Create `athena-gui/src/components/common/CodeBlock.tsx`:

```tsx
import type { ReactNode } from "react";
import styles from "./CodeBlock.module.css";

/** 等宽代码块：GitHub 代码底色与 Primer 等宽字体栈。 */
export function CodeBlock({ children, className }: { children: ReactNode; className?: string }) {
  return <pre className={className ? `${styles.code} ${className}` : styles.code}>{children}</pre>;
}
```

Create `athena-gui/src/components/common/CodeBlock.module.css`:

```css
.code {
  margin: 0;
  padding: var(--base-size-12) var(--base-size-16);
  border: var(--borderWidth-thin) solid var(--borderColor-muted);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-muted);
  color: var(--fgColor-default);
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  line-height: 1.45;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
```

- [ ] **Step 5: ErrorBoundary 改用 Flash**

把 `athena-gui/src/components/common/ErrorBoundary.tsx` 中 `render()` 方法里 `if (this.state.error) { return (...); }` 的返回内容替换为：

```tsx
        <Flash variant="danger" className={styles.flash}>
          <h3 className={styles.title}>面板加载失败</h3>
          <p className={styles.message}>{this.state.error.message}</p>
        </Flash>
```

并在现有 `import { Component, type ReactNode } from "react";` 之后加：

```tsx
import { Flash } from "@primer/react";
import styles from "./ErrorBoundary.module.css";
```

Create `athena-gui/src/components/common/ErrorBoundary.module.css`:

```css
.flash {
  margin: var(--base-size-16);
}

.title {
  margin: 0 0 var(--base-size-4);
  font-size: var(--text-body-size-medium);
  font-weight: var(--base-text-weight-semibold);
}

.message {
  margin: 0;
  font-size: var(--text-body-size-medium);
}
```

- [ ] **Step 6: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/common`
Expected: `building-blocks.test.tsx` 8 passed，`Icon.test.tsx` 2 passed。

Run: `npm --prefix athena-gui test -- src/components/__tests__/common-contracts.test.tsx`
Expected: 3 passed（ErrorBoundary 的「面板加载失败」标题与错误文本仍可找到）。

- [ ] **Step 7: 全量测试与构建**

Run: `npm --prefix athena-gui test` → 全部通过；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 8: 提交**

```bash
git add athena-gui/src/components/common/hypothesisStatus.ts athena-gui/src/components/common/StatusBadge.tsx athena-gui/src/components/common/EmptyState.tsx athena-gui/src/components/common/EmptyState.module.css athena-gui/src/components/common/Panel.tsx athena-gui/src/components/common/Panel.module.css athena-gui/src/components/common/KeyValueList.tsx athena-gui/src/components/common/KeyValueList.module.css athena-gui/src/components/common/Section.tsx athena-gui/src/components/common/Section.module.css athena-gui/src/components/common/CodeBlock.tsx athena-gui/src/components/common/CodeBlock.module.css athena-gui/src/components/common/ErrorBoundary.tsx athena-gui/src/components/common/ErrorBoundary.module.css athena-gui/src/components/common/__tests__/building-blocks.test.tsx
git commit -m "feat(gui): 新增基于 Primer 的共享面板部件与状态映射"
```

---

### Task 3: 外壳改为 GitHub 仓库页结构

**Files:**
- Create: `athena-gui/src/components/shell/ModuleNav.tsx`、`ModuleNav.module.css`
- Create: `athena-gui/src/components/shell/__tests__/ModuleNav.test.tsx`
- Modify: `athena-gui/src/components/shell/AppShell.tsx`、`AppShell.module.css`
- Modify: `athena-gui/src/components/shell/ContextSidebar.tsx`、`ContextSidebar.module.css`
- Modify: `athena-gui/src/components/shell/ContextDrawer.tsx`、`ContextDrawer.module.css`
- Modify: `athena-gui/src/components/shell/LogDrawer.tsx`、`LogDrawer.module.css`
- Modify: `athena-gui/src/components/__tests__/app-shell.test.tsx`
- Delete: `athena-gui/src/components/shell/FunctionRail.tsx`、`FunctionRail.module.css`、`AthenaWordmark.tsx`
- Delete: `athena-gui/src/components/__tests__/function-rail.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `renderUi`、`ThemeToggle`、`Icon`；Task 2 的 `EmptyState`、`ErrorBoundary`；`navigation.ts` 的 `MODULES` / `MODULE_BY_KEY` / `RELATED_PANELS`（不改）。
- Produces: `ModuleNav({ module: ModuleKey; counts?: Partial<Record<ModuleKey, number>>; onSelect(module: ModuleKey): void })`。`AppShell` 的 props 不变。设置不再是一个模块：由顶栏「设置」`IconButton` 打开，`AppShell` 内部用 `settingsOpen` 状态渲染 `<SettingsPanel onClose={...} />`（`SettingsPanel` 本任务不改，Task 6 改为 Primer Dialog）。

**与 spec 的差异（已核实，需在完成报告中说明）：**
- 只有「实验」标签显示计数（取 `viewModel.rightRail.searchAttempts`）。研究树的假设数不在 `viewModel` 中，为它新增数据请求超出本次范围。
- 会话列表用 `ActionList` + `ActionList.TrailingAction`（删除按钮），不是 `NavList`：`NavList.Item` 没有行尾操作位。工作区标题用 `Button variant="invisible"`：现有测试依赖它是可禁用的真按钮。详情视图入口用 `NavList`。
- 右侧详情 / 日志沿用现有的绝对定位浮层（非模态），只换为 Primer 样式。

- [ ] **Step 1: 写 ModuleNav 的失败测试**

Create `athena-gui/src/components/shell/__tests__/ModuleNav.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../../test/render";
import { ModuleNav } from "../ModuleNav";

describe("ModuleNav", () => {
  it("selects a module", () => {
    const onSelect = vi.fn();
    renderUi(<ModuleNav module="session" onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("button", { name: "研究树" }));

    expect(onSelect).toHaveBeenCalledWith("research-tree");
  });

  it("marks only the active module as the current page", () => {
    renderUi(<ModuleNav module="experiments" onSelect={vi.fn()} />);

    expect(screen.getByRole("button", { name: "实验" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "会话" })).not.toHaveAttribute("aria-current");
  });

  it("shows a counter only when the count is positive", () => {
    const { rerender } = renderUi(
      <ModuleNav module="session" counts={{ experiments: 3 }} onSelect={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /^实验/ })).toHaveTextContent("3");

    rerender(<ModuleNav module="session" counts={{ experiments: 0 }} onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: "实验" })).toBeInTheDocument();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/shell/__tests__/ModuleNav.test.tsx`
Expected: FAIL，找不到 `../ModuleNav`。

- [ ] **Step 2: 实现 ModuleNav**

Create `athena-gui/src/components/shell/ModuleNav.tsx`:

```tsx
import { UnderlineNav } from "@primer/react";
import type { ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { MODULES } from "./navigation";
import styles from "./ModuleNav.module.css";

interface ModuleNavProps {
  module: ModuleKey;
  /** 各模块标签上的计数；缺省或为 0 时不显示。 */
  counts?: Partial<Record<ModuleKey, number>>;
  onSelect(module: ModuleKey): void;
}

/** 模块标签栏：GitHub 仓库页式 UnderlineNav，替代原左侧图标功能轨。 */
export function ModuleNav({ module, counts = {}, onSelect }: ModuleNavProps) {
  return (
    <div className={styles.nav}>
      <UnderlineNav aria-label="模块">
        {MODULES.map((item) => {
          const count = counts[item.key];
          return (
            <UnderlineNav.Item
              key={item.key}
              as="button"
              aria-current={module === item.key ? "page" : undefined}
              leadingVisual={<Icon name={item.icon} size={16} />}
              counter={count ? count : undefined}
              onSelect={(event) => {
                event.preventDefault();
                onSelect(item.key);
              }}
            >
              {item.label}
            </UnderlineNav.Item>
          );
        })}
      </UnderlineNav>
    </div>
  );
}
```

Create `athena-gui/src/components/shell/ModuleNav.module.css`:

```css
.nav {
  background: var(--bgColor-muted);
  padding-inline: var(--base-size-8);
}
```

Run: `npm --prefix athena-gui test -- src/components/shell/__tests__/ModuleNav.test.tsx`
Expected: 3 passed。

- [ ] **Step 3: 为外壳新增两个失败测试**

在 `athena-gui/src/components/__tests__/app-shell.test.tsx` 中，紧挨 `it("toggles the context sidebar via the top-bar control", ...)` 之前插入：

```tsx
  it("shows the workspace and session as a breadcrumb with full text in the title", () => {
    renderUi(
      <AppShell
        currentRoot="C:/projects/a-very-long-workspace-name-that-will-be-truncated"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [{ id: "s-1", title: "分类 · f1_macro" }],
          currentSessionId: "s-1",
        }) as never}
      />,
    );

    const breadcrumb = screen.getByRole("navigation", { name: "当前位置" });
    expect(within(breadcrumb).getByText("a-very-long-workspace-name-that-will-be-truncated")).toHaveAttribute(
      "title",
      "C:/projects/a-very-long-workspace-name-that-will-be-truncated",
    );
    expect(within(breadcrumb).getByText("分类 · f1_macro")).toHaveAttribute("title", "分类 · f1_macro");
  });

  it("opens the settings dialog from the top-bar gear and closes it", () => {
    renderUi(
      <AppShell currentRoot={null} recentRoots={[]} switching={false} onSwitchWorkspace={vi.fn()} onSelectWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
    );

    expect(screen.queryByRole("dialog", { name: "运行设置" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    expect(screen.getByRole("dialog", { name: "运行设置" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭设置" }));
    expect(screen.queryByRole("dialog", { name: "运行设置" })).not.toBeInTheDocument();
  });
```

Run: `npm --prefix athena-gui test -- src/components/__tests__/app-shell.test.tsx`
Expected: 新增的两个测试 FAIL（没有「当前位置」导航，也没有名为「设置」的顶栏按钮会打开对话框）。

- [ ] **Step 4: 重写外壳**

把 `athena-gui/src/components/shell/AppShell.tsx` 整体替换为：

```tsx
import { Suspense, useEffect, useRef, useState } from "react";
import { IconButton, Label, type LabelColorOptions } from "@primer/react";
import { GearIcon, LogIcon, SidebarCollapseIcon, SidebarExpandIcon } from "@primer/octicons-react";
import type { ContextPanelKey, ModuleKey, PipelineStatus } from "../../types/ui";
import { usePipeline } from "../../hooks/usePipeline";
import { basename } from "../../lib/path";
import { ConversationPane } from "../conversation/ConversationPane";
import { Icon } from "../common/Icon";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { ModuleNav } from "./ModuleNav";
import { ContextSidebar } from "./ContextSidebar";
import { ContextDrawer } from "./ContextDrawer";
import { LogDrawer } from "./LogDrawer";
import { ThemeToggle } from "./ThemeToggle";
import { HumanRequestDialog } from "./HumanRequestDialog";
import { PANELS } from "../context/panels";
import { SettingsPanel } from "../SettingsPanel";
import { MODULE_BY_KEY } from "./navigation";
import styles from "./AppShell.module.css";

type Pipeline = ReturnType<typeof usePipeline>;

interface AppShellProps {
  currentRoot: string | null;
  recentRoots: string[];
  switching: boolean;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string, sessionId?: string): void;
  pipeline: Pipeline;
}

/** 运行状态 → 顶栏状态标签配色。 */
const STATUS_VARIANT: Record<PipelineStatus, LabelColorOptions> = {
  idle: "secondary",
  running: "success",
  paused: "attention",
  completed: "accent",
  error: "danger",
};

/** 统一研究工作区：GitHub 仓库页式顶栏 + 模块标签栏 + 左侧栏 / 主区 / 右侧详情。 */
export function AppShell({ currentRoot, recentRoots, switching, onSwitchWorkspace, onSelectWorkspace, pipeline }: AppShellProps) {
  const [module, setModule] = useState<ModuleKey>("session");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [drawerPanel, setDrawerPanel] = useState<ContextPanelKey | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const def = MODULE_BY_KEY[module];
  const { viewModel } = pipeline;
  const workspaceName = currentRoot ? basename(currentRoot) : "未选择工作区";
  const sessionTitle = pipeline.sessions.find((session) => session.id === pipeline.currentSessionId)?.title;

  // 切回「会话」模块时，重新拉取当前会话的 transcript，让右侧工作区恢复历史对话；
  // 运行中跳过（此时消息由实时事件驱动，避免重放打断进行中的执行）。
  const previousModule = useRef(module);
  useEffect(() => {
    const wasAway = previousModule.current !== "session";
    previousModule.current = module;
    if (
      module === "session" &&
      wasAway &&
      pipeline.viewModel.status !== "running"
    ) {
      void pipeline.switchSession(pipeline.currentSessionId);
    }
  }, [module, pipeline.currentSessionId, pipeline.viewModel.status, pipeline.switchSession]);

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <IconButton
          icon={sidebarCollapsed ? SidebarExpandIcon : SidebarCollapseIcon}
          variant="invisible"
          aria-label={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
          onClick={() => setSidebarCollapsed((v) => !v)}
        />
        <span className={styles.mark} aria-hidden="true">
          <Icon name="sparkles" size={16} />
        </span>
        <nav className={styles.breadcrumb} aria-label="当前位置">
          <span className={styles.wordmark}>Athena</span>
          <span className={styles.separator} aria-hidden="true">/</span>
          <span className={styles.crumb} title={currentRoot ?? undefined}>{workspaceName}</span>
          {sessionTitle && (
            <>
              <span className={styles.separator} aria-hidden="true">/</span>
              <span className={styles.crumbCurrent} title={sessionTitle}>{sessionTitle}</span>
            </>
          )}
        </nav>
        <div className={styles.topbarActions}>
          <span data-status={viewModel.status}>
            <Label variant={STATUS_VARIANT[viewModel.status] ?? "secondary"}>
              {viewModel.phase || "idle"} · {viewModel.status}
            </Label>
          </span>
          <IconButton
            icon={LogIcon}
            variant="invisible"
            aria-label={logOpen ? "关闭日志" : "打开日志"}
            onClick={() => setLogOpen((v) => !v)}
          />
          <ThemeToggle />
          <IconButton icon={GearIcon} variant="invisible" aria-label="设置" onClick={() => setSettingsOpen(true)} />
        </div>
      </header>

      <ModuleNav
        module={module}
        counts={{ experiments: viewModel.rightRail.searchAttempts }}
        onSelect={setModule}
      />

      <div className={styles.body}>
        {!sidebarCollapsed && (
          <ContextSidebar
            module={module}
            currentRoot={currentRoot}
            recentRoots={recentRoots}
            switching={switching}
            sessions={pipeline.sessions}
            currentSessionId={pipeline.currentSessionId}
            onSwitchWorkspace={onSwitchWorkspace}
            onSelectWorkspace={onSelectWorkspace}
            onSelectSession={pipeline.switchSession}
            onNewSession={pipeline.newSession}
            onDeleteSession={pipeline.deleteSession}
            onOpenDrawer={setDrawerPanel}
          />
        )}
        <main className={styles.main}>
          {module === "session" ? (
            <ConversationPane key={pipeline.currentSessionId} pipeline={pipeline} />
          ) : (
            <div className={styles.mainPanel}>
              <PanelView panel={def.panel} />
            </div>
          )}
        </main>
        {drawerPanel && (
          <ContextDrawer panel={drawerPanel} onClose={() => setDrawerPanel(null)} />
        )}
        {logOpen && (
          <LogDrawer
            entries={pipeline.logs}
            onClose={() => setLogOpen(false)}
            onClear={pipeline.clearLogs}
          />
        )}
      </div>

      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}

      <HumanRequestDialog
        request={pipeline.humanRequests?.[0] ?? null}
        onReply={(reply) => {
          const current = pipeline.humanRequests?.[0];
          if (current && typeof pipeline.replyToHumanRequest === "function") {
            return pipeline.replyToHumanRequest(current.request_id, reply);
          }
          return undefined;
        }}
        settling={pipeline.settlingRequestId === pipeline.humanRequests?.[0]?.request_id}
        error={pipeline.humanPendingError}
      />
    </div>
  );
}

function PanelView({ panel }: { panel: ContextPanelKey | null }) {
  const Panel = panel ? PANELS[panel] : undefined;
  return (
    <Suspense fallback={<EmptyState icon="loader" message="加载中…" />}>
      <ErrorBoundary>
        {Panel ? <Panel /> : <EmptyState icon="loader" message="该视图暂不可用" />}
      </ErrorBoundary>
    </Suspense>
  );
}
```

把 `athena-gui/src/components/shell/AppShell.module.css` 整体替换为：

```css
/* GitHub 仓库页式外壳：顶栏 + 模块标签栏 + 左侧栏 / 主区 / 右侧非模态详情。 */

.shell {
  height: 100vh;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
  background: var(--bgColor-default);
  color: var(--fgColor-default);
  overflow: hidden;
}

.topbar {
  display: flex;
  align-items: center;
  gap: var(--base-size-8);
  min-width: 0;
  padding: var(--base-size-12) var(--base-size-16) var(--base-size-8);
  background: var(--bgColor-muted);
}

.mark {
  display: grid;
  place-items: center;
  width: var(--base-size-32);
  height: var(--base-size-32);
  flex-shrink: 0;
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  color: var(--fgColor-default);
}

.breadcrumb {
  display: flex;
  align-items: center;
  gap: var(--base-size-4);
  min-width: 0;
  font-size: var(--text-title-size-small);
}

.wordmark {
  flex-shrink: 0;
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-default);
}

.separator {
  flex-shrink: 0;
  color: var(--fgColor-muted);
}

.crumb,
.crumbCurrent {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fgColor-default);
}

.crumbCurrent {
  font-weight: var(--base-text-weight-semibold);
}

.topbarActions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--base-size-4);
  flex-shrink: 0;
}

.body {
  min-height: 0;
  display: flex;
  position: relative;
}

.main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bgColor-default);
  overflow: hidden;
}

.main > * {
  flex: 1;
  min-height: 0;
}

.mainPanel {
  overflow-y: auto;
  padding: var(--base-size-16) var(--base-size-24);
}
```

删除 `athena-gui/src/components/shell/FunctionRail.tsx`、`athena-gui/src/components/shell/FunctionRail.module.css`、`athena-gui/src/components/shell/AthenaWordmark.tsx`、`athena-gui/src/components/__tests__/function-rail.test.tsx`（由 `ModuleNav.test.tsx` 取代）。

- [ ] **Step 5: 重写左侧栏与右侧侧栏**

把 `athena-gui/src/components/shell/ContextSidebar.tsx` 整体替换为：

```tsx
import { useMemo } from "react";
import { ActionList, Button, NavList } from "@primer/react";
import { FileDirectoryIcon, PlusIcon, TrashIcon } from "@primer/octicons-react";
import type { ContextPanelKey, ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { RELATED_PANELS } from "./navigation";
import { basename } from "../../lib/path";
import { loadWorkspaceSessions } from "../../lib/workspaceStorage";
import styles from "./ContextSidebar.module.css";

export interface SessionItem {
  id: string;
  title: string;
}

interface WorkspaceGroup {
  root: string;
  name: string;
  isCurrent: boolean;
  sessions: SessionItem[];
}

export interface ContextSidebarProps {
  module: ModuleKey;
  currentRoot: string | null;
  recentRoots: string[];
  switching: boolean;
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string, sessionId?: string): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
  onOpenDrawer(panel: ContextPanelKey): void;
}

/** 上下文侧栏：会话模块显示按工作区分组的会话列表，其余模块显示详情视图。 */
export function ContextSidebar({
  module,
  currentRoot,
  recentRoots,
  sessions,
  currentSessionId,
  onSwitchWorkspace,
  onSelectWorkspace,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  onOpenDrawer,
}: ContextSidebarProps) {
  const related = RELATED_PANELS[module];

  return (
    <aside className={styles.sidebar} aria-label="上下文侧栏">
      {module === "session" ? (
        <SessionContext
          currentRoot={currentRoot}
          recentRoots={recentRoots}
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSwitchWorkspace={onSwitchWorkspace}
          onSelectWorkspace={onSelectWorkspace}
          onSelectSession={onSelectSession}
          onNewSession={onNewSession}
          onDeleteSession={onDeleteSession}
        />
      ) : related.length === 0 ? (
        <p className={styles.hint}>无关联详情视图</p>
      ) : (
        <NavList aria-label="详情视图" className={styles.related}>
          <NavList.Group title="详情视图">
            {related.map((item) => (
              <NavList.Item key={item.key} as="button" onClick={() => onOpenDrawer(item.key)}>
                <NavList.LeadingVisual>
                  <Icon name={item.icon} size={16} />
                </NavList.LeadingVisual>
                {item.label}
              </NavList.Item>
            ))}
          </NavList.Group>
        </NavList>
      )}
    </aside>
  );
}

function SessionContext({
  currentRoot,
  recentRoots,
  sessions,
  currentSessionId,
  onSwitchWorkspace,
  onSelectWorkspace,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: {
  currentRoot: string | null;
  recentRoots: string[];
  sessions: SessionItem[];
  currentSessionId: string;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string, sessionId?: string): void;
  onSelectSession(id: string): void;
  onNewSession(): void;
  onDeleteSession(id: string): void;
}) {
  const otherGroups = useMemo(
    () => recentRoots
      .filter((root) => root !== currentRoot)
      .map((root): WorkspaceGroup => ({
        root,
        name: basename(root),
        isCurrent: false,
        sessions: loadWorkspaceSessions(root),
      })),
    [currentRoot, recentRoots],
  );

  const orderedRoots = currentRoot && !recentRoots.includes(currentRoot)
    ? [...recentRoots, currentRoot]
    : recentRoots;
  const otherGroupsByRoot = new Map(otherGroups.map((group) => [group.root, group]));
  const groups = orderedRoots.flatMap((root): WorkspaceGroup[] => {
    if (root === currentRoot) {
      return [{ root, name: basename(root), isCurrent: true, sessions }];
    }
    const group = otherGroupsByRoot.get(root);
    return group ? [group] : [];
  });

  return (
    <div className={styles.sessionContext}>
      <div className={styles.newSession}>
        <Button block leadingVisual={PlusIcon} onClick={onNewSession}>
          新会话
        </Button>
      </div>
      <div className={styles.workspaceScroll} data-testid="workspace-scroll">
        {groups.map((group) => (
          <section key={group.root} className={styles.workspaceGroup}>
            <Button
              variant="invisible"
              size="small"
              alignContent="start"
              block
              leadingVisual={FileDirectoryIcon}
              className={styles.workspaceHeader}
              onClick={() => {
                if (!group.isCurrent) onSelectWorkspace(group.root);
              }}
              disabled={group.isCurrent}
              title={group.isCurrent ? "当前工作区" : `切换到 ${group.root}`}
            >
              {group.name}
            </Button>
            {group.sessions.length === 0 ? (
              <p className={styles.hint}>暂无会话</p>
            ) : (
              <ActionList aria-label={`${group.name} 会话`}>
                {group.sessions.map((session) => (
                  <ActionList.Item
                    key={session.id}
                    active={group.isCurrent && session.id === currentSessionId}
                    title={`${group.name} · ${session.title}`}
                    onSelect={() =>
                      group.isCurrent ? onSelectSession(session.id) : onSelectWorkspace(group.root, session.id)
                    }
                  >
                    {session.title}
                    {group.isCurrent && (
                      <ActionList.TrailingAction
                        label={`删除会话 ${session.title}`}
                        icon={TrashIcon}
                        onClick={() => onDeleteSession(session.id)}
                      />
                    )}
                  </ActionList.Item>
                ))}
              </ActionList>
            )}
          </section>
        ))}
      </div>
      <footer className={styles.sidebarFooter}>
        <Button block variant="invisible" onClick={onSwitchWorkspace}>
          切换工作区
        </Button>
      </footer>
    </div>
  );
}
```

把 `athena-gui/src/components/shell/ContextSidebar.module.css` 整体替换为：

```css
/* 左侧栏：会话模块显示工作区与会话列表，其余模块显示详情视图入口。 */

.sidebar {
  width: 264px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-default);
}

.sessionContext {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.newSession {
  padding: var(--base-size-12) var(--base-size-12) var(--base-size-8);
}

.workspaceScroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 var(--base-size-8) var(--base-size-8);
}

.workspaceGroup {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
  padding-top: var(--base-size-8);
}

.workspaceHeader {
  font-weight: var(--base-text-weight-semibold);
}

.hint {
  margin: var(--base-size-4) var(--base-size-8);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.sidebarFooter {
  padding: var(--base-size-8) var(--base-size-12);
  border-top: var(--borderWidth-thin) solid var(--borderColor-muted);
}

.related {
  padding: var(--base-size-8);
}
```

把 `athena-gui/src/components/shell/ContextDrawer.tsx` 整体替换为：

```tsx
import { Suspense } from "react";
import { Heading, IconButton } from "@primer/react";
import { XIcon } from "@primer/octicons-react";
import type { ContextPanelKey } from "../../types/ui";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { PANELS, PANEL_TITLES } from "../context/panels";
import styles from "./ContextDrawer.module.css";

interface ContextDrawerProps {
  panel: ContextPanelKey;
  onClose(): void;
}

/** 按需打开的右侧详情侧栏（非模态）：承载指标/假设图/差异等次要视图。 */
export function ContextDrawer({ panel, onClose }: ContextDrawerProps) {
  const Panel = PANELS[panel];
  const title = PANEL_TITLES[panel] ?? panel;

  return (
    <aside className={styles.drawer} role="complementary" aria-label={title}>
      <header className={styles.header}>
        <Heading as="h2" variant="small">{title}</Heading>
        <IconButton icon={XIcon} variant="invisible" size="small" aria-label="关闭详情" onClick={onClose} />
      </header>
      <div className={styles.body}>
        <Suspense fallback={<EmptyState icon="loader" message="加载中…" />}>
          <ErrorBoundary>
            {Panel ? <Panel /> : <EmptyState icon="loader" message="该视图暂不可用" />}
          </ErrorBoundary>
        </Suspense>
      </div>
    </aside>
  );
}
```

把 `athena-gui/src/components/shell/ContextDrawer.module.css` 整体替换为：

```css
/* 右侧详情侧栏：非模态，浮在主工作区右侧，按需开合。 */

.drawer {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 400px;
  max-width: 88%;
  display: flex;
  flex-direction: column;
  border-left: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-default);
  box-shadow: var(--shadow-floating-large);
}

.header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
  padding: var(--base-size-8) var(--base-size-8) var(--base-size-8) var(--base-size-16);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-muted);
}

.body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--base-size-16);
}
```

把 `athena-gui/src/components/shell/LogDrawer.tsx` 整体替换为：

```tsx
import { Button, Heading, IconButton, Label } from "@primer/react";
import { XIcon } from "@primer/octicons-react";
import type { LogEntry } from "../../hooks/usePipeline";
import styles from "./LogDrawer.module.css";

interface LogDrawerProps {
  entries: LogEntry[];
  onClose(): void;
  onClear(): void;
}

function formatTime(at: number): string {
  const d = new Date(at);
  return `${d.getHours().toString().padStart(2, "0")}:${d
    .getMinutes()
    .toString()
    .padStart(2, "0")}:${d.getSeconds().toString().padStart(2, "0")}`;
}

function meta(entry: LogEntry): string {
  const parts = [entry.source, entry.channel, entry.plan, entry.tool].filter(
    (v): v is string => Boolean(v),
  );
  return parts.length ? parts.join(" · ") : "";
}

/** 右侧运行日志侧栏：按时间倒序展示后端推送的 state/output 原始事件。 */
export function LogDrawer({ entries, onClose, onClear }: LogDrawerProps) {
  const ordered = [...entries].reverse();

  return (
    <aside className={styles.drawer} role="complementary" aria-label="运行日志">
      <header className={styles.header}>
        <Heading as="h2" variant="small">运行日志</Heading>
        <div className={styles.headerActions}>
          <Button size="small" variant="invisible" onClick={onClear}>
            清空
          </Button>
          <IconButton icon={XIcon} variant="invisible" size="small" aria-label="关闭日志" onClick={onClose} />
        </div>
      </header>
      <div className={styles.body}>
        {ordered.length === 0 ? (
          <p className={styles.empty}>暂无日志事件。启动任务后这里会显示 state/output 流。</p>
        ) : (
          <ul className={styles.list}>
            {ordered.map((entry) => (
              <li key={entry.id} className={styles.item}>
                <div className={styles.itemHead}>
                  <Label variant={entry.kind === "state" ? "accent" : "secondary"} size="small">
                    {entry.kind}
                  </Label>
                  <span className={styles.time}>{formatTime(entry.at)}</span>
                  <span className={styles.meta}>{meta(entry)}</span>
                </div>
                {entry.text && <pre className={styles.text}>{entry.text}</pre>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
```

把 `athena-gui/src/components/shell/LogDrawer.module.css` 整体替换为：

```css
/* 右侧运行日志侧栏：非模态，浮在主工作区右侧。 */

.drawer {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 440px;
  max-width: 92%;
  display: flex;
  flex-direction: column;
  border-left: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-default);
  box-shadow: var(--shadow-floating-large);
}

.header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
  padding: var(--base-size-8) var(--base-size-8) var(--base-size-8) var(--base-size-16);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-muted);
}

.headerActions {
  display: flex;
  align-items: center;
  gap: var(--base-size-4);
}

.body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--base-size-12);
}

.empty {
  margin: var(--base-size-16) 0;
  text-align: center;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-muted);
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
}

.item {
  padding: var(--base-size-8) var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-muted);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
}

.itemHead {
  display: flex;
  align-items: center;
  gap: var(--base-size-8);
  min-width: 0;
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.time {
  flex-shrink: 0;
  font-family: var(--fontStack-monospace);
}

.meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.text {
  margin: var(--base-size-8) 0 0;
  padding: 0;
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fgColor-default);
}
```

- [ ] **Step 6: 运行外壳相关测试**

Run: `npm --prefix athena-gui test -- src/components/__tests__/app-shell.test.tsx src/components/shell src/__tests__/App.test.tsx`
Expected: 全部通过（`app-shell` 12 个，含新增 2 个；`ModuleNav` 3 个；`App` 测试不变）。现有断言无需修改：会话与工作区条目仍是按钮，删除按钮的可访问名仍是「删除会话 <标题>」，`data-testid="workspace-scroll"` 与页脚「切换工作区」结构保留。

- [ ] **Step 7: 全量测试与构建**

Run: `npm --prefix athena-gui test`
Expected: 207 passed（Task 2 后的 204 − 旧功能轨 2 + `ModuleNav` 3 + 外壳 2）。

Run: `npm --prefix athena-gui run build`
Expected: 成功。

注意：务必在 `athena-gui/` 下（或用 `--prefix athena-gui`）运行 Vitest。若在仓库根目录直接 `npx vitest`，会因缺少本项目的 jsdom 与 setup 配置而大批量失败——这不是测试不稳定。

- [ ] **Step 8: 提交**

```bash
git add athena-gui/src/components/shell/ModuleNav.tsx athena-gui/src/components/shell/ModuleNav.module.css athena-gui/src/components/shell/__tests__/ModuleNav.test.tsx athena-gui/src/components/shell/AppShell.tsx athena-gui/src/components/shell/AppShell.module.css athena-gui/src/components/shell/ContextSidebar.tsx athena-gui/src/components/shell/ContextSidebar.module.css athena-gui/src/components/shell/ContextDrawer.tsx athena-gui/src/components/shell/ContextDrawer.module.css athena-gui/src/components/shell/LogDrawer.tsx athena-gui/src/components/shell/LogDrawer.module.css athena-gui/src/components/__tests__/app-shell.test.tsx athena-gui/src/components/shell/FunctionRail.tsx athena-gui/src/components/shell/FunctionRail.module.css athena-gui/src/components/shell/AthenaWordmark.tsx athena-gui/src/components/__tests__/function-rail.test.tsx
git commit -m "feat(gui): 外壳改为 GitHub 仓库页式顶栏与模块标签栏"
```

---

### Task 4: 工作区选择页

**Files:**
- Modify: `athena-gui/src/components/shell/WorkspacePicker.tsx`、`WorkspacePicker.module.css`
- Modify: `athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `Icon`。
- Produces: `WorkspacePicker` props 不变。路径输入框新增可访问名「项目目录路径」。

**与 spec 的差异：** 「最近使用」与目录浏览列表用 `Button variant="invisible"` 列表而非 `ActionList`——`App.test.tsx` 依赖 `getByTitle("/b")` 点击最近路径，`ActionList.Item` 也能透传 `title`，但真按钮语义更直接，且无需改动现有断言。

- [ ] **Step 1: 写目录浏览失败的测试（降级路径）**

在 `athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx` 中：把第一行的导入改为 `import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";`，并在 `describe` 的最后一个测试之后追加：

```tsx
  it("shows a directory listing failure inside the browser and keeps it cancellable", async () => {
    mocks.isTauri.mockReturnValue(false);
    mocks.listWorkspaceDirectories.mockRejectedValueOnce(new Error("permission denied"));
    renderPicker();

    fireEvent.click(screen.getByRole("button", { name: "浏览目录…" }));

    const browser = await screen.findByRole("dialog", { name: "浏览工作区目录" });
    expect(await within(browser).findByText("permission denied")).toBeInTheDocument();
    const cancel = within(browser).getByRole("button", { name: "取消" });
    expect(cancel).toBeEnabled();
    fireEvent.click(cancel);
    expect(screen.queryByRole("dialog", { name: "浏览工作区目录" })).not.toBeInTheDocument();
  });
```

Run: `npm --prefix athena-gui test -- src/components/shell/__tests__/WorkspacePicker.test.tsx`
Expected: 新测试 PASS 或 FAIL 均可（旧实现用 `.card--error` 也显示文本）；本步的目的是先固定降级行为，Step 3 改写后必须仍然通过。

- [ ] **Step 2: 改写为 Primer 组件**

把 `athena-gui/src/components/shell/WorkspacePicker.tsx` 整体替换为：

```tsx
import { useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { Button, Flash, Heading, TextInput } from "@primer/react";
import { FileDirectoryIcon } from "@primer/octicons-react";
import { Icon } from "../common/Icon";
import styles from "./WorkspacePicker.module.css";
import {
  listWorkspaceDirectories,
  type WorkspaceDirectoryListing,
} from "../../lib/workspaceDialog";

interface WorkspacePickerProps {
  /** Active root reported by the backend (null until settings load). */
  currentRoot: string | null;
  /** Recently used roots, most-recent first. */
  recentRoots: string[];
  error: string | null;
  switching: boolean;
  browsing: boolean;
  onSelect(path: string): void;
  onBrowse(): void;
  onContinue(): void;
}

/** Full-screen workspace selection: recent list + path input + continue. */
export function WorkspacePicker({
  currentRoot,
  recentRoots,
  error,
  switching,
  browsing,
  onSelect,
  onBrowse,
  onContinue,
}: WorkspacePickerProps) {
  const [draft, setDraft] = useState("");
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState<string | null>(null);
  const [listing, setListing] = useState<WorkspaceDirectoryListing | null>(null);

  const canContinue = currentRoot != null && !switching;
  const canSelect = draft.trim() !== "";

  function submit() {
    if (draft.trim()) onSelect(draft);
  }

  async function openBrowser(path?: string | null) {
    setBrowserLoading(true);
    setBrowserError(null);
    try {
      setListing(await listWorkspaceDirectories(path));
    } catch (err) {
      setBrowserError(err instanceof Error ? err.message : String(err));
    } finally {
      setBrowserLoading(false);
    }
  }

  function handleBrowse() {
    if (isTauri()) {
      onBrowse();
      return;
    }
    setBrowserOpen(true);
    void openBrowser(currentRoot);
  }

  return (
    <div className={styles.picker}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            <Icon name="sparkles" size={16} />
          </span>
          Athena
        </div>
        <div className={styles.heading}>
          <Heading as="h1" variant="medium">选择工作区</Heading>
          <p className={styles.subtitle}>选择一个项目目录开始研究，或继续使用当前工作区。</p>
        </div>

        {error && <Flash variant="danger">{error}</Flash>}

        <section className={styles.current}>
          <span className={styles.label}>当前工作区</span>
          <span className={styles.path} title={currentRoot ?? ""}>
            <FileDirectoryIcon size={16} />
            {currentRoot ?? "加载中…"}
          </span>
          <Button variant="primary" onClick={onContinue} disabled={!canContinue}>
            继续使用当前
          </Button>
        </section>

        {recentRoots.length > 0 && (
          <section className={styles.section}>
            <span className={styles.label}>最近使用</span>
            <ul className={styles.list}>
              {recentRoots.map((root) => (
                <li key={root}>
                  <Button
                    variant="invisible"
                    block
                    alignContent="start"
                    leadingVisual={FileDirectoryIcon}
                    className={styles.item}
                    onClick={() => onSelect(root)}
                    title={root}
                  >
                    {root}
                  </Button>
                </li>
              ))}
            </ul>
          </section>
        )}

        <section className={styles.section}>
          <span className={styles.label}>打开其他目录</span>
          <Button onClick={handleBrowse} disabled={browsing || browserLoading}>
            {browsing || browserLoading ? "浏览中…" : "浏览目录…"}
          </Button>
          <div className={styles.row}>
            <TextInput
              block
              aria-label="项目目录路径"
              placeholder="输入项目目录的绝对路径…"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && draft.trim()) submit();
              }}
            />
            <Button onClick={submit} disabled={!canSelect}>
              打开
            </Button>
          </div>
        </section>

        {browserOpen && (
          <div className={styles.browser} role="dialog" aria-label="浏览工作区目录">
            <div className={styles.browserHeader}>
              <span className={styles.label}>选择工作区目录</span>
              <Button size="small" onClick={() => setBrowserOpen(false)} disabled={browserLoading}>
                取消
              </Button>
            </div>
            {browserError && <Flash variant="danger">{browserError}</Flash>}
            {listing && (
              <>
                <div className={styles.path} title={listing.path}>
                  <FileDirectoryIcon size={16} />
                  {listing.path}
                </div>
                <div className={styles.browserActions}>
                  <Button
                    size="small"
                    onClick={() => void openBrowser(listing.parent)}
                    disabled={!listing.parent || browserLoading}
                  >
                    上一级
                  </Button>
                  <Button
                    size="small"
                    variant="primary"
                    onClick={() => {
                      setBrowserOpen(false);
                      onSelect(listing.path);
                    }}
                    disabled={browserLoading}
                  >
                    选择此目录
                  </Button>
                </div>
                <ul className={`${styles.list} ${styles.browserList}`}>
                  {listing.directories.map((directory) => (
                    <li key={directory.path}>
                      <Button
                        variant="invisible"
                        block
                        alignContent="start"
                        leadingVisual={FileDirectoryIcon}
                        className={styles.item}
                        onClick={() => void openBrowser(directory.path)}
                        disabled={browserLoading}
                      >
                        {directory.name}
                      </Button>
                    </li>
                  ))}
                  {listing.directories.length === 0 && (
                    <li className={styles.browserEmpty}>此目录没有子目录。</li>
                  )}
                </ul>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
```

把 `athena-gui/src/components/shell/WorkspacePicker.module.css` 整体替换为：

```css
/* 工作区选择页：居中卡片，GitHub Blankslate 式的入口。 */

.picker {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: var(--base-size-24);
  background: var(--bgColor-muted);
}

.card {
  width: min(560px, 100%);
  display: flex;
  flex-direction: column;
  gap: var(--base-size-16);
  padding: var(--base-size-24);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-large);
  background: var(--bgColor-default);
  box-shadow: var(--shadow-resting-small);
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--base-size-8);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-default);
}

.mark {
  display: grid;
  place-items: center;
  width: var(--base-size-24);
  height: var(--base-size-24);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
}

.heading {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
}

.subtitle {
  margin: 0;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-muted);
}

.label {
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.current,
.section {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
}

.current {
  padding: var(--base-size-16);
  border: var(--borderWidth-thin) solid var(--borderColor-muted);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-muted);
}

.path {
  display: flex;
  align-items: center;
  gap: var(--base-size-8);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-default);
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
}

.item {
  font-family: var(--fontStack-monospace);
}

.row {
  display: flex;
  gap: var(--base-size-8);
}

.browser {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
  padding: var(--base-size-16);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
}

.browserHeader,
.browserActions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
}

.browserList {
  max-height: 240px;
  overflow-y: auto;
}

.browserEmpty {
  padding: var(--base-size-8);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}
```

- [ ] **Step 3: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/shell/__tests__/WorkspacePicker.test.tsx src/__tests__/App.test.tsx`
Expected: `WorkspacePicker` 7 passed，`App` 2 passed（现有断言均无需修改）。

Run: `npm --prefix athena-gui test` → 208 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 4: 提交**

```bash
git add athena-gui/src/components/shell/WorkspacePicker.tsx athena-gui/src/components/shell/WorkspacePicker.module.css athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx
git commit -m "feat(gui): 工作区选择页改用 Primer 组件"
```

---

### Task 5: 会话区（运行控制、输入框、欢迎页、待选假设、消息流、任务确认卡）

**Files:**
- Modify: `athena-gui/src/components/conversation/RunControls.tsx`、`RunControls.module.css`
- Create: `athena-gui/src/components/conversation/__tests__/RunControls.test.tsx`
- Modify: `athena-gui/src/components/conversation/Composer.tsx`、`Composer.module.css`
- Modify: `athena-gui/src/components/conversation/WelcomeHero.tsx`、`WelcomeHero.module.css`
- Modify: `athena-gui/src/components/conversation/PendingHypotheses.tsx`、`PendingHypotheses.module.css`
- Modify: `athena-gui/src/components/conversation/MessageList.tsx`、`MessageList.module.css`
- Modify: `athena-gui/src/components/cards/IntentPreviewCard.tsx`、`IntentPreviewCard.module.css`
- Modify: `athena-gui/src/components/cards/__tests__/IntentPreviewCard.test.tsx`
- 不改：`ConversationPane.tsx`、`ConversationPane.module.css`（后者没有颜色引用）

**Interfaces:**
- Consumes: Task 1 的 `renderUi`、`Icon`；Task 2 的 `CodeBlock`。
- Produces: 所有组件 props 不变。自动/手动切换改为 `SegmentedControl`：仅当点击的模式与当前不同时调用 `onToggleMode()`。

- [ ] **Step 1: 写运行控制条的失败测试**

Create `athena-gui/src/components/conversation/__tests__/RunControls.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../../../test/render";
import { createEmptyPipelineViewModel } from "../../../types/ui";
import { RunControls } from "../RunControls";

function renderControls(manual: boolean) {
  const onToggleMode = vi.fn();
  renderUi(
    <RunControls
      viewModel={{ ...createEmptyPipelineViewModel(), manual }}
      active={false}
      onPause={vi.fn()}
      onResume={vi.fn()}
      onStop={vi.fn()}
      onToggleMode={onToggleMode}
    />,
  );
  return onToggleMode;
}

describe("RunControls mode switch", () => {
  it("marks the current hypothesis selection mode as pressed", () => {
    renderControls(true);
    expect(screen.getByRole("button", { name: "手动" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "自动" })).toHaveAttribute("aria-pressed", "false");
  });

  it("toggles once when switching from automatic to manual", () => {
    const onToggleMode = renderControls(false);
    fireEvent.click(screen.getByRole("button", { name: "手动" }));
    expect(onToggleMode).toHaveBeenCalledTimes(1);
  });

  it("does not toggle when the already-selected mode is clicked again", () => {
    const onToggleMode = renderControls(false);
    fireEvent.click(screen.getByRole("button", { name: "自动" }));
    expect(onToggleMode).not.toHaveBeenCalled();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/conversation/__tests__/RunControls.test.tsx`
Expected: FAIL（旧实现是单个「自动/手动」按钮，没有 `aria-pressed`）。

- [ ] **Step 2: 改写运行控制条**

把 `athena-gui/src/components/conversation/RunControls.tsx` 整体替换为：

```tsx
import { Button, SegmentedControl, Spinner } from "@primer/react";
import type { PipelineViewModel } from "../../types/ui";
import { Icon } from "../common/Icon";
import styles from "./RunControls.module.css";

interface RunControlsProps {
  viewModel: PipelineViewModel;
  /** True when a real run has started or produced plans/attempts/SOTA activity. */
  active: boolean;
  onPause(): void;
  onResume(): void;
  onStop(): void;
  onToggleMode(): void;
}

const STATUS_LABEL: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "暂停",
  completed: "已完成",
  error: "错误",
};

/** Phase/status + runtime controls bar (pause/resume/stop + auto/manual). */
export function RunControls({ viewModel, active, onPause, onResume, onStop, onToggleMode }: RunControlsProps) {
  const { phase, status, manual, rightRail } = viewModel;
  const running = status === "running" && active;
  const paused = status === "paused" && active;
  const resumable = paused || viewModel.resumeAvailable;
  const terminal = status === "idle" || status === "completed" || status === "error" || !active;
  const best = rightRail.bestPrimary != null ? rightRail.bestPrimary.toFixed(4) : "--";

  return (
    <div className={styles.bar}>
      <div className={styles.status}>
        {running ? <Spinner size="small" srText={null} /> : <Icon name="sparkles" size={14} />}
        <strong className={styles.phase}>{phase || "idle"}</strong>
        <span className={styles.muted}>· {STATUS_LABEL[status] ?? status} ·</span>
        <SegmentedControl
          aria-label="假设选择模式"
          size="small"
          onChange={(index) => {
            // 只在切到另一种模式时通知，重复点击当前模式不触发切换。
            if ((index === 1) !== manual) onToggleMode();
          }}
        >
          <SegmentedControl.Button selected={!manual}>自动</SegmentedControl.Button>
          <SegmentedControl.Button selected={manual}>手动</SegmentedControl.Button>
        </SegmentedControl>
        <span className={styles.muted}>
          预算 {rightRail.budgetRemaining} · 最佳 {best}
        </span>
      </div>
      <div className={styles.actions}>
        <Button size="small" onClick={onPause} disabled={!running}>
          暂停
        </Button>
        <Button size="small" onClick={onResume} disabled={!resumable}>
          继续
        </Button>
        <Button size="small" variant="danger" onClick={onStop} disabled={terminal}>
          停止
        </Button>
      </div>
    </div>
  );
}
```

把 `athena-gui/src/components/conversation/RunControls.module.css` 整体替换为：

```css
/* 运行控制条：阶段与状态、假设选择模式、暂停/继续/停止。 */

.bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-12);
  padding: var(--base-size-8) var(--base-size-24);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-muted);
}

.status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--base-size-8);
  min-width: 0;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-default);
}

.phase {
  font-weight: var(--base-text-weight-semibold);
}

.muted {
  color: var(--fgColor-muted);
}

.actions {
  display: flex;
  gap: var(--base-size-8);
  flex-shrink: 0;
}
```

Run: `npm --prefix athena-gui test -- src/components/conversation/__tests__/RunControls.test.tsx`
Expected: 3 passed。

- [ ] **Step 3: 写确认卡标签的失败测试**

在 `athena-gui/src/components/cards/__tests__/IntentPreviewCard.test.tsx` 的 `describe` 末尾（最后一个 `it` 之后）追加：

```tsx
  it("shows the draft status and critical unresolved items as Primer labels", () => {
    render(
      <IntentPreviewCard preview={readyPreview({ status: "FAILED" })} onConfirm={noop} onRevise={noop} onRetry={noop} onCancel={noop} />,
    );

    expect(screen.getByText("FAILED")).toHaveAttribute("data-variant", "danger");
    expect(screen.getByText(/target/, { selector: "[data-variant]" })).toHaveAttribute("data-variant", "danger");
  });

  it("renders an unexpected draft status as a secondary label", () => {
    render(
      <IntentPreviewCard
        preview={readyPreview({ status: "ARCHIVED" as ClarificationPreview["status"] })}
        onConfirm={noop}
        onRevise={noop}
        onRetry={noop}
        onCancel={noop}
      />,
    );

    expect(screen.getByText("ARCHIVED")).toHaveAttribute("data-variant", "secondary");
  });
```

Run: `npm --prefix athena-gui test -- src/components/cards/__tests__/IntentPreviewCard.test.tsx`
Expected: 新增 2 个测试 FAIL（旧实现没有 `data-variant`）。

- [ ] **Step 4: 改写任务确认卡**

把 `athena-gui/src/components/cards/IntentPreviewCard.tsx` 整体替换为：

```tsx
import { useEffect, useState } from "react";
import { Button, Checkbox, FormControl, Label, Spinner, Textarea, type LabelColorOptions } from "@primer/react";
import type { ClarificationPreview } from "../../types/ui";
import { Icon } from "../common/Icon";
import styles from "./IntentPreviewCard.module.css";

interface IntentPreviewCardProps {
  preview: ClarificationPreview;
  onConfirm(acknowledgeUnresolved: boolean): Promise<void> | void;
  onRevise(instruction: string): Promise<void> | void;
  onRetry(): Promise<void> | void;
  onCancel(): Promise<void> | void;
  /** True when a confirmation/revise/retry/cancel is already in flight. */
  busy?: boolean;
}

const MAX_REVISION_LENGTH = 4000;
const READY = "READY_FOR_CONFIRMATION";
const FAILED = "FAILED";

function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      <span className={styles.fieldValue}>{value?.trim() ? value : "—"}</span>
    </div>
  );
}

/** 澄清草稿状态 → Primer Label 配色。 */
const STATUS_VARIANT: Record<string, LabelColorOptions> = {
  IDLE: "secondary",
  CLARIFYING: "attention",
  READY_FOR_CONFIRMATION: "accent",
  CONFIRMING: "accent",
  RUNNING: "success",
  FAILED: "danger",
};

function StatusLabel({ status }: { status: string }) {
  return <Label variant={STATUS_VARIANT[status] ?? "secondary"}>{status.replace(/_/g, " ")}</Label>;
}

/** Supervisor-style task understanding card; the decision gate before PREPARE. */
export function IntentPreviewCard({
  preview,
  onConfirm,
  onRevise,
  onRetry,
  onCancel,
  busy = false,
}: IntentPreviewCardProps) {
  const [revisionText, setRevisionText] = useState("");
  const [acknowledgeUnresolved, setAcknowledgeUnresolved] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  const pending = busy || pendingAction !== null;
  const hasCriticalUnresolved = preview.unresolved.some((item) => item.critical);
  const confirmationDisabled = pending || (hasCriticalUnresolved && !acknowledgeUnresolved);
  const revisionTextInvalid = !revisionText.trim() || revisionText.length > MAX_REVISION_LENGTH;

  // Local gate state is per displayed revision; reset when the revision changes.
  useEffect(() => {
    setRevisionText("");
    setAcknowledgeUnresolved(false);
    setPendingAction(null);
  }, [preview.draftId, preview.revision]);

  const runAction = async (name: string, action: () => Promise<void> | void) => {
    setPendingAction(name);
    try {
      await action();
    } finally {
      setPendingAction(null);
    }
  };

  const handleConfirm = () => {
    void runAction("confirm", () => onConfirm(acknowledgeUnresolved));
  };

  const handleRevise = () => {
    const instruction = revisionText.trim();
    if (!instruction || instruction.length > MAX_REVISION_LENGTH) return;
    void runAction("revise", async () => {
      await onRevise(instruction);
      setRevisionText("");
    });
  };

  const handleRetry = () => {
    void runAction("retry", () => onRetry());
  };

  const handleCancel = () => {
    void runAction("cancel", () => onCancel());
  };

  const isReady = preview.status === READY;
  const isFailed = preview.status === FAILED;
  const isRunning = preview.status === "RUNNING";
  const isConfirming = preview.status === "CONFIRMING" || pendingAction === "confirm";

  return (
    <section className={`${styles.card}${isRunning ? ` ${styles.cardStarted}` : ""}`}>
      <header className={styles.header}>
        <Icon name="sparkles" size={15} />
        <span className={styles.title}>{preview.understanding.title?.trim() || "任务理解"}</span>
        <StatusLabel status={preview.status} />
      </header>

      <div className={styles.grid}>
        <Field label="数据集" value={preview.understanding.dataset} />
        <Field label="目标" value={preview.understanding.target} />
        <Field label="任务类型" value={preview.understanding.task_type} />
        <Field
          label="主指标"
          value={
            preview.understanding.primary_metric || preview.understanding.direction
              ? `${preview.understanding.primary_metric ?? "—"} · ${preview.understanding.direction ?? "—"}`
              : null
          }
        />
        <Field label="评估方案" value={preview.understanding.evaluation_plan} />
      </div>

      {preview.answers.length > 0 && (
        <div className={styles.qa}>
          <div className={styles.qaTitle}>澄清问答</div>
          {preview.answers.map((answer) => (
            <div key={answer.request_id} className={styles.qaRow}>
              <span className={styles.question}>{answer.question}</span>
              <span className={styles.answer}>
                {answer.outcome === "skip"
                  ? "跳过"
                  : answer.outcome === "timeout"
                    ? "超时"
                    : answer.outcome === "cancelled"
                      ? "已取消"
                      : answer.choice_label ?? answer.value ?? ""}
              </span>
            </div>
          ))}
        </div>
      )}

      {preview.unresolved.length > 0 && (
        <div className={styles.unresolved}>
          <div className={styles.unresolvedTitle}>未解决项</div>
          {preview.unresolved.map((item) => (
            <div key={`${item.field}-${item.reason}`} className={styles.unresolvedRow}>
              <Label variant={item.critical ? "danger" : "attention"}>
                {item.field}
                {item.critical ? " · 关键" : ""}
              </Label>
              <span className={styles.unresolvedReason}>{item.reason}</span>
            </div>
          ))}
        </div>
      )}

      <footer className={styles.footer}>
        <div className={styles.meta}>
          <span>Revision {preview.revision}</span>
          {preview.failure && <span className={styles.failure}> {preview.failure.code}</span>}
        </div>

        {isRunning ? (
          <span className={styles.running}>
            <Spinner size="small" srText={null} /> 任务运行中…
            <Label variant="success">已启动</Label>
          </span>
        ) : isConfirming || (isReady && pending) ? (
          <span className={styles.running}>
            <Spinner size="small" srText={null} /> 正在处理…
          </span>
        ) : null}
      </footer>

      {isFailed && (
        <div className={styles.actions}>
          <Button variant="primary" onClick={handleRetry} disabled={pending}>
            Retry
          </Button>
          <Button onClick={handleCancel} disabled={pending}>
            Cancel
          </Button>
        </div>
      )}

      {isReady && (
        <div className={styles.actions}>
          <div className={styles.revise}>
            <FormControl id={`revise-${preview.draftId || "preview"}`}>
              <FormControl.Label>Revise with instruction</FormControl.Label>
              <Textarea
                block
                value={revisionText}
                onChange={(event) => setRevisionText(event.target.value)}
                maxLength={MAX_REVISION_LENGTH}
                rows={2}
                placeholder="Optional revision instruction"
              />
            </FormControl>
            <div className={styles.reviseActions}>
              {hasCriticalUnresolved && (
                <FormControl disabled={pending}>
                  <Checkbox
                    checked={acknowledgeUnresolved}
                    onChange={(event) => setAcknowledgeUnresolved(event.target.checked)}
                  />
                  <FormControl.Label>Acknowledge unresolved items</FormControl.Label>
                </FormControl>
              )}
              <Button variant="primary" onClick={handleConfirm} disabled={confirmationDisabled}>
                {isConfirming ? "Confirming…" : "确认并启动 (Confirm and start)"}
              </Button>
              <Button onClick={handleRevise} disabled={pending || revisionTextInvalid}>
                Revise
              </Button>
              <Button variant="invisible" onClick={handleCancel} disabled={pending}>
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}

      {preview.status === "CLARIFYING" && !isReady && !isFailed && !isRunning && (
        <p className={styles.hint}>任务理解中… 完成后将显示确认与启动。</p>
      )}
    </section>
  );
}
```

把 `athena-gui/src/components/cards/IntentPreviewCard.module.css` 整体替换为：

```css
/* 任务理解确认卡：PREPARE 前的决策门。形态参照 GitHub 评论框。 */

.card {
  margin-bottom: var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  overflow: hidden;
}

.cardStarted {
  border-color: var(--borderColor-success-muted);
}

.header {
  display: flex;
  align-items: center;
  gap: var(--base-size-8);
  padding: var(--base-size-8) var(--base-size-16);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-muted);
}

.title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: var(--base-text-weight-semibold);
}

.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--base-size-8) var(--base-size-24);
  padding: var(--base-size-16);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
  min-width: 0;
}

.fieldLabel {
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.fieldValue {
  font-size: var(--text-body-size-medium);
  word-break: break-word;
}

.qa,
.unresolved {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
  padding: 0 var(--base-size-16) var(--base-size-16);
}

.qaTitle,
.unresolvedTitle {
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.qaRow,
.unresolvedRow {
  display: flex;
  align-items: baseline;
  gap: var(--base-size-8);
  font-size: var(--text-body-size-medium);
}

.question,
.unresolvedReason {
  color: var(--fgColor-muted);
}

.answer {
  font-weight: var(--base-text-weight-semibold);
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
  padding: var(--base-size-8) var(--base-size-16);
  border-top: var(--borderWidth-thin) solid var(--borderColor-muted);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.failure {
  font-family: var(--fontStack-monospace);
  color: var(--fgColor-danger);
}

.running {
  display: inline-flex;
  align-items: center;
  gap: var(--base-size-8);
}

.actions {
  display: flex;
  gap: var(--base-size-8);
  padding: 0 var(--base-size-16) var(--base-size-16);
}

.revise {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
}

.reviseActions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--base-size-8);
}

.hint {
  margin: 0;
  padding: 0 var(--base-size-16) var(--base-size-16);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}
```

Run: `npm --prefix athena-gui test -- src/components/cards/__tests__/IntentPreviewCard.test.tsx`
Expected: 7 passed。

- [ ] **Step 5: 改写输入框、欢迎页、待选假设**

把 `athena-gui/src/components/conversation/Composer.tsx` 整体替换为：

```tsx
import { FormEvent, useState } from "react";
import { Button, Textarea } from "@primer/react";
import { PaperAirplaneIcon } from "@primer/octicons-react";
import styles from "./Composer.module.css";

interface ComposerProps {
  onSend(message: string): Promise<void>;
  disabled?: boolean;
}

/** Message input area with a textarea and send button. */
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
    <form className={styles.composer} onSubmit={(event) => void handleSubmit(event)}>
      <Textarea
        block
        resize="vertical"
        rows={3}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="描述你的 ML 任务..."
        aria-label="描述你的 ML 任务"
      />
      <div className={styles.footer}>
        <span className={styles.hint}>支持命令：/pause /resume /stop /manual /auto /select &lt;id&gt; /help</span>
        <Button type="submit" variant="primary" trailingVisual={PaperAirplaneIcon} disabled={disabled}>
          发送
        </Button>
      </div>
    </form>
  );
}
```

把 `athena-gui/src/components/conversation/Composer.module.css` 整体替换为：

```css
/* 输入区：多行文本框 + 命令提示 + 发送按钮。 */

.composer {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
  padding: var(--base-size-12) var(--base-size-24) var(--base-size-16);
  border-top: var(--borderWidth-thin) solid var(--borderColor-muted);
  background: var(--bgColor-default);
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
}

.hint {
  min-width: 0;
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}
```

把 `athena-gui/src/components/conversation/WelcomeHero.tsx` 整体替换为：

```tsx
import { Button, Heading } from "@primer/react";
import { Icon } from "../common/Icon";
import styles from "./WelcomeHero.module.css";

const EXAMPLE_PROMPTS = [
  "用图像分类数据集，优化验证集准确率",
  "在文本数据上做回归，降低预测误差",
  "对现有模型做超参数搜索，提升 F1 分数",
];

interface WelcomeHeroProps {
  onPrompt(prompt: string): void;
}

/** Empty-conversation landing with brand, tagline, and clickable example prompts. */
export function WelcomeHero({ onPrompt }: WelcomeHeroProps) {
  return (
    <div className={styles.hero}>
      <span className={styles.mark} aria-hidden="true">
        <Icon name="sparkles" size={24} />
      </span>
      <Heading as="h2" variant="medium">Athena</Heading>
      <p className={styles.subtitle}>对话式 ML 研究编排 · 从假设到实验全自动</p>
      <div className={styles.prompts}>
        {EXAMPLE_PROMPTS.map((prompt) => (
          <Button key={prompt} size="small" onClick={() => onPrompt(prompt)}>
            {prompt}
          </Button>
        ))}
      </div>
    </div>
  );
}
```

把 `athena-gui/src/components/conversation/WelcomeHero.module.css` 整体替换为：

```css
/* 空会话欢迎页：标识、标语、示例提示词。 */

.hero {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--base-size-8);
  padding: var(--base-size-40) var(--base-size-24);
  text-align: center;
}

.mark {
  display: grid;
  place-items: center;
  width: var(--base-size-48);
  height: var(--base-size-48);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-large);
  background: var(--bgColor-muted);
  color: var(--fgColor-default);
}

.subtitle {
  margin: 0;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-muted);
}

.prompts {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--base-size-8);
  max-width: 560px;
  margin-top: var(--base-size-8);
}
```

把 `athena-gui/src/components/conversation/PendingHypotheses.tsx` 整体替换为：

```tsx
import { Button, CounterLabel, Label } from "@primer/react";
import styles from "./PendingHypotheses.module.css";

interface PendingHypothesesProps {
  pending: Array<{ id: string; statement: string }>;
  onSelect(id: string): void;
}

/** Manual-mode hypothesis selector shown when PROPOSED hypotheses await selection. */
export function PendingHypotheses({ pending, onSelect }: PendingHypothesesProps) {
  if (pending.length === 0) return null;

  return (
    <section className={styles.panel} aria-label="待选假设">
      <div className={styles.title}>
        待选假设 <CounterLabel>{pending.length}</CounterLabel>
      </div>
      <ul className={styles.list}>
        {pending.map((h) => (
          <li key={h.id} className={styles.item}>
            <Label variant="accent">PROPOSED</Label>
            <span className={styles.statement}>{h.statement}</span>
            <Button size="small" onClick={() => onSelect(h.id)}>
              选择
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
```

把 `athena-gui/src/components/conversation/PendingHypotheses.module.css` 整体替换为：

```css
/* 手动模式下的待选假设列表。 */

.panel {
  flex-shrink: 0;
  margin: var(--base-size-12) var(--base-size-24) 0;
  border: var(--borderWidth-thin) solid var(--borderColor-accent-muted);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  overflow: hidden;
}

.title {
  display: flex;
  align-items: center;
  gap: var(--base-size-8);
  padding: var(--base-size-8) var(--base-size-16);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-accent-muted);
  background: var(--bgColor-accent-muted);
  font-size: var(--text-body-size-medium);
  font-weight: var(--base-text-weight-semibold);
}

.list {
  list-style: none;
  margin: 0;
  padding: var(--base-size-8) var(--base-size-16);
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
}

.item {
  display: flex;
  align-items: center;
  gap: var(--base-size-12);
  font-size: var(--text-body-size-medium);
}

.statement {
  flex: 1;
  min-width: 0;
  color: var(--fgColor-default);
}
```

- [ ] **Step 6: 消息流改用 Primer 样式**

把 `athena-gui/src/components/conversation/MessageList.tsx` 整体替换为：

```tsx
import { memo, useEffect, useRef, useState } from "react";
import { Flash } from "@primer/react";
import type { ClarificationPreview, UIMessage, UIMessagePreview } from "../../types/ui";
import { IntentPreviewCard } from "../cards/IntentPreviewCard";
import { CodeBlock } from "../common/CodeBlock";
import { Icon } from "../common/Icon";
import styles from "./MessageList.module.css";

interface MessageListProps {
  messages: UIMessage[];
  onConfirmPreview(acknowledgeUnresolved: boolean): Promise<void> | void;
  onRevisePreview(instruction: string): Promise<void> | void;
  onRetryPreview(): Promise<void> | void;
  onCancelPreview(): Promise<void> | void;
}

/** True for parallel Ideator lanes (plan ids like ``ideator-1`` or ``ideator-<round>-1``). */
function ideatorNumber(plan?: string): number | null {
  if (!plan) return null;
  const match = /^ideator-(?:\d+-)?([1-9][0-9]*)$/.exec(plan);
  return match ? Number(match[1]) : null;
}

type MessageSegment =
  | { kind: "ordinary"; items: UIMessage[] }
  | { kind: "ideator"; lane: number; items: UIMessage[] }
  | { kind: "clarification"; items: UIMessage[] };

interface ActiveClarificationPreview {
  index: number;
  preview: ClarificationPreview;
}

function activeClarificationPreview(
  messages: UIMessage[],
): ActiveClarificationPreview | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const msg = messages[index];
    if (msg.kind !== "intent-preview" || !msg.preview || !("draftId" in msg.preview)) continue;
    return msg.preview.status === "CLARIFYING" || msg.preview.status === "CONFIRMING"
      ? { index, preview: msg.preview }
      : null;
  }
  return null;
}

function belongsToTaskUnderstanding(
  message: UIMessage,
  preview: ClarificationPreview,
): boolean {
  if (message.scope === "task_understanding") {
    const acceptedScopeId = preview.draftId || preview.optimisticScopeId;
    return Boolean(acceptedScopeId) && message.scopeId === acceptedScopeId;
  }

  return message.sessionId === undefined
    && message.scope === undefined
    && message.scopeId === undefined;
}

function clarificationSegmentKey(segment: Extract<MessageSegment, { kind: "clarification" }>): string {
  const first = segment.items[0];
  const id = first?.id?.trim();
  if (id) return `clarification-${id}`;

  // UIMessage.id is required, but retain a deterministic, index-free fallback
  // for malformed restored records so nearby inserts do not remount this log.
  const fingerprint = [
    first?.role,
    first?.kind,
    first?.source,
    first?.channel,
    first?.tool,
    first?.plan,
    first?.content,
  ];
  return `clarification-fallback-${JSON.stringify(fingerprint)}`;
}

/** Group ideator lanes and backend output emitted during active task understanding. */
function segmentMessages(messages: UIMessage[]): MessageSegment[] {
  const segments: MessageSegment[] = [];
  const clarification = activeClarificationPreview(messages);

  for (const [index, msg] of messages.entries()) {
    const lane = ideatorNumber(msg.plan);
    const last = segments[segments.length - 1];

    if (
      clarification !== null &&
      index > clarification.index &&
      msg.role !== "user" &&
      msg.kind !== "intent-preview" &&
      belongsToTaskUnderstanding(msg, clarification.preview)
    ) {
      if (last?.kind === "clarification") {
        last.items.push(msg);
      } else {
        segments.push({ kind: "clarification", items: [msg] });
      }
    } else if (lane !== null && last?.kind === "ideator" && last.lane === lane) {
      last.items.push(msg);
    } else if (lane !== null) {
      segments.push({ kind: "ideator", lane, items: [msg] });
    } else {
      segments.push({ kind: "ordinary", items: [msg] });
    }
  }
  return segments;
}

function toClarificationPreview(
  preview: UIMessagePreview,
  started: boolean,
): ClarificationPreview {
  if ("draftId" in preview) return preview;
  return {
    draftId: "",
    revision: 0,
    status: started ? "RUNNING" : "READY_FOR_CONFIRMATION",
    understanding: {
      title: preview.title ?? "",
      dataset: preview.dataset ?? null,
      target: preview.target ?? null,
      task_type: preview.task_type ?? "other",
      primary_metric: preview.primary_metric ?? null,
      direction: preview.direction ?? null,
      evaluation_plan: preview.evaluation_plan ?? null,
    },
    answers: [],
    unresolved: [],
    failure: null,
  };
}

interface TrajectoryItemProps {
  msg: UIMessage;
  onConfirmPreview: MessageListProps["onConfirmPreview"];
  onRevisePreview: MessageListProps["onRevisePreview"];
  onRetryPreview: MessageListProps["onRetryPreview"];
  onCancelPreview: MessageListProps["onCancelPreview"];
}

/** A single trajectory record (user / supervisor / agent / tool / error). */
const TrajectoryItem = memo(function TrajectoryItem({ msg, onConfirmPreview, onRevisePreview, onRetryPreview, onCancelPreview }: TrajectoryItemProps) {
  if (msg.kind === "intent-preview" && msg.preview) {
    const preview = toClarificationPreview(msg.preview, msg.started === true);
    // While the authoritative draft is still being built, keep the chat focused
    // on the task-understanding conversation. The decision card appears only
    // once the draft is ready/failed/running (or for legacy previews).
    if ("draftId" in msg.preview && preview.status === "CLARIFYING") {
      return (
        <article className={`${styles["message-bubble"]} ${styles["message-bubble--supervisor"]}`}>
          <span className={`${styles.marker} ${styles["marker--supervisor"]}`}>任务理解</span>
          {msg.content || "任务理解中…"}
        </article>
      );
    }
    return (
      <IntentPreviewCard
        preview={preview}
        onConfirm={onConfirmPreview}
        onRevise={onRevisePreview}
        onRetry={onRetryPreview}
        onCancel={onCancelPreview}
      />
    );
  }
  if (msg.kind === "error") {
    return (
      <Flash variant="danger" className={styles.error}>
        <h3 className={styles.errorTitle}>错误</h3>
        <p className={styles.errorBody}>{msg.content}</p>
      </Flash>
    );
  }
  if (msg.role === "user") {
    return (
      <article className={`${styles["message-bubble"]} ${styles["message-bubble--user"]}`}>
        {msg.content}
      </article>
    );
  }
  if (msg.source === "tool") {
    const label = msg.tool || "tool";
    return (
      <details className={styles["tool-output"]}>
        <summary>
          {label} · {msg.channel ?? "output"}
        </summary>
        <CodeBlock className={styles.flush}>{msg.content}</CodeBlock>
      </details>
    );
  }
  if (msg.tool) {
    return (
      <article className={styles["tool-call"]}>
        <div className={styles["tool-call__head"]}>
          <Icon name="wrench" size={13} /> {msg.tool}
        </div>
        <CodeBlock className={styles.flush}>{msg.content}</CodeBlock>
      </article>
    );
  }

  const supervisor = msg.source === "supervisor";
  return (
    <article
      className={`${styles["message-bubble"]} ${
        supervisor ? styles["message-bubble--supervisor"] : styles["message-bubble--agent"]
      }`}
    >
      <span className={`${styles.marker} ${supervisor ? styles["marker--supervisor"] : styles["marker--agent"]}`}>
        {supervisor ? "监督者" : "Agent"}
      </span>
      {msg.content}
    </article>
  );
}, (previous, next) => {
  if (previous.msg !== next.msg) return false;
  if (next.msg.kind !== "intent-preview") return true;
  return previous.onConfirmPreview === next.onConfirmPreview
    && previous.onRevisePreview === next.onRevisePreview
    && previous.onRetryPreview === next.onRetryPreview
    && previous.onCancelPreview === next.onCancelPreview;
});

/** Renders chat messages, intent preview cards, and the trajectory (agent/tool/supervisor/ideator). */
export function MessageList({
  messages,
  onConfirmPreview,
  onRevisePreview,
  onRetryPreview,
  onCancelPreview,
}: MessageListProps) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const segments = segmentMessages(messages);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;

    const update = () => {
      setShowJumpToBottom(el.scrollHeight - el.scrollTop - el.clientHeight > 120);
    };

    update();
    el.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      el.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [messages.length]);

  const scrollToBottom = () => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

  return (
    <div className={styles["message-list"]} role="log" aria-live="polite" ref={listRef}>
      {segments.map((segment, index) => {
        if (segment.kind === "ordinary") {
          return segment.items.map((msg) => (
            <TrajectoryItem
              key={msg.id}
              msg={msg}
              onConfirmPreview={onConfirmPreview}
              onRevisePreview={onRevisePreview}
              onRetryPreview={onRetryPreview}
              onCancelPreview={onCancelPreview}
            />
          ));
        }
        if (segment.kind === "clarification") {
          return (
            <section
              key={clarificationSegmentKey(segment)}
              className={styles["clarification-activity"]}
              role="log"
              aria-label="任务理解过程"
              aria-live="polite"
            >
              <div className={styles["clarification-activity__title"]}>任务理解过程</div>
              {segment.items.map((msg) => (
                <TrajectoryItem
                  key={msg.id}
                  msg={msg}
                  onConfirmPreview={onConfirmPreview}
                  onRevisePreview={onRevisePreview}
                  onRetryPreview={onRetryPreview}
                  onCancelPreview={onCancelPreview}
                />
              ))}
            </section>
          );
        }
        return (
          <section key={`ideator-${segment.lane}-${index}`} className={styles["ideator-lane"]}>
            <div className={styles["ideator-lane__title"]}>Ideator {segment.lane}</div>
            {segment.items.map((msg) => (
              <TrajectoryItem
                key={msg.id}
                msg={msg}
                onConfirmPreview={onConfirmPreview}
                onRevisePreview={onRevisePreview}
                onRetryPreview={onRetryPreview}
                onCancelPreview={onCancelPreview}
              />
            ))}
          </section>
        );
      })}
      {showJumpToBottom && (
        <button
          type="button"
          className={styles["jump-to-bottom"]}
          onClick={scrollToBottom}
          aria-label="返回底部"
          title="返回底部"
        >
          <Icon name="chevronDown" size={16} />
          <span>返回底部</span>
        </button>
      )}
    </div>
  );
}
```

把 `athena-gui/src/components/conversation/MessageList.module.css` 整体替换为：

```css
/* 会话轨迹：用户/监督者/Agent 消息、工具调用与输出、任务理解过程、Ideator 并行车道。
   形态参照 GitHub 评论框：边框 + 标题栏，全部使用 Primer 变量。 */

.message-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--base-size-16) var(--base-size-24);
  scroll-behavior: smooth;
}

.message-list > * {
  max-width: 880px;
  margin-left: auto;
  margin-right: auto;
}

.jump-to-bottom {
  position: sticky;
  bottom: var(--base-size-12);
  display: flex;
  align-items: center;
  gap: var(--base-size-4);
  margin: 0 0 0 auto;
  padding: var(--base-size-4) var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-full);
  background: var(--bgColor-default);
  color: var(--fgColor-default);
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  cursor: pointer;
  box-shadow: var(--shadow-floating-small);
}

.message-bubble {
  margin-bottom: var(--base-size-12);
  padding: var(--base-size-8) var(--base-size-16);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  color: var(--fgColor-default);
  font-size: var(--text-body-size-medium);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.message-list > .message-bubble--user {
  max-width: 720px;
}

.message-bubble--user {
  border-color: var(--borderColor-accent-muted);
  background: var(--bgColor-accent-muted);
}

.message-bubble--supervisor {
  border-color: var(--borderColor-attention-muted);
}

.message-bubble--agent {
  background: var(--bgColor-muted);
}

.marker {
  display: inline-block;
  margin-right: var(--base-size-8);
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
}

.marker--supervisor {
  color: var(--fgColor-attention);
}

.marker--agent {
  color: var(--fgColor-accent);
}

.tool-call,
.tool-output {
  margin-bottom: var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  overflow: hidden;
}

.tool-call__head,
.tool-output summary {
  display: flex;
  align-items: center;
  gap: var(--base-size-4);
  padding: var(--base-size-8) var(--base-size-12);
  background: var(--bgColor-muted);
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.tool-call__head {
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
}

.tool-output summary {
  cursor: pointer;
}

.tool-output[open] summary {
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
}

.flush {
  border: 0;
  border-radius: 0;
}

.error {
  margin-bottom: var(--base-size-12);
}

.errorTitle {
  margin: 0 0 var(--base-size-4);
  font-size: var(--text-body-size-medium);
  font-weight: var(--base-text-weight-semibold);
}

.errorBody {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.clarification-activity,
.ideator-lane {
  margin-bottom: var(--base-size-12);
  padding: var(--base-size-8) var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
}

.clarification-activity {
  border-left: var(--base-size-4) solid var(--borderColor-attention-emphasis);
}

.ideator-lane {
  border-left: var(--base-size-4) solid var(--borderColor-done-emphasis);
}

.clarification-activity__title,
.ideator-lane__title {
  margin-bottom: var(--base-size-8);
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}
```

- [ ] **Step 7: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/__tests__/conversation-pane.test.tsx src/components/conversation src/components/cards`
Expected: 全部通过（会话 19 个、运行控制 3 个、确认卡 7 个）。现有断言依赖的文字（「暂停」「继续」「停止」「已启动」「任务理解过程」日志区等）均保留。

Run: `npm --prefix athena-gui test` → 213 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 8: 提交**

```bash
git add athena-gui/src/components/conversation/RunControls.tsx athena-gui/src/components/conversation/RunControls.module.css athena-gui/src/components/conversation/__tests__/RunControls.test.tsx athena-gui/src/components/conversation/Composer.tsx athena-gui/src/components/conversation/Composer.module.css athena-gui/src/components/conversation/WelcomeHero.tsx athena-gui/src/components/conversation/WelcomeHero.module.css athena-gui/src/components/conversation/PendingHypotheses.tsx athena-gui/src/components/conversation/PendingHypotheses.module.css athena-gui/src/components/conversation/MessageList.tsx athena-gui/src/components/conversation/MessageList.module.css athena-gui/src/components/cards/IntentPreviewCard.tsx athena-gui/src/components/cards/IntentPreviewCard.module.css athena-gui/src/components/cards/__tests__/IntentPreviewCard.test.tsx
git commit -m "feat(gui): 会话区改用 Primer 组件"
```

---

### Task 6: 设置弹窗改为 Primer Dialog

**Files:**
- Modify: `athena-gui/src/components/SettingsPanel.tsx`、`SettingsPanel.module.css`
- Modify: `athena-gui/src/components/__tests__/SettingsPanel.test.tsx`

**Interfaces:**
- Consumes: Task 3 中 `AppShell` 以 `settingsOpen` 状态渲染 `<SettingsPanel onClose={...} />`。
- Produces: `SettingsPanel({ onClose })` 不变；弹窗可访问名「运行设置」，关闭按钮可访问名「关闭设置」，Esc 调用 `onClose`。

**实现要点（实测得出）：**
- Primer Dialog 自带的关闭按钮用 `aria-labelledby` 指向提示浮层，jsdom 中无可访问名，且真实浏览器读作英文 "Close"。因此用 `renderHeader` 自定义页头，放一个 `aria-label="关闭设置"` 的 `IconButton`，与旧实现的可访问名一致。
- 三个开关用 `Checkbox` + `FormControl`（GitHub 设置页对布尔项就是这种控件），保留复选框语义；`FormControl disabled` 会传给内部 Checkbox。
- 「跳过 VALIDATE」的说明用 `FormControl.Caption`，Primer 自动写入 `aria-describedby`（id 为 `skip-validate-caption`）。测试改用 `toHaveAccessibleDescription` 断言，不再写死 id。
- 弹窗经 Portal 渲染：测试里不能用 `render()` 返回的 `container` 查找弹窗内元素。
- 原实现页脚压住表单的布局问题随 Dialog 自带的滚动区一并消失（Task 12 实机核对）。

- [ ] **Step 1: 更新测试（含两个新增测试）**

把 `athena-gui/src/components/__tests__/SettingsPanel.test.tsx` 整体替换为：

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_GUI_SETTINGS } from "../../lib/tauri-bridge";
import { SettingsPanel } from "../SettingsPanel";

const bridgeMocks = vi.hoisted(() => ({
  settingsGet: vi.fn(),
  settingsSet: vi.fn(),
  setProjectRoot: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  settingsGet: bridgeMocks.settingsGet,
  settingsSet: bridgeMocks.settingsSet,
  setProjectRoot: bridgeMocks.setProjectRoot,
}));

function settings(overrides: Record<string, unknown> = {}) {
  return {
    ...DEFAULT_GUI_SETTINGS,
    project_root: "C:/project",
    auto_validate: true,
    skip_validate: false,
    ...overrides,
  };
}

describe("SettingsPanel validation controls", () => {
  beforeEach(() => {
    bridgeMocks.settingsGet.mockReset();
    bridgeMocks.settingsSet.mockReset();
    bridgeMocks.setProjectRoot.mockReset();
  });

  it("saves skip validate while preserving the automatic validation choice", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);

    const skip = await screen.findByRole("checkbox", { name: "跳过 VALIDATE" });
    const automatic = screen.getByRole("checkbox", { name: "自动验证" });
    fireEvent.click(skip);

    expect(automatic).toBeDisabled();
    expect(automatic).toBeChecked();
    const help = screen.getByText(/SEARCH.*Final/);
    expect(help).toBeVisible();
    expect(skip).toHaveAccessibleDescription(/不会运行独立最终评估/);
    expect(skip).toHaveAccessibleDescription(/不会产生最终测试或泛化指标/);

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());
    const lastSettingsPatch = bridgeMocks.settingsSet.mock.calls[
      bridgeMocks.settingsSet.mock.calls.length - 1
    ]?.[0];
    expect(lastSettingsPatch).toMatchObject({
      auto_validate: true,
      skip_validate: true,
    });

    fireEvent.click(skip);
    expect(automatic).toBeEnabled();
    expect(automatic).toBeChecked();
  });

  it("renders a loaded skipped-validation snapshot", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings({ skip_validate: true }));

    render(<SettingsPanel onClose={vi.fn()} />);

    const skip = await screen.findByRole("checkbox", { name: "跳过 VALIDATE" });
    const automatic = screen.getByRole("checkbox", { name: "自动验证" });
    expect(skip).toBeChecked();
    expect(automatic).toBeDisabled();
    expect(automatic).toBeChecked();
    expect(screen.getByText(/SEARCH.*Final/)).toBeVisible();
  });

  it("does not submit a loaded masked API key", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings({
      model_connection: {
        ...DEFAULT_GUI_SETTINGS.model_connection,
        llm_api_key: "sec********alue",
      },
    }));
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);
    await screen.findByDisplayValue("C:/project");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());

    const calls = bridgeMocks.settingsSet.mock.calls;
    const patch = calls[calls.length - 1]?.[0];
    expect(patch.model_connection).not.toHaveProperty("llm_api_key");
  });

  it("submits an API key only after the user enters a new value", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings({
      model_connection: {
        ...DEFAULT_GUI_SETTINGS.model_connection,
        llm_api_key: "sec********alue",
      },
    }));
    bridgeMocks.settingsSet.mockImplementation(async (patch) => settings(patch));

    render(<SettingsPanel onClose={vi.fn()} />);
    await screen.findByDisplayValue("C:/project");
    // 弹窗经 Portal 渲染，不在 render() 返回的 container 内。
    const input = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(input, { target: { value: "new-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());

    const calls = bridgeMocks.settingsSet.mock.calls;
    const patch = calls[calls.length - 1]?.[0];
    expect(patch.model_connection).toMatchObject({ llm_api_key: "new-secret" });
  });

  it("closes on Escape", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    const onClose = vi.fn();

    render(<SettingsPanel onClose={onClose} />);
    const dialog = await screen.findByRole("dialog", { name: "运行设置" });
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the dialog open and shows the error when saving fails", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings());
    bridgeMocks.settingsSet.mockRejectedValue(new Error("settings write failed"));
    const onClose = vi.fn();

    render(<SettingsPanel onClose={onClose} />);
    await screen.findByDisplayValue("C:/project");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByText("settings write failed")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "运行设置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/__tests__/SettingsPanel.test.tsx`
Expected: FAIL——旧实现的复选框没有可访问描述关联到 `FormControl.Caption`（`toHaveAccessibleDescription` 失败），旧实现 Esc 不会关闭。

- [ ] **Step 2: 改写设置弹窗**

把 `athena-gui/src/components/SettingsPanel.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  DEFAULT_GUI_SETTINGS,
  setProjectRoot,
  settingsGet,
  settingsSet,
  type ComputeSettings,
  type GuiSettings,
} from "../lib/tauri-bridge";
import { Button, Checkbox, Dialog, Flash, FormControl, Heading, IconButton, Select, TextInput } from "@primer/react";
import { XIcon } from "@primer/octicons-react";
import styles from "./SettingsPanel.module.css";

type WritableField =
  | "concurrency"
  | "search_limit"
  | "ideation"
  | "direction"
  | "tolerance"
  | "auto_validate"
  | "skip_validate"
  | "manual_mode"
  | "experiment_timeout_s";

interface SettingsPanelProps {
  onClose(): void;
}

function normalizeSettings(raw: Partial<GuiSettings> | GuiSettings): GuiSettings {
  return {
    ...DEFAULT_GUI_SETTINGS,
    ...raw,
    model_connection: {
      ...DEFAULT_GUI_SETTINGS.model_connection,
      ...(raw.model_connection ?? {}),
    },
    compute: {
      ...DEFAULT_GUI_SETTINGS.compute,
      ...(raw.compute ?? {}),
      hosts: raw.compute?.hosts ?? [],
    },
  };
}

function editableSettings(settings: GuiSettings): GuiSettings {
  return {
    ...settings,
    model_connection: { ...settings.model_connection, llm_api_key: "" },
  };
}

/** 运行设置弹窗（Primer Dialog）：项目目录、模型连接、运行参数、算力与开关。 */
export function SettingsPanel({ onClose }: SettingsPanelProps) {
  const [settings, setSettings] = useState<GuiSettings>(DEFAULT_GUI_SETTINGS);
  const [form, setForm] = useState<GuiSettings>(DEFAULT_GUI_SETTINGS);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [projectRoot, setProjectRootInput] = useState("");
  const [switching, setSwitching] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = normalizeSettings(await settingsGet());
      setSettings(next);
      setForm(editableSettings(next));
      setProjectRootInput(next.project_root || "");
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function patchField(field: WritableField, value: unknown) {
    setSaved(false);
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  function patchModelConnection(
    field: keyof GuiSettings["model_connection"],
    value: string,
  ) {
    setSaved(false);
    setForm((prev) => ({
      ...prev,
      model_connection: { ...prev.model_connection, [field]: value },
    }));
  }

  function patchCompute(field: keyof ComputeSettings, value: unknown) {
    setSaved(false);
    setForm((prev) => ({
      ...prev,
      compute: { ...prev.compute, [field]: value },
    }));
  }

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    const { llm_api_key, ...modelConnection } = form.model_connection;
    const modelConnectionPatch = llm_api_key.trim()
      ? { ...modelConnection, llm_api_key: llm_api_key.trim() }
      : modelConnection;
    const patch: Partial<GuiSettings> = {
      concurrency: form.concurrency,
      search_limit: form.search_limit,
      ideation: form.ideation,
      direction: form.direction,
      tolerance: form.tolerance,
      auto_validate: form.auto_validate,
      skip_validate: form.skip_validate,
      manual_mode: form.manual_mode,
      experiment_timeout_s: form.experiment_timeout_s,
      data_root: form.data_root,
      compute: form.compute,
      model_connection: modelConnectionPatch as GuiSettings["model_connection"],
    };
    try {
      const next = normalizeSettings(await settingsSet(patch));
      setSettings(next);
      setForm(editableSettings(next));
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function switchProject() {
    setSwitching(true);
    setError(null);
    setSaved(false);
    try {
      const next = normalizeSettings(await setProjectRoot(projectRoot));
      setSettings(next);
      setForm(editableSettings(next));
      setProjectRootInput(next.project_root || "");
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSwitching(false);
    }
  }

  return (
    <Dialog
      width="xlarge"
      onClose={onClose}
      renderHeader={({ dialogLabelId, dialogDescriptionId }) => (
        <Dialog.Header className={styles.header}>
          <div className={styles.headerText}>
            <Dialog.Title id={dialogLabelId}>运行设置</Dialog.Title>
            <Dialog.Subtitle id={dialogDescriptionId}>配置 Athena 运行参数与项目目录</Dialog.Subtitle>
          </div>
          <IconButton icon={XIcon} variant="invisible" aria-label="关闭设置" onClick={onClose} />
        </Dialog.Header>
      )}
      footerButtons={[
        { content: "刷新", buttonType: "default", onClick: () => void refresh() },
        {
          content: saving ? "保存中…" : "保存",
          buttonType: "primary",
          onClick: () => void save(),
          disabled: saving,
        },
      ]}
    >
      {error && (
        <Flash variant="danger" className={styles.flash}>
          {error}
        </Flash>
      )}
      {saved && !error && (
        <Flash variant="success" className={styles.flash}>
          设置已保存
        </Flash>
      )}

      <section className={styles.section}>
        <Heading as="h3" variant="small">项目</Heading>
        <div className={styles.projectRow}>
          <TextInput
            block
            aria-label="项目目录"
            placeholder="选择项目目录…"
            value={projectRoot}
            onChange={(e) => setProjectRootInput(e.target.value)}
          />
          <Button variant="primary" onClick={() => void switchProject()} disabled={switching || !projectRoot.trim()}>
            {switching ? "切换中…" : "切换"}
          </Button>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">模型连接</Heading>
        <div className={styles.grid}>
          <FormControl>
            <FormControl.Label>PROVIDER</FormControl.Label>
            <Select
              block
              value={form.model_connection.provider}
              onChange={(e) => patchModelConnection("provider", e.target.value)}
            >
              <Select.Option value="deepseek">deepseek</Select.Option>
              <Select.Option value="openai">openai</Select.Option>
              <Select.Option value="qwen">qwen</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>MODEL_NAME</FormControl.Label>
            <TextInput
              block
              placeholder="deepseek-v4-flash"
              value={form.model_connection.model_name}
              onChange={(e) => patchModelConnection("model_name", e.target.value)}
            />
          </FormControl>

          <FormControl className={styles.full}>
            <FormControl.Label>BASE_URL</FormControl.Label>
            <TextInput
              block
              placeholder={
                form.model_connection.provider === "deepseek"
                  ? "https://api.deepseek.com"
                  : form.model_connection.provider === "openai"
                    ? "https://api.openai.com/v1"
                    : "https://dashscope.aliyuncs.com/compatible-mode/v1"
              }
              value={form.model_connection.base_url}
              onChange={(e) => patchModelConnection("base_url", e.target.value)}
            />
          </FormControl>

          <FormControl className={styles.full}>
            <FormControl.Label>LLM_API_KEY</FormControl.Label>
            <TextInput
              block
              type="password"
              autoComplete="off"
              placeholder={
                settings.model_connection.llm_api_key
                  ? `已设置 · ${settings.model_connection.llm_api_key}`
                  : "未设置"
              }
              value={form.model_connection.llm_api_key}
              onChange={(e) => patchModelConnection("llm_api_key", e.target.value)}
            />
            <FormControl.Caption>密钥只写回 .env，界面不回显明文；留空表示不修改。</FormControl.Caption>
          </FormControl>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">运行参数</Heading>
        <div className={styles.grid}>
          <FormControl>
            <FormControl.Label>并发度</FormControl.Label>
            <TextInput
              block
              type="number"
              min={1}
              value={String(form.concurrency)}
              onChange={(e) => patchField("concurrency", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>搜索上限</FormControl.Label>
            <TextInput
              block
              type="number"
              min={0}
              value={String(form.search_limit)}
              onChange={(e) => patchField("search_limit", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>Ideator 机制</FormControl.Label>
            <Select block value={form.ideation} onChange={(e) => patchField("ideation", e.target.value)}>
              <Select.Option value="ideageneration">ideageneration · 门禁生成</Select.Option>
              <Select.Option value="baseline">baseline · 消融对照</Select.Option>
              <Select.Option value="debate">debate · 辩论式</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>优化方向</FormControl.Label>
            <Select block value={form.direction} onChange={(e) => patchField("direction", e.target.value)}>
              <Select.Option value="maximize">maximize</Select.Option>
              <Select.Option value="minimize">minimize</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>容忍度</FormControl.Label>
            <TextInput
              block
              type="number"
              min={0}
              step="0.01"
              value={String(form.tolerance)}
              onChange={(e) => patchField("tolerance", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>实验超时（秒）</FormControl.Label>
            <TextInput
              block
              type="number"
              min={1}
              value={String(form.experiment_timeout_s)}
              onChange={(e) => patchField("experiment_timeout_s", Number(e.target.value))}
            />
          </FormControl>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">算力 / Compute</Heading>
        <div className={styles.grid}>
          <FormControl>
            <FormControl.Label>模式</FormControl.Label>
            <Select block value={form.compute.mode} onChange={(e) => patchCompute("mode", e.target.value)}>
              <Select.Option value="local">local · 本机</Select.Option>
              <Select.Option value="ssh">ssh · 远程 GPU</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>放置策略</FormControl.Label>
            <Select block value={form.compute.placement} onChange={(e) => patchCompute("placement", e.target.value)}>
              <Select.Option value="pack">pack</Select.Option>
              <Select.Option value="spread">spread</Select.Option>
              <Select.Option value="homogeneous">homogeneous</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>拿不到卡时</FormControl.Label>
            <Select block value={form.compute.fallback} onChange={(e) => patchCompute("fallback", e.target.value)}>
              <Select.Option value="never">never · 排队/失败</Select.Option>
              <Select.Option value="ask">ask · 询问</Select.Option>
            </Select>
          </FormControl>

          <FormControl>
            <FormControl.Label>每实验 GPU 数</FormControl.Label>
            <TextInput
              block
              type="number"
              min={1}
              value={String(form.compute.gpus_per_experiment)}
              onChange={(e) => patchCompute("gpus_per_experiment", Number(e.target.value))}
            />
          </FormControl>

          <FormControl>
            <FormControl.Label>排队超时（秒）</FormControl.Label>
            <TextInput
              block
              type="number"
              min={0}
              value={form.compute.queue_timeout_s == null ? "" : String(form.compute.queue_timeout_s)}
              onChange={(e) => {
                const value = e.target.value;
                patchCompute("queue_timeout_s", value === "" ? null : Number(value));
              }}
            />
          </FormControl>

          <FormControl className={styles.full}>
            <FormControl.Label>数据集根目录</FormControl.Label>
            <TextInput
              block
              placeholder="留空表示不设置"
              value={form.data_root ?? ""}
              onChange={(e) =>
                setForm((prev) => ({
                  ...prev,
                  data_root: e.target.value.trim() || null,
                }))
              }
            />
          </FormControl>
        </div>
        {form.compute.hosts.length > 0 && (
          <div className={styles.grid}>
            {form.compute.hosts.map((host) => (
              <ReadOnlyField
                key={host.name}
                label={host.name}
                value={`${host.ssh} · ${host.gpus === "auto" ? "auto" : host.gpus.join(",")} · ${host.max_leases} leases`}
              />
            ))}
          </div>
        )}
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">开关</Heading>
        <div className={styles.switches}>
          <FormControl disabled={form.skip_validate}>
            <Checkbox
              checked={form.auto_validate}
              onChange={(e) => patchField("auto_validate", e.target.checked)}
            />
            <FormControl.Label>自动验证</FormControl.Label>
          </FormControl>

          <FormControl id="skip-validate">
            <Checkbox
              checked={form.skip_validate}
              onChange={(e) => patchField("skip_validate", e.target.checked)}
            />
            <FormControl.Label>跳过 VALIDATE</FormControl.Label>
            <FormControl.Caption>
              SEARCH 完成后将直接生成 Final，不会运行独立最终评估，也不会产生最终测试或泛化指标。
            </FormControl.Caption>
          </FormControl>

          <FormControl>
            <Checkbox
              checked={form.manual_mode}
              onChange={(e) => patchField("manual_mode", e.target.checked)}
            />
            <FormControl.Label>手动选假设</FormControl.Label>
          </FormControl>
        </div>
      </section>

      <section className={styles.section}>
        <Heading as="h3" variant="small">只读状态</Heading>
        <div className={styles.grid}>
          <ReadOnlyField label="模型" value={settings.model ?? "—"} />
          <ReadOnlyField label="阶段" value={settings.phase} />
          <ReadOnlyField label="状态" value={settings.status} />
        </div>
      </section>
    </Dialog>
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <FormControl>
      <FormControl.Label>{label}</FormControl.Label>
      <TextInput block readOnly value={value} />
    </FormControl>
  );
}

export default SettingsPanel;
```

把 `athena-gui/src/components/SettingsPanel.module.css` 整体替换为：

```css
/* 运行设置弹窗内部布局；弹窗外壳、页脚与滚动由 Primer Dialog 负责。 */

.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--base-size-8);
}

.headerText {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
  min-width: 0;
}

.flash {
  margin-bottom: var(--base-size-12);
}

.section {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
  padding-bottom: var(--base-size-16);
}

.section + .section {
  padding-top: var(--base-size-16);
  border-top: var(--borderWidth-thin) solid var(--borderColor-muted);
}

.projectRow {
  display: flex;
  gap: var(--base-size-8);
}

.grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--base-size-12) var(--base-size-16);
}

.full {
  grid-column: 1 / -1;
}

.switches {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-12);
}
```

- [ ] **Step 3: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/__tests__/SettingsPanel.test.tsx src/components/__tests__/app-shell.test.tsx`
Expected: `SettingsPanel` 6 passed，`app-shell` 12 passed（Task 3 新增的「打开并关闭设置」测试点击「关闭设置」，现在由自定义页头提供）。

Run: `npm --prefix athena-gui test` → 215 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 4: 提交**

```bash
git add athena-gui/src/components/SettingsPanel.tsx athena-gui/src/components/SettingsPanel.module.css athena-gui/src/components/__tests__/SettingsPanel.test.tsx
git commit -m "feat(gui): 设置面板改为 Primer Dialog 与表单控件"
```

---

### Task 7: 人工确认弹窗改为 Primer Dialog + ActionList

**Files:**
- Modify: `athena-gui/src/components/shell/HumanRequestDialog.tsx`、`HumanRequestDialog.module.css`
- Modify: `athena-gui/src/components/shell/__tests__/HumanRequestDialog.test.tsx`

**Interfaces:**
- Produces: `HumanRequestDialog({ request, onReply, settling?, error? })` 不变。行为保持：点击选项立即提交该选项；Esc 仅在 `allow_skip` 不为 false 时等同 Skip；连续点击只提交一次；切换请求时清空输入；关闭后焦点回到打开前的元素。

**实现要点（实测得出）：**
- 选项列表：`ActionList role="listbox" selectionVariant="single"`，每项 `role="option"`。方向键由 Primer 的焦点区管理；禁用时为 `aria-disabled="true"`（不是原生 disabled），点击不会触发 `onSelect`。
- Primer Dialog 不会自动把焦点移进弹窗，因此显式传 `initialFocusRef`：有选项时取第一个 `[role="option"]`（经选项容器的回调 ref 获取），否则取输入框，再否则取 Skip。
- 用 `renderHeader` 只渲染标题，不渲染关闭按钮：这个弹窗只能通过回答关闭。
- `key={request.request_id}`：新请求到来时重新挂载 Dialog，焦点重新落到新请求的第一个选项。
- 删除旧测试「traps Tab focus within the dialog and cycles」：焦点陷阱改由 Primer 实现（基于 focusin 与哨兵元素），jsdom 无法用 `fireEvent.keyDown(Tab)` 模拟真实的 Tab 移动；Tab 循环改在 Task 12 实机核对。新增：无选项时聚焦输入框、无关闭按钮、传输错误不关闭弹窗、提交失败显示错误。

- [ ] **Step 1: 改写测试**

把 `athena-gui/src/components/shell/__tests__/HumanRequestDialog.test.tsx` 整体替换为：

```tsx
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HumanReply, HumanRequest } from "../../../types/ui";
import { HumanRequestDialog } from "../HumanRequestDialog";

const requestOne: HumanRequest = {
  request_id: "req-1",
  session_id: "s-1",
  scope_id: "draft-1",
  scope_kind: "clarification",
  prompt: "Choose metric",
  choices: [
    { label: "F1", value: "f1" },
    { label: "AUC", value: "auc" },
  ],
  allow_custom: true,
  allow_skip: true,
  created_at: "2026-09-01T10:00:00Z",
  expires_at: "2026-09-01T10:02:00Z",
};

describe("HumanRequestDialog", () => {
  it("focuses the first option on open and restores focus when the request closes", async () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();

    const view = render(<HumanRequestDialog request={requestOne} onReply={reply} />);
    await waitFor(() => expect(screen.getByRole("option", { name: "F1" })).toHaveFocus());

    view.rerender(<HumanRequestDialog request={null} onReply={reply} />);
    await waitFor(() => expect(trigger).toHaveFocus());
    trigger.remove();
  });

  it("focuses the text box when a request has no choices", async () => {
    render(<HumanRequestDialog request={{ ...requestOne, choices: [] }} onReply={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Free text answer" })).toHaveFocus());
  });

  it("resets the typed answer when the request changes", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    const view = render(<HumanRequestDialog request={requestOne} onReply={reply} />);
    const textbox = screen.getByRole("textbox", { name: "Free text answer" });
    fireEvent.change(textbox, { target: { value: "macro F1" } });
    expect(textbox).toHaveValue("macro F1");

    view.rerender(<HumanRequestDialog request={{ ...requestOne, request_id: "req-2" }} onReply={reply} />);
    expect(screen.getByRole("textbox", { name: "Free text answer" })).toHaveValue("");
  });

  it("has an accessible dialog name and no dismiss-only close button", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "需要你的确认" });
    // 弹窗只能通过回答关闭：除 Reply / Skip 外没有其他按钮（包括 Primer 默认的关闭按钮）。
    expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(["Reply", "Skip"]);
  });

  it("skips on Escape when skipping is allowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(reply).toHaveBeenCalledWith({ kind: "skip" });
  });

  it("does not skip on Escape when skipping is disallowed", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={{ ...requestOne, allow_skip: false }} onReply={reply} />);

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(reply).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });

  it("moves between options with the arrow keys", async () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} />);

    const first = screen.getByRole("option", { name: "F1" });
    const second = screen.getByRole("option", { name: "AUC" });
    await waitFor(() => expect(first).toHaveFocus());
    fireEvent.keyDown(first, { key: "ArrowDown" });
    expect(second).toHaveFocus();
    fireEvent.keyDown(second, { key: "ArrowUp" });
    expect(first).toHaveFocus();
  });

  it("makes every action unavailable while settling", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} settling />);
    expect(screen.getByRole("option", { name: "F1" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("option", { name: "AUC" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("textbox", { name: "Free text answer" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reply" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Skip" })).toBeDisabled();
  });

  it("submits exactly one reply per request even on rapid double clicks", () => {
    let resolveReply: (() => void) | undefined;
    const reply = vi.fn(
      (_reply: HumanReply) =>
        new Promise<void>((resolve) => {
          resolveReply = () => resolve();
        }),
    );
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    const choice = screen.getByRole("option", { name: "F1" });
    fireEvent.click(choice);
    fireEvent.click(choice);
    expect(reply).toHaveBeenCalledTimes(1);
    expect(reply).toHaveBeenCalledWith({ kind: "choice", value: "f1" });
    resolveReply?.();
  });

  it("submits a typed text reply", () => {
    const reply = vi.fn(async (_reply: HumanReply) => undefined);
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.change(screen.getByRole("textbox", { name: "Free text answer" }), { target: { value: "macro F1" } });
    fireEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(reply).toHaveBeenCalledWith({ kind: "text", text: "macro F1" });
  });

  it("shows a transport error without closing the dialog", () => {
    render(<HumanRequestDialog request={requestOne} onReply={vi.fn()} error="gateway disconnected" />);
    expect(screen.getByRole("alert")).toHaveTextContent("gateway disconnected");
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });

  it("keeps the dialog open and shows the failure when submitting a reply fails", async () => {
    const reply = vi.fn(async (_reply: HumanReply) => {
      throw new Error("reply rejected");
    });
    render(<HumanRequestDialog request={requestOne} onReply={reply} />);

    fireEvent.click(screen.getByRole("option", { name: "F1" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("reply rejected");
    expect(screen.getByRole("dialog", { name: "需要你的确认" })).toBeInTheDocument();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/shell/__tests__/HumanRequestDialog.test.tsx`
Expected: FAIL——旧实现的选项是 `button` 而非 `option`，且弹窗打开时不会把焦点移到第一个选项。

- [ ] **Step 2: 改写弹窗**

把 `athena-gui/src/components/shell/HumanRequestDialog.tsx` 整体替换为：

```tsx
import { useEffect, useRef, useState } from "react";
import { ActionList, Button, Dialog, Flash, TextInput } from "@primer/react";
import type { HumanChoice, HumanReply, HumanRequest } from "../../types/ui";
import styles from "./HumanRequestDialog.module.css";

interface HumanRequestDialogProps {
  request: HumanRequest | null;
  onReply(reply: HumanReply): Promise<void> | void;
  /** True while one reply is being submitted. */
  settling?: boolean;
  /** A transport/contract error to surface without dismissing the dialog. */
  error?: string | null;
}

/** Modal asking the human to answer the supervisor's outstanding question. */
export function HumanRequestDialog({
  request,
  onReply,
  settling = false,
  error = null,
}: HumanRequestDialogProps) {
  const [answer, setAnswer] = useState("");
  const [errorVisible, setErrorVisible] = useState<string | null>(null);
  const submittingRef = useRef(false);
  const firstOptionRef = useRef<HTMLElement | null>(null);
  const textRef = useRef<HTMLInputElement>(null);
  const skipRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setAnswer("");
    setErrorVisible(null);
    submittingRef.current = false;
  }, [request?.request_id]);

  const submitReply = async (reply: HumanReply) => {
    if (!request || settling || submittingRef.current) return;
    if (reply.kind === "text" && !answer.trim()) return;
    submittingRef.current = true;
    try {
      await onReply(reply);
      setErrorVisible(null);
    } catch (err) {
      setErrorVisible(err instanceof Error ? err.message : String(err));
    } finally {
      submittingRef.current = false;
      if (reply.kind !== "text") setAnswer("");
    }
  };

  if (!request) return null;

  const choices = request.choices ?? [];
  const showText = request.allow_custom !== false;
  const showSkip = request.allow_skip !== false;
  const allDisabled = settling || submittingRef.current;
  const visibleError = error ?? errorVisible;
  // Primer Dialog 不会自动把焦点移进弹窗：优先聚焦第一个选项，其次输入框，最后 Skip。
  const initialFocusRef = choices.length > 0 ? firstOptionRef : showText ? textRef : skipRef;

  const submitText = () => {
    if (!answer.trim()) return;
    void submitReply({ kind: "text", text: answer.trim() });
  };

  const choose = (choice: HumanChoice) => {
    void submitReply({ kind: "choice", value: choice.value });
  };

  const skip = () => {
    void submitReply({ kind: "skip" });
  };

  // 弹窗只能通过回答关闭（不渲染关闭按钮）；Esc 仅在允许跳过时等同 Skip。
  const handleClose = (gesture: "close-button" | "escape") => {
    if (gesture === "escape" && showSkip && !allDisabled) skip();
  };

  return (
    <Dialog
      key={request.request_id}
      width="large"
      onClose={handleClose}
      initialFocusRef={initialFocusRef}
      renderHeader={({ dialogLabelId }) => (
        <Dialog.Header>
          <Dialog.Title id={dialogLabelId}>需要你的确认</Dialog.Title>
        </Dialog.Header>
      )}
    >
      <p className={styles.prompt}>{request.prompt}</p>

      {choices.length > 0 && (
        <div
          className={styles.choices}
          ref={(node) => {
            firstOptionRef.current = node?.querySelector<HTMLElement>('[role="option"]') ?? null;
          }}
        >
          <ActionList role="listbox" aria-label="Options" selectionVariant="single">
            {choices.map((choice, index) => (
              <ActionList.Item
                key={choice.value}
                role="option"
                selected={false}
                disabled={allDisabled}
                onSelect={() => choose(choice)}
              >
                <ActionList.LeadingVisual>
                  <span aria-hidden="true">{index + 1}</span>
                </ActionList.LeadingVisual>
                {choice.label}
              </ActionList.Item>
            ))}
          </ActionList>
        </div>
      )}

      <div className={styles.actions}>
        {showText && (
          <>
            <TextInput
              ref={textRef}
              className={styles.input}
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submitText();
              }}
              placeholder="Other answer (optional)"
              disabled={allDisabled}
              aria-label="Free text answer"
            />
            <Button onClick={submitText} disabled={allDisabled || !answer.trim()}>
              Reply
            </Button>
          </>
        )}
        {showSkip && (
          <Button ref={skipRef} variant="invisible" onClick={skip} disabled={allDisabled}>
            Skip
          </Button>
        )}
      </div>

      {visibleError && (
        <Flash variant="danger" className={styles.error}>
          <span role="alert">{visibleError}</span>
        </Flash>
      )}
    </Dialog>
  );
}
```

把 `athena-gui/src/components/shell/HumanRequestDialog.module.css` 整体替换为：

```css
/* 人工确认弹窗内部布局；弹窗外壳与焦点管理由 Primer Dialog 负责。 */

.prompt {
  margin: 0 0 var(--base-size-12);
  font-size: var(--text-body-size-medium);
  line-height: 1.5;
  white-space: pre-wrap;
  color: var(--fgColor-default);
}

.choices {
  margin-bottom: var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--base-size-8);
}

.input {
  flex: 1;
  min-width: 180px;
}

.error {
  margin-top: var(--base-size-12);
}
```

- [ ] **Step 3: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/shell/__tests__/HumanRequestDialog.test.tsx`
Expected: 12 passed。

Run: `npm --prefix athena-gui test` → 218 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 4: 提交**

```bash
git add athena-gui/src/components/shell/HumanRequestDialog.tsx athena-gui/src/components/shell/HumanRequestDialog.module.css athena-gui/src/components/shell/__tests__/HumanRequestDialog.test.tsx
git commit -m "feat(gui): 人工确认弹窗改为 Primer Dialog 与选项列表"
```

---

### Task 8: 实验类面板（实验日志、实验管理、文件、差异）

**Files:**
- Create: `athena-gui/src/components/__tests__/experiment-panels.test.tsx`
- Modify: `athena-gui/src/components/ExperimentLog.tsx`；Create: `ExperimentLog.module.css`
- Modify: `athena-gui/src/components/ExperimentManager.tsx`；Create: `ExperimentManager.module.css`
- Modify: `athena-gui/src/components/FileTree.tsx`
- Modify: `athena-gui/src/components/DiffViewer.tsx`；Create: `DiffViewer.module.css`

**Interfaces:**
- Consumes: Task 2 的 `Panel`、`Section`、`KeyValueList`、`StatusBadge`、`EmptyState`；Task 1 的 `Icon`、`renderUi`。
- Produces: 四个面板仍为默认导出、无 props（`components/context/panels.ts` 的懒加载注册不变）。实验管理的状态下拉框可访问名「推进状态」，失败原因输入框可访问名「失败原因」。

**与 spec 的差异：** 「文件」面板展示的是最佳实验的工作区信息与产物列表（键值），后端不提供目录层级，因此不用 `TreeView`；表格用原生 `<table>` + Primer 变量（`DataTable` 仅在 experimental）。

- [ ] **Step 1: 写失败测试**

Create `athena-gui/src/components/__tests__/experiment-panels.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderUi } from "../../test/render";
import type { ExperimentDetail } from "../../lib/tauri-bridge";
import ExperimentLog from "../ExperimentLog";
import ExperimentManager from "../ExperimentManager";
import FileTree from "../FileTree";
import DiffViewer from "../DiffViewer";

const bridgeMocks = vi.hoisted(() => ({
  experimentsList: vi.fn(),
  experimentGet: vi.fn(),
  experimentTransition: vi.fn(),
  experimentSetSota: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  ...bridgeMocks,
}));

function experiment(id: string, overrides: Partial<ExperimentDetail["experiment"]> = {}, sota = false): ExperimentDetail {
  return {
    experiment: {
      id,
      parent_id: null,
      hypothesis_id: `h-${id}`,
      commit: "abc1234",
      plan: { kind: "search", change: "add window features", rubrics: ["macro F1"], run_config_ref: "", budget: {}, acceptance_rule: "beats SOTA" },
      gitwork: { path: `/work/${id}`, branch: `exp/${id}`, base_commit: "base000" },
      status: "SUCCEEDED",
      eval: { experiment_id: id, primary: 0.7412, secondary: {}, per_sample: "" },
      verdict: { winner: "candidate", p_value: 0.03 },
      artifacts: { model: `/work/${id}/model.pkl` },
      error: null,
      ...overrides,
    },
    hypothesis: {
      id: `h-${id}`,
      statement: `statement for ${id}`,
      intervention: "rolling window",
      expected_effect: "higher F1",
      status: "SUPPORTED",
      evidence_refs: [],
      parent_id: null,
      supersedes: [],
      priority: 1,
      order: null,
      patience: 1,
      turn_limit: null,
      sources: [],
    },
    sota,
  };
}

beforeEach(() => {
  for (const mock of Object.values(bridgeMocks)) mock.mockReset();
});

describe("ExperimentLog", () => {
  it("lists experiments on a timeline with lifecycle labels", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1"), experiment("exp-2", { status: "FAILED" })] });

    renderUi(<ExperimentLog />);

    expect(await screen.findByRole("heading", { level: 3, name: "实验日志 (2)" })).toBeInTheDocument();
    expect(screen.getByText("statement for exp-1")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toHaveAttribute("data-variant", "danger");
  });

  it("keeps the rendered timeline and shows the error when a refresh fails", async () => {
    bridgeMocks.experimentsList
      .mockResolvedValueOnce({ experiments: [experiment("exp-1")] })
      .mockRejectedValueOnce(new Error("gateway timeout"));

    renderUi(<ExperimentLog />);
    await screen.findByText("statement for exp-1");

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("gateway timeout")).toBeInTheDocument();
    expect(screen.getByText("statement for exp-1")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled());
  });
});

describe("ExperimentManager", () => {
  it("opens the detail for a clicked row", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1")] });
    bridgeMocks.experimentGet.mockResolvedValue(experiment("exp-1"));

    renderUi(<ExperimentManager />);
    fireEvent.click(await screen.findByText("statement for exp-1"));

    expect(await screen.findByText("实验详情")).toBeInTheDocument();
    expect(bridgeMocks.experimentGet).toHaveBeenCalledWith("exp-1");
    expect(screen.getByText("干预: rolling window · 预期: higher F1")).toBeInTheDocument();
  });

  it("refuses a FAILED transition without an error explanation", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1", { status: "RUNNING" })] });
    bridgeMocks.experimentGet.mockResolvedValue(experiment("exp-1", { status: "RUNNING" }));

    renderUi(<ExperimentManager />);
    fireEvent.click(await screen.findByText("statement for exp-1"));
    fireEvent.change(await screen.findByRole("combobox", { name: "推进状态" }), { target: { value: "FAILED" } });
    fireEvent.click(screen.getByRole("button", { name: "确认推进" }));

    expect(await screen.findByText("FAILED 状态需要填写 error 说明。")).toBeInTheDocument();
    expect(bridgeMocks.experimentTransition).not.toHaveBeenCalled();
  });
});

describe("FileTree and DiffViewer", () => {
  it("show the SOTA experiment's workspace, artifacts and change spec", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [experiment("exp-1"), experiment("exp-2", {}, true)] });

    renderUi(
      <>
        <FileTree />
        <DiffViewer />
      </>,
    );

    const workspaces = await screen.findAllByRole("heading", { name: "工作区" });
    expect(workspaces).toHaveLength(2);
    expect(screen.getAllByText("/work/exp-2")).toHaveLength(2);
    expect(screen.getByText("/work/exp-2/model.pkl")).toBeInTheDocument();
    expect(screen.getByText("exp-2 ★ SOTA")).toBeInTheDocument();
    expect(within(screen.getByRole("list")).getByText("macro F1")).toBeInTheDocument();
  });

  it("show empty states when there are no experiments", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [] });

    renderUi(
      <>
        <FileTree />
        <DiffViewer />
      </>,
    );

    expect(await screen.findByText("暂无实验。运行搜索后此处展示最佳实验的工作区与产物。")).toBeInTheDocument();
    expect(screen.getByText("暂无实验。运行搜索后此处展示最佳实验的变更规格。")).toBeInTheDocument();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/__tests__/experiment-panels.test.tsx`
Expected: FAIL——旧实现的状态徽章没有 `data-variant`、状态下拉框没有可访问名「推进状态」、分组标题不是 heading。

- [ ] **Step 2: 改写四个面板**

把 `athena-gui/src/components/ExperimentLog.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash, Timeline } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { Icon } from "./common/Icon";
import { Panel } from "./common/Panel";
import { StatusBadge } from "./common/StatusBadge";
import styles from "./ExperimentLog.module.css";

function primaryOf(d: ExperimentDetail): string {
  return d.experiment.eval?.primary != null ? d.experiment.eval.primary.toFixed(4) : "—";
}

function winnerOf(d: ExperimentDetail): string {
  return d.experiment.verdict?.winner ?? "—";
}

/** Compact, ordered timeline of experiment lifecycle events. */
export default function ExperimentLog() {
  const [experiments, setExperiments] = useState<ExperimentDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await experimentsList();
      setExperiments(res.experiments);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Panel
      title={`实验日志 (${experiments.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {experiments.length === 0 ? (
        <EmptyState icon="log" message="暂无实验记录。启动搜索后将在此处按序展示实验生命周期。" />
      ) : (
        <Timeline>
          {experiments.map((d) => (
            <Timeline.Item key={d.experiment.id} condensed>
              <Timeline.Badge>
                <Icon name="experiments" size={14} />
              </Timeline.Badge>
              <Timeline.Body>
                <div className={styles.head}>
                  <span className={styles.id}>
                    {d.experiment.id}
                    {d.sota ? " ★" : ""}
                  </span>
                  <StatusBadge status={d.experiment.status} />
                  <span>primary {primaryOf(d)}</span>
                  <span>winner {winnerOf(d)}</span>
                </div>
                <div className={styles.statement}>{d.hypothesis.statement}</div>
              </Timeline.Body>
            </Timeline.Item>
          ))}
        </Timeline>
      )}
    </Panel>
  );
}
```

Create `athena-gui/src/components/ExperimentLog.module.css`:

```css
/* 实验日志时间线条目。 */

.head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--base-size-8);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.id {
  font-family: var(--fontStack-monospace);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-default);
}

.statement {
  margin-top: var(--base-size-4);
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-default);
}
```

把 `athena-gui/src/components/ExperimentManager.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash, Select, TextInput } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  experimentGet,
  experimentSetSota,
  experimentTransition,
  experimentsList,
  type ExperimentDetail,
} from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { KeyValueList } from "./common/KeyValueList";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";
import { StatusBadge } from "./common/StatusBadge";
import styles from "./ExperimentManager.module.css";

/** Allowed experiment status transitions (see ``research_tree._ALLOWED_TRANSITIONS``). */
const ALLOWED_TRANSITIONS: Record<string, string[]> = {
  PENDING: ["RUNNING", "CANCELLED"],
  RUNNING: ["SUCCEEDED", "FAILED", "CANCELLED"],
  SUCCEEDED: [],
  FAILED: [],
  CANCELLED: [],
};

function primaryOf(detail: ExperimentDetail): string {
  return detail.experiment.eval?.primary != null
    ? detail.experiment.eval.primary.toFixed(4)
    : "--";
}

function winnerOf(detail: ExperimentDetail): string {
  return detail.experiment.verdict?.winner ?? "--";
}

/** Experiment table + detail drawer + status transition / SOTA actions. */
export default function ExperimentManager() {
  const [experiments, setExperiments] = useState<ExperimentDetail[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [targetStatus, setTargetStatus] = useState<string>("");
  const [transitionError, setTransitionError] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await experimentsList();
      setExperiments(res.experiments);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadDetail = useCallback(async (id: string) => {
    setLoading(true);
    setTargetStatus("");
    setTransitionError("");
    try {
      const res = await experimentGet(id);
      setDetail(res);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, []);

  function selectRow(id: string) {
    setSelectedId(id);
    void loadDetail(id);
  }

  async function applyTransition() {
    if (!detail || !targetStatus) return;
    if (targetStatus === "FAILED" && !transitionError.trim()) {
      setError("FAILED 状态需要填写 error 说明。");
      return;
    }
    setBusy(true);
    try {
      await experimentTransition(
        detail.experiment.id,
        targetStatus,
        targetStatus === "FAILED" ? transitionError.trim() : undefined,
      );
      await refresh();
      await loadDetail(detail.experiment.id);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function markSota() {
    if (!detail) return;
    setBusy(true);
    try {
      await experimentSetSota(detail.experiment.id);
      await refresh();
      await loadDetail(detail.experiment.id);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const allowedTargets = detail ? ALLOWED_TRANSITIONS[detail.experiment.status] ?? [] : [];
  const canSetSota =
    !!detail &&
    detail.experiment.status === "SUCCEEDED" &&
    ["baseline", "search"].includes(detail.experiment.plan.kind) &&
    !detail.sota;

  function closeDetail() {
    setSelectedId(null);
    setDetail(null);
    setTargetStatus("");
    setTransitionError("");
  }

  return (
    <Panel
      title={`实验管理 (${experiments.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={refreshing}>
          {refreshing ? "刷新中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {experiments.length === 0 ? (
        <EmptyState icon="experiments" message="暂无实验。启动搜索后将在此处管理实验状态。" />
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>实验</th>
                <th>状态</th>
                <th>假设</th>
                <th>primary</th>
                <th>winner</th>
              </tr>
            </thead>
            <tbody>
              {experiments.map((exp) => (
                <tr
                  key={exp.experiment.id}
                  onClick={() => selectRow(exp.experiment.id)}
                  className={exp.experiment.id === selectedId ? styles.selected : undefined}
                  aria-selected={exp.experiment.id === selectedId}
                >
                  <td className={styles.mono}>
                    {exp.experiment.id}
                    {exp.sota ? " ★" : ""}
                  </td>
                  <td>
                    <StatusBadge status={exp.experiment.status} />
                  </td>
                  <td className={styles.statementCell}>
                    <span className={styles.truncate}>{exp.hypothesis.statement}</span>
                  </td>
                  <td className={styles.mono}>{primaryOf(exp)}</td>
                  <td>{winnerOf(exp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId && (
        <Section
          title={
            <span className={styles.detailTitle}>
              实验详情
              <Button size="small" variant="invisible" onClick={closeDetail}>
                关闭
              </Button>
            </span>
          }
        >
          {loading && <p className={styles.muted}>加载详情中…</p>}

          {detail && (
            <>
              <KeyValueList
                columns={2}
                items={[
                  { label: "实验 ID", value: detail.experiment.id, mono: true },
                  { label: "状态", value: detail.experiment.status },
                  { label: "父实验", value: detail.experiment.parent_id ?? "—", mono: true },
                  { label: "假设 ID", value: detail.experiment.hypothesis_id, mono: true },
                  { label: "plan.kind", value: detail.experiment.plan.kind },
                  { label: "commit", value: detail.experiment.commit, mono: true },
                  { label: "SOTA", value: detail.sota ? "是" : "否" },
                  { label: "error", value: detail.experiment.error ?? "—" },
                  ...(detail.experiment.eval
                    ? [{ label: "eval.primary", value: detail.experiment.eval.primary.toFixed(4), mono: true }]
                    : []),
                  ...(detail.experiment.verdict
                    ? [
                        {
                          label: "verdict",
                          value: `${detail.experiment.verdict.winner} (p=${detail.experiment.verdict.p_value})`,
                        },
                      ]
                    : []),
                ]}
              />

              <div className={styles.hypothesis}>
                <div className={styles.subheading}>假设陈述</div>
                <p className={styles.statement}>{detail.hypothesis.statement}</p>
                <p className={styles.muted}>
                  干预: {detail.hypothesis.intervention} · 预期: {detail.hypothesis.expected_effect}
                </p>
              </div>

              {detail.path && detail.path.length > 0 && (
                <p className={styles.muted}>祖先链: {detail.path.join(" → ")}</p>
              )}
              {detail.descendants && detail.descendants.length > 0 && (
                <p className={styles.muted}>后代: {detail.descendants.join(", ")}</p>
              )}

              <div className={styles.actions}>
                <Select
                  aria-label="推进状态"
                  value={targetStatus}
                  onChange={(e) => setTargetStatus(e.target.value)}
                >
                  <Select.Option value="">推进状态…</Select.Option>
                  {allowedTargets.map((target) => (
                    <Select.Option key={target} value={target}>
                      {target}
                    </Select.Option>
                  ))}
                </Select>

                {targetStatus === "FAILED" && (
                  <TextInput
                    aria-label="失败原因"
                    value={transitionError}
                    onChange={(e) => setTransitionError(e.target.value)}
                    placeholder="error 说明（必填）"
                  />
                )}

                <Button
                  size="small"
                  onClick={() => void applyTransition()}
                  disabled={busy || !targetStatus || allowedTargets.length === 0}
                >
                  确认推进
                </Button>

                <Button size="small" variant="primary" onClick={() => void markSota()} disabled={busy || !canSetSota}>
                  {detail.sota ? "当前 SOTA" : "设为 SOTA"}
                </Button>
              </div>
            </>
          )}
        </Section>
      )}
    </Panel>
  );
}
```

Create `athena-gui/src/components/ExperimentManager.module.css`:

```css
/* 实验管理：GitHub 风格表格 + 详情分组 + 状态推进操作。 */

.tableWrap {
  overflow-x: auto;
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-body-size-medium);
}

.table th {
  padding: var(--base-size-8) var(--base-size-12);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-default);
  background: var(--bgColor-muted);
  text-align: left;
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.table td {
  padding: var(--base-size-8) var(--base-size-12);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-muted);
  vertical-align: middle;
}

.table tbody tr {
  cursor: pointer;
}

.table tbody tr:last-child td {
  border-bottom: 0;
}

.table tbody tr:hover {
  background: var(--bgColor-muted);
}

.selected,
.table tbody tr.selected:hover {
  background: var(--bgColor-accent-muted);
}

.mono {
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
}

.statementCell {
  max-width: 320px;
}

.truncate {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detailTitle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
}

.hypothesis {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
}

.subheading {
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.statement {
  margin: 0;
  font-size: var(--text-body-size-medium);
}

.muted {
  margin: 0;
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--base-size-8);
  padding-top: var(--base-size-12);
  border-top: var(--borderWidth-thin) solid var(--borderColor-muted);
}
```

把 `athena-gui/src/components/FileTree.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { KeyValueList } from "./common/KeyValueList";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";

/** Best experiment's workspace and produced artifacts. */
export default function FileTree() {
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await experimentsList();
      const sota = res.experiments.find((d) => d.sota);
      const target = sota ?? res.experiments[res.experiments.length - 1] ?? null;
      setDetail(target);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const artifacts = detail ? Object.entries(detail.experiment.artifacts) : [];

  return (
    <Panel
      title="工作区与产物"
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!detail ? (
        <EmptyState icon="files" message="暂无实验。运行搜索后此处展示最佳实验的工作区与产物。" />
      ) : (
        <>
          <Section title="工作区">
            <KeyValueList
              items={[
                { label: "路径", value: detail.experiment.gitwork.path, mono: true },
                { label: "分支", value: detail.experiment.gitwork.branch, mono: true },
                { label: "基线提交", value: detail.experiment.gitwork.base_commit, mono: true },
              ]}
            />
          </Section>

          <Section title={`产物 (${artifacts.length})`}>
            {artifacts.length === 0 ? (
              <EmptyState icon="files" message="暂无产物引用。" />
            ) : (
              <KeyValueList items={artifacts.map(([kind, ref]) => ({ label: kind, value: ref, mono: true }))} />
            )}
          </Section>
        </>
      )}
    </Panel>
  );
}
```

把 `athena-gui/src/components/DiffViewer.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { KeyValueList } from "./common/KeyValueList";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";
import styles from "./DiffViewer.module.css";

/** Best experiment's change spec: what changed, acceptance rule, and rubrics. */
export default function DiffViewer() {
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await experimentsList();
      const sota = res.experiments.find((d) => d.sota);
      const target = sota ?? res.experiments[res.experiments.length - 1] ?? null;
      setDetail(target);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const plan = detail?.experiment.plan;

  return (
    <Panel
      title="变更详情"
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!detail || !plan ? (
        <EmptyState icon="diff" message="暂无实验。运行搜索后此处展示最佳实验的变更规格。" />
      ) : (
        <>
          <Section title="实验">
            <p className={styles.mono}>
              {detail.experiment.id}
              {detail.sota ? " ★ SOTA" : ""}
            </p>
            <h4 className={styles.subheading}>变更内容</h4>
            <p className={styles.text}>{plan.change || "—"}</p>
            <h4 className={styles.subheading}>验收标准</h4>
            <p className={styles.text}>{plan.acceptance_rule || "—"}</p>
            {plan.rubrics.length > 0 && (
              <>
                <h4 className={styles.subheading}>评价指标</h4>
                <ul className={styles.rubrics}>
                  {plan.rubrics.map((rubric) => (
                    <li key={rubric}>{rubric}</li>
                  ))}
                </ul>
              </>
            )}
          </Section>

          <Section title="工作区">
            <KeyValueList
              items={[
                { label: "路径", value: detail.experiment.gitwork.path, mono: true },
                { label: "分支", value: detail.experiment.gitwork.branch, mono: true },
                { label: "基线提交", value: detail.experiment.gitwork.base_commit, mono: true },
              ]}
            />
          </Section>
        </>
      )}
    </Panel>
  );
}
```

Create `athena-gui/src/components/DiffViewer.module.css`:

```css
/* 变更详情：实验 ID、变更内容、验收标准与评价指标。 */

.mono {
  margin: 0;
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-default);
}

.subheading {
  margin: 0;
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.text {
  margin: 0;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-default);
  white-space: pre-wrap;
  word-break: break-word;
}

.rubrics {
  margin: 0;
  padding-left: var(--base-size-20);
  font-size: var(--text-body-size-medium);
}
```

- [ ] **Step 3: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/__tests__/experiment-panels.test.tsx`
Expected: 6 passed。

Run: `npm --prefix athena-gui test` → 224 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 4: 提交**

```bash
git add athena-gui/src/components/__tests__/experiment-panels.test.tsx athena-gui/src/components/ExperimentLog.tsx athena-gui/src/components/ExperimentLog.module.css athena-gui/src/components/ExperimentManager.tsx athena-gui/src/components/ExperimentManager.module.css athena-gui/src/components/FileTree.tsx athena-gui/src/components/DiffViewer.tsx athena-gui/src/components/DiffViewer.module.css
git commit -m "feat(gui): 实验类面板改用 Primer 组件"
```

---

### Task 9: 洞察类面板（模型轨迹、报告、图算法、数据分析）

**Files:**
- Create: `athena-gui/src/components/__tests__/insight-panels.test.tsx`
- Modify: `athena-gui/src/components/LLMIOPanel.tsx`、`LLMIOPanel.module.css`
- Modify: `athena-gui/src/components/ReportPanel.tsx`、`ReportPanel.module.css`
- Modify: `athena-gui/src/components/AlgorithmsPanel.tsx`；Create: `AlgorithmsPanel.module.css`
- Modify: `athena-gui/src/components/EDAPreview.tsx`、`EDAPreview.module.css`

**Interfaces:**
- Consumes: Task 2 的 `Panel`、`Section`、`CodeBlock`、`EmptyState`。
- Produces: 四个面板仍为默认导出、无 props。模型轨迹左侧用 `ActionList`（当前 agent 为 `active`）；消息角色用 `Label`（system→secondary、user→accent、assistant→success、tool→attention）。图算法的参数输入框通过 `FormControl.Label` 获得可访问名（参数名）。EDA 报告的 Markdown 采用 GitHub 渲染 README 的排版。

- [ ] **Step 1: 写失败测试**

Create `athena-gui/src/components/__tests__/insight-panels.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderUi } from "../../test/render";
import LLMIOPanel from "../LLMIOPanel";
import ReportPanel from "../ReportPanel";
import AlgorithmsPanel from "../AlgorithmsPanel";
import EDAPreview from "../EDAPreview";

const bridgeMocks = vi.hoisted(() => ({
  tracesList: vi.fn(),
  traceGet: vi.fn(),
  experimentsList: vi.fn(),
  generateReport: vi.fn(),
  graphAlgorithms: vi.fn(),
  graphAlgorithm: vi.fn(),
  edaReport: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  ...bridgeMocks,
}));

beforeEach(() => {
  for (const mock of Object.values(bridgeMocks)) mock.mockReset();
});

describe("LLMIOPanel", () => {
  it("replays the selected agent's messages with role labels", async () => {
    bridgeMocks.tracesList.mockResolvedValue({
      traces: [{ agent_id: "ideator-1", file: "a.jsonl", size_bytes: 10, messages: 2, updated_at: 0 }],
    });
    bridgeMocks.traceGet.mockResolvedValue({
      agent_id: "ideator-1",
      messages: [
        { seq: 1, ts: "", role: "user", kind: "text", content: "propose hypotheses" },
        { seq: 2, ts: "", role: "tool", kind: "tool_return", content: "rows=891" },
      ],
    });

    renderUi(<LLMIOPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /ideator-1/ }));

    expect(await screen.findByText("propose hypotheses")).toBeInTheDocument();
    expect(screen.getByText("user")).toHaveAttribute("data-variant", "accent");
    expect(screen.getByText("tool")).toHaveAttribute("data-variant", "attention");
    expect(screen.getByText("工具返回")).toBeInTheDocument();
  });

  it("shows the error when loading a trace fails", async () => {
    bridgeMocks.tracesList.mockResolvedValue({
      traces: [{ agent_id: "ideator-1", file: "a.jsonl", size_bytes: 10, messages: 2, updated_at: 0 }],
    });
    bridgeMocks.traceGet.mockRejectedValue(new Error("trace file missing"));

    renderUi(<LLMIOPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /ideator-1/ }));

    expect(await screen.findByText("trace file missing")).toBeInTheDocument();
  });
});

describe("ReportPanel", () => {
  it("summarizes experiments and shows a report generation failure", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [] });
    bridgeMocks.generateReport.mockRejectedValue(new Error("report backend unavailable"));

    renderUi(<ReportPanel />);
    expect(await screen.findByText("暂无实验数据。完成实验后此处汇总 SOTA、最佳指标与状态分布。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "生成报告" }));
    expect(await screen.findByText("report backend unavailable")).toBeInTheDocument();
  });
});

describe("AlgorithmsPanel", () => {
  it("runs the selected algorithm with its parameters and shows the JSON result", async () => {
    bridgeMocks.graphAlgorithms.mockResolvedValue({
      algorithms: [{ name: "ancestors", label: "祖先链", params: [{ name: "node_id", type: "str", required: true }] }],
    });
    bridgeMocks.graphAlgorithm.mockResolvedValue({ path: ["exp-0", "exp-1"] });

    renderUi(<AlgorithmsPanel />);
    fireEvent.change(await screen.findByRole("textbox", { name: /node_id/ }), { target: { value: "exp-1" } });
    fireEvent.click(screen.getByRole("button", { name: "运行" }));

    await waitFor(() => expect(bridgeMocks.graphAlgorithm).toHaveBeenCalledWith("ancestors", { node_id: "exp-1" }));
    expect(await screen.findByText(/"exp-1"/)).toBeInTheDocument();
  });

  it("shows the error when running an algorithm fails", async () => {
    bridgeMocks.graphAlgorithms.mockResolvedValue({
      algorithms: [{ name: "ancestors", label: "祖先链", params: [] }],
    });
    bridgeMocks.graphAlgorithm.mockRejectedValue(new Error("node not found"));

    renderUi(<AlgorithmsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "运行" }));

    expect(await screen.findByText("node not found")).toBeInTheDocument();
  });
});

describe("EDAPreview", () => {
  it("renders the Markdown report and figures", async () => {
    bridgeMocks.edaReport.mockResolvedValue({
      eda_dir: "/work/eda",
      report: "# 数据概览\n\n共 891 行",
      figures: [{ name: "hist.png", mime: "image/png", data: "AAAA" }],
    });

    renderUi(<EDAPreview />);

    expect(await screen.findByRole("heading", { level: 1, name: "数据概览" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "hist.png" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "图表 (1)" })).toBeInTheDocument();
  });

  it("shows the empty state before PREPARE has produced an EDA directory", async () => {
    bridgeMocks.edaReport.mockResolvedValue({ eda_dir: null, report: null, figures: [] });

    renderUi(<EDAPreview />);

    expect(await screen.findByText("暂无 EDA 报告。运行 PREPARE 阶段后将在此预览数据探索结果。")).toBeInTheDocument();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/__tests__/insight-panels.test.tsx`
Expected: FAIL——旧实现的角色徽章没有 `data-variant`，图算法参数输入框没有以参数名为可访问名的 label，图表分组标题不是 heading。

- [ ] **Step 2: 改写四个面板**

把 `athena-gui/src/components/LLMIOPanel.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { ActionList, Button, Flash, Label, type LabelColorOptions } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  traceGet,
  tracesList,
  type TraceDetail,
  type TraceMessage,
  type TraceRole,
  type TraceSummary,
} from "../lib/tauri-bridge";
import { CodeBlock } from "./common/CodeBlock";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import styles from "./LLMIOPanel.module.css";

/** 消息角色 → Primer Label 配色。 */
const ROLE_VARIANT: Record<TraceRole, LabelColorOptions> = {
  system: "secondary",
  user: "accent",
  assistant: "success",
  tool: "attention",
};

function formatUpdatedAt(mtime: number): string {
  if (!mtime) return "—";
  return new Date(mtime * 1000).toLocaleString();
}

function TraceBubble({ message }: { message: TraceMessage }) {
  const isToolCall = message.kind === "tool_call";
  const isToolReturn = message.kind === "tool_return";
  const argsJson = message.args ? JSON.stringify(message.args, null, 2) : null;

  return (
    <div className={styles.bubble}>
      <div className={styles.bubbleHead}>
        <Label variant={ROLE_VARIANT[message.role] ?? "secondary"}>{message.role}</Label>
        <span className={styles.seq}>seq {message.seq}</span>
      </div>

      {isToolCall ? (
        <details className={styles.details}>
          <summary>{message.content}</summary>
          {argsJson && <CodeBlock>{argsJson}</CodeBlock>}
        </details>
      ) : isToolReturn ? (
        <details className={styles.details}>
          <summary>工具返回</summary>
          <CodeBlock>{message.content}</CodeBlock>
        </details>
      ) : (
        <div className={styles.bubbleBody}>{message.content}</div>
      )}
    </div>
  );
}

/** LLM I/O trace browser: agent list + role-colored message timeline. */
export default function LLMIOPanel() {
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await tracesList();
      setTraces(res.traces);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    traceGet(selectedId)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  return (
    <Panel
      fill
      title={`LLM 输入 / 输出轨迹 (${traces.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {traces.length === 0 ? (
        <EmptyState icon="llm" message="暂无 agent 轨迹。运行搜索产生 rollout 后此处可回放 LLM I/O。" />
      ) : (
        <div className={styles.layout}>
          <div className={styles.list}>
            <ActionList aria-label="Agent 轨迹">
              {traces.map((trace) => (
                <ActionList.Item
                  key={trace.agent_id}
                  active={trace.agent_id === selectedId}
                  onSelect={() => setSelectedId(trace.agent_id)}
                >
                  <span className={styles.agentId}>{trace.agent_id}</span>
                  <ActionList.Description variant="block">
                    {trace.messages} 条 · {formatUpdatedAt(trace.updated_at)}
                  </ActionList.Description>
                </ActionList.Item>
              ))}
            </ActionList>
          </div>

          <div className={styles.pane}>
            {loading && <p className={styles.muted}>加载中…</p>}
            {!loading && !detail && <p className={styles.muted}>选择一个 agent 查看轨迹。</p>}
            {detail && detail.messages.length === 0 && <p className={styles.muted}>该 agent 暂无消息。</p>}
            {detail?.messages.map((message, index) => (
              <TraceBubble key={`${message.seq}-${index}`} message={message} />
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}
```

把 `athena-gui/src/components/LLMIOPanel.module.css` 整体替换为：

```css
/* 模型轨迹：左侧 agent 列表 + 右侧按角色着色的消息块。 */

.layout {
  flex: 1;
  min-height: 320px;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: var(--base-size-12);
}

.list,
.pane {
  min-height: 0;
  overflow-y: auto;
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
}

.pane {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-8);
  padding: var(--base-size-12);
}

.agentId {
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
}

.bubble {
  border: var(--borderWidth-thin) solid var(--borderColor-muted);
  border-radius: var(--borderRadius-medium);
  overflow: hidden;
}

.bubbleHead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--base-size-8);
  padding: var(--base-size-4) var(--base-size-8);
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-muted);
  background: var(--bgColor-muted);
}

.seq {
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.bubbleBody {
  padding: var(--base-size-8) var(--base-size-12);
  font-size: var(--text-body-size-small);
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fgColor-default);
}

.details {
  padding: var(--base-size-8) var(--base-size-12);
  font-size: var(--text-body-size-small);
}

.details summary {
  cursor: pointer;
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-default);
}

.details[open] summary {
  margin-bottom: var(--base-size-8);
}

.muted {
  margin: 0;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-muted);
}
```

把 `athena-gui/src/components/ReportPanel.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import { experimentsList, generateReport, type ExperimentDetail } from "../lib/tauri-bridge";
import { CodeBlock } from "./common/CodeBlock";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import styles from "./ReportPanel.module.css";

interface Summary {
  total: number;
  succeeded: number;
  failed: number;
  running: number;
  pending: number;
  bestPrimary: number | null;
  sotaId: string | null;
  sotaPrimary: number | null;
  hypothesisCount: number;
}

function summarize(experiments: ExperimentDetail[]): Summary {
  const summary: Summary = {
    total: experiments.length,
    succeeded: 0,
    failed: 0,
    running: 0,
    pending: 0,
    bestPrimary: null,
    sotaId: null,
    sotaPrimary: null,
    hypothesisCount: experiments.length,
  };
  for (const d of experiments) {
    const status = d.experiment.status;
    if (status === "SUCCEEDED") summary.succeeded += 1;
    else if (status === "FAILED") summary.failed += 1;
    else if (status === "RUNNING") summary.running += 1;
    else if (status === "PENDING") summary.pending += 1;

    const primary = d.experiment.eval?.primary ?? null;
    if (primary != null && (summary.bestPrimary == null || primary > summary.bestPrimary)) {
      summary.bestPrimary = primary;
    }
    if (d.sota) {
      summary.sotaId = d.experiment.id;
      summary.sotaPrimary = primary;
    }
  }
  return summary;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.stat}>
      <div className={styles.statLabel}>{label}</div>
      <div className={styles.statValue}>{value}</div>
    </div>
  );
}

/** Research summary report: SOTA, best metric, and experiment status breakdown. */
export default function ReportPanel() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await experimentsList();
      setSummary(summarize(res.experiments));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function handleGenerate() {
    try {
      const result = (await generateReport()) as { report?: string };
      setReport(result?.report ?? null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <Panel
      title="研究摘要"
      actions={
        <Button size="small" onClick={() => void handleGenerate()}>
          生成报告
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}
      {report && <CodeBlock>{report}</CodeBlock>}

      {!summary || summary.total === 0 ? (
        <EmptyState icon="report" message="暂无实验数据。完成实验后此处汇总 SOTA、最佳指标与状态分布。" />
      ) : (
        <div className={styles.grid}>
          <Stat label="最佳 primary" value={summary.bestPrimary != null ? summary.bestPrimary.toFixed(4) : "—"} />
          <Stat label="SOTA 实验" value={summary.sotaId ?? "—"} />
          <Stat label="SOTA 指标" value={summary.sotaPrimary != null ? summary.sotaPrimary.toFixed(4) : "—"} />
          <Stat label="实验总数" value={String(summary.total)} />
          <Stat label="成功 / 失败" value={`${summary.succeeded} / ${summary.failed}`} />
          <Stat label="进行中 / 待定" value={`${summary.running} / ${summary.pending}`} />
        </div>
      )}
    </Panel>
  );
}
```

把 `athena-gui/src/components/ReportPanel.module.css` 整体替换为：

```css
/* 研究摘要：统计卡网格。 */

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--base-size-12);
}

.stat {
  padding: var(--base-size-12) var(--base-size-16);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
}

.statLabel {
  font-size: var(--text-body-size-small);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-muted);
}

.statValue {
  margin-top: var(--base-size-4);
  font-family: var(--fontStack-monospace);
  font-size: var(--text-title-size-medium);
  font-weight: var(--base-text-weight-semibold);
  word-break: break-all;
  color: var(--fgColor-default);
}
```

把 `athena-gui/src/components/AlgorithmsPanel.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash, FormControl, Select, TextInput } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  graphAlgorithm,
  graphAlgorithms,
  type GraphAlgorithmInfo,
} from "../lib/tauri-bridge";
import { CodeBlock } from "./common/CodeBlock";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import styles from "./AlgorithmsPanel.module.css";

/** Dropdown + parameter form + JSON result runner for graph algorithms. */
export default function AlgorithmsPanel() {
  const [algorithms, setAlgorithms] = useState<GraphAlgorithmInfo[]>([]);
  const [selectedName, setSelectedName] = useState<string>("");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);

  const selected = algorithms.find((algo) => algo.name === selectedName) ?? null;

  const loadAlgorithms = useCallback(async () => {
    setLoading(true);
    try {
      const res = await graphAlgorithms();
      setAlgorithms(res.algorithms);
      if (res.algorithms.length > 0 && !selectedName) {
        setSelectedName(res.algorithms[0].name);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [selectedName]);

  useEffect(() => {
    void loadAlgorithms();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSelect(name: string) {
    setSelectedName(name);
    setParamValues({});
    setResult(null);
    setError(null);
  }

  function handleParamChange(name: string, value: string) {
    setParamValues((prev) => ({ ...prev, [name]: value }));
  }

  async function run() {
    if (!selected) return;
    setRunning(true);
    setError(null);
    try {
      const params: Record<string, unknown> = {};
      for (const p of selected.params) {
        params[p.name] = paramValues[p.name] ?? "";
      }
      const res = await graphAlgorithm(selected.name, params);
      setResult(res);
    } catch (err) {
      setError(errorMessage(err));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Panel title="图算法">
      <p className={styles.intro}>选择算法、填写参数后运行，结果以 JSON 展示。</p>

      {error && <Flash variant="danger">{error}</Flash>}

      {loading ? (
        <EmptyState icon="loader" message="加载算法清单…" />
      ) : algorithms.length === 0 ? (
        <EmptyState icon="algorithms" message="暂无可用算法。" />
      ) : (
        <>
          <div className={styles.runRow}>
            <FormControl>
              <FormControl.Label>算法</FormControl.Label>
              <Select value={selectedName} onChange={(e) => handleSelect(e.target.value)}>
                {algorithms.map((algo) => (
                  <Select.Option key={algo.name} value={algo.name}>
                    {algo.label}
                  </Select.Option>
                ))}
              </Select>
            </FormControl>
            <Button variant="primary" onClick={() => void run()} disabled={running || !selected}>
              {running ? "运行中…" : "运行"}
            </Button>
          </div>

          {selected && selected.params.length > 0 && (
            <div className={styles.params}>
              {selected.params.map((p) => (
                <FormControl key={p.name} required={p.required}>
                  <FormControl.Label>{p.name}</FormControl.Label>
                  <TextInput
                    block
                    value={paramValues[p.name] ?? ""}
                    onChange={(e) => handleParamChange(p.name, e.target.value)}
                    placeholder={p.required ? "必填" : "可选"}
                  />
                </FormControl>
              ))}
            </div>
          )}
        </>
      )}

      {result != null && <CodeBlock>{JSON.stringify(result, null, 2)}</CodeBlock>}
    </Panel>
  );
}
```

Create `athena-gui/src/components/AlgorithmsPanel.module.css`:

```css
/* 图算法：算法选择、参数表单与 JSON 结果。 */

.intro {
  margin: 0;
  font-size: var(--text-body-size-medium);
  color: var(--fgColor-muted);
}

.runRow {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--base-size-8);
}

.params {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--base-size-12) var(--base-size-16);
}
```

把 `athena-gui/src/components/EDAPreview.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import ReactMarkdown from "react-markdown";
import { edaReport, type EdaReport } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import { Section } from "./common/Section";
import styles from "./EDAPreview.module.css";

/** EDA (探索性数据分析) 报告预览：Markdown 正文 + 图表。 */
export default function EDAPreview() {
  const [data, setData] = useState<EdaReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await edaReport());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Panel
      title="EDA 报告"
      actions={
        <Button size="small" onClick={() => void refresh()}>
          刷新
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!data || !data.eda_dir ? (
        <EmptyState icon="files" message="暂无 EDA 报告。运行 PREPARE 阶段后将在此预览数据探索结果。" />
      ) : (
        <div className={styles.body}>
          {data.report ? (
            <article className={styles.markdown}>
              <ReactMarkdown>{data.report}</ReactMarkdown>
            </article>
          ) : (
            <EmptyState icon="report" message="EDA 目录中未找到 report.md。" />
          )}

          {data.figures.length > 0 && (
            <Section title={`图表 (${data.figures.length})`}>
              {data.figures.map((figure) => (
                <figure key={figure.name} className={styles.figure}>
                  <img
                    src={`data:${figure.mime};base64,${figure.data}`}
                    alt={figure.name}
                    className={styles.figureImg}
                  />
                  <figcaption className={styles.figureCaption}>{figure.name}</figcaption>
                </figure>
              ))}
            </Section>
          )}
        </div>
      )}
    </Panel>
  );
}
```

把 `athena-gui/src/components/EDAPreview.module.css` 整体替换为：

```css
/* EDA 报告：GitHub 渲染 Markdown 的排版 + 图表列表。 */

.body {
  display: flex;
  flex-direction: column;
  gap: var(--base-size-16);
}

.markdown {
  font-size: var(--text-body-size-large);
  line-height: 1.5;
  color: var(--fgColor-default);
  word-break: break-word;
}

.markdown > :first-child {
  margin-top: 0;
}

.markdown h1,
.markdown h2,
.markdown h3,
.markdown h4 {
  margin: var(--base-size-24) 0 var(--base-size-16);
  font-weight: var(--base-text-weight-semibold);
  line-height: 1.25;
}

.markdown h1,
.markdown h2 {
  padding-bottom: 0.3em;
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-muted);
}

.markdown h1 {
  font-size: 2em;
}

.markdown h2 {
  font-size: 1.5em;
}

.markdown h3 {
  font-size: 1.25em;
}

.markdown h4 {
  font-size: 1em;
}

.markdown p,
.markdown ul,
.markdown ol,
.markdown table,
.markdown pre,
.markdown blockquote {
  margin: 0 0 var(--base-size-16);
}

.markdown ul,
.markdown ol {
  padding-left: 2em;
}

.markdown code {
  padding: 0.2em 0.4em;
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-neutral-muted);
  font-family: var(--fontStack-monospace);
  font-size: 85%;
}

.markdown pre {
  padding: var(--base-size-16);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-muted);
  overflow: auto;
  font-size: 85%;
  line-height: 1.45;
}

.markdown pre code {
  padding: 0;
  background: transparent;
  font-size: 100%;
}

.markdown blockquote {
  padding: 0 1em;
  border-left: 0.25em solid var(--borderColor-default);
  color: var(--fgColor-muted);
}

.markdown table {
  display: block;
  width: max-content;
  max-width: 100%;
  overflow: auto;
  border-collapse: collapse;
}

.markdown th,
.markdown td {
  padding: 6px 13px;
  border: var(--borderWidth-thin) solid var(--borderColor-default);
}

.markdown th {
  font-weight: var(--base-text-weight-semibold);
}

.markdown tr:nth-child(2n) {
  background: var(--bgColor-muted);
}

.figure {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
}

.figureImg {
  width: 100%;
  max-width: 720px;
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-muted);
}

.figureCaption {
  font-family: var(--fontStack-monospace);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}
```

- [ ] **Step 3: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/__tests__/insight-panels.test.tsx`
Expected: 7 passed。

Run: `npm --prefix athena-gui test` → 231 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 4: 提交**

```bash
git add athena-gui/src/components/__tests__/insight-panels.test.tsx athena-gui/src/components/LLMIOPanel.tsx athena-gui/src/components/LLMIOPanel.module.css athena-gui/src/components/ReportPanel.tsx athena-gui/src/components/ReportPanel.module.css athena-gui/src/components/AlgorithmsPanel.tsx athena-gui/src/components/AlgorithmsPanel.module.css athena-gui/src/components/EDAPreview.tsx athena-gui/src/components/EDAPreview.module.css
git commit -m "feat(gui): 轨迹、报告、图算法与 EDA 面板改用 Primer 组件"
```

---

### Task 10: 可视化（指标曲线、研究树、假设图）

**Files:**
- Create: `athena-gui/src/components/common/FlowNodeCard.tsx`、`FlowNodeCard.module.css`
- Create: `athena-gui/src/components/__tests__/visualization.test.tsx`
- Modify: `athena-gui/src/components/MetricChart.tsx`；Create: `MetricChart.module.css`
- Modify: `athena-gui/src/components/ResearchTreeViz.tsx`
- Modify: `athena-gui/src/components/HypothesisGraphViz.tsx`、`HypothesisGraphViz.module.css`

**Interfaces:**
- Consumes: Task 2 的 `Panel`、`EmptyState`、`hypothesisVariant` / `hypothesisBorderColor` / `HYPOTHESIS_STATUS_VARIANT`。
- Produces: `FlowNodeCard({ statement, primaryText, status, badge })`；`minimapNodeClass(status: string): string`；`GraphFrame({ label, children })`。**删除** `ResearchTreeViz.tsx` 导出的 `STATUS_COLORS` 与 `statusColor`（唯一使用方 `HypothesisGraphViz` 同步改为 `hypothesisStatus`）。

**实现要点（实测得出）：**
- ReactFlow 的小地图节点与背景点阵通过 SVG `fill` **属性**上色，属性里的 `var(--…)` 是否生效取决于浏览器。因此：背景点阵不再传 `color`，改由 `GraphFrame` 的 CSS（`:global(.react-flow__background circle)`）设置 `fill`；小地图用 `nodeClassName={(n) => minimapNodeClass(...)}`，颜色写在 CSS Module 中。边与箭头的颜色通过 `style` 设置，可以直接用 `var(--…)`。
- 研究树测试依赖「第一个按钮是刷新」「自动保存是 checkbox」，`Panel` 操作区按「刷新、保存、自动保存复选框」排列。
- 研究树节点的标签文字仍是实验状态，配色仍取假设状态（与旧实现一致）。

- [ ] **Step 1: 写失败测试**

Create `athena-gui/src/components/__tests__/visualization.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { ReactFlowProvider } from "reactflow";
import { renderUi } from "../../test/render";
import { FlowNodeCard } from "../common/FlowNodeCard";
import MetricChart from "../MetricChart";
import HypothesisGraphViz from "../HypothesisGraphViz";

const bridgeMocks = vi.hoisted(() => ({
  experimentsList: vi.fn(),
  hypothesisGraph: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  ...bridgeMocks,
}));

beforeEach(() => {
  for (const mock of Object.values(bridgeMocks)) mock.mockReset();
});

function renderNode(status: string) {
  return renderUi(
    <ReactFlowProvider>
      <FlowNodeCard statement="加入 120 分钟滑动窗口统计特征" primaryText="0.7412" status={status} badge={status} />
    </ReactFlowProvider>,
  );
}

describe("FlowNodeCard", () => {
  it("colors the label and left border by hypothesis status", () => {
    renderNode("SUPPORTED");
    const label = screen.getByText("SUPPORTED");
    expect(label).toHaveAttribute("data-variant", "success");
    expect(label.parentElement).toHaveStyle({ borderLeftColor: "var(--borderColor-success-emphasis)" });
    expect(screen.getByText("0.7412")).toBeInTheDocument();
  });

  it("falls back to neutral styling for an unknown status", () => {
    renderNode("ARCHIVED");
    const label = screen.getByText("ARCHIVED");
    expect(label).toHaveAttribute("data-variant", "secondary");
    expect(label.parentElement).toHaveStyle({ borderLeftColor: "var(--borderColor-emphasis)" });
  });
});

describe("MetricChart", () => {
  it("shows the empty state when no experiment has a metric", async () => {
    bridgeMocks.experimentsList.mockResolvedValue({ experiments: [] });
    renderUi(<MetricChart />);
    expect(await screen.findByText("暂无评估指标。运行实验后此处展示 primary 指标趋势。")).toBeInTheDocument();
  });

  it("shows the error when loading experiments fails", async () => {
    bridgeMocks.experimentsList.mockRejectedValue(new Error("metrics unavailable"));
    renderUi(<MetricChart />);
    expect(await screen.findByText("metrics unavailable")).toBeInTheDocument();
  });
});

describe("HypothesisGraphViz", () => {
  it("shows the empty state before any hypothesis exists", async () => {
    bridgeMocks.hypothesisGraph.mockResolvedValue({ nodes: [], edges: [] });
    renderUi(<HypothesisGraphViz />);
    expect(await screen.findByText("暂无假设图数据。启动搜索产生假设后，此处将展示谱系与取代关系。")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "假设图 (0 个假设)" })).toBeInTheDocument();
  });

  it("shows the error when loading the graph fails", async () => {
    bridgeMocks.hypothesisGraph.mockRejectedValue(new Error("graph unavailable"));
    renderUi(<HypothesisGraphViz />);
    expect(await screen.findByText("graph unavailable")).toBeInTheDocument();
  });
});
```

Run: `npm --prefix athena-gui test -- src/components/__tests__/visualization.test.tsx`
Expected: FAIL，找不到 `../common/FlowNodeCard`。

- [ ] **Step 2: 新建节点卡片与图容器**

Create `athena-gui/src/components/common/FlowNodeCard.tsx`:

```tsx
import type { ReactNode } from "react";
import { Label } from "@primer/react";
import { Handle, Position } from "reactflow";
import { hypothesisBorderColor, hypothesisVariant } from "./hypothesisStatus";
import styles from "./FlowNodeCard.module.css";

interface FlowNodeCardProps {
  statement: string;
  primaryText: string;
  /** 假设状态：决定左边框与标签配色。 */
  status: string;
  /** 标签文字，如实验状态或「SUPPORTED ★ SOTA」。 */
  badge: string;
}

/** 研究树 / 假设图共用的 ReactFlow 节点：GitHub 卡片 + 状态标签 + 状态色左边框。 */
export function FlowNodeCard({ statement, primaryText, status, badge }: FlowNodeCardProps) {
  return (
    <div className={styles.node} style={{ borderLeftColor: hypothesisBorderColor(status) }}>
      <Handle type="target" position={Position.Top} />
      <div className={styles.title}>
        {statement.slice(0, 48)}
        {statement.length > 48 ? "…" : ""}
      </div>
      <div className={styles.meta}>
        Primary: <strong>{primaryText}</strong>
      </div>
      <Label variant={hypothesisVariant(status)}>{badge}</Label>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

/** 小地图节点的 class：按假设状态着色（SVG 的 fill 属性不解析 CSS 变量，改由 CSS 设置）。 */
export function minimapNodeClass(status: string): string {
  return `${styles.minimapNode} ${styles[`minimap-${hypothesisVariant(status)}`] ?? ""}`;
}

/** 图容器：Primer 边框与圆角，并把 ReactFlow 的控件、小地图、背景点阵改用 Primer 变量。 */
export function GraphFrame({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.frame} role="region" aria-label={label}>
      {children}
    </div>
  );
}
```

Create `athena-gui/src/components/common/FlowNodeCard.module.css`:

```css
/* ReactFlow 节点卡片与图容器。ReactFlow 内部元素用 :global 选择器改为 Primer 变量。 */

.node {
  min-width: 200px;
  max-width: 280px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--base-size-4);
  padding: var(--base-size-8) var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-left-width: var(--base-size-4);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  color: var(--fgColor-default);
  font-size: var(--text-body-size-medium);
  box-shadow: var(--shadow-resting-small);
}

.title {
  font-weight: var(--base-text-weight-semibold);
}

.meta {
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.meta strong {
  color: var(--fgColor-default);
}

.frame {
  position: relative;
  flex: 1;
  min-height: 360px;
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-default);
  overflow: hidden;
}

.frame :global(.react-flow__background circle) {
  fill: var(--borderColor-default);
}

.frame :global(.react-flow__controls) {
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  overflow: hidden;
  box-shadow: var(--shadow-resting-small);
}

.frame :global(.react-flow__controls-button) {
  border-bottom: var(--borderWidth-thin) solid var(--borderColor-muted);
  background: var(--bgColor-default);
  color: var(--fgColor-default);
  fill: var(--fgColor-default);
}

.frame :global(.react-flow__controls-button:hover) {
  background: var(--bgColor-muted);
}

.frame :global(.react-flow__minimap) {
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--bgColor-muted);
}

.frame :global(.react-flow__minimap-mask) {
  fill: var(--bgColor-neutral-muted);
}

.frame :global(.react-flow__edge-text) {
  fill: var(--fgColor-muted);
}

.frame :global(.react-flow__edge-textbg) {
  fill: var(--bgColor-default);
}

.frame :global(.react-flow__handle) {
  border-color: var(--bgColor-default);
  background: var(--borderColor-emphasis);
}

.frame :global(.react-flow__attribution) {
  background: transparent;
  color: var(--fgColor-muted);
}

.minimapNode {
  fill: var(--borderColor-emphasis);
}

.minimap-success {
  fill: var(--borderColor-success-emphasis);
}

.minimap-danger {
  fill: var(--borderColor-danger-emphasis);
}

.minimap-accent {
  fill: var(--borderColor-accent-emphasis);
}

.minimap-attention {
  fill: var(--borderColor-attention-emphasis);
}
```

- [ ] **Step 3: 改写三个可视化面板**

把 `athena-gui/src/components/ResearchTreeViz.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Checkbox, Flash, FormControl } from "@primer/react";
import { errorMessage } from "../lib/errors";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  type Node,
  type Edge,
  type NodeChange,
  useNodesState,
  useEdgesState,
  MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  treeGet,
  treeSave,
  type HypothesisData,
  type ResearchExperimentData,
  type ResearchTreeData,
} from "../lib/tauri-bridge";
import { layoutLineage } from "../lib/graphLayout";
import { EmptyState } from "./common/EmptyState";
import { FlowNodeCard, GraphFrame, minimapNodeClass } from "./common/FlowNodeCard";
import { Panel } from "./common/Panel";

const AUTO_SAVE_MS = 30_000;
const NODE_W = 220;
const NODE_H = 92;

/** Custom React Flow node rendering a research experiment with hypothesis status. */
interface ResearchNodeData {
  id: string;
  experiment: ResearchExperimentData;
  hypothesis: HypothesisData;
}

function ResearchNode({ data }: { data: ResearchNodeData }) {
  return (
    <FlowNodeCard
      statement={data.hypothesis.statement}
      primaryText={data.experiment.eval ? data.experiment.eval.primary.toFixed(4) : "无指标"}
      status={data.hypothesis.status}
      badge={data.experiment.status}
    />
  );
}

const nodeTypes = { researchNode: ResearchNode };

/** Converts a ResearchTreeData model into React Flow nodes/edges laid out by d3-dag. */
function buildGraph(tree: ResearchTreeData): { nodes: Node[]; edges: Edge[] } {
  const ids = Object.keys(tree.experiments);
  const lineageEdges: { source: string; target: string }[] = [];

  for (const [id, experiment] of Object.entries(tree.experiments)) {
    const hypothesis = tree.hypotheses[experiment.hypothesis_id];
    if (!hypothesis) {
      throw new Error(`Experiment ${id} references missing hypothesis ${experiment.hypothesis_id}`);
    }
    if (experiment.parent_id && tree.experiments[experiment.parent_id]) {
      lineageEdges.push({ source: experiment.parent_id, target: id });
    }
  }

  const positions = layoutLineage(ids, lineageEdges, { nodeWidth: NODE_W, nodeHeight: NODE_H });

  const nodes: Node[] = Object.entries(tree.experiments).map(([id, experiment]) => {
    const hypothesis = tree.hypotheses[experiment.hypothesis_id];
    const pos = positions.get(id) ?? { x: 0, y: 0 };
    return {
      id,
      type: "researchNode",
      position: pos,
      data: { id, experiment, hypothesis },
    };
  });

  const edges: Edge[] = lineageEdges.map((edge) => ({
    id: `${edge.source}->${edge.target}`,
    source: edge.source,
    target: edge.target,
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "var(--fgColor-muted)" },
    style: { stroke: "var(--fgColor-muted)", strokeWidth: 1.75 },
  }));

  return { nodes, edges };
}

/**
 * Interactive research tree visualization.
 * Loads experiment data from the backend, renders a React Flow graph laid out
 * by d3-dag, and supports manual drag, save/refresh, and auto-save.
 */
export default function ResearchTreeViz() {
  const [tree, setTree] = useState<ResearchTreeData | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoSave, setAutoSave] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await treeGet();
      setTree(result.tree);
      setError(null);
    } catch (err) {
      // Keep the last rendered tree, but surface the failure when there is nothing to show.
      setError(errorMessage(err));
    }
  }, []);

  const save = useCallback(async () => {
    setSaving(true);
    try {
      await treeSave();
      await refresh();
    } finally {
      setSaving(false);
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (autoSave) {
      timerRef.current = setInterval(() => void save(), AUTO_SAVE_MS);
    } else if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [autoSave, save]);

  const toggleAutoSave = useCallback(() => setAutoSave((value) => !value), []);
  const nodeCount = tree ? Object.keys(tree.experiments).length : 0;

  const graph = useMemo(
    () => (tree && nodeCount > 0 ? buildGraph(tree) : { nodes: [] as Node[], edges: [] as Edge[] }),
    [tree, nodeCount],
  );

  const positionedNodeIdsRef = useRef(new Set<string>());
  const [nodes, setNodes, applyNodesChange] = useNodesState(graph.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(graph.edges);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    for (const change of changes) {
      if (change.type === "position" && change.position) {
        positionedNodeIdsRef.current.add(change.id);
      }
    }
    applyNodesChange(changes);
  }, [applyNodesChange]);

  useEffect(() => {
    setNodes((currentNodes) => {
      const currentNodesById = new Map(currentNodes.map((node) => [node.id, node]));
      const refreshedNodeIds = new Set(graph.nodes.map((node) => node.id));

      for (const id of positionedNodeIdsRef.current) {
        if (!refreshedNodeIds.has(id)) {
          positionedNodeIdsRef.current.delete(id);
        }
      }

      return graph.nodes.map((node) => ({
        ...node,
        position: positionedNodeIdsRef.current.has(node.id)
          ? currentNodesById.get(node.id)?.position ?? node.position
          : node.position,
      }));
    });
    setEdges(graph.edges);
  }, [graph.edges, graph.nodes, setEdges, setNodes]);

  const actions = (
    <>
      <Button size="small" onClick={() => void refresh()}>
        刷新
      </Button>
      <Button size="small" onClick={() => void save()} disabled={saving}>
        {saving ? "保存中…" : "保存"}
      </Button>
      <FormControl>
        <Checkbox checked={autoSave} onChange={toggleAutoSave} />
        <FormControl.Label>自动保存</FormControl.Label>
      </FormControl>
    </>
  );

  if (!tree || nodeCount === 0) {
    return (
      <Panel title="研究树 (0 个实验)" actions={actions}>
        {error && <Flash variant="danger">{error}</Flash>}
        <EmptyState icon="tree" message="暂无实验数据。启动搜索后研究树将自动构建。" />
      </Panel>
    );
  }

  return (
    <Panel fill title={`研究树 (${nodeCount} 个实验)`} actions={actions}>
      <GraphFrame label="研究树可视化">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          minZoom={0.3}
          maxZoom={2}
        >
          <Background variant={BackgroundVariant.Dots} gap={24} size={1} />
          <Controls />
          <MiniMap nodeClassName={(n) => minimapNodeClass((n.data as ResearchNodeData).hypothesis.status)} />
        </ReactFlow>
      </GraphFrame>
    </Panel>
  );
}
```

把 `athena-gui/src/components/HypothesisGraphViz.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Flash, Label } from "@primer/react";
import { errorMessage } from "../lib/errors";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  MarkerType,
  type Node,
  type Edge,
  useNodesState,
  useEdgesState,
} from "reactflow";
import "reactflow/dist/style.css";
import { hypothesisGraph, type HypGraph, type HypGraphNode } from "../lib/tauri-bridge";
import { layoutLineage } from "../lib/graphLayout";
import { EmptyState } from "./common/EmptyState";
import { FlowNodeCard, GraphFrame, minimapNodeClass } from "./common/FlowNodeCard";
import { HYPOTHESIS_STATUS_VARIANT } from "./common/hypothesisStatus";
import { Panel } from "./common/Panel";
import styles from "./HypothesisGraphViz.module.css";

const NODE_W = 220;
const NODE_H = 96;

interface HypNodeData {
  node: HypGraphNode;
}

/** Custom React Flow node for a single hypothesis. */
function HypNode({ data }: { data: HypNodeData }) {
  return (
    <FlowNodeCard
      statement={data.node.statement}
      primaryText={data.node.primary != null ? data.node.primary.toFixed(4) : "未安排实验"}
      status={data.node.status}
      badge={`${data.node.status}${data.node.sota ? " ★ SOTA" : ""}`}
    />
  );
}

const nodeTypes = { hypNode: HypNode };

/** Convert the hypothesis graph DTO into React Flow nodes/edges laid out by d3-dag. */
function buildHypGraph(graph: HypGraph): { nodes: Node[]; edges: Edge[] } {
  const lineageEdges = graph.edges.filter((e) => e.kind === "lineage");
  const positions = layoutLineage(
    graph.nodes.map((n) => n.id),
    lineageEdges.map((e) => ({ source: e.source, target: e.target })),
    { nodeWidth: NODE_W, nodeHeight: NODE_H },
  );

  const nodes: Node[] = graph.nodes.map((node) => {
    const pos = positions.get(node.id) ?? { x: 0, y: 0 };
    return {
      id: node.id,
      type: "hypNode",
      position: pos,
      data: { node },
    };
  });

  const edges: Edge[] = graph.edges.map((edge) => {
    const supersedes = edge.kind === "supersedes";
    const color = supersedes ? "var(--fgColor-danger)" : "var(--fgColor-muted)";
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.label,
      style: {
        stroke: color,
        strokeWidth: supersedes ? 1.5 : 1.75,
        strokeDasharray: supersedes ? "6 4" : undefined,
      },
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color },
    };
  });

  return { nodes, edges };
}

/** Legend describing edge and status coloring. */
function Legend() {
  return (
    <div className={styles.legend}>
      <span className={styles.legendTitle}>图例</span>
      <span className={styles.legendRow}>
        <span className={styles.legendLine} />
        谱系 (lineage)
      </span>
      <span className={styles.legendRow}>
        <span className={`${styles.legendLine} ${styles.legendLineDashed}`} />
        取代 (supersedes)
      </span>
      <span className={styles.legendSwatches}>
        {Object.entries(HYPOTHESIS_STATUS_VARIANT).map(([status, variant]) => (
          <Label key={status} variant={variant} size="small">
            {status}
          </Label>
        ))}
      </span>
    </div>
  );
}

/** Interactive hypothesis-graph visualization with lineage + supersedes edges. */
export default function HypothesisGraphViz() {
  const [graphData, setGraphData] = useState<HypGraph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await hypothesisGraph();
      setGraphData(result);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const { nodes: builtNodes, edges: builtEdges } = useMemo(
    () => (graphData ? buildHypGraph(graphData) : { nodes: [] as Node[], edges: [] as Edge[] }),
    [graphData],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(builtNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(builtEdges);

  useEffect(() => {
    setNodes(builtNodes);
    setEdges(builtEdges);
  }, [builtNodes, builtEdges, setNodes, setEdges]);

  const nodeCount = graphData?.nodes.length ?? 0;

  return (
    <Panel
      fill
      title={`假设图 (${nodeCount} 个假设)`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {!graphData || nodeCount === 0 ? (
        <EmptyState icon="network" message="暂无假设图数据。启动搜索产生假设后，此处将展示谱系与取代关系。" />
      ) : (
        <GraphFrame label="假设图可视化">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.3 }}
            minZoom={0.3}
            maxZoom={2}
          >
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} />
            <Controls />
            <MiniMap nodeClassName={(n) => minimapNodeClass((n.data as HypNodeData).node.status)} />
          </ReactFlow>
          <Legend />
        </GraphFrame>
      )}
    </Panel>
  );
}
```

把 `athena-gui/src/components/HypothesisGraphViz.module.css` 整体替换为：

```css
/* 假设图图例：浮在图容器右上角。 */

.legend {
  position: absolute;
  top: var(--base-size-12);
  right: var(--base-size-12);
  z-index: 5;
  display: flex;
  flex-direction: column;
  gap: var(--base-size-4);
  padding: var(--base-size-8) var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--overlay-bgColor);
  box-shadow: var(--shadow-floating-small);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-muted);
}

.legendTitle {
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-default);
}

.legendRow {
  display: flex;
  align-items: center;
  gap: var(--base-size-4);
}

.legendLine {
  display: inline-block;
  width: var(--base-size-20);
  height: 0;
  border-top: 2px solid var(--fgColor-muted);
}

.legendLineDashed {
  border-top-style: dashed;
  border-top-color: var(--fgColor-danger);
}

.legendSwatches {
  display: flex;
  flex-wrap: wrap;
  gap: var(--base-size-4);
}
```

把 `athena-gui/src/components/MetricChart.tsx` 整体替换为：

```tsx
import { useCallback, useEffect, useState } from "react";
import { Button, Flash } from "@primer/react";
import { errorMessage } from "../lib/errors";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { experimentsList, type ExperimentDetail } from "../lib/tauri-bridge";
import { EmptyState } from "./common/EmptyState";
import { Panel } from "./common/Panel";
import styles from "./MetricChart.module.css";

interface Point {
  name: string;
  primary: number;
  status: string;
  sota: boolean;
}

function shortId(id: string): string {
  return id.length > 12 ? `…${id.slice(-8)}` : id;
}

function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: Point }> }) {
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0].payload;
  return (
    <div className={styles.tooltip}>
      <div className={styles.tooltipTitle}>{point.name}</div>
      <div className={styles.tooltipRow}>
        <span>primary</span>
        <strong>{point.primary.toFixed(4)}</strong>
      </div>
      <div className={styles.tooltipRow}>
        <span>status</span>
        <strong>{point.status}</strong>
      </div>
      {point.sota && <div className={styles.tooltipSota}>★ SOTA</div>}
    </div>
  );
}

/** Primary-metric trend across experiments, with the SOTA point highlighted. */
export default function MetricChart() {
  const [points, setPoints] = useState<Point[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await experimentsList();
      setPoints(
        res.experiments
          .filter((d: ExperimentDetail) => d.experiment.eval?.primary != null)
          .map((d: ExperimentDetail) => ({
            name: shortId(d.experiment.id),
            primary: d.experiment.eval!.primary,
            status: d.experiment.status,
            sota: d.sota,
          })),
      );
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <Panel
      title={`指标趋势 (${points.length})`}
      actions={
        <Button size="small" onClick={() => void refresh()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </Button>
      }
    >
      {error && <Flash variant="danger">{error}</Flash>}

      {points.length === 0 ? (
        <EmptyState icon="metrics" message="暂无评估指标。运行实验后此处展示 primary 指标趋势。" />
      ) : (
        <div className={styles.chart}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 12, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--borderColor-muted)" vertical={false} />
              <XAxis
                dataKey="name"
                tick={{ fontSize: 11, fill: "var(--fgColor-muted)" }}
                tickLine={false}
                axisLine={{ stroke: "var(--borderColor-default)" }}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "var(--fgColor-muted)" }}
                tickLine={false}
                axisLine={false}
                width={52}
                domain={["auto", "auto"]}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--borderColor-default)" }} />
              <Line
                type="monotone"
                dataKey="primary"
                stroke="var(--data-blue-color-emphasis)"
                strokeWidth={2}
                isAnimationActive={false}
                dot={({ cx, cy, payload }: { cx?: number; cy?: number; payload?: Point }) => {
                  if (cx == null || cy == null || !payload) return <></>;
                  return (
                    <circle
                      cx={cx}
                      cy={cy}
                      r={payload.sota ? 6 : 3}
                      className={payload.sota ? styles.sotaDot : styles.dot}
                      strokeWidth={payload.sota ? 2 : 1}
                    />
                  );
                }}
                activeDot={{ r: 5 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Panel>
  );
}
```

Create `athena-gui/src/components/MetricChart.module.css`:

```css
/* 指标趋势图：Recharts 曲线、数据点与浮层，全部使用 Primer 变量。 */

.chart {
  width: 100%;
  height: 280px;
}

.dot {
  fill: var(--data-blue-color-emphasis);
  stroke: var(--bgColor-default);
}

.sotaDot {
  fill: var(--fgColor-success);
  stroke: var(--bgColor-default);
}

.tooltip {
  padding: var(--base-size-8) var(--base-size-12);
  border: var(--borderWidth-thin) solid var(--borderColor-default);
  border-radius: var(--borderRadius-medium);
  background: var(--overlay-bgColor);
  box-shadow: var(--shadow-floating-small);
  font-size: var(--text-body-size-small);
  color: var(--fgColor-default);
}

.tooltipTitle {
  margin-bottom: var(--base-size-4);
  font-family: var(--fontStack-monospace);
  font-weight: var(--base-text-weight-semibold);
}

.tooltipRow {
  display: flex;
  justify-content: space-between;
  gap: var(--base-size-12);
  color: var(--fgColor-muted);
}

.tooltipRow strong {
  color: var(--fgColor-default);
}

.tooltipSota {
  margin-top: var(--base-size-4);
  font-weight: var(--base-text-weight-semibold);
  color: var(--fgColor-success);
}
```

- [ ] **Step 4: 运行测试**

Run: `npm --prefix athena-gui test -- src/components/__tests__/visualization.test.tsx src/components/__tests__/research-tree-viz.test.tsx`
Expected: `visualization` 6 passed，`research-tree-viz` 6 passed（现有断言无需修改）。

Run: `npm --prefix athena-gui test` → 237 passed；`npm --prefix athena-gui run build` → 成功。

- [ ] **Step 5: 提交**

```bash
git add athena-gui/src/components/common/FlowNodeCard.tsx athena-gui/src/components/common/FlowNodeCard.module.css athena-gui/src/components/__tests__/visualization.test.tsx athena-gui/src/components/MetricChart.tsx athena-gui/src/components/MetricChart.module.css athena-gui/src/components/ResearchTreeViz.tsx athena-gui/src/components/HypothesisGraphViz.tsx athena-gui/src/components/HypothesisGraphViz.module.css
git commit -m "feat(gui): 指标图、研究树与假设图改用 Primer 配色与卡片"
```

---

### Task 11: 清理旧设计系统与依赖

**Files:**
- Modify: `athena-gui/src/styles.css`（整体替换，删除兼容层与全部旧组件类）
- Modify: `athena-gui/src/App.tsx`；Create: `athena-gui/src/App.module.css`
- Delete: `athena-gui/src/components/shell/WorkspaceSwitcher.tsx`、`WorkspaceSwitcher.module.css`（全仓无 import 的死组件，且引用了将被删除的旧 token）
- Modify: `athena-gui/package.json`、`athena-gui/package-lock.json`

- [ ] **Step 1: 确认旧全局 class 只剩 App 的加载占位**

Run: `grep -rn 'className="' athena-gui/src --include=*.tsx | grep -v __tests__`
Expected: 只有 `src/App.tsx` 中的 `className="app-loading"`。若还有其他结果，说明前面某个任务漏改，先回到对应任务修正。

- [ ] **Step 2: App 加载占位改用 Primer Spinner**

在 `athena-gui/src/App.tsx` 中：加 `import { Spinner } from "@primer/react";` 与 `import styles from "./App.module.css";`，把 `return <div className="app-loading">加载中…</div>;` 替换为：

```tsx
    return (
      <div className={styles.loading}>
        <Spinner size="small" srText={null} />
        加载中…
      </div>
    );
```

Create `athena-gui/src/App.module.css`:

```css
/* 工作区设置加载前的占位。 */

.loading {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--base-size-12);
  background: var(--bgColor-default);
  color: var(--fgColor-muted);
  font-size: var(--text-body-size-medium);
}
```

- [ ] **Step 3: 删除兼容层与旧设计系统**

把 `athena-gui/src/styles.css` 整体替换为：

```css
/* Athena GUI 全局样式。颜色、尺寸、字体全部来自 GitHub Primer（@primer/primitives），
   组件样式见各自的 CSS Module。 */

:root {
  /* fontsource 注册的族名是 "Mona Sans Variable"，Primer 字体栈第一项是 "Mona Sans VF"，此处前置补齐。 */
  --fontStack-sansSerif: "Mona Sans Variable", "Mona Sans VF", -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans Backtick Fix", "Noto Sans", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  --BaseStyles-fontFamily: var(--fontStack-sansSerif);
}

* {
  box-sizing: border-box;
}

html,
body,
#root {
  margin: 0;
  min-height: 100%;
}

body {
  min-height: 100vh;
  background: var(--bgColor-default);
  color: var(--fgColor-default);
}
```

删除 `athena-gui/src/components/shell/WorkspaceSwitcher.tsx` 与 `WorkspaceSwitcher.module.css`。

- [ ] **Step 4: 移除旧依赖**

Run: `npm --prefix athena-gui uninstall lucide-react @fontsource/geist @fontsource/geist-mono`

- [ ] **Step 5: 验收检查（每条都应无输出）**

```bash
grep -rnE "#[0-9a-fA-F]{3,8}\b|rgba?\(" athena-gui/src --include=*.css --include=*.tsx --include=*.ts | grep -v __tests__
grep -rnE "var\(--(bg|surface|surface-elevated|surface-muted|border|border-strong|border-hover|border-focus|accent|accent-hover|accent-subtle|accent-soft|text-primary|text-secondary|text-tertiary|success|warning|danger|info|shadow-(xs|sm|md|lg)|font-sans|font-mono|radius-[a-z]+|space-[a-z0-9]+|chart-[0-9]|status-[a-z-]+|ease|dur)\)" athena-gui/src
grep -rn "@primer/react/experimental\|@primer/react/deprecated" athena-gui/src
grep -n "lucide-react\|@fontsource/geist" athena-gui/package.json
```

- [ ] **Step 6: 全量测试、构建与体积记录**

Run: `npm --prefix athena-gui test` → 237 passed；`npm --prefix athena-gui run build` → 成功。

记录体积（写入完成报告）。沙盒实测参考值：JS 合计 1643.2 kB（基线 1009.5），CSS 420.7 kB（基线 112.5），`dist` 2128.3 kB（基线 1409.7），主分块 `index` 883.9 kB / gzip 220.0 kB。Vite 可能提示主分块超过 500 kB，属预期（桌面应用）。

- [ ] **Step 7: 提交**

```bash
git add athena-gui/src/styles.css athena-gui/src/App.tsx athena-gui/src/App.module.css athena-gui/src/components/shell/WorkspaceSwitcher.tsx athena-gui/src/components/shell/WorkspaceSwitcher.module.css athena-gui/package.json athena-gui/package-lock.json
git commit -m "refactor(gui): 删除旧设计系统、兼容层与 lucide/Geist 依赖"
```

---

### Task 12: 实机验收、独立评审与收尾

- [ ] **Step 1: 启动实机**：仓库根目录 `uv sync --frozen` 后运行 `npm run dev:web`，打开 http://localhost:1421，选择一个**临时空目录**作为工作区。
- [ ] **Step 2: 亮/暗两种主题逐屏截图核对**：工作区选择、会话（运行控制、确认卡、消息流）、设置弹窗、人工确认弹窗（可用测试替身触发）、研究树、实验、实验日志、模型轨迹、报告、数据分析、日志侧栏、详情侧栏。无白底残留、无文字不可读、无布局溢出；设置弹窗页脚不再压住表单。
- [ ] **Step 3: 键盘核对**：Tab 能到达顶栏、模块标签、左侧栏、主区控件；两个弹窗打开时焦点被限制在弹窗内并能循环；设置弹窗 Esc 关闭；人工确认弹窗 Esc 仅在允许跳过时生效；焦点环可见。
- [ ] **Step 4: 窄窗口**：把窗口缩到约 900px 宽，模块标签收进「More items」菜单后所有模块仍可到达；面包屑截断、悬停显示完整路径。
- [ ] **Step 5: Codex 独立评审**（只读）：`codex exec --sandbox read-only -C <repo> "评审 athena-gui 迁移到 Primer 的改动：对照 docs/architecture/2026-10-10-primer-gui-redesign-design.md 检查遗漏、可访问性与回归风险"`，把结论与处理写入完成报告。
- [ ] **Step 6: 收尾**：写 `docs/codex_docs/2026-10-10-primer-gui-redesign-completion.md`（含与 spec 的差异清单、测试数、体积对比、截图结论、Codex 评审处理）；spec 状态改为「已实施」；删除本计划；更新 `codex_docs/CURRENT.md` 指向完成报告。

