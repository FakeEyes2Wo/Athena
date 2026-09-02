# Compaction / Context Window 完整改进设计

日期：2026-08-31
状态：v2 revised draft（已根据“设计缺陷反思”修订）
关联：`diff_docs/04-missing-parts-and-improvements.md` §2.2–2.5、`diff_docs/02-context-window-mechanism.md`、`code_spec/2026-08-31-compaction-context-window-plan.md`
范围：Python `src/athena/memory` + `src/athena/app_server`，并保持 `athena_ts` 的平行移植一致
目的：把 Athena 的上下文窗口与压缩机制从“最小可跑”升级为“动态窗口、可观测、可恢复、少丢失”的完整实现。

> v2 主要修订：active token 改用服务端 input_tokens；增加 output reserve 与 input hard cap；mid-turn 增加 protected_start 硬边界；rollout checkpoint 后只写 replacement 之后的 tail；引入三游标；remote compaction 降级为占位；增加 per-model registry。

---

## 1. 问题与目标

### 1.1 当前问题

| 编号 | 现状 | 后果 |
|---|---|---|
| 2.2 | `ContextManager` 固定 `context_limit=200_000`；`Compactor.should_compact(ctx, at_tokens=170_000)` 是写死的总 token 阈值 | 换不同模型时窗口错误，可能提前压缩或超窗 |
| 2.3 | 只有 `Total` 一种计量；没有 `BodyAfterPrefix`；没有 prefill 基线；没有 token budget / fallback buffer | 压缩时机不准，边界处反复压缩 |
| 2.4 | 只有本地摘要、只在 turn 前触发；无 manual / mid-turn / remote compact；无 hooks / analytics / window id | 无法观测和人工干预，长 turn 无保护 |
| 2.5 | compaction 只把旧历史换成一个摘要；rollout 恢复只保留最新摘要 + tail；没有保留最近真实用户消息，没有 `replacement_history` | 压缩后信息丢失，恢复出来的上下文与压缩前语义不一致，mid-turn 初始上下文错位 |

### 1.2 目标

1. 模型真实 context window 动态解析，禁止固定 200k / 170k 作为唯一依据。
2. 支持 `Total` 与 `BodyAfterPrefix` 两种计量范围，并优先使用服务端观测的 token usage / prefill。
3. 有 token budget、fallback buffer、滞后区间，避免在窗口边界反复压缩。
4. 支持 pre-turn、mid-turn、manual 三类触发；本地摘要为兜底，provider 支持时接入 remote compact。
5. 每个压缩产生一个可恢复、可观测的 checkpoint：`summary + replacement_history + window_id + trigger/reason/implementation/phase/status/tokens`。
6. 保留最近真实用户消息，mid-turn 时把 initial context 放回正确位置。
7. 旧 rollout 仍可恢复；新旧记录格式兼容。

---

## 2. 设计原则

- **配置优于硬编码**：context window、percent、budget、buffer 都从模型/配置解析。
- **模型能力优先，本地兜底**：先探测 provider 是否支持 remote compaction / usage；不支持时走本地摘要。
- **计量分层**：本地 estimate 是兜底，服务端 usage 是校准，prefill 是窗口基线。
- **压缩是可审计事件**：每次尝试都有 trigger/reason/implementation/phase/status/tokens，可回放。
- **压缩是可恢复检查点**：不只保存摘要，还保存能重建当前上下文的 `replacement_history`。
- **兼容优先**：旧 rollout 的 `compaction` 记录仍可读取；新字段缺省时走旧逻辑。

---

## 3. 总体架构

```text
┌─────────────────────────────────────────────────────────────────┐
│ Config / ModelInfo                                                │
│   context_window / effective_percent / budget / buffer / scope    │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│ ContextManager                                                   │
│   items + estimate + server usage + prefill + user boundaries    │
│   token_status(profile) -> ContextWindowStatus                   │
└───────────────┬───────────────────────────┬────────────────────┘
                │                           │
┌───────────────▼──────────────┐  ┌─────────▼────────────────────┐
│ AutoCompactWindowState       │  │ CompactionPolicy/Compactor    │
│ window_number / window_id /  │  │ should_compact / select_split │
│ prefill / budget flags       │  │ local_summary / remote /      │
│                              │  │ hooks / analytics             │
└───────────────┬──────────────┘  └─────────┬────────────────────┘
                │                           │
┌───────────────▼───────────────────────────▼────────────────────┐
│ ThreadRuntime / MemoryController                                 │
│   pre-turn maybe_compact / mid-turn inline / manual compact      │
│   persist checkpoint + tail, adjust turn cursor, emit event      │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│ RolloutRecorder / resume_context                                │
│   compaction record: summary + replacement_history + metadata    │
│   resume: rebuild from latest checkpoint + tail                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. 模型配置与能力发现

### 4.1 `ModelWindowProfile`

新增一个不可变配置对象：

```python
@dataclass(frozen=True, slots=True)
class ModelWindowProfile:
    model: str
    context_window: int                        # 模型真实总窗口（输入+输出）
    effective_context_window_percent: float = 0.80
    output_reserve_tokens: int = 0             # 构造时由 settings/registry 解析；未显式配置时使用全局 max_tokens
    auto_compact_enabled: bool = True
    auto_compact_scope: Literal["Total", "BodyAfterPrefix"] = "BodyAfterPrefix"
    auto_compact_token_budget: int | None = None
    auto_compact_fallback_buffer_tokens: int = 8_000
    keep_recent_tokens: int = 20_000
    keep_recent_user_messages: int = 8
    keep_recent_user_budget_tokens: int = 12_000
    replacement_token_budget: int | None = None # replacement 总预算，None = 用 post target
    post_compact_target_ratio: float = 0.60
    resume_interval_tokens: int = 5_000
    max_summary_input_tokens: int = 80_000      # 分层摘要的输入块上限
    max_compact_attempts_per_turn: int = 3
    remote_compaction: bool | None = None       # None=自动探测；本迭代仅预留接口
    summary_model: str = "haiku"

    def resolved_context_window(self) -> int:
        """模型有效总窗口（未扣输出预留）。"""
        return max(1, int(self.context_window * self.effective_context_window_percent))

    def input_hard_cap(self) -> int:
        """真正用于 prompt/历史输入的 hard cap，必须扣除输出预留。"""
        return max(1, self.resolved_context_window() - self.output_reserve_tokens)

    def auto_compact_limit(self) -> int:
        if self.auto_compact_token_budget is not None:
            return min(self.auto_compact_token_budget, self.input_hard_cap())
        return int(self.input_hard_cap() * 0.65)

    def effective_replacement_token_budget(self) -> int:
        """返回 replacement 的可用总预算；显式字段优先，否则按 input hard cap 推算。"""
        if self.replacement_token_budget is not None:
            return self.replacement_token_budget
        return int(self.input_hard_cap() * (1 - self.post_compact_target_ratio))
```

解释：

- `resolved_context_window()` 是模型有效总窗口，**不是**“prompt 可用的 hard cap”。
- `input_hard_cap()` 才是压缩必须保证的输入上限：`有效窗口 - 输出预留`。
- `auto_compact_limit()` 是正常自动压缩阈值，低于 input hard cap。
- `effective_replacement_token_budget()` 用于限制 `summary + replacement_history` 的总 token，避免保留内容过多导致压缩后仍超限。
- 多模型配置通过 `ModelRegistry` 按模型名解析，避免单个全局窗口误配 fast/pro 等不同模型。

### 4.2 配置入口

`config.example.toml` 增加：

```toml
[llm]
# 全局默认真实 context window；具体模型优先。
# 0 或留空表示未知，回退到 provider capability / 保守默认。
# context_window = 128000
# effective_context_window_percent = 0.80
# 为 prompt 预留的输出 token；不填则使用 [llm].max_tokens
# output_reserve_tokens = 8192

# 推荐：按模型单独配置，避免 fast/pro 不同窗口互相污染。
# [models."deepseek-v4-flash"]
# context_window = 128000
# effective_context_window_percent = 0.80
# output_reserve_tokens = 8192
#
# [models."deepseek-v4-pro"]
# context_window = 128000
# effective_context_window_percent = 0.75

[llm.compaction]
# 是否启用自动压缩（false 时仅手动）
auto_compact_enabled = true

# Total: 整个活跃上下文都计入；
# BodyAfterPrefix: 只计初始 prefill 之后新增的 token（推荐）
auto_compact_scope = "BodyAfterPrefix"

# 正常自动压缩阈值；不填则 = input_hard_cap * 0.65
# auto_compact_token_budget = 100000

# hard cap 前的 fallback buffer，避免刚过预算就撞墙
auto_compact_fallback_buffer_tokens = 8000

# 压缩时保留的最近 token / 真实用户消息数
keep_recent_tokens = 20000
keep_recent_user_messages = 8
keep_recent_user_budget_tokens = 12000

# replacement（summary + 保留消息）总 token 预算；不填自动用 post target 推算
# replacement_token_budget = 30000

# 压缩后回落目标（占 input hard cap 比例）
post_compact_target_ratio = 0.60

# 两次自动压缩之间的最小增量，防抖
resume_interval_tokens = 5000

# 分层摘要单块输入上限
max_summary_input_tokens = 80000

# 单 turn 内最大压缩尝试次数，防止无限循环
max_compact_attempts_per_turn = 3

# 是否允许 remote compaction；不填则按 provider 能力探测（本迭代只做占位）
# remote_compaction = false
```

同时将 `ContextManager(context_limit=200_000)` 的默认值改为从 profile 注入；调用链中不再出现 `170_000` 字面量。

### 4.2.1 `ModelRegistry`

```python
@dataclass(frozen=True, slots=True)
class ModelRegistry:
    profiles: Mapping[str, ModelWindowProfile]
    default: ModelWindowProfile

    def resolve(self, model: str) -> ModelWindowProfile:
        return self.profiles.get(model, self.default)
```

解析顺序：

1. `[models."{model}"]` 具体模型配置；
2. `[llm]` 全局默认；
3. provider capability；
4. 保守默认（带 warning）。

### 4.3 Provider capability

在 `BaseProvider` 增加能力探测接口（至少返回元数据，不强制请求）：

```python
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    context_window: int | None = None
    supports_usage: bool = False
    supports_remote_compaction: bool = False
    remote_compaction_version: Literal["v1", "v2"] | None = None
```

```python
class BaseProvider(ABC):
    @property
    def capabilities(self) -> ProviderCapabilities: ...
```

现有 `ResponsesProvider` 可以先返回静态/表驱动能力；后续如果 provider 有 `GET /models` 或配置表，再改为动态发现。

---

## 5. 上下文容器与 Token 计量

### 5.1 `ContextManager` 扩展

保留现有 `items / tokens / version / append / replace_range / rollback / snapshot`，新增：

```python
class ContextManager:
    # 新状态
    _server_usage: TokenUsageInfo | None
    _prefill_input_tokens: int | None      # 当前 compaction window 的 prefill 基线
    _prefill_source: Literal["server", "estimated"] | None
    _last_user_indices: list[int]          # 辅助定位真实用户消息
    _window_state: AutoCompactWindowState | None
    _model_profile: ModelWindowProfile | None
    _server_token_limit_reached: bool      # provider 明确报“上下文超限”
    # 两个游标必须分离：
    #   turn_start_index   当前 turn 的起始，用于保护/严格回滚
    #   persist_cursor     已写入 rollout 的分界
    _turn_start_index: int
    _persist_cursor: int

    def set_window_profile(self, profile: ModelWindowProfile) -> None: ...
    def set_turn_start(self, index: int) -> None: ...
    def set_persist_cursor(self, index: int) -> None: ...
    def update_server_usage(self, usage: TokenUsageInfo) -> None: ...
    def set_prefill_baseline(
        self, tokens: int, *, source: Literal["server", "estimated"]
    ) -> None: ...
    def active_context_tokens(self) -> int:
        """以服务端 input_tokens/prompt_tokens 为准；无服务端时用本地 estimate。"""
        ...
    def scope_tokens(self, scope: AutoCompactScope) -> int: ...
    def token_status(self) -> ContextWindowStatus: ...
    def is_real_user_message(self, msg: ModelMessage) -> bool: ...
    def real_user_message_indices(self, items: list[ModelMessage] | None = None) -> list[int]: ...
    def rebase_snapshot_after_replace(
        self, idx: int, start: int, end: int, new_count: int
    ) -> int: ...
```

### 5.2 `TokenUsageInfo`

```python
@dataclass(frozen=True, slots=True)
class TokenUsageInfo:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    raw: dict[str, Any] | None = None
```

服务端 usage 优先级（修正 v2）：

1. `input_tokens` / `prompt_tokens` 用于 `active_context_tokens`，**不要用 `total_tokens`**（它包含本次输出）。
2. `total_tokens` 只用于诊断/成本统计，不参与窗口状态。
3. 第一次请求的 `input_tokens` 作为该 window 的 `prefill_input_tokens`；若缺失，用本地 estimate 作为 estimated baseline。
4. 后续请求的 server usage 只更新最近观测，不覆盖已经建立的 prefill baseline。
5. 若 provider 返回“context length exceeded”，设置 `_server_token_limit_reached = True`，触发强制压缩。

### 5.3 计量范围

```python
class AutoCompactScope(str, Enum):
    TOTAL = "Total"
    BODY_AFTER_PREFIX = "BodyAfterPrefix"
```

```python
def scope_tokens(self, scope: AutoCompactScope) -> int:
    active = self.active_context_tokens()
    if scope is AutoCompactScope.TOTAL:
        return active
    # 第一次模型请求前 prefill 未知：不能当作 0，否则会错误乐观。
    if self._prefill_input_tokens is None:
        return active
    return max(0, active - self._prefill_input_tokens)
```

### 5.4 `ContextWindowStatus`

```python
@dataclass(frozen=True, slots=True)
class ContextWindowStatus:
    active_context_tokens: int            # 服务端 input/prompt 或本地 estimate
    server_input_tokens: int | None       # 最近一次服务端观测（诊断）
    server_total_tokens: int | None       # 仅诊断，不参与窗口判定
    auto_compact_scope_tokens: int
    auto_compact_scope_limit: int
    full_context_window_limit: int        # resolved（未扣输出）
    input_hard_cap: int                   # resolved - output_reserve
    output_reserve_tokens: int
    fallback_buffer_tokens: int
    base_window_tokens_remaining: int
    token_budget_remaining: int
    token_limit_reached: bool             # 服务端/本地判定“必须压缩”
    input_hard_cap_reached: bool          # active >= input_hard_cap
    should_auto_compact: bool             # 正常预算触发
    should_force_compact: bool            # input hard cap / fallback / server signal
```

计算规则（修正 v2）：

```python
full_limit = profile.resolved_context_window()
input_hard_cap = profile.input_hard_cap()
auto_limit = profile.auto_compact_limit()
scope_tokens = ctx.scope_tokens(profile.auto_compact_scope)
active = ctx.active_context_tokens()      # 已修正为 server input / estimate
remaining = max(0, auto_limit - scope_tokens)
input_hard_cap_reached = active >= input_hard_cap

should_auto_compact = (
    scope_tokens >= auto_limit
    or active >= input_hard_cap - profile.auto_compact_fallback_buffer_tokens
)
should_force_compact = (
    input_hard_cap_reached
    or token_limit_reached
)
```

### 5.5 `AutoCompactWindowState`

```python
@dataclass(slots=True)
class AutoCompactWindowState:
    model_id: str = ""
    window_number: int = 1
    window_id: str = ""
    first_window_id: str = ""
    previous_window_id: str | None = None
    prefill_input_tokens: int | None = None
    prefill_source: str | None = None
    new_context_window_requested: bool = False
    token_budget_reminder_delivered: bool = False
    auto_compact_fallback_delivered: bool = False
    last_compaction_at: str | None = None

    @classmethod
    def start(cls, thread_id: str) -> "AutoCompactWindowState":
        window_number = 1
        window_id = make_window_id(thread_id, window_number)
        return cls(window_number=window_number, window_id=window_id,
                   first_window_id=window_id)
```

`window_id` 建议格式：`{thread_id}:w{window_number}:{uuid4().hex[:8]}`。

窗口推进：

- 每次成功的 compaction 都会 `window_number += 1`，生成新 `window_id`，旧 id 记为 `previous_window_id`。
- `prefill_input_tokens` 在新窗口下一次模型请求时重新采样；优先 server-observed，其次 local estimate。
- 模型切换时必须新建 window state：`model_id` 变化、window_number 重置、prefill 清空，避免把不同模型的窗口基线混用。

---

## 6. 压缩数据模型

### 6.1 `CompactionCheckpoint`

替代/扩展现有 `Compaction`：

```python
@dataclass(slots=True)
class CompactionCheckpoint:
    version: int
    summary: str
    replacement_history: list[ModelMessage]   # 真正替换旧区间的消息
    original_items: list[ModelMessage]        # 被替换掉的原始消息（调试/回滚用）
    window_id: str
    window_number: int
    trigger: str                              # auto | manual | remote_fallback
    reason: str                               # token_budget | full_window | token_limit | user_request | remote_failure
    implementation: str                       # local_summary | remote | remote_v2 | drop_oldest
    phase: str                                # pre_turn | mid_turn | manual
    status: str                               # completed | skipped | failed
    tokens_before: int
    tokens_after: int
    scope_tokens_before: int
    scope_tokens_after: int
    prefill_input_tokens: int | None
    initial_context_injection: InitialContextInjection
    protected_start: int = 0                  # 本次压缩保护的最小索引（通常是 turn_start）
    replacement_count: int = 0                # len(replacement_history)
    compact_attempt: int = 1                  # 同一 turn 内第几次尝试
    error: str | None = None
```

### 6.2 `CompactSplit` 选择结果

```python
@dataclass(frozen=True, slots=True)
class CompactSplit:
    compact_start: int          # 旧区间起点
    compact_end: int            # 旧区间终点（不含）
    protected_start: int        # 当前 turn 起始，mid-turn 不能越过
    replacement: list[ModelMessage]  # summary + preserved recent user exchanges
    replacement_count: int      # len(replacement)
    keep_tail_start: int        # 压缩后 tail 在新列表中的起点
```

### 6.3 `InitialContextInjection`

```python
class InitialContextInjection(str, Enum):
    DO_NOT_INJECT = "DoNotInject"          # pre-turn / manual
    BEFORE_LAST_USER_MESSAGE = "BeforeLastUserMessage"  # mid-turn
    AT_START = "AtStart"                   # 极少数需要强制放回最前的情况
```

---

## 7. 压缩策略

### 7.1 分界与保留算法

新 `Compactor` 不再只按“从后往前累积 keep_recent_tokens”切一刀，而是带保护边界和整体预算：

```text
1. 选定旧区间 [0, split)：
   - split 由 keep_recent_tokens 从后往前确定；
   - mid-turn 时还必须满足 split <= protected_start（当前 turn 起始），
     否则把 split 收缩到 protected_start，确保不吞本轮新消息。
2. 在旧区间中反向收集“真实用户消息”：
   - 真实用户 = 含 UserPromptPart / user-prompt 的 ModelRequest，不含 tool-return、
     mailbox 系统信封（可按前缀排除）。
   - 收集上限：keep_recent_user_messages 条，且累计 token 不超过 keep_recent_user_budget_tokens。
3. 对每个被保留的真实用户消息，同时保留它后面紧邻的 assistant 回复/工具结果
   （只要仍在旧区间内且未超预算），形成 replacement_history。
4. 对更旧的内容生成摘要；若旧内容超过 max_summary_input_tokens，先分层分段摘要再合并。
5. replacement = [summary_message] + preserved_recent_exchanges。
6. 计算 replacement 总 token：
   - 若超过 replacement_token_budget，先减少保留用户块，再缩短摘要；
   - 若仍超限，把 compact_end 向左扩展（压缩更多老消息），重新构造；
   - 全部失败才 drop_oldest。
7. 校验 replacement_count + tail 后的 ctx.tokens <= post_compact_target，
   不满足则回到第 5 步迭代，最多 max_compact_attempts_per_turn 次。
```

伪代码（修正 v2）：

```python
def select_split(self, ctx, profile, *, protected_start=0) -> CompactSplit:
    items = ctx.items
    raw_split = self._split_recent(items, profile.keep_recent_tokens)
    split = min(raw_split, protected_start) if protected_start > 0 else raw_split
    old = items[:split]
    tail = items[split:]

    preserved: list[ModelMessage] = []
    budget = profile.keep_recent_user_budget_tokens
    user_idx = _last_real_user_indices(old, profile.keep_recent_user_messages)
    for start in user_idx:
        end = _next_exchange_end(old, start)   # 到下一个真实用户消息前，或旧区间尾
        block = old[start:end]
        if sum(estimate(m) for m in preserved + block) > budget:
            break
        preserved = block + preserved           # 保持原始顺序

    summary_input = _exclude_preserved(old, preserved)
    if estimate_tokens(summary_input) > profile.max_summary_input_tokens:
        summary = await self._hierarchical_summarize(summary_input, profile)
    else:
        summary = await self._summarize(summary_input, profile)

    replacement = [summary_message(summary)] + preserved
    # 整体预算/回退循环见正文；这里先返回基础 split
    return CompactSplit(
        compact_start=0,
        compact_end=split,
        protected_start=protected_start,
        replacement=replacement,
        replacement_count=len(replacement),
        keep_tail_start=len(replacement),
    )
```

注意：

- 不需要在 `replacement_history` 中重复保存 tail；tail 仍留在 `ContextManager` 中。
- mid-turn 的 `protected_start` 是硬边界；若 `raw_split >= protected_start`，宁可少压缩也不能把当前 turn 吞掉。

### 7.2 触发判定

```python
def should_compact(
    self,
    ctx: ContextManager,
    profile: ModelWindowProfile,
    *,
    force: bool = False,
    server_token_limit_reached: bool = False,
) -> tuple[bool, str]:
    status = ctx.token_status(profile)
    if force or server_token_limit_reached:
        return True, "token_limit_reached"
    if status.should_force_compact:
        return True, "input_hard_cap_reached"
    if status.should_auto_compact:
        # 防抖：距上一次成功压缩后，必须增长 resume_interval_tokens
        if self._can_resume(ctx, profile):
            return True, "token_budget"
    return False, ""
```

### 7.3 防抖

- 成功压缩后记录 `last_compaction_tokens`。
- 下一次 `should_auto_compact` 仅当 `active_tokens - last_compaction_tokens >= resume_interval_tokens`。
- 强制信号（hard cap / server token limit）不受防抖限制。

### 7.4 本地摘要

保留现有轻量模型摘要，但：

- 摘要 prompt 区分真实用户消息、工具调用、工具返回、系统/初始上下文。
- 摘要目标不是“全文重写”，而是保留决策、结论、代码变更、文件路径、假设、失败原因、未完成事项。
- 摘要结果放 `[HISTORY SUMMARY]\n...`，并作为 `replacement_history[0]`。
- 若待摘要内容超过 `max_summary_input_tokens`，先按用户 turn/时间块分段摘要，再合并；所有层级都失败才 drop_oldest。
- 若摘要失败：
  - 记录 `status=failed, implementation=local_summary`；
  - 若已接近 hard cap，执行“从最老消息逐条丢弃”的兜底，并记录 `implementation=drop_oldest`；
  - 若仍不安全，向调用方返回 `ContextWindowExceeded` 可恢复错误。

### 7.5 Remote Compaction

```python
class RemoteCompactionSupport(str, Enum):
    UNSUPPORTED = "Unsupported"
    V1 = "RemoteV1"
    V2 = "RemoteV2"
```

压缩实现选择：

```text
if provider.capabilities.supports_remote_compaction:
    try:
        checkpoint = await remote_compactor.compact(...)
        implementation = "remote_v2" / "remote"
    except Exception:
        record remote_failure analytics
        checkpoint = await local_compactor.compact(...)
        implementation = "local_summary"
else:
    checkpoint = await local_compactor.compact(...)
```

Remote 适配器建议接口：

```python
class RemoteCompactor(Protocol):
    async def compact(
        self,
        ctx: ContextManager,
        profile: ModelWindowProfile,
        *,
        phase: str,
        reason: str,
    ) -> CompactionCheckpoint: ...
```

**修订 v2：本迭代只保留占位，不实现具体 remote 逻辑。**
当前 `ResponsesProvider` 走 OpenAI Chat Completions，通常没有 remote compaction；实现完整 remote V1/V2 属于 YAGNI。第一阶段只定义 capability 字段和极薄 protocol，`remote_compaction` 默认关闭。后续有真实后端需要时再单独开阶段。

### 7.6 Hooks

新增最小 hook 协议：

```python
@dataclass(frozen=True, slots=True)
class CompactionHooks:
    pre: Callable[[CompactionContext], Awaitable[None]] | None = None
    post: Callable[[CompactionContext], Awaitable[None]] | None = None
```

`CompactionContext` 至少包含：

```python
@dataclass(frozen=True, slots=True)
class CompactionContext:
    thread_id: str
    turn_id: str | None
    phase: str
    trigger: str
    reason: str
    tokens_before: int
    scope_tokens_before: int
    profile: ModelWindowProfile
```

用途：审计、告警、写入外部数据库、阻断/改写压缩策略。

### 7.7 Analytics / Event

每次压缩（包括 skipped / failed）发出一个类型化事件：

```python
{
  "kind": "context_compaction",
  "window_id": "...",
  "window_number": 3,
  "thread_id": "...",
  "turn_id": "...",
  "phase": "pre_turn|mid_turn|manual",
  "trigger": "auto|manual|remote_fallback",
  "reason": "token_budget|input_hard_cap_reached|token_limit_reached|user_request",
  "implementation": "local_summary|remote|remote_v2|drop_oldest",
  "status": "completed|skipped|failed",
  "tokens_before": 150000,
  "tokens_after": 42000,
  "scope_tokens_before": 80000,
  "scope_tokens_after": 22000,
  "prefill_input_tokens": 18000,
  "replacement_history_count": 12,
  "summary_tokens": 1500,
  "duration_ms": 1234,
  "error": null
}
```

现有 `Event { kind, event_ref, data }` 可直接承载；前端 `traces.py` 需扩展渲染 `context_compaction`。

---

## 8. 触发与编排

### 8.1 Pre-turn

保留 `ThreadRuntime.maybe_compact()`，但使用新 profile/status：

```python
async def maybe_compact(
    self,
    *,
    phase: str = "pre_turn",
    reason: str = "auto",
    force: bool = False,
) -> CompactionCheckpoint | None:
    if self._compactor is None or self._ctx is None or self._llm is None:
        return None
    profile = self._ctx.window_profile()
    should, actual_reason = await self._compactor.should_compact(
        self._ctx, profile, force=force,
        server_token_limit_reached=reason == "token_limit_reached"
    )
    if not should:
        return None
    ckpt = await self._compactor.compact(
        self._ctx, self._llm,
        phase=phase, trigger="auto" if phase != "manual" else "manual",
        reason=actual_reason,
        profile=profile,
    )
    if ckpt.status == "completed":
        await self._persist_compaction(ckpt)
    await self._emit_compaction_event(ckpt)
    return ckpt
```

`_run_turn` 中：

```python
await runtime.maybe_compact(phase="pre_turn")
```

### 8.2 Mid-turn inline

关键点：在 Agent 的采样循环内部、下一次模型请求之前调用 `maybe_compact(phase="mid_turn")`。

#### 接线（修订 v2：明确 MemoryController 接口）

原稿的问题：`_ThreadRunner.run_with_context` 没有 ThreadRuntime 引用，无法直接拿到 `maybe_compact`。因此引入 `MemoryController`，由 ThreadRuntime 持有并显式传下去。

```python
class MemoryController:
    """ThreadRuntime 暴露给 Agent 的内存编排接口。"""
    def __init__(self, runtime: "ThreadRuntime"): ...

    async def maybe_compact(
        self,
        *,
        phase: str = "mid_turn",
        reason: str = "auto",
        force: bool = False,
        protected_start: int | None = None,
    ) -> CompactionCheckpoint | None: ...
```

调用链：

```text
ThreadRuntime._run_turn
  └─ run_with_context(thread, turn, emit, memory, cancel,
                      memory_controller=self._make_memory_controller())
       └─ _ThreadRunner.run_with_context(..., memory_controller)
            └─ RunSession(memory_controller=...)
                 └─ BaseAgentRunner(compact_if_needed=session.memory_controller.maybe_compact)
                      └─ AgentContext.compact_if_needed
                           └─ BaseAgent.run → await ctx.compact_if_needed("mid_turn")
```

`AgentContext` 增加：

```python
compact_if_needed: Callable[[str], Awaitable[Any]] | None = None
```

兼容策略：

- 旧 runner 不支持新签名时，`memory_controller` 为 `None`，`compact_if_needed` 也为 `None`，行为退回无 mid-turn 模式。
- 新 runner 必须显式接收并传递 `MemoryController`，不靠全局状态。

#### provider 侧 usage / token limit 捕获

- `ResponsesProvider.stream()` 调用 Chat Completions 时增加：
  ```python
  "stream_options": {"include_usage": True}
  ```
- 收集流末尾 `chunk.usage`，在 `response_completed` 事件中带上（**必须保留 `prompt_tokens/input_tokens`**，`total_tokens` 仅诊断）：
  ```python
  data={"finish_reason": ..., "accumulated_text": ..., "usage": {"input_tokens": ..., "output_tokens": ..., "total_tokens": ...}}
  ```
- `_sample_once` 或 `BaseAgent.run` 调用 `ctx.memory.update_server_usage(...)`，只用 `input_tokens` 更新 active context。
- 若 provider 返回“context length exceeded / maximum context length”类错误，标记 `server_token_limit_reached=True`，在下一次采样前强制 mid-turn compact。

#### 重要：双 cursor 修复（修订 v2）

必须区分两个游标，不能用一个 `before_index` 包打天下：

```python
# ThreadRuntime / MemoryController 维护：
turn_start_index   # 当前 turn 第一条新消息之前的位置；mid-turn split 的硬保护边界
persist_cursor     # 已写入 rollout 的分界；正常结束时用它记录新增消息
rollback_cursor    # 失败时回滚到哪个位置
```

mid-turn compact 会改变消息列表长度，因此所有游标都要重定位：

```python
def _rebase_all_cursors(ctx, ckpt):
    start = ckpt.compact_start
    end = ckpt.compact_end
    new_count = ckpt.replacement_count
    turn_start_index = ctx.rebase_snapshot_after_replace(turn_start_index, start, end, new_count)
    persist_cursor = ctx.rebase_snapshot_after_replace(persist_cursor, start, end, new_count)
    rollback_cursor = ctx.rebase_snapshot_after_replace(rollback_cursor, start, end, new_count)
```

`rebase_snapshot_after_replace` 规则：

```python
def rebase_snapshot_after_replace(self, idx, start, end, new_count):
    if idx <= start:
        return idx
    if idx >= end:
        return idx - (end - start) + new_count
    return start + new_count
```

#### mid-turn 持久化协议（修订 v2）

1. mid-turn 成功 compact 后，先写 `compaction` checkpoint。
2. 只写 `ctx.items[ckpt.replacement_count:]`——因为 `replacement_history` 已经写在 checkpoint 里，**不能**再按旧格式写 `items[1:]`，否则恢复时会重复。
3. 更新 `persist_cursor = len(ctx.items)`。
4. 后续新消息继续由正常结束路径的 `items_since(persist_cursor)` 写入。
5. `turn_start_index` 仍用于保护 split；`rollback_cursor` 视回滚策略而定。

#### mid-turn 失败/回滚策略（二选一，默认 A）

- **A. 提交式 mid-turn compaction（默认）**：
  - 压缩后立即持久化 checkpoint + tail；
  - `rollback_cursor` 更新为“最后一次 compact 后的 persist_cursor”；
  - 失败只回滚 compact 之后的新消息；
  - 副作用明确：本轮 compact 前已产生的部分 assistant/tool 消息会永久保留。
- **B. 可撤销式 mid-turn compaction**：
  - `rollback_cursor` 保持 `turn_start_index`；
  - 失败时先用 `original_items` 把旧区间还原，再回滚；
  - 若 checkpoint 已写盘，还需写一条 `compaction_revert` 或接受“内存回退、磁盘保留 checkpoint”的差异；
  - 成本高，仅在产品要求严格 turn 原子性时启用。

设计默认选择 A，因为长 turn 中“先压缩再失败”比“放弃压缩回到超窗旧历史”更安全；但必须在文档/事件中标记“部分中间消息可能保留”。

### 8.3 Manual compact

新增独立操作，不依赖 StartTurn：

```python
@dataclass(frozen=True, slots=True)
class CompactThread:
    thread_id: str
    reason: str = "manual"
    force: bool = True
```

`submission_loop` 新增分支：

```python
case Submission(op=CompactThread(thread_id=tid, reason=reason)):
    if runtime.active_turn is not None:
        msg.reply.set_exception(RuntimeError("thread is busy"))
    else:
        ckpt = await runtime.maybe_compact(phase="manual", reason=reason, force=True)
        msg.reply.set_result(ckpt)
```

`ThreadHandle` 暴露：

```python
async def compact(self, *, reason: str = "manual") -> CompactionCheckpoint | None:
    return await self._runtime.submit(CompactThread(thread_id=self.thread_id, reason=reason))  # 或直接内部方法
```

App-server / GUI RPC 增加：

```text
thread/compact/start
参数: { thread_id, reason? }
返回: { ok, window_id, status, tokens_before, tokens_after, ... }
```

同时至少提供一个内部命令 / CLI / TUI 快捷键（例如 `compact`），不能只停留在 JSON-RPC，否则实际使用路径不可达。

### 8.4 多次触发与保护

- pre-turn 和 mid-turn 共用同一套 `should_compact` / 防抖逻辑。
- 若同一次 turn 已 mid-turn 成功压缩一次，后续达到 hard cap 仍允许强制压缩。
- 若连续多次压缩后仍无法降到 `post_compact_target`，启用 `drop_oldest`，并发出 warning/analytics。
- 单 turn 内压缩尝试次数受 `max_compact_attempts_per_turn` 限制；达到上限后不再循环，改为向调用方报告 `ContextWindowExceeded`。

---

## 9. Rollout 持久化与恢复

### 9.1 新 compaction 记录

```json
{
  "seq": 123,
  "ts": "2026-08-31T12:00:00.000Z",
  "type": "compaction",
  "version": 42,
  "window_id": "agent-1:w3:a1b2c3d4",
  "window_number": 3,
  "previous_window_id": "agent-1:w2:...",
  "first_window_id": "agent-1:w1:...",
  "trigger": "auto",
  "reason": "token_budget",
  "implementation": "local_summary",
  "phase": "mid_turn",
  "status": "completed",
  "summary": "[HISTORY SUMMARY]\n...",
  "replacement_history": [ ...ModelMessage JSON... ],
  "replacement_count": 14,
  "protected_start": 0,
  "initial_context_injection": "BeforeLastUserMessage",
  "tokens_before": 151000,
  "tokens_after": 43000,
  "scope_tokens_before": 81000,
  "scope_tokens_after": 23000,
  "prefill_input_tokens": 18000
}
```

兼容与写入规则（修订 v2）：

- 旧记录只有 `type/version/summary` 时，`resume_context_sync` 走旧逻辑（摘要 + tail）。
- 新记录同时写 `summary` 与 `replacement_history`，旧读取器忽略新字段仍可显示摘要。
- **checkpoint 之后只写 `items[replacement_count:]`**，即 replacement 之后的 tail + 后续消息；不要写 `items[1:]`，否则恢复时会把 `replacement_history` 中的保留消息重复追加。
- 原始旧消息在 rollout 中仍作为普通 `msg` 记录保留在 compaction 之前，因此完整历史可回溯；活动上下文恢复则使用最新 checkpoint 的 `replacement_history`。
- 每次重写 tail 会带来一定文件膨胀；本迭代只记录 `rollout_size_bytes` 到 analytics，不在核心路径自动清理。

### 9.2 `resume_context_sync` 新逻辑

```python
def _resume_context_sync(rollout_path: Path) -> ContextManager:
    ctx = ContextManager()
    window = AutoCompactWindowState.start("resumed")
    for line in ...:
        if record.get("type") == "compaction":
            ctx = ContextManager()
            if isinstance(record.get("replacement_history"), list):
                for msg in record["replacement_history"]:
                    ctx.append(model_message_from_payload(msg))
            else:
                # 旧格式回退
                ctx.append(ModelRequest(parts=[SystemPromptPart(
                    content=f"{HISTORY_SUMMARY_PREFIX}{record['summary']}"
                )]))
            # 恢复窗口状态
            window = AutoCompactWindowState(
                window_number=record.get("window_number", 1),
                window_id=record.get("window_id", ...),
                first_window_id=record.get("first_window_id", ...),
                previous_window_id=record.get("previous_window_id"),
                prefill_input_tokens=record.get("prefill_input_tokens"),
            )
            continue
        # 后续 msg 照常 append
```

### 9.3 全量历史恢复（可选）

提供工具方法：

```python
def resume_full_history(rollout_path: Path) -> list[dict]:
    """返回未压缩的原始记录列表；用于审计、研究、调试，不用于活上下文。"""
```

默认活跃上下文仍只重建最新窗口，避免把全部旧消息重新塞回模型。

---

## 10. GUI / 可观测性

- `traces.py` 读取 compaction 时：
  - 展示 `summary`；
  - 展示 `replacement_history` 条数与 window_id；
  - 展示 trigger/reason/implementation/phase/status/tokens。
- 事件流新增 `context_compaction`，前端可显示“上下文已压缩”卡片。
- thread 元数据可查询：
  ```text
  thread/state → { context: { window_id, window_number, tokens, input_hard_cap, output_reserve, scope, status } }
  ```
- 日志至少记录：每次压缩的完整 analytics。

---

## 11. 文件改动清单（建议）

### Python

| 文件 | 改动 |
|---|---|
| `src/athena/core/agent/settings.py` | 读取 `context_window` / `effective_context_window_percent` / `output_reserve_tokens` / `[llm.compaction]` / per-model 配置 |
| `src/athena/core/agent/models.py` | `AgentContext` 增加 `compact_if_needed`；新增 `ModelWindowProfile` / `ModelRegistry` |
| `src/athena/memory/context_manager.py` | token usage、prefill、scope、window state、user boundaries、双 cursor、rebase |
| `src/athena/memory/compaction.py` | 新 `CompactionCheckpoint`、`CompactSplit`、保护边界、整体预算、分层摘要、remote 占位、hooks/analytics |
| `src/athena/memory/rollout.py` | compaction 记录写 `replacement_history`；checkpoint 后只写 tail；恢复逻辑 |
| `src/athena/app_server/submissions.py` | `CompactThread` 操作 |
| `src/athena/app_server/thread_runtime.py` | `MemoryController`、`maybe_compact` 新签名、mid-turn 透传、双 cursor、手动 compact、事件 |
| `src/athena/app_server/thread_manager.py` | `ThreadHandle.compact()`、RPC 接线 |
| `src/athena/gui/traces.py` | 渲染新 compaction 元数据 |
| `src/athena/core/agent/provider.py` | `stream_options` 收集 `input_tokens` usage、capabilities |
| `src/athena/core/agent/runtime.py` | `_sample_once` 用 `input_tokens` 更新 server usage；`BaseAgent.run` 调用 mid-turn compact |
| `src/athena/agents/base_runner.py` | 接收并透传 `compact_if_needed` |
| `src/athena/core/agent/session.py` | 保存并透传 `MemoryController` |
| `config.example.toml` | 新配置节 + per-model examples |

### TypeScript

平行移植以下核心文件：

| 文件 | 改动 |
|---|---|
| `athena_ts/packages/athena-agent/src/memory/context-manager.ts` | 同步 token usage / prefill / scope / user boundary / cursor rebase |
| `athena_ts/packages/athena-agent/src/memory/compaction.ts` | 同步 checkpoint / replacement_history / 本地 fallback（remote 仅占位） |
| `athena_ts/packages/athena-agent/src/memory/rollout.ts` | 同步记录与恢复 |

若 TS 侧只是内存库、尚未接入完整 turn 编排，可先只同步数据模型和恢复逻辑；mid-turn/manual 待 TS harness 稳定后再接。

---

## 12. 实施阶段

### Phase 1：数据与配置

1. 定义 `ModelWindowProfile`、`ModelRegistry`、`TokenUsageInfo`、`AutoCompactScope`、`ContextWindowStatus`、`AutoCompactWindowState`。
2. `ContextManager` 增加 server usage / prefill / scope 计算 / window state / 双 cursor。
3. `settings` + `config.example.toml` 接入全局与 per-model 配置。
4. 删除 `should_compact(ctx, at_tokens=170_000)` 的硬编码路径。

### Phase 2：可恢复压缩

5. 修改 `Compaction` → `CompactionCheckpoint`，生成 `replacement_history`。
6. 保留最近真实用户消息与对应 assistant 块，且强制 `protected_start` 边界。
7. 更新 rollout 记录；**checkpoint 后只写 `items[replacement_count:]`**，并更新 `resume_context_sync`。
8. 保持旧 rollout 兼容。

### Phase 3：触发与编排

9. Pre-turn 使用新 status 判定。
10. Mid-turn inline：provider 捕获 `input_tokens`、`MemoryController` 透传、双 cursor 修复、提交式持久化。
11. Manual compact：`CompactThread` + `thread/compact/start` + 内部命令。
12. Remote compact：本迭代只做占位与回退骨架，不实现具体后端。

### Phase 4：观测与测试

13. `context_compaction` 事件 + GUI trace。
14. 单测/集成测试覆盖预算、hard cap、防抖、replacement 恢复、mid-turn cursor、manual。

---

## 13. 测试与验收标准

### 13.1 单元测试

- `ModelWindowProfile.resolved_context_window()` / `input_hard_cap()` / percent / output reserve 计算。
- `ContextManager.scope_tokens(Total/BodyAfterPrefix)`，prefill server vs estimated；prefill 未知时回退 Total。
- `ContextWindowStatus` 五种边界：
  - 未到预算
  - 到达 auto budget
  - 到达 fallback buffer
  - 到达 input hard cap
  - server token_limit_reached
- 防抖：压缩后未增长 `resume_interval_tokens` 不再自动压缩。
- `select_split` 的 `protected_start`：mid-turn 不得吞当前 turn。
- `select_split`：保留最近 N 条真实用户消息；保留 token 预算截断；replacement 总预算迭代；不把 tail 重复放入 replacement。
- `resume_context_sync`：新记录用 replacement_history 重建；checkpoint 后 tail 不重复；旧记录仍走摘要回退。
- mid-turn 双 cursor（turn_start / persist / rollback）rebase：替换后都指向正确位置。
- `active_context_tokens` 使用 `input_tokens` 而非 `total_tokens` 的回归单测。

### 13.2 集成测试

- 长会话自动 pre-turn 压缩。
- 模拟超长 turn 连续工具调用触发 mid-turn 压缩。
- 手动 `thread/compact/start` 空闲线程成功、运行中线程返回 busy。
- provider 返回 context length exceeded → 强制压缩日志 `reason=token_limit_reached`。
- remote compaction 失败 → 回退本地摘要并记录 `implementation=local_summary`。

### 13.3 验收（Definition of Done）

1. 代码中不再出现固定 `200_000` / `170_000` 作为压缩依据（保留默认常量仅作未知模型回退）。
2. 同一模型可根据配置得到不同 `input_hard_cap`，且正确扣除输出预留。
3. `ContextWindowStatus` 可查询，且 JSON / 日志能完整看到 input_tokens / total_tokens / scope / prefill / budget / hard cap。
4. 任意一次成功压缩后，rollout 记录含 `summary + replacement_history + window_id + trigger/reason/implementation/phase/status/tokens`。
5. 从 rollout 恢复后的上下文等于压缩后实际上下文（不缺失最近真实用户消息、不错位 initial context、不重复 replacement 中已保存的消息）。
6. mid-turn 压缩不会吞掉当前 turn 消息；`protected_start` 单测通过。
7. 支持 manual compact；运行中线程有明确 busy 行为。
8. mid-turn 压缩后 turn 继续可正常完成，rollout 可恢复，且双 cursor 场景不会越界丢消息。
9. 旧 rollout 文件仍可读取和显示。

---

## 14. 兼容 / 迁移 / 回滚

- 新代码读取旧 rollout：`replacement_history` 不存在时退回旧摘要格式。
- 旧代码读取新 rollout：只取 `summary`，忽略新字段，行为不崩。
- 默认配置保持 `BodyAfterPrefix` 关闭或开启？
  - 建议默认 `BodyAfterPrefix`，但 `keep_recent_tokens=20_000` 等旧值保留，降低行为突变。
  - 如担心回归，可提供 `auto_compact_scope = "Total"` 一键回到旧口径。
- 回滚方式：
  - 每个阶段独立提交；Phase 1/2 可单独回退；
  - mid-turn inline 若出现问题，可配置关闭（`compact_if_needed=None`），不影响 pre-turn/manual。

---

## 15. 风险与未决

1. **Token 估算仍偏粗**：本设计不彻底替换 tokenizer，只让服务端 usage 校准。若 provider 不返回 usage，`BodyAfterPrefix` 仍依赖 estimate。
2. **真实用户消息识别**：mailbox 信封、工具返回、系统消息混在 `ModelRequest` 中，需要稳定的“真实用户”判定规则；建议用 `UserPromptPart` + 排除 `[ATHENA MAILBOX MESSAGE]` 前缀。
3. **mid-turn 与失败回滚**：默认提交式 compact 会保留部分中间消息；如果产品要求严格 turn 原子性，需启用可撤销式方案并增加额外持久化复杂度。
4. **Remote compaction 依赖后端**：当前 OpenAI Chat Completions 无此能力，本设计已降级为占位；不阻塞本地路径。
5. **配置爆炸**：字段较多，建议集中到 `ModelWindowProfile` / `ModelRegistry`，不要散落到各函数签名。
6. **TS 与 Python 双重维护**：如果后续确定走 DSH/外部 harness，应优先让外部 harness 承担这些能力；本设计可作为自研路径的决策参考。
7. **输出预留策略依赖后端**：不同 provider 对 context window 是否包含输出 token 的表述可能不同，`output_reserve_tokens` 需要按真实后端校准，不能拍脑袋。
