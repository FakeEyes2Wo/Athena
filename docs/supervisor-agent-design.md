# Athena SupervisorAgent 设计

> 状态：详细设计，待用户审阅
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 运行时设计：[agent-kernel-runtime-design.md](agent-kernel-runtime-design.md)
> 范围：根 SupervisorAgent 的输入、工具、决策边界、等待与现有 ResearchRuntime 的适配

## 1. 目标

SupervisorAgent 是 Athena 工作流的动态编排者。它根据用户目标、项目事实和子 Agent 结果决定：

- 使用哪些已注册 Agent 类型；
- 哪些实例可以并发；
- 何时 follow-up 原实例；
- 何时等待 Agent 或用户；
- 何时进入下一阶段、停止或请求人工判断。

SupervisorAgent 不拥有 Scheduler、预算账本、权限、ArtifactStore、ResearchTree 或评估硬门槛。这些仍由确定性服务维护。

## 2. 与当前 Supervisor 的关系

当前 [experiment/supervisor.py](../src/athena/experiment/supervisor.py) 是纯函数式搜索裁决器，只根据比较结果和预算返回 ACCEPT/REJECT/STOP。它应重命名为：

```text
SearchDecisionPolicy
```

该策略继续作为普通确定性服务供 SupervisorAgent 使用，但不升级为 Agent，也不管理子 Agent。

新的 SupervisorAgent 位于：

```text
src/athena/agents/supervisor.py
```

两个概念不能继续共用 `Supervisor` 名称。

## 3. 实例身份

每个研究项目至少有一个 root SupervisorAgent：

```text
agent_type = "supervisor"
agent_id   = 独立不透明 ID
name       = 用户可见名称，默认 "supervisor"
parent_id  = None
```

同一项目首版只创建一个 root Supervisor 实例。重新打开项目时显式 follow-up 该 `agent_id`，恢复其私有记忆；不得创建同名替代实例来冒充恢复。

## 4. Turn 输入

Kernel 在每次 Supervisor turn 开始时组装最小输入：

```text
current user/trigger message
registered agent_type list
ResearchState summary ref
budget summary
unread mailbox messages
Supervisor selected memory refs
explicit context_refs
```

完整 ResearchTree、报告、日志和数据不直接展开进 prompt，只注入 ArtifactRef 和短摘要。Supervisor 需要细节时通过只读工具读取指定 Artifact。

Kernel 不自动把所有项目记忆塞入上下文。记忆检索产生候选后，由 Supervisor 显式选择本 turn 使用的引用。

## 5. 最小工具面

Supervisor 只有五个编排工具：

```text
spawn(agent_type, name, content, context_refs=[])
send(agent_id, content, context_refs=[])
followup(agent_id, content, context_refs=[])
wait_for(agent_ids)
wait_for_human(content, context_refs=[])
```

以及业务所需的只读工具，例如读取 Artifact、查看 ResearchTree 摘要和预算。业务工具与编排工具分开注册。

工具语义：

- `spawn` 总是创建新实例；同一 agent_type 可调用多次。
- `followup` 只有显式 agent_id 才复用原实例和私有记忆。
- `send` 不唤醒目标，适合补充不需要立即处理的上下文。
- `wait_for` 登记依赖并结束当前 turn。
- `wait_for_human` 持久化问题并结束当前 turn。

首版不提供动态注册新类型、修改调度优先级、强制写全局记忆和绕过 EvaluationPolicy 的工具。

## 6. 单个 Turn 的行为

Supervisor 每个 turn 只完成一个可审计的编排步骤：

1. 读取触发消息、未读 completion 和项目摘要。
2. 判断当前目标是否已有可用实例。
3. 需要新能力时按 agent_type `spawn`；需要延续上下文时 `followup` 原 agent_id。
4. 可以并发发起多个互不依赖的 Agent。
5. 若结果尚未齐备，调用 `wait_for` 并结束 turn。
6. 若需要用户权威判断，调用 `wait_for_human` 并结束 turn。
7. 若阶段完成，写入结果 Artifact，并返回普通 `AgentOutcome`。

不引入 `SupervisorDecision` DTO。动态决定通过工具调用、Kernel journal 和最终结果 Artifact 共同形成审计记录。

## 7. Completion 处理

子 Agent 完成后，Kernel 使用同一个最小 `AgentMessage` 写入 Supervisor mailbox：

```text
source = None                 Kernel 内部 completion
content                       child run id、状态与简短失败信息
context_refs                  成功结果及其他已提交 ArtifactRefs
```

不新增 completion DTO；Supervisor 从短消息和 ArtifactRefs 继续工作，需要完整运行详情时通过只读运行摘要查询获得。

Supervisor 被唤醒后：

- 不重新读取所有子 Agent memory；
- 只读取 completion 中的 ArtifactRef；
- 根据业务结果决定下一步；
- 需要修订时 follow-up 原作者实例；
- 需要独立观点时创建新实例。

普通 completion 不代表结果被接受。通过与否仍由相应的确定性政策或 EvaluationPolicy 判定。

## 8. DataAnalysis 编排

Supervisor 对 DataAnalysis 的职责仅是编排：

```text
spawn DataAgent
  -> 等待 DataAnalysis v1
spawn ReflectionAgent
  -> 等待 rubric/review
EvaluationPolicy 计算 passed/failed

failed -> followup 原 DataAgent
passed -> 推进下一阶段或请求用户
```

Supervisor 不能自己提交 DataAnalysis、替 Reflection 打分或直接改 report。完整边界见 [data-analysis-agent-workflow-design.md](data-analysis-agent-workflow-design.md)。

## 9. 确定性约束

以下决定不交给 Supervisor 模型：

- 是否超过全局并发限制；
- 是否超过预算；
- agent_type 是否已注册；
- follow-up 目标是否 busy；
- mailbox、Run 和 Artifact 是否提交成功；
- EvaluationPolicy 是否 passed；
- 全局记忆是否得到用户批准；
- 项目和 Agent 是否已归档或删除。

Supervisor 可以在 `failed` 后选择返工、终止或询问用户，但不能把硬门槛结果改成 `passed`。

## 10. ResearchRuntime 适配

当前 `ResearchRuntime` 持有 `_run_task` 并静态推进 PREPARE、SEARCH、VALIDATE、REPORT。目标结构为：

```text
ResearchRuntime / App API
  -> 创建或 follow-up root SupervisorAgent
  -> 保存 AgentRun id
  -> 投影 Kernel 事件为外部 phase/status
```

ResearchRuntime 保留：

- ResearchTree；
- BudgetSnapshot；
- 用户可见 phase；
- pause/stop/report 等外部命令；
- 项目保存路径。

ResearchRuntime 移除：

- 自己创建 Agent task；
- 静态调用 DataPipeline/Ideator/CodeAgent 的执行链；
- Agent retry 和并发所有权；
- Agent memory 与 mailbox。

用户可见阶段仍是确定性事实，但阶段内调用哪些 Agent 由 Supervisor 动态决定。

## 11. Pause、Stop 与用户确认

- pause：停止派发该项目的新 Agent turn，已在运行的 turn 到安全边界后停下；不删除 mailbox 或等待。
- resume：恢复该项目 ready Agent 的 FIFO 入队。
- stop：中断活动 Run，保留已提交 Artifact 和逻辑 Agent，项目进入可审计终态。
- wait_for_human：项目仍然存活，只是 Supervisor 进入 WAITING_FOR_HUMAN。

App Server 只负责传输审批请求和回复。客户端断线不等同于拒绝。

## 12. 失败反馈

首版不让 Supervisor 预判所有错误。任何子 Run 失败都使用统一 completion 反馈：

```text
agent_id + run status + 简短错误 + 已提交 ArtifactRefs
```

Supervisor 再根据实际结果和预算选择动作。Kernel 不自动重试，Supervisor 也不依赖复杂错误分类。

## 13. 最小验证

- root Supervisor 可跨多个 turn 复用同一私有记忆；
- 同一 turn 可 spawn 多个不同或相同类型实例；
- completion 只在命中等待条件时唤醒 Supervisor；
- 修订任务 follow-up 原 Agent，而不是创建替代实例；
- Supervisor 无法绕过预算、未注册类型和 EvaluationPolicy 硬门槛；
- WAITING_FOR_HUMAN 在 App Server 断线后仍可恢复；
- ResearchRuntime 不再拥有 Agent 执行 task 或 memory。
