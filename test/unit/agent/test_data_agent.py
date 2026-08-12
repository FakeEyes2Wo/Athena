"""DataAgent 业务 Agent 集成测试：内层 LLM agent 写 analysis.py → 收集 → 提交。

DataAgent 用 ``inner_builder`` 构造内层 LLM ReAct Agent（prompt=data_agent.md +
通用工具），fake provider 让内层直接收尾；测试预写 workspace（``analysis.py`` +
``report.md`` + ``figures/*.png``），验证编排：v1→v2 所有权链、提交 Bundle 内容、
``analysis.py`` 落盘。不依赖真实 LLM API（Task 10 补 @pytest.mark.slow 集成测试）。
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic_ai.messages import ModelRequest, SystemPromptPart

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.data_agent import (
    ANALYSIS_ENTRYPOINT,
    DataAgent,
    _validated_workspace,
)
from athena.agents.prompt_agent import load_prompt
from athena.agents.tools.generic_tools import generic_tool_registry
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentConfig, AgentContext, AgentOutcome
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import DirectoryBundle, VersionedBundle
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.memory.context_manager import ContextManager
from athena.research.contracts import EDAAttemptOutcome, EDARepairFailure

from test.unit._support import FakeProvider
from ._support import JsonCodec, request_payload


def _inner_builder(agent_type, *, model, client, workspace, runtime=None):
    """返回带 fake provider 的内层 Agent；文件由测试预写，不在构建器内创建。"""
    return Agent(
        FakeProvider(), generic_tool_registry(workspace), load_prompt(agent_type)
    )


def test_data_agent_rejects_workspace_outside_project_root(tmp_path) -> None:
    """workspace 落在 project_root 之外 → 拒绝，不允许逃逸到系统 temp。"""
    runtime = SimpleNamespace(project_root=str(tmp_path / "project"))
    with pytest.raises(ValueError, match="outside project root"):
        _validated_workspace(str(tmp_path / "outside"), runtime)


def test_workspace_python_prefers_local_venv(tmp_path) -> None:
    """canonical 校验解释器优先 workspace-local .venv（Validation 10），缺省回退当前解释器。"""
    import sys

    from athena.agents.data_agent import _workspace_python

    posix = tmp_path / "ws-posix"
    (posix / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (posix / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    assert _workspace_python(posix) == str(posix / ".venv" / "bin" / "python")

    windows = tmp_path / "ws-win"
    (windows / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    (windows / ".venv" / "Scripts" / "python.exe").write_text("x", encoding="utf-8")
    assert _workspace_python(windows) == str(
        windows / ".venv" / "Scripts" / "python.exe"
    )

    plain = tmp_path / "ws-plain"
    assert _workspace_python(plain) == sys.executable


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

    # 提交的 Manifest 可读：analysis.py + report.md + figures/ 图片，v2 parent_ref 指向 v1
    files = await DirectoryBundle.files(store, v2)
    assert "analysis.py" in files  # EDA 可复现：入口脚本入包
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
    (workspace / ANALYSIS_ENTRYPOINT).write_text("pass\n", encoding="utf-8")
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
async def test_data_agent_role_turn_normalizes_malformed_proposal(tmp_path) -> None:
    """LLM 产出错误形状（role_proposal 为映射、data_files 为对象列表）→ 归一化为合同。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    malformed = json.dumps(
        {
            "role_proposal": {"train.csv": "training", "test.csv": "test"},
            "data_files": [
                {"path": "train.csv", "role": "training"},
                {"path": "test.csv", "role": "test"},
            ],
            "target_column": "Survived",
            "reasoning": "test.csv columns are a strict subset of train.csv minus the target.",
        }
    )
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"Path('dataset_role_proposal.json').write_text({malformed!r})\n",
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
    assert isinstance(proposal["role_proposal"], str)
    assert "train.csv: training" in proposal["role_proposal"]
    assert proposal["data_files"] == ["train.csv", "test.csv"]
    assert proposal["target_column"] == "Survived"
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

    attempt = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert attempt.status == "succeeded"
    assert attempt.bundle_ref == agent.latest_ref
    assert (workspace / "run_count.txt").read_text() == "1"


@pytest.mark.asyncio
async def test_data_agent_repair_followup_fixes_script(tmp_path) -> None:
    """Supervisor 驱动修复：初始 repairable_failure → failure_ref 修复 follow-up → succeeded。

    每 turn 一次 canonical 校验（无内部修复循环）；修复 prompt 含上轮失败详情。
    """
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class RepairingInner:
        def __init__(self) -> None:
            self.tools = ToolRegistry()
            self.config = AgentConfig()
            self.inputs: list[str] = []

        async def run(self, ctx: AgentContext) -> AgentOutcome:
            self.inputs.append(ctx.input_text or "")
            if len(self.inputs) == 1:
                # 观测到的 Matplotlib labels 失败（Validation 11）：错误的 tick_labels 属性访问
                (workspace / ANALYSIS_ENTRYPOINT).write_text(
                    "raise AttributeError(\"'Axes' object has no attribute 'tick_labels'\")\n",
                    encoding="utf-8",
                )
            else:
                # 修复：改用 set_xticks / tick_params（stdlib-only 收尾，避免测试依赖 matplotlib）
                (workspace / ANALYSIS_ENTRYPOINT).write_text(
                    "from pathlib import Path\n"
                    "Path('report.md').write_text('# EDA Report')\n"
                    "Path('figures').mkdir(exist_ok=True)\n"
                    "Path('figures/plot.png').write_bytes(b'png')\n",
                    encoding="utf-8",
                )
            return AgentOutcome(result_ref="inner://done")

    inner = RepairingInner()
    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=lambda *_args, **_kwargs: inner,
    )

    # turn 1：初始生成失败 → repairable_failure + failure_ref
    attempt1 = await agent.run(
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
    result1 = EDAAttemptOutcome.model_validate_json(
        await store.get_text(attempt1.result_ref)
    )
    assert result1.status == "repairable_failure"
    assert result1.bundle_ref is None
    failure = EDARepairFailure.model_validate_json(
        await store.get_text(result1.failure_ref)
    )
    assert "tick_labels" in failure.stderr
    assert result1.failure_signature == failure.failure_signature

    # turn 2：修复 follow-up（带 failure_ref）→ 内层改脚本 → succeeded（同一链 v1）
    attempt2 = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "eda",
                    "data_path": str(data_path),
                    "workspace": str(workspace),
                    "failure_ref": result1.failure_ref,
                    "repair_count": 1,
                }
            )
        )
    )
    result2 = EDAAttemptOutcome.model_validate_json(
        await store.get_text(attempt2.result_ref)
    )
    assert result2.status == "succeeded"
    assert result2.repair_count == 1
    assert result2.bundle_ref is not None
    assert bundle.latest(agent.analysis_id) == result2.bundle_ref
    assert "tick_labels" in inner.inputs[1]  # 修复 prompt 含上轮失败详情
    assert "write_file tool replaces the entire file" in inner.inputs[1]
    assert inner.config.max_tokens == 8192


@pytest.mark.asyncio
async def test_data_agent_missing_script_returns_repairable_failure(tmp_path) -> None:
    """A missing initial output crosses the Supervisor boundary as a repair Plan."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class NoopInner:
        def __init__(self) -> None:
            self.tools = ToolRegistry()
            self.calls = 0

        async def run(self, ctx: AgentContext) -> AgentOutcome:
            self.calls += 1
            return AgentOutcome(result_ref="inner://done")

    inner = NoopInner()
    agent = DataAgent(
        store,
        VersionedBundle(store),
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=lambda *_args, **_kwargs: inner,
    )

    outcome = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "eda",
                    "execution_id": "exec_1",
                    "data_path": str(_dataset(tmp_path)),
                    "workspace": str(workspace),
                }
            )
        )
    )

    attempt = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    failure = EDARepairFailure.model_validate_json(
        await store.get_text(attempt.failure_ref)
    )
    assert inner.calls == 1
    assert attempt.status == "repairable_failure"
    assert attempt.execution_id == "exec_1"
    assert "analysis.py" in failure.stderr


@pytest.mark.asyncio
async def test_data_agent_cancellation_does_not_create_missing_script_failure(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = _direct_ctx(
        json.dumps(
            {
                "kind": "eda",
                "execution_id": "exec_1",
                "data_path": str(_dataset(tmp_path)),
                "workspace": str(workspace),
            }
        )
    )

    class CancellingInner:
        tools = ToolRegistry()

        async def run(self, inner_ctx: AgentContext) -> AgentOutcome:
            inner_ctx.cancel.set()
            return AgentOutcome(result_ref="inner://cancelled")

    agent = DataAgent(
        store,
        VersionedBundle(store),
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=lambda *_args, **_kwargs: CancellingInner(),
    )

    with pytest.raises(asyncio.CancelledError):
        await agent.run(ctx)


@pytest.mark.asyncio
async def test_data_agent_missing_script_loops_inside_repair_plan(tmp_path) -> None:
    """Once in a repair Plan, keep prompting until analysis.py is present."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class DelayedWriterInner:
        def __init__(self) -> None:
            self.tools = ToolRegistry()
            self.config = AgentConfig()
            self.inputs: list[str] = []

        async def run(self, ctx: AgentContext) -> AgentOutcome:
            self.inputs.append(ctx.input_text or "")
            if len(self.inputs) == 2:
                (workspace / ANALYSIS_ENTRYPOINT).write_text(
                    "from pathlib import Path\n"
                    "Path('report.md').write_text('# EDA Report')\n"
                    "Path('figures').mkdir(exist_ok=True)\n"
                    "Path('figures/plot.png').write_bytes(b'png')\n",
                    encoding="utf-8",
                )
            return AgentOutcome(result_ref="inner://done")

    failure = EDARepairFailure(
        attempt=0,
        command=[sys.executable, ANALYSIS_ENTRYPOINT],
        exit_code=None,
        stderr="DataAgent did not produce analysis.py",
        failure_signature="missing-analysis",
    )
    failure_ref = await store.put_text(failure.model_dump_json())
    inner = DelayedWriterInner()
    agent = DataAgent(
        store,
        VersionedBundle(store),
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=lambda *_args, **_kwargs: inner,
    )

    outcome = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "eda",
                    "execution_id": "exec_1",
                    "data_path": str(_dataset(tmp_path)),
                    "workspace": str(workspace),
                    "failure_ref": failure_ref,
                    "repair_count": 1,
                }
            )
        )
    )

    attempt = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert attempt.status == "succeeded"
    assert len(inner.inputs) == 2
    assert "Missing files: analysis.py" in inner.inputs[1]
    assert "Call write_file now" in inner.inputs[1]
    assert inner.config.max_turns == 1
    assert inner.config.max_tokens == 8192
    assert inner.config.tool_choice == "required"


@pytest.mark.asyncio
async def test_data_agent_role_script_failure_is_repairable(tmp_path) -> None:
    """Role discovery uses the same Supervisor-visible repair contract as EDA."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "raise RuntimeError('ambiguous file roles')\n", encoding="utf-8"
    )
    agent = DataAgent(
        store,
        VersionedBundle(store),
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )

    outcome = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "role",
                    "execution_id": "exec_1",
                    "data_path": str(_dataset(tmp_path)),
                    "workspace": str(workspace),
                }
            )
        )
    )

    attempt = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert attempt.status == "repairable_failure"
    assert attempt.execution_id == "exec_1"
    failure = EDARepairFailure.model_validate_json(
        await store.get_text(attempt.failure_ref)
    )
    assert "ambiguous file roles" in failure.stderr


@pytest.mark.asyncio
async def test_data_agent_requires_an_assigned_workspace(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    agent = DataAgent(
        store,
        VersionedBundle(store),
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=_inner_builder,
    )

    with pytest.raises(ValueError, match="workspace"):
        await agent.run(_direct_ctx(json.dumps({"data_path": str(_dataset(tmp_path))})))


@pytest.mark.asyncio
async def test_data_agent_single_validation_returns_repairable_failure(
    tmp_path,
) -> None:
    """单次 canonical 校验失败 → repairable_failure（每 turn 一次；修复由 Supervisor 驱动）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "from pathlib import Path\n"
        "count = Path('run_count.txt')\n"
        "count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')\n"
        "raise RuntimeError('still broken')\n",
        encoding="utf-8",
    )

    class NoopInner:
        def __init__(self) -> None:
            self.tools = ToolRegistry()
            self.inputs: list[str] = []

        async def run(self, ctx: AgentContext) -> AgentOutcome:
            self.inputs.append(ctx.input_text or "")
            return AgentOutcome(result_ref="inner://done")

    inner = NoopInner()
    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_1",
        model="fake",
        inner_builder=lambda *_args, **_kwargs: inner,
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

    assert (workspace / "run_count.txt").read_text() == "1"  # 只跑一次
    assert len(inner.inputs) == 1  # 无内部修复循环
    attempt = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert attempt.status == "repairable_failure"
    assert attempt.bundle_ref is None
    failure = EDARepairFailure.model_validate_json(
        await store.get_text(attempt.failure_ref)
    )
    assert failure.exit_code != 0
    assert "still broken" in failure.stderr
    assert attempt.failure_signature == failure.failure_signature
    assert bundle.chains() == {}  # 失败不提交任何版本


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


# ---- Memory Flow Fixes：DataAgent 状态恢复（memory-flow-fixes-design §DataAgent State Recovery）----


@pytest.mark.asyncio
async def test_data_agent_restores_state_after_restart(tmp_path) -> None:
    """跨重启：新 DataAgent 从 rollout 重放的 memory 恢复 task/workspace/analysis_id/latest_ref。

    重启后纯文本修订 follow-up（无 data_path/workspace）能复用同一所有权链提交 v2，
    证明缺失字段由 v1 marker 恢复（而非新建链/新建 workspace）。
    """
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = _seed_workspace(tmp_path / "workspace")
    rollout_dir = tmp_path / "sessions"

    def _spec(aid: str, _cfg=None) -> AgentSpec:
        agent = DataAgent(
            store, bundle, aid, model="fake", inner_builder=_inner_builder
        )
        return AgentSpec(runner=BaseAgentRunner(agent), codec=JsonCodec())

    registry = AgentTypeRegistry()
    registry.register("data", _spec)

    rt1 = AgentRuntime(
        type_registry=registry, project_root=tmp_path, rollout_dir=rollout_dir
    )
    rt1.start()
    agent_id, run1 = await rt1.create_root("data", _request(str(data_path), workspace))
    summary1 = await rt1.wait_run(run1, timeout=5)
    assert summary1.status == RunStatus.COMPLETED
    outcome1_ref = json.loads(summary1.response_ref)["result_ref"]
    v1 = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome1_ref)
    ).bundle_ref
    await rt1.aclose()

    # 重启：全新 runtime + 全新 DataAgent 实例，同一 rollout 目录恢复记忆
    rt2 = AgentRuntime(
        type_registry=registry, project_root=tmp_path, rollout_dir=rollout_dir
    )
    rt2.start()
    await rt2.resume_agent(agent_id, agent_type="data")
    run2 = await rt2.followup(agent_id, {"content": "revise the report"})
    summary2 = await rt2.wait_run(run2, timeout=5)
    assert summary2.status == RunStatus.COMPLETED

    chains = bundle.chains()
    assert len(chains) == 1  # 恢复 analysis_id → 同一链提交 v2（而非新建链）
    _analysis_id, v2 = next(iter(chains.items()))
    assert v2 is not None and v2 != v1
    await rt2.aclose()


@pytest.mark.asyncio
async def test_data_agent_ignores_malformed_state_marker(tmp_path) -> None:
    """损坏的 v1 marker 不阻断含完整任务的正常请求运行。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "from pathlib import Path\n"
        "Path('report.md').write_text('# EDA Report')\n"
        "Path('figures').mkdir(exist_ok=True)\n"
        "Path('figures/plot.png').write_bytes(b'png')\n",
        encoding="utf-8",
    )

    bad_memory = ContextManager()
    bad_memory.append(
        ModelRequest(
            parts=[SystemPromptPart(content="[ATHENA DATA AGENT STATE v1]\n{not-json")]
        )
    )
    bad_ctx = _direct_ctx(
        json.dumps(
            {
                "data_path": str(data_path),
                "target": "label",
                "workspace": str(workspace),
            },
            ensure_ascii=False,
        )
    )
    bad_ctx.memory = bad_memory

    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_2",
        model="fake",
        inner_builder=_inner_builder,
    )
    outcome = await agent.run(bad_ctx)
    assert outcome.result_ref  # 正常提交，未被损坏 marker 阻断


@pytest.mark.slow
@pytest.mark.asyncio
async def test_data_agent_real_llm_repairs_tick_labels(tmp_path) -> None:
    """真实 LLM 端到端修复 Matplotlib tick_labels 失败（Validation 11 完整版，用 .env 凭据）。

    初始脚本预写为确定性失败（错误的 ``ax.tick_labels`` 属性）；把真实 stderr 固化为
    EDARepairFailure 交给修复 follow-up，真实 DeepSeek 读修复 prompt 改脚本 → canonical 通过。
    无 API key 时 skip（.env 缺失/CI 无凭据）。
    """
    from athena.agents.data_agent import (
        _MAX_FAILURE_STDERR_CHARS,
        _repair_signature,
        _run_script,
    )
    from athena.core.agent import settings as llm_settings

    if not llm_settings.api_key():
        pytest.skip("no LLM API key in .env")

    store = LocalArtifactStore(tmp_path / "artifacts")
    bundle = VersionedBundle(store)
    data_path = _dataset(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "figures").mkdir(exist_ok=True)
    (workspace / ANALYSIS_ENTRYPOINT).write_text(
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "import pandas as pd\n"
        "import sys\n"
        "df = pd.read_csv(sys.argv[1])\n"
        "fig, ax = plt.subplots()\n"
        "ax.hist(df.iloc[:, 0])\n"
        "ax.tick_labels = []  # 错误的 Matplotlib API\n"
        "fig.savefig('figures/plot.png')\n"
        "open('report.md', 'w').write('# EDA Report\n')\n",
        encoding="utf-8",
    )

    # 确定性失败：预写脚本的 canonical 校验 stderr
    returncode, _stdout, stderr = await _run_script(workspace, str(data_path), "label")
    assert returncode != 0, "预写的 buggy 脚本应确定性失败"
    failure = EDARepairFailure(
        attempt=0,
        command=[sys.executable, ANALYSIS_ENTRYPOINT, str(data_path), "label"],
        exit_code=returncode,
        stderr=stderr[:_MAX_FAILURE_STDERR_CHARS],
        failure_signature=_repair_signature(returncode, stderr),
    )
    failure_ref = await store.put_text(failure.model_dump_json())

    agent = DataAgent(
        store,
        bundle,
        owner_agent_id="agent_real",
        model=llm_settings.model_name(),  # client 惰性：ResponsesProvider 首次调用才建
    )
    outcome = await agent.run(
        _direct_ctx(
            json.dumps(
                {
                    "kind": "eda",
                    "data_path": str(data_path),
                    "workspace": str(workspace),
                    "failure_ref": failure_ref,
                    "repair_count": 1,
                }
            )
        )
    )
    attempt = EDAAttemptOutcome.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert (
        attempt.status == "succeeded"
    ), f"real-LLM repair failed: {attempt.failure_signature}"
    assert attempt.bundle_ref is not None


def _data_eda_prompt() -> str:
    """加载 data prompt 的 kind=eda 段（纯文本断言辅助）。"""
    return load_prompt("data").split('## `kind="eda"`', 1)[1]


def test_data_prompt_enforces_analysis_before_report_render() -> None:
    """EDA prompt 强制四阶段数据流：分析/绘图先于渲染，报告原子发布。"""
    eda = _data_eda_prompt()
    for phase in ("discover", "analyze", "plot", "render + publish"):
        assert phase in eda
    assert "atomically replace" in eda  # 失败不留下半成品 report.md
    assert "Never write" in eda  # 分析阶段不写 Markdown 结论


def test_data_prompt_requires_multifile_relationship_analysis() -> None:
    """EDA prompt 显式要求多文件关系分类（不依赖文件名）。"""
    eda = _data_eda_prompt()
    for relationship in (
        "schema-compatible partitions",
        "train/test pairs",
        "relational tables",
        "auxiliary files",
    ):
        assert relationship in eda


def test_data_prompt_role_contract_is_unchanged() -> None:
    """kind=role 的严格 schema-subset 合同保持不变。"""
    role = (
        load_prompt("data")
        .split('## `kind="role"`', 1)[1]
        .split('## `kind="eda"`', 1)[0]
    )
    assert "dataset_role_proposal.json" in role
    assert "strict subset" in role
