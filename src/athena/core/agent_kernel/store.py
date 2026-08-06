"""AgentGraphStore — 控制面事实的线性化点（设计 §3.1、§3.7）。

只追加 journal + 派生索引。序列器单线程保证事务原子性；重试同一
``command_id`` 只取原结果。首版内存实现，§4.7 SQLite 后续替换。
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentId,
    AgentMessage,
    AgentPath,
    AgentSnapshot,
    AgentSpec,
    AgentStatus,
    CommandId,
    RunId,
    RunStatus,
    RunSummary,
)


@dataclass
class AgentRecord:
    """Agent 的派生状态记录。"""

    agent_id: AgentId
    path: AgentPath
    name: str
    role: str
    parent_id: AgentId | None
    status: AgentStatus
    spec: AgentSpec
    created_sequence: int
    pending_run_id: RunId | None = None


@dataclass
class RunRecord:
    """一次执行实例的派生状态。generation 标识执行实例（§3.2 CAS）。"""

    run_id: RunId
    agent_id: AgentId
    parent_run_id: RunId | None
    status: RunStatus
    generation: int
    request_ref: str
    response_ref: str | None = None
    error: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class JournalRecord:
    """树内单调 sequence 的一条生命周期记录。"""

    sequence: int
    command_id: CommandId
    kind: str
    payload: dict[str, Any]


@dataclass
class OutboxRecord:
    """子 Run 完成通知。以 child_run_id 去重（§3.4）。"""

    child_run_id: RunId
    child_agent_id: AgentId
    parent_agent_id: AgentId
    summary: RunSummary


@dataclass(frozen=True)
class CommandResult:
    """幂等命令结果。error 保存异常实例以便重试时原样重抛。"""

    value: Any = None
    error: BaseException | None = None


@dataclass(frozen=True)
class StoreSnapshot:
    """crash recovery 的基础状态（§3.7）。"""

    sequence: int
    agents: dict[AgentId, AgentRecord]
    runs: dict[RunId, RunRecord]
    mailboxes: dict[AgentId, list[AgentMessage]]
    mailbox_committed: dict[AgentId, int]
    outbox: dict[RunId, OutboxRecord]
    command_results: dict[CommandId, CommandResult]
    applied: dict[CommandId, tuple[int, str]]


class AgentGraphStore:
    """只追加 journal 的线性化存储。生命周期事实在此提交。"""

    def __init__(self) -> None:
        self._sequence = 0
        self._journal: list[JournalRecord] = []
        self._agents: dict[AgentId, AgentRecord] = {}
        self._runs: dict[RunId, RunRecord] = {}
        self._mailboxes: dict[AgentId, list[AgentMessage]] = {}
        self._mailbox_committed: dict[AgentId, int] = {}
        self._outbox: dict[RunId, OutboxRecord] = {}
        self._command_results: dict[CommandId, CommandResult] = {}
        # command_id -> (应用 sequence, 命令指纹)；指纹用于识别 ID 复用冲突（B3）
        self._applied: dict[CommandId, tuple[int, str]] = {}

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def journal(self) -> list[JournalRecord]:
        return list(self._journal)

    def record_result(self, command_id: CommandId, result: CommandResult) -> None:
        """记录幂等命令结果。"""
        self._command_results[command_id] = result

    def result_for(self, command_id: CommandId) -> CommandResult | None:
        return self._command_results.get(command_id)

    @staticmethod
    def _fingerprint(kind: str, payload: dict[str, Any]) -> str:
        """命令的规范化指纹：同 command_id 重试须一致，否则视为 ID 复用冲突（B3）。"""
        return hashlib.sha256(repr((kind, payload)).encode()).hexdigest()

    def commit(
        self, *, command_id: CommandId, kind: str, payload: dict[str, Any]
    ) -> int:
        """追加一条 journal 记录并应用派生状态，返回其 sequence。

        原子性：``_apply`` 先校验后变更，失败时零状态变更、不写 journal（B1）。
        幂等：同一 ``command_id`` 且指纹一致时只应用一次，重试返回原 sequence；
        指纹不一致视为 ID 复用冲突。CAS loser（终态已定 / 过期 generation）不推进
        sequence、不写 journal，但标记已处理（B2）。
        """
        fingerprint = self._fingerprint(kind, payload)
        applied = self._applied.get(command_id)
        if applied is not None:
            if applied[1] != fingerprint:
                raise ValueError(
                    f"command {command_id} reused with a different payload"
                )
            return applied[0]
        if not self._apply(kind, payload):
            # 无状态变更（CAS loser / no-op）→ 不推进 sequence，不写 journal
            self._applied[command_id] = (self._sequence, fingerprint)
            return self._sequence
        seq = self._sequence + 1
        self._journal.append(JournalRecord(seq, command_id, kind, payload))
        self._sequence = seq
        self._applied[command_id] = (seq, fingerprint)
        return seq

    def agent(self, agent_id: AgentId) -> AgentRecord | None:
        return self._agents.get(agent_id)

    def agents(self) -> dict[AgentId, AgentRecord]:
        return dict(self._agents)

    def run(self, run_id: RunId) -> RunRecord | None:
        return self._runs.get(run_id)

    def runs(self) -> dict[RunId, RunRecord]:
        return dict(self._runs)

    def mailbox(self, agent_id: AgentId) -> list[AgentMessage]:
        return self._mailboxes.setdefault(agent_id, [])

    def mailbox_committed(self, agent_id: AgentId) -> int:
        return self._mailbox_committed.get(agent_id, 0)

    def outbox_for(self, child_run_id: RunId) -> OutboxRecord | None:
        return self._outbox.get(child_run_id)

    def run_summary(self, run_id: RunId) -> RunSummary | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return RunSummary(
            run_id=run.run_id,
            agent_id=run.agent_id,
            status=run.status,
            response_ref=run.response_ref,
            error=run.error,
            reason=run.reason,
        )

    def agent_snapshot(self, agent_id: AgentId) -> AgentSnapshot | None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return AgentSnapshot(
            agent_id=agent.agent_id,
            path=agent.path,
            name=agent.name,
            role=agent.role,
            status=agent.status,
            parent_id=agent.parent_id,
            pending_run_id=agent.pending_run_id,
        )

    def _apply(self, kind: str, payload: dict[str, Any]) -> bool:
        """应用一条命令。所有查找/校验先于任何变更；返回是否产生状态变更。

        失败时零状态变更（原子提交，B1）；CAS loser 返回 False（B2）。
        """
        if kind == "spawn":
            agent = payload["agent"]
            run = payload["run"]
            self._agents[agent.agent_id] = agent
            self._runs[run.run_id] = run
            self._mailboxes.setdefault(agent.agent_id, [])
            self._mailbox_committed.setdefault(agent.agent_id, 0)
            return True
        elif kind == "run_queued":
            run = payload["run"]
            agent = self.require_agent(run.agent_id)
            self._runs[run.run_id] = run
            agent.pending_run_id = run.run_id
            return True
        elif kind == "run_running":
            run_id = payload["run_id"]
            generation = payload["generation"]
            run = self.require_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return False  # 终态 Run 不可重开（B2）
            agent = self.require_agent(run.agent_id)
            run.status = RunStatus.RUNNING
            run.generation = generation
            agent.status = AgentStatus.RUNNING
            agent.pending_run_id = run.run_id
            return True
        elif kind == "run_terminal":
            run_id = payload["run_id"]
            run = self.require_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return False  # CAS：终态 first-writer-wins（B2）
            generation = payload.get("generation", run.generation)
            if generation < run.generation:
                return False  # 过期 generation → 不写入（B2）
            outbox = payload.get("outbox")
            if outbox is not None and not hasattr(outbox, "child_run_id"):
                raise ValueError("malformed outbox payload")
            agent = self.require_agent(run.agent_id)
            run.status = payload["status"]
            run.generation = generation
            run.response_ref = payload.get("response_ref")
            run.error = payload.get("error")
            run.reason = payload.get("reason")
            agent.pending_run_id = None
            if agent.status == AgentStatus.RUNNING:
                agent.status = AgentStatus.IDLE
            if outbox is not None:
                self._outbox[outbox.child_run_id] = outbox
            return True
        elif kind == "mailbox":
            agent_id = payload["agent_id"]
            message = payload["message"]
            self._mailboxes.setdefault(agent_id, []).append(message)
            return True
        elif kind == "mailbox_committed":
            agent_id = payload["agent_id"]
            committed = payload.get("committed", len(self.mailbox(agent_id)))
            current = self._mailbox_committed.get(agent_id, 0)
            self._mailbox_committed[agent_id] = max(current, committed)  # 单调（B7）
            return True
        elif kind == "outbox":
            record = payload["record"]
            self._outbox[record.child_run_id] = record
            return True
        elif kind == "agent_closed":
            agent = self.require_agent(payload["agent_id"])
            agent.status = AgentStatus.CLOSED
            return True
        elif kind == "agent_error":
            agent = self.require_agent(payload["agent_id"])
            agent.status = AgentStatus.ERROR
            return True
        raise ValueError(f"unknown journal kind: {kind}")

    def require_run(self, run_id: RunId) -> RunRecord:
        """返回 Run；不存在则抛 KeyError（用于调用方已确认存在的路径）。"""
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        return run

    def require_agent(self, agent_id: AgentId) -> AgentRecord:
        """返回 Agent；不存在则抛 KeyError（用于调用方已确认存在的路径）。"""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return agent

    def snapshot(self) -> StoreSnapshot:
        """导出当前状态；记录重建为新对象，不受后续变更影响。"""
        return StoreSnapshot(
            sequence=self._sequence,
            agents={aid: AgentRecord(**vars(a)) for aid, a in self._agents.items()},
            runs={rid: RunRecord(**vars(r)) for rid, r in self._runs.items()},
            mailboxes={k: list(v) for k, v in self._mailboxes.items()},
            mailbox_committed=dict(self._mailbox_committed),
            outbox={k: OutboxRecord(**vars(v)) for k, v in self._outbox.items()},
            command_results=dict(self._command_results),
            applied=dict(self._applied),
        )

    def load(self, snapshot: StoreSnapshot, journal: list[JournalRecord]) -> None:
        """以快照为基础，重放 sequence 大于快照序列的 journal。"""
        self._sequence = max(
            snapshot.sequence,
            max((r.sequence for r in journal), default=0),
        )
        self._agents = dict(snapshot.agents)
        self._runs = dict(snapshot.runs)
        self._mailboxes = {k: list(v) for k, v in snapshot.mailboxes.items()}
        self._mailbox_committed = dict(snapshot.mailbox_committed)
        self._outbox = dict(snapshot.outbox)
        self._command_results = dict(snapshot.command_results)
        self._journal = list(journal)
        # 快照前已应用的命令（含指纹）恢复幂等账本，journal 只补快照后的（B3）
        self._applied = dict(snapshot.applied)
        for record in sorted(journal, key=lambda r: r.sequence):
            if record.sequence > snapshot.sequence:
                self._apply(record.kind, record.payload)
                self._applied[record.command_id] = (
                    record.sequence,
                    self._fingerprint(record.kind, record.payload),
                )
