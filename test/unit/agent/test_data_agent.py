"""DataAgent 确定性业务 Agent 集成测试（设计 §7.3、data-analysis-agent-workflow §5）。

DataAgent 生成固定名 ``analysis.py`` 并运行，收集 ``report.md`` 与
``figures/*.png`` 提交 DataAnalysis 版本；不再暴露 DataTools。
"""

import json
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.data_agent import (
    ANALYSIS_CONFIG,
    ANALYSIS_ENTRYPOINT,
    DEFAULT_ANALYSIS_SCRIPT,
    DataAgent,
)
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import AgentSpec, RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import DirectoryBundle, VersionedBundle

from ._support import JsonCodec, request_payload


class FakeRuntime:
    """替代真实子进程：校验脚本/配置已写入，再模拟产出 report.md + 图片。"""

    def __init__(self) -> None:
        self.requests: list = []
        self.returncode = 0

    async def run(self, request) -> SimpleNamespace:
        self.requests.append(request)
        workspace = request.cwd
        assert (workspace / ANALYSIS_ENTRYPOINT).is_file()  # 已写分析脚本
        assert (workspace / ANALYSIS_CONFIG).is_file()  # 已写运行配置
        figures = workspace / "figures"
        figures.mkdir(exist_ok=True)
        (figures / "plot.png").write_bytes(b"fake-png")
        (workspace / "report.md").write_text("分析报告", encoding="utf-8")
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "data.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        path, index=False
    )
    return path


def _request(data_path: str, target: str, workspace: str) -> dict:
    return request_payload(
        {"data_path": data_path, "target": target, "workspace": workspace}
    )


def _runtime(agent: DataAgent, tmp_path) -> AgentRuntime:
    registry = AgentTypeRegistry()
    registry.register(
        "data",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(agent), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return rt


@pytest.mark.asyncio
async def test_data_agent_writes_script_and_commits_v1_then_v2(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    runtime = FakeRuntime()
    agent = DataAgent(store, bundle, owner_agent_id="agent_1", runtime=runtime)
    rt = _runtime(agent, tmp_path)

    agent_id, run1 = await rt.create_root(
        "data", _request(str(data_path), "label", str(workspace))
    )
    summary = await rt.wait_run(run1, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert (workspace / ANALYSIS_ENTRYPOINT).read_text(
        encoding="utf-8"
    ) == DEFAULT_ANALYSIS_SCRIPT  # 生成默认 EDA+plot 脚本
    analysis_id = agent.analysis_id
    v1 = agent.latest_ref
    assert analysis_id is not None and v1 is not None
    assert bundle.owner(analysis_id) == "agent_1"
    assert bundle.latest(analysis_id) == v1

    # 同 owner follow-up → v2，parent_ref 指向 v1
    run2 = await rt.followup(
        agent_id, _request(str(data_path), "label", str(workspace))
    )
    summary = await rt.wait_run(run2, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    v2 = agent.latest_ref
    assert v2 is not None and v2 != v1
    assert bundle.latest(analysis_id) == v2

    # 提交的 Manifest 可读：report.md + figures/ 图片，v2 的 parent_ref 指向 v1
    files = await DirectoryBundle.files(store, v2)
    assert "report.md" in files
    assert any(key.startswith("figures/") for key in files)
    manifest = json.loads(await store.get_text(v2))
    assert manifest["parent_ref"] == v1
    await rt.aclose()


@pytest.mark.asyncio
async def test_data_agent_script_failure_surfaces_error(tmp_path) -> None:
    """脚本返回非零 → DataAgent 报错，不提交版本。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"

    class FailingRuntime:
        async def run(self, request) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom: bad data")

    agent = DataAgent(store, bundle, owner_agent_id="agent_1", runtime=FailingRuntime())
    rt = _runtime(agent, tmp_path)
    agent_id, run1 = await rt.create_root(
        "data", _request(str(data_path), "label", str(workspace))
    )
    summary = await rt.wait_run(run1, timeout=5)
    assert summary.status == RunStatus.FAILED
    assert bundle.chains() == {}  # 未产生任何版本
    await rt.aclose()
