"""AgentGraphStore JSON 序列化往返测试（设计 §10 生产持久化基础）。"""

from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    RunRecord,
    WaitRecord,
)
from athena.core.agent_kernel.store_json import load_store_json, store_to_json
from athena.core.agent_kernel.types import (
    AgentMessage,
    AgentStatus,
    RunStatus,
)


def _spawn_payload() -> dict:
    return {
        "agent": AgentRecord(
            agent_id="a1",
            path=("root",),
            name="root",
            agent_type="data",
            parent_id=None,
            status=AgentStatus.IDLE,
            created_sequence=1,
        ),
        "run": RunRecord(
            run_id="a1:r:1",
            agent_id="a1",
            parent_run_id=None,
            status=RunStatus.QUEUED,
            generation=0,
            request_ref="{}",
        ),
    }


def _build() -> AgentGraphStore:
    store = AgentGraphStore()
    store.commit(command_id="c1", kind="spawn", payload=_spawn_payload())
    store.commit(
        command_id="c2",
        kind="mailbox",
        payload={
            "agent_id": "a1",
            "message": AgentMessage(source="user", content="hi", context_refs=[]),
        },
    )
    store.commit(
        command_id="c3",
        kind="wait_enter",
        payload={
            "run_id": "a1:r:1",
            "wait": WaitRecord(agent_id="a1", kind="agents", target_ids=["kid"]),
            "generation": 0,
        },
    )
    return store


def test_json_round_trip_preserves_state() -> None:
    store = _build()
    text = store_to_json(store)
    fresh = AgentGraphStore()
    load_store_json(text, fresh)
    assert fresh.sequence == store.sequence
    assert fresh.agents() == store.agents()
    assert fresh.runs() == store.runs()
    assert [m.content for m in fresh.mailbox("a1")] == ["hi"]
    assert fresh.waits()["a1"].kind == "agents"
    assert fresh.waits()["a1"].target_ids == ["kid"]
    assert fresh.agent("a1").status == AgentStatus.WAITING  # wait_enter 后状态保留


def test_json_round_trip_is_stable() -> None:
    """同一状态序列化两次得到相同 JSON（幂等）。"""
    store = _build()
    first = store_to_json(store)
    second = store_to_json(store)
    assert first == second
