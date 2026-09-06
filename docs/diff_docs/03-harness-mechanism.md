# Harness 机制差异：Athena vs Codex

这里的 “harness” 指 agent 运行框架本身：Turn/Session/Step、事件与协议、
工具执行、多 Agent、环境/沙箱/权限、上下文注入、持久化、Hooks、模型能力等。

## 1. Session / Turn / Step 模型

### Athena

- `ThreadRuntime`（`src/athena/app_server/thread_runtime.py`）：
  - 一个 Thread 一个 runtime，最多一个 active turn
  - `submission_queue` / `control_queue` / `_MergedQueue`
  - `submission_loop` 消费 `Submission`
  - `StartTurn` / `InterruptTurn` / `GetForkSnapshot` / `ShutdownThread`
  - `_run_turn`：maybe_compact → 快照 → runner → 持久化/回滚
- `AgentRuntime`（`src/athena/core/agent/agent_runtime.py`）：
  - 面向上层提供 create/spawn/followup/send_message/wait/interrupt 等
  - Agent 树通过 `_FacadeRecord` + mailbox 模拟
  - 没有独立 Step 概念，一个 Turn 就是一次 ReAct 循环
- `AgentContext`：thread/turn/emit/tools/cancel/memory/ask_user 的扁平上下文

### Codex

- `Session`（`core/src/session/session.rs`）：线程级状态/服务/历史
- `TurnContext`：
  - turn 级配置、provider、model info、环境快照、MCP、多 Agent 版本、
    current_date/timezone、developer instructions、network、windows sandbox、
    available models、dynamic tools、turn metadata、timing/telemetry 等
- `StepContext`：
  - 每次模型采样请求的快照：settings、token_budget、environment、
    capability roots、MCP binding、tool router、AGENTS.md
- `TurnInput` / `TurnInputSubmission`：
  - 支持 start / steer / recover / suspend / start-if-idle 等完整输入生命周期
- `SessionTask`：
  - `Regular`、`Compact`、`Review`、`UserShell` 等任务类型，每种有独立 span
- `TurnRuntime` / `ActiveTurn` / `RunningTask` 状态机更完整

## 2. 事件与协议

### Athena

- 通用 `Event { kind, event_ref, data }` + `EventJournal` + `FairMux` 多订阅
- JSON-RPC 方法集合很小：
  - `thread/start`、`thread/fork`、`turn/start`、`turn/interrupt`
  - `thread/subscribe`、`thread/unsubscribe`、`server/shutdown`
  - `item/approval/request`、`item/userInput/request`、`tool/call/request`
- 没有类型化 item 流、没有 `item/started|completed`、没有 delta 细分

### Codex

- `codex_protocol::protocol::EventMsg` 包含大量类型化事件：
  - `TurnStarted` / `TurnCompleted` / `TurnError`
  - `ItemStarted` / `ItemCompleted`
  - `AgentMessageContentDelta`、`ReasoningSummaryTextDelta`、`ReasoningRawContentDelta`
  - `PlanDelta`、`CommandExecution`、`FileChange`、`McpToolCall`
  - `ContextCompaction`、`SafetyBuffering`、`Warning`、`RateLimits` 等
- App-server 提供 `thread/compact/start`、`turn/steer`、`thread/searchOccurrences`、
  plugin/marketplace、conversation summary、account usage 等大量 RPC
- 线程 `itemsView`（notLoaded/summary/full）、turn items、pagination

## 3. 工具执行模型

### Athena

- `ToolSpec { name, description, input_schema, concurrency_safe, max_result_chars }`
- `ToolRegistry` 按名称排序
- `@tool` 装饰器由函数签名生成 JSON Schema
- `BaseTool.ainvoke`：emit begin → execute → emit end/error
- `_sample_once`：
  - 流式收到 `function_call` 后解析工具名
  - `concurrency_safe` 并行，非安全工具串行 barrier
  - 结果写回 `ToolReturnPart`，失败转为 `[ERROR] ...`
- 没有 namespace、没有 deferred/tool search、没有 exposure/read-only/destructive 元数据
- 没有工具 readiness、没有 per-step tool router、没有参数 diff
- 没有 pre/post tool hooks、没有工具生命周期事件细粒度通知
- 协议层有 `request_approval` / `request_user_input`，但工具调用没有统一接入
  审批、网络授权、沙箱的策略链
- 工具异常只是 `ToolResult(success=False, error, traceback)`

### Codex

- `ToolRegistry` + `RegisteredTool` + `ToolExposure`：
  - 支持 Function / Freeform / Namespace / ToolSearch / WebSearch
  - deferred exposure 与 tool search
- `ToolRouter`：
  - 每个 Step 锁定模型可见 tool plan
  - 支持 `ToolMode`、Code Mode、child management tools
  - 支持 namespace info、diff consumer
- `ToolCallRuntime`：
  - 并行执行 gate（read/write lock）
  - 每个工具调用有 readiness、取消、abort、时序、telemetry
  - 支持 streamed argument diff
- `ToolOutput` 抽象输出为 `ResponseInputItem`：
  - 函数输出、custom tool output、tool search output、aborted response
  - `success` / `status` / `execution` 语义
- 生命周期/analytics：
  - `notify_tool_start` / `notify_tool_finish` / `notify_tool_aborted`
  - 控制工具调用 analytics
  - executed tool calls metadata
- Hooks：
  - pre/post tool use hooks，可改写输入/过滤结果
- 沙箱/审批：
  - sandboxing、network approval、approvals、guardian
- MCP、connectors、skills、plugins 都接入工具计划

## 4. 多 Agent / 协作

### Athena

- `AgentRuntime._records` 保存 parent/path/name
- `mailbox` 为 `deque[AgentMessage]`
- `spawn` / `followup` / `send_message` / `wait_for` / `wait_for_human` / `interrupt`
- `AgentWaitResult` 支持 FIRST_COMPLETED / ALL_COMPLETED
- 所有等待/唤醒是内存语义
- 没有 agent role、没有 agent identity、没有 collaboration mode、没有 V1/V2 metadata
- 没有 subagent notification 注入到上下文的类型化机制

### Codex

- `agent-identity`、`agent-roles`、`collaboration-mode-templates` 等独立 crate
- 完整 Multi-Agent V1/V2：
  - parent/root turn id
  - `InterAgentCommunication` 与 metadata
  - `SubAgentActivity`、`CollabAgentToolCall`
  - spawn/send_message/followup 工具有独立命名空间
- 有 `codex_delegate`：子 Codex 线程、one-shot、无审批
- 有 agent control / execution capacity / completion watcher
- 有 worker reviewer / tester 等角色管理

## 5. 环境、沙箱、权限、执行

### Athena

- 主要靠 `git_workspace`、`artifact_store`、远程执行 channel 等外围模块
- `ToolContext` 没有环境/权限/沙箱；工具自管
- 没有 executor capability discovery、没有 shell snapshot、没有环境选择
- 协议层有 `request_approval` / `request_user_input`，但没有 guardian/审批策略链
  内建到 agent 的工具执行循环

### Codex

- `TurnEnvironment`：
  - environment_id、cwd、workspace_roots、user_home、temp dirs、shell、
    executor OS、shell snapshot
  - permission profile、sandbox context
  - windows sandbox level / private desktop / proxy
- `exec_policy`、`sandboxing`、`network_approval`、`approvals`、`guardian`
- `unified_exec`、`shell_command`、`exec-server`、`worktree`
- MCP prewarm / refresh / startup grace
- 插件、skills、connectors 的安装/授权/共享

## 6. 上下文注入（Instructions / World State / Fragments）

### Athena

- system prompt 仅由 Agent 构造时传入，写入 `ContextManager` 一次
- 没有 session meta、没有 AGENTS.md 加载、没有 world state、没有环境/工具/插件说明
- 没有 context diff；每次只能完整重发
- 没有 developer/user contextual fragment 的分类

### Codex

- `context/` 下大量 `ContextualUserFragment`：
  - world state、environment、model、tools、plugins、permissions、personality、
    compact permissions、multi-agent mode、collaboration mode、realtime、AGENTS.md
- `context_manager/updates.rs` 会把相邻可合并 fragment 合并成一条 message
- `world_state` 维护 baseline，后续以 `WorldStateItem::patch` 增量注入
- `reference_context_item` 支持模型可见 settings 的 diff/reinject
- `TurnContextItem` 持久化，rollout 可重建
- 有 `HookPrompt` / `additional_context` / `inject_fragment_without_turn`

## 7. 持久化 / 恢复 / 断点

### Athena

- `RolloutRecorder`：只追加消息和 compaction
- `AgentRuntime.resume_agent` 按 rollout 恢复记忆
- 上层 research state 有独立 breakpoint 机制
- 没有 thread store / state db / rollout search / metadata / compression

### Codex

- `rollout` crate：recorder、compression、metadata、search、reference index、
  session index、state db、persistence metrics、policy、reverse scanner
- `history` crate：RolloutItem 完整类型
- `thread-store`、`app-server` thread 持久化
- `fork_thread`、`resume`、`multi_agent_resume`、`compact_resume_fork`
- Guardian root conversation evidence / authorization version

## 8. Hooks / 生命周期扩展

### Athena

- 无通用 hook runtime
- 只有少量硬编码事件（tool begin/end/error、agent text/function_call）
- 没有 session start / turn stop / pre/post compact / pre/post tool hooks

### Codex

- `hook_runtime`：
  - session start hooks
  - turn stop hooks
  - pre/post compact hooks
  - pre/post tool use hooks
  - async hook results draining
  - pending input / additional context recording
- 支持 lifecycle notifications / execution mode / MCP tool hooks
- 有 `hooks` crate 和 app-server hook 协议

## 9. 模型 Provider 能力

### Athena

- `ResponsesProvider` 实际走 OpenAI `chat.completions` 流式
- 支持 openai/deepseek/qwen；Anthropic 是 stub
- 手动适配 json_schema / json_object + schema 注入
- 有 DSML 文本过滤
- 有简单流重试（5 次，指数退避）
- 没有模型能力发现、没有 remote compaction support、
  没有 Responses API / WebSocket / prompt cache / turn metadata / service tier

### Codex

- 多 provider / model-provider 能力
- Responses API over HTTP / WebSocket、turn-state sticky routing、fallback
- 模型上下文窗口、输入模态、reasoning effort/summary、service tier
- prompt cache key、request compression、incremental requests
- remote compaction support 探测
- `model_info.resolved_context_window()`、`effective_context_window_percent`
- 完整的 `ModelClientSession` 生命周期与 telemetry

## 10. 差异速览

| 维度 | Athena | Codex |
| --- | --- | --- |
| 运行模型 | Thread + Turn，简单 ActiveTurn | Session + TurnContext + StepContext + SessionTask |
| 输入生命周期 | Start/Interrupt | Start/Steer/Recover/Suspend/StartIfIdle |
| 事件模型 | 通用 kind + data | 类型化 item/delta/lifecycle |
| 工具注册 | 简单 name/schema/concurrency | namespace/exposure/tool search/router/readiness |
| 工具并行 | concurrency_safe flag | 全局 parallel gate + per-call cancellation |
| 工具输出 | ToolResult→字符串 | typed FunctionCallOutput/ContentItems/status |
| 审批/沙箱 | 未闭环 | guardian/approvals/network/exec policy/sandbox |
| 环境 | 无内建 | TurnEnvironment/executor/shell/capabilities |
| 上下文注入 | system prompt | fragments/world state/turn context diffs |
| Hooks | 无 | session/turn/compact/tool hooks |
| 多 Agent | mailbox/内存等待 | Multi-Agent V1/V2 + inter-agent communication + roles |
| Provider | chat.completions 兼容 | model provider/Responses/WebSocket/prompt cache |
| 持久化 | JSONL 消息+compaction | rollout/history/state db/search/compression/fork |
| 可观测性 | 简单事件/审计 | analytics/telemetry/rollout metrics/compaction events |
