# Athena Agent Kernel Runtime 设计

> 状态：详细设计，待用户审阅
> 日期：2026-08-08
> 上位设计：[dynamic-agent-orchestration-design.md](dynamic-agent-orchestration-design.md)
> 范围：`core/agent_kernel`、`core/agent` 适配、Agent 类型注册、调度、mailbox、等待与恢复

## 1. 文档职责

本文细化 Athena 动态 Agent 编排的执行内核。上位设计决定“Agent 是执行单位、Supervisor 动态编排、Kernel 确定性执行”；本文只回答这些决定如何映射到当前代码。

本文不设计具体业务 Agent、DataAnalysis 内容、记忆晋升规则或实现任务拆分。相关设计见：

- [supervisor-agent-design.md](supervisor-agent-design.md)
- [data-analysis-agent-workflow-design.md](data-analysis-agent-workflow-design.md)
- [agent-memory-human-wait-design.md](agent-memory-human-wait-design.md)

## 2. 当前代码判断

当前 [core/agent_kernel](../src/athena/core/agent_kernel/) 已具备可复用骨架：

- 单命令序列器和 `AgentGraphStore.commit()` 线性化点；
- Agent 与 Run 分离；
- FIFO、全局并发槽、generation/CAS；
- mailbox、follow-up、父子 completion outbox；
- Agent 私有 `ContextManager`；
- Agent 等待子任务时释放执行 lease。
- 当前 `AgentMessage` 已收敛为 `source/content/context_refs`，公共消息不再携带 sequence。

需要修正的不是这些机制本身，而是它们的身份、持久化和接入方式：

1. `AgentSpec` 当前携带 runner、codec 和 role，并被直接放入 `AgentRecord`，不能可靠持久化或跨进程重建。
2. `agent_id` 当前由 name/path 拼接，同名实例会冲突。
3. Scheduler 的 ready 队列保存 `RunId`，并把所有逻辑 Agent 长期计入 resident 容量。
4. parked wait 仍依赖存活的 Python coroutine，重启后不能继续。
5. `InMemoryResourcesFactory` 恢复时创建空上下文，没有恢复私有记忆。
6. [core/agent/control.py](../src/athena/core/agent/control.py) 与新 Kernel 各自拥有 task、memory 和 handle，形成双控制面。
7. App Server `ThreadRuntime`、ResearchRuntime 和 Ideator 仍各自拥有部分 Agent 生命周期。

目标不是增加兼容层，而是让 `core/agent_kernel` 成为唯一 Agent 生命周期所有者。

## 3. 目标所有权

```text
Composition Root
  ├─ AgentTypeRegistry     已注册类型与 factory
  ├─ AgentKernel           命令、状态迁移、调度、等待与恢复
  ├─ AgentGraphStore       小型持久化事实
  ├─ ArtifactStore         请求、结果和大对象
  └─ SessionResources      单实例私有记忆

core/agent
  └─ BaseAgent + sampling/tool loop

src/athena/agents
  └─ Supervisor/Data/Plot/Reflection 等业务实现
```

所有权约束：

- Registry 不运行 Agent，也不保存实例状态。
- Scheduler 不决定业务优先级、重试或下一阶段。
- GraphStore 不保存 Python runner、factory、model client 或 `ContextManager` 对象。
- `core/agent` 不创建子 Agent，不维护全局 handle 表。
- 业务 Agent 不直接读写 Scheduler、GraphStore 或其他 Agent 的 memory。

## 4. 类型注册与实例创建

### 4.1 Registry

首版只注册静态 Agent 类型：

```python
registry.register(agent_type, factory)
```

概念上的 factory 形态为：

```python
factory(agent_id, config_ref) -> runtime binding
```

`runtime binding` 是进程内实现细节，负责把现有 Kernel runner 接口适配到 `BaseAgent.run(AgentContext) -> AgentOutcome`。factory 可以闭包捕获模型供应商、工具构造器和普通服务；其返回值和 Registry 本身都不写入 GraphStore，也不新增公共 factory DTO。

动态创建并注册新 Agent 类型不进入首版；该保留项由上位设计统一记录。

### 4.2 AgentRecord

持久化 Agent 记录只保留：

```text
agent_id
agent_type
name
parent_id
status
config_ref
context_ref
pending_run_id
created_sequence
```

- `agent_id` 是不透明唯一 ID，与 name/path 解耦。
- `agent_type` 是 factory 查找键。
- `name` 仅用于显示，可以重复，不参与调度。
- `parent_id` 表达树关系；展示路径是派生值，不是身份。
- `config_ref` 可为空，非空时指向该实例的不可变配置 Artifact。

不再把 `AgentSpec` 或 role 持久化到 AgentRecord。原 role 语义拆成明确的 `agent_type`；需要更细的业务角色时，使用不同类型或配置 Artifact。

### 4.3 统一请求与结果

所有 Agent 请求使用同一个 `AgentMessage`：

```python
AgentMessage(
    source: AgentId | None,
    content: str,
    context_refs: list[ArtifactRef],
)
```

调用方只提交 `content + context_refs`，Kernel 负责填写权威 `source`；`None` 保留给 Kernel 内部 completion。消息作为 mailbox 的小型内部记录持久化，大内容仍放入 ArtifactStore。Registry 可以在进程内复用统一适配器，但 GraphStore 不持久化每个 Agent 的 codec。Agent 完成时继续返回：

```python
AgentOutcome(result_ref, next_context_ref)
```

类型化业务结果保存在 `result_ref` 指向的 Artifact 中，由业务边界自行验证；Kernel 不理解内容。

## 5. 控制面

### 5.1 Python 调用面

Composition Root 和普通服务使用现有 capability 风格，但参数改为 `agent_type`：

```text
create_root(agent_type, name, content, context_refs=[]) -> (AgentHandle, AgentRun)
spawn(parent, agent_type, name, content, context_refs=[]) -> (AgentHandle, AgentRun)
send(target, content, context_refs=[])
followup(target, content, context_refs=[]) -> AgentRun
wait_agent(targets)
interrupt(target, reason)
close(target)
```

`AgentHandle` 只持有稳定 `agent_id` 和所属 control；`AgentRun` 只持有 `run_id`。两者不缓存状态、memory、task 或 queue。

### 5.2 Supervisor 工具面

SupervisorAgent 只获得五个受控命令：

```text
spawn
send
followup
wait_for
wait_for_human
```

Kernel 在每个 Supervisor turn 开始时注入当前已注册的 `agent_type` 列表，因此不增加注册表查询 DTO。首版不向业务 Agent 暴露注册类型、关闭任意子树或直接修改状态的能力。

## 6. Scheduler

### 6.1 调度单位

ready 队列保存 `agent_id`，不是 `run_id`。AgentRecord 的 `pending_run_id` 指向待执行 turn，dispatcher 取出 Agent 后再读取对应 RunRecord。

```text
FIFO[agent_id]
  -> 读取 pending_run_id
  -> 占用全局执行槽
  -> 启动该 Agent 的一个 turn
```

同一 Agent 同时最多一个非终态 Run。多个同类型实例可以占用多个槽并行运行。

### 6.2 容量

首版 Scheduler 只有：

- `max_active_agents` 全局并发限制；
- 一个 FIFO ready 队列。

不做类型配额、优先级、权重和自动重试。逻辑 Agent 总数由项目预算限制，不由 Scheduler resident set 限制。IDLE、WAITING 和 WAITING_FOR_HUMAN 都不占执行槽。

## 7. Turn 与等待

### 7.1 状态分离

Run 仍表示一次 turn：

```text
QUEUED -> RUNNING -> COMPLETED | FAILED | INTERRUPTED
```

Agent 表示可持续复用的实例：

```text
IDLE -> QUEUED -> RUNNING -> IDLE
                  -> WAITING
                  -> WAITING_FOR_HUMAN
```

`COMPLETED` 是 Run 终态，不是 Agent 终态。只有显式项目删除才让逻辑实例不可恢复。

### 7.2 持久化等待

`wait_for` 和 `wait_for_human` 不暂停 Python coroutine。它们在安全边界结束当前 turn，并在同一事务中：

1. 提交当前 Run 的终态；
2. 保存 Agent 等待状态；
3. 保存等待条件；
4. 推进已消费 mailbox 游标；
5. 保存最新 `context_ref`；
6. 释放执行槽。

等待子 Agent 使用依赖 `agent_id` 集合；等待用户使用一个稳定 request id。等待记录是 Kernel 内部事实，不新增公共 `WaitRecord` 或 `ApprovalRecord` DTO。

### 7.3 唤醒

子 Run completion 与终态原子写入 outbox。投递父 mailbox 后：

- 命中父 Agent 已登记依赖时，Kernel 创建新的 Run 并把父 Agent放回 FIFO；
- 未命中时只保留消息，不自动运行父 Agent；
- 多个 completion 同时到达时，等待条件只触发一次新 Run。

用户回复采用相同规则：App Server 把 reply 交回 Kernel，Kernel 原子写 mailbox、清除相应等待条件并创建新 Run。传输超时或断线不删除 WAITING_FOR_HUMAN。

## 8. Mailbox 事务

公共消息不包含 sequence，也不新增 mailbox envelope 类型。消息顺序直接使用 `AgentGraphStore.commit()` 的 journal 顺序和 mailbox 列表位置；已消费边界继续使用单一 committed cursor。

- `send`：只写 mailbox，不创建 Run。
- `followup`：写消息和创建 Run 是同一事务；目标 busy 时零状态变更。
- completion：与子 Run 终态一同写 outbox，父 mailbox 投递按 child run id 去重。
- mailbox 只有一个 committed cursor；只有成功结束或进入持久化等待的当前 generation 能推进。

首版不做每来源速率限制和复杂 mailbox 配额。真实运行出现资源问题后再补充。

## 9. 私有记忆与资源卸载

每个 Agent 的 `context_ref` 指向可恢复的 rollout/compaction 状态。生产 `SessionResourcesFactory`：

1. Agent 入队运行前，根据 `context_ref` 恢复 `ContextManager`；
2. turn 中所有 memory 写入仍受 run/generation 门禁；
3. turn 结束时追加 rollout 并更新 `context_ref`；
4. Agent 进入 IDLE 或等待后释放模型会话和进程内资源。

`InMemoryResourcesFactory` 只用于测试。Kernel 重启时不得用空 ContextManager 代替已有私有记忆。

## 10. 持久化

持久化实现继续沿用 `AgentGraphStore` 的单一提交边界。生产实现使用项目内 SQLite：

```text
.athena/agent-graph.sqlite3
```

最小事实集合：

```text
agents
runs
mailbox
waits
outbox
commands
```

大型 request、response、context、trace 和错误详情进入 ArtifactStore，SQLite 只保存短引用。现有内存 Store 保留为相同行为的测试实现。

恢复规则：

- QUEUED：重新进入 FIFO；
- RUNNING：原调用栈不可恢复，将该 Run 标记为 `kernel_restarted`，实例回到 IDLE；
- WAITING：恢复依赖条件，不主动运行；
- WAITING_FOR_HUMAN：继续等待对应 request id；
- terminal Run：不重新执行，completion outbox 按 idempotency key 补投递。

## 11. 现有模块退场

迁移后的唯一边界如下：

| 当前所有者 | 目标 |
|---|---|
| `core/agent/control.py` | 调用方迁移后删除；不保留第二个 AgentControl |
| `app_server/ThreadRuntime` | 只保留外部协议适配，Agent 生命周期进入 Kernel |
| `ResearchRuntime._run_task` | 由 root SupervisorAgent 的 AgentRun 取代 |
| Ideator 私有 agent/history 字典 | 每个辩者改为已注册 Agent 实例及其私有记忆 |
| `AgentSpec.role` | 改为 Registry 的 `agent_type` |
| `AgentRecord.spec` | 改为 `agent_type + config_ref` |

迁移期间不允许新旧控制面双写同一个 Agent。某个调用方一旦切到 Kernel，就不再回退到旧 runtime。

## 12. 最小验证

首版只验证核心公共行为：

- 同一 agent_type 创建多个独立实例；
- FIFO 和全局并发限制；
- send 不唤醒，followup 创建新 Run；
- wait 释放槽并在 completion 后只唤醒一次；
- WAITING_FOR_HUMAN 在进程重启后仍存在；
- IDLE 资源卸载后 follow-up 能恢复相同私有记忆；
- busy follow-up 零状态变更；
- terminal completion 在恢复前后最多投递一次。

其余错误检查由真实端到端运行暴露后补充。
