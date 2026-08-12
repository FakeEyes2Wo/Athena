"""DataAnalysis 评审闭环测试（设计 §7.3）：DataAgent 提交 v1 → Reflection 只读评审。

Reflection 输出结构化 decision（ACCEPT/REVISE）扁平 JSON，与 Supervisor 评审闭环
（supervisor_imp_docs Task 5）同合同。
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.data_agent import DataAgent
from athena.agents.reflection_agent import ReflectionAgent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import DirectoryBundle, VersionedBundle

from test.unit._support import fake_inner_builder
from ._support import JsonCodec, request_payload


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "dataset.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        path, index=False
    )
    return path


def _request(data_path: str, *, report: str | None = None) -> dict:
    payload = {
        "data_path": data_path,
        "target": "label",
        "workspace": str(
            Path(data_path).parent / ".athena" / "workspaces" / "data-agent"
        ),
    }
    if report is not None:
        payload["report"] = report
    return request_payload(payload)


def _runtime(tmp_path, data_agent, reflection) -> AgentRuntime:
    registry = AgentTypeRegistry()
    registry.register(
        "data",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(data_agent), codec=JsonCodec()
        ),
    )
    registry.register(
        "reflection",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(reflection), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


@pytest.mark.asyncio
async def test_data_analysis_review_loop(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=fake_inner_builder,
    )
    reflection = ReflectionAgent(store)
    rt = _runtime(tmp_path, data_agent, reflection)

    # DataAgent 驱动内层 LLM（fake）产出报告并提交 v1
    data_id, run1 = await rt.create_root(
        "data", _request(str(_dataset(tmp_path))), name="data-root"
    )
    summary = await rt.wait_run(run1, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    v1 = data_agent.latest_ref
    assert v1 is not None
    analysis_id = data_agent.analysis_id
    assert analysis_id is not None

    # ReflectionAgent 只读评审 v1
    _, run2 = await rt.create_root(
        "reflection", request_payload({"data_analysis_ref": v1}), name="reflection-root"
    )
    summary = await rt.wait_run(run2, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    response = JsonCodec().decode_response(summary.response_ref)
    review_ref = response["result_ref"]

    # review 输出结构化 decision 扁平 JSON（与 Supervisor 评审闭环同合同）
    payload = json.loads(await store.get_text(review_ref))
    assert payload["decision"] == "ACCEPT"
    assert payload["issues"] == []
    assert payload["required_changes"] == []
    assert isinstance(payload["evidence_refs"], list)

    # Reflection 未修改 DataAnalysis：v1 仍是 latest，report 内容不变（脚本产出）
    assert bundle.latest(analysis_id) == v1
    da_files = await DirectoryBundle.files(store, v1)
    assert (await store.get_text(da_files["report.md"])).strip() == "分析报告"
    assert any(key.startswith("figures/") for key in da_files)
    await rt.aclose()


@pytest.mark.asyncio
async def test_revision_loop_failed_then_revised(tmp_path) -> None:
    """设计 §7.3：v1 评审 failed → follow-up 原 DataAgent 提交 v2 → 复审 passed。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=fake_inner_builder,
    )
    reflection = ReflectionAgent(store)
    dataset = str(_dataset(tmp_path))
    rt = _runtime(tmp_path, data_agent, reflection)

    # v1：report 覆盖为空 → Reflection 判 failed
    data_id, run1 = await rt.create_root(
        "data", _request(dataset, report=""), name="data-root"
    )
    await rt.wait_run(run1, timeout=5)
    v1 = data_agent.latest_ref
    assert v1 is not None
    analysis_id = data_agent.analysis_id

    _, run2 = await rt.create_root(
        "reflection", request_payload({"data_analysis_ref": v1}), name="reflection-root"
    )
    await rt.wait_run(run2, timeout=5)
    review1 = JsonCodec().decode_response(rt.run_summary(run2).response_ref)[
        "result_ref"
    ]
    payload1 = json.loads(await store.get_text(review1))
    assert payload1["decision"] == "REVISE"
    assert "report 为空或缺失" in payload1["issues"]

    # Supervisor 决定返工 → follow-up 原 DataAgent（同 owner 提交 v2，无空覆盖）
    run3 = await rt.followup(data_id, _request(dataset))
    await rt.wait_run(run3, timeout=5)
    v2 = data_agent.latest_ref
    assert v2 is not None and v2 != v1
    assert bundle.latest(analysis_id) == v2
    assert bundle.owner(analysis_id) == "agent_1"  # v2 由同一 DataAgent 提交

    # 复审 v2 → passed
    _, run4 = await rt.create_root(
        "reflection",
        request_payload({"data_analysis_ref": v2}),
        name="reflection-root-2",
    )
    await rt.wait_run(run4, timeout=5)
    review2 = JsonCodec().decode_response(rt.run_summary(run4).response_ref)[
        "result_ref"
    ]
    payload2 = json.loads(await store.get_text(review2))
    assert payload2["decision"] == "ACCEPT"  # passed
    assert payload2["issues"] == []

    # 旧版本保留，lineage 正确
    assert bundle.latest(analysis_id) == v2
    assert v1 != v2
    await rt.aclose()


@pytest.mark.asyncio
async def test_revise_followup_recovers_task_and_workspace(tmp_path) -> None:
    """REVISE follow-up（仅评审原因，无 data_path）沿用首次任务并复用 workspace 重跑。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=fake_inner_builder,
    )
    dataset = str(_dataset(tmp_path))
    rt = _runtime(tmp_path, data_agent, None)

    # 首次运行（EDA kind）：产 report + figures + proposal，提交 v1
    data_id, run1 = await rt.create_root("data", _request(dataset), name="data-root")
    await rt.wait_run(run1, timeout=5)
    v1 = data_agent.latest_ref
    assert v1 is not None
    ws1 = data_agent._workspace
    assert ws1 is not None and ws1.is_dir()

    # Supervisor REVISE：follow-up 只带评审原因（supervisor _followup_agent 的 payload）
    run2 = await rt.followup(
        data_id,
        {"content": "role proposal 未通过评审：target 缺失", "context_refs": []},
    )
    summary = await rt.wait_run(run2, timeout=5)
    assert summary.status == RunStatus.COMPLETED, summary.error
    v2 = data_agent.latest_ref
    assert v2 is not None and v2 != v1  # 沿用任务重跑，提交 v2
    assert data_agent._workspace == ws1  # 复用同一 workspace（LLM 可读修自己的脚本）
    await rt.aclose()
