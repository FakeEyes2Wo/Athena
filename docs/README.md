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

## Academic Survey

一句主题 → 一个可检索的论文语料库。四段流水线各有一篇工具文档，组合根与全链路合在一篇。

| 文档 | 状态 | 用途 |
|---|---|---|
| [全链路：组合根与流水线](academic_survey_ch.md) | current | 三个入口、环境变量、成本账与真机失败清单 |
| [PaperScout Agent](paper_scout_agent_ch.md) | current | 多轮 search/expand 检索、打分门槛纪律 |
| [PaperScout 复现](paper_scout_reproduction_ch.md) | current | 与论文口径的对照与偏差 |
| [paper_source 工具](paper_source_tool_ch.md) | current | 取源通道、版本固定与限流 |
| [paper_source 上游契约](paper_source_upstream_contract_ch.md) | current | 交给取源的 `PaperRef` 长什么样 |
| [paper_markdown 工具](paper_markdown_tool_ch.md) | current | TeX/PDF → 带图表解读的 Markdown |
| [paper_markdown 输出](paper_markdown_rag_output_ch.md) | current | chunk 与视觉单元的落盘形态 |
| [paper_markdown 质量](paper_markdown_rag_quality_ch.md) | current | 质量门禁判定与诊断码 |
| [paper_rag 工具](paper_rag_tool_ch.md) | current | 两个检索算子 + 三个遍历算子 + 整篇读取 |

## Kaggle 与 Supervisor 人类门

| 文档 | 状态 | 用途 |
|---|---|---|
| [Kaggle 接入与 Supervisor 人类门](kaggle_supervisor_gate_ch.md) | current | 下载/预算/VALIDATE 三个门、假设监控、每 Agent 最少 Kaggle 工具 |

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
