# Messages 机制差异：Athena vs Codex

## 1. 核心消息模型

### Athena：`ModelMessage`（PydanticAI / TS zod 移植）

Athena 的消息模型直接采用/复刻 `pydantic_ai.messages`：

- `ModelMessage = ModelRequest | ModelResponse`
- `ModelRequest.parts`：`SystemPromptPart`、`UserPromptPart`、`TextPart`、`ToolReturnPart`
- `ModelResponse.parts`：`TextPart`、`ToolCallPart`
- 每个 part 只有 `part_kind`、`content`、`tool_name`、`args`、`tool_call_id` 等最小字段
- 没有消息级稳定 `id`
- 没有消息级时间戳、`turn_id`、phase、delivery、citation、encrypted content 等元数据
- 没有 `reasoning`、`plan`、`command_execution`、`file_change`、`mcp`、`web_search`、`image_generation`、`review`、`context_compaction` 等独立 item 类型

TS 侧 `athena_ts/packages/athena-agent/src/messages.ts` 用 zod discriminated union 复刻了同一最小集合。

### Codex：`ResponseItem` / `TurnItem`

Codex 使用完整的 Responses API 风格 item 模型：

- `protocol/src/items.rs` 定义公开 `TurnItem`：
  - `UserMessage`、`AgentMessage`、`FunctionCallOutput`、`HookPrompt`
  - `Plan`、`Reasoning`、`CommandExecution`
  - `DynamicToolCall`、`CollabAgentToolCall`、`SubAgentActivity`
  - `WebSearch`、`ImageView`、`ImageGeneration`
  - `FileChange`、`McpToolCall`、`ContextCompaction`
  - `EnteredReviewMode` / `ExitedReviewMode`
  - 扩展项 `Extension`
- `protocol/src/models.rs` 定义底层 `ResponseItem`：
  - `Message { role, content, phase, internal_chat_message_metadata_passthrough }`
  - `AgentMessage { author, recipient, content }`
  - `Reasoning { summary, content, encrypted_content }`
  - `FunctionCall` / `FunctionCallOutput` / `CustomToolCall` / `CustomToolCallOutput`
  - `ToolSearchCall` / `ToolSearchOutput` / `WebSearchCall`
  - `ImageGenerationCall` / `LocalShellCall` / `Compaction` / `CompactionTrigger`
  - `ContextCompaction` / `AdditionalTools` / `Other`
- `ContentItem` 支持 `InputText`、`InputImage`、`InputAudio`、`OutputText`
- `ResponseItemId` 提供稳定 id；`InternalChatMessageMetadataPassthrough` 携带 `turn_id`、
  `create_time`、`content_item_kinds`、`cell_id`、`executed_tool_calls`、
  `tool_calls_complete` 等宿主元数据
- `ResponseItemEnvelope` 持久化时额外保留 `CodexHarnessMetadata`（例如
  `client_authored`），与原始 item 分离存储

**结论**：Athena 消息模型是“最小 LLM 往返模型”，Codex 是“完整宿主/协议 item 模型”。
Athena 只能表达对话和基础工具往返，Codex 能表达整个 agent 运行过程（推理、计划、命令、
文件变更、多 Agent 通信、检索、图像、压缩等）。

## 2. 发送给模型的请求表示

### Athena

`src/athena/core/agent/provider.py` 的 `_to_api()` 把 `ModelMessage` 压平为 OpenAI
Chat Completions `messages`：

- `system-prompt` → `role=system`
- `user-prompt` / `text` → `role=user`
- `tool-return` → `role=tool` + `tool_call_id`
- `ModelResponse` + tool-call → `role=assistant` + `tool_calls`
- 其它内容一律降级为文本或丢弃

因此：

- 多模态（图片/音频）没有一等表示
- 结构化工具输出只能转成字符串
- 没有 `reasoning`、`plan`、`web_search_*`、`image_generation_*` 等 item 透传
- 没有 namespace / tool search / deferred tools
- 没有稳定 item id、没有内联元数据

### Codex

`codex-rs/protocol/src/models.rs` 的 `ResponseInputItem` 是直接面向 Responses API 的输入：

- `Message { role, content: Vec<ContentItem>, phase }`
- `FunctionCallOutput { call_id, output }`
- `McpToolCallOutput { call_id, output }`
- `CustomToolCallOutput { call_id, name, output }`
- `ToolSearchOutput { call_id, status, execution, tools }`

工具调用/返回可以携带结构化 `FunctionCallOutputBody`（文本或 `ContentItems`），
支持图片、音频、加密内容；模型可见输出类型保留 `success`、`status`、`execution` 等语义。

## 3. 事件流 / 前端消费

### Athena

- `EventJournal` 只有 `Event { thread_id, turn_id, sequence, kind, event_ref, data }`
- `EventNotification` 同样只是 `kind + data`，没有类型化的 item 生命周期
- 事件种类是字符串常量（如 `agent/text_delta`、`agent/function_call`、`tool/begin` 等）
- GUI 从 rollout JSONL 重新归一化为展示消息

### Codex

- app-server 协议有 `item/started`、`item/completed`、`item/*Delta`
- 类型化事件包括：
  - `AgentMessageContentDeltaEvent`、`ReasoningSummaryTextDelta`
  - `ReasoningRawContentDelta`、`PlanDelta`
  - `CommandExecution` 状态/输出
  - `FileChange` diff、`McpToolCall` 生命周期
  - `ContextCompaction` item
  - `TurnStarted` / `TurnCompleted` / `TurnError`
- 前端可消费稳定的 item id、`phase`（commentary/final_answer）、`delivery`、`memory_citation`

## 4. 持久化 / rollout

### Athena

`src/athena/memory/rollout.py`（和 TS 移植）只写两种记录：

```json
{"seq": 0, "ts": "...", "msg": [ModelMessage...]}
{"seq": 1, "type": "compaction", "version": 1, "summary": "..."}
```

恢复时逐行读取，遇到新 compaction 就以摘要重新初始化上下文，再回放其后消息。
没有 session meta、没有 turn context/world state/event/security/realtime、没有索引。

### Codex

`codex-rs/history/src/lib.rs` 的 `RolloutItem` 是一等持久化类型：

- `SessionMeta(SessionMetaLine)`
- `ResponseItem(ResponseItemEnvelope)`（原始 item + harness metadata）
- `InterAgentCommunication` / `InterAgentCommunicationMetadata`
- `Compacted(CompactedItem)`（消息 + replacement_history + 窗口 id）
- `TurnContext(TurnContextItem)` / `WorldState(WorldStateItem)`
- `SecurityRiskScore`、`EventMsg`、`RealtimeItem`

`codex-rs/rollout` 还提供：

- 压缩存储（`compression.rs`）
- metadata / search / reference index
- rollout policy、persistence metrics
- state DB、reverse JSONL scanner
- rollout 文件名与 session index

**结论**：Athena 的 rollout 能“重放对话”，Codex 的 rollout 能“恢复整个会话状态”。

## 5. 多 Agent 消息

### Athena

`AgentMessage` 只是一个内存 mailbox 信封：

```python
AgentMessage(source, content, context_refs)
```

写入上下文时转成普通 user/text part；没有作者/接收者、没有结构化指令、没有加密内容。

### Codex

- `ResponseItem::AgentMessage { author, recipient, content }`
- `AgentMessageInputContent` 支持 `InputText` 与 `EncryptedContent`
- `InterAgentCommunication` 是历史/持久化的一等消息
- 多 Agent 有 `parent_turn_id`、`root_turn_id`、`MultiAgentVersion` 等元数据
- 协作工具调用（`CollabAgentToolCall`）单独建模

## 6. 差异速览

| 维度 | Athena | Codex |
| --- | --- | --- |
| 消息类型 | 2 种（request/response）+ 5 种 part | `ResponseItem` / `TurnItem` 数十种 |
| 消息 ID | 无 | `ResponseItemId` |
| 消息元数据 | 无 | phase/delivery/citation/turn_id/content_item_kinds/encrypted 等 |
| 多模态 | 无 | text/image/audio content items |
| 推理/计划/命令/文件 | 无 | 全部有独立 item |
| 工具输出结构 | 字符串/ToolResult | `FunctionCallOutputPayload` / `ContentItems` / 成功后失败状态 |
| 事件协议 | 通用字符串 kind | 类型化 item 生命周期 + delta |
| rollout 内容 | 仅消息 + compaction | session/meta/turn/world state/事件/安全/realtime 等 |
| 多 Agent 消息 | 内存 mailbox | 类型化 inter-agent communication + metadata |
