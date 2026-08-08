"""确定性 MemoryStore 单元测试（设计 agent-memory-human-wait §8）。"""

import pytest

from athena.memory.index import MemoryStore


def _project_fact(store: MemoryStore, summary: str, kind: str = "fix") -> str:
    return store.add(
        scope="project",
        kind=kind,
        summary=summary,
        source_refs=["sha256:abc"],
        created_by="agent_1",
    )


def test_project_memory_retrievable_by_scope_and_kind() -> None:
    store = MemoryStore()
    _project_fact(store, "修复了数据切分 bug", kind="fix")
    _project_fact(store, "决定采用 accuracy", kind="decision")
    fix_entries = store.retrieve(scope="project", kind="fix")
    assert [e.summary for e in fix_entries] == ["修复了数据切分 bug"]
    assert len(store.retrieve(scope="project")) == 2


def test_global_candidate_not_retrievable_before_approval() -> None:
    store = MemoryStore()
    candidate_id = store.add(
        scope="global",
        kind="fix",
        summary="可跨项目复用的修复",
        source_refs=["sha256:x"],
        created_by="agent_1",
    )
    # 批准前其他项目检索不到
    assert store.retrieve(scope="global") == []
    store.approve(candidate_id)
    assert [e.summary for e in store.retrieve(scope="global")] == ["可跨项目复用的修复"]


def test_rejected_global_candidate_leaves_audit_only() -> None:
    store = MemoryStore()
    candidate_id = store.add(
        scope="global",
        kind="fix",
        summary="未通过的候选",
        source_refs=["sha256:y"],
        created_by="agent_1",
    )
    store.reject(candidate_id)
    assert store.retrieve(scope="global") == []  # 不写全局索引
    assert store.get(candidate_id).status == "rejected"  # 审计保留
    assert len(store.all()) == 1


def test_supersedes_hides_old_entry_but_keeps_audit() -> None:
    store = MemoryStore()
    v1 = _project_fact(store, "旧结论")
    v2 = store.add(
        scope="project",
        kind="fix",
        summary="新结论",
        source_refs=["sha256:z"],
        created_by="agent_1",
        supersedes=v1,
    )
    summaries = [e.summary for e in store.retrieve(scope="project")]
    assert summaries == ["新结论"]  # 默认检索忽略旧条目
    assert store.get(v1).status == "active"  # 审计仍保留旧条目
    assert len(store.all()) == 2
    assert v2 != v1


def test_entries_are_immutable_append_only() -> None:
    store = MemoryStore()
    entry_id = _project_fact(store, "原始")
    # 修正只能创建 supersedes 新条目，不能改写旧条目
    store.add(
        scope="project",
        kind="fix",
        summary="修正",
        source_refs=[],
        created_by="agent_1",
        supersedes=entry_id,
    )
    assert store.get(entry_id).summary == "原始"
    assert len(store.all()) == 2


def test_unknown_entry_raises() -> None:
    store = MemoryStore()
    with pytest.raises(KeyError):
        store.get("mem_nonexistent")
