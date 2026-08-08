"""simple_agents（Code/Ideator/Plot）确定性业务 Agent 测试。

覆盖合并后的 :mod:`athena.agents.simple_agents`：CodeAgent/IdeatorAgent 的
确定性 JSON payload 形状与 run_impl 委托，以及 PlotAgent 的图片/图注产出。
"""

import json

import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.simple_agents import CodeAgent, IdeatorAgent, PlotAgent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentOutcome
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, RunStatus
from athena.core.artifact_store import LocalArtifactStore

from ._support import JsonCodec, request_payload


def _runtime(agent, agent_type: str, tmp_path) -> AgentRuntime:
    registry = AgentTypeRegistry()
    registry.register(
        agent_type,
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(agent), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


async def _result_ref(summary) -> str:
    return json.loads(summary.response_ref)["result_ref"]


@pytest.mark.asyncio
async def test_code_agent_writes_candidate_diff(tmp_path) -> None:
    """确定性缺省：把输入写为 ``{diff, status: candidate}`` JSON Artifact。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = CodeAgent(store)
    rt = _runtime(agent, "code", tmp_path)
    _, run_id = await rt.create_root("code", {"content": "补丁描述"})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    artifact = json.loads(await store.get_text(await _result_ref(summary)))
    assert artifact == {"diff": "补丁描述", "status": "candidate"}
    await rt.aclose()


@pytest.mark.asyncio
async def test_ideator_agent_writes_proposed_hypothesis(tmp_path) -> None:
    """确定性缺省：把输入写为 ``{hypothesis, status: PROPOSED}`` JSON Artifact。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = IdeatorAgent(store)
    rt = _runtime(agent, "ideator", tmp_path)
    _, run_id = await rt.create_root("ideator", {"content": "新假设"})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    artifact = json.loads(await store.get_text(await _result_ref(summary)))
    assert artifact == {"hypothesis": "新假设", "status": "PROPOSED"}
    await rt.aclose()


@pytest.mark.asyncio
async def test_agents_delegate_to_injected_run_impl(tmp_path) -> None:
    """注入 run_impl 时委托真实逻辑并返回其结果，不写缺省 payload。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    called: list[str] = []

    async def fake_impl(ctx) -> AgentOutcome:
        called.append(ctx.input_text or "")
        return AgentOutcome(result_ref="result://real")

    agent = IdeatorAgent(store, run_impl=fake_impl)
    rt = _runtime(agent, "ideator", tmp_path)
    _, run_id = await rt.create_root("ideator", {"content": "任务"})
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert called == ["任务"]  # 真实实现被调用
    assert await _result_ref(summary) == "result://real"
    await rt.aclose()


@pytest.mark.asyncio
async def test_plot_agent_writes_image_and_payload(tmp_path) -> None:
    """按请求生成占位图字节，返回 image_ref + caption + observations。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = PlotAgent(store)
    rt = _runtime(agent, "plot", tmp_path)
    _, run_id = await rt.create_root(
        "plot", request_payload({"figure": "hist", "caption": "分布图"})
    )
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    payload = json.loads(await store.get_text(await _result_ref(summary)))
    assert payload["caption"] == "分布图"
    assert payload["observations"] == ["figure: hist"]
    assert await store.get_bytes(payload["image_ref"]) == b"hist"
    await rt.aclose()
