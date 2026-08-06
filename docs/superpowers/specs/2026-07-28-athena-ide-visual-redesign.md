# Athena IDE 视觉美化设计文档

> 日期：2026-07-28
> 状态：设计完成，待实施
> 视角：纯美术/视觉层 — 不改结构，只改视觉呈现
> 关联文档：`docs/superpowers/specs/2026-07-28-athena-ide-frontend-redesign.md`

## 1. 目标

以 Codex 式浅色工具美学为基线，对 Athena IDE 前端进行全覆盖视觉美化。不改组件结构和数据流，仅在 CSS/设计 token 层面重新定义颜色、字体、间距、圆角、阴影和动效。

## 2. 视觉基调

- **风格**：Codex Canvas — 浅色幕布 + 白色卡片 + 克制冷蓝强调
- **气质**：工具感为主（Geist 全系 + 紧凑排版），数据区注入一丝学术仪式感（Lora 衬线暂不启用，保留为未来可选升级，当前全系 Geist 保证工具感纯度）
- **原则**：强调色只在 5% 的场景出现；层级靠灰度和间距区分，不靠喊；阴影只用两层，反对重阴影

## 3. 颜色系统

### 3.1 设计 Token

| Token | 值 | HSB | 用途 |
|---|---|---|---|
| `--bg` | `#F7F8FA` | 220°, 2%, 98% | 全局背景（微灰幕布） |
| `--surface` | `#FFFFFF` | — | 所有面板/卡片 |
| `--surface-elevated` | `#F1F3F6` | 220°, 3%, 96% | hover 态 |
| `--surface-muted` | `#F0F2F5` | 218°, 3%, 95% | 输入框底色 |
| `--border` | `rgba(0,0,0,0.07)` | — | 默认边框 |
| `--border-hover` | `rgba(37,99,235,0.18)` | — | hover 边框 |
| `--border-focus` | `rgba(37,99,235,0.35)` | — | 聚焦边框 |
| `--accent` | `#2563EB` | 218°, 84%, 92% | 强调色（按钮/link） |
| `--accent-hover` | `#1D4ED8` | — | 按压态 |
| `--accent-subtle` | `rgba(37,99,235,0.06)` | — | 选中背景 |
| `--text-primary` | `#111318` | 228°, 21%, 9% | 标题/正文 |
| `--text-secondary` | `#6B7280` | 220°, 9%, 50% | 说明文字 |
| `--text-tertiary` | `#9CA3AF` | 217°, 8%, 69% | 禁用/提示 |
| `--success` | `#059669` | 168°, 97%, 59% | 指标上涨 |
| `--warning` | `#D97706` | 33°, 97%, 53% | 预算预警 |
| `--danger` | `#DC2626` | 0°, 83%, 86% | 失败/错误 |
| `--shadow-sm` | `0 1px 3px rgba(0,0,0,0.04)` | — | 面板默认阴影 |
| `--shadow-md` | `0 4px 16px rgba(0,0,0,0.06)` | — | 弹出层阴影 |

### 3.2 图表色板

Viridis 衍生 6 色（数据区/图表使用，保持学术感）：
`#2563EB` / `#059669` / `#D97706` / `#7C3AED` / `#DB2777` / `#0891B2`

## 4. 字体系统

### 4.1 字体选型

| 角色 | 字体 | 来源 |
|---|---|---|
| 主字体 | **Geist** | Vercel (OFL)，替代 Inter |
| 等宽 | **Geist Mono** | Vercel (OFL) |

### 4.2 字阶

| 层级 | 字体 | 大小/行高 | 字重 | 用途 |
|---|---|---|---|---|
| H1 | Geist | 20px / 1.3 | 700 | 页面标题 |
| H2 | Geist | 15px / 1.4 | 600 | 面板标题 |
| H3 | Geist | 13px / 1.4 | 600 | 卡片标题 |
| Body-conversation | Geist | 14px / 1.55 | 400 | 对话正文 |
| Body-system | Geist | 13px / 1.5 | 400 | 系统消息 |
| Caption | Geist | 12px / 1.4 | 400 | 辅助文字 |
| Micro-label | Geist | 11px / 1.3 | 500 | 微标签 (uppercase/spaced) |
| Code/Metrics | Geist Mono | 13px / 1.6 | 400 | 代码和数值 |

### 4.3 侧边栏字体

| 元素 | 字体 | 大小/行高 | 字重 |
|---|---|---|---|
| 会话列表项 | Geist | 13px / 1.4 | 500 (active) / 400 (default) |
| 分组标签 | Geist | 11px / 1.3 | 600, letter-spacing: +0.05em |
| 新建按钮 | Geist | 12px / 1.4 | 400 |

### 4.4 字重策略

仅使用 400 / 500 / 600 / 700 四个字重。字重变化即信息层级，不靠字号猛跳。

## 5. 间距与圆角

### 5.1 间距 Token

| Token | 值 | 用途 |
|---|---|---|
| `space-xs` | 4px | 图标与文字紧贴 |
| `space-sm` | 8px | 卡片内同类元素间距 |
| `space-md` | 12px | 卡片内不同区块间距 |
| `space-lg` | 16px | 面板内边距、组件间标准间距 |
| `space-xl` | 20px | 外壳 padding、区域分隔 |
| `space-2xl` | 24px | 大区块分隔、header-bottom |
| `space-3xl` | 32px | 页面级分隔 |

### 5.2 各区域间距

| 区域 | 内边距 | 内部间距 |
|---|---|---|
| 侧边栏 | `12px` | 列表项间距 `4px` |
| 对话区消息列表 | `py: 16px, px: 24px` | 气泡间距 `12px` |
| 输入框区域 | `12px 24px 20px` | — |
| 右栏卡片 | `12px 14px` | 卡片间距 `8px` |
| 底部详情层 | `16px 20px` | — |
| 消息气泡 | `12px 16px` | — |

### 5.3 圆角策略

| 元素 | 圆角 | 理由 |
|---|---|---|
| 大面板 | `16px` | 柔和包裹 |
| 卡片 | `12px` | 比面板更紧，区分层级 |
| 按钮 | `10px` | 现代工具标准 |
| 输入框 | `10px` | 和按钮统一 |
| 消息气泡 | `14px` | 对话区更软 |
| 小标签/badge | `6px` | 紧凑微圆 |

全部偶数圆角，保证 retina 下 anti-alias 效果干净。

## 6. 组件视觉

### 6.1 AppShell — 整体布局

三列网格：`220px / 1fr / 280px`，列间距 `16px`，外壳 `padding: 16px 20px 0`。

全局背景 `#F7F8FA`，面板统一白底 + `1px solid rgba(0,0,0,0.07)` + `box-shadow: 0 1px 3px rgba(0,0,0,0.04)` + `border-radius: 16px`。

### 6.2 TopBar

- 高度 `44px`，`padding: 8px 20px`
- 左侧：品牌标记 `#2563EB` + 标题 `14px / 600`
- 右侧：连接灯 `6px` 绿点 + phase 名 `12px / #6B7280`
- 底部 `1px solid rgba(0,0,0,0.06)` 分隔，不加阴影

### 6.3 会话侧边栏

- Header: `11px / 600` 标签 "会话"，右侧 `[+ 新建]` 按钮
- 列表项 `padding: 10px 12px`，`border-radius: 10px`，间距 `4px`
- **激活态**: `color: #2563EB` + `background: rgba(37,99,235,0.06)`
- **默认态**: `color: #6B7280`，透明底
- **hover**: `background: #F1F3F6`，左侧浮现 `3px` 蓝色细条
- "新建"按钮：`dashed` 边框，`12px`，`color: #6B7280`

### 6.4 对话区

- Header `padding: 20px 24px 12px`，底部 1px 分隔
  - 标题 "Athena" `20px / 700`
  - 副标题 `13px / #6B7280`
- **用户气泡**: `background: rgba(37,99,235,0.05)` + `border: 1px solid rgba(37,99,235,0.12)` + `border-radius: 14px`
- **Athena 气泡**: `background: #F0F2F5` + `border: 1px solid rgba(0,0,0,0.06)` + `border-radius: 14px`
- **意图预览卡**: 白色底 + `border: 1px solid rgba(37,99,235,0.14)`，右侧蓝色确认按钮
- **输入框**: `background: #F0F2F5`，无默认边框，`border-radius: 10px`，聚焦时 `border: 1px solid rgba(37,99,235,0.35)`
- **发送按钮**: `background: #2563EB, color: #FFF, border-radius: 10px, height: 34px`

### 6.5 右栏摘要卡

- `padding: 12px 14px`，`border-radius: 12px`，间距 `8px`
- 三行信息：微标签 `11px/600` / 数值 `15px/600` / 辅助 `12px/#6B7280`
- hover: `translateY(-1px)` + `border-color: rgba(37,99,235,0.25)` + `background: #F1F3F6`
- 整卡可点击，打开底部详情层

### 6.6 底部详情层

- **收起态**: `padding: 12px 20px`，`color: #9CA3AF`
- **展开态**: `padding: 16px 20px`，`min-height: 240px`，`max-height: 42vh`，overflow-y: auto
- 顶部 `2px solid rgba(37,99,235,0.10)` 分隔
- Header: 标题 `16px / 600` + 关闭按钮 `12px`

### 6.7 按钮体系

| 类型 | 样式 | 用途 |
|---|---|---|
| Primary | `bg: #2563EB, color: #FFF, radius: 10px, h: 34px` | 发送/确认/开始 |
| Secondary | `bg: transparent, border: 1px rgba(0,0,0,0.10), radius: 10px` | 取消/关闭 |
| Ghost | `bg: transparent, hover bg: #F1F3F6` | 侧边栏项/图标按钮 |
| Danger | `color: #DC2626, border: 1px rgba(220,38,38,0.15)` | 停止/删除 |

## 7. 动效

只使用克制动效，禁止弹跳、霓虹和重玻璃拟态：

| 动效 | 时长 | 缓动 | 场景 |
|---|---|---|---|
| hover 色变 | `160ms` | ease | 按钮、卡片、列表项 |
| hover 位移 | `160ms` | ease | 卡片 `translateY(-1px)` |
| 详情层展开 | `240ms` | `cubic-bezier(0.4, 0, 0.2, 1)` | ContextSurface 开合 |
| 聚焦边框 | `200ms` | ease | 输入框聚焦 |

## 8. 落地说明

### 8.1 实施范围

- 仅改 CSS 和设计 token（`styles.css` / `App.css` / 各组件 CSS）
- 可选：引入 Geist 字体（`@fontsource/geist` / `@fontsource/geist-mono` 或 CDN）
- 不改组件结构、数据流、TypeScript 类型
- 不改 `PythonBridge`、WebSocket 协议、后端逻辑

### 8.2 字体引入方案

```css
/* 方案 A: npm 包 (推荐) */
@import '@fontsource/geist';
@import '@fontsource/geist-mono';

/* 方案 B: CDN (备选) */
/* @import url('https://cdn.jsdelivr.net/npm/@fontsource/geist@latest/index.css'); */
```

### 8.3 不影响的功能

- Tauri 桌面壳
- WebSocket 实时通信
- Monaco Editor 代码区
- usePipeline / useEvents hooks
- IDE 后端消息协议

## 9. 验收标准

- [ ] 全局背景为微灰 `#F7F8FA`，面板为纯白卡片浮在灰底上
- [ ] Geist 字体已启用，无 Inter/系统字体回退
- [ ] 强调色仅出现在主按钮、链接、选中态、聚焦态
- [ ] 所有面板圆角 16px，卡片 12px，按钮 10px
- [ ] 侧边栏激活项为蓝字浅蓝底，无额外装饰
- [ ] 右栏卡 hover 有 1px 上浮 + 边框微变色
- [ ] 底部详情层展开/收起流畅，无卡顿
- [ ] 图表使用 Viridis 衍生 6 色
- [ ] 对话框整体视觉中心在对话区，右栏明显更窄
- [ ] 无重阴影、无霓虹、无弹跳动效
