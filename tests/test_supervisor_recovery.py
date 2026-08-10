"""Supervisor 崩溃恢复测试（supervisor_imp_docs Task 9）。

使用真实 SQLite + ArtifactStore，验证关键耐久边界恢复后 exactly-once 语义：
dispatch 对账不重复 spawn、已提交 facts/预算幂等重放、开放 HumanRequest 跨
重启持久化。
"""

from pathlib import Path

import pytest

from athena.research.supervisor.executor import PlanExecutor
from athena.research.supervisor.journal import PlanJournal
from athena.research.supervisor.models import (
    OperationType,
    PlanStatus,
    SupervisorOperation,
    SupervisorPlan,
)
from athena.research.supervisor.state import ProjectStateStore
from test.unit.research.test_supervisor_core import (
    _components,
    _configure,
    _now,
    FakeWorkerRuntime,
)


def _reopen(tmp_path):
    return PlanJournal(tmp_path / ".athena" / "supervisor.db")


def test_dispatch_reconciliation_after_reopen(tmp_path) -> None:
    """agent_dispatched_before_journal_completion：重开 reserve 返回已派发身份，不重复 spawn。"""
    c = _components(tmp_path)
    journal, execution_id = c["journal"], c["execution_id"]
    lease = journal.claim_lease(execution_id, "owner")
    key = "plan_x:data"
    record = journal.reserve_dispatch(key, "data", "payload:ref", lease=lease)
    assert record["status"] == "RESERVED"
    journal.complete_dispatch(key, "agent_1", "run_1", lease=lease)
    journal.close()  # 崩溃：dispatch 已落库但操作未完成

    # 重开：同一 dispatch key 返回 DISPATCHED（含 agent_id），调用方 resume 而非 spawn
    reopened = _reopen(tmp_path)
    record2 = reopened.reserve_dispatch(
        key,
        "data",
        "payload:ref",
        lease=reopened.claim_lease(execution_id, "owner"),
    )
    assert record2["status"] == "DISPATCHED"
    assert record2["agent_id"] == "agent_1"
    assert record2["run_id"] == "run_1"
    reopened.close()


@pytest.mark.asyncio
async def test_reexecution_after_reopen_is_idempotent(tmp_path) -> None:
    """test_score_written_before_sota_commit：重放已完成的 SEARCH round 不重复消费预算/事实。"""
    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    executor, runtime = c["executor"], c["runtime"]
    runtime.next_payload = {
        "candidates": [
            {"candidate_id": "a", "test_score": 0.9, "direction": "maximize"}
        ]
    }
    spawn = SupervisorOperation(
        operation_id="op_spawn",
        operation_type=OperationType.SPAWN_BATCH,
        idempotency_key="spawn:data",
        inputs={"spawns": [{"key": "data", "agent_type": "data", "payload": {}}]},
    )
    wait = SupervisorOperation(
        operation_id="op_wait",
        operation_type=OperationType.WAIT_AGENTS,
        idempotency_key="wait:data",
        inputs={"from_op": "op_spawn", "keys": ["data"]},
    )
    service = SupervisorOperation(
        operation_id="op_svc",
        operation_type=OperationType.RUN_SERVICE,
        idempotency_key="run:freeze_round",
        inputs={
            "service": "search.freeze_ranking_round",
            "request": {
                "parent_score": 0.8,
                "selected_count": 1,
                "candidates": {
                    "from_op": "op_wait",
                    "worker": "data",
                    "field": "candidates",
                },
            },
        },
    )
    plan = SupervisorPlan(
        plan_id="plan_recover",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="SEARCH_ROUND",
        operations=[spawn, wait, service],
        created_at=_now(),
    )
    lease = journal.claim_lease(execution_id, "owner")
    await executor.execute(plan, lease=lease)
    assert plan.status == PlanStatus.COMPLETED
    assert state.budget().search_experiments_used == 1
    journal.close()  # 崩溃：score 已写入但未二次提交

    # 重开重放同一计划：已 SUCCEEDED 的 op 跳过，不重复消费预算或覆盖事实
    reopened = _reopen(tmp_path)
    state2 = ProjectStateStore(reopened)
    loaded = reopened.load_plan(plan.plan_id)
    assert loaded is not None
    executor2 = PlanExecutor(
        journal=reopened,
        state=state2,
        runtime=FakeWorkerRuntime(c["store"]),
        store=c["store"],
        services=c["services"],
    )
    lease2 = reopened.claim_lease(execution_id, "owner")
    await executor2.execute(loaded, lease=lease2)
    assert state2.budget().search_experiments_used == 1  # 预算只消费一次
    assert state2.facts().sota_experiment_ref == "a"  # SOTA 事实只提交一次
    assert len(c["runtime"].spawned) == 1  # 不重复 spawn
    reopened.close()


def test_open_human_request_survives_reopen(tmp_path) -> None:
    """human_request_open_before_restart：开放请求跨重启持久化，不重复创建。"""
    from athena.research.supervisor.models import HumanRequest

    c = _components(tmp_path)
    journal, execution_id = c["journal"], c["execution_id"]
    request = HumanRequest(
        request_id="req_persist",
        execution_id=execution_id,
        reason_code="data_role_resolution",
        question="如何解决？",
        options=["ACCEPT_CURRENT_PROPOSAL", "SUBMIT_ROLE_CORRECTION"],
        created_at=_now(),
    )
    journal.save_human_request(request)
    journal.close()  # 崩溃：请求已创建但未回答

    reopened = _reopen(tmp_path)
    stored = reopened.human_request("req_persist")
    assert stored is not None and stored.status == "OPEN"
    assert stored.question == "如何解决？"
    assert [r.request_id for r in reopened.open_human_requests(execution_id)] == [
        "req_persist"
    ]
    reopened.close()


def test_final_score_written_before_db_commit(tmp_path) -> None:
    """final_score_written_before_db_commit：SCORED 后崩溃，重开只提交不重评分/不新建 attempt。"""
    import pytest

    c = _components(tmp_path)
    journal, execution_id = c["journal"], c["execution_id"]
    lease = journal.claim_lease(execution_id, "owner")
    attempt = journal.reserve_final_test(
        execution_id=execution_id,
        sota_experiment_id="sota_a",
        final_test_ref="sha256:f",
        lease=lease,
        sota_ref="sha256:s",
        eval_spec_ref="sha256:e",
        dataset_ref="sha256:d",
    )
    for status in ("RUNNING", "PREDICTIONS_WRITTEN"):
        attempt = journal.advance_final_test(
            attempt.attempt_id,
            status,
            lease=lease,
            prediction_ref="sha256:p",
        )
    attempt = journal.advance_final_test(
        attempt.attempt_id,
        "SCORED",
        lease=lease,
        score_ref="sha256:score",
        final_test_score=0.92,
    )
    journal.close()  # 崩溃：score 已持久化但尚未 COMMITTED

    reopened = _reopen(tmp_path)
    lease2 = reopened.claim_lease(execution_id, "owner")
    # 重开不新建 attempt：同一唯一键返回同一 attempt，从已到达的 SCORED 继续
    same = reopened.reserve_final_test(
        execution_id=execution_id,
        sota_experiment_id="sota_a",
        final_test_ref="sha256:f",
        lease=lease2,
        sota_ref="sha256:s",
        eval_spec_ref="sha256:e",
        dataset_ref="sha256:d",
    )
    assert same.attempt_id == attempt.attempt_id
    assert same.status == "SCORED"
    # 恢复决策（design §2.5）：score 已存在 → 只提交，不重训/不重预测/不重评分
    from athena.research.validation import (
        RECOVERY_COMMIT,
        RECOVERY_EVALUATE,
        final_test_recovery_action,
    )

    assert final_test_recovery_action(same) == RECOVERY_COMMIT
    # 对照：尚无 score_ref 时才需 evaluator 评分（恢复决策正确区分两种耐久状态）
    assert (
        final_test_recovery_action(
            same.model_copy(update={"status": "PREDICTIONS_WRITTEN", "score_ref": None})
        )
        == RECOVERY_EVALUATE
    )
    # 提交路径只做 SCORED→COMMITTED CAS，不涉及 evaluator（调用方按决策不评分）
    committed = reopened.advance_final_test(
        same.attempt_id,
        "COMMITTED",
        lease=lease2,
        final_test_score=0.92,
    )
    assert committed.status == "COMMITTED"
    assert committed.final_test_score == 0.92  # 复用已有 score，不重评分
    assert committed.score_ref == "sha256:score"  # 不重新预测
    reopened.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_incomplete_plan_resumes_after_reopen(tmp_path) -> None:
    """incomplete Plan 自身推进 state_version 后的恢复：刷新 snapshot，只重放未完成 op。

    SEARCH_NO_SOTA 计划先 COMMIT_FACTS（推进 state_version）后中断；重开时
    coordinator 恢复该 Plan，校验不再因过期 snapshot_version 拒绝，剩余
    SET_EXECUTION_STATUS op 重放完成并把 execution 置为 FAILED。
    """
    from athena.research.supervisor.coordinator import SupervisorCoordinator
    from athena.research.supervisor.models import ControlStatus, OperationStatus
    from athena.research.supervisor.planner import DeterministicSupervisorPlanner
    from athena.research.supervisor.validator import PlanValidator

    c = _components(tmp_path)
    journal, state, execution_id = c["journal"], c["state"], c["execution_id"]
    _configure(c)
    state.commit_facts(
        {
            "dataset_role_proposal_ref": "sha256:p",
            "dataset_manifest_ref": "sha256:m",
            "dataset_role_review_ref": "sha256:r",
            "eval_spec_ref": "sha256:e",
            "eda_report_ref": "sha256:d",
            "eda_review_ref": "sha256:rv",
            "baseline_experiment_ref": "sha256:b",
            "search_stop_ref": "search_stop:accepted",  # 已停止但无 sota_experiment_ref
        }
    )
    plan = c["planner"].next_plan(
        execution_id, state.facts(), state.budget(), ControlStatus.RUNNING
    )
    assert plan.reason_code == "SEARCH_NO_SOTA"
    assert plan.operations[0].operation_type is OperationType.COMMIT_FACTS
    lease = journal.claim_lease(execution_id, "owner")
    journal.save_plan(plan, lease=lease)
    # 模拟第一个 op（COMMIT_FACTS failure_reason）已执行 → 推进 state_version；随后中断
    state.commit_facts({"failure_reason": "SEARCH_STOPPED_NO_SOTA"})
    journal.advance_operation(
        plan.operations[0], plan.plan_id, lease=lease, status=OperationStatus.SUCCEEDED
    )
    journal.set_plan_status(plan.plan_id, PlanStatus.RUNNING, lease=lease)
    assert journal.snapshot_version() > plan.snapshot_version  # snapshot 已过期
    assert journal.get_fact("failure_reason") == "SEARCH_STOPPED_NO_SOTA"
    # 模拟崩溃：旧 writer 的 lease 已过期，重开后 coordinator 经 fencing 接管
    journal._conn.execute("DELETE FROM leases WHERE execution_id = ?", (execution_id,))
    journal._conn.commit()
    journal.close()

    # 重开：coordinator 绑定原 execution，恢复未完成 Plan
    journal2 = PlanJournal(tmp_path / ".athena" / "supervisor.db")
    state2 = ProjectStateStore(journal2)
    execution_id2 = journal2.active_execution_id() or journal2.latest_execution_id()
    assert execution_id2 == execution_id
    coord2 = SupervisorCoordinator(
        journal=journal2,
        state=state2,
        planner=DeterministicSupervisorPlanner(journal2, state2),
        validator=PlanValidator(journal2, state2),
        executor=PlanExecutor(
            journal=journal2,
            state=state2,
            runtime=FakeWorkerRuntime(c["store"]),
            store=c["store"],
            services=c["services"],
        ),
    )
    coord2.bind(execution_id2)
    terminal = await coord2.run()
    assert terminal == ControlStatus.FAILED  # 剩余 SET_EXECUTION_STATUS op 重放完成
    assert state2.control_status() is ControlStatus.FAILED
    assert journal2.get_fact("failure_reason") == "SEARCH_STOPPED_NO_SOTA"
    journal2.close()
