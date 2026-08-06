# Athena 统一 Agent 内核设计

Status: approved
Owner: Athena maintainers
Date: 2026-08-04
Last revised: 2026-08-05

## 文档状态

本文设计 Athena 的统一 Agent 底层，以一套生命周期同时承载 app-server、
ResearchRuntime 与 Ideator。本文不是实施记录；源码在迁移完成前仍以当前实现为准。

2026-08-04 至 2026-08-05 已裁可：

- 第一节“内核与所有权”；
- 第二节“公开类型与方法”；
- 第三节“AgentKernel 状态机与消息流”；
- 第四节“权限、配置、错误与持久化”；
- 第五节“迁移、验收与退场”。

## 问题摘要

Athena 现有三处相互重叠的 Agent 生命周期：

- `core/agent/control.py` 自管 Agent task、future、event queue 与 memory；
- `app_server/thread_runtime.py` 另管 Thread/Turn、journal、snapshot、取消、终态与
  memory；
- `ideator/ideator.py` 的 `_DebateRunner` 再管辩者实例、binding 与 history。

此外，`core/agent/runtime.py` 以动态 `setattr(run_with_context)` 兼容三参数与五参数
Runner。其结果是同一 Agent 执行在不同入口拥有不同的中断、恢复、消息及终态语义，
且 Ideator 若要获得 Codex 式多 Agent 能力，势必继续复制运行时。

本设计选择重铸统一底层，不保留两套 runtime。

## 1. 内核与所有权（已裁可）

### 1.1 总体结构

```text
App Server / ResearchRuntime / Ideator
                 |
            AgentControl
                 |
             AgentKernel
      +----------+-----------+----------------+
      |          |           |                |
AgentRegistry AgentScheduler AgentSession AgentGraphStore
                              |                |
                          AgentTurn   SessionResourcesFactory
```

每棵根 Agent 树只有一个共享的 `AgentControl` 与 `AgentKernel`。子 Agent 不另建控制面，
故树内路径、容量、消息、Turn 与关闭均由同一处裁定。

### 1.2 唯一所有者

| 构件 | 唯一职责 | 不得承担 |
|---|---|---|
| `AgentControl` | 面向调用方的能力门面；校验调用权限并委托 Kernel | 不保存 task、future、memory 或业务辩论状态 |
| `AgentKernel` | 生命周期事务、状态迁移、消息投递、并发与关闭编排 | 不含 Ideator 的提案、评审或裁决规则 |
| `AgentRegistry` | 稳定 `AgentPath`、父子树、名字、role、状态与 reservation | 不运行模型 |
| `AgentScheduler` | 总 Agent 容量与同时运行容量；排队及公平唤醒 | 不决定业务 quorum |
| `AgentSession` | memory、mailbox、journal、future 索引、snapshot、interrupt、rollback 与 force-close | 不拥有多个互相独立的 Agent 身份 |
| `AgentTurn` | 一次请求的执行、事件序列与唯一终态 | 不跨 Turn 保存会话记忆 |
| `AgentGraphStore` | Agent 树、Run 索引及可恢复的控制面事实 | 不保存研究领域事实 |
| `SessionResourcesFactory` | 为 Session 构造 provider、tools、artifact 与外部资源绑定 | 不参与调度与状态迁移 |

现有 `ThreadRuntime` 的状态能力并入 `AgentSession`。独立的
`RuntimeThreadManager` 生命周期删除；app-server 的 Thread API 仅保留为
`AgentSession` 的协议适配层。旧 `AgentControl` 自管的 task、future、event queue 与
context manager 亦删除，不形成兼容 runtime。

### 1.3 身份与执行分离

Agent 是可持续收信、保有 memory 并执行多个 Turn 的稳定身份；Run 是其中一次执行。
两者分别建模：

```text
AgentStatus = STARTING | IDLE | RUNNING | ERROR | CLOSED
RunStatus   = QUEUED | RUNNING | COMPLETED | FAILED | INTERRUPTED
```

`INTERRUPTED` 属于 Run 终态，不是 Agent 终态。一个 Run 被中断后，Agent 回到 `IDLE`，
其 mailbox 与 memory 仍在；只有 `close` 才使 Agent 永久进入 `CLOSED`。

### 1.4 领域边界

`AgentTeam` 只封装成员表、成员并发、quorum 与唯一 arbiter 约束。Ideator 继续唯一拥有：

- proposal、review、revision 与 judgment 的阶段次序；
- 匿名互评、lineage、失败降级规则及审计 artifact；
- 假说字段校验与 `ResearchTree` 登记契约。

辩者人数可配置，arbiter 必须且只能有一个；arbiter 不计入普通成员 quorum。

Ideator 创建的每个辩者 Run 与 arbiter Run 均须获得完整的实验假说输入。所谓“完整”是
逻辑可达而非强制内联复制：`DataProfile`、论文、模型引用、既有 hypotheses 与
`ResearchTree` 快照可组成只读请求，并以稳定 artifact 引用承载大对象。任何辩者不得
因并发或 fork 而只见局部输入；裁决者还须获得全体候选、评审、修案、失败记录及其
lineage。

### 1.5 不变式

- 一个领域概念只有一个生命周期 owner，不留第二套 runtime。
- 根树共享一个 registry、scheduler 与 graph store。
- Agent 总量上限与同时运行上限分开计算。
- 路径、名字及容量在 spawn 前原子预留；spawn 失败时全部回滚。
- app-server、ResearchRuntime 与 Ideator 只能经 `AgentControl` 操作 Agent。
- `ResearchTree` 仍是实验事实源；`AgentGraphStore` 不复制研究事实。
- Provider、工具与 artifact 等资源由 factory 注入，不由 Kernel 硬编码。

### 1.6 本节范围之外

本节不改变模型采样算法、工具协议、ResearchTree v2 schema 或 Ideator 的领域输出。
持久化格式、预算扣减点与权限矩阵由第三、四节规定；具体迁移步骤留待第五节裁可。

## 2. 公开类型与方法（已裁可）

### 2.1 最小公开类型集

| 类型 | 含义 |
|---|---|
| `AgentSpec[RequestT, ResponseT]` | Agent 的不可变能力说明：runner、codec、role 与资源需求 |
| `AgentControl` | 对 Agent 树执行命令的唯一公开控制面 |
| `AgentHandle` | 指向稳定 Agent 身份的 capability |
| `AgentRun[ResponseT]` | 指向一次 Turn/Run 的 capability |
| `AgentRunner[RequestT, ResponseT]` | 执行一个已类型化请求的协议 |
| `AgentCodec[RequestT, ResponseT]` | 请求、响应与 journal artifact 之间的类型化编解码协议 |
| `ForkPolicy` | `none`、`full` 或 `last_n` 历史继承规则 |
| `AgentTeam` | 可配置成员、并发、quorum 与唯一 arbiter 的薄协调器 |

其余 `AgentKernel`、`AgentSession`、`AgentTurn`、registry、scheduler 与 store 均为内部
实现类型，不进入日常业务调用面。

### 2.2 Runner 与 Codec 契约

Runner 只保留一个规范入口；不得再以动态属性维持三参数、五参数两种调用约定。其概念
签名为：

```python
class AgentRunner(Protocol[RequestT, ResponseT]):
    async def run(
        self,
        request: RequestT,
        *,
        session: AgentSession,
        emit: EventSink,
    ) -> ResponseT: ...
```

`session` 由 Kernel 提供，Runner 不创建或关闭它。`emit` 只追加当前 Run 的事件，不能
直接改写状态。Runner 返回领域响应；异常由 Kernel 映射为 Run 终态。

Codec 的概念签名为：

```python
class AgentCodec(Protocol[RequestT, ResponseT]):
    def encode_request(self, value: RequestT) -> ArtifactRef: ...
    def decode_request(self, ref: ArtifactRef) -> RequestT: ...
    def encode_response(self, value: ResponseT) -> ArtifactRef: ...
    def decode_response(self, ref: ArtifactRef) -> ResponseT: ...
```

Codec 只管稳定、可恢复的类型边界，不管执行、重试或状态迁移。`AgentSpec` 持有 Runner
与 Codec 的绑定，因而 app-server、ResearchRuntime 与 Ideator 不再各写结果适配器。

### 2.3 Handle 与 Run

`AgentHandle` 只含 Agent path/id 与受限的 control capability；它不持有 asyncio task、
future、queue 或 memory。所有状态查询均回到 Kernel，避免句柄缓存陈旧事实。

`AgentHandle` 可观察跨 Run 的 Session journal：

```python
handle.events(after_sequence=0) -> AsyncIterator[AgentEvent]
```

该方法不在 Handle 缓存事件、cursor 或状态。它供 app-server 的 Thread 级续读使用；单 Run
事件仍由 `AgentRun.events()` 提供。

`AgentRun` 代表一次 Turn，公开：

```python
await run.wait(timeout=None) -> ResponseT
await run.cancel(reason="caller_cancelled") -> None
run.events(after_sequence=0) -> AsyncIterator[AgentEvent]
await run.summary() -> RunSummary
```

`AgentRun` 同样不拥有执行 task；它以 `run_id` 查询 Kernel。`wait()` 可被多个调用方等待，
但 Run 只提交一次终态与一次响应 artifact。

### 2.4 AgentControl

规范控制面为：

```python
async def create_root(
    spec: AgentSpec[RequestT, ResponseT],
    task: RequestT,
    *,
    name: str = "root",
) -> tuple[AgentHandle, AgentRun[ResponseT]]: ...

async def spawn(
    parent: AgentHandle,
    spec: AgentSpec[RequestT, ResponseT],
    task: RequestT,
    *,
    name: str | None = None,
    fork: ForkPolicy = ForkPolicy.none(),
) -> tuple[AgentHandle, AgentRun[ResponseT]]: ...

async def send_message(target: AgentHandle, message: AgentMessage) -> None: ...

async def followup(
    target: AgentHandle,
    task: RequestT,
) -> AgentRun[ResponseT]: ...

async def wait_agent(
    targets: Collection[AgentHandle],
    *,
    return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED,
    timeout: float | None = None,
) -> AgentWaitResult: ...

async def interrupt(target: AgentHandle, reason: str) -> None: ...
def list_agents(path_prefix: AgentPath | None = None) -> list[AgentSnapshot]: ...
async def close(target: AgentHandle, *, recursive: bool = False) -> None: ...
async def aclose() -> None: ...
```

返回 `(AgentHandle, AgentRun)` 是有意的：调用者同时取得稳定身份与首个 Run，却不会把
两者混为同一生命周期。`followup` 只创建新 Run，故仅返回 `AgentRun`。

### 2.5 命令语义

| 命令 | mailbox | 新建 Run | 保留 Agent/Memory | 可恢复后续执行 |
|---|---:|---:|---:|---:|
| `send_message` | 写入 | 否 | 是 | 是 |
| `followup` | 经同一通信入口提交任务 | 是 | 是 | 是 |
| `interrupt` | 不清空 | 否 | 是 | 是 |
| `close` | 关闭 | 否 | 否 | 否 |

`send_message` 只投递，不触发 Turn。`followup` 与之共享寻址、codec、鉴权器及 mailbox
提交通路，但分别校验 `MESSAGE` 与 `START_RUN` 权项；目标无非终态 Run 时创建 Run，
目标 busy 时抛 `AgentBusyError`，不暗中排入第二个并行 Turn。

`interrupt` 只取消目标当前 Run，等待其提交 `INTERRUPTED` 后返回；Agent 随后回到
`IDLE`。`close` 是永久操作；`recursive=False` 且仍有活子孙时必须失败，防止留下孤儿。
`aclose()` 关闭整棵根树及 Kernel 资源，用于 composition root 的最终清理。

### 2.6 Spawn 与 Fork

spawn 依次完成路径、名字、Agent 总量槽位与首次 Run 槽位的事务性预留，然后构造
Session resources、注册 Agent 并提交首次 Run。任何一步失败，所有预留与半成品记录均
回滚；调用方不得观察到半注册 Agent。

`ForkPolicy` 只有三种：

```python
ForkPolicy.none()
ForkPolicy.full()
ForkPolicy.last_n(turns: int)
```

fork 前必须物化并 flush 父 Agent 已提交历史。复制时过滤内部控制消息、未完成工具调用
及仅属父 Run 的瞬时元数据；`full` 继承全部可见历史，`last_n` 继承最近 N 个完整 Turn，
`none` 只取得显式 task 与 `AgentSpec` 所声明的只读资源。fork 复制的是上下文快照，不是
父 Session 的可变 memory 对象。

### 2.7 AgentTeam 与 Ideator

`AgentTeam` 构造时即校验：

- 普通成员数大于零，且可由 Ideator 配置；
- arbiter 必须且只能有一个；
- arbiter 不计入普通 quorum；
- `1 <= quorum <= member_count`；
- 成员并发不超过 Kernel 的全局并发限制。

Team 只提供成员 fan-out、按 quorum 等待、收集结果与调用 arbiter 的通用动作。阶段
prompt、匿名映射、评审分配、修案 lineage、假说去重及最终 `DebateResult` 仍在 Ideator。

Ideator 不再持有 `_DebateRunner.agents/bindings/histories`。它以 `AgentSpec` 创建辩者与
arbiter，并把同一版本的完整只读研究输入注入各首次 Run；后续阶段以 `followup` 在原
Agent 上继续，借 `AgentSession` 保留各自记忆。裁决阶段的请求另包含全部辩论产物及失败
记录，由唯一 arbiter 生成最终响应。

## 3. AgentKernel 状态机与消息流（已裁可）

### 3.1 方案选择与线性化

内核采用“整棵根 Agent 树一个命令序列器”。备选而未取者为：

- 每个 Session 加锁、Registry 另锁：初期迁移较易，但跨 Agent 操作必须维持复杂锁序；
- 每个 Session 一个 Actor、另设图协调器：扩展性较强，但构件与故障面过多。

单序列器使所有生命周期竞态只有一个裁定点，符合统一底层与低心智负担的目标。该
序列器是确定性内核机制，不是 `AgentTeam` 中执行模型推理的唯一 arbiter。

所有 `spawn`、`send_message`、`followup`、`interrupt`、`close` 与 Run terminal commit
均经此入口。每条命令具有树内单调 `sequence` 与幂等 `command_id`；其线性化点为
`AgentGraphStore` 事务提交。重试同一 `command_id` 只能取得原结果，不得重复创建 Run、
消息或终态。

序列器只执行短事务：校验、状态迁移、journal/store 提交与异步 effect 派发。模型采样、
工具执行、provider 请求与资源构造均在序列器外运行；其成功、失败或取消再作为命令回送
内核。普通 token、工具进度与领域事件写入各自 Session journal，不占用全树生命周期
队列。

### 3.2 Agent 与 Run 状态机

| 事件 | Agent 迁移 | Run 迁移 |
|---|---|---|
| spawn 预留 | 无 -> `STARTING` | 尚不可见 |
| 初始化与首 Run 注册 | `STARTING -> IDLE` | 新建 `QUEUED` |
| scheduler 派发 | `IDLE -> RUNNING` | `QUEUED -> RUNNING` |
| 正常完成 | `RUNNING -> IDLE` | `RUNNING -> COMPLETED` |
| 普通执行失败 | `RUNNING -> IDLE` | `RUNNING -> FAILED` |
| 中断排队 Run | `IDLE -> IDLE` | `QUEUED -> INTERRUPTED` |
| 中断运行 Run | `RUNNING -> IDLE` | `RUNNING -> INTERRUPTED` |
| Session 级致命错误 | 任意非关闭态 -> `ERROR` | 活跃 Run -> `FAILED` |
| close | 任意非关闭态 -> `CLOSED` | 非终态 Run -> `INTERRUPTED` |

`RunStatus` 的合法边为：

```text
QUEUED -> RUNNING -> COMPLETED | FAILED | INTERRUPTED
QUEUED ----------> INTERRUPTED
terminal --------> no transition
```

`QUEUED` 时 Agent 仍为 `IDLE`，但记录唯一 `pending_run_id`。因此 busy 判定依据“是否存在
非终态 Run”，不得只看 `AgentStatus`。同一 Agent 最多有一个 `QUEUED` 或 `RUNNING`
Run。

公开状态不新增 `CLOSING`、`QUIESCING` 或 `PARKED`。Kernel 内部另持 closing fence、
quiescence future、execution phase 与 generation；这些状态用于阻断新命令及丢弃旧 runner
迟到信号。

普通 Run 的模型、工具或领域失败只使 Run 进入 `FAILED`，Agent 随后回到 `IDLE`。
`AgentStatus.ERROR` 仅表示 Session、journal、codec 或资源绑定已损坏，不能安全继续执行。

### 3.3 Mailbox 与 Followup

每个 Agent mailbox 按该目标的命令 `sequence` 严格 FIFO。`send_message` 在消息持久化后
返回；此返回只表示“已接纳”，不表示目标已经消费。它永不创建 Run：

- Agent 空闲时，消息保留至下一 Run；
- Agent 运行时，消息在下一个 `session.checkpoint()` 安全边界注入；
- 若当前 Run 在下一 checkpoint 前终结，消息留给下一 Run。

Session 为 mailbox 维护 `visible_cursor` 与 `committed_cursor`。Runner 可在安全边界看见
新消息，但只有相应 memory checkpoint 成功后才推进 `committed_cursor`。中断、崩溃或
checkpoint 失败时，未提交消息仍可再次交付，不得静默丢失。

`followup` 与消息共用寻址、权限、codec 与持久化入口，但将以下动作合为一个事务：

1. 校验 Agent 非 closing/closed/error，且无非终态 Run；
2. 预留 `run_id` 与 scheduler 位置；
3. 写入 task envelope、`QUEUED` Run 与 `pending_run_id`。

目标 busy 时抛 `AgentBusyError`，事务零变更，mailbox 不留待执行任务。root Agent 允许
`followup`，使 app-server Thread/Turn 适配层无需第二条启动通道。

### 3.4 Terminal Commit 与父子通知

终态以 `run_id + generation` 作 compare-and-set，first-writer-wins。GraphStore 的终态事务
必须原子完成：

```text
CAS Run.status from QUEUED/RUNNING to terminal
+ write unique terminal_marker(run_id)
+ write response/error/reason/context_ref
+ append one terminal event
+ release scheduler lease
+ append parent-completion outbox record
```

response artifact 必须先不可变落盘，再由终态事务提交其引用。CAS 失败的迟到 success、
failure、cancel 或 event 读取既有终态后退出，不得改写结果或重发通知。

子 Run 终结后，outbox 以 `child_run_id` 去重，将 child path、run id、status、result/error
summary、artifact ref 与 lineage 作为 completion 消息投至父 mailbox。父已关闭时仍保留
GraphStore 审计记录，但不投递 mailbox。completion 不自动触发父 Run；`wait_agent` 与
`AgentTeam` quorum watcher 在事务提交后由同一 outbox 唤醒。

### 3.5 Interrupt 与 Close

interrupt 与自然完成均进入同一终态 CAS：

- success 先提交时，Run 为 `COMPLETED`，随后 interrupt 成功 no-op；
- interrupt 先提交时，Run 为 `INTERRUPTED`，迟到 runner 信号按 generation 丢弃；
- 中断 `QUEUED` Run 不启动 runner；
- idle 或重复 interrupt 为幂等 no-op，不伪造新终态；
- `AgentRun.cancel()` 绑定精确 run id；`interrupt(handle)` 只作用于线性化时观察到的当前
  Run。

interrupt 返回前必须满足：terminal 已耐久提交、runner 已停止、memory writer 已静止、
execution lease 已释放、Agent 已安全回到 `IDLE` 或进入关闭流程。`run.wait()` 对
`INTERRUPTED` 抛领域异常 `AgentRunInterrupted`，不得以 `asyncio.CancelledError` 误取消
等待者任务。

`close(recursive=False)` 若发现未关闭子孙或该子树尚有 spawn reservation，整次失败且
零状态变更。`close(recursive=True)` 先原子设置 subtree closing fence，再冻结子树快照，
以后序关闭子孙、最后关闭目标。fence 之后该路径下的 spawn、message 与 followup 均被
拒绝。

并发及重复 close 共享同一 close future。close 先终结 queued/running Run，再关闭
mailbox 与 Session resources，最后写 `CLOSED` tombstone 并释放 resident slot。
`aclose()` 的次序为：全局 closing fence、子树后序关闭、flush journal/outbox/snapshot、
scheduler、resources factory，最后关闭 GraphStore。

### 3.6 调度与 Wait

`max_agents` 统计根树内所有非 `CLOSED` Agent，包含 root；只有写入 `CLOSED` tombstone
后才释放 resident slot。`max_active_runs` 只统计实际获得 execution lease 的 Run，
`QUEUED` 不计。二者不可合并为一个 semaphore。

ready queue 采用 FIFO。Run 调用内核 `wait_agent` 时可转入内部 `PARKED` phase 并释放
execution lease，公共 `RunStatus` 仍为 `RUNNING`；待子 Agent、mailbox 或 timeout 唤醒后
重新排入 ready queue。由此即使 `max_active_runs == 1`，父等子亦不会死锁。

`wait_agent` 在线性化时冻结每个目标的当前非终态 `run_id`；其后创建的新 Run 不属于本次
等待。`FIRST_COMPLETED` 在任一冻结 Run 终结时返回，`ALL_COMPLETED` 在全部终结时返回。
timeout 返回带 `timed_out=True` 的部分状态，不取消任何目标。Agent 已 idle 且无非终态
Run 时视为立即完成。

### 3.7 Crash Recovery

Kernel 启动时先装载最近 snapshot，再按全树 `sequence` 重放其后的 lifecycle journal：

- 已有 terminal marker 的 Run 直接重建 summary、waiter 与 outbox 状态；
- `QUEUED` Run 保持原序重新进入 ready queue；
- crash 时仍为 `RUNNING` 且无 terminal marker 的 Run，以新 generation 提交
  `FAILED(kernel_restarted)`；
- `STARTING` 但未完成注册事务的 reservation 全部释放；
- Session resources 重建失败的 Agent 进入 `ERROR`。

Kernel 不自动重跑 crash 时的 `RUNNING` Run，因为工具可能已有不可逆副作用。未提交
memory checkpoint 的模型文本、工具进度与消息消费只保留为 audit trace，不晋升为会话
memory；已提交 checkpoint 与未消费 mailbox 则完整恢复。

进程内单序列器只保证逻辑单写；跨 crash 的 exactly-once 由 terminal marker、CAS 与可
重放 outbox 共同保证。恢复发布 outbox 时仍以 `child_run_id` 去重。

### 3.8 验收不变式

- 同一 Agent 永不同时存在两个非终态 Run；
- 每个 Run 恰有一个 terminal marker 与一个 terminal event；
- interrupt 返回后旧 runner 不可再写 memory 或事件；
- busy followup 不改变 mailbox、Run 表或 scheduler；
- completion 在 crash 前后至多向父 mailbox 投递一次；
- 父 Run 等待子 Run 时不会因并发上限为一而死锁；
- crash 不会自动重放可能有副作用的 `RUNNING` Run；
- `CLOSED` Agent 永久拒绝新消息、Run 与子 Agent。

## 4. 权限、配置、错误与持久化（已裁可）

### 4.1 Capability 模型

内核采用“`AgentControl` 即 capability 门面”，不另建通用 RBAC 或策略引擎。固定角色
矩阵不足以表达 Team 匿名通信；通用 RBAC 又超出单机 Athena 所需，故二者均不采用。

每个 Control view 内部绑定一项不可伪造、默认不可转授的授权：

```text
grant_id
principal_id
tree_id
subject_agent_id + generation
scope: SELF | SUBTREE | TEAM | EXACT_TARGET
rights: DISCOVER | OBSERVE | MESSAGE | START_RUN |
        SPAWN_CHILD | INTERRUPT_RUN | CLOSE_AGENT |
        CLOSE_SUBTREE | KERNEL_ADMIN
```

grant、caller identity 与 control nonce 不进入公开 DTO，不得编码进 message、memory、
artifact、snapshot 或 fork 历史。每条命令在 Kernel 序列器内同时完成目标解析、实时图/Team
关系检查、权限检查与状态提交，避免“先查后用”的 TOCTOU。

`AgentPath` 只用于显示及相对寻址，不是权限凭证。Handle 同时绑定稳定 UUID、tree id、
generation 与 control nonce；路径即使日后复用，旧 Handle 亦不得命中新 Agent。模型工具
入口拒绝裸 UUID、thread id 与任意绝对路径。

默认授权矩阵为：

| 调用者 | 目标 | 默认授权 |
|---|---|---|
| composition root | 整棵绑定树 | 全部权项、`create_root`、root `followup` 与 `aclose` |
| 普通 Agent | 自身 | 观察自身；不得自行 followup、interrupt 或 close |
| 普通 Agent | 直属子 Agent/自身子树 | 生直属子 Agent；观察、等待及管理自身后代 |
| 子孙 Agent | 父/祖先 | 仅上行 `MESSAGE`；无发现、启动、中断或关闭权 |
| 父/祖先 | 后代 | `OBSERVE/MESSAGE/START_RUN/INTERRUPT_RUN/CLOSE_AGENT` |
| Team member | peer | 仅经匿名 Team channel 使用 `MESSAGE` |
| Team coordinator/Ideator | Team 成员及 arbiter | 创建、观察、启动、中断与关闭 |
| arbiter | 普通成员 | 只读候选、评审、失败及 lineage；无管理权 |
| 跨 Team | 任意 | 全拒绝；首版不设跨 Team grant |

root Agent 与 composition root 是两个 principal。模型中的 root Agent 不持有
`KERNEL_ADMIN`，不能关闭整树或控制宿主。app-server 与 ResearchRuntime 只获得其工作流
所需的受限 Control view。

普通 Agent 调 `spawn(parent, ...)` 时，Kernel 强制 `parent == caller`；仅 composition
root 与 Team coordinator 可在授权范围内指定其他 parent。子 Agent 的 scope 与 rights
始终取父授权和子 Spec 请求的交集，只能衰减，不能扩权。

`MESSAGE` 与 `START_RUN` 是不同权项。`send_message` 与 `followup` 共用命令通路及鉴权器，
但 peer 消息权绝不蕴含启动昂贵模型、工具或新 Run 的权力。递归 close 另需
`CLOSE_SUBTREE`，不得由普通 `CLOSE_AGENT` 推导。

Team member 只看匿名 alias 与允许公开的状态；真实 AgentPath、内部 id、task prompt 与
artifact ref 不由 `list_agents` 暴露。Team channel 由 Kernel 写入真实 author、team、kind
与 grant id，模型只能提供 payload，不能伪造 `system`、`completion` 或 `control` 消息。

### 4.2 数据、Artifact 与工具边界

“全量注入”专指实验科学上下文，包括 `DataProfile`、论文、模型引用、ResearchTree、
既有 hypotheses、候选、评审与 lineage。以下内容永不注入模型：

- provider/API 凭证、环境密钥与 capability grant；
- 其他 Session 的私有 memory；
- 匿名 peer 的真实路径及身份映射；
- traceback、原始系统错误与未脱敏环境变量。

工具调用必须在实际执行文件、网络、shell 或 artifact 读取的 sink 再次检查 caller
capability；prompt 或模型输出声称“已授权”不产生任何权限。

ArtifactRef 是内容完整性定位符，不是授权凭证。Session 只可解析由初始请求、已授权工具
结果或 Team coordinator 显式授予的引用；resolver 在读取前检查该 Session 的 allow-set。
凭证与 secret 不得写入 ArtifactStore。大对象继续以 SHA-256 校验，GraphStore 只记录短
引用。

mailbox 对单条 payload、每来源速率、目标 item 数与总字节设限。用户 lane 达限时原子
拒绝且零变更；completion 使用独立 system lane，不受用户消息占满影响，其最大规模仍受
全树 Agent 上限约束。

### 4.3 Research BudgetConfig

原公开预算快照型取消，改为不可变配置：

```python
class BudgetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_experiments: int = Field(default=20, gt=0)
    max_no_improve: int = Field(default=5, gt=0)
```

`BudgetConfig` 只表达用户所定上限，不含 `remaining`、`no_improve_streak` 或
`is_exhausted` 等可变字段。它仍由 `athena.research` 所有，不进入 AgentKernel。

ResearchRuntime 内部 ledger 记录 `consumed_experiments` 与 `no_improve_streak`，并与研究
运行时 checkpoint 一同持久化。剩余额度及是否耗尽均由 `BudgetConfig + ledger` 计算，
不再把运行状态伪装成 config，也不再公开第二个 Snapshot DTO。

ResearchTree 仍保存实验事实，不保存运行控制计数；AgentGraphStore 亦不复制研究预算。
Supervisor 读取只读的计算结果，不直接修改 config 或 ledger。

### 4.4 Agent 运行资源配置

Kernel 运行资源由 composition root 的单一内部配置记录控制，不增加公开配置层。安全
默认值为：

```text
max_agents=32              max_active_runs=8
max_spawn_depth=4          max_message_bytes=64 KiB
max_mailbox_bytes=4 MiB    max_tool_calls_per_run=64
max_event_bytes=256 KiB    max_run_seconds=3600
```

Ideator 默认仍只建立三名辩者与一名 arbiter；`max_agents=32` 是整树硬上限，并非默认
成员数。

parent、Team 与 `AgentSpec` 的限制逐层取最小值。Run 入队时预留估算输入 token、最大
输出 token 及工具调用额度；终结后按 provider usage 结算并退还未用额度。provider 未
报告 usage 或进程在结算前崩溃时，未结算额度按预留上限扣除，以防超支。

预算不足发生在 Run 建立前时，命令失败且零状态变更；执行中耗尽时，Run 进入
`INTERRUPTED(reason=budget_exhausted)`。已实际执行的工具调用及外部副作用不退款。

Research `BudgetConfig` 与 Agent 资源限制不可合并：前者回答“尚可做多少实验”，后者回答
“一棵 Agent 树可占多少运行资源”。

### 4.5 错误与重试

公开异常维持小型层次：

```text
AgentError
+-- AgentCommandError(code, retryable, details)
|   `-- AgentBusyError
+-- AgentRunFailed
`-- AgentRunInterrupted
```

稳定错误 code 为 `NOT_FOUND`、`PERMISSION_DENIED`、`BUSY`、`CLOSED`、
`LIMIT_REACHED`、`BUDGET_EXHAUSTED`、`INVALID_REQUEST`、`CODEC_ERROR`、
`STORE_UNAVAILABLE` 与 `INTERNAL`。

越出 capability 范围的目标统一返回 `NOT_FOUND`，避免泄露树结构；目标已在可见范围内但
缺少具体动词权时才返回 `PERMISSION_DENIED`。app-server 将 code 映射为稳定 RPC error，
不把 Python 异常消息直接暴露给客户端。

公开错误只含稳定文案、code 与安全 details。prompt、credential、traceback 与 provider
原始响应经脱敏后写入受保护的 `error_ref`，不得进入 RPC、mailbox 或普通事件。

Kernel 不自动重试模型或工具副作用。控制命令只可用原 `command_id` 幂等重试；存储提交
结果不明时先查 command ledger。工具首版不自动重试，即使调用方认为其幂等；后续若增
重试能力，须由 ToolSpec 显式声明并复用同一 invocation id。

### 4.6 Fork 继承矩阵

| 内容 | `none` | `full` | `last_n` |
|---|---:|---:|---:|
| 已提交历史/memory 值 | 否 | 全部 | 最近 N 个完整 Turn |
| ArtifactRef | 仅显式 task 所给 | 已授权历史所引用 | 所复制历史所引用 |
| mailbox 与消费游标 | 否 | 否 | 否 |
| active/queued Run | 否 | 否 | 否 |
| capability/grant | 否；重新衰减签发 | 否；重新衰减签发 | 否；重新衰减签发 |
| Team membership | 否；须由 coordinator 登记 | 否 | 否 |
| provider client/credential | 否；factory 重建 | 否；factory 重建 | 否；factory 重建 |
| tools/environment | 父授权与子 Spec 交集 | 同左 | 同左 |

`last_n(turns)` 要求 `turns >= 1`；大于现存完整 Turn 数时复制全部可用 Turn，零 Turn 使用
`none` 表达。父 Agent 运行中 fork 时，只取最近已提交 checkpoint，不复制在飞采样、待批
工具或半成品。fork 保留 `forked_from` provenance，但子 Session 的 memory 与资源皆为
独立对象。

### 4.7 AgentGraphStore 与 Codec

本地 `AgentGraphStore` 采用 SQLite WAL，默认文件为 `.athena/agent-graph.sqlite3`。不以
JSONL 或 ArtifactStore 另造事务数据库。SQLite 启用 foreign keys、WAL 与
`synchronous=FULL`，由 Kernel 单写序列器提交。

规范表域为：

```text
trees / agents / runs / mailbox / lifecycle_events
commands / terminal_markers / completion_outbox / resource_ledger
```

大型 request、response、trace 与 snapshot 继续进入现有 SHA-256 ArtifactStore；SQLite
只存版本化短 DTO 与 ArtifactRef。terminal marker、scheduler lease、parent outbox 与
resource settlement 必须按第三节在同一事务中提交。

所有持久化 DTO 使用严格 JSON/Pydantic，包含 `schema_version` 与 `codec_id`，并设
`extra="forbid"`。禁止 pickle。未知版本、未知字段或 codec 缺失立即失败；数据库版本以
SQLite `user_version` 管理，只接受显式迁移，不作静默兼容。

Runner、Control、Handle、Future、Queue、provider client 与 credential 永不序列化。
GraphStore 只存 `spec_key`、role、配置摘要与 codec version；恢复时由
`SessionResourcesFactory` 重建活对象，解析失败则 Agent 进入 `ERROR`。

首版不删除 lifecycle、terminal 或 closed tombstone。snapshot 只加速恢复，不取代规范
记录；completion outbox 全部投递并持久化 snapshot 后方可 checkpoint SQLite WAL。

### 4.8 威胁模型与安全结论

运行环境按“单机、单项目、宿主用户可信；模型输出、外部论文/网页与消息 payload 不可信”
建模。本设计不提供远程多租户 AuthN；若 app-server 日后暴露为远程服务，须另立用户身份
与租户隔离设计。

| 风险 | 等级 / 置信度 | 规范处置 |
|---|---|---|
| 裸 id/path 导致跨树或跨分支控制 | High / High | caller-bound capability；Kernel 内授权 |
| `MESSAGE` 提升为 `START_RUN` | High / High | 权项分离；失败零变更 |
| 跨 Team 横移及匿名性破坏 | High / High | 默认拒绝；匿名 Team channel |
| root Agent 与宿主管理权混淆 | High / High | principal 分离；admin 只属 composition root |
| grant 经 fork/message 泄露或陈旧重放 | High / Medium | 不序列化、不可转授、绑定 generation |
| secret、prompt 与 trace 外泄 | Medium / High | 全入口脱敏；secret 永不持久化或注入 |
| spawn/mailbox/tool 资源耗尽 | Medium / High | 有界配置、预留、结算与速率限制 |

未定义权限时的设计安全结论为 `needs changes`；纳入本节后为 `pass`。实现验收必须覆盖
伪造 Handle、跨树、跨 Team、旧 generation、匿名性、预算竞态、恶意 mailbox、未知
schema、pickle 拒绝与 secret 扫描。

### 4.9 验收不变式

- 任意模型输出均不能扩大 caller capability；
- `MESSAGE` grant 不能创建 Run 或消费模型/工具预算；
- 子 Agent、fork 与 Team member 的权限只可衰减；
- 跨树及跨 Team 目标不可被发现、观察或控制；
- `BudgetConfig` 不含运行状态，ResearchRuntime ledger 可从 checkpoint 恢复；
- Agent 资源预留、终态与退款在同一事务中至多结算一次；
- GraphStore 不含 credential、活对象或 pickle payload；
- 未知 schema/codec 明确失败，已提交数据库仍可按旧版本只读诊断；
- completion system lane 不因用户 mailbox 满而丢失；
- 公开错误与事件不含 prompt、traceback、secret 或未授权 artifact ref。

## 5. 迁移、验收与退场（已裁可）

### 5.1 单轨迁移原则

迁移采用“一条实施分支、一个发布切点、一个状态 owner”。新 Kernel 可在尚未接线的内部
模块中完成，但不得用 feature flag、环境变量或依赖注入让生产 workload 在 legacy 与
Kernel 间选择；不得 shadow write、双 journal、双 terminal commit 或双 outbox。首次把生产
入口接至 Kernel 的同一合并单元，必须同时移除该入口至旧 runtime 的路径。整项迁移完成
前不发布其中间状态。

唯一可长期保留的兼容层是 app-server 的无状态协议翻译器。它只做 DTO、错误码与事件形状
转换，不持有 Agent/Run 状态、id 映射、pending task、memory、queue、future 或 close
所有权。协议 `thread_id` 直接使用稳定 Agent id，`turn_id` 直接使用 Run id；Kernel 是
唯一事实源。

现有 app-server v1 有三个不能用门面伪装的语义缺口：

- `thread/start` 创建空闲 Thread，而 `create_root(spec, task)` 原子创建身份与首 Run；
- `thread/fork(after_turn_id)` 创建空闲子 Thread，而 `spawn(..., task, fork)` 原子创建首 Run；
- v1 的订阅是跨 Turn 的 Thread journal，而 `AgentRun.events()` 是单 Run 事件流。

故不保留 v1/v2 双模式，而作一次协议 v2 硬切：

| 方法 | v2 语义 |
|---|---|
| `thread/start` | 请求增加 `request_ref`；原子返回 `thread_id` 与首个 `turn_id` |
| `turn/start` | 只为既有 idle Agent 创建后续 Run；busy 时返回稳定 `BUSY` 且零变更 |
| `thread/fork` | 以 `fork_policy` 取代 `after_turn_id`，并携带新 `request_ref`；原子返回子 `thread_id` 与首个 `turn_id` |
| `turn/interrupt` | 精确绑定 `turn_id/run_id`，不得按“当前最新 Run”取消 |
| `thread/subscribe` | 方法与 `after_sequence` 语义保留；直接读取 Kernel 所有的 AgentSession journal |
| `thread/unsubscribe` | 只释放传输订阅，不改变 Agent/Run 生命周期 |

为承接跨 Run 订阅而不另造 Manager，`AgentHandle` 提供一个只读方法：

```python
handle.events(after_sequence=0) -> AsyncIterator[AgentEvent]
```

它读取 Kernel 所有的 AgentSession journal；`AgentRun.events()` 仍只读取单 Run 事件。该方法
不在 Handle 缓存事件、cursor 或状态。

初始化握手只接受 `protocol_version=2`；旧客户端得到明确版本错误。这样 app-server 仍可保留
Thread/Turn 领域词汇，却不需 `RuntimeThreadManagerCompat`、`ThreadHandleCompat`、假 Run 或
第二套生命周期。

### 5.2 实施次序

以下次序是所有权迁移顺序，不是可分别发布的兼容阶段：

1. **冻结可观察契约。** 先以 golden fixtures 固定新 v2 的请求、响应、错误、事件顺序、
   fork、interrupt、reconnect 与 shutdown 行为；以现有测试补出 legacy 所含竞态事实，
   但不把旧 Python 类型列为新契约。
2. **落统一内核与存储。** 实现 `AgentSession`、单序列器、registry、scheduler、
   `AgentGraphStore`、codec、terminal CAS、completion outbox 与恢复；尚不接生产入口。
3. **迁 Runner 与 Session 所有权。** 把采样循环改为唯一 `AgentRunner.run()`；memory、
   compaction、rollout、mailbox cursor、interrupt 与 checkpoint 全归 `AgentSession`。
   删除动态 `run_with_context` 双签名及以 `setattr` 维持的适配。
4. **迁 app-server。** `ExecutionAdapter` 直接持有受限 `AgentControl`；订阅组件只读 Kernel
   journal。生命周期 composition root 从 `owns_manager` 一次改为 `owns_control`，关闭时只
   调一次 `control.aclose()`。协议 v2 与服务端、Python client、GUI client 同批更新。
5. **迁业务调用方。** Ideator 以 `AgentSpec + AgentTeam + followup` 运行；app-server、
   ResearchRuntime 与其他调用者也只经 `AgentControl`。不得留下仅供旧调用方使用的 shim。
6. **迁 Research 预算。** `SEARCH_START` 只构造 `BudgetConfig`；实验 admission 与
   `no_improve_streak` 改由 ResearchRuntime ledger 幂等结算，Supervisor 只读计算结果。
7. **尽迁尽删。** 删除旧 runtime、manager、submission loop、双 Runner 签名、旧控制面
   状态及其直接类型测试；把行为测试迁至 Kernel 契约。清除旧导出、示例、注释与生产引用，
   再运行全套门禁后方可合并。

### 5.3 所有权搬迁与删除界线

| 旧所有者/内容 | 唯一新所有者 | 删除条件 |
|---|---|---|
| `ThreadRuntime` 的 memory、compactor、rollout、journal、active Turn 与 close 状态 | `AgentSession` / `AgentTurn` | app、订阅及测试均已只走 Kernel，且竞态与恢复测试等价 |
| `RuntimeThreadManager` 的 handle registry、id、fork 与全局关闭 | `AgentRegistry` / composition-root `AgentControl` | `ExecutionAdapter` 不再 `manager.get()`，Ideator 不再创建私有 manager |
| 旧 `AgentControl` 的 semaphore、task、future、event queue 与独立 memory | 新 capability `AgentControl` / Kernel | 新控制面成为唯一公开导出，所有调用方同批改完 |
| `_DebateRunner._agents/_bindings/_histories` | `SessionResourcesFactory`、`AgentSpec`、`AgentSession.memory` | codec、binding、history 各有唯一 owner，Ideator 不再 import 或 monkeypatch runner |
| `_DebateRunner.put_request/read_result` | 类型化 `AgentCodec` | 所有 debate stage 只经 codec 编解码 |
| 旧预算快照型的上限字段 | `BudgetConfig` | Research 构造、事件、Supervisor 与测试均已使用 config |
| 旧预算快照型的耗用字段 | ResearchRuntime ledger | admission、checkpoint、恢复与幂等结算测试通过 |

`AgentTeam` 只关闭其拥有的 Team 子树，不关闭共享 Kernel。Ideator 一轮结束时不得再调用整树
`aclose()`；composition root 才拥有 Kernel 最终关闭权。

### 5.4 数据与 Schema 迁移

现有 `ThreadRuntime` 的 Queue、Task、Future 与在飞采样皆为进程内对象，不作对象级迁移。
切换前须设置 admission fence，等待或中断所有活 Turn 与 Research workflow，提交已有 artifact
后停服；若仍有非终态工作，迁移拒绝开始。不得把假终态写入新库以模拟在飞状态。

`.athena/agent-graph.sqlite3` 是新规范库。首次启动在单一事务中创建 schema、写
`user_version` 并执行 integrity check；以后只接受显式、前向、可重复 dry-run 的迁移。
迁移前复制数据库及 Research checkpoint，迁移失败时事务回滚，原文件不改。

现有 SHA-256 artifact 可原样复用；只把已授权的 `ArtifactRef` 写入新 GraphStore，不复制
大对象。`ResearchTree` schema 不变，且不把研究事实搬入 GraphStore。

当前版本没有独立持久化的旧预算快照 checkpoint，故不发明 legacy budget 数据
转换器。切换后新研究 Run 从 `BudgetConfig` 与空 ledger 开始；切换前的活研究 Run 必须排空
或明确中断。日后持久化的只会是版本化 `BudgetConfig + ledger` checkpoint。

### 5.5 验收矩阵

| 门禁 | 必测场景 | 通过条件 |
|---|---|---|
| 单序列器 | followup/terminal、interrupt/success、spawn/close 两两竞态；重复 command id | sequence 唯一递增；任一 Agent 至多一个非终态 Run；失败命令零变更 |
| Mailbox | 多来源并发投递、checkpoint 边界、user lane 满、completion system lane | 同一目标严格 FIFO；busy followup 不入 mailbox；用户消息满不阻塞 completion |
| Terminal/outbox | success、failure、interrupt、迟到 runner；terminal commit 后、outbox ack 前崩溃 | 每 Run 恰一终态；资源至多结算一次；父方按 child run id 恰收一次 completion |
| 恢复 | command、artifact、terminal、outbox、snapshot 各边界注入 crash | 重放等于无 crash 参考状态；未消费消息不丢；有副作用的 RUNNING 不自动重跑 |
| 权限 | 伪造 Handle、旧 generation、跨树/跨 Team、`MESSAGE` 提权、fork/spawn 衰减 | 越权目标不可发现；消息不能启动 Run；alias、grant、prompt 与 secret 不泄露 |
| Ideator | 可变辩者、零/一/二 arbiter、quorum、成员失败、匿名互评、timeout | 只接受一个 arbiter；低于 quorum 不裁决；符合 quorum 时该 arbiter 只调用一次；全员科学输入一致 |
| app-server v2 | start/fork/turn/interrupt/subscribe/reconnect/shutdown、错误映射及 GUI E2E | DTO golden 一致；事件续读无重无漏；精确 interrupt；旧 v1 明确拒绝而非误解释 |
| Research | frozen `BudgetConfig`、并发 admission、改进/未改进、checkpoint/crash | config 无运行状态；ledger 按 experiment id 幂等；不超上限；恢复前后一致 |

竞态测试使用 barrier 与 fault hook 固定事务边界，不以 `sleep` 猜时序。除定向门禁外，必须
通过当前 Python 全套回归及被 v2 改动的 GUI 构建与 E2E。

### 5.6 发布与回滚

发布只含一个切点：停服并 fence admission，排空旧工作，备份，运行 schema preflight，部署
同时更新的 Kernel、app-server v2 与客户端，再以 create/followup/fork/interrupt/reconnect
烟测。未通过即不开 admission。不存在“先默认新内核、旧树继续排空”的发布期双 owner。

回滚触发条件包括不变式破坏、权限或匿名泄漏、预算超扣、协议误解释、恢复不等价及 store
提交不可靠。回滚时重新 fence admission，保留新 artifact 与新数据库作只读诊断，恢复切换前
数据库/checkpoint 及旧二进制。旧 runtime 不得读取已由新 Kernel 推进的数据；切换后新建的
Agent 状态不能倒灌旧 runtime，须明确标为不可续跑。若已有不可丢失的新状态，则停止服务并
前向修复，不作破坏性降级。

### 5.7 完成定义

迁移须同时满足：

- 每棵 root tree 恰有一个 registry、scheduler、journal writer、terminal writer 与生命周期
  序列器；
- app-server、ResearchRuntime 与 Ideator 不拥有 Agent 的 Queue、Task、Future、memory 或
  状态机，只经受限 `AgentControl`；
- `src/`、`test/`、`tests/` 中对 `ThreadRuntime`、`RuntimeThreadManager`、`ThreadHandle`、
  `_DebateRunner`、动态 `run_with_context` 与旧预算快照公开型零引用；
- 无 legacy backend 开关、双写、shadow runtime、compat runtime 或待后删 shim；
- app-server v2 golden、Kernel 竞态/恢复、权限、Ideator、Research budget、GUI E2E 与现有
  全套回归皆通过；
- 数据迁移 dry-run、发布烟测、失败回滚及 secret scan 均以复制的真实 fixture 演练通过；
- 文档、公开导出、示例及错误码已与唯一新实现一致，无高危未决项。

## 参考实现与现状证据

Athena：

- `src/athena/core/agent/control.py`
- `src/athena/core/agent/runtime.py`
- `src/athena/app_server/thread_runtime.py`
- `src/athena/ideator/ideator.py`

Codex 参考：

- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/agent/control/spawn.rs`
- `codex-rs/core/src/agent/control/execution.rs`
- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/status.rs`
- `codex-rs/core/src/tools/handlers/multi_agents_v2/message_tool.rs`
- `codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs`
- `codex-rs/core/src/tools/handlers/multi_agents_v2/list_agents.rs`
- `codex-rs/core/src/tools/handlers/multi_agents_v2/interrupt_agent.rs`

所取原则为共享控制面、稳定 AgentPath、spawn reservation、消息与 followup 同路、fork
前 flush、interrupt 不等于 shutdown，以及 completion 结果回送父 Agent。Athena 不照搬
Codex 的 Rust 类型层次，而保留 Python 项目所需的最小公开类型集。
