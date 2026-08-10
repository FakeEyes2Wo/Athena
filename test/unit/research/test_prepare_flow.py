"""PREPARE 状态机测试（supervisor_imp_docs Task 5）。

用 fake worker 驱动 coordinator，断言 PREPARE 的中间门槛确实阻止阶段推进：
role review / EDA review 未接受前不产生 baseline；评审计划按计划顺序产出。
并覆盖 Reflection ACCEPT/REVISE 闭环：REVISE 后 follow-up 同一 DataAgent、
修订预算耗尽后 auto FAILED / interactive 持久化 HumanRequest。
"""

import pytest

from athena.research.supervisor.models import (
    ControlStatus,
    OperationType,
    ResearchPhase,
)
from athena.research.supervisor.state import project_phase
from test.unit.research.test_supervisor_core import _components, _configure


@pytest.mark.asyncio
async def test_prepare_requires_role_and_eda_review_before_baseline(tmp_path) -> None:
    """PREPARE 门槛：role_review/eda_review/baseline 缺一不可，补齐才进入 SEARCH。"""
    c = _components(tmp_path)
    _configure(c)
    state = c["state"]
    state.commit_facts(
        {
            "dataset_role_proposal_ref": "sha256:p",
            "dataset_manifest_ref": "sha256:m",
            "eval_spec_ref": "sha256:e",
            "eda_report_ref": "sha256:d",
        }
    )
    assert project_phase(state.facts()) is ResearchPhase.PREPARE  # 缺 role/eda review
    state.commit_facts({"dataset_role_review_ref": "sha256:r"})
    assert (
        project_phase(state.facts()) is ResearchPhase.PREPARE
    )  # 缺 eda_review/baseline
    state.commit_facts({"eda_review_ref": "sha256:rv"})
    assert project_phase(state.facts()) is ResearchPhase.PREPARE  # 缺 baseline
    state.commit_facts({"baseline_experiment_ref": "sha256:b"})
    assert project_phase(state.facts()) is ResearchPhase.SEARCH


@pytest.mark.asyncio
async def test_prepare_sequence_emits_review_plans(tmp_path) -> None:
    """coordinator 从 PREPARE 推进时按计划顺序产出评审与接受提升计划。"""
    c = _components(tmp_path)
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, planner = c["executor"], c["planner"]
    lease = journal.claim_lease(execution_id, "test-owner")

    reason_codes: list[str] = []
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    while plan is not None and len(reason_codes) < 8:
        reason_codes.append(plan.reason_code)
        await executor.execute(plan, lease=lease)
        plan = planner.next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )

    assert reason_codes == [
        "PREPARE_INGEST",
        "PREPARE_ROLE_REVIEW",
        "PREPARE_ROLE_REVIEW_ACCEPT",
        "PREPARE_EVAL",
        "PREPARE_EDA",
        "PREPARE_EDA_REVIEW",
        "PREPARE_EDA_REVIEW_ACCEPT",
        "PREPARE_BASELINE",
    ]
    # role/EDA review 由独立 Reflection worker 执行并接受（draft 保留，verdict 已清空）
    assert state.facts().dataset_role_review_ref is not None
    assert state.facts().eda_review_ref is not None
    assert isinstance(journal.get_fact("role_review_draft_ref"), str)
    assert journal.get_fact("role_review_verdict") is None
    journal.close()


@pytest.mark.asyncio
async def test_revise_follows_up_same_data_agent(tmp_path) -> None:
    """REVISE 后 follow-up 必须指向原 DataAgent（同 thread/workspace/reader lineage）。"""
    c = _components(tmp_path)
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, planner, runtime = c["executor"], c["planner"], c["runtime"]
    lease = journal.claim_lease(execution_id, "test-owner")

    # ingest → 记录原始 DataAgent 身份
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_INGEST"
    await executor.execute(plan, lease=lease)
    original = journal.get_fact("data_agent_id")
    assert isinstance(original, str) and original.startswith("agent_")

    # 第一次评审 REVISE
    runtime.review_verdict = "REVISE"
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_ROLE_REVIEW"
    await executor.execute(plan, lease=lease)
    assert journal.get_fact("role_review_verdict") == "REVISE"

    # 下一步：FOLLOWUP_AGENT 指向原 DataAgent，等待并提交修订后的 proposal
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_ROLE_REVIEW_REVISE"
    followup = plan.operations[0]
    assert followup.operation_type == OperationType.FOLLOWUP_AGENT
    assert followup.inputs["agent_id"] == original
    await executor.execute(plan, lease=lease)
    assert journal.get_fact("role_review_revision") == 1

    # 修订完成 → 重新评审（verdict 已清空）
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_ROLE_REVIEW"
    assert journal.get_fact("role_review_verdict") is None
    journal.close()


@pytest.mark.asyncio
async def test_eda_review_revise_follows_up_same_eda_agent(tmp_path) -> None:
    """EDA 评审 REVISE 后 follow-up 同一 EDA DataAgent（同 lineage）。"""
    c = _components(tmp_path)
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, planner, runtime = c["executor"], c["planner"], c["runtime"]
    lease = journal.claim_lease(execution_id, "test-owner")

    # role review ACCEPT、eval、EDA 依次完成，停在 EDA_REVIEW 前
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    while plan is not None and plan.reason_code != "PREPARE_EDA_REVIEW":
        await executor.execute(plan, lease=lease)
        plan = planner.next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )
    assert plan is not None and plan.reason_code == "PREPARE_EDA_REVIEW"
    eda_agent = journal.get_fact("eda_agent_id")
    assert isinstance(eda_agent, str) and eda_agent.startswith("agent_")

    # EDA 评审 REVISE → follow-up 指向 EDA DataAgent
    runtime.review_verdict = "REVISE"
    await executor.execute(plan, lease=lease)
    assert journal.get_fact("eda_review_verdict") == "REVISE"
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_EDA_REVIEW_REVISE"
    followup = plan.operations[0]
    assert followup.operation_type == OperationType.FOLLOWUP_AGENT
    assert followup.inputs["agent_id"] == eda_agent
    journal.close()


@pytest.mark.asyncio
async def test_eda_review_auto_fails_after_revisions_exhausted(tmp_path) -> None:
    """auto 模式：EDA 评审两次修订仍 REVISE → execution FAILED（role 保持 ACCEPT）。"""
    c = _components(tmp_path)  # interaction_mode=auto
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    c["runtime"].eda_review_verdict = "REVISE"  # role 默认 ACCEPT

    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.FAILED
    assert journal.get_fact("failure_reason") == "EDA_REVIEW_EXHAUSTED"
    assert journal.get_fact("eda_review_revision") == 2
    assert state.facts().dataset_role_review_ref is not None  # role 已接受
    assert state.facts().eda_review_ref is None  # EDA 未被强制接受
    journal.close()


@pytest.mark.asyncio
async def test_eda_review_interactive_requests_human_then_promotes(tmp_path) -> None:
    """interactive 模式：EDA 修订耗尽 → 持久化 eda_review_resolution，接受后提升。"""
    c = _components(tmp_path, interaction_mode="interactive")
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, planner, runtime = c["executor"], c["planner"], c["runtime"]
    lease = journal.claim_lease(execution_id, "test-owner")
    runtime.eda_review_verdict = "REVISE"  # role 默认 ACCEPT

    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    while plan is not None:
        await executor.execute(plan, lease=lease)
        plan = planner.next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )

    request = journal.human_request("eda_review_resolution")
    assert request is not None and request.status == "OPEN"
    assert state.facts().eda_review_ref is None  # 阻塞，未被强制接受

    journal.answer_human_request(
        "eda_review_resolution", "ACCEPT_CURRENT_PROPOSAL", "human"
    )
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_EDA_REVIEW_ACCEPT"
    await executor.execute(plan, lease=lease)
    assert state.facts().eda_review_ref is not None
    assert journal.get_fact("eda_resolution") == "ACCEPT_CURRENT_PROPOSAL"
    journal.close()


@pytest.mark.asyncio
async def test_role_review_auto_fails_after_revisions_exhausted(tmp_path) -> None:
    """auto 模式：两次自动修订仍 REVISE → execution FAILED，不强制接受。"""
    c = _components(tmp_path)  # interaction_mode=auto
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    c["runtime"].review_verdict = "REVISE"

    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.FAILED
    assert journal.get_fact("failure_reason") == "DATA_ROLE_REVIEW_EXHAUSTED"
    assert state.facts().dataset_role_review_ref is None  # 未被强制接受
    assert journal.get_fact("role_review_revision") == 2
    journal.close()


@pytest.mark.asyncio
async def test_revision_limit_from_execution_config(tmp_path) -> None:
    """修订上限读 ExecutionConfig 投影事实：非默认值 1 生效（一次修订即耗尽）。"""
    c = _components(tmp_path)  # interaction_mode=auto
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    state.commit_facts({"prepare_revision_limit": 1})  # 模拟 TASK_CONFIGURE 投影
    c["runtime"].review_verdict = "REVISE"

    terminal = await c["coordinator"].run()
    assert terminal == ControlStatus.FAILED
    assert journal.get_fact("role_review_revision") == 1  # 只允许一次修订
    assert state.facts().dataset_role_review_ref is None
    journal.close()


@pytest.mark.asyncio
async def test_role_review_interactive_requests_human_then_promotes(tmp_path) -> None:
    """interactive 模式：修订预算耗尽 → 持久化 data_role_resolution，接受后提升。"""
    c = _components(tmp_path, interaction_mode="interactive")
    _configure(c)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, planner, runtime = c["executor"], c["planner"], c["runtime"]
    lease = journal.claim_lease(execution_id, "test-owner")
    runtime.review_verdict = "REVISE"

    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    while plan is not None:
        await executor.execute(plan, lease=lease)
        plan = planner.next_plan(
            execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
        )

    request = journal.human_request("data_role_resolution")
    assert request is not None and request.status == "OPEN"
    assert state.facts().dataset_role_review_ref is None  # 阻塞，未被强制接受

    # 真人回答 ACCEPT_CURRENT_PROPOSAL → 提升为权威事实
    journal.answer_human_request(
        "data_role_resolution", "ACCEPT_CURRENT_PROPOSAL", "human"
    )
    plan = planner.next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan is not None and plan.reason_code == "PREPARE_ROLE_REVIEW_ACCEPT"
    await executor.execute(plan, lease=lease)
    assert state.facts().dataset_role_review_ref is not None
    assert journal.get_fact("role_resolution") == "ACCEPT_CURRENT_PROPOSAL"
    journal.close()
