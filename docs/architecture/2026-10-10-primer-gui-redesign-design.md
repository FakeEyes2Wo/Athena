# Athena GUI 迁移到 GitHub Primer 设计系统

> 状态：已实施（见 docs/codex_docs/2026-10-10-primer-gui-redesign-completion.md）
> 日期：2026-10-10
> 范围：`athena-gui/`（Web GUI 与 Tauri 桌面壳共用的前端）；不涉及 Python、Rust、网关协议

## 1. 背景与目标

Athena GUI 目前使用自建设计系统：`athena-gui/src/styles.css` 中的 token 与通用组件类（`.btn`、`.input`、`.badge`、`.card` 等），加上各组件的 CSS Module。自调配色缺少外部用户反馈，用户决定整体照搬一个经过大规模验证的成熟设计系统。

目标：前端视觉与组件全面迁移到 GitHub Primer（`@primer/react` + `@primer/primitives`），亮色用 Primer Light，暗色用 Primer Dark（GitHub 默认暗色），字体用 Primer 字体栈。迁移后界面中的颜色、字号、圆角、间距、控件尺寸与交互状态全部来自 Primer，不再维护自写色值。

## 2. 已确认的决策

| 决策 | 结论 |
|---|---|
| 照搬对象 | GitHub Primer（MIT 许可，组件与 token 均为官方发布包） |
| 实现方式 | 方案 C：改用 `@primer/react` 组件库（而非只引 token 或手抄色值） |
| 暗色主题 | Primer `dark`（GitHub 默认暗色）；亮色 Primer `light` |
| 主题切换 | 保持亮 / 暗两档，交互不变 |
| 字体 | Mona Sans（`@fontsource-variable/mona-sans`，OFL，随应用打包）+ Primer 等宽字体栈 |
| 布局 | 改为 GitHub 仓库页结构（见第 5 节），信息架构与模块划分不变 |
| 早先的 Tokyo Night 改动 | 未提交，整体由本方案替换 |

## 3. 范围

### 做

- 依赖：新增 `@primer/react`（38.x）、`@primer/primitives`（11.x）、`@primer/octicons-react`（19.x）、`@fontsource-variable/mona-sans`；移除 `lucide-react`、`@fontsource/geist`、`@fontsource/geist-mono`。
- 根部接入 `ThemeProvider` + `BaseStyles` 与 Primer 主题 CSS；`useTheme` 驱动 Primer 的 color mode。
- 外壳按 GitHub 仓库页重排；所有界面改用 Primer 组件（映射见第 6 节）。
- 删除 `styles.css` 中的自建设计系统与不再引用的 CSS Module；保留的业务样式只引用 Primer CSS 变量。
- Recharts、ReactFlow、react-markdown 改用 Primer token 着色。
- 测试随组件调整；新增行为补正常与异常路径测试。

### 不做

- 不改网关协议（`SUPPORTED_METHODS`）、`usePipeline` 等 hooks、数据流与业务逻辑。
- 不改模块划分：会话、研究树、实验、数据分析、模型轨迹、报告、设置。
- 不新增功能（如 Diff 行着色、代码语法高亮、日志级别）——这些是独立需求。
- 不使用 `@primer/react/experimental` 与 `@primer/react/deprecated` 入口的组件。
- 不处理 `monaco-editor` / `@monaco-editor/react`：两者在依赖中但源码无任何 import（仅 `components/context/panels.ts` 注释提及），属于另一项清理。

## 4. 架构

### 4.1 根部接入

```
main.tsx
  └─ ThemeProvider colorMode={day|night} dayScheme="light" nightScheme="dark"
       └─ BaseStyles
            └─ App
```

- 引入的 Primer CSS 仅限：`@primer/primitives` 的 `functional/themes/light.css`、`functional/themes/dark.css`、`base` 与 `functional` 下的 size / typography，以及 `@primer/react` 自身组件样式。不引入其余 12 个主题变体。
- `useTheme`：对外接口（`mode`、`cycleMode`）与 localStorage key `athena-theme` 不变，已保存的用户偏好继续生效；内部改为驱动 `ThemeProvider` 的 `colorMode`，并在 `document.documentElement` 上同步 Primer 需要的 `data-color-mode` / `data-light-theme` / `data-dark-theme`，保证 Portal 渲染的浮层（Dialog、Tooltip、ActionMenu）拿到同一主题变量。首屏在 React 挂载前应用主题，避免闪烁（沿用现有做法）。

### 4.2 样式层

- `styles.css` 删除全部自建 token 与通用组件类，仅保留 Primer 不覆盖的全局规则（如 `html/body/#root` 高度、ReactFlow 容器尺寸），且只引用 Primer 变量（`--bgColor-*`、`--fgColor-*`、`--borderColor-*`、`--borderRadius-*`、`--base-size-*`、`--text-*`、`--fontStack-*`）。
- 组件 CSS Module 保留业务专属样式（消息气泡、工具调用块、Ideator 并行车道、图节点等），全部改为引用 Primer 变量；不再出现十六进制或 rgba 字面色值。
- 验收时以 grep 检查：`athena-gui/src` 下 CSS 与 TSX 中无十六进制 / `rgba(` 色值，无对已删除旧 token（`--accent`、`--surface` 等）的引用。

### 4.3 图标

`components/common/Icon.tsx` 保持 `IconName` 与 `<Icon name size />` 接口，内部由 lucide 换为 `@primer/octicons-react` 的名称映射；调用方不改。无 Octicon 对应的名称在映射表中显式选定最接近的图标，不允许运行时缺图标。

## 5. 外壳布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ ☰  ◆ Athena  /  <工作区>  /  <会话>                ● 状态   ⌨  ◐  ⚙   │ 顶栏
│ 会话   研究树 (n)   实验 (n)   数据分析   模型轨迹   报告               │ UnderlineNav
├───────────────┬──────────────────────────────────────┬───────────────┤
│ NavList       │                                      │ 详情 / 日志    │
│ 会话列表 或    │             主内容区                  │ 非模态侧栏     │
│ 详情视图入口   │                                      │ （按需打开）   │
└───────────────┴──────────────────────────────────────┴───────────────┘
```

1. 顶栏（仿 GitHub 仓库页头）：折叠按钮、Athena 标识、「工作区 / 会话」路径；右侧运行状态 `Label`，日志、主题、设置三个 `IconButton`（均带 `aria-label`）。设置入口由原左侧功能轨移到此处。
2. 模块导航：原 `FunctionRail` 换为顶栏下的 `UnderlineNav`；研究树、实验显示 `CounterLabel`。
3. 左侧栏：`NavList`。会话模块显示工作区与会话列表（新建、删除）；其他模块显示 `RELATED_PANELS` 的详情视图入口。可折叠。
4. 右侧详情（`ContextDrawer`）与日志（`LogDrawer`）保持非模态侧栏，不遮挡主区；标题栏用 `Heading` + `IconButton`。不用 Primer 侧滑 `Dialog`（其为模态，会改变现有交互）。
5. Primer `PageLayout` / `SplitPageLayout` 只支持一个 Pane，而外壳需要左右两侧栏，因此三栏网格自行实现，网格内部件全部使用 Primer 组件与变量。

`navigation.ts` 的 `MODULES` / `RELATED_PANELS` 数据结构保留，作为 `UnderlineNav` 与 `NavList` 的数据源。

## 6. 组件映射

### 6.1 通用部件

| 现状 | Primer |
|---|---|
| `.btn` / `--primary` / `--subtle` / `--ghost` / `--sm` | `Button`（`variant` primary / default / invisible / danger，`size="small"`）；纯图标用 `IconButton` |
| `.input` / `.select` / `.field` | `TextInput` / `Select` / `Textarea` + `FormControl`（`.Label`、`.Caption`、`.Validation`） |
| `.switch` | `ToggleSwitch` |
| `.card--error` | `Flash variant="danger"` |
| `.card` 容器 | 带 `--borderColor-default` 边框、`--borderRadius-medium` 圆角的容器（CSS Module） |
| `.badge--*`、`StatusBadge` | `Label` |
| `.panel-toolbar` + `h3` | `PageHeader`（标题区 + Actions） |
| `.table` | 原生 `<table>` + Primer 变量样式（`DataTable` 仅在 experimental，不用） |
| `.kv` | `<dl>`，标签 `--fgColor-muted` |
| `.timeline` | `Timeline` |
| `.code-block` / `<pre>` | `--bgColor-muted` 底 + `--fontStack-monospace` |
| `EmptyState` | 按 Blankslate 规格自行实现（`Blankslate` 仅在 experimental，不用） |
| `.icon--spin` | `Spinner` |
| `<details>` | 原生 `details` + Primer 变量样式 |
| `title=` 提示 | `Tooltip` |
| 布局容器 | `Stack`（v38 已移除 `Box`） |

### 6.2 假设状态到 Label 的映射

| HypothesisStatus | `Label` variant |
|---|---|
| SUPPORTED | `success` |
| REFUTED | `danger` |
| PROPOSED | `accent` |
| REJECTED | `secondary` |
| INCONCLUSIVE | `attention` |
| 未知值 | `secondary`，文本照原样显示 |

此映射集中定义在一处（替代 `ResearchTreeViz.tsx` 现有的 `STATUS_STYLES`），研究树、假设图、待选假设、实验列表共用。

### 6.3 各界面

| 界面 | 要点 |
|---|---|
| 工作区选择（`WorkspacePicker`） | 居中卡片；最近使用列表 `ActionList`；路径 `TextInput` + `Button`；连接失败 `Flash` |
| 会话（`ConversationPane` 等） | 消息气泡、工具调用、Ideator 车道参照 GitHub 评论框（边框 + 标题栏）自写，全部用 Primer 变量；`RunControls` 自动/手动用 `SegmentedControl`，暂停/继续/停止用 `Button`（停止 `variant="danger"`）；Composer 用 `Textarea` + 主按钮；欢迎页示例提示用 `Button`；`IntentPreviewCard`、`PendingHypotheses` 改 Primer 表单控件 + `Label` |
| 设置（`SettingsPanel`） | `Dialog`（页头、可滚动主体、页脚按钮）+ `FormControl`；顺带消除现有页脚压住表单的布局问题 |
| 人工确认（`HumanRequestDialog`） | `Dialog`；选项用 `ActionList` 单选；补充回答 `TextInput`；错误 `Flash` |
| 实验管理（`ExperimentManager`） | 原生表格（Primer 样式）、筛选 `Select`、状态 `Label` |
| 实验日志（`ExperimentLog`） | `Timeline` |
| 模型轨迹（`LLMIOPanel`） | 左侧 `ActionList`（单选高亮），右侧评论框样式消息块 |
| 报告（`ReportPanel`） | 统计卡容器，数值用等宽字体 |
| 文件（`FileTree`）/ 差异（`DiffViewer`）/ 数据分析（`EDAPreview`） | 目录用 `TreeView`；键值 `<dl>`；Markdown 用 GitHub markdown 排版 |
| 图算法（`AlgorithmsPanel`） | `FormControl` + `Select` / `TextInput`；结果代码块 |
| 日志侧栏（`LogDrawer`） | 条目列表 + `Label` 区分 state / output |

## 7. 第三方可视化

- Recharts（`MetricChart`）：网格 `--borderColor-muted`，曲线用 Primer 数据色（`--data-blue-color-emphasis` 等），SOTA 点 `--fgColor-success`；tooltip 用 `--overlay-bgColor` 与 `--shadow-floating-small`。
- ReactFlow（`ResearchTreeViz`、`HypothesisGraphViz`）：节点为 GitHub 卡片（`--bgColor-default`、`--borderColor-default`、`--borderRadius-medium`），节点内放状态 `Label` 并以状态色左边框区分；背景点阵、Controls、MiniMap 用 Primer 变量。
- react-markdown：GitHub markdown 排版（标题层级、表格、代码块、引用），只引用 Primer 变量。

## 8. 迁移顺序

每一步结束时应用可运行、`npm test` 与 `npm run build` 通过，不做一次性替换。

1. 接入：依赖、`ThemeProvider` / `BaseStyles` / 主题 CSS、`useTheme`、`Icon`（Octicons）。
2. 外壳：顶栏、`UnderlineNav`、`NavList` 左侧栏、右侧非模态侧栏。
3. 会话：消息列表、Composer、RunControls、确认卡、待选假设、欢迎页。
4. 弹窗：设置、人工确认。
5. 数据面板：实验、日志、模型轨迹、报告、文件、差异、数据分析、图算法、日志侧栏。
6. 可视化：Recharts、ReactFlow、Markdown。
7. 清理：删除旧设计系统与无引用 CSS Module，移除 lucide 与 Geist 依赖。

## 9. 测试与验收

### 测试

- 现有 20 个测试文件以 `getByRole` / `getByText` 为主，随组件调整查询；唯一按 class 断言的 `toHaveClass("icon-btn")` 改为按角色与可访问名断言。
- 新行为测试（正常路径 + 异常/降级路径）：
  - `useTheme`：切换后 `ThemeProvider` color mode 与 `document.documentElement` 上的 Primer 属性同步；localStorage 中无值或值非法时回落到系统偏好。
  - 状态映射：五种 `HypothesisStatus` 对应正确 `Label` variant；未知状态回落 `secondary` 且不抛错。
  - `Icon`：每个 `IconName` 都能渲染出 SVG；不存在的名称在类型层面被拒绝。
- `scripts/dev.test.cjs` 是 `node:test` 文件，被 Vitest 默认匹配误收导致 `npm test` 报「No test suite found」，与本次改动无关，但会掩盖真实失败；实施时在 `vitest.config.ts` 中把它排除出 Vitest 范围（不改该文件本身）。

### 验收标准

1. `npm test`（athena-gui）全部通过，无跳过、无未解释失败。
2. `npm run build`（含 `tsc`）通过。
3. `athena-gui/src` 下无十六进制 / `rgba(` 字面色值，无对已删除旧 token 的引用；`package.json` 中无 `lucide-react`、`@fontsource/geist*`。
4. 实机：亮、暗两种主题下，工作区选择、会话（含运行控制与确认卡）、设置弹窗、人工确认弹窗、研究树、实验、实验日志、模型轨迹、报告、数据分析、日志侧栏逐屏截图核对，无白底残留、无文字不可读、无布局溢出。
5. 键盘：Tab 能到达顶栏、模块导航、左侧栏、主区控件与弹窗；弹窗打开时焦点受限、Esc 关闭（人工确认弹窗沿用 `allow_skip` 规则）；焦点环可见。
6. 记录改版前后打包体积对比（见附录）。
7. Codex 独立评审一次，结论与处理记入完成报告。

## 10. 风险

| 风险 | 应对 |
|---|---|
| 所需组件只在 experimental 入口 | 只用稳定入口；`DataTable`、`Blankslate`、`InlineMessage` 已确认仅在 experimental，按第 6.1 节替代方案实现 |
| Portal 浮层主题不一致 | 主题属性同步到 `document.documentElement`（第 4.1 节） |
| 体积增加 | 桌面应用不敏感；只引入两个主题 CSS；记录前后对比 |
| 中文排版 | Mona Sans 无中文字形，中文回落系统字体（与现状一致）；核对中英混排行高 |
| 测试大面积改动掩盖回归 | 每步只改该步涉及的测试；行为断言（角色、可访问名、回调）保持不变，只改查询方式 |

## 附录：改版前基线

`npm run build` 于 2026-10-10 在当前工作区执行（含未提交的 Tokyo Night CSS 改动，仅 CSS，对体积影响可忽略）：

| 产物 | 体积 |
|---|---|
| JS 合计 | 1009.5 kB |
| CSS 合计 | 112.5 kB |
| `dist` 合计（含字体） | 1409.7 kB |
| 最大分块 | `MetricChart` 352.6 kB（gzip 102.6 kB）、`index` 262.1 kB（gzip 82.0 kB）、`ResearchTreeViz` 238.7 kB（gzip 77.9 kB） |

Primer 版本核对（2026-10-10，npm）：`@primer/react` 38.40.1（peer：React 18.x || 19.x）、`@primer/primitives` 11.10.0、`@primer/octicons-react` 19.40.0、`@fontsource-variable/mona-sans` 5.3.0。
