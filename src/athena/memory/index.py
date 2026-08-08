"""确定性项目/全局记忆索引（设计 agent-memory-human-wait §3-§6）。

条目不可原地改写：修正旧条目时创建 ``supersedes`` 新条目，历史保留。
项目记忆（``scope="project"``）只保存已验证事实/决定/修复；全局记忆候选
（``scope="global"``）必须 ``approve`` 后才能被检索，``reject`` 只留审计。
默认检索忽略被 supersede 的旧条目，但审计保留全部条目。
"""

from dataclasses import asdict, dataclass, replace
from uuid import uuid4


@dataclass(frozen=True)
class MemoryEntry:
    """一条不可变记忆条目（最小字段，不携带业务对象）。"""

    entry_id: str
    scope: str  # "project" | "global"
    kind: str  # fact / decision / fix
    summary: str
    source_refs: list[str]
    created_by: str
    supersedes: str | None = None
    status: str = "active"  # active | approved | rejected


class MemoryStore:
    """项目/全局记忆的 append-only 索引（首版内存实现）。"""

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}

    def add(
        self,
        *,
        scope: str,
        kind: str,
        summary: str,
        source_refs: list[str],
        created_by: str,
        supersedes: str | None = None,
    ) -> str:
        """提交一条记忆条目，返回 entry_id；不可原地改写。"""
        entry_id = f"mem_{uuid4().hex[:8]}"
        self._entries[entry_id] = MemoryEntry(
            entry_id=entry_id,
            scope=scope,
            kind=kind,
            summary=summary,
            source_refs=list(source_refs),
            created_by=created_by,
            supersedes=supersedes,
        )
        return entry_id

    def approve(self, entry_id: str) -> None:
        """批准全局候选；未批准前其他项目不能检索到它。"""
        self._set_status(entry_id, "approved")

    def reject(self, entry_id: str) -> None:
        """拒绝全局候选：只保留审计，不写全局索引。"""
        self._set_status(entry_id, "rejected")

    def _set_status(self, entry_id: str, status: str) -> None:
        """以新状态替换条目（frozen dataclass 不可原地改写）。"""
        entry = self._require(entry_id)
        self._entries[entry_id] = replace(entry, status=status)

    def retrieve(
        self, *, scope: str | None = None, kind: str | None = None
    ) -> list[MemoryEntry]:
        """按 scope/kind 过滤可检索条目；忽略被 supersede 的旧条目与未批准全局候选。"""
        superseded = {e.supersedes for e in self._entries.values() if e.supersedes}
        result: list[MemoryEntry] = []
        for entry in self._entries.values():
            if entry.status == "rejected":
                continue
            if scope == "global" and entry.status != "approved":
                continue  # 全局候选批准前不可检索
            if scope is not None and entry.scope != scope:
                continue
            if kind is not None and entry.kind != kind:
                continue
            if entry.entry_id in superseded:
                continue  # 被新条目取代的旧条目默认不可检索
            result.append(entry)
        return result

    def get(self, entry_id: str) -> MemoryEntry:
        """按 id 取条目（含 superseded/rejected，供审计）。"""
        return self._require(entry_id)

    def all(self) -> list[MemoryEntry]:
        """全部条目（含被忽略与拒绝），供审计。"""
        return list(self._entries.values())

    def to_dict(self) -> list[dict]:
        """序列化全部条目（供项目状态持久化）。"""
        return [asdict(e) for e in self._entries.values()]

    def load_dict(self, entries: list[dict]) -> None:
        """从持久化条目重建索引（append-only 恢复）。"""
        self._entries = {e["entry_id"]: MemoryEntry(**e) for e in entries}

    def _require(self, entry_id: str) -> MemoryEntry:
        if entry_id not in self._entries:
            raise KeyError(f"unknown memory entry: {entry_id}")
        return self._entries[entry_id]


if __name__ == "__main__":
    store = MemoryStore()
    entry_id = store.add(
        scope="project",
        kind="fix",
        summary="示例：修复了数据切分 bug",
        source_refs=["sha256:demo"],
        created_by="demo",
    )
    print(f"MemoryStore.add -> {entry_id}")
    print(f"retrieve(scope='project') -> {len(store.retrieve(scope='project'))} 条")
