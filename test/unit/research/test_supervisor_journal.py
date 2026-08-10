"""Journal/lease/executor 恢复持久化测试（supervisor_imp_docs Task 2）。

覆盖：lease fencing（StaleLeaseError / LeaseConflictError）、幂等
complete_operation（不重复提交/消费预算）、dispatch 对账记录、durable outbox 事件。
"""

from pathlib import Path

import pytest

from athena.research.supervisor.journal import (
    LeaseConflictError,
    PlanJournal,
    StaleLeaseError,
)
from athena.research.supervisor.models import (
    OperationType,
    SupervisorOperation,
    SupervisorPlan,
    utc_now,
)


def _commit_op(idempotency_key: str) -> SupervisorOperation:
    return SupervisorOperation(
        operation_id=f"op_{idempotency_key.replace(':', '_')}",
        operation_type=OperationType.COMMIT_FACTS,
        idempotency_key=idempotency_key,
        inputs={"facts": {"sota_experiment_ref": "sha256:s"}},
    )


def _plan(
    journal: PlanJournal, execution_id: str, ops: list[SupervisorOperation]
) -> SupervisorPlan:
    return SupervisorPlan(
        plan_id="plan_x",
        execution_id=execution_id,
        sequence=1,
        snapshot_version=journal.snapshot_version(),
        reason_code="SEARCH_ROUND",
        operations=ops,
        created_at=utc_now(),
    )


def _seed_budget(journal: PlanJournal) -> None:
    journal.commit_facts(
        {
            "budget": {
                "max_total_plans": 100,
                "plans_used": 0,
                "max_search_experiments": 20,
                "search_experiments_used": 0,
                "max_consecutive_no_improvement": 5,
                "consecutive_no_improvement": 0,
            }
        },
        0,
    )


def test_claim_lease_and_fencing(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "s.db")
    first = journal.claim_lease("exec-1", "owner-a", ttl_seconds=1)
    assert first.generation == 0
    with pytest.raises(LeaseConflictError):
        journal.claim_lease("exec-1", "owner-b")
    second = journal.take_over_expired_lease("exec-1", "owner-b", now=first.expires_at)
    assert second.generation == first.generation + 1
    # 旧 lease（generation 0）写入被 fencing
    with pytest.raises(StaleLeaseError):
        journal.complete_operation(_commit_op("k"), "plan_x", lease=first)
    # 新 lease 可写
    assert journal.complete_operation(_commit_op("k2"), "plan_x", lease=second) == 1
    journal.close()


def test_complete_operation_is_idempotent(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "s.db")
    execution_id = journal.create_execution()["execution_id"]
    _seed_budget(journal)
    lease = journal.claim_lease(execution_id, "owner-a")
    op = _commit_op("search:round:1:commit")
    journal.save_plan(_plan(journal, execution_id, [op]))
    version = journal.complete_operation(
        op,
        "plan_x",
        lease=lease,
        facts={"sota_experiment_ref": "sha256:s"},
        search_experiment=True,
    )
    # 幂等重放：返回首次版本，不重复提交/消费
    replay = journal.complete_operation(
        op,
        "plan_x",
        lease=lease,
        facts={"sota_experiment_ref": "sha256:DIFFERENT"},
        search_experiment=True,
    )
    assert replay == version
    assert journal.read_budget(execution_id)["search_experiments_used"] == 1
    assert journal.get_fact("sota_experiment_ref") == "sha256:s"
    journal.close()


def test_reserve_dispatch_is_reconcilable(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "s.db")
    execution_id = journal.create_execution()["execution_id"]
    lease = journal.claim_lease(execution_id, "owner-a")
    first = journal.reserve_dispatch("plan:1:init", "init", "sha256:p", lease=lease)
    assert first["status"] == "RESERVED"
    assert first["agent_id"] is None
    # 未完成派发：再次 reserve 仍无身份（可重新 spawn）
    again = journal.reserve_dispatch("plan:1:init", "init", "sha256:p", lease=lease)
    assert again["agent_id"] is None
    journal.complete_dispatch("plan:1:init", "agent_x", "run_x", lease=lease)
    # 已派发：恢复时认领，不再 spawn
    claimed = journal.reserve_dispatch("plan:1:init", "init", "sha256:p", lease=lease)
    assert claimed["agent_id"] == "agent_x"
    assert claimed["run_id"] == "run_x"
    journal.close()


def test_append_and_read_events(tmp_path: Path) -> None:
    journal = PlanJournal(tmp_path / "s.db")
    execution_id = journal.create_execution()["execution_id"]
    journal.append_event(execution_id, "plan/started", {"plan_id": "p"})
    journal.append_event(execution_id, "plan/completed", {"plan_id": "p"})
    events = journal.read_events(execution_id)
    assert [event["kind"] for event in events] == ["plan/started", "plan/completed"]
    assert [event["seq"] for event in events] == [1, 2]
    journal.close()
