# Athena Agent Kernel Runtime 设计

> 状态：一次性迁移专题设计
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 范围：Agent 身份、Run、消息、调度、等待和恢复

## 1. 职责

AgentKernel 只回答“哪个 Agent 的哪个 Run 可以执行，以及状态如何可靠提交”。它不决定业务阶段、重试、接受/拒绝、SOTA 或报告内容。

```text
AgentTypeRegistry -> AgentKernel -> AgentGraphStore
                         |
                         -> AgentSession -> BaseAgentRunner
```

## 2. 当前实现状态

已经进入实现并通过相关测试的部分：

- 不透明 `agent_id`、同父同名实例和每实例 factory；
- Agent/Run 分离、AgentId FIFO、generation 门禁；
- `AgentMessage(source/content/context_refs)`；
- 无假 trigger 的 Turn 投影和成功/wait 后 mailbox checkpoint；
- 静态 spawn 权限矩阵和五个 Run 绑定工具；
- 持久化 wait、completion outbox、JSON snapshot 恢复；
- 按 `context_ref` 恢复私有 rollout。

一次性切换仍需补齐：

1. spawn/followup 的 trigger source 也必须保留调用 Agent，而不是统一显示为 user。
2. ProjectStore 必须在每次权威 Kernel 命令后耐久化，不能只在部分 ProjectRuntime 方法保存 snapshot。
3. App Server、ResearchRuntime 和业务 Pipeline 必须停止拥有第二套 Agent 生命周期。
4. 旧 `next_context_ref` 和 `AgentTask` 调用方迁完后删除。

## 3. Registry 与实例

```python
registry.register(agent_type, factory)
factory(agent_id) -> AgentSpec
```

每次创建或恢复 `agent_id` 都产生新的 runtime binding。允许共享无状态服务、模型配置和连接池，不允许共享带 turn 状态的业务 Agent 对象。

AgentRecord 最小事实：

```text
agent_id
agent_type
name
parent_id
status
context_ref
pending_run_id
created_sequence
```

path 是展示派生值，不是身份。首版不增加公开 config 或 role 合同；实例差异通过初始消息和 ArtifactRefs 表达。

## 4. Run 与 Turn

一个 Agent 同时最多有一个非终态 Run：

```text
Agent: IDLE -> QUEUED -> RUNNING -> IDLE
                              -> WAITING
                              -> WAITING_FOR_HUMAN

Run: QUEUED -> RUNNING -> COMPLETED | FAILED | INTERRUPTED
```

`COMPLETED` 是 Run 终态，不是 Agent 终态。Scheduler 的 ready 队列保存 `agent_id`；取出时读取其 `pending_run_id`。

首版只有 FIFO 和全局活动 Agent 上限，不增加类型配额、优先级或自动重试。

## 5. Turn 输入与结果

BaseAgentRunner 机械组装：

1. 有真实请求时放入 trigger；wait 唤醒没有 trigger。
2. 在 trigger 后附加按提交顺序排列的未读 mailbox。
3. `input_text` 只是 trigger content 的迁移视图。
4. Agent 正常返回或成功进入持久化等待后提交 mailbox cursor。
5. 失败或中断不提交 cursor，消息可以重新交付。

目标结果合同只有：

```python
AgentOutcome(result_ref)
```

Kernel 不解析业务 Artifact。`context_ref` 由 AgentSession 在安全边界更新，业务 Agent 无权返回另一条私有记忆引用。

## 6. 消息语义

```text
send       -> 写 mailbox，不创建 Run
followup   -> 为原实例创建新 Run
completion -> Kernel 写父 mailbox
human_reply-> 写等待 Agent mailbox 并创建唤醒 Run
```

消息顺序使用 Store 提交顺序和单一 committed cursor，不把 sequence 放进公共消息。

source 规则：

- 外部入口：`"user"`；
- 绑定 Run 的 Agent 工具：当前 `agent_id`；
- Kernel completion：`None`。

## 7. 工具投影

Kernel 只有五个模型编排命令：

```text
spawn  send  followup  wait_for  wait_for_human
```

RunToolProjector 根据 `agent_type + RunSession` 注入工具。source、parent、项目范围和权限来自绑定层，不是模型参数。spawn 目标必须通过 [已注册 Agent 目录设计](registered-agent-catalog-design.md) 的静态矩阵。

wait 工具成功提交后以 runner 内部控制流结束 turn；wait 后的模型输出、memory 写入和工具调用不能提交。

## 8. 持久化等待

进入等待的单一提交边界必须包含：

```text
current Run terminal
Agent waiting status
wait condition/request id
mailbox cursor
context_ref
lease release
```

completion 或用户回复命中条件时只创建一个新 Run。客户端断线和 transport timeout 不取消等待。

## 9. 恢复

生产状态不使用 pickle，也不持久化 runner、model client、Task、锁或 ContextManager 对象。

```text
QUEUED             -> 重新入队
RUNNING            -> 标记 kernel_restarted，不恢复调用栈
WAITING            -> 恢复依赖
WAITING_FOR_HUMAN  -> 继续等待 request id
terminal           -> 不重跑，按 outbox 补投 completion
```

恢复前先装配七类 factory，再按 `agent_type` 重建每个 Session。

## 10. 失败边界

Kernel 只保证失败不写坏状态：busy follow-up 零变更，旧 generation 不能提交，新旧 completion 不重复。领域失败只形成 RunSummary，是否 follow-up、换实例或停止由 Supervisor 决定。

## 11. 验收

- 同类型多个实例的 runner、memory 和 Run 相互独立；
- send 不唤醒，followup 创建新 Run；
- trigger 与 mailbox 顺序、source 和 cursor 正确；
- wait 释放槽，重启后只唤醒一次；
- terminal completion 最多投递一次；
- pause/stop 不产生新的派发；
- 每次权威命令后项目状态可从耐久 Store 恢复；
- Kernel 外不存在第二套 Agent task、mailbox 或 wait 所有者。
