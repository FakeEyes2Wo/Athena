# Athena Rust 迁移设计（审校版）

> 状态：目标设计，不代表当前 `athena-rust` 已经实现。
> 日期：2026-07-25
> 源范围：`src/athena` 中当前已有可执行行为的模块。
> 目标目录：仓库根目录 `athena-rust/`，不迁入 Python 的 `src/`。

## 1. 问题与目标

现有 Python 实现已经形成四条可验证的核心链路：

1. 协议请求经过 Transport 和 MessageProcessor，进入 ThreadManager。
2. 每个 Thread 通过 SubmissionLoop 串行提交 Turn，并产生可订阅事件。
3. Agent 流式读取模型响应、执行工具、维护上下文并记录 rollout。
4. GitWorkspace 与 ResearchTree 提供当前已经落地的实验工作区和研究树能力。

Rust 迁移的目标不是逐行翻译，而是在保持外部协议和已有行为兼容的前提下：

- 用所有权和 Actor 边界替代共享可变对象；
- 为请求、Turn、事件和关闭流程建立明确状态机；
- 保留 Python 版的背压、订阅回放、工具顺序和 rollout 恢复语义；
- 通过 Python/Rust 黄金样本和行为测试证明兼容性；
- 允许迁移期间随时切回 Python。

## 2. 范围

### 2.1 纳入本次迁移

| Python 模块 | 迁移内容 |
|---|---|
| `core/schemas.py` | 领域 DTO、枚举、非空约束和交叉字段校验 |
| `core/tool_types.py`、`core/tool.py` | ToolSpec、ToolContext、ToolResult、Tool、Registry |
| `memory/context_manager.py` | 消息上下文、token 估算、snapshot/rollback |
| `memory/compaction.py` | 上下文压缩和并发版本检查 |
| `memory/rollout.py` | JSONL 写入、compaction checkpoint、崩溃恢复 |
| `core/agent/agent.py` | 流式采样、工具调用循环、并发屏障 |
| `core/agent/provider.py` | OpenAI-compatible Chat Completions 流 |
| `core/agent/subagent.py` | 子 Agent 生命周期、消息和事件队列 |
| `app_server/*` | 协议、传输、Client/Server、事件、Runtime、生命周期 |
| `core/gitutils/workspace.py` | 当前已有的 worktree、diff、commit、remove 行为 |
| `core/research/research_tree.py` | 当前已有的树、节点、实验和 prompt 行为 |
| `agents/demo_agent.py` | 示例工具和 Agent 组装 |
| `execution/handlers.py` | 已有 ThreadManager/Submission 抽象所表达的行为 |

### 2.2 明确不纳入本计划

以下文件目前只有“待实现”说明、空实现或未来设计，本计划不创建对应空 crate，也不为其虚构验收标准：

- `agents/control/scheduler.py`
- `agents/data_analysis/plot_agent.py`
- `agents/policy/supervisor.py`
- `agents/prepare/data_agent.py`
- `agents/search/code_agent.py`
- `agents/search/research_agent.py`
- `execution/agent_monitor.py`
- `execution/sandbox_runtime.py`
- `research/proximity.py`
- `research/ranking.py`
- `storage/artifact_store.py`
- `storage/state_store.py`
- `utils/single_turn_chat.py`
- `workflows/prepare/*`
- `workflows/search/*`
- `workflows/validate/*`
- `workflows/report/*`
- `workflows/prompts.py`

`ResearchTree` 中尚未实现的持久化 TODO 同样不在本次范围内；只迁移其当前可执行行为。

## 3. 设计原则

1. **兼容外部，重构内部**：协议字段、方法名、错误码和事件顺序保持兼容，Python 内部类结构不要求照搬。
2. **状态单一所有者**：Thread 状态由每 Thread 一个 Actor 独占，不使用 `Arc<Mutex<ThreadRuntime>>`。
3. **可靠控制面**：Request、Response、ServerRequest 和 Event 不静默丢失；只有 Notification 明确允许尽力而为。
4. **上下文事务化**：Turn 成功提交新 Context，失败丢弃工作副本，中断按既定策略持久化已产生消息。
5. **无空壳完成**：空 crate 编译不算完成；每个任务必须有对应行为测试。
6. **先契约后实现**：先生成 Python 黄金样本，再实现 Rust。

## 4. Workspace 布局

```text
athena-rust/
├─ Cargo.toml
├─ Cargo.lock
├─ rust-toolchain.toml
├─ crates/
│  ├─ athena-types/
│  ├─ athena-protocol/
│  ├─ athena-tools/
│  ├─ athena-memory/
│  ├─ athena-runtime/
│  ├─ athena-agent/
│  ├─ athena-server/
│  ├─ athena-workspace/
│  └─ athena-research/
└─ tests/
   └─ fixtures/
      ├─ protocol/
      ├─ messages/
      └─ rollout/
```

包名必须使用 `athena-*`，Rust 导入名自然转换为 `athena_*`。禁止使用 `core`、`test`、`server` 这类容易与标准库或通用包冲突的裸包名。

Workspace 根配置统一声明：

- `edition = "2024"`
- 明确 `rust-version`
- 公共依赖版本
- `unsafe_code = "forbid"`
- Clippy 和 Rust lint
- release/debug profile

## 5. 依赖方向

```text
athena-types
├── athena-protocol
├── athena-tools
├── athena-memory
├── athena-runtime ── athena-agent
├── athena-workspace
└── athena-research

athena-server ──> protocol + runtime
athena-agent  ──> tools + memory + runtime
athena-research ──> types + workspace
```

约束：

- `athena-runtime` 不依赖具体 Agent；
- `athena-agent` 实现 Runtime 定义的 `TurnRunner`；
- `athena-memory` 通过 `Summarizer` trait 调用摘要模型，不依赖 Agent；
- Server 不引用 Agent 内部类型；
- 不允许依赖环。

## 6. 核心类型设计

### 6.1 `athena-types`

职责：

- `ArtifactRef`
- `CommitHash`
- `ThreadId`、`TurnId`、`SessionId`
- `MetricSpec`、`TaskMetaData`、`DataCard`
- `Hypothesis`、`HypothesisStatus`
- `ExperimentPlan`
- `AthenaThread`、`AthenaTurn`

设计要求：

- 非空字符串使用经过校验的 newtype，而不是 `type ArtifactRef = String`；
- `HypothesisStatus` 使用 `SCREAMING_SNAKE_CASE` 序列化；
- `MetricDirection` 使用 `snake_case`；
- `patience_grant > 0` 时必须存在 `patience_evidence_ref`；
- Thread/Turn status 使用枚举，协议边界按 Python 当前字符串值序列化；
- 仅数据 DTO 派生 Serialize/Deserialize，运行时对象不强制序列化。

### 6.2 `athena-protocol`

职责：

- 稳定方法名；
- Request、Response、Notification、ServerRequest、EventNotification；
- Operation 参数和结果 DTO；
- `ErrorCode`、`RpcError` 和内部错误到协议错误的映射。

兼容规则：

- 错误码数值与 Python 完全一致；
- DTO 拒绝未知字段；
- `request_id` 非负；
- 当前 Python 允许 Response 的 result/error 都为空，Rust v1 不额外收紧；
- 对外错误不得包含 prompt、API key、文件内容或 traceback；
- 方法名必须包括 approval、user input 和 tool call server request。

## 7. Transport 与 Server

### 7.1 通道

使用三个独立有界 `tokio::mpsc` 通道：

| 通道 | 内容 | 满载语义 |
|---|---|---|
| client → server | Request、Notification、ServerRequestReply | Request 等待容量并超时；Notification 可丢弃；Reply 可靠等待 |
| server → client control | Response、ServerRequest、TransportControl | 可靠等待 |
| server → client event | EventNotification | 独立可靠等待 |

禁止把 Event 改成 `try_send` 静默丢弃。

### 7.2 生命周期

- Transport 创建后拆分为 ClientHalf 和 ServerHalf；
- ready 使用 `watch` 或等价状态通道；
- 关闭通过 CancellationToken 和 drop Sender 传播，不依赖向已满队列硬塞 `None`；
- close 必须幂等；
- ClientWorker 在关闭时令所有 pending request 以 CLOSED 失败；
- request ID 溢出必须返回错误。

### 7.3 MessageProcessor 状态机

```text
Created → Initializing → Ready → Draining → Terminated
```

- 初始化必须使用保留 request ID 0；
- Ready 前拒绝业务请求；
- inflight request ID 不允许重复；
- Semaphore 控制最大并发；
- shutdown 先停止接收新业务请求，再排空或取消已有任务；
- ServerRequest reply 通过独立 pending map 解析。

## 8. Event 与订阅

### 8.1 EventJournal

Journal 内部维护：

- `Vec<Event>` 或带保留策略的等价结构；
- 下一 sequence；
- tail sequence 的 `watch::Sender<u64>`。

`append(EventDraft)` 由 Journal 自己分配 sequence，禁止调用方先读取 `next_sequence` 再回写，从而消除竞态。

订阅读取流程：

1. 按 cursor 从 Journal 回放已有事件；
2. 订阅 tail 更新；
3. 收到更新后继续按 cursor 读取；
4. 即使通知合并，Journal 中的数据也不会丢失。

### 8.2 Subscription 与 FairMux

- 每个 Subscription 拥有一个发送端；
- FairMux 独占所有接收端；
- add/remove 通过控制命令发送给 Mux Actor；
- round-robin 每次最多从一个 ready subscription 取一条；
- 无数据时事件驱动等待，不进行固定周期轮询；
- 移除时先标记 inactive、停止 pump，再从 Mux 删除。

## 9. Memory 与 Rollout

### 9.1 消息模型

使用可无损表达现有 PydanticAI 消息的结构：

```text
ModelMessage
└─ parts: Vec<MessagePart>
   ├─ SystemPrompt
   ├─ UserPrompt
   ├─ Text
   ├─ ToolCall
   └─ ToolReturn
```

不能简化为单个 `role + content`，否则会丢失同一 Assistant 消息中的文本和多个工具调用。

### 9.2 ContextManager

保持以下不变量：

- `tokens == sum(estimate(message))`
- append、replace_range、rollback 后 version 单调递增；
- 对外返回 clone，外部不能绕过 token 统计修改内部消息；
- 工具结果超过上限时保留头尾并插入统一截断标记；
- snapshot 包含 index 和 version。

### 9.3 Compaction

- 先记录 source version；
- 在不持有 Context 锁的情况下调用 Summarizer；
- 提交前再次比较 version；
- 无早期消息时不产生 checkpoint；
- 保留最近 token 的切分行为与 Python 一致。

### 9.4 Rollout

JSONL 记录必须保持结构化消息，不能把消息再次编码成 JSON 字符串。

记录类型：

- message record；
- compaction checkpoint。

恢复：

- 流式读取，不一次性加载整个文件；
- 最新 compaction 替换之前的回放状态；
- 允许忽略崩溃造成的最后一条截断记录；
- 路径中的 thread ID 必须净化；
- open/close 幂等。

## 10. Tool 与 Agent

### 10.1 Tool

`Tool` trait 只负责业务执行：

- `spec()`
- `execute(input, context)`

`ToolExecutor` 统一负责：

- 输入校验；
- begin/end/error 事件；
- 异常归一化；
- 结果截断；
- 取消传播。

Registry 采用 Builder 构建，构建完成后只读，因此 resolve/specs 不需要异步锁。

`ToolContext` 包含：

- tool name；
- call ID；
- `Arc<dyn EventSink>`；
- `CancellationToken`。

### 10.2 Provider

Provider trait 返回流，而不是直接返回 StepOutcome：

```text
ProviderEvent
├─ TextDelta
├─ ToolCall
├─ ResponseCompleted
└─ Error
```

要求：

- 按 tool call index 累积增量；
- finish 后按 index 生成完整调用；
- 非法 JSON 参数转换为当前兼容策略；
- 流断开返回明确错误；
- 取消时及时结束网络流。

### 10.3 Agent Loop

单个 Turn：

1. 注入一次 system prompt；
2. 解析用户输入；
3. 请求 Provider 流；
4. 发出文本 delta 事件；
5. 按顺序调度工具；
6. 写入 Assistant tool calls；
7. 按原调用顺序写入 ToolReturn；
8. 无 tool call 且有最终文本时完成。

并发规则：

- concurrency-safe 工具可并行；
- non-concurrency-safe 工具等待所有前置工具完成；
- 它成为后续所有工具的串行屏障；
- Provider 或 Turn 失败时取消并等待所有工具任务。

达到 `max_turns` 但未产生最终结果应返回明确错误，不伪装为成功。

### 10.4 上下文事务

Runtime 把 Context 工作副本移交给 `TurnRunner`：

- 成功：返回 `TurnOutput { context, result_ref, next_context_ref }` 并提交；
- 失败：丢弃工作副本；
- 中断：按 Python 当前规则记录已产生消息，再提交 interrupted 终态；
- Agent 不自行创建与 Runtime 脱离的永久 Context。

### 10.5 输入解析

本次不实现待办中的 ArtifactStore。定义窄接口 `InputResolver`：

- 默认实现只支持普通文本和经过根目录限制的 `artifact://` 文件；
- canonicalize 后必须仍位于允许根目录；
- 读取失败时按当前兼容策略回退到原始 ref；
- 后续 ArtifactStore 实现不属于本计划。

## 11. Runtime

### 11.1 TurnRunner

`athena-runtime` 定义对象安全的异步 `TurnRunner` trait；`athena-agent` 提供实现。Runtime 不依赖 Agent crate。

输入包括：

- Thread/Turn 快照；
- Context 工作副本；
- EventSink；
- CancellationToken。

输出包括：

- result ref；
- next context ref；
- 更新后的 Context。

### 11.2 Thread Actor

每个 Thread 一个 Actor，独占：

- Thread 状态；
- 当前 Context；
- EventJournal；
- active turn；
- completed context/result 索引；
- submission receiver。

状态：

```text
Idle → Running → Idle
Idle/Running → Closing → Closed
```

Actor 使用 `tokio::select!` 同时处理：

- StartTurn；
- InterruptTurn；
- GetForkSnapshot；
- ShutdownThread；
- runner 完成信号。

终态只由 Actor 提交。Interrupt 先提交唯一 interrupted 终态，再取消 runner；之后到达的 success/failure 信号因 turn generation 不匹配而被忽略。

### 11.3 ThreadManager

- 管理 ThreadHandle 注册表；
- start 时为每个 Thread 创建独立 Context、Compactor 和 Rollout；
- fork 只允许引用已完成 Turn 的 snapshot；
- close 获取一次 handles 快照后释放锁，再并发关闭；
- close 幂等；
- 不在持有 manager 锁时等待 Thread Actor。

## 12. GitWorkspace 与 ResearchTree

### 12.1 `athena-workspace`

只迁移 `LocalGitWorkspace` 当前已有能力：

- init；
- create worktree；
- diff；
- commit；
- remove；
- branch/path/ownership 校验；
- review 后变更检测；
- 可重试删除。

所有 Git 调用使用参数数组，不拼接 shell 字符串。删除前验证目标处于受管 workspace 根目录内。

### 12.2 `athena-research`

只迁移：

- Experiment；
- ResearchTreeNode；
- ResearchTreeNodes；
- ResearchTree 当前节点创建、查询、prompt 和实验关联逻辑。

不实现树持久化、ranking 或 proximity。

## 13. 测试策略

### 13.1 契约测试

由 Python 导出：

- 所有协议 DTO JSON；
- ErrorCode；
- Hypothesis 枚举和校验样本；
- PydanticAI 消息样本；
- rollout 和 compaction 样本。

Rust 对这些 fixture 做反序列化、再序列化和语义比较。

### 13.2 行为测试

必须覆盖现有 Python 测试表达的行为：

- protocol；
- transport；
- MessageProcessor；
- ClientWorker；
- subscriptions/FairMux；
- ThreadRuntime/ThreadManager；
- 1000 次 complete/interrupt 竞争；
- ContextManager；
- Compactor；
- Rollout；
- Tool；
- Agent 和 SubAgent；
- GitWorkspace；
- ResearchTree 当前公开行为。

集成测试放入实际 crate 的 `tests/` 目录，例如：

- `crates/athena-server/tests/app_server_e2e.rs`
- `crates/athena-runtime/tests/thread_races.rs`
- `crates/athena-agent/tests/tool_loop.rs`

不创建没有合法 target 的通用 `test` crate。

## 14. 验收标准

- `cargo fmt --all --check` 通过；
- `cargo clippy --workspace --all-targets -- -D warnings` 通过；
- `cargo test --workspace` 通过；
- 所有纳入范围的 Python 行为都有 Rust 对应测试；
- 协议黄金样本双向兼容；
- 1000 次终态竞争测试始终只产生一个终态；
- Notification 之外的消息不静默丢失；
- shutdown 在 5 秒内完成且无遗留 owned task；
- rollout 从最新 compaction 正确恢复；
- GitWorkspace 不允许越界路径和未受管 worktree；
- 不以空 crate、0 tests 或单纯 `cargo build` 作为功能完成证明。

## 15. 迁移与回滚

1. Python 保持行为基准，不在 Rust 未验收前删除。
2. 先完成 fixture 和 Rust crate 测试，再接入真实调用方。
3. 接入层保留 `python`/`rust` 引擎选择。
4. Rust 失败时切回 Python，不改变 Python rollout 和协议数据。
5. 只有在协议、行为、恢复和关闭测试全部通过后才切换默认引擎。

## 16. 风险

- PydanticAI 消息结构比简单 role/content 更复杂，过度简化会破坏 tool call 回放；
- completion/interrupt/shutdown 竞态是最高风险区域；
- Rust async 锁若跨 await 使用容易引入死锁；
- rollout 格式若与 Python 不兼容会阻断恢复；
- 正在进行的旧计划实现可能继续改写工作区，实施前必须先冻结并盘点 WIP。

## 17. 非目标

- 不实现当前 Python 中标记“待实现”的功能；
- 不引入 PyO3；
- 不删除 Python 实现；
- 不改变 v1 线协议；
- 不做数据库、分布式队列或跨进程网络服务；
- 不在行为兼容前进行性能型重构。
