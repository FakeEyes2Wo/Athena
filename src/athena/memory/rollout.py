"""Append-only JSONL 持久化到项目目录。

对齐 Codex ``rollout/src/recorder.rs``。

路径规则: ``{project}/.athena/sessions/{date}/rollout-{short_id}.jsonl``
序列化: PydanticAI 内置 ``ModelMessagesTypeAdapter`` — 无需自定义格式。
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
)

from athena.memory.context_manager import ContextManager


class RolloutRecorder:
    """单个 Thread 的 append-only JSONL 记录器。

    用法::

        recorder = RolloutRecorder(project_root)
        await recorder.open(thread_id)
        recorder.record(msg)
        recorder.record_compaction(version, summary)
        await recorder.close()

    每次写入后立即 flush，确保不完整运行仍留下可恢复数据。
    恢复时流式读取记录，遇到更新的 compaction checkpoint 时重置内存上下文。

    Attributes:
        path: 当前文件路径（``open()`` 之前为 ``None``）。
    """

    __slots__ = ("_base", "_path", "_fd", "_seq", "_adapter")

    def __init__(self, project_root: Path) -> None:
        self._base = project_root / ".athena" / "sessions"
        self._path: Path | None = None
        self._fd = None
        self._seq = 0
        self._adapter = ModelMessagesTypeAdapter  # 预构建的单例

    # ── 属性 ───────────────────────────────────────────────────────

    @property
    def path(self) -> Path | None:
        """当前 JSONL 文件路径，``open()`` 之前为 ``None``。"""
        return self._path

    # ── 生命周期 ───────────────────────────────────────────────────

    async def open(self, thread_id: str) -> Path:
        """创建当天的 rollout 文件并返回路径。

        打开状态下可重复调用 — 返回当前路径。
        """
        if self._fd is not None:
            assert self._path is not None
            return self._path

        now = datetime.now(timezone.utc)
        day_dir = self._base / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        short_id = _safe_short_id(thread_id)
        self._path = day_dir / f"rollout-{short_id}-{uuid4().hex[:8]}.jsonl"
        self._fd = open(self._path, "a", encoding="utf-8", newline="\n")
        self._seq = 0
        return self._path

    def record(self, msg: ModelMessage) -> None:
        """追加一条 PydanticAI 消息为 JSONL 行。"""
        if self._fd is None:
            return
        payload = self._adapter.dump_python([msg], mode="json")
        self._write_line({"seq": self._seq, "ts": _utc_now_iso(), "msg": payload})

    def record_compaction(self, version: int, summary: str) -> None:
        """追加一条 compaction 检查点标记。

        恢复时以最新的 compaction 作为起始点。
        """
        if self._fd is None:
            return
        self._write_line(
            {
                "seq": self._seq,
                "type": "compaction",
                "version": version,
                "summary": summary,
            }
        )

    async def close(self) -> None:
        """关闭底层文件句柄。"""
        if self._fd is not None:
            self._fd.close()
            self._fd = None

    # ── 内部 ───────────────────────────────────────────────────────

    def _write_line(self, obj: object) -> None:
        assert self._fd is not None
        self._fd.write(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self._fd.flush()
        self._seq += 1


# ── 恢复 ──────────────────────────────────────────────────────────────


async def resume_context(rollout_path: Path) -> "ContextManager":
    """从 rollout JSONL 文件重建 :class:`ContextManager`。

    文件在事件循环线程之外流式读取。每个更新的 compaction 替换之前的重放状态，
    只保留其摘要及后续消息。损坏/截断的记录被跳过。
    """
    return await asyncio.to_thread(_resume_context_sync, rollout_path)


# ── 辅助函数 ───────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_short_id(thread_id: str) -> str:
    """返回紧凑且安全的文件名 ID。"""
    safe = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in thread_id[:12]
    )
    return safe or "thread"


def _resume_context_sync(rollout_path: Path) -> ContextManager:
    """流式读取一个 rollout，仅保留最新的 compacted 上下文。"""
    adapter = ModelMessagesTypeAdapter
    ctx = ContextManager()

    with rollout_path.open("r", encoding="utf-8", errors="replace") as fd:
        for line in fd:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 崩溃可能导致最后一行不完整。跳过损坏记录而非丢失整个可恢复会话。
                continue
            if not isinstance(record, dict):
                continue

            if record.get("type") == "compaction":
                summary = record.get("summary")
                if not isinstance(summary, str):
                    continue
                ctx = ContextManager()
                ctx.append(
                    ModelRequest(
                        parts=[
                            SystemPromptPart(content=f"[HISTORY SUMMARY]\n{summary}")
                        ]
                    )
                )
                continue

            payload = record.get("msg")
            if payload is None:
                continue
            try:
                messages = adapter.validate_python(payload)
            except Exception:
                continue
            for message in messages:
                ctx.append(message)

    return ctx
