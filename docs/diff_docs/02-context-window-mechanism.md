# Context Window 机制差异：Athena vs Codex

## 1. 上下文容器

### Athena：`ContextManager`

`src/athena/memory/context_manager.py`（TS 同名移植）是一个简单的内存列表：

- `_items: list[ModelMessage]`
- `_token_count`：维护 `sum(estimate_one(msg))`
- `_context_limit`：默认 `200_000`
- `_version`：append/replace/rollback 时递增
- `snapshot() -> (index, version)`、`items_since(idx)`、`rollback(idx)`、`replace_range()`
- `token_margin(ratio=0.85)` 只是 `0.85 * limit - tokens`

关键特性：

- 深拷贝 `items` 给外部
- 只对 `ModelRequest` 的 `tool-return` 做 `truncate_text` 预处理
- token 估算为 `len(str)//4` 的粗略估计
- 没有记录模型真实 token usage
- 没有 `token_info`、没有 server usage、没有 prefill baseline
- 没有 user-turn boundary 概念
- 没有 contextual fragment / world state / context baseline

### Codex：`ContextManager`（`core/src/context_manager/history.rs`）

Codex 的容器是 Arc 共享的 `Vec<ResponseItemEnvelope>`：

- `items: Arc<Vec<ResponseItemEnvelope>>`
- `history_version` 在重写/回滚时递增
- `token_info: Option<TokenUsageInfo>`：记录服务端 token usage
- `reference_context_item`：用于模型可见 settings diff 的基线
- `world_state_baseline`：用于增量注入 world state
- `for_prompt(input_modalities)` 做真正的 prompt 预处理：
  - 调用/输出配对归一化（缺失补合成输出，孤立输出删除）
  - 按模型 input modalities 剥离不支持的图片/音频
  - 保留 `ResponseItemEnvelope` metadata
- `remove_first_item` 会同步移除对应 call/output 对
- `drop_last_n_user_turns(num_turns)` 支持按用户 turn 回滚，并处理 pre-turn context updates
- `estimate_token_count` / `get_total_token_usage` 使用 JSON 序列化字节 + 多模态专门估算

## 2. Token 估算

### Athena

- `_estimate_one`：字符串长度/4，工具 args 序列化也计数
- 无法区分文本、图片、音频、reasoning、加密内容
- 只是一个“下限/量级”估算

### Codex

- `estimate_item_token_count(item)`：先计算模型可见字节数，再按 4 bytes/token 换算
- 对 inline base64 图片：
  - 普通图固定 `RESIZED_IMAGE_BYTES_ESTIMATE`（约 7373 bytes → 约 1844 tokens）
  - `detail=original` 按 32px patch 数和 `ORIGINAL_IMAGE_MAX_PATCHES` 估算，带 LRU 缓存
- 对音频：`estimate_audio_token_count`
- 对加密 reasoning/compaction：`estimate_reasoning_length` 等专门公式
- 对 encrypted function output：单独估算
- `get_total_token_usage` 利用服务端 `TokenUsageInfo`，区分 server-reasoning 是否已包含
- 还有 `items_after_last_model_generated_item` 处理本地新增、尚未被服务端计数的 item

## 3. 窗口状态与自动压缩阈值

### Athena

- `ContextManager.limit` 是固定数字，不随模型变化
- `Compactor.should_compact(ctx, at_tokens=170_000)` 仅检查 `ctx.tokens >= at_tokens`
- 没有模型 context window、没有 auto-compact scope、没有 full-context hard cap
- 没有 fallback buffer / token budget

### Codex

`session/context_window.rs` 的 `context_window_token_status` 计算：

- `active_context_tokens`：当前全部活跃上下文
- `auto_compact_scope_tokens` / `auto_compact_scope_limit`：
  - `Total`：整个上下文都计入
  - `BodyAfterPrefix`：只计初始 prefill 之后新增的 token，由
    `auto_compact_window.rs` 维护 `prefill_input_tokens`
- `full_context_window_limit`：`model_info.resolved_context_window() * effective_context_window_percent`
- `base_window_tokens_remaining`：取 auto-compact 余量与 full window 余量的较小值
- `auto_compact_fallback_buffer_tokens`：配置 token budget 时加缓冲
- `full_context_window_limit_reached` / `token_limit_reached`：强制压缩信号

`state/auto_compact_window.rs` 还维护：

- `window_number`
- `first_window_id` / `previous_window_id` / `window_id`
- `new_context_window_requested`
- `token_budget_reminder_delivered` / `auto_compact_fallback_delivered`
- server-observed prefill 优于 estimated prefill

## 4. 压缩策略

### Athena：本地摘要压缩

`src/athena/memory/compaction.py`：

- `keep_recent=20_000` token
- 从后往前找到保留分界，把旧消息交给轻量模型
- 生成 `[HISTORY SUMMARY]\n...` 的 `SystemPromptPart`
- `replace_range(0, split, [summary])`
- `record_compaction` 后 rollout 重新写 tail
- 仅支持本地 LLM 摘要
- 无 `remote compaction`、无 `mid-turn compaction`、无 manual compact
- 无前置/后置 hooks
- 无 compaction analytics（trigger/reason/implementation/phase/strategy/status）
- 无窗口 id

### Codex：多模式压缩

`tasks/compact.rs` 根据 provider 能力选择：

- `RemoteCompactionSupport::V2` + `RemoteCompactionV2` feature → remote V2
- `RemoteCompactionSupport::V2` → remote
- `Unsupported` → 本地 `run_compact_task`

`compact.rs` 本地压缩比 Athena 丰富：

- 保留最近真实 user messages（`COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`）
- 摘要取自“最后一次 assistant 消息”并带 `SUMMARY_PREFIX`
- 生成 `CompactedItem`，包含 `replacement_history`
- 支持 `InitialContextInjection::DoNotInject`（pre-turn/manual）与
  `BeforeLastUserMessage`（mid-turn，需要把 initial context 插回真实用户消息之前）
- 压缩过程中如果 context window exceeded，会从最老 item 逐个删除重试
- 有 pre/post compact hooks
- 有 analytics（trigger/reason/implementation/phase/strategy/status/tokens）
- 有 `window_number` / window ids 和自动窗口推进
- 有 `ContextCompaction` turn item 发到前端

`compact_remote*.rs` 还包含：

- `trim_function_call_history_to_fit_context_window`
- remote prompt 重写 / metadata 保留
- 图片预算、MCP resource origin checkpoint
- remote V2 attempt / fallback / retry

## 5. 触发时机

### Athena

- 只在每次 Turn 开始前 `maybe_compact()`
- `ThreadRuntime._run_turn`：`maybe_compact()` → 快照 → runner → 持久化/回滚
- 没有 pre-sampling 精确估算（pending user input / context updates 尚未计入）
- 没有 mid-turn 压缩

### Codex

- `run_turn` 中有 `run_pre_sampling_compact`
- 支持 inline auto compact（pre-turn、mid-turn）
- 支持独立 manual compact turn（`thread/compact/start`）
- 自动压缩会根据 `ContextWindowTokenStatus` 与 token budget 触发
- 压缩失败/ContextWindowExceeded 有专门的恢复路径

## 6. 持久化恢复

### Athena

`resume_context_sync`：

- 顺序读 JSONL
- 遇到 compaction 则重置 `ContextManager`
- 以 `[HISTORY SUMMARY]` system message 开始，再重放后续消息
- 只保留最新摘要；旧原消息不在 rollout 中

### Codex

- `RolloutItem::Compacted(CompactedItem)` 保存 `message` 与 `replacement_history`
- 恢复时可重建压缩后的完整历史（包括保留的 recent user messages、重新注入的 context）
- rollout 中还有 session meta / turn context / world state / event msgs，
  可重建完整会话，而不只是对话消息
- 有 `Forked` / `Resumed` / `Cleared` 初始历史模型

## 7. 差异速览

| 维度 | Athena | Codex |
| --- | --- | --- |
| 容器内容 | ModelMessage 列表 | ResponseItemEnvelope 列表 + metadata |
| 稳定 ID / 元数据 | 无 | ResponseItemId + 内部 passthrough |
| Token 估算 | 字符串长度/4 | JSON bytes + 图片/音频/加密专门估算 + server usage |
| Context window | 固定 200k | 按 model info 动态解析 + effective percent |
| Auto-compact scope | 无 | Total / BodyAfterPrefix |
| 强制 full window | 无 | model resolved context window hard cap |
| 压缩模式 | 本地摘要 | 本地 / remote / remote v2 |
| 压缩时机 | Turn 前 | pre-sampling / mid-turn / manual standalone |
| 压缩历史保留 | 只保留摘要 | 保留 recent user messages + replacement_history + context injection |
| Hooks | 无 | pre/post compact hooks |
| Analytics | 无 | CompactionEvent + 窗口/trigger/reason/status |
| 窗口 id | 无 | window_number + first/previous/window ids |
| 恢复 | 仅最新摘要+tail | 完整 replacement_history + session/world state |
