import pytest

from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    CommandResult,
    OutboxRecord,
    RunRecord,
)
from athena.core.agent_kernel.types import (
    AgentMessage,
    AgentSpec,
    AgentStatus,
    RunStatus,
)

from ._support import EchoRunner, JsonCodec


def _spec() -> AgentSpec:
    return AgentSpec(runner=EchoRunner(), codec=JsonCodec(), role="debater")


def _spawn(store: AgentGraphStore, agent_id: str = "root") -> None:
    store.commit(
        command_id=f"c:{agent_id}",
        kind="spawn",
        payload={
            "agent": AgentRecord(
                agent_id=agent_id,
                path=(agent_id,),
                name=agent_id,
                role="debater",
                parent_id=None,
                status=AgentStatus.IDLE,
                spec=_spec(),
                created_sequence=store.sequence + 1,
            ),
            "run": RunRecord(
                run_id=f"{agent_id}:r1",
                agent_id=agent_id,
                parent_run_id=None,
                status=RunStatus.QUEUED,
                generation=0,
                request_ref="request",
            ),
        },
    )


def test_commit_assigns_monotonic_sequence_and_journals() -> None:
    store = AgentGraphStore()
    agent = AgentRecord(
        agent_id="a1",
        path=("a1",),
        name="a1",
        role="debater",
        parent_id=None,
        status=AgentStatus.IDLE,
        spec=_spec(),
        created_sequence=1,
    )
    run = RunRecord(
        run_id="a1:r1",
        agent_id="a1",
        parent_run_id=None,
        status=RunStatus.QUEUED,
        generation=0,
        request_ref="request",
    )
    payload = {"agent": agent, "run": run}
    assert store.commit(command_id="c1", kind="spawn", payload=payload) == 1
    assert store.commit(command_id="c2", kind="spawn", payload=payload) == 2
    assert [r.kind for r in store.journal] == ["spawn", "spawn"]


def test_result_for_round_trip() -> None:
    store = AgentGraphStore()
    assert store.result_for("cmd-1") is None
    store.record_result("cmd-1", CommandResult(value=("root", "root:r1")))
    assert store.result_for("cmd-1").value == ("root", "root:r1")


def test_spawn_indexes_agent_and_run() -> None:
    store = AgentGraphStore()
    _spawn(store)
    assert store.agent("root").status == AgentStatus.IDLE
    assert store.run("root:r1").status == RunStatus.QUEUED
    assert store.agent("missing") is None


def test_run_running_then_terminal_updates_statuses() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="c2",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 1},
    )
    assert store.run("root:r1").generation == 1
    assert store.agent("root").status == AgentStatus.RUNNING
    store.commit(
        command_id="c3",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.COMPLETED,
            "response_ref": "result://1",
            "error": None,
            "reason": None,
        },
    )
    assert store.run("root:r1").status == RunStatus.COMPLETED
    assert store.agent("root").status == AgentStatus.IDLE
    assert store.agent("root").pending_run_id is None


def test_mailbox_cursors_track() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="hi", sequence=1),
        },
    )
    assert [m.content for m in store.mailbox("root")] == ["hi"]
    store.commit(
        command_id="k1", kind="mailbox_committed", payload={"agent_id": "root"}
    )
    assert store.mailbox_committed("root") == 1


def test_outbox_dedup_by_child_run_id() -> None:
    store = AgentGraphStore()
    _spawn(store)
    _spawn(store, "child")
    summary = store.run_summary("child:r1")
    record = OutboxRecord(
        child_run_id="child:r1",
        child_agent_id="child",
        parent_agent_id="root",
        summary=summary,
    )
    store.commit(command_id="o1", kind="outbox", payload={"record": record})
    store.commit(command_id="o2", kind="outbox", payload={"record": record})
    assert store.outbox_for("child:r1") is record


def test_snapshot_and_load_rebuild_identical_state() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="c2",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 1},
    )
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="hi", sequence=1),
        },
    )
    snapshot = store.snapshot()
    fresh = AgentGraphStore()
    fresh.load(snapshot, [])
    assert fresh.agent("root").status == AgentStatus.RUNNING
    assert fresh.run("root:r1").generation == 1
    assert [m.content for m in fresh.mailbox("root")] == ["hi"]


def test_snapshot_is_independent_of_later_mutation() -> None:
    store = AgentGraphStore()
    _spawn(store)
    snapshot = store.snapshot()
    store.commit(command_id="z", kind="agent_closed", payload={"agent_id": "root"})
    assert snapshot.agents["root"].status == AgentStatus.IDLE


def test_load_advances_sequence_past_replayed_journal() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="hi", sequence=1),
        },
    )
    snapshot = store.snapshot()
    store.commit(
        command_id="m2",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="hi2", sequence=2),
        },
    )
    fresh = AgentGraphStore()
    fresh.load(snapshot, store.journal)
    assert [m.content for m in fresh.mailbox("root")] == ["hi", "hi2"]
    assert (
        fresh.commit(
            command_id="c1",
            kind="run_running",
            payload={"run_id": "root:r1", "generation": 1},
        )
        == snapshot.sequence + 2
    )


def test_commit_is_idempotent_by_command_id() -> None:
    store = AgentGraphStore()
    agent = AgentRecord(
        agent_id="a1",
        path=("a1",),
        name="a1",
        role="debater",
        parent_id=None,
        status=AgentStatus.IDLE,
        spec=_spec(),
        created_sequence=1,
    )
    run = RunRecord(
        run_id="a1:r1",
        agent_id="a1",
        parent_run_id=None,
        status=RunStatus.QUEUED,
        generation=0,
        request_ref="request",
    )
    payload = {"agent": agent, "run": run}
    assert store.commit(command_id="c1", kind="spawn", payload=payload) == 1
    assert store.commit(command_id="c1", kind="spawn", payload=payload) == 1
    assert [r.kind for r in store.journal] == ["spawn"]


def test_terminal_cas_first_writer_wins() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="rr",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 1},
    )
    store.commit(
        command_id="t1",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.COMPLETED,
            "response_ref": "r://1",
            "error": None,
            "reason": None,
        },
    )
    store.commit(
        command_id="t2",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.FAILED,
            "response_ref": None,
            "error": "late",
            "reason": None,
        },
    )
    assert store.run("root:r1").status == RunStatus.COMPLETED
    assert store.run("root:r1").response_ref == "r://1"


def test_failed_apply_leaves_zero_state_change() -> None:
    store = AgentGraphStore()
    seq = store.sequence
    with pytest.raises(KeyError):
        store.commit(
            command_id="bad",
            kind="run_terminal",
            payload={"run_id": "missing"},
        )
    assert store.sequence == seq
    assert store.journal == []


def test_commit_is_failure_atomic_leaves_no_residue() -> None:
    store = AgentGraphStore()
    seq = store.sequence
    agent = AgentRecord(
        agent_id="a1",
        path=("a1",),
        name="a1",
        role="debater",
        parent_id=None,
        status=AgentStatus.IDLE,
        spec=_spec(),
        created_sequence=1,
    )
    with pytest.raises(KeyError):
        # 缺 run 键 → 不得残留孤 Agent
        store.commit(command_id="bad1", kind="spawn", payload={"agent": agent})
    assert store.sequence == seq
    assert store.agent("a1") is None
    assert store.journal == []
    with pytest.raises(KeyError):
        # 缺 message 键 → 不得残留空 mailbox
        store.commit(command_id="bad2", kind="mailbox", payload={"agent_id": "a1"})
    assert store.sequence == seq
    assert store.journal == []


def test_terminal_cas_loser_does_not_advance_sequence() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="rr",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 1},
    )
    store.commit(
        command_id="t1",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.COMPLETED,
            "response_ref": "r://1",
            "error": None,
            "reason": None,
        },
    )
    before = store.sequence
    store.commit(
        command_id="t2",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.FAILED,
            "response_ref": None,
            "error": "late",
            "reason": None,
        },
    )
    assert store.sequence == before  # CAS loser 不推进 sequence
    assert store.run("root:r1").status == RunStatus.COMPLETED


def test_run_running_refuses_terminal_run() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="rr",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 1},
    )
    store.commit(
        command_id="t1",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.COMPLETED,
            "response_ref": "r://1",
            "error": None,
            "reason": None,
        },
    )
    before = store.sequence
    store.commit(
        command_id="rr2",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 2},
    )
    assert store.sequence == before  # 终态 Run 不可重开
    assert store.run("root:r1").status == RunStatus.COMPLETED


def test_terminal_rejects_stale_generation() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="rr",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 2},
    )
    before = store.sequence
    store.commit(
        command_id="t1",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.FAILED,
            "generation": 1,  # 过期 generation → 不写入
            "response_ref": None,
            "error": "stale",
            "reason": None,
        },
    )
    assert store.sequence == before
    assert store.run("root:r1").status == RunStatus.RUNNING


def test_idempotency_ledger_survives_snapshot_and_load() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="first", sequence=1),
        },
    )
    snapshot = store.snapshot()
    fresh = AgentGraphStore()
    fresh.load(snapshot, [])  # 空 journal 恢复
    # 快照前已应用的 m1 不得重复执行
    fresh.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="first", sequence=1),
        },
    )
    assert [m.content for m in fresh.mailbox("root")] == ["first"]


def test_commit_rejects_reused_command_id_with_different_payload() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="first", sequence=1),
        },
    )
    with pytest.raises(ValueError):
        store.commit(
            command_id="m1",
            kind="mailbox",
            payload={
                "agent_id": "root",
                "message": AgentMessage(source="x", content="second", sequence=1),
            },
        )


def test_terminal_rejects_future_generation() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="rr",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 2},
    )
    before = store.sequence
    store.commit(
        command_id="t1",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.COMPLETED,
            "generation": 99,  # 未来 generation → 拒绝
            "response_ref": "r://1",
            "error": None,
            "reason": None,
        },
    )
    assert store.sequence == before
    assert store.run("root:r1").status == RunStatus.RUNNING


def test_terminal_rejects_non_terminal_status() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="rr",
        kind="run_running",
        payload={"run_id": "root:r1", "generation": 1},
    )
    before = store.sequence
    store.commit(
        command_id="t1",
        kind="run_terminal",
        payload={
            "run_id": "root:r1",
            "status": RunStatus.RUNNING,  # 非终态 → 拒绝
            "generation": 1,
            "response_ref": None,
            "error": None,
            "reason": None,
        },
    )
    assert store.sequence == before
    assert store.run("root:r1").status == RunStatus.RUNNING


def test_mailbox_committed_rejects_cursor_beyond_length() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(
        command_id="m1",
        kind="mailbox",
        payload={
            "agent_id": "root",
            "message": AgentMessage(source="x", content="a", sequence=1),
        },
    )
    before = store.sequence
    store.commit(
        command_id="ck1",
        kind="mailbox_committed",
        payload={"agent_id": "root", "committed": 99},  # > len → 拒绝
    )
    assert store.sequence == before
    assert store.mailbox_committed("root") == 0


def test_fingerprint_is_key_order_independent() -> None:
    store = AgentGraphStore()
    fp1 = store._fingerprint("mailbox", {"agent_id": "a", "message": "x"})
    fp2 = store._fingerprint("mailbox", {"message": "x", "agent_id": "a"})
    assert fp1 == fp2
