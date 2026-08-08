# Athena SupervisorAgent 设计

> 状态：一次性迁移专题设计
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 完整流程：[agent-end-to-end-workflow-design.md](agent-end-to-end-workflow-design.md)

## 1. 单一职责

SupervisorAgent 根据用户目标、项目事实、预算和子 Agent 结果，动态决定下一次编排动作：

```text
spawn | send | followup | wait_for | wait_for_human | finish
```

它不拥有 Scheduler、phase、ResearchTree、Git、ArtifactStore、评估门槛或其他 Agent 私有记忆。

当前 `experiment/supervisor.py` 的 ACCEPT/REJECT/STOP 逻辑是确定性 `SearchDecisionPolicy`，不是 SupervisorAgent。

## 2. root 实例

每个项目只有一个稳定 root：

```text
agent_type = supervisor
parent_id  = None
agent_id   = persisted root_supervisor_id
name       = supervisor
```

重启时恢复原 `agent_id`。不能创建同名实例冒充恢复，也不能把编排进度只保存在 `SupervisorAgent` 对象字段中。

## 3. Turn 输入

Supervisor 每个 turn 只接收：

```text
trigger and unread mailbox messages
registered agent types
project phase/status summary
budget summary
ResearchTree summary ref
explicit artifact and memory refs
```

完整数据、报告、日志和 ResearchTree 不直接展开进 prompt。需要细节时使用只读工具按 ref 获取。

## 4. 决策循环

每个 turn 执行一个可审计步骤：

1. 读取 phase、硬门槛、mailbox 和失败证据。
2. 判断当前阶段缺少哪个已提交事实。
3. 需要新观点或并行工作时 spawn 新实例。
4. 需要延续作者上下文或修复真实失败时 follow-up 原实例。
5. 结果未齐时 wait_for；需要权威用户判断时 wait_for_human。
6. 阶段事实满足时请求 ProjectRuntime 投影下一阶段。
7. REPORT 通过后返回最终 `result_ref`。

不增加 `SupervisorDecision` DTO。工具调用、Kernel journal 和 Artifact 共同构成审计记录。

## 5. 固定门槛

Supervisor 不能绕过：

| 阶段 | 必须存在 |
|---|---|
| CONFIGURE | 有效 task_ref |
| PREPARE | DataAnalysis、frozen EvalSpec、successful baseline |
| SEARCH | successful SOTA，策略或预算停止 |
| VALIDATE | ablation、exactly-one final-test |
| REPORT | passed FinalReport review |

EvaluationPolicy、Comparator、SearchDecisionPolicy 和阶段投影是确定性服务。Supervisor 可以选择返工方式，但不能篡改结果。

## 6. 实例选择

- 新任务默认 spawn 新实例。
- 修订同一 DataAnalysis、代码尝试或 FinalReport 时 follow-up 原作者。
- 同类型实例可以并行，name 可以重复。
- 只有显式 `agent_id` 可以复用私有记忆。
- PlotAgent 只作为需要图片的业务 Agent 子实例。

## 7. 等待与失败

调用 wait 后当前 turn 立即结束。唤醒后 Supervisor 从已提交 phase、mailbox 和 refs 重新判断，不依赖旧调用栈。

Run 失败时只允许：

```text
followup original agent
spawn independent agent
request user
stop current stage/project
```

Kernel 不自动重试，Supervisor 也不能无限返工；预算耗尽是明确停止条件。

## 8. 当前缺口

当前 SupervisorAgent 只会创建一个 DataAgent 并等待，仍是确定性骨架。一次性切换必须让它读取项目事实并覆盖完整六阶段，同时删除 `ResearchRuntime._run_task` 的阶段编排职责。

## 9. 验收

- 同一 root 跨 turn 和重启保持相同 agent_id 与私有记忆；
- 可以并发创建多个 Ideator/CodeAgent；
- failed DataAnalysis/FinalReport 会 follow-up 原作者；
- wait 不占执行槽，completion 只唤醒一次；
- 阶段门槛不满足时不能推进；
- Supervisor 没有直接修改 ResearchTree、Git、EvalSpec 或全局记忆的工具；
- 完整流程不再依赖 Supervisor 对象内的临时布尔字段。
