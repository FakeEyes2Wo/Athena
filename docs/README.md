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
| [研究核心机制整理](research_core_mechanisms_ch.md) | current | ResearchTree / SEARCH 搜索 / IdeaGenerator / Hypothesis 排序 / 动态 EDA |
| [代码规范](代码规范.md) | current | 导入/注释/异常/格式/测试约定 |
| [可运行研究工作流](../README.md#运行) | current | `Athena-cli` 安装、认证、CLI 参数、产物和失败语义 |
| [测试指南](operations/testing.md) | current | 本地测试层次与验收命令 |
| [Core 工具设计](architecture/2026-08-09-core-tool-simplification-design.md) | current | Core 工具极简改造设计（历史） |
| [Prompt 驱动 Agents 设计](architecture/2026-08-09-prompt-driven-agents-design.md) | current | Prompt 驱动 Agents 改造设计（历史） |

## AutoResearch 设计（2026-08-15）

| 文档 | 状态 | 用途 |
|---|---|---|
| [AutoResearch 设计目录](autoresearch/README.md) | design | AutoResearch 设计文档入口与已确认决策 |
| [AutoResearch 框架与 TS 插件设计](autoresearch/2026-08-15-autoresearch-ts-plugin-design.md) | design | 端到端 idea→experiment→paper 全流程、独立 @athena/autoresearch 包、阶段状态机、论文生成三路径、预算与质量闸 |
| [AutoResearch 泛化与极简架构](autoresearch/2026-08-15-autoresearch-generalization-and-minimalism.md) | design | RunSpec + PipelineRunner + StageContext + Provider 可插拔；SVG skill 不绑定、默认 native-svg 兜底 |
| [AutoResearch 详细设计](autoresearch/2026-08-15-autoresearch-detailed-design.md) | design | 实现级设计：包结构、zod 契约、服务 API、阶段伪代码、插件注册、事件、恢复、测试与验收 |
| [AutoResearch 阶段协议与 Paper Engine](autoresearch/2026-08-15-autoresearch-protocols-and-paper-engine.md) | design | Athena 适配接口、入池协议、TemplateKit/Overleaf/LatexBuilder/Composer/Reviewer/Packaging 契约、事件 payload、错误码、BDD 场景 |
| [AutoResearch 论文图与可信实验/消融设计](autoresearch/2026-08-15-autoresearch-figures-and-experiment-design.md) | design | LLM 生成 SVG 架构图、SVG 渲染管线、EDA 图不进入论文、多数据集可信实验、消融矩阵、BDD 增量 |
| [Hypothesis 本地池设计](autoresearch/2026-08-15-hypothesis-local-pool-design.md) | design | 独立 HypothesisPool 服务、池状态机、与 ResearchTree 对账、论文回写 |

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

## Compute Resources（远程 GPU 执行）

| 文档 | 状态 | 用途 |
|---|---|---|
| [Compute Resources 设计](compute-resources-design.md) | current | 当前设计：协议缝、租约、常驻通道、镜像、数据分发、限制、测试与后续需求 |
| [远程 GPU 执行：需求分析与架构设计](architecture/2026-08-19-remote-gpu-execution-design.md) | current | 需求分析、备选方案、实现记录与真机验收（历史） |

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
