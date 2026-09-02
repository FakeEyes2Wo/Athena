# Athena 缺失部分与改进思路

基于前三个文档的比较，Athena 当前 messages 机制和 context window 机制都接近
“能跑的最小实现”，而参考 Codex 已把这部分做成“可恢复、可观测、可扩展的完整 harness”。
下面按模块列出缺失，并给出改进优先级和可选路线。

> 注意：Athena 已有文档《Athena 作为 DSH 插件重设计》计划把 TS 端运行在 DSH 之上，
> 复用 DSH 的 agent/turn/tool/subagent 能力。因此以下改进有两种落地方式：
> 1. 在现有 Python/自研 TS harness 内补齐；
> 2. 不再自研，转而把缺失能力交给 DSH/外部 harness，Athena 只保留研究域。
> 推荐在路线中先明确这条边界，避免重复造轮子。

## 1. Messages 层缺失

### 1.1 缺少完整的消息 item 类型

- 消息只有 request/response + 少量 part
- 没有 reasoning、plan、command execution、file change、web search、image generation、
  review、context compaction、MCP tool call、subagent activity 等类型化 item
- 无法忠实记录和回放完整的 agent 工作过程

**改进**：

- 若继续自研，引入统一的 `AthenaItem` discriminated union（类似 `TurnItem`）
- 至少增加 `reasoning`、`tool_call`/`tool_output`、`user_message`、`assistant_message`、
  `context_compaction`、`file_change` 等核心 item
- 前端/GUI 应从 item 类型渲染，而不是依赖通用字符串 kind

### 1.2 缺少稳定 ID 与消息元数据

- 没有 `message_id` / `item_id`
- 没有 `turn_id`、`create_time`、`phase`、`delivery`、`memory_citation`
- prompt cache 排序、增量更新、GUI 定位、审计都缺少锚点

**改进**：

- 给每条消息/item 分配稳定字符串 ID
- 消息上携带 `turn_id`、`seq`、`created_at`、`phase`（commentary/final_answer）
- 保留 harness 自有 metadata（例如来源 agent、client_authored）

### 1.3 缺少多模态与结构化工具输出

- Content 只有字符串
- 图片、音频、结构化 content items、加密内容都没有一等表示

**改进**：

- 将 `content` 从 `str` 抽成 `list[ContentItem]` 或至少支持 `{type: text|image|audio}`
- 工具输出使用 `{success, body}` 结构化载体，保留成功/失败语义

## 2. Context Window 层缺失

### 2.1 Token 估算过于粗糙

- `len(str)//4` 对中文、代码、图片、音频、reasoning 都不准
- 没有服务端 token usage，无法校准

**改进**：

- 接入 tokenizer 或按字节/字符分语言估算
- 记录每次 model 返回的 `usage.input_tokens` / `total_tokens`，用服务端值校准本地估算
- 多模态内容按类型单独估算

### 2.2 缺少模型真实的 context window

- 默认固定 `200_000`
- 没有 `model_info.resolved_context_window()`
- 没有 effective percent / full context hard cap

**改进**：

- 在模型配置中加入 `context_window` 和 `effective_context_window_percent`
- 在 turn 启动时解析当前模型的真实窗口，动态设置 limit

### 2.3 缺少 auto-compact scope / prefill / 缓冲

- 只有总 token 阈值，没有 `BodyAfterPrefix`
- 没有 prefill_input_tokens baseline
- 没有 fallback buffer / token budget

**改进**：

- 支持 `Total` 与 `BodyAfterPrefix` 两种计量范围
- 维护 compaction window 的 prefill 基线（优先 server-observed）
- 引入 token budget 与 fallback buffer，避免在边界反复压缩

### 2.4 压缩能力单一

- 只有本地摘要、只在 turn 前触发
- 无 manual compact、无 mid-turn compact、无 remote compact
- 无 pre/post hooks、无 analytics、无 window id

**改进**：

- 增加手动压缩入口（如 `thread/compact/start` 或内部命令）
- 在 turn 过程中也根据 `token_limit_reached` 触发 inline compact
- 若使用支持 remote compaction 的模型/provider，接入 remote compact
- 记录 compaction 的 trigger/reason/implementation/phase/status/tokens
- 引入窗口 id，便于恢复和观测

### 2.5 压缩后信息丢失严重

- Athena 把旧历史替换成一个摘要，原消息不在 rollout 中
- 没有保留最近真实 user messages，没有 `replacement_history`
- 没有把 initial context 放回正确位置（mid-turn 场景）

**改进**：

- 压缩 checkpoint 保存 `summary + replacement_history`
- 保留最近 N 条真实用户消息（或按 token 预算截断）
- 区分 pre-turn/manual（不注入 initial context）和 mid-turn（把 context 放回最后真实 user 消息之前）
- 持久化时同时记录 compressed item 与后续 tail，保证恢复可重建完整历史

## 3. Harness 层缺失

### 3.1 Turn/Session/Step 不完整

- 没有 StepContext：工具计划、环境、MCP、AGENTS.md 在每次采样之间会变化
- 没有 session 级完整状态：base instructions、world state、turn context、settings 历史
- 输入只有 Start/Interrupt，没有 steer/recover/suspend/start-if-idle

**改进**：

- 若自研，增加 `Session`、`TurnContext`、`StepContext` 三层
- 每个模型请求生成不可变 Step 快照，工具计划/环境/MCP 绑定到具体 step
- 支持 steer（向进行中的 turn 追加用户输入）和 recover（恢复中断 turn）

### 3.2 缺少上下文注入/WORLD STATE

- 没有 context fragments 合并、没有 world state diff、没有 reference context item
- 每次只能完整重发 system prompt

**改进**：

- 引入上下文 fragment 接口（环境、模型、工具、插件、权限、AGENTS.md、personality）
- 维护 `world_state_baseline`，后续以 patch 形式增量注入
- 维护 `reference_context_item`，支持 settings diff/reinject
- 将 turn context / world state 持久化到 rollout

### 3.3 工具层过于简单

- 无 namespace、exposure、tool search、deferred tools
- 无 per-step tool router
- 无 readiness、argument diff、tool hooks、telemetry
- 审批/用户输入仅有协议层 RPC，未形成工具级 guardian/沙箱/网络授权策略链
- 输出只有字符串

**改进**：

- 引入 `ToolSpec` 的 exposure/category/read_only/destructive/命名空间
- 若工具数量大，支持 deferred tool search 而不是把所有工具塞进 prompt
- 每个 Step 固定 `ToolRouter`，避免工具计划中途变化导致不一致
- 增加 pre/post tool use hooks 与工具生命周期事件
- 接入沙箱/审批/网络授权（至少对 shell、文件写、网络访问做门禁）
- 工具输出使用 typed payload，保留 success/error

### 3.4 缺少完整事件协议

- 通用 kind 字符串让前端/回放/测试都难以类型安全
- 没有 item started/completed/delta，无法流式展示推理、计划、命令、文件变更

**改进**：

- 定义类型化事件联合：`AgentTextDelta`、`ToolCallStarted`、`ToolCallCompleted`、
  `ReasoningDelta`、`PlanDelta`、`CommandOutput`、`FileChange`、`ContextCompaction`
- 保留向后兼容的通用事件通道，但新 UI 优先走类型化事件

### 3.5 缺少 Hook 与扩展点

- 没有 session start / turn stop / pre-post compaction / pre-post tool hooks
- 没有插件和扩展生命周期

**改进**：

- 设计最小 hook runtime：
  - `session_start`、`turn_start`、`turn_stop`
  - `pre_compact`、`post_compact`
  - `pre_tool_use`、`post_tool_use`
- Hook 可以记录/改写输入输出、阻断操作，为审批、审计、用户提示提供统一扩展点

### 3.6 持久化/恢复不足

- 只保存消息，不保存 session/thread metadata、turn context、world state、事件
- 没有索引、搜索、压缩、state db
- Agent 记忆恢复只重放对话，不恢复运行环境/工具计划/审批状态

**改进**：

- 扩展 rollout item：`session_meta`、`turn_context`、`world_state`、`event`、`compacted`
- 增加线程级索引（按 thread/session/agent id 查询）
- 为长会话提供 rollout 压缩策略（历史归档/裁剪）
- 支持 fork / resume / clear 等初始历史模式

### 3.7 Provider/模型能力缺失

- 只有 chat.completions，没有 Responses API/WebSocket/remote compaction/prompt cache
- 没有 model info 能力发现（context window、input modalities、reasoning、service tier）

**改进**：

- 抽象 provider capability：`context_window`、`input_modalities`、`remote_compaction`、
  `structured_output`、`streaming`
- 根据 capability 动态选择请求格式与压缩策略
- 接入 prompt cache 友好排序（尽量保持 prefix 稳定）

### 3.8 多 Agent 与协作不足

- mailbox + 内存 wait 很轻，但没有 agent roles/identity、inter-agent message 类型、
  子代理通知注入、加密切片、parent/root turn 传播

**改进**：

- 给 `AgentMessage` 增加结构化 payload、来源/目标、类型
- 增加 parent/root turn id 传播
- 子代理完成/失败时向父代理上下文注入类型化通知
- 若继续走 DSH 路线，优先复用 DSH subagent 能力，不再自研 AgentRuntime

## 4. 推荐优先级

| 优先级 | 事项 | 理由 |
| --- | --- | --- |
| P0 | 明确“继续自研 harness”还是“复用 DSH/外部 harness” | 避免反复造轮子，决定后续所有投入 |
| P0 | 消息/item 加稳定 ID 和 typed item 基础 | 所有恢复、展示、审计、事件都依赖它 |
| P0 | 动态 context window + 服务端 usage 校准 | 避免超窗、压缩误判 |
| P1 | 压缩 checkpoint 保存 replacement_history + recent user messages | 解决压缩后信息丢失、恢复不完整 |
| P1 | 引入 StepContext 或至少 per-turn tool plan/environment snapshot | 工具计划与环境在 turn 内必须一致 |
| P1 | 工具输出 typed payload + 基础审批/沙箱 | 提升正确性、安全性和可扩展性 |
| P1 | 类型化事件流（item started/completed/delta） | 前端和回放体验的关键 |
| P2 | context fragments / world state diff | 长会话上下文可控，prompt 更省 |
| P2 | hooks / 插件扩展点 | 审批、审计、用户交互可插拔 |
| P2 | rollout 扩展 / 索引 / 搜索 / 压缩 | 大工作量但长期收益高 |
| P3 | 多 Agent roles / inter-agent communication 完整化 | 研究域复杂协作时再深入 |
| P3 | remote compaction / Responses API / WebSocket | 依赖后端能力，可按需接入 |

## 5. 实施建议

1. **不要一次性重写**。先做“可观测性”和“数据模型”两层：
   - 消息带 id/元数据、rollout 记录 typed item
   - context window 状态可查询、压缩事件可追踪
2. **以小步对齐 Codex 的核心路径**：
   - 动态窗口 → prefill/scope → 压缩策略 → replacement_history
   - 再从工具 router → hooks → step snapshot → 审批/沙箱
3. **如果确定走 DSH 插件路线**，则本仓库的 `athena_ts` M1 自研 agent/context 可降级，
   上述 P0-P1 中与 agent harness 重合的部分应交给 DSH；Athena 只需补齐研究域所需
   的持久化契约、事件适配和顶层编排。
