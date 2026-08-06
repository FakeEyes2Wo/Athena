# Athena 目标架构

Status: approved
Owner: Athena maintainers
Last verified: 2026-07-31
Source of truth: `architecture/current.md` and `architecture/research-runtime-v2.md`

## 原则

Athena 采用渐进归一的模块化单体。每个概念只有一个事实源；兼容模块只能重导出；Agent 负责不确定推理，生命周期、预算、执行、比较与持久化由确定性服务负责。

```text
Research control  athena.research.ResearchRuntime
Policy            Supervisor + experiment.ranking + research.budget.BudgetSnapshot
Execution         CodeAgent + GitWorkBranch + protected evaluator
Facts             core.research_tree.ResearchTree v2 + core.contracts.ArtifactRef
Thread control    athena.app_server (保持独立，不承载研究状态)
Adapters          gui_gateway + Tauri + React
```

## 已达成

- ResearchTree v2 是唯一实验事实源，只加载 `version: 2`。
- PREPARE、SEARCH、VALIDATE、REPORT 共用 PENDING -> RUNNING -> terminal 契约。
- 代码执行失败不再伪造零指标或候选胜出。
- Ranking 与 SearchLoop 各只有一个业务实现。
- VALIDATE 记录真实消融与 final-test 实验；REPORT 只由树证据生成。
- 独立 ResearchRuntime 持有 phase、task、budget、run task、暂停门与订阅。
- IDE 不直接变更研究树；桌面端只消费 v2 图。
- Brainstorm 到 Ideator 的迁移已完成：旧 package 与 wrapper 已删除；`Ideator.generate()` 返回含 hypotheses、transcript、failures 与 audit artifact 的 `DebateResult`，IdeaGeneration 消费该结果、登记并仅返回原样 hypotheses 列表，列表中的 `evidence_refs` 保留 audit 引用。

## 后续边界

- app-server 继续只处理 Thread/Turn 协议，不迁入研究状态。
- app-server 独立拥有 Thread/Turn 生命周期；`athena.ideator.Ideator` 独立拥有多 Agent debate policy，不接管协议生命周期。
- 实际数据准备、workspace 与 Agent 组合由应用 composition root 注入 ResearchRuntime；缺失注入必须失败。
- ELO 风格先进度排序须在 rubric、pair construction、cold start 与 confidence calibration 完整批准后方可替换当前稳定策略。
- 任何新增 schema 版本都必须显式设计，不得静默兼容旧快照。

## 完成定义

- 一个概念只有一个规范类型与业务实现。
- 成功状态必须有真实、可追溯证据。
- UI 不可直接写研究事实。
- 兼容路径只重导出或适配。
- 文档中的已实现项均能指向源码和自动化测试。
