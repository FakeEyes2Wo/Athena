"""DataAnalysis 评审闭环测试（设计 §7.3）：DataAgent 提交 v1 → Reflection 只读评审。"""

import json
from pathlib import Path
from types import SimpleNamespace

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

from ._support import JsonCodec, request_payload


class FakeRuntime:
    """替代真实子进程：生成 report.md + figures/*.png（评审环测试关注提交链）。"""

    async def run(self, request) -> SimpleNamespace:
        workspace = request.cwd
        figures = workspace / "figures"
        figures.mkdir(exist_ok=True)
        (figures / "plot.png").write_bytes(b"fake-png")
        (workspace / "report.md").write_text("分析报告", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "dataset.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        path, index=False
    )
    return path


def _request(data_path: str, *, report: str | None = None) -> dict:
    payload = {"data_path": data_path, "target": "label"}
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
        store, bundle, owner_agent_id="agent_1", runtime=FakeRuntime()
    )
    reflection = ReflectionAgent(store)
    rt = _runtime(tmp_path, data_agent, reflection)

    # DataAgent 写脚本并提交 v1
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

    # review Bundle 结构正确，score 绑定 rubric_version
    review_files = await DirectoryBundle.files(store, review_ref)
    assert set(review_files) == {"rubric.json", "score.json", "review.md"}
    score = json.loads(await store.get_text(review_files["score.json"]))
    assert score["rubric_version"] == 1
    assert score["scores"]["report_nonempty"] == 1

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
        store, bundle, owner_agent_id="agent_1", runtime=FakeRuntime()
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
    score1 = json.loads(
        await store.get_text(
            (await DirectoryBundle.files(store, review1))["score.json"]
        )
    )
    assert score1["scores"]["report_nonempty"] == 0  # failed

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
    score2 = json.loads(
        await store.get_text(
            (await DirectoryBundle.files(store, review2))["score.json"]
        )
    )
    assert score2["scores"]["report_nonempty"] == 1  # passed

    # 旧版本保留，lineage 正确
    assert bundle.latest(analysis_id) == v2
    assert v1 != v2
    await rt.aclose()
