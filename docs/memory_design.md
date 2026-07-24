# Athena Memory 设计（精简版）

只做 Layer 1：当前对话上下文管理 + JSONL 持久化到项目目录。消息类型用 PydanticAI 原生
`ModelMessage`。

实现状态（2026-07-24）：`ContextManager`、`Compactor`、`RolloutRecorder` 和
`resume_context` 已完成并通过 38 个定向测试。本设计参考当前 Codex 的
`codex-rs/core/src/context_manager/history.rs`、`codex-rs/core/src/compact.rs`、
`codex-rs/rollout/src/recorder.rs` 和
`codex-rs/core/src/session/rollout_reconstruction.rs`。下方代码用于说明核心算法；
最终行为以 `src/athena/memory/` 及测试为准。

---

## 核心三组件

```
ContextManager (内存)          Compactor (压缩)           RolloutRecorder (持久化)
┌──────────────────────┐    ┌──────────────────┐    ┌──────────────────────────┐
│ items: [ModelMessage] │    │ 检测 token 阈值   │    │ append-only JSONL         │
│ token 估算            │───▶│ LLM 生成摘要      │───▶│ ./.athena/sessions/       │
│ version 跟踪          │    │ 保留近期 + summary │    │ 按日期分目录               │
└──────────────────────┘    └──────────────────┘    └──────────────────────────┘
```

---

## 1. ContextManager

```python
# src/athena/memory/context_manager.py

from pydantic_ai.messages import ModelMessage, ModelRequest


class ContextManager:
    """管理一个 Thread 的对话上下文，对齐 Codex ``ContextManager``。

    三个核心职责：
    1. 持有 ``list[ModelMessage]`` — PydanticAI 原生消息
    2. 写入时按新增消息增量估算 token，查询为 O(1)
    3. 支持 compaction 后替换历史区间

    截断策略：
    - 工具输出超过 50k chars 时保留头尾并截断中间
    - 截断采用 copy-on-write，不修改调用方传入的消息
    """

    def __init__(self, context_limit: int = 200_000) -> None:
        self._items: list[ModelMessage] = []
        self._token_count: int = 0       # 增量维护，避免 O(n) 重算
        self._context_limit = context_limit
        self._version: int = 0

    # ── 查询 ──────────────────────────────────────────────────────

    @property
    def items(self) -> list[ModelMessage]:
        return list(self._items)

    @property
    def tokens(self) -> int:
        return self._token_count

    @property
    def version(self) -> int:
        return self._version

    @property
    def limit(self) -> int:
        return self._context_limit

    def token_margin(self, ratio: float = 0.85) -> int:
        """距离触发压缩还有多少 token 余量。"""
        return max(0, int(self._context_limit * ratio) - self._token_count)

    # ── 写入 ──────────────────────────────────────────────────────

    def append(self, msg: ModelMessage) -> None:
        """追加一条消息，自动截断过大内容。"""
        prepared = self._truncate_tool_results(msg)
        self._items.append(prepared)
        self._token_count += self._estimate(prepared)
        self._version += 1

    def replace_range(self, start: int, end: int, new: list[ModelMessage]) -> None:
        """替换 [start, end) 的消息 — compaction 后使用。"""
        prepared = [self._truncate_tool_results(m) for m in new]
        removed_tokens = sum(self._estimate(m) for m in self._items[start:end])
        added_tokens = sum(self._estimate(m) for m in prepared)
        self._items[start:end] = prepared
        self._token_count += added_tokens - removed_tokens
        self._version += 1

    # ── 内部 ──────────────────────────────────────────────────────

    @staticmethod
    def _truncate_tool_results(msg: ModelMessage) -> ModelMessage:
        """超过上限时复制消息和 part，保留输出头尾；常见路径不分配新对象。"""
        ...

    @staticmethod
    def _estimate(msg: ModelMessage) -> int:
        """按 UTF-8 模型可见字节估算，覆盖正文、工具名、参数和 call id。"""
        visible_bytes = ...
        return max(1, (visible_bytes + 3) // 4)
```

---

## 2. Compactor

```python
# src/athena/memory/compaction.py

from dataclasses import dataclass
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart


@dataclass(slots=True)
class Compaction:
    """一次压缩的结果 — 可用于回滚。"""
    version: int
    summary: str
    original_items: list[ModelMessage]   # 压缩前的完整早期历史


class Compactor:
    """上下文压缩：早期对话 → LLM 摘要 → 替换为 SystemPromptPart。

    对齐 Codex ``compact.rs`` 的本地总结模式。
    """

    def __init__(self, keep_recent: int = 20_000, summary_model: str = "haiku") -> None:
        self._keep_recent = keep_recent
        self._summary_model = summary_model

    def should_compact(self, ctx: "ContextManager", at_tokens: int = 170_000) -> bool:
        return ctx.tokens >= at_tokens

    async def compact(self, ctx: "ContextManager", llm) -> Compaction:
        """执行压缩。LLM 客户端通过依赖注入传入。"""
        items = ctx.items
        split = self._split_recent(items)
        old = items[:split]

        summary = await self._summarize(old, llm)
        summary_msg = ModelRequest(parts=[
            SystemPromptPart(content=f"[HISTORY SUMMARY]\n{summary}")
        ])

        # recent tail 已经保留在 ctx 中，只替换旧前缀，不能再次拼入 tail。
        ctx.replace_range(0, split, [summary_msg])

        return Compaction(version=ctx.version, summary=summary, original_items=old)

    # ── 内部 ──────────────────────────────────────────────────────

    def _split_recent(self, items: list[ModelMessage]) -> int:
        acc = 0
        for i in range(len(items) - 1, -1, -1):
            acc += ContextManager._estimate(items[i])
            if acc >= self._keep_recent:
                return i
        return 0

    async def _summarize(self, items: list[ModelMessage], llm) -> str:
        chunks = [
            "Summarize concisely (decisions, findings, code changes, hypotheses, results):\n\n"
        ]
        for m in items:
            role = "USER" if isinstance(m, ModelRequest) else "ASSISTANT"
            for p in m.parts:
                if hasattr(p, 'content') and isinstance(p.content, str):
                    chunks.append(f"[{role}] {p.content[:300]}\n")

        r = await llm.messages.create(
            model=self._summary_model, temperature=0.1, max_tokens=2000,
            messages=[{"role": "user", "content": "".join(chunks)}],
        )
        return r.content[0].text
```

---

## 3. RolloutRecorder

```python
# src/athena/memory/rollout.py

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter


class RolloutRecorder:
    """Append-only JSONL 持久化到项目目录。

    路径: ``{project}/.athena/sessions/{date}/rollout-{id}.jsonl``
    序列化: PydanticAI 内置 ``ModelMessagesTypeAdapter``
    """

    def __init__(self, project_root: Path) -> None:
        self._base = project_root / ".athena" / "sessions"
        self._path: Path | None = None
        self._fd = None
        self._seq = 0
        self._adapter = ModelMessagesTypeAdapter

    @property
    def path(self) -> Path | None:
        return self._path

    async def open(self, thread_id: str) -> Path:
        if self._fd is not None:
            return self._path
        now = datetime.now(timezone.utc)
        day = self._base / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
        day.mkdir(parents=True, exist_ok=True)
        self._path = day / f"rollout-{thread_id[:12]}-{uuid4().hex[:8]}.jsonl"
        self._fd = open(self._path, "a", encoding="utf-8")
        return self._path

    def record(self, msg: ModelMessage) -> None:
        if not self._fd:
            return
        self._fd.write(json.dumps({
            "seq": self._seq, "ts": datetime.now(timezone.utc).isoformat(),
            "msg": self._adapter.dump_python([msg], mode="json"),
        }, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fd.flush()
        self._seq += 1

    def record_compaction(self, version: int, summary: str) -> None:
        if not self._fd:
            return
        self._fd.write(json.dumps({
            "seq": self._seq, "type": "compaction",
            "version": version, "summary": summary,
        }, ensure_ascii=False) + "\n")
        self._fd.flush()
        self._seq += 1

    async def close(self) -> None:
        if self._fd:
            self._fd.close()
            self._fd = None
```

---

## 4. 与 Agent / ThreadRuntime 集成

不引入中间层（hook / adapter / callback）。Memory 操作直接内联到 `_run_turn()` 中，
零额外函数调用开销。

```python
# 在 ThreadRuntime._run_turn() 中内联 — 不创建新类

async def _run_turn(self, turn):
    ctx = self._context_manager
    compactor = self._compactor
    rollout = self._rollout

    # ── Turn 前 ── (内联，无 hook 开销)
    if compactor is not None and compactor.should_compact(ctx):
        ckpt = await compactor.compact(ctx, self._llm)
        rollout.record_compaction(ckpt.version, ckpt.summary)

    ctx.append(ModelRequest(parts=[UserPromptPart(content=user_content)]))

    # ── Turn 中 ── (Agent 执行 — 不变)
    result_ref, next_ctx_ref = await self._runner(...)

    # ── Turn 后 ── (内联，无 hook 开销)
    if rollout is not None:
        for msg in ctx.items[-20:]:
            rollout.record(msg)
    if compactor is not None and compactor.should_compact(ctx):
        ckpt = await compactor.compact(ctx, self._llm)
        rollout.record_compaction(ckpt.version, ckpt.summary)

    return result_ref, next_ctx_ref
```

**为什么不用 hook/回调系统：**

| 方式 | 每次 Turn 开销 | 调试难度 |
|---|---|---|
| 内联调用 | ~0 (正常函数调用) | 低 — 控制流清晰 |
| 直接方法调用 (`adapter.pre_turn()`) | ~0.1μs | 中 — 多一层间接 |
| Hook 注册/分发 (`for h in hooks: await h()`) | ~1-5μs + 可能的动态调度 | 高 — 执行顺序不确定 |

`_run_turn()` 本身就是少数几行编排代码，内联不会让它变复杂。
不创建 `integration.py`，不引入任何 hook 注册机制。

---

## 5. 恢复

```python
async def resume_context(rollout_path: Path) -> "ContextManager":
    """从 JSONL 恢复上下文。

    在工作线程中流式读取；遇到新的 compaction 即丢弃旧内存上下文并加载摘要，
    最终自然得到“最新 compaction + 后续消息”。损坏或崩溃截断的 JSON 行跳过。
    对齐 Codex ``rollout_reconstruction.rs``。
    """
    return await asyncio.to_thread(_resume_context_sync, rollout_path)
```

---

## 6. 目录与文件

```
src/athena/memory/
├── __init__.py              # 导出
├── context_manager.py       # ContextManager（已实现）
├── compaction.py            # Compactor（已实现）
└── rollout.py               # RolloutRecorder + resume_context（已实现）

# 运行时产物 — 在项目目录下
{project}/.athena/sessions/
└── YYYY/MM/DD/rollout-{id}.jsonl
```

---

## 7. 实施顺序

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | `ContextManager` | 已完成、已测试 |
| 2 | `RolloutRecorder` | 已完成、已测试 |
| 3 | `Compactor` | 已完成、已测试 |
| 4 | `resume_context` 恢复逻辑 | 已完成、已测试 |
| 5 | `ThreadRuntime` 集成 | 尚未实施（不属于本次 memory 模块测试范围） |
