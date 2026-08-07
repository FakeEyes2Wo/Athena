# Athena 已注册 Agent 目录设计

> 状态：详细设计，待用户审阅
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 运行时设计：[agent-kernel-runtime-design.md](agent-kernel-runtime-design.md)
> 范围：首版静态 `agent_type`、职责边界、最小工具权限，以及当前模块到目标 Agent 的映射

## 1. 文档职责

本文回答两个问题：首版 Registry 注册哪些 Agent 类型，以及旧设计中的角色应该成为 Agent、普通 Service 还是工具。

本文不新增公共 DTO，不定义 Agent 专用消息，也不把内部方法名变成 Kernel 合同。所有类型仍使用同一个 `AgentMessage`、`AgentContext` 和 `AgentOutcome`。

## 2. Agent 准入规则

只有满足以下任一条件的职责才注册为 Agent：

- 需要独立、可恢复的多轮上下文；
- 需要独立工具权限或隔离执行环境；
- 需要与同类型其他实例并发运行；
- 需要模型完成生成、解释、综合或证据判断。

以下职责保留为确定性 Service 或工具：

- 状态迁移、预算、并发限制和 FIFO；
- 阈值计算、Elo 更新、排序和 score aggregation；
- timeout、stall、文件边界和数据切分检查；
- Artifact、ResearchTree、Git 和全局记忆的权威写入。

判断标准是“是否需要独立推理上下文”，不是旧类名是否包含 `Agent`。能由普通函数可靠完成的行为不注册 Agent。

## 3. 首版静态类型

首版只注册以下七种类型：

| `agent_type` | 单一职责 | 主要结果 Artifact | 可复用同一实例的场景 |
|---|---|---|---|
| `supervisor` | 动态编排项目内已注册 Agent | 阶段结果、编排摘要、人工确认请求 | 项目后续请求与子 Agent completion |
| `data` | 数据检查、EDA、分析报告和 DataAnalysis 版本提交 | `DataAnalysis/` Bundle | review 后生成 v2、v3 |
| `plot` | 根据数据引用和图规格生成图片 | 图片、图注、观察和可选绘图代码 | 修改同一张图或延续同一视觉上下文 |
| `reflection` | 生成/追加 rubric，按指定版本评审 Artifact | rubric、score、review、记忆候选 | 同一评审链的复审或按需重评 |
| `ideator` | 生成、辩论、修订和演化可证伪假设 | hypothesis、debate transcript、lineage | 同一研究方向的后续辩论 |
| `code` | 在受限工作区生成或修订代码 | diff、日志、运行结果和评估引用 | 根据失败证据修复原实验 |
| `report` | 从已批准 Artifact 综合最终叙述报告 | 最终报告 Bundle | 对同一报告做基于 review 的修订 |

一个类型可以有多个独立实例。`name` 用于显示角色，例如 `debater-1`、`debater-2`、`judge`，但这些名称不产生新的 `agent_type`。

## 4. 类型边界

### 4.1 SupervisorAgent

- 选择类型、实例、并发关系和下一步。
- 读取项目摘要、预算、mailbox 和 ArtifactRefs。
- 不执行确定性评分，不直接修改 ResearchTree、Git 或其他 Agent 私有记忆。
- 不承担 DataAnalysis、代码或最终报告的业务所有权。

### 4.2 DataAgent 与 PlotAgent

- DataAgent 是 DataAnalysis 版本的唯一提交者。
- PlotAgent 是通用辅助类型，不拥有调用方报告。
- 任何确实需要图片的 Agent 都可以创建 PlotAgent；是否等待由调用方决定。
- PlotAgent 不能借助通用性绕过调用方的 Artifact 所有权规则。

完整流程见 [data-analysis-agent-workflow-design.md](data-analysis-agent-workflow-design.md)。

### 4.3 ReflectionAgent

ReflectionAgent 是通用只读评审类型，不只服务 DataAnalysis：

- 先生成或选择精确 rubric 版本，再评分；
- 对每一项提供证据，不只给总分；
- 可按任务配置使用外部论文检索，尤其用于新颖性判断；
- 可从多次 review 中生成项目/全局记忆候选；
- 不能修改被评 Artifact，也不能确定性改写 `passed/failed`。

Meta-review、假设审查和 DataAnalysis review 使用同一类型，通过 `content`、`context_refs` 和工具配置区分，不拆成多个评审 Agent 类型。

### 4.4 IdeatorAgent

- 生成、辩论、修订和演化假设，但不原地覆盖历史假设。
- 多个辩者是多个 `ideator` 实例，拥有独立 `agent_id` 和私有记忆。
- judge 可以是带不同 `name/config_ref` 的 `ideator` 实例，不增加 `judge` 类型。
- pairwise 判断必须绑定 rubric；Elo 数值更新由确定性服务完成。
- Proximity 计算和候选去重优先使用普通算法，只有语义判断部分调用 Agent。

### 4.5 CodeAgent

- 同一 `code` 类型承担代码生成与基于真实失败证据的修订，不再增加 `fix` 类型。
- 修订使用 follow-up 原 CodeAgent，以保留它对工作区、尝试和失败的私有记忆。
- 文件权限、受保护评估文件、数据 split 和执行预算由确定性服务强制执行。
- CodeAgent 只产生候选 diff 和运行 Artifact；Git commit 与实验接受仍由权威边界执行。

已验证的故障、修复方式和验证结果可以晋升为项目记忆；未经验证的尝试只保留在该 Agent 私有记忆和运行日志中。

### 4.6 ReportAgent

- 只读取已批准的报告、实验、评审和 ResearchTree ArtifactRefs。
- 综合叙述和引用，不重新运行实验、不改变指标、不补造证据。
- 需要图时可以创建 PlotAgent，但只有已完成并被引用的图片才能进入当前报告版本。
- 报告 review 仍由独立 ReflectionAgent 完成；修订 follow-up 原 ReportAgent。

## 5. 最小工具权限

Kernel 提供统一编排命令，但 Composition Root 按类型注入最小能力；权限是进程配置，不形成公开 `AgentRoleSpec`。

首版规则：

- `supervisor`：可在当前项目内 spawn 所有已注册业务类型，并 send/followup/wait。
- `ideator`：可创建 `ideator` 辩者、`reflection` 评审者和 `plot` 辅助实例。
- 其他非 `plot` 类型：需要图片时可创建 `plot`，并等待或 follow-up 自己创建的子实例。
- `plot`：不创建业务子 Agent。
- 业务 Agent 不能动态注册类型、关闭任意非子树实例、修改 Scheduler 或直接写全局记忆。

若一个类型不需要某项命令，其 factory 不注入该工具。Kernel 命令集合保持统一，权限收窄不产生新的消息或调用 DTO。

所有编排工具都绑定当前 Run；模型只提供目标类型/实例、`content` 和 `context_refs`。来源 Agent、父子关系、项目范围与权限由 Kernel 注入，模型不能伪造。

## 6. 当前模块映射

| 当前模块/旧角色 | 目标归属 | 理由 |
|---|---|---|
| `core.agent.Agent` / `BaseAgent` | `core/agent` 单 Agent runtime | 通用采样与工具循环，不是注册类型 |
| `experiment.pipeline.DataPipeline` 中的 data/plot 对象 | `data` + `plot` | 独立实例、Artifact 交接和可恢复修订 |
| `ideator.Ideator` 与私有 `RuntimeThreadManager` | 多个 `ideator` 实例 | 辩者身份、记忆和并发进入 Kernel |
| `workflows.search.CodeAgent` | `code` | 代码生成与修订共用原实例 |
| `workflows.report.FinalReportWorkflow` 的可选 agent | `report` | 报告生成获得稳定身份与版本链 |
| `experiment.supervisor.Supervisor` | `SearchDecisionPolicy` | 确定性 ACCEPT/REJECT/STOP，不是 Agent |
| `code.monitor.AgentMonitor` | 合并/更名为确定性 execution monitor | timeout/stall 检查不需要模型上下文 |
| `execution.ExecutionMonitor` | 确定性 Service | 观察并发执行并发出健康事件，不做业务决策 |
| `AgentResource` | integrations、ArtifactStore 和受控下载工具 | 资源解析/下载优先确定性实现 |
| `AgentObjective` | TaskParser + `reflection` | 目标解析确定性，rubric 生成需要评审推理 |
| `AgentInit` | PREPARE services + `code` | 环境事实由 Service 维护，修复由原 CodeAgent 完成 |
| `AgentFix` | follow-up 原 `code` | 减少类型并复用失败上下文 |
| `AgentScheduler` | `core/agent_kernel.AgentScheduler` | Scheduler 是确定性运行层，不是模型 Agent |
| Ranking / Proximity / Evaluation | 普通算法或 Policy | 分数更新、相似度和阈值必须可重复 |

迁移后，只有 `src/athena/agents/` 中的业务实现被 Registry 注册；其他含 `Agent` 历史名称的类不因此自动成为 Agent。

## 7. 内部实现约定

这些约定不进入 Kernel 公共合同：

- Prompt 和 Pydantic `description` 使用英文，中文只在用户展示层生成。
- Agent 内部动作优先使用 `parse`、`collect`、`normalize`、`retrieve`、`plan`、`generate`、`select`、`validate`、`execute`、`record`、`reflect` 等原语。
- 不为 `build_xxx`、`manage_xxx` 或 `handle_xxx` 之类复合业务短语建立新的跨模块接口。
- 同一 Agent 的不同任务差异优先通过消息、Artifact 和 `config_ref` 表达；只有工具权限或生命周期语义真正不同才增加类型。

## 8. 最小验证

- Composition Root 只注册上述七个稳定类型。
- 同一类型可以创建多个独立实例，`name` 不影响 factory 查找。
- Ideator 辩者和 judge 不再拥有 Kernel 外的私有 thread/history 表。
- Code 修订 follow-up 原实例，不创建独立 FixAgent。
- PlotAgent 可被不同业务类型创建，但不能提交调用方 Artifact。
- ExecutionMonitor、SearchDecisionPolicy、EvaluationPolicy 和 Elo 更新均不出现在 Registry。
- 每个 factory 只获得其职责所需工具；权限收窄不改变通用消息合同。
- 未注册类型由 Kernel 拒绝，动态新增类型仍不进入首版。
