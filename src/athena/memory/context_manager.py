"""基于 PydanticAI ModelMessage 的对话上下文管理。

snapshot/items_since/rollback 构成版本化快照协议：
- snapshot() 返回 (idx, version) — compaction 后 version 变化可检测
- items_since(idx) 返回 idx 之后新增的消息
- rollback(idx) 回滚并恢复 token 计数，保持 sum(estimate(m)) == _token_count 不变量
"""

from dataclasses import replace

from pydantic_ai.messages import ModelMessage, ModelRequest

_MAX_TOOL_RESULT_CHARS = 50_000
_TRUNCATION_MARKER = "\n... [TRUNCATED] ...\n"


class ContextManager:
    """单个 Thread 的对话上下文，管理消息列表和 token 估算。

    Token 不变量：_token_count 始终等于 sum(_estimate_one(m) for m in _items)。
    所有写入操作（append, replace_range, rollback）都维护此不变量。
    """

    __slots__ = ("_items", "_token_count", "_context_limit", "_version")

    def __init__(self, context_limit: int = 200_000) -> None:
        self._items: list[ModelMessage] = []
        self._token_count: int = 0
        self._context_limit = context_limit
        self._version: int = 0

    # ── 查询 ─────────────────────────────────────────────────

    @property
    def items(self) -> list[ModelMessage]:
        """深拷贝 — 外部修改不影响内部状态和 token 计数。"""
        return [replace(m, parts=[replace(p) for p in m.parts]) for m in self._items]

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
        return max(0, int(self._context_limit * ratio) - self._token_count)

    # ── 快照/回滚 ────────────────────────────────────────────

    def snapshot(self) -> tuple[int, int]:
        """返回 (idx, version)。compaction 后 version 递增，检测过期快照。"""
        return (len(self._items), self._version)

    def items_since(self, idx: int) -> list[ModelMessage]:
        """返回 idx 之后新增的消息。边界保护：越界返回空列表。"""
        if idx < 0 or idx >= len(self._items):
            return []
        return list(self._items[idx:])

    def rollback(self, idx: int) -> None:
        """回滚到 idx，删除之后的消息并恢复 token 计数。"""
        if idx < 0 or idx > len(self._items):
            return
        removed = sum(self._estimate_one(m) for m in self._items[idx:])
        self._items = self._items[:idx]
        self._token_count -= removed
        self._version += 1

    # ── 写入 ─────────────────────────────────────────────────

    def append(self, msg: ModelMessage) -> None:
        prepared = self._prepare(msg)
        self._items.append(prepared)
        self._token_count += self._estimate_one(prepared)
        self._version += 1

    def replace_range(
        self, start: int, end: int, new_items: list[ModelMessage]
    ) -> None:
        """替换 [start:end] 区间的消息（compaction 使用）。"""
        prepared = [self._prepare(item) for item in new_items]
        removed = sum(self._estimate_one(m) for m in self._items[start:end])
        added = sum(self._estimate_one(m) for m in prepared)
        self._items[start:end] = prepared
        self._token_count += added - removed
        self._version += 1

    # ── 内部 ─────────────────────────────────────────────────

    @staticmethod
    def _prepare(msg: ModelMessage) -> ModelMessage:
        """预处理消息：深拷贝 parts 并截断过长工具返回。

        截断策略：保留头尾各半，中间插入截断标记。
        这样既保留 prompt 上下文（头部），又保留最终输出（尾部）。
        """
        if not isinstance(msg, ModelRequest):
            return replace(msg, parts=list(msg.parts))
        parts = list(msg.parts)
        for i, part in enumerate(parts):
            if getattr(part, "part_kind", None) == "tool-return" and isinstance(
                getattr(part, "content", None), str
            ):
                content: str = part.content
                if len(content) > _MAX_TOOL_RESULT_CHARS:
                    budget = _MAX_TOOL_RESULT_CHARS - len(_TRUNCATION_MARKER)
                    head = budget // 2
                    tail = budget - head
                    parts[i] = replace(
                        part,
                        content=(content[:head] + _TRUNCATION_MARKER + content[-tail:]),
                    )
        return replace(msg, parts=parts)

    @staticmethod
    def _estimate_one(msg: ModelMessage) -> int:
        """粗略 token 估算：字符串长度 / 4，含 args 序列化开销。"""
        total = 0
        for part in msg.parts:
            c = getattr(part, "content", None)
            if isinstance(c, str):
                total += len(c)
            a = getattr(part, "args", None)
            if a is not None:
                total += len(str(a))
        return max(1, total // 4)
