# Athena IDE Frontend Redesign Design

> 日期：2026-07-28
> 状态：设计完成，待实施
> 关联文档：`docs/superpowers/specs/2026-07-28-athena-ide-design.md`

## 1. 目标

将 Athena IDE 前端重构为 **Codex 式对话工作台**：以聊天为主入口，右侧保留窄幅运行摘要栏，底部通过按需展开的详情层承载 ResearchTree、Metrics、Diff、File Tree 和 Report 等重内容。

## 2. 核心原则

1. **聊天永远是主角**
2. **右栏只放摘要，不放复杂内容**
3. **复杂内容进入详情层**
4. **整体视觉极简高级黑**
5. **交互像 Codex，但多一层 ML 仪表盘能力**

## 3. 信息架构

### 3.1 主聊天区

主聊天区占据最大面积，承担：
- 自然语言输入
- 流式 token 输出
- 任务理解预览
- 参数确认卡片
- 运行动作卡片
- 实验结果卡片

聊天区不是普通消息列表，而是 Athena 的主控制台。

### 3.2 右侧窄摘要栏

右栏保持常驻，但宽度受控，不得抢占主视觉。

默认仅显示以下摘要：
- 当前 phase / running 状态
- Budget Snapshot
- 当前 best result
- ResearchTree 摘要入口

右栏不直接展示完整图表、完整 diff 或完整 tree。

### 3.3 底部详情层

复杂内容统一进入底部详情层（context surface）：
- ResearchTree 详情
- Metrics 图表
- Experiment log
- Diff viewer
- File tree
- Report preview

详情层默认收起，通过点击右栏摘要或聊天中的结构化卡片展开。

## 4. 组件重组

### 4.1 顶层组件

- `AppShell`
  - 负责整体布局装配
- `ConversationPane`
  - 负责聊天流、输入框、流式输出、动作卡片
- `RightRail`
  - 负责窄摘要栏
- `ContextSurface`
  - 负责底部详情层
- `TopBar`
  - 负责轻量标题和连接状态
- `OverlaySurface`
  - 负责少数聚焦型弹层

### 4.2 聊天子组件

- `MessageList`
- `MessageBubble`
- `StreamingMessage`
- `Composer`
- `ActionCard`
- `IntentPreviewCard`
- `SearchRunCard`
- `ResultSummaryCard`
- `ErrorCard`

### 4.3 右栏子组件

- `StatusCard`
- `BudgetCard`
- `BestResultCard`
- `ResearchSnapshotCard`
- `QuickActionsCard`（可选）

### 4.4 详情层子组件

- `ContextHeader`
- `ContextTabs`
- `ContextBody`
- `MetricsPanel`
- `ResearchTreePanel`
- `ExperimentLogPanel`
- `DiffPanel`
- `FileTreePanel`
- `ReportPanel`

## 5. 视觉系统

### 5.1 风格

- 极简高级黑
- 低饱和度
- 细边框
- 轻阴影
- 低噪声层级

### 5.2 颜色策略

- 页面背景：深黑
- 面板背景：深灰黑
- 强调色：单一冷色系（冷蓝或青绿）
- 不使用多套主题色
- 不使用大面积霓虹风装饰

### 5.3 卡片语言

统一 4 类卡片：
- Info Card
- Action Card
- Result Card
- Warning Card

### 5.4 动效

只使用克制动效：
- fade / translate
- expand / collapse
- bottom sheet 上滑
- 状态轻微渐变

禁止夸张弹跳、霓虹闪烁和重玻璃拟态。

## 6. 状态流转

### 6.1 默认启动

启动后默认展示：
- 聊天主区
- 右侧窄摘要栏
- 底部详情层收起

### 6.2 任务发起

用户输入自然语言任务后：
1. 聊天区显示意图解析结果
2. 必要时插入参数确认卡
3. 用户确认后进入运行态

### 6.3 运行中

运行过程中：
- 聊天区持续流式更新
- 右栏同步刷新 phase / budget / best result
- 详情层默认不打断主流程

### 6.4 查看详情

点击右栏摘要或结果卡后：
- 底部详情层展开
- 自动定位到相关面板
- 关闭后回到聊天主视图

### 6.5 暂停 / 恢复 / 终止

- Pause：当前实验完成后暂停
- Resume：从暂停处继续
- Stop：优雅终止并保留结果

### 6.6 错误

错误必须以可见状态呈现：
- 聊天流插入错误卡
- 右栏同步状态变化
- 必要时自动切换详情层到错误相关内容

## 7. 测试与验收

### 7.1 视觉验收

- 聊天区是视觉中心
- 右栏明显更窄
- 界面整体呈极简高级黑
- 没有 demo 感

### 7.2 结构验收

- 主界面由聊天区、右栏、详情层组成
- 复杂内容不直接堆在右栏
- 右栏只显示摘要入口

### 7.3 交互验收

- 任务输入后出现意图预览
- 确认后可启动运行
- 运行状态实时更新
- 点击摘要可展开详情层
- 暂停 / 恢复 / 终止状态明确

### 7.4 测试范围

- 聊天消息渲染
- 流式输出渲染
- 摘要卡片渲染
- 详情层展开 / 收起
- 状态映射
- 主流程交互

## 8. 落地范围

本次 redesign 仅改前端体验层，不改：
- Python IDE backend
- Rust `PythonBridge`
- WebSocket 协议
- AI4ML pipeline 逻辑

## 9. 最终定义

Athena 新前端 = **Codex 式对话工作台** + **窄幅运行摘要栏** + **按需展开的 ML 详情层** + **极简高级黑视觉系统**
