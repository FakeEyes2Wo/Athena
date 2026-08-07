# Athena 文档索引

Status: current
Owner: Athena maintainers
Last verified: 2026-08-07
Source of truth: `src/athena/`, `src/gui_gateway/`, `athena-gui/`, `tests/`, `test/unit/`

## 首读

| 文档 | 状态 | 用途 |
|---|---|---|
| [当前工程与目录](architecture/current.md) | current | 当前 owner、运行链与已验证边界 |
| [目标架构](architecture/target.md) | approved | 依赖方向、长期约束与后续边界 |
| [Research Runtime v2](architecture/research-runtime-v2.md) | approved | 研究图、工作流与运行时设计 |
| [ResearchTree v2 设计](design_research_tree.md) | current | v2 schema、生命周期与持久化 |
| [可运行研究工作流](../README.md#run-the-complete-agent-workflow) | current | `src/main.py` 安装、认证、CLI 参数、产物和失败语义 |
| [测试指南](operations/testing.md) | current | 本地测试层次与验收命令 |
| [Memory 设计](memory_design.md) | current | Thread 上下文、压缩与恢复 |

## 设计规格

| 文档 | 状态 | 用途 |
|---|---|---|
| [统一 Agent 内核设计](superpowers/specs/2026-08-04-unified-agent-kernel-design.md) | approved | 统一 Agent 控制面、Session/Turn 生命周期、公开能力与 Ideator 边界 |
| [统一 Agent 内核实现计划](superpowers/plans/2026-08-05-athena-unified-agent-kernel.md) | current | 内核核心（§1-§3 + §4.4/§4.5 契约）分步实现 |
| [完整研究工作流设计](superpowers/specs/2026-08-07-athena-main-workflow-design.md) | implemented | `src/main.py` 的 PREPARE → SEARCH → VALIDATE → REPORT 组合、后端和失败语义 |
| [完整研究工作流实现计划](superpowers/plans/2026-08-07-athena-main-workflow.md) | current | 数据冻结、Codex/Qoder 接入、实验提交、CLI 入口和验收步骤 |
| [可信实验执行设计](superpowers/specs/2026-08-07-athena-trusted-execution-design.md) | implemented | 冻结 EvalSpec、phase-scoped 数据、最终测试重放与 durable 证据 |
| [可信实验执行实现计划](superpowers/plans/2026-08-07-athena-trusted-execution.md) | current | 分步实现 trusted evaluator/runtime 与证据持久化 |
| [本地可信工作流收口设计](superpowers/specs/2026-08-07-athena-local-workflow-closeout-design.md) | approved | CLI 预检、本地模式、离线 E2E 契约与退出码收口 |

## 维护规则

Canonical model owners are `athena.core.contracts`,
`athena.core.thread_models`, `athena.core.research_models`,
`athena.research.models`, `athena.evaluation.types`, and `athena.data.types`.
Research graph facts live in `athena.core.research_tree`; workspace contracts
live in `athena.core.workspace`, with local implementation in
`athena.git_workspace`.

- 每个领域概念只能有一个规范 owner；兼容路径只能重导出或适配。
- “已实现”必须有源码与自动化测试证据。
- 未配置能力必须明确失败，不得返回占位成功。
- app-server 保持 Thread/Turn 边界；研究运行时位于 `athena.research`。
- 架构变化先更新目标，验证完成后再更新当前事实。
