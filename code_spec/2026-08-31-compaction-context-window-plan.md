# Compaction / Context Window 分阶段实施计划

日期：2026-08-31
状态：v2 revised implementation plan（已按设计缺陷反思修订）
关联设计：`code_spec/2026-08-31-compaction-context-window-design.md`
范围：Python 核心链路 + TypeScript 平行移植（TS 仅同步数据模型与恢复）
目标：按可独立验证的阶段，把 design 转成可落地的代码改动；每阶段有边界条件、测试、回滚路径。

> v2 修订：所有阶段已同步“input_tokens vs total_tokens”、“input hard cap / output reserve”、“protected_start”、“replacement 整体预算”、“checkpoint 后写 replacement_count 之后的 tail”、“三游标”、“MemoryController 接线”、“remote 占位”、“per-model registry”。

---

## 0. 计划总览

| 阶段 | 内容 | 主要风险 | 可独立验证 |
|---|---|---|---|
| Phase 1 | 配置与 ModelWindowProfile | 配置缺省、模型未知、percent 非法 | 单测配置解析 |
| Phase 2 | ContextManager token / prefill / scope | 无 server usage、首轮 prefill 缺失、模型切换 | 单测 status |
| Phase 3 | CompactionCheckpoint / split / 本地摘要 | 用户消息识别、replacement 超预算、摘要模型截断 | 单测 checkpoint |
| Phase 4 | Rollout 记录与恢复 | 新旧格式兼容、损坏行、重复 tail | 恢复测试 |
| Phase 5 | Pre-turn 集成 | 压缩失败不阻断 turn、持久化顺序 | 集成测试 |
| Phase 6 | Mid-turn inline | cursor 修复、rollout 一致性、失败回滚 | 模拟长 turn 测试 |
| Phase 7 | Manual compact + RPC | busy/closed/并发 | API 测试 |
| Phase 8 | Remote compact 占位 + fallback | 后端不支持、失败回退 | 假 provider 测试 |
| Phase 9 | Hooks / analytics / GUI | 事件过大、hook 异常、脱敏 | 事件读取测试 |
| Phase 10 | 回归与验收 | 兼容旧 rollout、TS 同步 | 全量测试 |

每个 Phase 应单独提交、单独可回滚；不要在单个大 PR 里同时完成全部阶段。

---

## Phase 1：模型配置与 ModelWindowProfile

### 1.1 目标

- 新增 `ModelWindowProfile` 值对象。
- 从 `settings` / `config.toml` 读取真实窗口与压缩参数。
- 扩展 provider capability，为 dynamic window 提供来源。
- 删除压缩阈值中的硬编码 `170_000` 路径，保留未知模型时的保守 fallback。

### 1.2 具体步骤

1. 在 `src/athena/memory/` 下新增 `window_profile.py`（或放在 `core/agent/models.py`，以不引入循环依赖为准）。
   - 定义 `ModelWindowProfile`、`ModelRegistry`、`AutoCompactScope`、`ProviderCapabilities`。
2. `settings.py` 增加读取：
   ```python
   def context_window() -> int | None
   def output_reserve_tokens() -> int | None
   def effective_context_window_percent() -> float
   def compaction_config() -> dict
   def model_registry() -> dict[str, ModelWindowProfile]
   ```
3. `config.example.toml` 增加 `[llm.compaction]` 与 per-model `[models."..."]` 示例。
4. `BaseProvider` 增加 `capabilities` property；先采用表驱动/静态值。
5. `Compactor.__init__` 改为接收 `profile: ModelWindowProfile | None`，不接收裸 `keep_recent` + 数字阈值。
6. 在 `AgentRuntime` / `ThreadRuntime` 创建时按 `ModelRegistry` 解析当前模型 profile，并传入 `ContextManager` 和 `Compactor`。
7. `ModelWindowProfile` 必须实现 `input_hard_cap()` = `resolved_context_window() - output_reserve`，所有压缩判定使用 input hard cap。

### 1.3 边界条件与决策

| 边界 | 处理 |
|---|---|
| `context_window` 未配置 | 查 provider capability；仍没有则使用保守默认（例如 128_000）并打 warning，不允许静默认为 200k |
| `context_window <= 0` | 视为未配置；同上 |
| `output_reserve_tokens` 未配置 | 回退到全局 `max_tokens`；两者都未知则按 `resolved * 0.9` 保守预留 |
| `effective_context_window_percent` 缺省 | 默认 `0.80` |
| percent `< 0` 或 `> 1` | 夹到 `[0.10, 1.00]`，打 warning；不抛错 |
| `auto_compact_token_budget` 缺省 | 用 `input_hard_cap() * 0.65` |
| budget ≥ input hard cap | 夹到 `input_hard_cap - fallback_buffer`；仍小于 buffer 时夹到 `input_hard_cap * 0.5`，并 warning |
| fallback_buffer ≥ input hard cap | 夹到 `input_hard_cap * 0.10`，并 warning |
| `replacement_token_budget` 未配置 | 按 `input_hard_cap * (1 - post_compact_target_ratio)` 推算 |
| `keep_recent_user_messages = 0` | 关闭保留真实用户消息，只保留摘要 + tail |
| `keep_recent_tokens = 0` | 不保留 tail，可能把全部旧历史压缩；必须允许但 warning |
| 模型在会话中途切换 | 以当前 turn 的 model 重新解析 profile；若模型名变化，重置 `AutoCompactWindowState`（新窗口、新 prefill），避免旧窗口的 limit 失效 |
| provider capability 与 config 冲突 | 显式配置优先；未配置时用 provider capability；两边都未知用保守默认 |
| 配置文件解析失败 | 回退所有默认，不崩；日志记录 |
| `summary_model` 不可用 | 只能在真正压缩时失败，不阻塞启动 |
| 不同模型只有全局配置 | 必须提供 `ModelRegistry` 或全局 + provider fallback；禁止把所有模型当成同一窗口 |

### 1.4 验收

- `ModelWindowProfile.resolved_context_window()` / `input_hard_cap()` 对合法/非法 percent 和 output reserve 都稳定。
- 不再有 `should_compact(ctx, at_tokens=170_000)` 调用路径。
- 同一进程内两个不同模型可得到不同 `input_hard_cap`。

---

## Phase 2：ContextManager token 计量 / prefill / scope

### 2.1 目标

- `ContextManager` 能记录 server usage、prefill baseline、window state。
- 能计算 `Total` 与 `BodyAfterPrefix` 两种 scope。
- 能生成 `ContextWindowStatus`，供压缩判定。

### 2.2 具体步骤

1. 扩展 `ContextManager`：
   - 字段：`_server_usage`、`_prefill_input_tokens`、`_prefill_source`、`_window_profile`、`_window_state`、`_turn_start_index`、`_persist_cursor`、`_server_token_limit_reached`。
   - 方法：`set_window_profile`、`set_turn_start`、`set_persist_cursor`、`update_server_usage`、`set_prefill_baseline`、`scope_tokens`、`token_status`、`rebase_snapshot_after_replace`、`real_user_message_indices`。
2. 定义 `TokenUsageInfo`；明确 `input_tokens` 是 active 口径，`total_tokens` 仅诊断。
3. 定义 `ContextWindowStatus` 计算函数，使用 `input_hard_cap()` 而非 `resolved_context_window()`。
4. 在 provider 流加入 `stream_options={"include_usage": True}`，并在 `response_completed` 事件中返回 `usage`。
5. 在 `_sample_once` / `BaseAgent.run` 中调用 `ctx.memory.update_server_usage(...)`，且只取 `input_tokens` 更新 active。

### 2.3 关键算法

```python
def active_context_tokens(self):
    # 服务端 input_tokens 才是当前上下文大小；total_tokens 含输出，禁止用于窗口判定。
    if self._server_usage is not None and self._server_usage.input_tokens is not None:
        return self._server_usage.input_tokens
    return self._token_count  # 本地 estimate 兜底
```

```python
def scope_tokens(self, scope):
    active = self.active_context_tokens()
    if scope == AutoCompactScope.TOTAL:
        return active
    prefill = self._prefill_input_tokens
    if prefill is None:
        # 尚无第一次请求：只能按 Total 估计，避免误判
        return active
    return max(0, active - prefill)
```

```python
def token_status(self):
    profile = self._require_profile()
    full = profile.resolved_context_window()
    input_hard_cap = profile.input_hard_cap()
    auto_limit = profile.auto_compact_limit()
    scope = self.scope_tokens(profile.auto_compact_scope)
    active = self.active_context_tokens()
    remaining = max(0, auto_limit - scope)
    return ContextWindowStatus(
        active_context_tokens=active,
        server_input_tokens=self._server_usage.input_tokens if self._server_usage else None,
        server_total_tokens=self._server_usage.total_tokens if self._server_usage else None,
        auto_compact_scope_tokens=scope,
        auto_compact_scope_limit=auto_limit,
        full_context_window_limit=full,
        input_hard_cap=input_hard_cap,
        output_reserve_tokens=profile.output_reserve_tokens,
        fallback_buffer_tokens=profile.auto_compact_fallback_buffer_tokens,
        base_window_tokens_remaining=remaining,
        token_budget_remaining=remaining,
        token_limit_reached=self._server_token_limit_reached,
        input_hard_cap_reached=active >= input_hard_cap,
        should_auto_compact=scope >= auto_limit or active >= input_hard_cap - profile.auto_compact_fallback_buffer_tokens,
        should_force_compact=active >= input_hard_cap or self._server_token_limit_reached,
    )
```

### 2.4 边界条件与决策

| 边界 | 处理 |
|---|---|
| 第一次模型请求前没有 server usage | `prefill` 为 None；`BodyAfterPrefix` 按 Total 处理，避免“active - 0”导致过度乐观 |
| 第一次请求有 server `input_tokens` | 将其作为该 window 的 `prefill_input_tokens`，source=server |
| 第一次请求无 usage | 用请求前本地 estimate 作为 prefill，source=estimated；后续有 server usage 时**不覆盖**已建立的 prefill，保持窗口基线稳定 |
| 第二次及以后请求的 usage | 只更新最近 usage 用于校准/告警，不更新 prefill baseline |
| compaction 后新窗口 | 清空 prefill；下一次模型请求重新采样 |
| server 返回 `total_tokens` 但未返回 `input_tokens` | 无法建立 prefill；继续用估计值 |
| `cached_input_tokens` | 只用于诊断/prompt cache 分析，不直接改变 scope（可由后续优化接入） |
| 本地 estimate 与 server usage 差异巨大 | `active_context_tokens` 优先使用 server `input_tokens`；`total_tokens` 仅诊断；日志记录差值 |
| 模型切换 | 重置 prefill、window state、server usage；避免旧窗口污染 |
| 消息被 replace 后 token 数减少 | scope 允许变小；`max(0, active - prefill)` 防止负数 |
| `active_context_tokens` 超过 Python int 范围 | 实际不会；仍用 int 并避免 float |
| 多次调用 `token_status` 性能 | 计算 O(1)，只读已维护的计数；不要在 status 中遍历 items |

### 2.5 验收

- 无 server usage 时 `BodyAfterPrefix` 不误判。
- 服务端 `input_tokens` 到达后可正确计算 prefill 和 scope；`total_tokens` 不参与窗口判定。
- 模型切换会 reset window state。
- `ContextWindowStatus` 五个边界（未到 / auto / fallback / input hard cap / server token limit）单测通过。

---

## Phase 3：CompactionCheckpoint 与 split 选择

### 3.1 目标

- 压缩结果从“只有 summary”升级为“summary + replacement_history + 元数据”。
- 保留最近真实用户消息。
- 支持 pre-turn / mid-turn / manual 的 initial context 注入策略。

### 3.2 数据模型

```python
@dataclass(slots=True)
class CompactionCheckpoint:
    version: int
    summary: str
    replacement_history: list[ModelMessage]
    original_items: list[ModelMessage]
    window_id: str
    window_number: int
    trigger: str
    reason: str
    implementation: str
    phase: str
    status: str
    tokens_before: int
    tokens_after: int
    scope_tokens_before: int
    scope_tokens_after: int
    prefill_input_tokens: int | None
    initial_context_injection: InitialContextInjection
    protected_start: int = 0
    replacement_count: int = 0
    compact_attempt: int = 1
    error: str | None = None
```

### 3.3 具体步骤

1. 将 `Compaction` 改名为/扩展为 `CompactionCheckpoint`（旧字段保留或兼容）。
2. 实现 `Compactor.select_split(ctx, profile, *, protected_start=0)`：
   - `protected_start` 是 mid-turn 的硬保护边界；pre-turn/manual 传 0；
   - `raw_split` 不得越过 `protected_start`。
3. 实现“真实用户消息”识别：
   ```python
   def is_real_user_message(msg):
       if not isinstance(msg, ModelRequest):
           return False
       return any(
           getattr(p, "part_kind", None) == "user-prompt"
           and not str(getattr(p, "content", "")).startswith("[ATHENA MAILBOX MESSAGE]")
           for p in msg.parts
       )
   ```
4. 实现保留块构建：
   - 从旧区间尾部反向扫描真实用户消息。
   - 对每条真实用户消息，带上它之后到下一个真实用户消息之前的 assistant/tool 消息。
   - 受 `keep_recent_user_messages` 和 `keep_recent_user_budget_tokens` 双重限制。
5. 生成摘要时排除已保留块，避免把保留的真实内容再摘要一次。
6. 构造 `replacement_history = [summary_message] + preserved_blocks`，并记录 `replacement_count`。
7. 对 mid-turn 注入 initial context：
   - 从旧区间中提取系统/initial context 消息；
   - 若 phase == "mid_turn" 且存在保留的真实用户消息，将 initial context 插到最后一条保留真实用户消息之前；
   - 否则按 `DoNotInject` / `AtStart`。
8. 计算 tokens_before/after、scope_before/after。
9. 增加整体预算循环：
   - 若 `replacement` 总 token 超过 `replacement_token_budget`，先减少保留用户块，再缩短摘要；
   - 若 `replacement + tail` 仍超 `post_compact_target`，扩大 compact 范围/继续压缩；
   - 最多 `max_compact_attempts_per_turn` 次；仍失败则 `drop_oldest`。

### 3.4 边界条件与决策

| 边界 | 处理 |
|---|---|
| 旧区间为空 | 返回 skipped，不生成 checkpoint；不写 rollout |
| 旧区间只有系统 prompt / 无用户消息 | 只生成摘要，不保留用户块 |
| 最近真实用户消息超过预算 | 从最新往旧保留，直到 budget 用尽；更旧的进入摘要 |
| 保留块与 tail 重叠 | 不允许：保留块只从 `old = items[:split]` 中取，tail 从 `items[split:]` 开始 |
| mid-turn `raw_split` >= `protected_start` | split 收缩到 `protected_start`，当前 turn 消息绝不进入旧区间；如果因此无旧消息，返回 skipped |
| `replacement_history` 超过 `replacement_token_budget` | 减少保留用户块 → 缩短摘要 → 扩大 compact 范围 → drop_oldest |
| replacement + tail 仍超 `post_compact_target` | 迭代压缩；超过 `max_compact_attempts_per_turn` 后报 `ContextWindowExceeded`，不得无限循环 |
| 摘要 prompt 本身超过摘要模型窗口 | 对旧区间分块做层级摘要：先分段摘要，再合并；仍失败则 drop_oldest |
| 摘要模型返回空字符串 | 视为失败；尝试一次降级摘要，再失败走 drop_oldest |
| 摘要输出被 max_tokens 截断 | 保留截断文本并加 `[SUMMARY TRUNCATED]` 标记，不能静默 |
| `replacement_history` 含超长工具返回 | 保留时仍走 `ContextManager._prepare` 截断；摘要阶段只取摘要需要的头部 |
| 用户消息是空文本 / 只有图片 | 按真实用户消息保留原始消息结构，不因文本为空丢弃 |
| mailbox 信封误判为真实用户 | 用 `[ATHENA MAILBOX MESSAGE]` 前缀排除；后续新增自定义前缀也列入排除表 |
| system prompt 在旧区间中间 | mid-turn 注入应保持 relative order；如果 initial context 已在 tail 中，不得重复注入 |
| 多个 system prompt / 多段 initial context | 作为一组注入，保持顺序 |
| 手动压缩时是否保留 recent user messages | 保留（manual 只是触发方式，不改变信息保留策略） |
| 压缩后 tokens_after 仍 > hard cap | 进入 drop_oldest 循环，每次删除最老 item 并重估 |
| 压缩过程中上下文被并发修改 | 保留现有 version check；失败时返回 failed，不写 rollout |
| `original_items` 过大 | 仅进程内保存，用于失败回滚/调试；默认不写入 rollout，避免 JSONL 巨幅膨胀 |
| 远程实现不产生 replacement_history | 由 remote adapter 构造等价 checkpoint；无法构造时回退本地 |
| 同 turn 内多次压缩 | 每次生成新的 window_id；replace 只作用于当前旧区间；history 不跨窗口累积 |

### 3.5 验收

- replacement_history 不丢失最近真实用户消息。
- replacement 与 tail 无缝衔接，没有重复或遗漏。
- mid-turn 压缩绝不吞当前 turn 消息（protected_start 单测）。
- replacement 总预算/整体 post target 可迭代，不会无限循环。
- mid-turn 压缩后 initial context 位于最后真实用户消息之前。
- 多层边界（无旧消息、全空、超预算、摘要失败、hard cap）均单测覆盖。

---

## Phase 4：Rollout 持久化与恢复

### 4.1 目标

- 写出新格式 compaction checkpoint。
- 旧格式仍可读；新格式旧代码也可忽略新字段。
- 恢复时重建最近窗口的真实上下文。

### 4.2 记录格式

```json
{
  "seq": 123,
  "type": "compaction",
  "version": 42,
  "window_id": "...",
  "window_number": 3,
  "previous_window_id": null,
  "first_window_id": "...",
  "trigger": "auto",
  "reason": "token_budget",
  "implementation": "local_summary",
  "phase": "mid_turn",
  "status": "completed",
  "summary": "[HISTORY SUMMARY]\n...",
  "replacement_history": [ ... ],
  "initial_context_injection": "BeforeLastUserMessage",
  "tokens_before": 151000,
  "tokens_after": 43000,
  "scope_tokens_before": 81000,
  "scope_tokens_after": 23000,
  "prefill_input_tokens": 18000
}
```

### 4.3 具体步骤

1. `RolloutRecorder.record_compaction(ckpt: CompactionCheckpoint)` 写全量元数据。
2. 保留旧版 `record_compaction(version, summary)` 兼容入口或内部兼容分支。
3. `resume_context_sync`：
   - 遇到 compaction 且存在 `replacement_history` → 用该列表重建；
   - 否则回退旧摘要逻辑。
4. 恢复后设置 `AutoCompactWindowState`。
5. `traces.py` / `read_trace` 支持新字段展示。

### 4.4 持久化协议（含 mid-turn，修订 v2）

使用三个游标：

- `turn_start_index`：当前 turn 第一条新消息之前的位置，用于保护 split。
- `persist_cursor`：尚未写入 rollout 的分界。
- `rollback_cursor`：失败时回滚到的位置（提交式 = 最后一次 compact 后的 persist_cursor；可撤销式 = turn_start_index）。

流程：

- **turn 开始 / pre-turn 完成后**：`turn_start_index = persist_cursor = rollback_cursor = ctx.snapshot()[0]`。
- **mid-turn 成功 compact 后**：
  1. `rollout.record_compaction(ckpt)`；
  2. 写出 `ctx.items[ckpt.replacement_count:]`，**不是 `items[1:]`**（因为 checkpoint 已包含 replacement_history）；
  3. `persist_cursor = ctx.snapshot()[0]`；
  4. 默认提交式：`rollback_cursor = persist_cursor`。
- **turn 正常结束**：`record_items(ctx.items_since(persist_cursor))`。
- **turn 失败且未发生 mid-turn compact**：`rollback(turn_start_index)`，丢弃本轮所有新消息。
- **turn 失败且已发生 mid-turn compact（提交式）**：`rollback(rollback_cursor)`，只丢弃 compact 之后的新消息；已写出的 checkpoint + tail 保留。
- **turn 失败且已发生 mid-turn compact（可撤销式）**：先 `replace_range` 还原 `original_items`，再 `rollback(turn_start_index)`；如果 checkpoint 已写盘，还需要额外处理磁盘上的 checkpoint 与内存恢复一致性。

该协议避免单一 `items_since(旧 cursor)` 在中途 replace 后越界，也避免把 `replacement_history` 中的保留消息重复写入。

### 4.5 边界条件与决策

| 边界 | 处理 |
|---|---|
| 旧 rollout 只有 `{type,version,summary}` | 兼容回退：摘要 + tail |
| 新 rollout 被旧代码读取 | 旧代码只看 `summary`，忽略新字段，不崩 |
| checkpoint 后误写 `items[1:]` | 会导致恢复重复；必须按 `replacement_count` 之后开始写，测试中显式断言无重复 |
| `replacement_history` 不是合法 JSON / 格式损坏 | 跳过该条 compaction，退回旧摘要逻辑；若该字段存在但为空列表，也回退 |
| rollout 中途截断（半行 JSON） | 跳过损坏行；保留之前已成功恢复的部分 |
| 多个 compaction 记录 | 以最后一个有效记录为当前窗口；之间旧 tail 不再重放 |
| compaction 后没有后续消息 | 上下文 = replacement_history；恢复正常 |
| compaction record 的 version 与消息 version 不一致 | 以 compaction record 为权威；后续 msg 按文件顺序追加 |
| 磁盘写入失败 | compaction 记录失败则视为压缩未持久化；内存可保留但必须发 failed analytics；禁止把 persist_cursor 前移 |
| replacement_history 巨大导致单行过大 | 可接受（JSONL 本身如此）；但 analytics 不写全量，只写 count |
| 旧 `HISTORY_SUMMARY_PREFIX` 是否保留 | 保留；summary 文本仍带该前缀，新 replacement_history 中的 summary message 也带 |
| `resume_context` 与 `resume_context_sync` 一致性 | 两份实现必须同步；TS 也同步 |
| 从空文件恢复 | 返回空 ContextManager，不报错 |
| 文件权限不足 | 抛 OSError 并由上层处理，不静默丢数据 |

### 4.6 验收

- 旧 rollout 可恢复出等效旧行为。
- 新 rollout 可恢复出含 recent user messages 的完整上下文。
- 损坏/截断 rollout 不导致整体失败。
- mid-turn 持久化协议下，恢复后的上下文与内存一致。

---

## Phase 5：Pre-turn 自动压缩集成

### 5.1 目标

- `ThreadRuntime.maybe_compact()` 改用新 `ModelWindowProfile` / status。
- 压缩结果持久化并发出事件。
- 压缩失败不应无条件杀死 turn。

### 5.2 具体步骤

1. 修改 `maybe_compact` 签名：
   ```python
   async def maybe_compact(
       self,
       *,
       phase: str = "pre_turn",
       reason: str = "auto",
       force: bool = False,
   ) -> CompactionCheckpoint | None
   ```
2. 在 `_run_turn` 开头调用。
3. 成功后写 rollout、更新窗口、emit `context_compaction`。
4. 失败时 emit failed event，继续执行 turn；若 `input_hard_cap_reached` 且失败，则返回 `ContextWindowExceeded` 让上层决定。

### 5.3 边界条件与决策

| 边界 | 处理 |
|---|---|
| 没有 compactor / llm / ctx | 静默返回 None |
| 没有旧消息可压缩 | 返回 skipped 不写 rollout |
| 压缩成功但 rollout 写失败 | 保留内存压缩结果，发 failed analytics；下次 turn 前可能再次压缩 |
| 压缩失败但未到 hard cap | 记录 failed，turn 继续 |
| 压缩失败且已到 hard cap | 若 provider 可处理则继续；若无法处理，向上抛 `ContextWindowExceeded` 或 `ModelRequestError`，避免无限重试 |
| pre-turn 与手动并发 | Submission loop 已串行；仍用 runtime 内部锁保护 ctx 操作 |
| `should_compact` 返回 False 但 server 刚报 token_limit | 以 server 信号强优先 |
| 模型在 turn 间切换 | 在 maybe_compact 前重新解析 profile，并检查是否需要 reset window state |
| token budget 刚触发但增长不足防抖 | 不压缩，继续 |
| 压缩后 still near hard cap | 在 same maybe_compact 内继续 drop_oldest 直到安全，或返回 warning |

### 5.4 验收

- 超阈值 turn 前自动压缩。
- 压缩失败不影响正常短 turn。
- 事件/日志能看到 trigger/reason。

---

## Phase 6：Mid-turn inline compact

### 6.1 目标

- 在长 turn 的工具循环中，根据服务端 `input_tokens` usage / token_limit 触发压缩。
- 保证 `turn_start_index` / `persist_cursor` / `rollback_cursor` 三者在 replace 后都正确。
- 保证当前 turn 消息不被 mid-turn compact 吞掉。
- 保证失败回滚和 rollout 记录一致。

### 6.2 接线（修订 v2：显式 MemoryController）

1. 新增 `MemoryController`，由 `ThreadRuntime` 持有：
   ```python
   class MemoryController:
       async def maybe_compact(
           self,
           *,
           phase: str = "mid_turn",
           reason: str = "auto",
           force: bool = False,
           protected_start: int | None = None,
       ) -> CompactionCheckpoint | None: ...
   ```
2. `ThreadRuntime._run_turn` 修改 runner 调用：
   ```python
   contextual_runner(
       thread, turn, emit, runtime._ctx, cancel,
       memory_controller=self._memory_controller,
   )
   ```
3. `_ThreadRunner.run_with_context` 增加 `memory_controller=None` 参数，并把它放进 `RunSession`。
4. `BaseAgentRunner` 从 `session.memory_controller` 取出并注入 `AgentContext.compact_if_needed`。
5. `AgentContext` 增加：
   ```python
   compact_if_needed: Callable[[str], Awaitable[CompactionCheckpoint | None]] | None = None
   ```
6. `BaseAgent.run` 在每次 `_sampling_loop` 返回 `continue` 后调用：
   ```python
   if ctx.compact_if_needed is not None:
       await ctx.compact_if_needed("mid_turn")
   ```
7. 旧 runner 未传 `memory_controller` 时保持 `None`，不触发 inline。
8. `provider.stream` 收集 `input_tokens` usage；`_sample_once` 更新 memory。

### 6.3 控制流

```text
每一次采样完成后:
  1. 工具结果写回 mem
  2. 若 continue:
       a. 用服务端 input_tokens 更新 server usage
       b. 调用 compact_if_needed("mid_turn")
       c. 若成功:
          - ThreadRuntime 以 protected_start = turn_start_index 执行 split
          - 写 checkpoint
          - 只写 ctx.items[replacement_count:]（tail）
          - 更新 persist_cursor = len(mem)
          - 提交式：rollback_cursor = persist_cursor
          - rebase turn_start_index / persist_cursor / rollback_cursor
          - emit analytics
       d. 下一次采样前再次检查 cancel
```

### 6.4 边界条件与决策

| 边界 | 处理 |
|---|---|
| runner 未接入 callback | 不触发 inline，行为退回旧版 |
| tool 仍在执行 / 并发任务未完成 | 只在采样间隙调用，不在工具执行中途修改 mem |
| server usage 未返回 | 用本地 estimate；仍可能触发 auto |
| server 返回 token_limit_reached 错误 | 标记强制；在下一个采样间隙压缩；若 provider error 直接终止，则在错误处理路径尝试一次 compact 后重新请求 |
| mid-turn compact 与 turn 失败 | 默认提交式：已写 rollout 的 checkpoint/tail 保留，只回滚 rollback_cursor 之后的新消息 |
| compact 后 `persist_cursor` / `turn_start_index` / `rollback_cursor` 未更新 | 后续 `items_since` 会越界/丢消息；三个游标必须一起 rebase |
| compact 时替换范围包含当前 turn 的新消息 | 不允许：`protected_start = turn_start_index` 是硬边界；若 split 尝试越过，直接收缩或 skipped |
| checkpoint 后误写 `items[1:]` | 会重复恢复；必须写 `items[replacement_count:]` |
| 同 turn 多次 compact | 每次更新 window_id、persist_cursor、rollback_cursor；后续 tail 写入基于最新 context |
| mid-turn 压缩后 turn 失败且采用可撤销式 | 需额外处理已写盘 checkpoint 的撤销/一致性；默认提交式不处理 |
| 压缩后仍 continue | 继续原循环；`_sampling_loop` 使用新的 `mem.items` |
| 压缩后 cancel | 正常取消路径：已持久化的 checkpoint/tail 保留，只丢弃 compact 后新增未写消息 |
| 压缩本身抛异常 | 捕获，记录 failed，继续原 turn；若已到 hard cap，转成可恢复错误 |
| 当前 turn 刚开始、还没有新消息 | mid-turn callback 可能仍触发（如果旧上下文超限）；行为等同于 pre-turn，但 phase 记 mid_turn |
| `ModelResponse` 的 text 还没写 mem？ | `_finalize_step` 先写 mem 再返回 continue，所以 callback 时上下文完整 |

### 6.5 验收

- 模拟长工具循环能一次或多次 inline 压缩。
- 压缩后 turn 能正常完成，当前 turn 消息不丢失。
- 失败回滚后内存与 rollout 一致。
- 三个游标在 replace 后都正确，`items_since` 不再因 replace 丢消息。
- checkpoint 后恢复不会重复 replacement_history 中已保留的消息。

---

## Phase 7：Manual compact + RPC

### 7.1 目标

- 增加独立的手动压缩入口，不依赖普通 turn。
- 前端/RPC 可调用 `thread/compact/start`，同时提供内部命令/CLI/TUI 入口。

### 7.2 具体步骤

1. `submissions.py` 增加 `CompactThread`。
2. `ThreadRuntime` 增加 `compact_now(force=True, reason="manual")`。
3. `submission_loop` 增加分支。
4. `ThreadHandle` 增加 `compact()`。
5. GUI gateway / app server 增加 RPC：
   ```text
   thread/compact/start
   { thread_id, reason? }
   -> { ok, window_id, status, tokens_before, tokens_after, ... }
   ```
6. 增加内部命令 / CLI / TUI 入口（例如 `compact`），避免实际使用只能走 RPC。

### 7.3 边界条件与决策

| 边界 | 处理 |
|---|---|
| 线程不存在 | 返回 KeyError / not found |
| 线程已关闭 / closing | 返回 ClosedError |
| 线程正在运行 turn | 返回 busy；不打断当前 turn（设计上 manual 只允许空闲时执行） |
| 连续多个 manual 请求 | submission loop 串行；第二个执行时可能 already compacted，返回 skipped / no-op |
| 没有 compactor / llm | 返回明确错误，不静默 |
| force=True 且无旧消息 | 返回 skipped，不写 rollout |
| force=False | 走正常 should_compact，可能不压缩 |
| manual 与 pre-turn 同时排队 | 串行处理；先到的先执行，后到的看到已压缩则跳过 |
| manual 压缩失败 | 返回失败响应 + failed analytics；不改变线程状态 |
| manual 时模型未知 / profile 未配置 | 使用当前线程 profile；无法解析则错误 |
| 手动压缩的时间点 | 只允许 idle，避免和 active turn 的 ctx 竞争 |
| 前端轮询 | RPC 返回立即结果；如需进度，可后续加异步事件 |

### 7.4 验收

- 空闲线程 manual compact 成功，返回 window_id / tokens。
- 运行中线程返回 busy。
- 无 compactor 时返回明确错误。

---

## Phase 8：Remote compaction 占位与 fallback（降级为可选）

### 8.1 目标（修订 v2）

- 本迭代 **不实现具体 remote compaction**。
- 只保留 capability 字段和极薄 `RemoteCompactor` protocol，为将来接入预留。
- 默认走本地摘要，避免 YAGNI。

### 8.2 具体步骤（修订 v2）

1. 定义 `RemoteCompactionSupport`（Unsupported / V1 / V2），但默认 `remote_compaction=false`。
2. 定义极薄 `RemoteCompactor` protocol；不绑定任何真实后端。
3. 在 `Compactor.compact` 中只保留分支骨架：
   ```python
   if (
       profile.remote_compaction
       and provider.capabilities.supports_remote_compaction
       and self._remote_compactor is not None
   ):
       try:
           return await self._remote_compactor.compact(...)
       except Exception as exc:
           emit failed remote analytics
   return await local_compactor.compact(...)
   ```
4. 增加 `remote_compaction_version` 枚举，但不写 V1/V2 请求体。
5. 测试只验证“未启用 remote 时直接本地”和“启用但 remote 抛错时回退本地”，不造真实 remote 协议。

### 8.3 边界条件与决策

| 边界 | 处理 |
|---|---|
| remote 未实现 | 直接本地 |
| remote 超时 | 本地 fallback，记录耗时 |
| remote 返回空 / 格式错误 | 视为失败，本地 fallback |
| remote 返回的 replacement_history 无法映射到 Pydantic ModelMessage | 重建失败则本地 fallback；不把坏数据写入 rollout |
| remote 成功但 tokens_after 仍超限 | 本地 drop_oldest 继续 |
| remote 有副作用（如服务端已压缩）后本地再压缩 | 可能重复；remote adapter 应返回足够信息，避免二次压缩 |
| provider capability 声明支持但实际 4xx | 捕获后 fallback，并缓存/降低该 provider 的 remote 优先级 |
| remote V2 需要特殊请求体/流 | 由 adapter 自管，不污染通用 Compactor |
| 无网络 | 本地兜底；不把网络错误当致命 |

### 8.4 验收（修订 v2）

- 默认关闭 remote，本地路径完整。
- 启用 remote 但 fake remote 抛错时回退 local，rollout 仍是合法 checkpoint。
- 不要求真实 provider remote 协议可跑。

---

## Phase 9：Hooks / Analytics / GUI

### 9.1 目标

- 压缩前后可扩展。
- 每次压缩有完整事件。
- GUI trace 能展示窗口信息。

### 9.2 具体步骤

1. `CompactionHooks` 注册到 `ThreadRuntime`。
2. 定义 `CompactionContext`。
3. `maybe_compact` / `compact_now` 调用 pre/post。
4. 发送 `context_compaction` 事件到 journal。
5. 更新 `traces.py` 解析新字段。
6. 更新前端类型（如适用）。

### 9.3 边界条件与决策

| 边界 | 处理 |
|---|---|
| pre hook 抛异常 | catch + log，不阻断压缩 |
| post hook 抛异常 | catch + log，不改变压缩结果 |
| hook 需要改写输入 | 本版只允许观察/记录；改写留给后续版本 |
| 事件缺少 turn_id | manual / pre-turn 可能为 null，字段允许 null |
| analytics 全量 replacement_history | 禁止；只写 count 和 tokens |
| summary 含敏感信息 | 沿用现有 `redact`；trace 展示脱敏 |
| 事件顺序 | compaction 事件必须在 checkpoint 写盘成功之后发出；failed 事件在失败后发出 |
| 同一毫秒多事件 | 用 seq / ts 排序；事件本身不承担事务 |

### 9.4 验收

- 压缩日志含 trigger/reason/implementation/phase/status/tokens/window_id。
- GUI trace 能显示 summary + replacement_history count。
- hook 异常不影响主流程。

---

## Phase 10：TypeScript 平行移植

### 10.1 目标

- 保持 `athena_ts` 的 memory 层与 Python 一致。
- 至少同步数据模型、split、rollout 恢复。

### 10.2 边界条件

- TS 若尚无完整 turn 编排，可以不同步 mid-turn/manual，只同步：
  - `ContextManager` token usage / prefill / scope
  - `CompactionCheckpoint`
  - `RolloutRecorder` 新格式与 `resumeContextSync` 新恢复
- 行为必须与 Python 单测对齐；用 fixture JSON 共享测试数据。

---

## 11. 全局边界条件汇总（重点验收清单）

### 11.1 配置与模型

- [ ] 模型窗口未知时不静默使用 200k。
- [ ] percent / budget / buffer / output reserve 非法值时夹紧并告警，不抛启动异常。
- [ ] per-model registry 能区分 fast/pro 不同窗口。
- [ ] 模型切换 reset 窗口状态。

### 11.2 Token 计量

- [ ] 无 server usage 时 BodyAfterPrefix 不产生错误乐观。
- [ ] `active_context_tokens` 使用 `input_tokens`，不使用 `total_tokens`。
- [ ] server usage 优先于 estimate。
- [ ] prefill 只在窗口首请求建立。
- [ ] scope 永不为负。
- [ ] input hard cap 扣除了 output reserve。

### 11.3 压缩选择

- [ ] 最近 N 条真实用户消息保留。
- [ ] 保留块与 tail 不重复。
- [ ] 空旧区间 skipped。
- [ ] 摘要失败可降级 drop_oldest。
- [ ] replacement 超限可迭代降级且不超过最大尝试次数。
- [ ] mid-turn 受 protected_start 保护，不吞当前 turn 消息。
- [ ] mid-turn initial context 位置正确。

### 11.4 持久化

- [ ] 新旧 rollout 格式兼容。
- [ ] checkpoint 后只写 `items[replacement_count:]`，恢复不重复。
- [ ] 损坏行跳过。
- [ ] mid-turn 后 turn_start / persist / rollback 三游标正确。
- [ ] 失败回滚不产生内存/磁盘分叉。

### 11.5 触发

- [ ] pre-turn 自动压缩。
- [ ] mid-turn 可多次压缩。
- [ ] manual busy/closed 行为明确，且有内部命令入口。
- [ ] remote 为占位；若启用且失败回退本地。

### 11.6 观测

- [ ] 成功/失败/skipped 都有事件。
- [ ] GUI 能显示窗口元数据。
- [ ] hook 异常不阻断。

---

## 12. 测试策略

### 12.1 单元测试文件建议

- `test/unit/memory/test_window_profile.py`
- `test/unit/memory/test_context_manager_status.py`
- `test/unit/memory/test_compaction_split.py`
- `test/unit/memory/test_compaction_replacement.py`
- `test/unit/memory/test_rollout_compaction_recovery.py`
- `test/unit/app_server/test_manual_compact.py`
- `test/unit/agent/test_midturn_compact.py`
- `test/unit/provider/test_usage_capture.py`
- `test/unit/remote/test_remote_compact_fallback.py`

### 12.2 集成测试

- 使用假 provider 返回 usage / token_limit。
- 构造超长消息序列触发 pre-turn、mid-turn、manual。
- 模拟 rollout 崩溃恢复。

### 12.3 测试替身

- `FakeLLM` 返回固定 summary。
- `FakeRemoteCompactor` 可配置成功/失败/格式错误。
- `FakeProviderCapabilities` 可配置 context_window / remote support / usage。

---

## 13. 回滚与迁移

- 每个 Phase 独立小提交。
- 若 Phase 6（mid-turn）风险高，可先用 feature flag 关闭：
  ```python
  ATHENA_MID_TURN_COMPACT=0
  ```
- 若 Phase 4 新 rollout 格式有问题，可回退到旧 `record_compaction(version, summary)`；恢复逻辑保留旧分支。
- 配置文件全部带默认值，旧 config.toml 无需改也能启动。
- 破坏性变更最小化：
  - 不删除旧 `Compaction` 类字段；
  - 不改变 `RolloutRecorder.record_compaction` 旧方法语义（新方法重载/新增参数）。

---

## 14. 明确不做（Out of Scope）

- 不引入完整 tokenizer；只做服务端 usage 校准。
- 不实现真正的 Anthropic native provider / Responses API（本计划只留 remote adapter 接口）。
- 不重写整个 Session/Turn/StepContext 模型。
- 不实现完整 world state / context fragments。
- 不重做 GUI 整体架构，只扩展 compaction 事件展示。
- 不把 `original_items` 全量写入 rollout，避免文件爆炸。

---

## 15. 设计缺陷反思与修订（v1 评审）

本节记录对上一版 design/plan 的自我批判。以下问题不全是“立刻全改”，但必须在实现前明确决策；否则后续会返工。

### 15.1 最严重缺陷：`active_context_tokens` 混淆了输入与总 token

- 原稿写“server usage 优先、`total_tokens` 作为 active context”。
- 问题：Chat Completions 的 `total_tokens = prompt_tokens + completion_tokens`，包含当前这次输出的 token。若用它作为上下文大小，窗口状态会随单次回答长度剧烈跳动，导致误压缩。
- 修订：
  - `active_context_tokens` 应以 **服务端 `prompt_tokens` / `input_tokens`** 为准。
  - `total_tokens` 只用于诊断。
  - 增加 `output_reserve_tokens`（或直接用 `max_tokens`）参与 hard cap 计算：
    ```text
    input_hard_cap = resolved_context_window() - output_reserve_tokens
    ```
  - `ContextWindowStatus` 同时暴露 `input_usage` 与 `total_usage`，避免再次混淆。

### 15.2 严重缺陷：没有强制“当前 turn 的消息不可被 mid-turn compact 吞掉”

- 原 `select_split` 只按 `keep_recent_tokens` 从后往前找分界，没有“当前 turn 起始索引”的硬边界。
- 问题：mid-turn 压缩时如果本轮新消息/工具结果很大，预算算法可能把本轮消息当成可压缩旧历史的一部分；压缩后还可能破坏 `before_index` 与回滚语义。
- 修订：
  - `select_split(..., protected_start: int)`。
  - mid-turn 时 `protected_start = turn_start_index`，split 不得越过它。
  - 增加 `CompactSplit.protected_start` 校验；出现问题直接失败，不静默吞当前 turn 数据。
  - pre-turn/manual 的 `protected_start = 0`（或按策略允许压缩全部旧历史）。

### 15.3 严重缺陷：rollout 写入与 `replacement_history` 会出现重复恢复

- 原稿写“checkpoint 后写 `items[1:]`”，这是旧格式只含 summary 时的写法。
- 新格式 `replacement_history` 已包含 summary + 保留的真实用户消息。若再写 `items[1:]`，恢复时会把保留消息再追加一遍，产生重复上下文。
- 修订：
  - 新格式 checkpoint 后只写 `ctx.items[len(replacement_history):]`，即“replacement 之后的 tail + 后续新消息”。
  - 旧格式（只有 summary）继续写 `items[1:]`。
  - 在恢复测试中必须断言“没有重复用户消息/重复 assistant 块”。

### 15.4 严重缺陷：mid-turn 的 cursor / rollback 模型仍不完整

- 原稿用单一 `persist_cursor` 替代 `before_index`，丢失了“turn 起点”语义。
- 问题：
  - 提交式压缩后，turn 失败时只回滚压缩之后的消息，会让本轮压缩前已产生的部分 assistant/tool 消息永久留在历史里。
  - 若要严格 turn 原子性，需要可撤销式压缩；但原稿没有给出完整撤销协议（如何从 rollout 撤回 checkpoint）。
- 修订（明确二选一，且实现前必须选）：
  - **方案 A（默认，简单）**：`turn_start_cursor` 与 `persist_cursor` 分离；mid-turn compact 后保留此前消息，失败只回滚 compact 后的消息。需在文档中明确“失败 turn 可能留下部分中间消息”。
  - **方案 B（严格原子）**：mid-turn compact 只在当前 turn 尚无未持久化重要消息时允许，或在失败时把 `original_items` 还原并写一条 `compaction_revert` 到 rollout。该方案成本高，建议仅当产品要求严格回滚时启用。

### 15.5 重要缺陷：`replacement_history` 没有整体 token 上限

- 原稿只限制“保留真实用户消息预算”，没有限制 replacement + tail 的总量。
- 问题：保留 8 条用户消息及附带的大工具输出可能让压缩后上下文仍然超限，或接近 hard cap，造成反复压缩。
- 修订：
  - 增加 `replacement_token_budget` / `post_compact_target_tokens`。
  - 每次构造 replacement 后计算 `ctx.tokens`，若超过目标：
    1. 减少保留的用户消息块；
    2. 缩短摘要；
    3. 增加 compact 范围（把更多旧 tail 也纳入摘要）；
    4. 最后才 drop_oldest。
  - 每次同 turn 压缩设置最大尝试次数，防止无限循环。

### 15.6 重要缺陷：mid-turn 接线方式没有落到具体接口

- 原稿说“ThreadRuntime 暴露包装”，但实际 `_ThreadRunner.run_with_context` 并没有 ThreadRuntime 引用，也没有中转字段。
- 问题：直接改 `AgentContext` 不足以把 `ThreadRuntime.maybe_compact` 传到 `BaseAgentRunner`。
- 修订：
  - 新增 `MemoryController` 接口，由 `ThreadRuntime` 持有并传给 runner。
  - 修改 `run_with_context(thread, turn, emit, memory, cancel, memory_controller=None)`。
  - `_ThreadRunner` → `RunSession` → `BaseAgentRunner` → `AgentContext.compact_if_needed` 逐层透传。
  - 若不改 runner 签名，就必须为旧 runner 保留 None 回退（无 mid-turn）。

### 15.7 重要缺陷：模型配置只有全局值，无法区分 fast/pro 等多个模型

- 原稿 `[llm] context_window` 是全局单值，但 Athena 有 `model_name` 与 `model_pro`，未来还可能有 survey/vision 等模型。
- 问题：一个全局窗口无法正确覆盖多模型。
- 修订：
  - 增加 `ModelRegistry` / `[models."model-name"]` 形式的 per-model profile。
  - 解析顺序：具体模型配置 > 全局默认 > provider capability > 保守默认。
  - 模型名变化时重置 `AutoCompactWindowState`。

### 15.8 重要缺陷：hard cap 未考虑输出 token 预留

- 同 15.1，`full_context_window_limit` 只按 percent 算输入上下文，没有扣减 `max_tokens`。
- 修订：hard cap 增加 `output_reserve`，并在 `ContextWindowStatus` 中单独暴露，避免“输入没超窗但 prompt + max_tokens 已超模型限制”。

### 15.9 中等缺陷：`BodyAfterPrefix` 的 prefill 语义仍可能被误读

- `prefill_input_tokens` 取“第一次请求的 input_tokens”，可能包含该轮用户问题；因此 BodyAfterPrefix 在窗口刚建立时为 0，之后增长才计数。
- 这符合“记录初始 prefill 之后新增 token”的定义，但必须在文档中明确：
  - 第一次请求前的 status 不能使用 `BodyAfterPrefix`（prefill 未知），应回退到 Total。
  - 若第一次请求的 input_tokens 无法获得，需要估计 prefill 并标记 source=estimated。
  - 后续 server usage 不得覆盖已建立的 prefill 基线。

### 15.10 中等缺陷：摘要大历史可能超出摘要模型自身窗口

- 原稿只提“分块层级摘要”，没有定义具体算法和参数。
- 修订：
  - 增加 `max_summary_input_tokens`。
  - 超限时先按用户 turn / 时间块切分，逐块摘要，再合并摘要。
  - 所有层级都失败时走 drop_oldest，并把 `implementation=drop_oldest` 写入 analytics。

### 15.11 中等缺陷：remote compaction 是未经验证的抽象，可能 YAGNI

- 当前 provider 均无 remote compaction，引入 V1/V2、adapter、fallback 会增加不需要的复杂度。
- 修订：
  - 保留 capability 字段和极薄接口，但 **不实现具体 remote 逻辑**。
  - 把“remote 完整实现”移出 P0/P1，作为后续可选阶段。
  - 避免在本地路径中引入空转分支。

### 15.12 较低缺陷：手动压缩的 UI/入口只写了 RPC，没有 TUI/前端按钮

- 若用户实际通过 CLI/TUI 使用，只有 RPC 不够。
- 修订：至少增加内部命令 `compact` / TUI 快捷键；GUI 按钮可作为增强。

### 15.13 较低缺陷：rollout 文件不断追加重复 tail，长期体积增长

- 每次 compaction 后重写 tail，旧记录又不会删除，文件会随压缩次数线性增长。
- 修订：
  - 暂不自动压缩 rollout；但在 analytics 中记录 `rollout_size_bytes`。
  - 后续可增加 rollout 物理压缩/归档，但不在本计划核心路径。

### 15.14 需要明确的产品决策

1. mid-turn 失败是否允许保留部分中间消息？（影响是否做可撤销压缩）
2. `BodyAfterPrefix` 是否作为默认 scope？（影响旧用户行为变化幅度）
3. 保留用户消息是否连 assistant/tool 回复一起保留，还是只保留 user 原文？（影响信息量 vs 体积）
4. remote compaction 是否在本迭代真做？（当前建议不做）
5. 多模型配置是否立即做 per-model registry？（建议至少先做查表，避免返工）

---

## 16. 最终验收（Definition of Done）

1. `ContextManager` 使用 `ModelWindowProfile` / `ModelRegistry` 动态解析真实窗口。
2. `Total` / `BodyAfterPrefix`、server `input_tokens` usage、prefill、fallback buffer、output reserve、防抖全部可单测验证。
3. 任意压缩产生 `CompactionCheckpoint`，含 summary + replacement_history + window_id + analytics + protected_start。
4. 新 rollout 可恢复重用 replacement_history，且不重复保留消息；旧 rollout 仍兼容。
5. pre-turn、mid-turn、manual 三类触发均可运行。
6. mid-turn 压缩后 turn 正常完成，当前 turn 消息不被吞，双/三游标正确，崩溃/失败恢复的上下文与内存一致。
7. remote compaction 为占位；若启用且失败，自动回退本地且不产生坏数据。
8. GUI/日志可观测每次压缩的完整元数据。
9. TypeScript memory 层与 Python 核心行为对齐（或明确降级范围）。
10. 全量相关测试通过，旧行为无回归。

---

## 17. 第二轮反思（v2 review）

第一轮修订解决了口径、游标、重复恢复等问题，但继续深挖仍发现以下缺陷。这些应在 v3 修订时处理。

### 17.1 最严重：初始 system / context 仍可能被 pre-turn 压缩掉

- 当前 `select_split` 仍然默认从 index 0 开始压缩。
- Athena 的 `ContextManager` 第一条消息通常是 `SystemPromptPart`（Agent system prompt）。
- 如果把它包含进旧区间，本地摘要会替换掉原始 system prompt；`drop_oldest` 也可能删除它。
- 这会造成模型指令丢失，不只是“信息丢失”，而是**行为丢失**。

**v3 修订方向**：

- 引入 `protected_prefix_count` 或 `initial_context_items`，`compact_start` 必须位于保护前缀之后。
- pre-turn/manual 也保留原始 system/initial context，而不是只靠 mid-turn 的 `InitialContextInjection`。
- `drop_oldest` 永远不能删除保护前缀。
- 检查 `replacement_history` 中不得重复出现被保护的 system 消息。

### 17.2 严重：`active_context_tokens` 在两次请求之间会过期

- 现在设计直接使用最近一次服务端 `input_tokens` 作为 active。
- 但每次采样之后、下一次请求之前，本地可能已经 append 了新的 user / tool 消息；这些消息还没有服务端计数。
- 如果直接沿用旧 `input_tokens`，窗口状态会低估当前上下文，可能延迟压缩。

**v3 修订方向**：

- `ContextManager` 保存 `_local_tokens_at_last_server_usage`。
- 计算：
  ```text
  active = server_input_tokens + (local_estimate_now - local_estimate_at_last_server_usage)
  ```
- 没有 server usage 时仍用本地 estimate。
- `BodyAfterPrefix` 也用同样的 active 口径。

### 17.3 严重：checkpoint + tail 不是原子写入，崩溃窗口会丢 tail

- 当前协议先写 `compaction` 记录，再写 `items[replacement_count:]`。
- 如果进程在这两步之间崩溃，恢复时看到 checkpoint，但看不到 checkpoint 之后的 tail，当前上下文的 tail 会丢失。

**v3 修订方向（二选一）**：

1. **自包含 checkpoint（推荐）**：`compaction` 记录内同时保存 `replacement_history` + 当前 `tail_history`（或 `context_snapshot`），恢复时不需要依赖 checkpoint 之后的 tail。
2. **两阶段提交**：先写 `compaction_pending`，再写 tail，最后写 `compaction_committed`；恢复时只有看到 committed 才切换到新窗口，并且能区分“pending 后已写但未 committed 的 tail”。

自包含方案更简单，代价是 rollout 体积；两阶段方案文件更小但恢复逻辑复杂。建议先选自包含，后续再做 rollout 物理压缩。

### 17.4 重要：`_run_turn` 的代码路径没有和游标设计完全对齐

- 计划只描述了游标概念，但没有明确改 `ThreadRuntime._run_turn` 的具体代码：
  - 正常结束时应用 `items_since(persist_cursor)` 而不是旧 `before_index`；
  - 失败时应用 `rollback(rollback_cursor)`；
  - 取消时如何处理 mid-turn 已持久化消息要显式决策；
  - `before_index` 这个旧变量应删除或改名。

**v3 修订方向**：

- 在 Phase 5/6 中给出 ThreadRuntime 的成段伪代码：
  ```python
  turn_start_index = persist_cursor = rollback_cursor = len(ctx.items)
  ...
  # runner
  ...
  # 正常
  runtime.record_items(ctx.items_since(persist_cursor))
  # 异常
  ctx.rollback(rollback_cursor)
  ```
- 取消路径：已持久化的 checkpoint/tail 保留，未持久化的最新消息按 rollback_cursor 回滚或丢弃。
- 明确 `before_index` 退役，避免两套游标并存。

### 17.5 重要：`drop_oldest` 没有保护前缀

- `drop_oldest` 如果简单“删最老 item”，会删除 system prompt / initial context。
- 必须定义 `drop_oldest` 只能从 `protected_prefix_count` 之后开始删。
- 更安全的是：先扩大 compact 范围，尽量避免 drop_oldest。

### 17.6 中等：`BodyAfterPrefix` 的 prefill 基线含义仍需产品决策

- 当前取“第一次请求的 input_tokens”，会包含该轮用户问题，因此 scope 度量的是“窗口开始后新增量”。
- 这符合 Codex 的 BodyAfterPrefix 说法，但需要明确写进文档，避免实现者误以为是“纯 system 前缀之后”。
- 如果产品期望“只统计 system 之外的对话体”，则 prefill 应只取 system/initial context 的 token，而非第一次请求 total input。

### 17.7 中等：remote 降级后仍保留较多枚举/分支，可能继续增加认知负担

- 虽然已降级为占位，但 `RemoteCompactionSupport`、`RemoteCompactor` protocol、fallback 分支仍会进入核心路径。
- 若确认本迭代不碰 remote，建议把这些类型移到独立模块/独立 feature flag，核心 `Compactor` 只依赖本地接口。

### 17.8 低：手动 compact 的“force”语义需要明确

- force=True 是否在“没有旧消息可压缩”时报错还是 skipped？
- force=True 时是否允许压缩到只剩系统前缀+摘要，还是必须保留 tail？
- 需要定义清楚，否则 API 返回语义不稳定。

---

## 18. 第三轮反思：更隐藏的问题（v2 → v3 候选）

继续深入审阅实际代码路径后，发现以下更隐蔽的问题。它们不会在“正常小样本”下暴露，但会在多线程、真实工具调用、进程崩溃或配置漂移时出现。

### 18.1 严重：`Compactor` 实例可能被多个 Thread 共享，压缩状态会串线

- `RuntimeThreadManager` 把同一个 `compactor` 对象通过 `_memory_kwargs` 传给所有 ThreadRuntime。
- 如果 `last_compaction_tokens`、`compact_attempt`、`last_compaction_at` 等状态放在 `Compactor` 实例字段上，不同 Thread 会互相污染。
- 例如 Thread A 刚压缩完，Thread B 会误以为“刚压缩过”而跳过压缩。

**v3 修订方向**：

- 所有**可变压缩状态**必须放在 `ContextManager` / `AutoCompactWindowState` 或 `MemoryController` 中，按 Thread 隔离。
- `Compactor` 只保留不可变配置和纯函数逻辑。
- `should_compact` 必须接收当前 Thread 的窗口状态，而不是读 `self._last_*`。

### 18.2 严重：没有保证 tool call / tool return 成对完整性

- 对话历史中一个工具调用是 `ModelResponse + ToolCallPart`，对应结果通常是后续 `ModelRequest + ToolReturnPart`。
- 如果 `split` 落在两者之间，或保留块在中间断开，压缩后的上下文会出现“有 tool return 无对应 tool call”或反之。
- 对 OpenAI 兼容 API，这可能导致请求 400；对恢复来说也会产生非法消息序列。

**v3 修订方向**：

- 引入“安全切割点”：split 必须位于完整用户 turn 边界，不能拆散 tool call/return 对。
- 增加辅助函数：
  - `_find_safe_split(items, desired_split)`：从 desired_split 向前/向后调整到最近完整边界；
  - 校验 `replacement + tail` 中不存在孤立 tool return / tool call。
- 在 select_split 单元测试中构造“tool call 与 return 跨分界”的用例。

### 18.3 重要：自包含 checkpoint 与“checkpoint 后写 tail”仍未二选一

- 第二轮反思提出“自包含 checkpoint”或“两阶段提交”，但没有从现有协议中删除“checkpoint 后写 tail”。
- 如果实现者同时采用自包含 + 仍在 checkpoint 后写 tail，恢复时会把 tail 重复追加。
- 如果采用自包含，checkpoint 之后**不应该**再写同一批 tail；后续只写“压缩之后新增的消息”。

**v3 修订方向**：

- 明确**唯一持久化模型**：
  - 模型 A：`compaction` 记录保存 `replacement_history` + `tail_history`（自包含），之后不再写旧 tail；
  - 模型 B：`compaction` 记录仅存 replacement，之后必须写 tail，但需要 pending/committed 两阶段。
- 建议直接选 A，并把“ checkpoint 后写 tail”从所有步骤中删除，避免两套语义并存。

### 18.4 重要：服务端输入 + 本地增量的校准并不精确

- `active = server_input_tokens + (local_now - local_at_usage)` 假设本地 estimate 与服务端 tokenizer 比例一致。
- 但本地是 `len//4`，对中文、代码、图片、JSON 工具参数误差可能很大。
- 如果本地 estimate 在 server 观测点偏差 30%，新增 delta 也会以错误的刻度计入。

**v3 修订方向**：

- 不要假装精确。可以保留 **两个值**：
  - `active_server_based`：server_input + local_delta（用于趋势）；
  - `active_local_estimate`：完整本地 estimate（用于兜底）。
- 压缩判定采用二者中的**保守较大值**（或分别计算 auto / force 阈值），避免低估。
- 若未来接入真实 tokenizer，再替换该近似。

### 18.5 重要：恢复时可能复用过期窗口状态

- 现在 `resume_context_sync` 会从 checkpoint 恢复 `prefill_input_tokens` / window_number。
- 但如果用户换模型、改 context_window、改 effective percent 后重启，旧 prefill 和窗口状态可能失效。
- 更隐蔽：即使模型名没变，provider 后端更新了上下文窗口，旧状态也可能不适用。

**v3 修订方向**：

- compaction 记录持久化 `model_id` / `profile_fingerprint`。
- 恢复时：
  - 当前 `model_id` 一致 → 保留窗口状态；
  - 不一致 → 重置为新窗口（window_number=1，清空 prefill）；
  - `profile_fingerprint` 变化也重置。
- 不要把旧 prefill 当成新配置下的有效基线。

### 18.6 重要：压缩过程缺少取消/中断语义

- 摘要 LLM 调用可能耗时数秒甚至更久。
- 如果用户 interrupt / shutdown / cancel 发生在：
  - 摘要进行中：可能白等；
  - 摘要完成但 replace 前：安全；
  - replace 完成但 rollout 写盘前：内存已变、磁盘未记，状态不一致。
- 当前设计没有像工具执行那样定义 cancel 检查点。

**v3 修订方向**：

- `Compactor.compact` 接受 `cancel: asyncio.Event | None`。
- 在摘要前、摘要后、replace 前、rollout 写盘前检查 cancel。
- 若 cancel 发生在 replace 前：不修改内存，返回 cancelled；
- 若 cancel 发生在 replace 后但未持久化：需要决定“回滚压缩”或“强制完成持久化”；推荐强制完成持久化，避免内存/磁盘分叉。
- pre-turn / mid-turn / manual 都要走同一 cancel 协议。

### 18.7 重要：保留 system 前缀与 `BeforeLastUserMessage` 可能重复或语义冲突

- 第二轮提出必须保护 system 前缀。
- 但现有 mid-turn 设计又要求 `InitialContextInjection.BeforeLastUserMessage` 把 initial context 放回最后真实用户消息之前。
- 如果 system 前缀已经被保护在 index 0，再执行 BeforeLastUserMessage 可能会造成同一 context 出现两份。

**v3 修订方向**：

- 明确“单一 context 事实源”：
  - system/initial context 默认始终保留在最新位置；
  - 如果它已经在 index 0 且未被压缩，则不重复注入；
  - 只有在它被旧区间吞掉（或需要移动到保留用户之前）时才执行 BeforeLastUserMessage；
  - 注入前必须检测是否已存在，避免重复。
- 将 `protected_prefix_count` 与 `InitialContextInjection` 合并成一个明确的“初始上下文处理”决策。

### 18.8 中等：summary 作为第二个 system 消息可能影响 provider 语义

- 当前 `summary_msg` 是 `SystemPromptPart`。
- 如果原始 system prompt 被保护在开头，后面又跟一个 `[HISTORY SUMMARY]` system 消息，某些 provider 可能对“多次 system/中途 system”敏感或影响 prompt cache。

**v3 修订方向**：

- 可以选择把摘要作为 `UserPromptPart` 或带明确标记的正文，而不是 system role；
- 或者在 `_to_api` 中把 `HISTORY SUMMARY` 归一化为普通 user/assistant 上下文；
- 至少要在适配层测试所有 provider 对多个 system 消息的兼容性。

### 18.9 中等：输出预留是否应计入 hard cap 取决于 provider 语义

- 有的 provider context window 是“输入+输出合计”，有的文档可能只说输入上限。
- 统一扣 `output_reserve_tokens` 可能过于保守或不够保守。

**v3 修订方向**：

- `ProviderCapabilities` 增加字段：
  ```python
  context_window_includes_output: bool = True
  ```
- 若 includes_output=True，则 `input_hard_cap = resolved - reserve`；
- 若 includes_output=False，则 `input_hard_cap = resolved`。
- 该能力应来自真实后端校验，不能拍脑袋。

### 18.10 低：`force=true` 的 manual compact 还缺少“允许压缩到什么程度”的契约

- 是否允许把当前 tail 也压缩到只剩系统+摘要？
- 是否必须保留最近真实用户消息？
- 建议约定：manual force 仍必须遵守 `keep_recent_tokens` 和 `keep_recent_user_messages`，只是跳过预算 gate，不跳过信息保留规则。
