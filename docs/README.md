# Athena 文档索引

Status: current
Owner: Athena maintainers
Last verified: 2026-08-08
Source of truth: `src/athena/`, `src/gui_gateway/`, `athena-gui/`, `tests/`, `test/unit/`

## 首读

| 文档 | 状态 | 用途 |
|---|---|---|
| [当前工程与目录](architecture/current.md) | current | 当前 owner、运行链与已验证边界 |
| [目标架构](architecture/target.md) | approved | 依赖方向、长期约束与后续边界 |
| [Research Runtime v2](architecture/research-runtime-v2.md) | approved | 研究图、工作流与运行时设计 |
| [可运行研究工作流](../README.md#run-the-complete-agent-workflow) | current | `src/main.py` 安装、认证、CLI 参数、产物和失败语义 |
| [测试指南](operations/testing.md) | current | 本地测试层次与验收命令 |

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
