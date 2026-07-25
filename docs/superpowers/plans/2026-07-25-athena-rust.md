# Athena Rust 迁移实施计划（审校版）

> 设计依据：`docs/superpowers/specs/2026-07-25-athena-rust-design.md`
> 状态：待实施。本文中的任务默认均未完成；只有通过对应验收命令后才能勾选。
> 范围：仅迁移 `src/athena` 中当前已经存在可执行行为的模块。

## 1. 问题摘要

旧计划存在以下阻断问题：

- 目标只写了 app_server、memory、agent，却声称完成整个迁移；
- 多个空 crate 被标为完成；
- Runtime 和 Server 只有行数估计，没有决策完整的实现步骤；
- `core` 包名会与 Rust 标准 `core` 冲突；
- Event/FairMux 示例包含无法编译或会丢事件的设计；
- Agent 按值取得 Context 后没有把更新返回 Runtime；
- 独立 `test` crate 没加入 workspace，也没有合法 target；
- 测试范围远小于现有 Python 行为。

本计划以契约优先、Actor 状态所有权和 Python/Rust 行为对照为主线重新组织实施。

## 2. 本计划范围

### 2.1 纳入

- `src/athena/core/schemas.py`
- `src/athena/core/tool.py`
- `src/athena/core/tool_types.py`
- `src/athena/core/agent/agent.py`
- `src/athena/core/agent/provider.py`
- `src/athena/core/agent/subagent.py`
- `src/athena/core/gitutils/workspace.py`
- `src/athena/core/research/research_tree.py` 的当前行为
- `src/athena/memory/*`
- `src/athena/app_server/*`
- `src/athena/execution/handlers.py` 表达的现有抽象
- `src/athena/agents/demo_agent.py`

### 2.2 不纳入

本计划不实现、也不创建对应空 crate：

- scheduler、plot/data/research/code agent、policy supervisor；
- agent monitor、sandbox runtime；
- ranking、proximity；
- artifact store、state store；
- single-turn chat；
- prepare/search/validate/report workflows；
- ResearchTree 尚未实现的持久化。

这些项目只有在 Python 侧先具备明确行为和验收标准后，才进入新的独立计划。

## 3. 全局实施规则

1. 实施前停止其他会写入 `athena-rust` 的进程。
2. 不使用 `git reset --hard`、不删除未知 WIP、不覆盖用户修改。
3. 包名统一为 `athena-*`，禁止裸 `core`、`test`。
4. 生产代码禁止 `unwrap()`、`expect()` 和未说明的 panic。
5. `unsafe_code` 设为 forbid。
6. 只有协议和持久化 DTO 派生 Serialize/Deserialize。
7. 不把所有状态机械包进 `Arc<Mutex<_>>`；优先 Actor 独占。
8. Request、Response、ServerRequest、Event 不允许静默丢失。
9. 每个任务必须先写失败测试或导入 Python fixture，再写实现。
10. `cargo build` 或 0 tests 不能作为任务完成证据。
11. 每个任务结束运行本任务测试和 workspace 回归测试。
12. 不修改本计划排除的 Python 待实现文件。

## 4. 目标 Workspace

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
└─ tests/fixtures/
```

集成测试放在对应 crate 的 `tests/` 下，不创建通用 `test` crate。

---

## Task 0：冻结基线并盘点现有 WIP

### 目标

在不丢失当前未提交 Rust 修改的情况下，建立迁移基线，防止旧计划继续并发写入。

### 文件

- Modify: `.gitignore`
- Create: `scripts/export_rust_contract_fixtures.py`
- Create: `athena-rust/tests/fixtures/protocol/`
- Create: `athena-rust/tests/fixtures/messages/`
- Create: `athena-rust/tests/fixtures/rollout/`
- Create: `athena-rust/WIP_INVENTORY.md`

### 步骤

- [ ] 0.1 确认没有其他 Agent、脚本或 Cargo 生成器继续写入 `athena-rust`。
- [ ] 0.2 记录 `git status --short` 和 `git diff -- athena-rust`。
- [ ] 0.3 在 `WIP_INVENTORY.md` 逐项记录现有 flat crate：
  - 已实现代码；
  - 空文件；
  - 已有测试；
  - 可复用内容；
  - 与目标设计冲突的内容。
- [ ] 0.4 将 `athena-rust/target/` 加入 `.gitignore`。
- [ ] 0.5 编写 fixture 导出脚本，只读取 Python 对象并输出稳定 JSON。
- [ ] 0.6 导出以下 fixture：
  - ErrorCode 和所有协议方法名；
  - 请求、响应、通知和 operation DTO；
  - domain DTO 与 Hypothesis 校验样本；
  - PydanticAI system/user/text/tool-call/tool-return 消息；
  - rollout message 与 compaction checkpoint。
- [ ] 0.7 运行当前 Python 基线测试并记录结果。

### Python 基线命令

```powershell
uv run pytest -q `
  test\unit\app_server `
  test\unit\test_context_manager.py `
  test\unit\test_compaction.py `
  test\unit\test_rollout.py `
  test\unit\test_tool.py `
  test\unit\test_agent.py `
  test\unit\test_git_workspace.py
```

### 验收

- WIP 已被完整记录，没有删除或重置；
- fixture 导出可重复运行且输出稳定；
- Python 基线通过；
- 完整 pytest 当前缺少 `agent_tool_example` 的问题被记录，但不阻塞本计划，因为该文件不属于 `src/athena` 迁移范围。

---

## Task 1：重建 Cargo Workspace

### 目标

建立无名称冲突、依赖集中管理、测试可发现的 workspace 骨架。

### 文件

- Modify: `athena-rust/Cargo.toml`
- Modify: `athena-rust/rust-toolchain.toml`
- Modify: `athena-rust/Cargo.lock`
- Create: `athena-rust/crates/athena-types/Cargo.toml`
- Create: `athena-rust/crates/athena-types/src/lib.rs`
- Create: `athena-rust/crates/athena-protocol/Cargo.toml`
- Create: `athena-rust/crates/athena-protocol/src/lib.rs`
- Create: `athena-rust/crates/athena-tools/Cargo.toml`
- Create: `athena-rust/crates/athena-tools/src/lib.rs`
- Create: `athena-rust/crates/athena-memory/Cargo.toml`
- Create: `athena-rust/crates/athena-memory/src/lib.rs`
- Create: `athena-rust/crates/athena-runtime/Cargo.toml`
- Create: `athena-rust/crates/athena-runtime/src/lib.rs`
- Create: `athena-rust/crates/athena-agent/Cargo.toml`
- Create: `athena-rust/crates/athena-agent/src/lib.rs`
- Create: `athena-rust/crates/athena-server/Cargo.toml`
- Create: `athena-rust/crates/athena-server/src/lib.rs`
- Create: `athena-rust/crates/athena-workspace/Cargo.toml`
- Create: `athena-rust/crates/athena-workspace/src/lib.rs`
- Create: `athena-rust/crates/athena-research/Cargo.toml`
- Create: `athena-rust/crates/athena-research/src/lib.rs`

### 步骤

- [ ] 1.1 在 `[workspace.package]` 统一 edition、rust-version、license。
- [ ] 1.2 在 `[workspace.dependencies]` 统一 tokio、serde、serde_json、thiserror、tracing、uuid 等版本。
- [ ] 1.3 在 `[workspace.lints.rust]` 禁止 unsafe。
- [ ] 1.4 在 `[workspace.lints.clippy]` 启用生产代码质量门。
- [ ] 1.5 添加九个 `athena-*` workspace members。
- [ ] 1.6 将现有 flat crate 内容按 WIP inventory 迁移或留待对应任务迁移；不得直接删除未核对文件。
- [ ] 1.7 删除所有对 package `core` 的依赖引用，改为 `athena-types`。
- [ ] 1.8 确认每个 crate 至少有合法 lib target。

### 验收

```powershell
cargo metadata --no-deps
cargo check --workspace
```

- metadata 中恰好包含目标九个 crate；
- 不存在名为 `core`、`test` 的 package；
- 不存在 workspace 外游离 crate；
- workspace 可在没有任何空壳“完成声明”的情况下检查通过。

---

## Task 2：实现 `athena-types` 与 `athena-protocol`

### 目标

先冻结领域模型和 v1 线协议，使后续 crate 依赖稳定契约。

### 文件

- Create: `athena-rust/crates/athena-types/src/ids.rs`
- Create: `athena-rust/crates/athena-types/src/domain.rs`
- Create: `athena-rust/crates/athena-types/src/status.rs`
- Modify: `athena-rust/crates/athena-types/src/lib.rs`
- Create: `athena-rust/crates/athena-types/tests/python_fixtures.rs`
- Create: `athena-rust/crates/athena-protocol/src/error.rs`
- Create: `athena-rust/crates/athena-protocol/src/method.rs`
- Create: `athena-rust/crates/athena-protocol/src/envelope.rs`
- Create: `athena-rust/crates/athena-protocol/src/operations.rs`
- Modify: `athena-rust/crates/athena-protocol/src/lib.rs`
- Create: `athena-rust/crates/athena-protocol/tests/python_fixtures.rs`

### 接口

`athena-types`：

- `NonBlankString`
- `ArtifactRef`、`CommitHash`
- `ThreadId`、`TurnId`、`SessionId`
- `MetricDirection`、`MetricSpec`
- `TaskMetaData`、`DataCard`
- `HypothesisStatus`、`Hypothesis`
- `ExperimentPlan`
- `ThreadStatus`、`TurnStatus`
- `AthenaThread`、`AthenaTurn`

`athena-protocol`：

- `ErrorCode`、`RpcError`
- `RequestEnvelope`、`ResponseEnvelope`
- `ClientNotification`
- `ServerRequest`、`ServerRequestReply`
- `EventNotification`
- 全部 operation params/results
- method 常量和 `is_control_method`

### 步骤

- [ ] 2.1 先编写读取 Python fixture 的失败测试。
- [ ] 2.2 实现非空 newtype 的 TryFrom/Deserialize 校验。
- [ ] 2.3 实现 Hypothesis patience 交叉字段校验。
- [ ] 2.4 保持 Python 当前枚举序列化形式。
- [ ] 2.5 DTO 使用 `deny_unknown_fields`。
- [ ] 2.6 保持当前 Response result/error 可选语义，不擅自升级协议。
- [ ] 2.7 实现稳定、脱敏的错误映射。
- [ ] 2.8 补齐 approval、userInput 和 tool call 方法常量。
- [ ] 2.9 为 request ID、sequence 等边界编写测试。

### 验收

```powershell
cargo test -p athena-types
cargo test -p athena-protocol
```

- Python fixture 全部可读并语义往返；
- 错误码数值完全一致；
- 空白 ArtifactRef 被拒绝；
- patience 校验与 Python 一致；
- 未知字段被拒绝；
- 生产代码无 panic 路径。

---

## Task 3：实现 `athena-memory`

### 目标

无损迁移消息、上下文、压缩和 rollout 恢复。

### 文件

- Create: `athena-rust/crates/athena-memory/src/message.rs`
- Create: `athena-rust/crates/athena-memory/src/context.rs`
- Create: `athena-rust/crates/athena-memory/src/compaction.rs`
- Create: `athena-rust/crates/athena-memory/src/rollout.rs`
- Modify: `athena-rust/crates/athena-memory/src/lib.rs`
- Create: `athena-rust/crates/athena-memory/tests/context_parity.rs`
- Create: `athena-rust/crates/athena-memory/tests/compaction.rs`
- Create: `athena-rust/crates/athena-memory/tests/rollout_recovery.rs`

### 接口

- `ModelMessage`
- `MessagePart::{SystemPrompt, UserPrompt, Text, ToolCall, ToolReturn}`
- `ContextManager`
- `ContextSnapshot`
- `Summarizer` trait
- `Compactor`
- `Compaction`
- `RolloutRecord`
- `RolloutRecorder`
- `resume_context`

### 步骤

- [ ] 3.1 用 Python message fixture 建立无损解析测试。
- [ ] 3.2 实现 Context 的 append/items/snapshot/items_since/rollback/replace_range。
- [ ] 3.3 所有写操作维护 token 总和不变量。
- [ ] 3.4 工具结果截断保留头尾，不修改调用方原对象。
- [ ] 3.5 实现 Compactor 的 recent-token 切分。
- [ ] 3.6 摘要调用期间不持有 Context 锁。
- [ ] 3.7 提交摘要前验证 source version。
- [ ] 3.8 Rollout 以结构化 JSON 写 message，不进行双重 JSON 编码。
- [ ] 3.9 最新 compaction checkpoint 重置恢复状态。
- [ ] 3.10 跳过崩溃产生的最后一条 torn record。
- [ ] 3.11 净化 rollout 文件名中的 thread ID。
- [ ] 3.12 验证 open/close 幂等。

### 验收

```powershell
cargo test -p athena-memory
```

- Python 消息 fixture 无信息丢失；
- token 不变量覆盖 append/replace/rollback；
- compaction 并发修改返回错误；
- rollout 可恢复最新 checkpoint 后的上下文；
- 大文件恢复使用流式读取。

---

## Task 4：实现 `athena-tools`

### 目标

迁移工具注册、生命周期事件、错误归一化、结果截断和并发元数据。

### 文件

- Create: `athena-rust/crates/athena-tools/src/spec.rs`
- Create: `athena-rust/crates/athena-tools/src/context.rs`
- Create: `athena-rust/crates/athena-tools/src/tool.rs`
- Create: `athena-rust/crates/athena-tools/src/registry.rs`
- Create: `athena-rust/crates/athena-tools/src/executor.rs`
- Modify: `athena-rust/crates/athena-tools/src/lib.rs`
- Create: `athena-rust/crates/athena-tools/tests/tool_lifecycle.rs`
- Create: `athena-rust/crates/athena-tools/tests/registry.rs`

### 接口

- `TOOL_BEGIN`、`TOOL_END`、`TOOL_ERROR`
- `ToolSpec`
- `ToolOutput`
- `ToolError`
- `ToolResult`
- `EventSink` trait
- `ToolContext`
- `Tool` trait
- `ToolRegistryBuilder`
- `ToolRegistry`
- `ToolExecutor`

### 步骤

- [ ] 4.1 Tool 业务实现只负责 execute。
- [ ] 4.2 ToolExecutor 负责 begin/end/error 事件。
- [ ] 4.3 取消错误不包装成普通 ToolResult，必须传播取消。
- [ ] 4.4 普通执行错误归一化为失败结果，并仅在内部日志保留诊断。
- [ ] 4.5 超长字符串结果设置 truncated 并真正截断输出。
- [ ] 4.6 Builder 拒绝重复工具名。
- [ ] 4.7 build 后 Registry 只读，specs 按名称稳定排序。
- [ ] 4.8 实现 resolve、search 和并发 dispatch 所需接口。

### 验收

```powershell
cargo test -p athena-tools
```

- 生命周期事件顺序与 Python 一致；
- duplicate/missing tool 返回稳定错误；
- specs 排序稳定；
- 取消被传播；
- 错误中不向外暴露 traceback。

---

## Task 5：实现 `athena-runtime`

### 目标

建立事件 Journal、订阅源、TurnRunner 契约、每 Thread Actor 和 ThreadManager。

### 文件

- Create: `athena-rust/crates/athena-runtime/src/event.rs`
- Create: `athena-rust/crates/athena-runtime/src/journal.rs`
- Create: `athena-rust/crates/athena-runtime/src/submission.rs`
- Create: `athena-rust/crates/athena-runtime/src/runner.rs`
- Create: `athena-rust/crates/athena-runtime/src/thread_actor.rs`
- Create: `athena-rust/crates/athena-runtime/src/thread_handle.rs`
- Create: `athena-rust/crates/athena-runtime/src/thread_manager.rs`
- Modify: `athena-rust/crates/athena-runtime/src/lib.rs`
- Create: `athena-rust/crates/athena-runtime/tests/event_journal.rs`
- Create: `athena-rust/crates/athena-runtime/tests/thread_runtime.rs`
- Create: `athena-rust/crates/athena-runtime/tests/thread_races.rs`
- Create: `athena-rust/crates/athena-runtime/tests/thread_manager.rs`

### 接口

- `EventDraft`、`Event`
- `EventJournal`
- `StartTurn`、`InterruptTurn`、`GetForkSnapshot`、`ShutdownThread`
- `Submission`
- `TurnInput`、`TurnOutput`
- `TurnRunner` trait
- `ThreadState`
- `ThreadHandle`
- `RuntimeThreadManager`

### 步骤

- [ ] 5.1 Journal append 内部分配单调 sequence。
- [ ] 5.2 Journal 使用历史记录 + tail watch，不能只依赖瞬时通知。
- [ ] 5.3 定义 `TurnRunner`，输入和输出都显式携带 Context。
- [ ] 5.4 每 Thread 创建一个 Actor，状态不暴露为共享 Mutex。
- [ ] 5.5 StartTurn 只允许 Idle 状态。
- [ ] 5.6 runner 通过独立 task 执行，Actor 同时继续接收 interrupt/shutdown。
- [ ] 5.7 terminal event 只由 Actor 提交。
- [ ] 5.8 interrupt 提交终态后取消并等待 runner，忽略迟到 completion。
- [ ] 5.9 failure 丢弃 Context 工作副本。
- [ ] 5.10 success 提交返回的 Context 和 refs。
- [ ] 5.11 fork 只读取已完成 Turn 的 snapshot。
- [ ] 5.12 shutdown 拒绝新提交、终止 active turn、令 pending submission 失败。
- [ ] 5.13 Manager close 不在持锁状态等待 Thread。
- [ ] 5.14 Manager close 串行和并发调用均幂等。

### 验收

```powershell
cargo test -p athena-runtime
```

- basic complete、failure、interrupt、fork、shutdown 全覆盖；
- active turn 期间第二个 StartTurn 被拒绝；
- emit 在 terminal 后被拒绝；
- 多 Thread runner 可并行；
- complete/interrupt 竞争连续运行 1000 次，每次恰好一个 terminal event；
- shutdown 后无 owned task 残留。

---

## Task 6：实现 `athena-agent`

### 目标

实现 Provider 流、工具调用循环、Context 事务和 SubAgent。

### 文件

- Create: `athena-rust/crates/athena-agent/src/config.rs`
- Create: `athena-rust/crates/athena-agent/src/provider.rs`
- Create: `athena-rust/crates/athena-agent/src/openai_provider.rs`
- Create: `athena-rust/crates/athena-agent/src/input.rs`
- Create: `athena-rust/crates/athena-agent/src/agent.rs`
- Create: `athena-rust/crates/athena-agent/src/subagent.rs`
- Modify: `athena-rust/crates/athena-agent/src/lib.rs`
- Create: `athena-rust/crates/athena-agent/examples/demo_agent.rs`
- Create: `athena-rust/crates/athena-agent/tests/provider_mapping.rs`
- Create: `athena-rust/crates/athena-agent/tests/tool_loop.rs`
- Create: `athena-rust/crates/athena-agent/tests/subagent.rs`

### 接口

- `AgentConfig`
- `ProviderEvent`
- `LlmProvider` trait
- `OpenAiProvider`
- `InputResolver` trait
- `RestrictedFileInputResolver`
- `Agent`
- `AgentOutcome`
- `AgentRunner`
- `AgentControl`
- `AgentHandle`
- `AgentEvent`
- `AgentResult`

### 步骤

- [ ] 6.1 Provider 返回事件流，不直接返回 StepOutcome。
- [ ] 6.2 映射 Context 消息为 OpenAI Chat Completions 消息。
- [ ] 6.3 文本 delta 保留 accumulated 文本。
- [ ] 6.4 多 tool call 按 index 缓冲 name/id/arguments。
- [ ] 6.5 流错误显式传播。
- [ ] 6.6 Agent 注入 system prompt 和 user input。
- [ ] 6.7 文本 delta 经 EventSink 发出。
- [ ] 6.8 concurrency-safe 工具并发运行。
- [ ] 6.9 non-concurrency-safe 工具成为严格串行屏障。
- [ ] 6.10 工具结果按原调用顺序写回 Context。
- [ ] 6.11 Provider 错误取消并等待所有工具 task。
- [ ] 6.12 max_turns 耗尽返回错误，不返回伪成功。
- [ ] 6.13 `AgentRunner` 实现 Runtime 的 `TurnRunner`。
- [ ] 6.14 RestrictedFileInputResolver canonicalize 路径并限制根目录。
- [ ] 6.15 SubAgent 使用 Semaphore 限制并发。
- [ ] 6.16 send_message 写入对应 SubAgent Context。
- [ ] 6.17 cancel、wait、next_event 与 Python 当前语义一致。
- [ ] 6.18 将 demo_agent 的 read_file/list_dir 迁为受限示例工具。

### 验收

```powershell
cargo test -p athena-agent
```

- 纯文本 response 正常完成；
- 单个和多个工具调用正确回写；
- 串行屏障测试证明顺序；
- Provider 错误不被报告为成功；
- 取消后无工具 task 遗留；
- SubAgent 消息和事件可达；
- artifact 路径不能逃逸允许根目录；
- 测试不访问真实网络。

---

## Task 7：实现 `athena-server`

### 目标

迁移 Transport、ClientWorker、AthenaClient、MessageProcessor、ExecutionAdapter、SubscriptionRegistry、FairMux 和 AppServer 生命周期。

### 文件

- Create: `athena-rust/crates/athena-server/src/error.rs`
- Create: `athena-rust/crates/athena-server/src/transport.rs`
- Create: `athena-rust/crates/athena-server/src/client.rs`
- Create: `athena-rust/crates/athena-server/src/processor.rs`
- Create: `athena-rust/crates/athena-server/src/execution.rs`
- Create: `athena-rust/crates/athena-server/src/subscription.rs`
- Create: `athena-rust/crates/athena-server/src/lifecycle.rs`
- Modify: `athena-rust/crates/athena-server/src/lib.rs`
- Create: `athena-rust/crates/athena-server/tests/transport.rs`
- Create: `athena-rust/crates/athena-server/tests/client_worker.rs`
- Create: `athena-rust/crates/athena-server/tests/message_processor.rs`
- Create: `athena-rust/crates/athena-server/tests/subscriptions.rs`
- Create: `athena-rust/crates/athena-server/tests/app_server_e2e.rs`

### 接口

- `Transport`
- `TransportClientHalf`
- `TransportServerHalf`
- `Sequencer`
- `ClientWorker`
- `AthenaClient`
- `MessageProcessor`
- `ExecutionAdapter`
- `SubscriptionRegistry`
- `FairMux`
- `AppServer`

### 步骤

- [ ] 7.1 建立 c2s、s2c-control、s2c-event 三个有界通道。
- [ ] 7.2 request 等待容量并支持 timeout。
- [ ] 7.3 notification 满载时允许丢弃。
- [ ] 7.4 response/server request/event 使用可靠发送。
- [ ] 7.5 ready barrier 与 initialize/initialized 握手一致。
- [ ] 7.6 close 通过 cancellation + drop sender 传播并保持幂等。
- [ ] 7.7 ClientWorker 正确解析 response、event 和 ServerRequest。
- [ ] 7.8 timeout 时从 pending map 移除 request。
- [ ] 7.9 shutdown 时令所有 pending request 以 CLOSED 失败。
- [ ] 7.10 Processor 实现 Created/Initializing/Ready/Draining/Terminated 状态机。
- [ ] 7.11 initialize 只接受 request ID 0 和受支持协议版本。
- [ ] 7.12 重复 inflight ID 返回 DUPLICATE_REQUEST_ID。
- [ ] 7.13 executor 异常映射为脱敏 RpcError。
- [ ] 7.14 Subscription pump 从 Journal cursor 回放。
- [ ] 7.15 FairMux 独占 subscription receiver 并做 round-robin。
- [ ] 7.16 FairMux 无 ready subscription 时事件驱动等待，不固定轮询。
- [ ] 7.17 ExecutionAdapter 映射 thread/start、turn/start、interrupt、fork、subscribe、unsubscribe。
- [ ] 7.18 AppServer 初始化失败时逆序清理已启动资源。
- [ ] 7.19 shutdown 按 admission → subscriptions → threads → transport 顺序完成。

### 验收

```powershell
cargo test -p athena-server
```

- Transport 满载、独立 event lane、ready、close 行为与 Python 一致；
- busy subscription 不饿死其他 subscription；
- 相同 cursor 可创建不同 subscription ID；
- 请求生命周期和错误码兼容；
- AppServer 端到端完成 initialize → thread → turn → subscribe → event → shutdown；
- 正常关闭没有遗留 task。

---

## Task 8：实现 `athena-workspace`

### 目标

迁移 `LocalGitWorkspace` 当前已有、已有 Python 测试覆盖的行为。

### 文件

- Create: `athena-rust/crates/athena-workspace/src/error.rs`
- Create: `athena-rust/crates/athena-workspace/src/model.rs`
- Create: `athena-rust/crates/athena-workspace/src/command.rs`
- Create: `athena-rust/crates/athena-workspace/src/local.rs`
- Modify: `athena-rust/crates/athena-workspace/src/lib.rs`
- Create: `athena-rust/crates/athena-workspace/tests/local_git_workspace.rs`

### 接口

- `GitWorkspaceError`
- `GitWorkBranch`
- `GitWorkspace` trait
- `LocalGitWorkspace`

### 步骤

- [ ] 8.1 Git 子进程只使用参数数组，不拼 shell 命令。
- [ ] 8.2 init 验证 repo、base commit 和受管根目录。
- [ ] 8.3 create 验证 branch 名并创建受管 worktree。
- [ ] 8.4 diff 输出稳定 ArtifactRef/路径引用。
- [ ] 8.5 commit 检测 review 后新增修改。
- [ ] 8.6 空 diff 重用 HEAD，不创建空 commit。
- [ ] 8.7 remove 默认拒绝 dirty workspace。
- [ ] 8.8 force remove 只允许受管路径。
- [ ] 8.9 删除失败保持可重试状态。

### 验收

```powershell
cargo test -p athena-workspace
```

- 对应 Python GitWorkspace 测试全部有 Rust 等价测试；
- 无越界删除；
- 无 shell 注入；
- dirty、invalid、unowned 输入被拒绝；
- 测试只操作临时 Git 仓库。

---

## Task 9：实现 `athena-research`

### 目标

只迁移 `research_tree.py` 当前已经存在的模型和算法，不实现持久化、ranking 或 proximity。

### 文件

- Create: `athena-rust/crates/athena-research/src/experiment.rs`
- Create: `athena-rust/crates/athena-research/src/tree.rs`
- Modify: `athena-rust/crates/athena-research/src/lib.rs`
- Create: `athena-rust/crates/athena-research/tests/research_tree.rs`
- Create: `athena-rust/tests/fixtures/research/`

### 接口

- `Experiment`
- `ResearchTreeNode`
- `ResearchTree`
- 当前 Python 暴露的 node create/get/prompt/experiment 操作

### 步骤

- [ ] 9.1 从 Python 生成固定 ResearchTree fixture 和 prompt fixture。
- [ ] 9.2 实现 Experiment result 转换和 prompt。
- [ ] 9.3 实现节点创建和父子关系。
- [ ] 9.4 实现按 ID 查询。
- [ ] 9.5 实现当前 prompt 聚合行为。
- [ ] 9.6 实现当前 experiment 关联行为。
- [ ] 9.7 不添加 save/load、ranking 或 proximity API。

### 验收

```powershell
cargo test -p athena-research
```

- fixture 树结构与 Python 一致；
- prompt 输出语义一致；
- 无效节点引用返回类型化错误；
- crate 中不存在未实现持久化的伪 API。

---

## Task 10：全局兼容、质量门与迁移交付

### 目标

证明所有纳入范围的 Rust 行为可交付，并保留 Python 回滚路径。

### 文件

- Modify: `README.md`
- Create: `athena-rust/README.md`
- Create: `athena-rust/tests/COMPATIBILITY.md`
- Modify: `docs/superpowers/specs/2026-07-25-athena-rust-design.md`
- Modify: `docs/superpowers/plans/2026-07-25-athena-rust.md`

### 步骤

- [ ] 10.1 建立 Python fixture 重导出 + Rust 测试的一键命令。
- [ ] 10.2 运行所有 Rust crate 测试。
- [ ] 10.3 运行 fmt 和 clippy。
- [ ] 10.4 运行纳入范围的 Python 基线测试，确认迁移期间未破坏 Python。
- [ ] 10.5 在 `COMPATIBILITY.md` 记录每个 Python 模块、测试和 Rust 模块的对应关系。
- [ ] 10.6 在 `athena-rust/README.md` 记录构建、测试、示例运行和环境变量。
- [ ] 10.7 明确 Rust 尚未覆盖的“待实现”模块，不把它们写成 Rust 缺陷或已完成能力。
- [ ] 10.8 只有全部证据存在时，才更新本计划相应 checkbox。

### 最终验收命令

```powershell
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace

uv run pytest -q `
  test\unit\app_server `
  test\unit\test_context_manager.py `
  test\unit\test_compaction.py `
  test\unit\test_rollout.py `
  test\unit\test_tool.py `
  test\unit\test_agent.py `
  test\unit\test_git_workspace.py
```

### Definition of Done

- 九个目标 crate 均有实际行为和测试；
- 没有空 crate 被声明完成；
- Python fixture 与 Rust 完全兼容；
- 终态竞争、背压、事件公平、关闭、rollout 恢复均有证据；
- Rust 和纳入范围的 Python 测试同时通过；
- fmt/clippy 无错误和 warning；
- Python 实现仍可独立运行，回滚不需要数据迁移；
- 排除范围未被偷偷加入。

---

## 5. Repo 影响

### 将修改

- `.gitignore`
- `athena-rust/Cargo.toml`
- `athena-rust/Cargo.lock`
- `athena-rust/rust-toolchain.toml`
- `README.md`
- 本设计和计划文档

### 将创建

- `athena-rust/crates/athena-*`
- `athena-rust/tests/fixtures`
- `athena-rust/WIP_INVENTORY.md`
- `athena-rust/README.md`
- `athena-rust/tests/COMPATIBILITY.md`
- fixture 导出脚本

### 迁移期间保持

- `src/athena` Python 行为基准；
- 当前未提交 Rust WIP，直至 Task 0 完成盘点；
- Python rollout 和协议 fixture。

## 6. 风险与应对

| 风险 | 应对 |
|---|---|
| 旧执行器继续写工作区 | Task 0 前停止并发 writer，记录 WIP |
| PydanticAI 消息丢字段 | 使用 MessagePart + Python fixture |
| complete/interrupt 双终态 | Actor 独占提交，1000 次竞争测试 |
| Notify 丢唤醒 | Journal 持久记录 + watch tail |
| event 满载丢失 | event lane 使用可靠 send |
| Rust 锁跨 await 死锁 | Actor 所有权；锁只保护短生命周期 registry |
| rollout 不兼容 | Python fixture 和恢复测试 |
| Git 删除越界 | canonicalize + 受管根验证 |
| 空 crate 伪完成 | DoD 要求行为测试，禁止 0 tests 作为证据 |

## 7. 测试说明

- 单元测试验证纯类型、状态和算法；
- crate integration tests 验证异步边界；
- Python fixtures 验证跨语言契约；
- Fake Provider/Fake Tool/Fake Runner 替代真实网络；
- GitWorkspace 使用临时仓库；
- 并发测试必须有 timeout，防止测试永久挂起；
- 关闭测试要检查 task 已结束，而不仅检查函数返回。

## 8. 迁移与回滚

1. Python 在整个迁移期保持可运行。
2. Rust 只读取兼容 fixture，不修改 Python rollout。
3. 上层调用方在 Rust 全部验收前继续使用 Python。
4. 切换后如出现问题，恢复 Python 调用入口即可。
5. 不删除 Python 代码、不转换历史数据，直到另有经批准的清理计划。

## 9. 开放问题

本计划没有阻塞实施的开放问题。未来若要迁移当前标记“待实现”的模块，必须先在 Python 或独立规格中确定其行为、输入输出和验收标准，再创建新计划。
