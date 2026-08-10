"""DataAgent 业务 Agent 集成测试：内层 LLM agent 写 analysis.py → 收集 → 提交。

DataAgent 用 ``inner_builder`` 构造内层 LLM ReAct Agent（prompt=data_agent.md +
通用工具），fake provider 让内层直接收尾；测试预写 workspace（``analysis.py`` +
``report.md`` + ``figures/*.png``），验证编排：v1→v2 所有权链、提交 Bundle 内容、
``analysis.py`` 落盘。不依赖真实 LLM API（Task 10 补 @pytest.mark.slow 集成测试）。
"""

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.data_agent import ANALYSIS_ENTRYPOINT, DataAgent
from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentContext
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import DirectoryBundle, VersionedBundle
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.memory.context_manager import ContextManager

from test.unit._support import FakeProvider
from ._support import JsonCodec, request_payload


def _inner_builder(agent_type, *, model, client, workspace):
    """返回带 fake provider 的内层 Agent；文件由测试预写，不在构建器内创建。"""
    return Agent(
        FakeProvider(), generic_tool_registry(workspace), load_prompt(agent_type)
    )


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "data.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        path, index=False
    )
    return path


def _seed_workspace(workspace: Path, *, report: str = "分析报告") -> Path:
    """预写 LLM 产物的 workspace：analysis.py + report.md + figures/*.png。"""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "# llm-driven analysis.py\n", encoding="utf-8"
    )
    (workspace / "report.md").write_text(report, encoding="utf-8")
    figures = workspace / "figures"
    figures.mkdir(exist_ok=True)
    (figures / "plot.png").write_bytes(b"fake-png")
    return workspace


def _request(data_path: str, workspace: Path) -> dict:
    return request_payload(
        {"data_path": data_path, "target": "label", "workspace": str(workspace)}
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


async def _noop_emit(kind: str, ref: str, data: dict | None = None) -> None:
    pass


def _direct_ctx(input_text: str) -> AgentContext:
    return AgentContext(
        thread=AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="c1"
        ),
        turn=AthenaTurn(
            turn_id="t1-turn", thread_id="t1", request_ref="c1", status="running"
        ),
        emit=_noop_emit,
        tools=ToolRegistry(),
        cancel=asyncio.Event(),
        memory=ContextManager(),
        input_text=input_text,
    )


@pytest.mark.asyncio
async def test_data_agent_llm_driven_commits_v1_then_v2(tmp_path) -> None:
    """预写 LLM 产物 workspace → v1 create + v2 commit（owner 链），产物完整。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = _seed_workspace(tmp_path / "workspace")
    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )
    rt = _runtime(agent, tmp_path)

    agent_id, run1 = await rt.create_root("data", _request(str(data_path), workspace))
    summary = await rt.wait_run(run1, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert (workspace / ANALYSIS_ENTRYPOINT).is_file()  # LLM 写入的入口脚本落盘
    analysis_id = agent.analysis_id
    v1 = agent.latest_ref
    assert analysis_id is not None and v1 is not None
    assert bundle.owner(analysis_id) == "agent_1"
    assert bundle.latest(analysis_id) == v1

    # 同 owner follow-up → v2，parent_ref 指向 v1
    run2 = await rt.followup(agent_id, _request(str(data_path), workspace))
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
async def test_data_agent_role_turn_commits_only_validated_proposal(tmp_path) -> None:
    """角色识别回合只提交 proposal，不要求 EDA 报告或图表。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    proposal_json = json.dumps(
        {
            "role_proposal": "data.csv is the training dataset",
            "data_files": ["data.csv"],
            "target_column": "label",
            "reasoning": "The target column is present in the file.",
        }
    )
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path('run_count.txt').write_text('1')\n"
        "Path('received_data_path.txt').write_text(sys.argv[1])\n"
        f"Path('dataset_role_proposal.json').write_text({proposal_json!r})\n",
        encoding="utf-8",
    )
    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )

    outcome = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "role",
                    "data_path": str(data_path),
                    "workspace": str(workspace),
                }
            )
        )
    )

    proposal = json.loads(await store.get_text(outcome.result_ref))
    assert proposal["data_files"] == ["data.csv"]
    assert (workspace / "run_count.txt").read_text() == "1"
    assert (workspace / "received_data_path.txt").read_text() == str(data_path)
    assert bundle.chains() == {}


@pytest.mark.asyncio
async def test_data_agent_role_turn_rejects_invalid_proposal(tmp_path) -> None:
    """角色提议缺少合同必填字段时不提交 Artifact。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "dataset_role_proposal.json").write_text(
        json.dumps(
            {
                "role_proposal": "data.csv is the training dataset",
                "data_files": ["data.csv"],
                "target_column": "label",
            }
        ),
        encoding="utf-8",
    )
    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )

    with pytest.raises(ValueError, match="reasoning"):
        await agent.run(
            _direct_ctx(
                json.dumps(
                    {
                        "kind": "role",
                        "data_path": str(data_path),
                        "workspace": str(workspace),
                    }
                )
            )
        )
    assert bundle.chains() == {}


@pytest.mark.asyncio
async def test_data_agent_eda_runs_generated_script_once(tmp_path) -> None:
    """EDA 回合由外层执行 LLM 生成脚本一次，再收集报告与图。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "from pathlib import Path\n"
        "count = Path('run_count.txt')\n"
        "count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')\n"
        "Path('report.md').write_text('# EDA Report', encoding='utf-8')\n"
        "Path('figures').mkdir(exist_ok=True)\n"
        "Path('figures/plot.png').write_bytes(b'png')\n",
        encoding="utf-8",
    )
    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )

    outcome = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "eda",
                    "data_path": str(data_path),
                    "workspace": str(workspace),
                }
            )
        )
    )

    assert outcome.result_ref == agent.latest_ref
    assert (workspace / "run_count.txt").read_text() == "1"


@pytest.mark.asyncio
async def test_data_agent_no_report_does_not_commit(tmp_path) -> None:
    """内层未产出 report.md → 收集失败，DataAgent 报错且不提交任何版本。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / ANALYSIS_ENTRYPOINT).write_text("print('x')\n", encoding="utf-8")
    # 无 report.md / figures → _collect_files 抛错

    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )
    request = json.dumps(
        {"data_path": str(data_path), "target": "label", "workspace": str(workspace)},
        ensure_ascii=False,
    )
    with pytest.raises(RuntimeError, match="report.md"):
        await agent.run(_direct_ctx(request))
    assert bundle.chains() == {}  # 未产生任何版本
