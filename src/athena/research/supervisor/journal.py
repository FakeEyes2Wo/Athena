"""PlanJournal — 每个项目一个 SQLite 数据库（supervisor_design §7）。

数据库保存 execution、Plan、operation、权威事实元数据、state_version 与待发布
事件。Artifact 正文、图片和日志仍由内容寻址 ArtifactStore 保存，不写入 SQLite
大字段。Plan/operation 状态迁移、事实提交与 state_version 更新在同一事务内完成。
"""

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from athena.core.contracts import new_id
from athena.research.contracts import FinalTestAttempt
from athena.research.supervisor.models import (
    ControlStatus,
    HumanRequest,
    LeaseToken,
    OperationStatus,
    PlanStatus,
    SupervisorOperation,
    SupervisorPlan,
    utc_now,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    interaction_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    wait_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    inputs TEXT NOT NULL,
    input_refs TEXT NOT NULL,
    preconditions TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_id TEXT,
    run_id TEXT,
    result_refs TEXT NOT NULL,
    outputs TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    fact_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    state_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS human_requests (
    request_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    plan_id TEXT,
    reason_code TEXT NOT NULL,
    question TEXT NOT NULL,
    context_refs TEXT NOT NULL,
    input_mode TEXT NOT NULL,
    options TEXT NOT NULL,
    allow_free_text INTEGER NOT NULL,
    response_schema TEXT,
    default_answer TEXT,
    expires_at TEXT,
    status TEXT NOT NULL,
    answer TEXT,
    answer_source TEXT,
    requesting_agent_id TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    execution_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatched (
    dispatch_key TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_id TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS final_test_attempts (
    attempt_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    sota_experiment_id TEXT NOT NULL,
    eval_spec_version TEXT NOT NULL,
    status TEXT NOT NULL,
    sota_ref TEXT NOT NULL,
    eval_spec_ref TEXT NOT NULL,
    dataset_ref TEXT NOT NULL,
    final_test_ref TEXT,
    prediction_ref TEXT,
    score_ref TEXT,
    test_score REAL,
    final_test_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_final_test_key ON final_test_attempts (
    execution_id, sota_experiment_id, final_test_ref, eval_spec_version
);
"""


def _parse_iso(text: str) -> datetime:
    """解析 ISO 时间戳（utc_now 产出的带时区格式）。"""
    return datetime.fromisoformat(text)


# final-test 唯一状态机（Task 8）：只能前向推进，终态后不再转换。
_FINAL_TEST_FORWARD: dict[str, frozenset[str]] = {
    "RESERVED": frozenset({"RUNNING", "FAILED_UNRECOVERABLE"}),
    "RUNNING": frozenset({"PREDICTIONS_WRITTEN", "FAILED_UNRECOVERABLE"}),
    "PREDICTIONS_WRITTEN": frozenset({"SCORED", "FAILED_UNRECOVERABLE"}),
    "SCORED": frozenset({"COMMITTED", "FAILED_UNRECOVERABLE"}),
    "COMMITTED": frozenset(),
    "FAILED_UNRECOVERABLE": frozenset(),
}


class PlanJournal:
    """execution/plan/operation 状态与权威事实元数据的 SQLite 持久化。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- execution ----

    def create_execution(self, *, interaction_mode: str = "auto") -> dict[str, str]:
        """创建并返回一个活动 execution 的最小记录。"""
        execution_id = new_id("exec")
        self._conn.execute(
            "INSERT INTO executions (execution_id, interaction_mode, status, created_at)"
            " VALUES (?, ?, ?, ?)",
            (execution_id, interaction_mode, "RUNNING", utc_now()),
        )
        self._conn.commit()
        return {"execution_id": execution_id, "interaction_mode": interaction_mode}

    def execution_status(self, execution_id: str) -> ControlStatus | None:
        row = self._conn.execute(
            "SELECT status FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return ControlStatus(row["status"]) if row else None

    def set_execution_status(self, execution_id: str, status: ControlStatus) -> None:
        """改变 execution 控制状态（不投影研究阶段）。"""
        self._conn.execute(
            "UPDATE executions SET status = ? WHERE execution_id = ?",
            (status.value, execution_id),
        )
        self._conn.commit()

    # ---- plan / operation 状态机 ----

    def save_plan(
        self, plan: SupervisorPlan, *, lease: LeaseToken | None = None
    ) -> None:
        """插入或更新一份 Plan（整行覆盖，含 operations）。

        只用于首次持久化或校验失败落库；operation 状态推进用
        :meth:`advance_operation` / :meth:`complete_operation`（避免覆盖已终结 op）。
        """
        if lease is not None:
            self._assert_lease(lease)
        self._conn.execute(
            "INSERT OR REPLACE INTO plans (plan_id, execution_id, sequence,"
            " snapshot_version, reason_code, wait_policy, status, created_at,"
            " completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.plan_id,
                plan.execution_id,
                plan.sequence,
                plan.snapshot_version,
                plan.reason_code,
                plan.wait_policy,
                plan.status.value,
                plan.created_at,
                plan.completed_at,
            ),
        )
        for operation in plan.operations:
            self._conn.execute(
                "INSERT OR REPLACE INTO operations (operation_id, plan_id,"
                " operation_type, idempotency_key, inputs, input_refs,"
                " preconditions, status, agent_id, run_id, result_refs, outputs,"
                " error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operation.operation_id,
                    plan.plan_id,
                    operation.operation_type.value,
                    operation.idempotency_key,
                    json.dumps(operation.inputs, ensure_ascii=False),
                    json.dumps(operation.input_refs, ensure_ascii=False),
                    json.dumps(operation.preconditions, ensure_ascii=False),
                    operation.status.value,
                    operation.agent_id,
                    operation.run_id,
                    json.dumps(operation.result_refs, ensure_ascii=False),
                    json.dumps(operation.outputs, ensure_ascii=False),
                    operation.error,
                ),
            )
        self._conn.commit()

    def load_plan(self, plan_id: str) -> SupervisorPlan | None:
        """按 plan_id 恢复一份完整 Plan（含 operations）。"""
        plan_row = self._conn.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if plan_row is None:
            return None
        op_rows = self._conn.execute(
            "SELECT * FROM operations WHERE plan_id = ? ORDER BY rowid",
            (plan_id,),
        ).fetchall()
        return SupervisorPlan(
            plan_id=plan_row["plan_id"],
            execution_id=plan_row["execution_id"],
            sequence=plan_row["sequence"],
            snapshot_version=plan_row["snapshot_version"],
            reason_code=plan_row["reason_code"],
            wait_policy=plan_row["wait_policy"],
            status=PlanStatus(plan_row["status"]),
            created_at=plan_row["created_at"],
            completed_at=plan_row["completed_at"],
            operations=[self._operation_from_row(row) for row in op_rows],
        )

    def load_unfinished_plan(self, execution_id: str) -> SupervisorPlan | None:
        """恢复该 execution 未终结的 Plan（优先级：pending > running）。"""
        for status in (PlanStatus.PENDING, PlanStatus.RUNNING):
            row = self._conn.execute(
                "SELECT plan_id FROM plans WHERE execution_id = ? AND status = ?"
                " ORDER BY sequence DESC LIMIT 1",
                (execution_id, status.value),
            ).fetchone()
            if row is not None:
                return self.load_plan(row["plan_id"])
        return None

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> SupervisorOperation:
        return SupervisorOperation(
            operation_id=row["operation_id"],
            operation_type=row["operation_type"],
            idempotency_key=row["idempotency_key"],
            inputs=json.loads(row["inputs"]),
            input_refs=json.loads(row["input_refs"]),
            preconditions=json.loads(row["preconditions"]),
            status=OperationStatus(row["status"]),
            agent_id=row["agent_id"],
            run_id=row["run_id"],
            result_refs=json.loads(row["result_refs"]),
            outputs=json.loads(row["outputs"]),
            error=row["error"],
        )

    # ---- 权威事实 ----

    def snapshot_version(self) -> int:
        """当前 state_version（从未提交版本 0 起单调递增）。"""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'snapshot_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def commit_facts(
        self,
        facts: dict[str, object],
        expected_version: int,
    ) -> int:
        """CAS 提交事实；state_version 不匹配时抛 ``VersionConflict``。"""
        current = self.snapshot_version()
        if current != expected_version:
            raise VersionConflict(current, expected_version)
        next_version = current + 1
        for key, value in facts.items():
            self._conn.execute(
                "INSERT INTO facts (fact_key, value, state_version) VALUES (?, ?, ?)"
                " ON CONFLICT(fact_key) DO UPDATE SET value = excluded.value,"
                " state_version = excluded.state_version",
                (key, json.dumps(value, ensure_ascii=False), next_version),
            )
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('snapshot_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(next_version),),
        )
        self._conn.commit()
        return next_version

    def get_fact(self, key: str) -> object | None:
        row = self._conn.execute(
            "SELECT value FROM facts WHERE fact_key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def all_facts(self) -> dict[str, object]:
        rows = self._conn.execute("SELECT fact_key, value FROM facts").fetchall()
        return {row["fact_key"]: json.loads(row["value"]) for row in rows}

    # ---- 人工请求 ----

    def save_human_request(self, request: HumanRequest) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO human_requests (request_id, execution_id, plan_id,"
            " reason_code, question, context_refs, input_mode, options,"
            " allow_free_text, response_schema, default_answer, expires_at, status,"
            " answer, answer_source, requesting_agent_id, created_at, answered_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.request_id,
                request.execution_id,
                request.plan_id,
                request.reason_code,
                request.question,
                json.dumps(request.context_refs, ensure_ascii=False),
                request.input_mode,
                json.dumps(request.options, ensure_ascii=False),
                int(request.allow_free_text),
                (
                    json.dumps(request.response_schema, ensure_ascii=False)
                    if request.response_schema is not None
                    else None
                ),
                request.default_answer,
                request.expires_at,
                request.status,
                request.answer,
                request.answer_source,
                request.requesting_agent_id,
                request.created_at,
                request.answered_at,
            ),
        )
        self._conn.commit()

    def open_human_requests(self, execution_id: str) -> list[HumanRequest]:
        rows = self._conn.execute(
            "SELECT * FROM human_requests WHERE execution_id = ? AND status = 'OPEN'"
            " ORDER BY created_at",
            (execution_id,),
        ).fetchall()
        return [self._request_from_row(row) for row in rows]

    def human_request(self, request_id: str) -> HumanRequest | None:
        row = self._conn.execute(
            "SELECT * FROM human_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return self._request_from_row(row) if row else None

    def answer_human_request(self, request_id: str, answer: str, source: str) -> None:
        self._conn.execute(
            "UPDATE human_requests SET status = 'ANSWERED', answer = ?,"
            " answer_source = ?, answered_at = ? WHERE request_id = ?",
            (answer, source, utc_now(), request_id),
        )
        self._conn.commit()

    # ---- lease / fencing（写锁，防并发与崩溃恢复双重写入） ----

    def claim_lease(
        self, execution_id: str, owner_id: str, *, ttl_seconds: int = 60
    ) -> LeaseToken:
        """获取或续租 execution 写锁；他人持有有效锁抛 ``LeaseConflictError``。

        过期锁自动 fencing（generation +1），旧 owner 的写入立即失效。
        """
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        row = self._conn.execute(
            "SELECT * FROM leases WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO leases (execution_id, owner_id, generation, expires_at)"
                " VALUES (?, ?, 0, ?)",
                (execution_id, owner_id, expires),
            )
            self._conn.commit()
            return LeaseToken(
                execution_id=execution_id,
                owner_id=owner_id,
                generation=0,
                expires_at=expires,
            )
        if row["owner_id"] == owner_id:
            self._conn.execute(
                "UPDATE leases SET expires_at = ? WHERE execution_id = ?",
                (expires, execution_id),
            )
            self._conn.commit()
            return LeaseToken(
                execution_id=execution_id,
                owner_id=owner_id,
                generation=row["generation"],
                expires_at=expires,
            )
        if _parse_iso(row["expires_at"]) > now:
            raise LeaseConflictError(
                f"lease for {execution_id} held by {row['owner_id']}"
                f" until {row['expires_at']}"
            )
        return self.take_over_expired_lease(execution_id, owner_id, now=now.isoformat())

    def take_over_expired_lease(
        self, execution_id: str, owner_id: str, *, now: str | None = None
    ) -> LeaseToken:
        """接管已过期的 lease：generation +1，旧 owner 的所有写入被 fencing。"""
        current = self._conn.execute(
            "SELECT generation FROM leases WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        generation = (current["generation"] if current else 0) + 1
        base = _parse_iso(now) if now is not None else datetime.now(timezone.utc)
        expires = (base + timedelta(seconds=60)).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO leases (execution_id, owner_id, generation,"
            " expires_at) VALUES (?, ?, ?, ?)",
            (execution_id, owner_id, generation, expires),
        )
        self._conn.commit()
        return LeaseToken(
            execution_id=execution_id,
            owner_id=owner_id,
            generation=generation,
            expires_at=expires,
        )

    def heartbeat(self, lease: LeaseToken, *, ttl_seconds: int = 60) -> LeaseToken:
        """续租并校验 ownership；lease 已失效则抛 ``StaleLeaseError``。"""
        self._assert_lease(lease)
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        self._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE execution_id = ?",
            (expires, lease.execution_id),
        )
        self._conn.commit()
        return LeaseToken(
            execution_id=lease.execution_id,
            owner_id=lease.owner_id,
            generation=lease.generation,
            expires_at=expires,
        )

    def _assert_lease(self, lease: LeaseToken) -> None:
        """校验 lease 仍为当前 owner 且未过期；否则抛 ``StaleLeaseError``。"""
        row = self._conn.execute(
            "SELECT * FROM leases WHERE execution_id = ?", (lease.execution_id,)
        ).fetchone()
        if row is None:
            raise StaleLeaseError(f"no lease for {lease.execution_id}")
        if row["owner_id"] != lease.owner_id or row["generation"] != lease.generation:
            raise StaleLeaseError(
                f"stale lease: expected owner={lease.owner_id} gen={lease.generation},"
                f" got owner={row['owner_id']} gen={row['generation']}"
            )
        if _parse_iso(row["expires_at"]) <= datetime.now(timezone.utc):
            raise StaleLeaseError(f"lease for {lease.execution_id} expired")

    # ---- dispatch 对账（防止崩溃恢复后重复 spawn） ----

    def reserve_dispatch(
        self,
        dispatch_key: str,
        agent_type: str,
        payload_ref: str,
        *,
        lease: LeaseToken,
    ) -> dict[str, object]:
        """预留一次外部派发；幂等。返回当前派发记录。

        记录已含 agent_id/run_id（上次已派发）→ 调用方据其认领，不再 spawn；
        否则返回 RESERVED 记录，调用方 spawn 后调 :meth:`complete_dispatch` 写回身份。
        """
        self._assert_lease(lease)
        self._conn.execute(
            "INSERT OR IGNORE INTO dispatched (dispatch_key, agent_type, payload_ref,"
            " status, created_at) VALUES (?, ?, ?, 'RESERVED', ?)",
            (dispatch_key, agent_type, payload_ref, utc_now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM dispatched WHERE dispatch_key = ?", (dispatch_key,)
        ).fetchone()
        return dict(row)

    def complete_dispatch(
        self,
        dispatch_key: str,
        agent_id: str,
        run_id: str,
        *,
        lease: LeaseToken,
    ) -> None:
        """写回已 spawn 的 agent 身份（RESERVED → DISPATCHED），仅允许一次。"""
        self._assert_lease(lease)
        self._conn.execute(
            "UPDATE dispatched SET status = 'DISPATCHED', agent_id = ?, run_id = ?"
            " WHERE dispatch_key = ? AND status = 'RESERVED'",
            (agent_id, run_id, dispatch_key),
        )
        self._conn.commit()

    # ---- operation / plan 状态推进（定向更新，禁止覆盖已终结 operation） ----

    def set_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        *,
        lease: LeaseToken,
        completed_at: str | None = None,
    ) -> None:
        """推进 Plan 状态；已终结的 Plan 保持终态，不回退。"""
        self._assert_lease(lease)
        prior = self._conn.execute(
            "SELECT status FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if prior is not None and prior["status"] in (
            PlanStatus.COMPLETED.value,
            PlanStatus.FAILED.value,
            PlanStatus.CANCELLED.value,
        ):
            return
        self._conn.execute(
            "UPDATE plans SET status = ?, completed_at = ? WHERE plan_id = ?",
            (status.value, completed_at, plan_id),
        )
        self._conn.commit()

    def advance_operation(
        self,
        operation: SupervisorOperation,
        plan_id: str,
        *,
        lease: LeaseToken,
        status: OperationStatus | None = None,
        error: str | None = None,
    ) -> None:
        """持久化 operation 状态与 outputs；已终结的 operation 保持终态。"""
        self._assert_lease(lease)
        if status is not None:
            operation.status = status
        prior = self._conn.execute(
            "SELECT status FROM operations WHERE operation_id = ? AND plan_id = ?",
            (operation.operation_id, plan_id),
        ).fetchone()
        if prior is not None and prior["status"] in (
            OperationStatus.SUCCEEDED.value,
            OperationStatus.FAILED.value,
        ):
            return
        self._conn.execute(
            "UPDATE operations SET status = ?, outputs = ?, error = ?"
            " WHERE operation_id = ? AND plan_id = ?",
            (
                operation.status.value,
                json.dumps(operation.outputs, ensure_ascii=False),
                error,
                operation.operation_id,
                plan_id,
            ),
        )
        self._conn.commit()

    def complete_operation(
        self,
        operation: SupervisorOperation,
        plan_id: str,
        *,
        lease: LeaseToken,
        facts: dict[str, object] | None = None,
        search_experiment: bool = False,
    ) -> int:
        """单事务内幂等完成 operation：状态→SUCCEEDED、提交 facts、消费预算、推进 version。

        同一 operation（按 idempotency_key）重复调用返回首次 state_version，
        不重复提交事实或消费预算。lease 无效或过期 → ``StaleLeaseError``。
        """
        self._assert_lease(lease)
        idem_key = operation.idempotency_key
        prior = self._conn.execute(
            "SELECT value FROM facts WHERE fact_key = ?",
            (f"idempotency::{idem_key}",),
        ).fetchone()
        if prior is not None:
            return int(prior["value"])  # 幂等重放：返回首次版本
        next_version = self.snapshot_version() + 1
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "UPDATE operations SET status = ?, outputs = ?"
                " WHERE operation_id = ? AND plan_id = ?",
                (
                    OperationStatus.SUCCEEDED.value,
                    json.dumps(operation.outputs, ensure_ascii=False),
                    operation.operation_id,
                    plan_id,
                ),
            )
            for key, value in (facts or {}).items():
                self._conn.execute(
                    "INSERT INTO facts (fact_key, value, state_version) VALUES (?, ?, ?)"
                    " ON CONFLICT(fact_key) DO UPDATE SET value = excluded.value,"
                    " state_version = excluded.state_version",
                    (key, json.dumps(value, ensure_ascii=False), next_version),
                )
            self._conn.execute(
                "INSERT INTO facts (fact_key, value, state_version) VALUES (?, ?, ?)",
                (f"idempotency::{idem_key}", str(next_version), next_version),
            )
            if search_experiment:
                self._apply_budget_locked(search_experiment=True, version=next_version)
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('snapshot_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(next_version),),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        operation.status = OperationStatus.SUCCEEDED
        return next_version

    def _apply_budget_locked(self, *, search_experiment: bool, version: int) -> None:
        """在打开的显式事务内递增预算计数（必须与 complete_operation 同事务）。"""
        raw = self.get_fact("budget")
        budget = raw if isinstance(raw, dict) else {}
        if search_experiment:
            budget["search_experiments_used"] = (
                int(budget.get("search_experiments_used", 0)) + 1
            )
        self._conn.execute(
            "INSERT INTO facts (fact_key, value, state_version) VALUES ('budget', ?, ?)"
            " ON CONFLICT(fact_key) DO UPDATE SET value = excluded.value,"
            " state_version = excluded.state_version",
            (json.dumps(budget, ensure_ascii=False), version),
        )

    def read_budget(self, execution_id: str) -> dict[str, int]:
        """读取 execution 的预算事实（plans_used / search_experiments_used 等）。"""
        raw = self.get_fact("budget")
        if not isinstance(raw, dict):
            return {}
        return {
            key: int(value)
            for key, value in raw.items()
            if isinstance(value, (int, float))
        }

    # ---- durable outbox 事件 ----

    def append_event(
        self, execution_id: str, kind: str, data: dict[str, object]
    ) -> None:
        """追加一条 durable outbox 事件（已提交事实不受影响）。"""
        self._conn.execute(
            "INSERT INTO events (execution_id, kind, data, created_at)"
            " VALUES (?, ?, ?, ?)",
            (execution_id, kind, json.dumps(data, ensure_ascii=False), utc_now()),
        )
        self._conn.commit()

    def read_events(
        self, execution_id: str, after_seq: int = 0
    ) -> list[dict[str, object]]:
        """读取 execution 的待发布事件（seq 递增）。"""
        rows = self._conn.execute(
            "SELECT seq, kind, data, created_at FROM events"
            " WHERE execution_id = ? AND seq > ? ORDER BY seq",
            (execution_id, after_seq),
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "data": json.loads(row["data"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---- execution 辅助（供 StateStore / Planner 使用，避免外泄 _conn） ----

    def active_execution_id(self) -> str | None:
        """当前活动 execution id（RUNNING/PAUSED/WAITING_FOR_HUMAN）；无则返回 None。

        不再隐式创建——创建 execution 是 RUN/TASK_CONFIGURE 的显式生命周期命令。
        """
        row = self._conn.execute(
            "SELECT execution_id FROM executions WHERE status IN ('RUNNING',"
            " 'PAUSED', 'WAITING_FOR_HUMAN') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["execution_id"] if row else None

    def latest_execution_id(self) -> str | None:
        """最近一次 execution id（任意状态）；无任何 execution 返回 None。"""
        row = self._conn.execute(
            "SELECT execution_id FROM executions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["execution_id"] if row else None

    def interaction_mode(self, execution_id: str) -> str:
        """execution 的交互模式（interactive/auto）；缺失时默认 interactive。"""
        row = self._conn.execute(
            "SELECT interaction_mode FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return row["interaction_mode"] if row else "interactive"

    # ---- final-test 唯一状态机（supervisor_design §2.5 / Task 8） ----

    def reserve_final_test(
        self,
        *,
        execution_id: str,
        sota_experiment_id: str,
        sota_ref: str,
        eval_spec_ref: str,
        dataset_ref: str,
        final_test_ref: str,
        eval_spec_version: str = "1",
        lease: LeaseToken,
    ) -> FinalTestAttempt:
        """预留唯一 final-test attempt（幂等 + 数据库唯一约束，按唯一键复用）。

        唯一键为 ``(execution_id, sota_experiment_id, final_test_ref,
        eval_spec_version)``，由 ``uq_final_test_key`` UNIQUE 索引在数据库层强制。
        冻结的 SOTA/EvalSpec/dataset refs 与 attempt 在同一 INSERT 事务内落库
        （design §2.5）。lease owner/未过期/generation 匹配与 INSERT 在同一个
        ``BEGIN IMMEDIATE`` 原子事务内：立即持有写锁，阻断并发接管在"检查后、
        写入前"插入窗口（fencing 不失效）。并发或崩溃恢复后重新 reserve 返回
        同一 attempt，不产生第二个逻辑尝试；若同一唯一键已被不同的冻结 refs
        预留，直接拒绝。
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_lease(lease)
            attempt_id = new_id("ft")
            now = utc_now()
            self._conn.execute(
                "INSERT OR IGNORE INTO final_test_attempts (attempt_id, execution_id,"
                " sota_experiment_id, eval_spec_version, status, sota_ref,"
                " eval_spec_ref, dataset_ref, final_test_ref, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    execution_id,
                    sota_experiment_id,
                    eval_spec_version,
                    sota_ref,
                    eval_spec_ref,
                    dataset_ref,
                    final_test_ref,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM final_test_attempts WHERE execution_id = ?"
                " AND sota_experiment_id = ? AND final_test_ref = ?"
                " AND eval_spec_version = ?",
                (execution_id, sota_experiment_id, final_test_ref, eval_spec_version),
            ).fetchone()
            if (
                row["sota_ref"] != sota_ref
                or row["eval_spec_ref"] != eval_spec_ref
                or row["dataset_ref"] != dataset_ref
            ):
                raise ValueError(
                    "final-test key already reserved with different frozen refs"
                    f" (sota={row['sota_ref']}/{sota_ref},"
                    f" eval_spec={row['eval_spec_ref']}/{eval_spec_ref},"
                    f" dataset={row['dataset_ref']}/{dataset_ref})"
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self._attempt_from_row(row)

    def advance_final_test(
        self,
        attempt_id: str,
        expected_status: str,
        *,
        lease: LeaseToken,
        prediction_ref: str | None = None,
        score_ref: str | None = None,
        test_score: float | None = None,
        final_test_score: float | None = None,
    ) -> FinalTestAttempt:
        """按前向状态机推进 final-test（原子 lease fencing + SQL compare-and-set）。

        回退或非法转换直接拒绝；已有非空 score 不可被不同非空值覆盖（崩溃恢复
        只向前提交，不重训/重预测/重评分）。状态写入用 ``UPDATE ... WHERE status
        = ?`` 的 CAS：并发 writer 已推进时 rowcount 为 0，抛 ``StaleLeaseError``
        阻止双重提交。lease owner/未过期/generation 匹配与 UPDATE 在同一个
        ``BEGIN IMMEDIATE`` 原子事务内，fencing 检查与写入之间不存在并发接管
        窗口。prediction/score Artifact refs 在迁移时保存（COALESCE 保留已有值）。
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_lease(lease)
            row = self._conn.execute(
                "SELECT * FROM final_test_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown final-test attempt: {attempt_id}")
            current = row["status"]
            allowed = _FINAL_TEST_FORWARD.get(current, frozenset())
            if expected_status not in allowed:
                raise ValueError(
                    f"invalid final-test transition: {current} -> {expected_status}"
                )
            test_score, final_test_score = self._reconcile_scores(
                row, test_score, final_test_score
            )
            updated = self._conn.execute(
                "UPDATE final_test_attempts SET status = ?, prediction_ref ="
                " COALESCE(?, prediction_ref), score_ref = COALESCE(?, score_ref),"
                " test_score = COALESCE(?, test_score),"
                " final_test_score = COALESCE(?, final_test_score),"
                " updated_at = ? WHERE attempt_id = ? AND status = ?",
                (
                    expected_status,
                    prediction_ref,
                    score_ref,
                    test_score,
                    final_test_score,
                    utc_now(),
                    attempt_id,
                    current,
                ),
            )
            if updated.rowcount == 0:
                raise StaleLeaseError(
                    f"final-test {attempt_id} already advanced by another writer"
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self._attempt_from_row(
            self._conn.execute(
                "SELECT * FROM final_test_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        )

    @staticmethod
    def _reconcile_scores(
        row: sqlite3.Row,
        test_score: float | None,
        final_test_score: float | None,
    ) -> tuple[float | None, float | None]:
        """已有非空 score 不可被不同非空值覆盖；相同值按幂等恢复置空以只向前提交。"""
        resolved = []
        for column, proposed in (
            ("test_score", test_score),
            ("final_test_score", final_test_score),
        ):
            persisted = row[column]
            if proposed is None:
                resolved.append(None)
            elif persisted is None:
                resolved.append(proposed)
            elif math.isclose(persisted, proposed, rel_tol=1e-9, abs_tol=1e-12):
                resolved.append(None)  # 相同：只向前提交，不重写
            else:
                raise ValueError(
                    f"refusing to overwrite persisted {column}={persisted}"
                    f" with {proposed}"
                )
        return resolved[0], resolved[1]

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> FinalTestAttempt:
        return FinalTestAttempt(
            attempt_id=row["attempt_id"],
            execution_id=row["execution_id"],
            sota_experiment_id=row["sota_experiment_id"],
            eval_spec_version=row["eval_spec_version"],
            status=row["status"],
            sota_ref=row["sota_ref"],
            eval_spec_ref=row["eval_spec_ref"],
            dataset_ref=row["dataset_ref"],
            final_test_ref=row["final_test_ref"],
            prediction_ref=row["prediction_ref"],
            score_ref=row["score_ref"],
            test_score=row["test_score"],
            final_test_score=row["final_test_score"],
        )

    def next_sequence(self, execution_id: str) -> int:
        """返回该 execution 的下一个 Plan sequence。"""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM plans"
            " WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return int(row["seq"])

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> HumanRequest:
        return HumanRequest(
            request_id=row["request_id"],
            execution_id=row["execution_id"],
            plan_id=row["plan_id"],
            reason_code=row["reason_code"],
            question=row["question"],
            context_refs=json.loads(row["context_refs"]),
            input_mode=row["input_mode"],
            options=json.loads(row["options"]),
            allow_free_text=bool(row["allow_free_text"]),
            response_schema=(
                json.loads(row["response_schema"]) if row["response_schema"] else None
            ),
            default_answer=row["default_answer"],
            expires_at=row["expires_at"],
            status=row["status"],
            answer=row["answer"],
            answer_source=row["answer_source"],
            requesting_agent_id=row["requesting_agent_id"],
            created_at=row["created_at"],
            answered_at=row["answered_at"],
        )


class VersionConflict(ValueError):
    """事实 CAS 提交时 state_version 已变化。"""

    def __init__(self, current: int, expected: int) -> None:
        super().__init__(
            f"state_version conflict: current={current}, expected={expected}"
        )
        self.current = current
        self.expected = expected


class LeaseConflictError(RuntimeError):
    """execution 写锁被其他未过期 owner 持有。"""


class StaleLeaseError(RuntimeError):
    """写入所用 lease 非当前 owner、generation 已推进或已过期。"""
