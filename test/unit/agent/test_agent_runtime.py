import asyncio
import json

from ._support import BlockingAgent, make_runtime

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentOutcome

from athena.core.agent.codec import JsonCodec
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, AgentStatus, RunStatus
from athena.agents.base_runner import BaseAgentRunner
from athena.core.artifact_store import LocalArtifactStore


async def test_create_root_and_followup_run_turns(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "hello"})
    assert agent_id and run_id
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    payload = json.loads(summary.response_ref)
    store = LocalArtifactStore(tmp_path / "artifacts")
    result = json.loads(await store.get_text(payload["result_ref"]))
    assert result["result"] == "hello"

    run2 = await rt.followup(agent_id, {"content": "world"})
    summary2 = await rt.wait_run(run2, timeout=5)
    payload2 = json.loads(summary2.response_ref)
    result2 = json.loads(await store.get_text(payload2["result_ref"]))
    assert result2["result"] == "world"
    await rt.aclose()


async def test_send_message_delivers_to_next_turn(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "first"})
    await rt.wait_run(run_id, timeout=5)
    await rt.send_message(agent_id, "queued-note")
    run2 = await rt.followup(agent_id, {"content": "second"})
    summary2 = await rt.wait_run(run2, timeout=5)
    payload2 = json.loads(summary2.response_ref)
    store = LocalArtifactStore(tmp_path / "artifacts")
    result2 = json.loads(await store.get_text(payload2["result_ref"]))
    assert result2["result"] == "second"  # trigger 仍是本轮 content
    await rt.aclose()


async def test_interrupt_marks_run_interrupted(tmp_path):
    gate = asyncio.Event()
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "block",
        lambda aid, cfg=None: AgentSpec(
            runner=BaseAgentRunner(BlockingAgent(gate)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    agent_id, run_id = await rt.create_root("block", {"content": "x"})
    await asyncio.wait_for(gate.wait(), timeout=2)  # 等 turn 进入运行
    assert rt.agent_status(agent_id) == AgentStatus.RUNNING
    await rt.interrupt(agent_id, "test stop")
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.INTERRUPTED
    await rt.aclose()


async def test_spawn_records_parent(tmp_path):
    rt = make_runtime(tmp_path)
    parent, run1 = await rt.create_root("stub", {"content": "p"})
    await rt.wait_run(run1, timeout=5)
    child, run2 = await rt.spawn(parent, "stub", {"content": "c"})
    await rt.wait_run(run2, timeout=5)
    snaps = {s.agent_id: s for s in rt.list_agents()}
    assert snaps[child].parent_id == parent
    assert snaps[child].path == (
        "root",
        "stub",
    )  # 名称缺省 = agent_type;root 固定名为 "root"
    await rt.aclose()
