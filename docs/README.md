# Athena 文档索引

Status: current
Owner: Athena maintainers
Last verified: 2026-08-10
Source of truth: `src/athena/`, `src/gui_gateway/`, `athena-gui/`, `tests/`, `test/unit/`

## 首读

| 文档 | 状态 | 用途 |
|---|---|---|
| [Supervisor 设计基线](supervisor_design.md) | current | 目标架构：研究流程、控制面、数据隔离与验收门（最终产品合同） |
| [Supervisor 实现计划](supervisor_imp_docs.md) | current | 实现状态、任务拆分、全局约束与冻结验证命令 |
| [代码规范](代码规范.md) | current | 导入/注释/异常/格式/测试约定 |
| [可运行研究工作流](../README.md#run-the-complete-agent-workflow) | current | `src/main.py` 安装、认证、CLI 参数、产物和失败语义 |
| [测试指南](operations/testing.md) | current | 本地测试层次与验收命令 |
| [Core 工具设计](architecture/2026-08-09-core-tool-simplification-design.md) | current | Core 工具极简改造设计（历史） |
| [Prompt 驱动 Agents 设计](architecture/2026-08-09-prompt-driven-agents-design.md) | current | Prompt 驱动 Agents 改造设计（历史） |

## 维护规则

Canonical model owners are `athena.core.contracts`,
`athena.core.thread_models`, `athena.core.research_models`, and
`athena.research.models`. Data-agent models live in `athena.agents.data_models`.
Research graph facts live in `athena.core.research_tree`; workspace contracts
live in `athena.core.workspace`, with local implementation in
`athena.core.git_workspace`.

- 每个领域概念只能有一个规范 owner；兼容路径只能重导出或适配。
- “已实现”必须有源码与自动化测试证据。
- 未配置能力必须明确失败，不得返回占位成功。
- app-server 保持 Thread/Turn 边界；研究运行时位于 `athena.research`。
- 架构变化先更新目标，验证完成后再更新当前事实。
