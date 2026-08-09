"""项目 Composition Root 测试（设计 §3.1/§6）：root_supervisor 持久化与编排。"""

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

from athena.core.agent.types import AgentCommandError, AgentStatus
from athena.research.models import MetricDef, MetricSpec, TaskMetaData
from athena.research.project_runtime import ProjectRuntime
from athena.core.bundle import DirectoryBundle
from test.unit._support import make_project


@pytest.mark.asyncio
async def test_register_defaults_requires_model(tmp_path) -> None:
    """无 model → register_defaults 报错，不再静默确定性。"""
    project = ProjectRuntime(tmp_path)
    with pytest.raises(RuntimeError, match="requires a model"):
        project.register_defaults()  # 无 model → 报错，不再静默确定性
    await project.close()


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
    project = make_project(tmp_path)
    sid = await project.open(message="start")
    assert sid is not None
    assert (tmp_path / ".athena" / "project.json").exists()
    await project.close()


@pytest.mark.asyncio
async def test_reopen_reuses_same_root_supervisor(tmp_path) -> None:
    first = make_project(tmp_path)
    sid1 = await first.open(message="start")
    await first.close()

    # 重新打开（全新实例）→ 复用同一 root_supervisor_id，不用新实例冒充
    reopened = make_project(tmp_path)
    sid2 = await reopened.open(message="continue")
    assert sid2 == sid1
    assert reopened.supervisor_id == sid1
    await reopened.close()


@pytest.mark.asyncio
async def test_open_resume_restores_supervisor_rollout(tmp_path):
    """Codex 风格持久化：root Supervisor 对话经确定性 rollout JSONL，重启复用同 id。"""
    pr1 = make_project(tmp_path)
    sid = await pr1.open(message="hello")
    rollout = tmp_path / ".athena" / "sessions" / f"{sid}.jsonl"
    assert rollout.exists()
    await pr1.close()

    pr2 = make_project(tmp_path)
    sid2 = await pr2.open(message="again")
    assert sid2 == sid  # 同一 supervisor id 恢复
    await pr2.close()


@pytest.mark.asyncio
async def test_supervisor_uses_tools_to_orchestrate_child(tmp_path) -> None:
    """设计 §5.2：Supervisor 经受控工具 spawn data 子并 wait_for，子完成后唤醒。"""
    project = make_project(tmp_path)
    sid = await project.open(message="start")
    assert sid is not None

    # supervisor 首轮 spawn data-child + wait_for → 子完成后唤醒回 IDLE
    assert await _eventually(
        lambda: project.kernel.agent_status(sid) == AgentStatus.IDLE, timeout=15
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
    project = make_project(tmp_path)
    await project.configure(_task())
    assert project.phase == "CONFIGURED"


@pytest.mark.asyncio
async def test_prepare_data_analysis_passed(tmp_path) -> None:
    """设计 §7.1：DataAnalysis 评审通过 → 接受 v1，阶段推进 PREPARE。"""
    project = make_project(tmp_path)
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
    project = make_project(tmp_path)
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
    project = make_project(tmp_path)
    version = project.freeze_eval_spec(
        MetricDef(name="accuracy", direction="maximize", description="acc")
    )
    assert version == 1
    assert project.eval_specs.version == 1
    assert project.eval_specs.current.primary.name == "accuracy"


@pytest.mark.asyncio
async def test_eval_protocol_append_only_preserves_v1(tmp_path) -> None:
    project = make_project(tmp_path)
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
    project = make_project(tmp_path)
    project.freeze_eval_spec(
        MetricDef(name="accuracy", direction="maximize", description="acc")
    )
    await project.close()

    reopened = make_project(tmp_path)
    await reopened.open()
    assert reopened.eval_specs.version == 1  # 从 JSON 恢复
    assert reopened.eval_specs.current.primary.name == "accuracy"
    await reopened.close()


@pytest.mark.asyncio
async def test_projected_phase_from_committed_facts(tmp_path) -> None:
    """设计 §4.2：阶段由已提交事实确定性投影，不依赖 Agent 声明。"""
    project = make_project(tmp_path)
    assert project.projected_phase() == "IDLE"  # 无任务
    await project.configure(_task())
    assert project.projected_phase() == "CONFIGURED"
    await project.open()
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    # InitAgent 在 PREPARE 前置冻结评估协议 → 有分析 + 有协议即 PREPARE
    assert project.projected_phase() == "PREPARE"
    await project.close()


@pytest.mark.asyncio
async def test_project_status_reflects_kernel(tmp_path) -> None:
    project = make_project(tmp_path)
    assert project.project_status() == "RUNNING"
    await project.open()
    assert project.project_status() == "RUNNING"  # supervisor 未等人工
    await project.close()


@pytest.mark.asyncio
async def test_register_defaults_covers_eight_types(tmp_path) -> None:
    """registered-agent-catalog §3：Composition Root 注册全部八个静态类型。

    AgentRuntime 门面在构造时即创建 asyncio.Future（RuntimeThreadManager），
    故需在事件循环内构造 ProjectRuntime（与其他用例一致）。
    """
    project = make_project(tmp_path)  # 同步方法,勿 await
    assert set(project.kernel._registry.types) == {
        "supervisor",
        "init",
        "data",
        "plot",
        "reflection",
        "ideator",
        "code",
        "report",
    }
    await project.close()


@pytest.mark.asyncio
async def test_pause_stops_dispatch_then_resume_continues(tmp_path) -> None:
    """设计 §13：pause 停止派发新 turn，resume 恢复。

    AgentRuntime 门面下 pause 无全局队列可排队（# COMPAT: 降级为拒绝新派发），
    故 pause 后 spawn 报错；resume 后派发真实 data 子并完成。
    """
    project = make_project(tmp_path)
    sid = await project.open()
    project.pause()
    assert project.project_status() == "PAUSED"
    content = json.dumps(
        {
            "data_path": str(_dataset(tmp_path)),
            "target": "label",
            "workspace": str(tmp_path / "ws"),
        }
    )
    # pause 后 spawn → 门面拒绝（runtime paused），不再 QUEUED 排队
    with pytest.raises(AgentCommandError):
        await project.kernel.spawn(sid, "data", {"content": content}, name="c")
    project.resume()
    assert project.project_status() == "RUNNING"
    _, child_run = await project.kernel.spawn(
        sid, "data", {"content": content}, name="c"
    )
    assert await _eventually(
        lambda: project.kernel.run_summary(child_run).status.value == "completed",
        timeout=10,
    )
    await project.close()


@pytest.mark.asyncio
async def test_stop_projects_cancelled_and_halts_dispatch(tmp_path) -> None:
    """设计 §13：stop 中断活动 Run、投影 CANCELLED、不再派发新 turn。"""
    project = make_project(tmp_path)
    sid = await project.open()
    await project.stop()
    assert project.project_status() == "CANCELLED"
    # 停止后 spawn → 门面拒绝（runtime paused），不再派发
    with pytest.raises(AgentCommandError):
        await project.kernel.spawn(sid, "data", {"content": ""}, name="c")
    await project.close()


from athena.research.runtime import ResearchRuntime


@pytest.mark.asyncio
async def test_research_runtime_delegates_phase_to_project(tmp_path) -> None:
    """设计 §15：ResearchRuntime 可把 phase/status 投影委托给 ProjectRuntime。"""
    project = make_project(tmp_path)
    await project.configure(_task())
    runtime = ResearchRuntime(project=project)
    assert runtime.phase == "CONFIGURED"  # 委托项目投影
    assert runtime.project_status() == "RUNNING"


@pytest.mark.asyncio
async def test_ideator_code_report_are_real_factories(tmp_path) -> None:
    """方案2 §8：ideator/code/report 均为真实 factory，产出真实结果 Artifact。"""
    project = make_project(tmp_path)
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
from athena.agents.simple_agents import IdeatorAgent
from athena.core.agent.models import AgentOutcome
from athena.core.agent.codec import JsonCodec
from athena.core.agent.types import AgentSpec


@pytest.mark.asyncio
async def test_injectable_real_impl_delegates(tmp_path) -> None:
    """生产包装契约：注入 run_impl 时委托真实逻辑并返回其结果。"""
    project = make_project(tmp_path)
    await project.open()
    called: list[str] = []

    async def fake_impl(ctx) -> AgentOutcome:
        called.append(ctx.input_text or "")
        return AgentOutcome(result_ref="result://real")

    project.kernel._registry.register(
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
from athena.agents.simple_agents import IdeatorAgent


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
    project = make_project(tmp_path)
    await project.open()
    fake_ideator = _FakeIdeator()
    run_impl = production.ideator_run_impl(fake_ideator, project.store, _FakeProject())
    project.kernel._registry.register(
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
    project = make_project(tmp_path)
    await project.open()
    fake_ideator = _FakeIdeator()
    agent = IdeatorAgent(
        project.store,
        run_impl=production.ideator_run_impl(
            fake_ideator, project.store, _FakeProject()
        ),
    )
    project.kernel._registry.register(
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
    """设计 §5：SEARCH spawn Ideator 提交假设 + CodeAgent 生成候选，推进阶段。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    candidate_ref = await project.run_search("假设A")
    assert candidate_ref.startswith("sha256:")
    assert project.phase == "SEARCH"
    candidate = json.loads(await project.store.get_text(candidate_ref))
    assert candidate["diff"] == "implement 假设A"  # CodeAgent 候选 diff
    assert any(h.statement == "假设A" for h in project.tree.pending_hypotheses())
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
    project = make_project(tmp_path)
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
    project = make_project(tmp_path)
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


from athena.agents.report_agent import ReportAgent


@pytest.mark.asyncio
async def test_report_agent_model_path_synthesizes_from_user_input(tmp_path) -> None:
    """ReportAgent 真实路径：设计 prompt + UserInput（content + context_refs）→ 报告 Bundle。"""
    project = make_project(tmp_path)
    await project.open()
    captured: list[str] = []

    async def fake_model(prompt: str) -> str:
        captured.append(prompt)
        return "# 报告\n\n基于证据的综合。"

    agent = ReportAgent(project.store, model=fake_model)
    project.kernel._registry.register(
        "report-model",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(agent), codec=JsonCodec()
        ),
    )
    evidence_ref = await project.store.put_text("已批准证据正文")
    _, run_id = await project.kernel.create_root(
        "report-model",
        {"content": "请撰写最终报告", "context_refs": [evidence_ref]},
        name="report-model-root",
    )
    summary = await project.kernel.wait_run(run_id, timeout=2)
    assert summary.status.value == "completed"
    assert "已批准证据正文" in captured[0]  # UserInput 证据进入 prompt
    assert "请撰写最终报告" in captured[0]
    result_ref = json.loads(summary.response_ref)["result_ref"]
    files = await DirectoryBundle.files(project.store, result_ref)
    assert (
        await project.store.get_text(files["report.md"]) == "# 报告\n\n基于证据的综合。"
    )
    await project.close()


@pytest.mark.asyncio
async def test_task_survives_reopen(tmp_path) -> None:
    """端到端设计 §3：task 事实随项目状态持久化，跨重启恢复。"""
    project = make_project(tmp_path)
    await project.configure(_task())
    await project.close()

    reopened = make_project(tmp_path)
    await reopened.open()
    assert reopened.projected_phase() == "CONFIGURED"  # 从 JSON 恢复 task
    await reopened.close()


@pytest.mark.asyncio
async def test_projected_phase_advances_through_six_stages(tmp_path) -> None:
    """端到端 §5：六阶段投影由已提交事实驱动，SEARCH→VALIDATE→REPORT→COMPLETED。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    assert project.projected_phase() == "PREPARE"
    await project.run_search("假设A")
    assert project.projected_phase() == "VALIDATE"  # 有 SOTA 事实
    await project.run_validate("sota-ref")
    assert project.projected_phase() == "REPORT"  # 有 validation 事实
    await project.run_report("最终报告正文")
    assert project.projected_phase() == "COMPLETED"  # 有 report 事实
    await project.close()


@pytest.mark.asyncio
async def test_phase_refs_survive_reopen(tmp_path) -> None:
    """端到端 §3：各阶段 refs 随项目状态持久化，跨重启投影完整六阶段。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    await project.run_search("假设A")
    await project.run_validate("sota-ref")
    await project.close()

    reopened = make_project(tmp_path)
    await reopened.open()
    assert reopened.projected_phase() == "REPORT"  # sota+validation 事实恢复
    await reopened.close()


@pytest.mark.asyncio
async def test_project_memory_survives_reopen(tmp_path) -> None:
    """memory-human-wait §7：项目记忆随项目状态持久化，跨重启检索。"""
    project = make_project(tmp_path)
    await project.open()
    entry_id = project.add_project_memory(
        kind="fix",
        summary="修复了数据切分 bug",
        source_refs=["sha256:x"],
        created_by="agent_1",
    )
    assert project.memory.get(entry_id).scope == "project"
    await project.close()

    reopened = make_project(tmp_path)
    await reopened.open()
    entries = reopened.memory.retrieve(scope="project")
    assert [e.summary for e in entries] == ["修复了数据切分 bug"]
    await reopened.close()


@pytest.mark.asyncio
async def test_validate_final_test_exactly_once(tmp_path) -> None:
    """端到端 §5.4：final-test 恰好一次，已提交结果后拒绝重入。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    await project.run_search("假设A")
    v1 = await project.run_validate("sota-ref")
    assert v1.startswith("sha256:")
    # 第二次 final-test → 拒绝（恰好一次不变量）
    with pytest.raises(RuntimeError, match="already has a final-test"):
        await project.run_validate("sota-ref")
    await project.close()


@pytest.mark.asyncio
async def test_search_stops_when_budget_exhausted(tmp_path) -> None:
    """端到端 §5.3：预算耗尽后 run_search 拒绝继续。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    # 预算设 1：一次搜索后耗尽
    project.budget.remaining = 1
    project.budget.no_improve_streak = project.budget.max_no_improve - 1
    await project.run_search("假设A")
    assert project.budget.is_exhausted
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await project.run_search("假设B")
    await project.close()


@pytest.mark.asyncio
async def test_budget_survives_reopen(tmp_path) -> None:
    """端到端 §5.3：预算随项目状态持久化，跨重启不重置。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    await project.run_search("假设A")
    assert project.budget.remaining == 19  # 默认 20，消耗一次
    await project.close()

    reopened = make_project(tmp_path)
    await reopened.open()
    assert reopened.budget.remaining == 19  # 从 JSON 恢复
    await reopened.close()


@pytest.mark.asyncio
async def test_search_requires_prepare(tmp_path) -> None:
    """端到端 §5.2-5.3：SEARCH 前置 PREPARE，CONFIGURED 直接进入被拒绝。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    with pytest.raises(RuntimeError, match="SEARCH requires PREPARE"):
        await project.run_search("假设A")
    await project.close()


@pytest.mark.asyncio
async def test_message_delivers_to_supervisor_without_turn(tmp_path) -> None:
    """端到端 §4：message 只投递到 root Supervisor，不触发新 turn。"""
    project = make_project(tmp_path)
    sid = await project.open(message="start")
    # 等待 supervisor 首轮编排（spawn 子 + 登记 wait）结束，避免与首轮 turn 竞争
    assert await _eventually(
        lambda: project.kernel.agent_status(sid) != AgentStatus.RUNNING, timeout=10
    )
    await project.message("追加指令")
    mailbox = project.kernel._records[sid].mailbox
    assert any(m.content == "追加指令" for m in mailbox)
    assert project.kernel.agent_status(sid) != AgentStatus.RUNNING  # 不触发 turn
    await project.close()


@pytest.mark.asyncio
async def test_message_requires_open_project(tmp_path) -> None:
    """端到端 §4：未 open 的 project 调 message 报错。"""
    project = make_project(tmp_path)
    with pytest.raises(RuntimeError, match="not opened"):
        await project.message("x")


@pytest.mark.asyncio
async def test_full_workflow_evidence_traceable_across_reopen(tmp_path) -> None:
    """完成条件8（端到端 §10.10）：完整 CSV 流程到 COMPLETED，跨重启全部事实可追溯。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    sota_ref = await project.run_search("假设A")
    validation_ref = await project.run_validate(sota_ref)
    report_ref = await project.run_report("最终研究报告正文")
    assert project.phase == "COMPLETED"
    entry_id = project.add_project_memory(
        kind="fix", summary="已验证修复", source_refs=[report_ref], created_by="agent_1"
    )
    await project.close()

    # 跨重启：全部阶段事实从 JSON 恢复，报告可追溯
    reopened = make_project(tmp_path)
    await reopened.open()
    assert reopened.projected_phase() == "COMPLETED"
    assert reopened.eval_specs.version >= 1
    files = await DirectoryBundle.files(reopened.store, report_ref)
    assert await reopened.store.get_text(files["report.md"]) == "最终研究报告正文"
    assert reopened.memory.retrieve(scope="project")
    assert reopened.budget.remaining == 19  # SEARCH 消费一次
    await reopened.close()


@pytest.mark.asyncio
async def test_search_commits_hypothesis_to_research_tree(tmp_path) -> None:
    """设计 §4：run_search 把 Ideator 假设提交到 ResearchTree（所有权契约）。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    sota_ref = await project.run_search("假设A")
    assert sota_ref.startswith("sha256:")
    hypotheses = project.tree.pending_hypotheses()
    assert any(h.statement == "假设A" for h in hypotheses)
    await project.close()


@pytest.mark.asyncio
async def test_research_tree_survives_reopen(tmp_path) -> None:
    """设计 §4：ResearchTree 随项目状态持久化，跨重启保留假设。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    await project.run_search("假设A")
    await project.close()

    reopened = make_project(tmp_path)
    await reopened.open()
    assert any(h.statement == "假设A" for h in reopened.tree.pending_hypotheses())
    await reopened.close()


@pytest.mark.asyncio
async def test_search_spawns_code_agent_for_hypothesis(tmp_path) -> None:
    """完成条件3：SEARCH 生命周期由 Kernel 管理——Ideator + CodeAgent 均经 Kernel spawn。"""
    project = make_project(tmp_path)
    await project.open()
    await project.configure(_task())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    await project.run_search("假设A")
    agent_names = [a.name for a in project.kernel.list_agents()]
    assert "ideator-root" in agent_names  # Ideator 经 Kernel 创建
    assert "code-root" in agent_names  # CodeAgent 经 Kernel 创建
    await project.close()


@pytest.mark.asyncio
async def test_llm_agent_runs_through_kernel_via_base_agent_runner(tmp_path) -> None:
    """框架集成：Agent(output_type) 经 BaseAgentRunner 在 kernel 跑通一个 turn。"""
    from athena.agents.base_runner import BaseAgentRunner
    from athena.core.agent.provider import ResponsesProvider, StreamEvent
    from athena.core.agent.runtime import Agent
    from athena.core.agent.codec import JsonCodec
    from athena.core.agent.types import AgentSpec
    from athena.core.tool import ToolRegistry
    from pydantic import BaseModel

    class Out(BaseModel):
        ok: bool

    class FakeModel:
        async def stream(self, *_args, **_kwargs):
            yield StreamEvent(
                "text_delta", {"delta": '{"ok":true}', "accumulated": '{"ok":true}'}
            )
            yield StreamEvent("response_completed")

    project = make_project(tmp_path)
    await project.open()
    store = project.store
    agent = Agent(
        ResponsesProvider("m"), ToolRegistry(), "p", output_type=Out, artifacts=store
    )
    agent.model = FakeModel()
    project.kernel._registry.register(
        "llm-demo",
        lambda _aid, _cfg=None: AgentSpec(
            runner=BaseAgentRunner(agent), codec=JsonCodec()
        ),
    )
    _, run_id = await project.kernel.create_root(
        "llm-demo", {"content": "go"}, name="llm-root"
    )
    summary = await project.kernel.wait_run(run_id, timeout=2)
    assert summary.status.value == "completed"
    response = json.loads(summary.response_ref)
    assert response["result_ref"].startswith("sha256:")
    await project.close()
