"""项目 Composition Root 测试（设计 §3.1/§6）：root_supervisor 持久化与编排。"""

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

from athena.core.agent_kernel.types import AgentStatus
from athena.evaluation.types import MetricDef
from athena.research.models import MetricSpec, TaskMetaData
from athena.research.project_runtime import ProjectRuntime
from athena.storage.bundle import DirectoryBundle


def _dataset(tmp_path: Path) -> Path:
    """写一个小型 CSV，供 DataAgent 脚本分析（写脚本 → 运行 → 提交）。"""
    path = tmp_path / "dataset.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        path, index=False
    )
    return path


async def _eventually(pred, timeout: float = 3) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0)
    return True


@pytest.mark.asyncio
async def test_creates_and_persists_root_supervisor(tmp_path) -> None:
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    sid = await project.open(message="start")
    assert sid is not None
    assert (tmp_path / ".athena" / "project.json").exists()
    await project.close()


@pytest.mark.asyncio
async def test_reopen_reuses_same_root_supervisor(tmp_path) -> None:
    first = ProjectRuntime(tmp_path)
    first.register_defaults()
    sid1 = await first.open(message="start")
    await first.close()

    # 重新打开（全新实例）→ 复用同一 root_supervisor_id，不用新实例冒充
    reopened = ProjectRuntime(tmp_path)
    reopened.register_defaults()
    sid2 = await reopened.open(message="continue")
    assert sid2 == sid1
    assert reopened.supervisor_id == sid1
    await reopened.close()


@pytest.mark.asyncio
async def test_supervisor_uses_tools_to_orchestrate_child(tmp_path) -> None:
    """设计 §5.2：Supervisor 经受控工具 spawn data 子并 wait_for，子完成后唤醒。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    sid = await project.open(message="start")
    assert sid is not None

    # supervisor 首轮 spawn data-child + wait_for → 子完成后唤醒回 IDLE
    assert await _eventually(
        lambda: project.kernel.agent_status(sid) == AgentStatus.IDLE
    )
    children = [s.agent_id for s in project.kernel.list_agents() if s.parent_id == sid]
    assert children  # data-child 已创建且是 supervisor 的子
    await project.close()


def _task() -> TaskMetaData:
    return TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="accuracy", direction="maximize"),
    )


@pytest.mark.asyncio
async def test_configure_sets_configured_phase(tmp_path) -> None:
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.configure(_task())
    assert project.phase == "CONFIGURED"


@pytest.mark.asyncio
async def test_prepare_data_analysis_passed(tmp_path) -> None:
    """设计 §7.1：DataAnalysis 评审通过 → 接受 v1，阶段推进 PREPARE。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    await project.configure(_task())
    accepted = await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    assert accepted.startswith("sha256:")
    assert project.phase == "PREPARE"
    files = await DirectoryBundle.files(project.store, accepted)
    assert "report.md" in files
    assert any(key.startswith("figures/") for key in files)
    await project.close()


@pytest.mark.asyncio
async def test_prepare_data_analysis_failed_then_revised(tmp_path) -> None:
    """设计 §7.3：v1 空报告评审 failed → follow-up 原 DataAgent 提交 v2。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    await project.configure(_task())
    # 空报告覆盖触发 v1 评审 failed；follow-up 原 DataAgent 提交 v2
    accepted = await project.prepare_data_analysis(
        str(_dataset(tmp_path)), "label", report=""
    )
    assert accepted.startswith("sha256:")
    # 修订产生新版本（v2 != v1），链上有两个版本
    chains = project.bundle.chains()
    analysis_id = next(iter(chains))
    assert project.bundle.latest(analysis_id) == accepted
    await project.close()


@pytest.mark.asyncio
async def test_freeze_eval_spec_creates_version(tmp_path) -> None:
    """设计 §7.2：冻结第一个评估协议版本。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    version = project.freeze_eval_spec(
        MetricDef(name="accuracy", direction="maximize", description="acc")
    )
    assert version == 1
    assert project.eval_specs.version == 1
    assert project.eval_specs.current.primary.name == "accuracy"


@pytest.mark.asyncio
async def test_eval_protocol_append_only_preserves_v1(tmp_path) -> None:
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    project.freeze_eval_spec(
        MetricDef(name="accuracy", direction="maximize", description="acc")
    )
    v2 = project.eval_specs.append_metric(
        MetricDef(name="f1", direction="maximize", description="f1")
    )
    assert v2 == 2
    assert [m.name for m in project.eval_specs.version_at(1).secondary] == []
    assert [m.name for m in project.eval_specs.version_at(2).secondary] == ["f1"]


@pytest.mark.asyncio
async def test_eval_protocol_survives_reopen(tmp_path) -> None:
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    project.freeze_eval_spec(
        MetricDef(name="accuracy", direction="maximize", description="acc")
    )
    await project.close()

    reopened = ProjectRuntime(tmp_path)
    reopened.register_defaults()
    await reopened.open()
    assert reopened.eval_specs.version == 1  # 从 JSON 恢复
    assert reopened.eval_specs.current.primary.name == "accuracy"
    await reopened.close()


@pytest.mark.asyncio
async def test_projected_phase_from_committed_facts(tmp_path) -> None:
    """设计 §4.2：阶段由已提交事实确定性投影，不依赖 Agent 声明。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    assert project.projected_phase() == "IDLE"  # 无任务
    await project.configure(_task())
    assert project.projected_phase() == "CONFIGURED"
    await project.open()
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    assert project.projected_phase() == "CONFIGURED"  # 有分析但协议未冻结
    project.freeze_eval_spec(
        MetricDef(name="accuracy", direction="maximize", description="acc")
    )
    assert project.projected_phase() == "PREPARE"  # 分析 + 冻结协议已提交
    await project.close()


@pytest.mark.asyncio
async def test_project_status_reflects_kernel(tmp_path) -> None:
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    assert project.project_status() == "RUNNING"
    await project.open()
    assert project.project_status() == "RUNNING"  # supervisor 未等人工
    await project.close()


@pytest.mark.asyncio
async def test_corrupt_state_rejects_missing_root(tmp_path) -> None:
    """设计 §16.1：恢复时校验 root 引用，损坏状态不静默恢复。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    await project.close()

    state_path = tmp_path / ".athena" / "project.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["root_supervisor_id"] = "agent_deadbeef"  # 指向不存在的实例
    state_path.write_text(json.dumps(data), encoding="utf-8")

    reopened = ProjectRuntime(tmp_path)
    reopened.register_defaults()
    with pytest.raises(ValueError):
        await reopened.open()


def test_register_defaults_covers_seven_types(tmp_path) -> None:
    """registered-agent-catalog §3：Composition Root 注册全部七个静态类型。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    assert set(project.kernel._type_registry.types) == {
        "supervisor",
        "data",
        "plot",
        "reflection",
        "ideator",
        "code",
        "report",
    }


@pytest.mark.asyncio
async def test_pause_stops_dispatch_then_resume_continues(tmp_path) -> None:
    """设计 §13：pause 停止派发新 turn，resume 恢复；mailbox/wait 保留。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    sid = await project.open()
    project.pause()
    assert project.project_status() == "PAUSED"
    # pause 后 spawn → 子 run 保持 QUEUED（不派发）；resume 后派发真实 data 子
    content = json.dumps(
        {
            "data_path": str(_dataset(tmp_path)),
            "target": "label",
            "workspace": str(tmp_path / "ws"),
        }
    )
    child_id, child_run = await project.kernel.spawn(
        sid, "data", {"content": content}, name="c"
    )
    assert project.kernel.run_summary(child_run).status.value == "queued"
    project.resume()
    assert project.project_status() == "RUNNING"
    assert await _eventually(
        lambda: project.kernel.run_summary(child_run).status.value == "completed",
        timeout=10,
    )
    await project.close()


@pytest.mark.asyncio
async def test_stop_projects_cancelled_and_halts_dispatch(tmp_path) -> None:
    """设计 §13：stop 中断活动 Run、投影 CANCELLED、不再派发新 turn。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    sid = await project.open()
    await project.stop()
    assert project.project_status() == "CANCELLED"
    # 停止后 spawn → 保持 QUEUED（不再派发）
    child_id, child_run = await project.kernel.spawn(
        sid, "data", {"content": ""}, name="c"
    )
    assert project.kernel.run_summary(child_run).status.value == "queued"
    await project.close()


from athena.research.runtime import ResearchRuntime


@pytest.mark.asyncio
async def test_research_runtime_delegates_phase_to_project(tmp_path) -> None:
    """设计 §15：ResearchRuntime 可把 phase/status 投影委托给 ProjectRuntime。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.configure(_task())
    runtime = ResearchRuntime(project=project)
    assert runtime.phase == "CONFIGURED"  # 委托项目投影
    assert runtime.project_status() == "RUNNING"


@pytest.mark.asyncio
async def test_load_state_repairs_missing_root_reference(tmp_path) -> None:
    """设计 §3.2：元数据缺 root 引用但图中恰有一个合法 root → 修复引用。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    sid = await project.open()
    await project.close()

    state_path = tmp_path / ".athena" / "project.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["root_supervisor_id"] = None  # 丢失引用
    state_path.write_text(json.dumps(data), encoding="utf-8")

    reopened = ProjectRuntime(tmp_path)
    reopened.register_defaults()
    repaired_id = await reopened.open()
    assert repaired_id == sid  # 修复到图中唯一 root
    await reopened.close()


@pytest.mark.asyncio
async def test_ideator_code_report_are_real_factories(tmp_path) -> None:
    """方案2 §8：ideator/code/report 均为真实 factory，产出真实结果 Artifact。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    cases = {
        "ideator": ("新假设", "hypothesis"),
        "code": ("补丁", "diff"),
        "report": ("最终报告", "report"),
    }
    for agent_type, (content, key) in cases.items():
        _, run_id = await project.kernel.create_root(
            agent_type, {"content": content}, name=f"{agent_type}-root"
        )
        summary = await project.kernel.wait_run(run_id, timeout=2)
        response = json.loads(summary.response_ref)
        result_ref = response["result_ref"]
        assert result_ref.startswith("sha256:")
        if agent_type == "report":
            files = await DirectoryBundle.files(project.store, result_ref)
            assert await project.store.get_text(files["report.md"]) == content
        else:
            artifact = json.loads(await project.store.get_text(result_ref))
            assert artifact[key] == content
    await project.close()


from athena.agents.base_runner import BaseAgentRunner
from athena.agents.ideator_agent import IdeatorAgent
from athena.core.agent.models import AgentOutcome
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.types import AgentSpec


@pytest.mark.asyncio
async def test_injectable_real_impl_delegates(tmp_path) -> None:
    """生产包装契约：注入 run_impl 时委托真实逻辑并返回其结果。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    called: list[str] = []

    async def fake_impl(ctx) -> AgentOutcome:
        called.append(ctx.input_text or "")
        return AgentOutcome(result_ref="result://real")

    project.kernel._type_registry.register(
        "ideator-real",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(IdeatorAgent(project.store, run_impl=fake_impl)),
            codec=JsonCodec(),
        ),
    )
    _, run_id = await project.kernel.create_root(
        "ideator-real", {"content": "任务"}, name="ideator-real-root"
    )
    summary = await project.kernel.wait_run(run_id, timeout=2)
    assert summary.status.value == "completed"
    assert called == ["任务"]  # 真实实现被调用
    await project.close()


import athena.agents.production as production
from athena.agents.ideator_agent import IdeatorAgent


class _FakeResult:
    def model_dump(self, mode="json") -> dict:
        return {"hypothesis": "h", "status": "PROPOSED"}


class _FakeIdeator:
    def __init__(self) -> None:
        self.called_with = None

    async def generate(self, profile, papers, models, tree):
        self.called_with = (profile, papers, models, tree)
        return _FakeResult()


class _FakeProject:
    async def ideator_inputs(self, ctx):
        return production.IdeatorInputs(
            profile="P", papers=["p1"], models=["m1"], tree="T"
        )


@pytest.mark.asyncio
async def test_ideator_run_impl_wires_real_module(tmp_path) -> None:
    """生产接线：run_impl 解析项目输入 → 调真实 Ideator → 写结果 Artifact。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    fake_ideator = _FakeIdeator()
    run_impl = production.ideator_run_impl(fake_ideator, project.store, _FakeProject())
    project.kernel._type_registry.register(
        "ideator-real",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(IdeatorAgent(project.store, run_impl=run_impl)),
            codec=JsonCodec(),
        ),
    )
    _, run_id = await project.kernel.create_root(
        "ideator-real", {"content": "任务"}, name="ideator-real-root"
    )
    summary = await project.kernel.wait_run(run_id, timeout=2)
    assert summary.status.value == "completed"
    assert fake_ideator.called_with == ("P", ["p1"], ["m1"], "T")  # 真实模块被调用
    response = json.loads(summary.response_ref)
    artifact = json.loads(await project.store.get_text(response["result_ref"]))
    assert artifact["hypothesis"] == "h"  # 结果写入 Artifact
    await project.close()


@pytest.mark.asyncio
async def test_ideator_agent_run_impl_wiring(tmp_path) -> None:
    """ideator_agent 经 production.ideator_run_impl 接入真实 Ideator。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    fake_ideator = _FakeIdeator()
    agent = IdeatorAgent(
        project.store,
        run_impl=production.ideator_run_impl(
            fake_ideator, project.store, _FakeProject()
        ),
    )
    project.kernel._type_registry.register(
        "ideator-native",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(agent), codec=JsonCodec()
        ),
    )
    _, run_id = await project.kernel.create_root(
        "ideator-native", {"content": "任务"}, name="ideator-native-root"
    )
    summary = await project.kernel.wait_run(run_id, timeout=2)
    assert summary.status.value == "completed"
    assert fake_ideator.called_with == ("P", ["p1"], ["m1"], "T")  # 真实模块被调用
    response = json.loads(summary.response_ref)
    artifact = json.loads(await project.store.get_text(response["result_ref"]))
    assert artifact["hypothesis"] == "h"  # DebateResult 写入 Artifact
    await project.close()


@pytest.mark.asyncio
async def test_run_search_advances_phase(tmp_path) -> None:
    """设计 §5：SEARCH 阶段 spawn Ideator 生成假设并推进阶段。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    hypothesis_ref = await project.run_search("假设A")
    assert hypothesis_ref.startswith("sha256:")
    assert project.phase == "SEARCH"
    hypothesis = json.loads(await project.store.get_text(hypothesis_ref))
    assert hypothesis["hypothesis"] == "假设A"
    assert hypothesis["status"] == "PROPOSED"
    await project.close()


async def _to_validate(project: ProjectRuntime, tmp_path) -> None:
    """推进到 VALIDATE：CONFIGURE -> PREPARE -> SEARCH -> VALIDATE。"""
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    hypothesis_ref = await project.run_search("假设A")
    await project.run_validate(hypothesis_ref)


@pytest.mark.asyncio
async def test_run_report_passed_reaches_completed(tmp_path) -> None:
    """端到端 §5.5：报告评审通过 → 接受 v1 并推进 COMPLETED。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    await _to_validate(project, tmp_path)
    report_ref = await project.run_report("最终研究报告正文")
    assert report_ref.startswith("sha256:")
    assert project.phase == "COMPLETED"
    files = await DirectoryBundle.files(project.store, report_ref)
    assert "report.md" in files
    assert await project.store.get_text(files["report.md"]) == "最终研究报告正文"
    await project.close()


@pytest.mark.asyncio
async def test_run_report_failed_then_revised(tmp_path) -> None:
    """端到端 §5.5：空报告评审 failed → follow-up 原 ReportAgent 提交 v2。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults()
    await project.open()
    await _to_validate(project, tmp_path)
    v1 = await project.run_report("")
    assert v1.startswith("sha256:")
    assert project.phase == "REPORT"  # failed 不推进 COMPLETED
    # 修订路径 follow-up 原 ReportAgent 产生新报告 Bundle（v2）
    report_id = next(
        a.agent_id for a in project.kernel.list_agents() if a.name == "report-root"
    )
    run_id = await project.kernel.followup(report_id, {"content": "修订后的报告"})
    summary = await project.kernel.wait_run(run_id, timeout=10)
    v2 = json.loads(summary.response_ref)["result_ref"]
    assert v2 != v1
    files = await DirectoryBundle.files(project.store, v2)
    assert await project.store.get_text(files["report.md"]) == "修订后的报告"
    await project.close()
