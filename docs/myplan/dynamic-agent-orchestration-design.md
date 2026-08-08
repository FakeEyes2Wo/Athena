# Athena 动态 Agent 编排设计

> 状态：方案 2 已确认，一次性全面迁移
> 日期：2026-08-08
> 范围：公共合同、所有权和切换边界

## 1. 文档导航

本文只定义全局不变量。完整运行流程以 [Agent 端到端工作流设计](agent-end-to-end-workflow-design.md) 为准；专题细节分别见：

- [Agent Kernel Runtime 设计](agent-kernel-runtime-design.md)
- [SupervisorAgent 设计](supervisor-agent-design.md)
- [已注册 Agent 目录设计](registered-agent-catalog-design.md)
- [DataAnalysis Agent 工作流设计](data-analysis-agent-workflow-design.md)
- [Agent 记忆与人工等待设计](agent-memory-human-wait-design.md)

发生冲突时，公共合同以本文为准，阶段和切换流程以端到端文档为准，模块内部实现以对应专题文档为准。

## 2. 目标

Athena 只保留一套 Agent 执行体系：

```text
App Server -> ProjectRuntime -> root SupervisorAgent -> AgentKernel
           -> registered Agent instances -> deterministic services
```

`Agent` 是执行、调度和记忆单位。`AgentTask`、`ResearchRuntime._run_task`、Ideator 私有线程管理器和直接串联业务 Agent 的 Pipeline 不再拥有生命周期。

本次采用一次性切换：七类 Agent 全部接入、完整流程通过后，旧执行路径在同一切换中退场。不设计长期双写或双 Runtime 同步。

## 3. 最小公共合同

### 3.1 身份

```text
agent_id
agent_type
name
```

- `agent_id` 是唯一定位键。
- `agent_type` 是静态 Registry 键。
- `name` 只用于显示，可以重复。
- 同类型允许多个独立实例；每个实例拥有独立 runtime binding 和私有记忆。

### 3.2 消息

```python
AgentMessage(source, content, context_refs)
```

- 用户入口的 `source` 为 `"user"`。
- Agent 工具调用的 `source` 为当前 `agent_id`。
- Kernel completion 的 `source` 为 `None`。
- 正式结果和大对象只通过 `context_refs` 传递。

不增加 invocation、decision、completion 或 role DTO。

### 3.3 运行结果

```python
AgentOutcome(result_ref)
```

`result_ref` 指向已提交 Artifact。私有记忆的 `context_ref` 只归 `AgentSession` 所有；旧 `next_context_ref` 是迁移字段，Kernel 忽略，旧调用方退场时删除。

### 3.4 编排工具

```text
spawn  send  followup  wait_for  wait_for_human
```

工具绑定当前 Run。模型不能提交 source、parent、项目范围或权限。权限由静态 `agent_type` 矩阵决定。

## 4. 所有权

| 事实 | 唯一所有者 |
|---|---|
| Agent、Run、mailbox、wait、私有记忆引用 | AgentKernel |
| 项目装配、root id、phase/status 投影 | ProjectRuntime |
| 假设、实验和 SOTA | ResearchTree |
| 报告、图片、日志、rubric 和评估结果 | ArtifactStore |
| 代码提交与工作区 | GitWorkspace |
| 阈值、排序、预算和评估硬门槛 | 确定性 Service/Policy |
| 动态选择 Agent、并发关系和返工 | SupervisorAgent |

业务 Agent 不直接改写上述权威事实；它们提交候选 Artifact，由所有者校验和提交。

## 5. 七个静态类型

```text
supervisor  data  plot  reflection  ideator  code  report
```

动态创建新类型不进入本次切换，只保留 `TODO(dynamic-agent-type)`。

PlotAgent 是通用绘图子 Agent。DataAnalysis 版本只能由原 DataAgent 实例提交。ReflectionAgent 先生成或选择 rubric，再评分。Report 修订必须 follow-up 原 ReportAgent。

## 6. 固定阶段，动态编排

```text
CONFIGURE -> PREPARE -> SEARCH -> VALIDATE -> REPORT -> COMPLETED
```

Supervisor 可以动态决定阶段内使用哪些已注册 Agent、创建多少实例、并发还是串行、follow-up 还是新建，但不能绕过数据隔离、append-only EvalSpec/rubric、预算、权限、Git、EvaluationPolicy、用户确认和阶段完成条件。

## 7. 记忆与等待

- 私有记忆绑定 `agent_id`，只在 follow-up 原实例时恢复。
- 项目记忆只保存有证据的事实、决定和已验证修复。
- 全局记忆必须经过用户确认。
- `wait_for` 和 `wait_for_human` 持久化条件、结束当前 turn 并释放执行槽。
- 唤醒创建新 Run，不恢复旧 Python 调用栈。

## 8. 一次性切换完成条件

1. 七种类型均为真实 factory，不再使用 `SimpleAgent` 占位。
2. `ProjectRuntime` 可以完成六阶段流程并跨重启恢复。
3. SEARCH 的 Ideator/Code 生命周期由 Kernel 管理。
4. VALIDATE 只对冻结 SOTA 执行，final-test 恰好一次。
5. REPORT 使用 ReportAgent + ReflectionAgent 形成版本闭环。
6. App Server 只调用 ProjectRuntime，不拥有 Agent task、wait 或 memory。
7. `ResearchRuntime._run_task`、旧 AgentTask 和 Pipeline 直调 Agent 路径退场。
8. 一个真实小型 CSV 从用户请求运行到最终报告，所有结果可追溯。

## 9. 非目标

本次不增加动态类型注册、复杂调度优先级、mailbox 配额、自动错误分类、自动降级、向量记忆库或大规模预防性错误矩阵。真实失败反馈给 Supervisor，由其在预算和硬门槛内决定修复、换实例、请求用户或停止。
