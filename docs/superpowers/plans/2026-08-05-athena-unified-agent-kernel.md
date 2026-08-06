# Athena 统一 Agent 内核实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现已裁可设计（2026-08-05 版）的**内核核心**：§1-§3 全量 + §4.5 错误层 + §4.4 默认运行配置 + §2.3/§5.1 `handle.events()` 跨 Run journal 读取。以单一命令序列器承载整棵根 Agent 树的生命周期事务、状态机、mailbox/followup、终态 CAS、父子通知、interrupt/close、wait/PARKED 与 crash recovery。

**Architecture:** 新建独立包 `src/athena/core/agent_kernel/`（5 个源文件：`types / store / session / kernel / control`），与现有 `core/agent/` 并行，不迁移不删除旧实现（§5 迁移后续）。所有命令经一个 asyncio 队列单任务处理，线性化点为 `AgentGraphStore.commit`；模型执行在序列器外运行，完成后以 `run_finished` 命令回送。store 首版用内存实现（§4.7 SQLite 列为后续）。

**Tech Stack:** Python 3.11+、`asyncio`、dataclasses、`typing.Protocol`、既有 `ContextManager`、pytest + pytest-asyncio。

## Global Constraints

- Python `>=3.11`；不新增第三方依赖；单行 ≤ 88 列（black）；类型注解齐全；遵循 `docs/代码规范.md`。
- **代码规范要点**：导入全部在文件头（标准库→第三方→项目内）；**不用 `from __future__ import annotations`**（前向引用用字符串注解）；不用装饰性分隔线；`except` 必须注释触发场景；不滥用 try/except；嵌套 ≤ 3 层；参数与类 attr 尽量少；减少多余的 `_` 私有函数。
- **范围**：只实现内核核心。`§4.1` 能力模型、`§4.7` SQLite、`§4.3` BudgetConfig、`§4.6` fork 继承、`§4.2` 限流与 `§5` 迁移均为后续，不在本计划。
- 新代码只写入 `src/athena/core/agent_kernel/` 与 `test/unit/agent_kernel/`。**不得修改** `core/agent/`、`app_server/`、`ideator/`。
- 测试命令统一 `uv run pytest test/unit/agent_kernel/<file>::<test> -q`。
- 只在本机 `main` checkout 提交，不 push、不切分支。

**默认运行配置（§4.4）**：`max_agents=32`、`max_active_runs=8`、`max_spawn_depth=4`。byte/tool/run 时长类限制与 `§4.2` 一并后续。

---

## Task 1: 公开类型、状态枚举、错误层与异常

**Files:**
- Create: `src/athena/core/agent_kernel/types.py`
- Test: `test/unit/agent_kernel/test_types.py`
- Create: `test/unit/agent_kernel/__init__.py`

**Interfaces:**
- Consumes: `athena.core.contracts.ArtifactRef`
- Produces: `AgentStatus`、`RunStatus`、`TERMINAL_RUN_STATUSES`、`ReturnWhen`、`AgentPath`、`AgentId`、`RunId`、`CommandId`、`ErrorCode`、`EventSink`、`AgentRunner`、`AgentCodec`、`AgentSpec`、`ForkPolicy`、`AgentMessage`、`AgentEvent`、`RunSummary`、`AgentSnapshot`、`AgentWaitResult`、`AgentError`、`AgentCommandError`、`AgentBusyError`、`AgentRunFailed`、`AgentRunInterrupted`

- [ ] **Step 1: 写失败测试**

`test/unit/agent_kernel/__init__.py`:
```python
"""AgentKernel 单元测试包。"""
```
`test/unit/agent_kernel/test_types.py`:
```python
import pytest

from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentMessage,
    AgentSpec,
    AgentStatus,
    ErrorCode,
    ForkPolicy,
    ReturnWhen,
    RunStatus,
)


def test_run_status_enum_values_match_spec() -> None:
    assert [s.value for s in RunStatus] == [
        "queued", "running", "completed", "failed", "interrupted",
    ]


def test_agent_status_enum_values_match_spec() -> None:
    assert [s.value for s in AgentStatus] == [
        "starting", "idle", "running", "error", "closed",
    ]


def test_terminal_set_contains_only_run_terminal_statuses() -> None:
    assert TERMINAL_RUN_STATUSES == frozenset(
        {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
    )


def test_interrupted_is_run_terminal_but_not_agent_terminal() -> None:
    assert RunStatus.INTERRUPTED in TERMINAL_RUN_STATUSES
    assert AgentStatus.CLOSED not in TERMINAL_RUN_STATUSES


def test_return_when_has_both_modes() -> None:
    assert {r.value for r in ReturnWhen} == {"first_completed", "all_completed"}


def test_fork_policy_has_exactly_three_forms() -> None:
    assert ForkPolicy.none().mode == "none"
    assert ForkPolicy.full().mode == "full"
    assert ForkPolicy.last_n(3).turns == 3


def test_fork_policy_rejects_nonpositive_turns() -> None:
    with pytest.raises(ValueError):
        ForkPolicy.last_n(0)


def test_agent_message_marks_control_messages() -> None:
    assert AgentMessage(source=None, content={}, sequence=1).is_control
    assert not AgentMessage(source="root", content="hi", sequence=2).is_control


def test_agent_spec_holds_runner_codec_role() -> None:
    spec = AgentSpec(runner=object(), codec=object(), role="debater")
    assert spec.role == "debater"


def test_error_code_values_match_spec() -> None:
    assert [c.value for c in ErrorCode] == [
        "NOT_FOUND", "PERMISSION_DENIED", "BUSY", "CLOSED", "LIMIT_REACHED",
        "BUDGET_EXHAUSTED", "INVALID_REQUEST", "CODEC_ERROR",
        "STORE_UNAVAILABLE", "INTERNAL",
    ]


def test_error_hierarchy_matches_spec() -> None:
    from athena.core.agent_kernel.types import (
        AgentBusyError, AgentCommandError, AgentError,
        AgentRunFailed, AgentRunInterrupted,
    )
    assert issubclass(AgentCommandError, AgentError)
    assert issubclass(AgentBusyError, AgentCommandError)
    assert issubclass(AgentRunFailed, AgentError)
    assert issubclass(AgentRunInterrupted, AgentError)
    assert AgentBusyError().code == ErrorCode.BUSY
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_types.py -q`
Expected: 收集错误（模块不存在）。

- [ ] **Step 3: 实现类型与异常**

`src/athena/core/agent_kernel/types.py`:
```python
"""AgentKernel 公开类型与契约（设计 §1.3、§2、§4.4、§4.5）。"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from athena.core.contracts import ArtifactRef

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")

AgentPath = tuple[str, ...]
AgentId = str
RunId = str
CommandId = str


class AgentStatus(str, Enum):
    """Agent 生命周期状态（§1.3）。CLOSED 是唯一 Agent 终态。"""

    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    CLOSED = "closed"


class RunStatus(str, Enum):
    """Run 生命周期状态（§1.3）。INTERRUPTED 是 Run 终态而非 Agent 终态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
)


class ReturnWhen(Enum):
    """wait_agent 返回条件（§3.6）。"""

    FIRST_COMPLETED = "first_completed"
    ALL_COMPLETED = "all_completed"


class ErrorCode(str, Enum):
    """稳定错误 code（§4.5）。"""

    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    BUSY = "BUSY"
    CLOSED = "CLOSED"
    LIMIT_REACHED = "LIMIT_REACHED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INVALID_REQUEST = "INVALID_REQUEST"
    CODEC_ERROR = "CODEC_ERROR"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class AgentError(RuntimeError):
    """Agent 领域异常基类（§4.5）。"""


class AgentCommandError(AgentError):
    """命令错误，带稳定 code；失败命令必须零状态变更。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


class AgentBusyError(AgentCommandError):
    """目标已有非终态 Run，followup 被拒绝（§3.3）。"""

    def __init__(self, message: str = "agent busy", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.BUSY, message, details=details)


class AgentRunFailed(AgentError):
    """Run 执行失败（FAILED 终态）后 wait() 抛出的领域异常。"""


class AgentRunInterrupted(AgentError):
    """Run 被中断后 wait() 抛出的领域异常（§3.5）。"""


@runtime_checkable
class EventSink(Protocol):
    """只追加当前 Run 事件的异步回调（§2.2）。"""

    async def __call__(
        self, kind: str, event_ref: ArtifactRef, data: dict[str, Any] | None = None
    ) -> None: ...


@runtime_checkable
class AgentRunner(Protocol[RequestT, ResponseT]):
    """执行一个已类型化请求的协议（§2.2）。session 由 Kernel 提供。"""

    async def run(
        self,
        request: RequestT,
        *,
        session: "AgentSession",
        emit: EventSink,
    ) -> ResponseT: ...


@runtime_checkable
class AgentCodec(Protocol[RequestT, ResponseT]):
    """请求、响应与 artifact 之间的类型化编解码（§2.2）。"""

    def encode_request(self, value: RequestT) -> ArtifactRef: ...
    def decode_request(self, ref: ArtifactRef) -> RequestT: ...
    def encode_response(self, value: ResponseT) -> ArtifactRef: ...
    def decode_response(self, ref: ArtifactRef) -> ResponseT: ...


@dataclass(frozen=True)
class AgentSpec(Generic[RequestT, ResponseT]):
    """Agent 的不可变能力说明（§2.1）。"""

    runner: AgentRunner[RequestT, ResponseT]
    codec: AgentCodec[RequestT, ResponseT]
    role: str = "agent"


@dataclass(frozen=True)
class ForkPolicy:
    """历史继承规则（§2.6）。只有 none/full/last_n 三种构造。"""

    mode: str
    turns: int | None = None

    @classmethod
    def none(cls) -> "ForkPolicy":
        return cls("none")

    @classmethod
    def full(cls) -> "ForkPolicy":
        return cls("full")

    @classmethod
    def last_n(cls, turns: int) -> "ForkPolicy":
        if turns <= 0:
            raise ValueError("last_n turns must be positive")
        return cls("last_n", turns)


@dataclass(frozen=True)
class AgentMessage:
    """投递至目标 mailbox 的消息（§3.3）。source 为 None 表示内部控制消息。"""

    source: AgentId | None
    content: object
    sequence: int

    @property
    def is_control(self) -> bool:
        return self.source is None


@dataclass(frozen=True)
class AgentEvent:
    """Session journal 中的一条事件（§2.3）。sequence 为 Session 内单调。"""

    run_id: RunId
    sequence: int
    kind: str
    event_ref: ArtifactRef
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunSummary:
    """Run 的只读摘要（§2.3）。"""

    run_id: RunId
    agent_id: AgentId
    status: RunStatus
    response_ref: ArtifactRef | None = None
    error: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AgentSnapshot:
    """Agent 元数据快照（§2.4 list_agents）。"""

    agent_id: AgentId
    path: AgentPath
    name: str
    role: str
    status: AgentStatus
    parent_id: AgentId | None
    pending_run_id: RunId | None = None


@dataclass(frozen=True)
class AgentWaitResult:
    """wait_agent 的结果（§3.6）。completed 按冻结目标给出终态摘要。"""

    completed: dict[AgentId, RunSummary]
    timed_out: bool = False
```

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_types.py -q`
Expected: PASS（11 passed）。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/types.py test/unit/agent_kernel/__init__.py test/unit/agent_kernel/test_types.py
git commit -m "feat(agent-kernel): add public types, status enums, error layer"
```

---

## Task 2: AgentGraphStore — 控制面事实的线性化点

**Files:**
- Create: `src/athena/core/agent_kernel/store.py`
- Test: `test/unit/agent_kernel/test_store.py`

**Interfaces:**
- Consumes: Task 1 的枚举与值类型。
- Produces: `AgentRecord`、`RunRecord`、`JournalRecord`、`OutboxRecord`、`CommandResult`、`StoreSnapshot`、`AgentGraphStore`：
  - `commit(*, command_id, kind, payload) -> int`
  - `record_result(command_id, result)` / `result_for(command_id)`
  - `agent / agents / run / runs / mailbox / mailbox_committed / outbox_for`
  - `run_summary / agent_snapshot`
  - `snapshot() -> StoreSnapshot` / `load(snapshot, journal)`
  - journal kinds：`spawn`、`run_queued`、`run_running`、`run_terminal`、`mailbox`、`mailbox_committed`、`outbox`、`agent_closed`、`agent_error`

- [ ] **Step 1: 写失败测试**

`test/unit/agent_kernel/test_store.py`:
```python
from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    CommandResult,
    OutboxRecord,
    RunRecord,
)
from athena.core.agent_kernel.types import (
    AgentMessage,
    AgentSpec,
    AgentStatus,
    RunStatus,
)


def _spec() -> AgentSpec:
    return AgentSpec(runner=object(), codec=object(), role="debater")


def _spawn(store: AgentGraphStore, agent_id: str = "root") -> None:
    store.commit(command_id=f"c:{agent_id}", kind="spawn", payload={
        "agent": AgentRecord(
            agent_id=agent_id, path=(agent_id,), name=agent_id, role="debater",
            parent_id=None, status=AgentStatus.IDLE, spec=_spec(),
            created_sequence=store.sequence + 1,
        ),
        "run": RunRecord(
            run_id=f"{agent_id}:r1", agent_id=agent_id, parent_run_id=None,
            status=RunStatus.QUEUED, generation=0, request_ref="request",
        ),
    })


def test_commit_assigns_monotonic_sequence_and_journals() -> None:
    store = AgentGraphStore()
    assert store.commit(command_id="c1", kind="spawn", payload={}) == 1
    assert store.commit(command_id="c2", kind="spawn", payload={}) == 2
    assert [r.kind for r in store.journal] == ["spawn", "spawn"]


def test_result_for_round_trip() -> None:
    store = AgentGraphStore()
    assert store.result_for("cmd-1") is None
    store.record_result("cmd-1", CommandResult(value=("root", "root:r1")))
    assert store.result_for("cmd-1").value == ("root", "root:r1")


def test_spawn_indexes_agent_and_run() -> None:
    store = AgentGraphStore()
    _spawn(store)
    assert store.agent("root").status == AgentStatus.IDLE
    assert store.run("root:r1").status == RunStatus.QUEUED
    assert store.agent("missing") is None


def test_run_running_then_terminal_updates_statuses() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(command_id="c2", kind="run_running",
                 payload={"run_id": "root:r1", "generation": 1})
    assert store.run("root:r1").generation == 1
    assert store.agent("root").status == AgentStatus.RUNNING
    store.commit(command_id="c3", kind="run_terminal", payload={
        "run_id": "root:r1", "status": RunStatus.COMPLETED,
        "response_ref": "result://1", "error": None, "reason": None,
    })
    assert store.run("root:r1").status == RunStatus.COMPLETED
    assert store.agent("root").status == AgentStatus.IDLE
    assert store.agent("root").pending_run_id is None


def test_mailbox_cursors_track() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(command_id="m1", kind="mailbox",
                 payload={"agent_id": "root",
                          "message": AgentMessage(source="x", content="hi", sequence=1)})
    assert [m.content for m in store.mailbox("root")] == ["hi"]
    store.commit(command_id="k1", kind="mailbox_committed", payload={"agent_id": "root"})
    assert store.mailbox_committed("root") == 1


def test_outbox_dedup_by_child_run_id() -> None:
    store = AgentGraphStore()
    _spawn(store)
    _spawn(store, "child")
    summary = store.run_summary("child:r1")
    record = OutboxRecord(child_run_id="child:r1", child_agent_id="child",
                          parent_agent_id="root", summary=summary)
    store.commit(command_id="o1", kind="outbox", payload={"record": record})
    store.commit(command_id="o2", kind="outbox", payload={"record": record})
    assert store.outbox_for("child:r1") is record


def test_snapshot_and_load_rebuild_identical_state() -> None:
    store = AgentGraphStore()
    _spawn(store)
    store.commit(command_id="c2", kind="run_running",
                 payload={"run_id": "root:r1", "generation": 1})
    store.commit(command_id="m1", kind="mailbox",
                 payload={"agent_id": "root",
                          "message": AgentMessage(source="x", content="hi", sequence=1)})
    snapshot = store.snapshot()
    fresh = AgentGraphStore()
    fresh.load(snapshot, [])
    assert fresh.agent("root").status == AgentStatus.RUNNING
    assert fresh.run("root:r1").generation == 1
    assert [m.content for m in fresh.mailbox("root")] == ["hi"]


def test_snapshot_is_independent_of_later_mutation() -> None:
    store = AgentGraphStore()
    _spawn(store)
    snapshot = store.snapshot()
    store.commit(command_id="z", kind="agent_closed", payload={"agent_id": "root"})
    assert snapshot.agents["root"].status == AgentStatus.IDLE
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_store.py -q`
Expected: 收集错误。

- [ ] **Step 3: 实现 store**

`src/athena/core/agent_kernel/store.py`:
```python
"""AgentGraphStore — 控制面事实的线性化点（设计 §3.1、§3.7）。

只追加 journal + 派生索引。序列器单线程保证事务原子性；重试同一
``command_id`` 只取原结果。首版内存实现，§4.7 SQLite 后续替换。
"""

from dataclasses import dataclass
from typing import Any

from athena.core.agent_kernel.types import (
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

    def result_or_raise(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.value


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

    def commit(self, *, command_id: CommandId, kind: str, payload: dict[str, Any]) -> int:
        """追加一条 journal 记录并应用派生状态，返回其 sequence。"""
        seq = self._sequence + 1
        self._sequence = seq
        self._apply(kind, payload)
        self._journal.append(JournalRecord(seq, command_id, kind, payload))
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
            run_id=run.run_id, agent_id=run.agent_id, status=run.status,
            response_ref=run.response_ref, error=run.error, reason=run.reason,
        )

    def agent_snapshot(self, agent_id: AgentId) -> AgentSnapshot | None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return AgentSnapshot(
            agent_id=agent.agent_id, path=agent.path, name=agent.name,
            role=agent.role, status=agent.status, parent_id=agent.parent_id,
            pending_run_id=agent.pending_run_id,
        )

    def _apply(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "spawn":
            self._agents[payload["agent"].agent_id] = payload["agent"]
            self._runs[payload["run"].run_id] = payload["run"]
            self._mailboxes.setdefault(payload["agent"].agent_id, [])
            self._mailbox_committed.setdefault(payload["agent"].agent_id, 0)
        elif kind == "run_queued":
            run = payload["run"]
            self._runs[run.run_id] = run
            self._agents[run.agent_id].pending_run_id = run.run_id
        elif kind == "run_running":
            run = self._runs[payload["run_id"]]
            run.status = RunStatus.RUNNING
            run.generation = payload["generation"]
            agent = self._agents[run.agent_id]
            agent.status = AgentStatus.RUNNING
            agent.pending_run_id = run.run_id
        elif kind == "run_terminal":
            run = self._runs[payload["run_id"]]
            run.status = payload["status"]
            run.response_ref = payload.get("response_ref")
            run.error = payload.get("error")
            run.reason = payload.get("reason")
            agent = self._agents[run.agent_id]
            agent.pending_run_id = None
            if agent.status == AgentStatus.RUNNING:
                agent.status = AgentStatus.IDLE
        elif kind == "mailbox":
            self.mailbox(payload["agent_id"]).append(payload["message"])
        elif kind == "mailbox_committed":
            agent_id = payload["agent_id"]
            self._mailbox_committed[agent_id] = len(self.mailbox(agent_id))
        elif kind == "outbox":
            self._outbox[payload["record"].child_run_id] = payload["record"]
        elif kind == "agent_closed":
            self._agents[payload["agent_id"]].status = AgentStatus.CLOSED
        elif kind == "agent_error":
            self._agents[payload["agent_id"]].status = AgentStatus.ERROR
        else:
            raise ValueError(f"unknown journal kind: {kind}")

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
        )

    def load(self, snapshot: StoreSnapshot, journal: list[JournalRecord]) -> None:
        """以快照为基础，重放 sequence 大于快照序列的 journal。"""
        self._sequence = snapshot.sequence
        self._agents = dict(snapshot.agents)
        self._runs = dict(snapshot.runs)
        self._mailboxes = {k: list(v) for k, v in snapshot.mailboxes.items()}
        self._mailbox_committed = dict(snapshot.mailbox_committed)
        self._outbox = dict(snapshot.outbox)
        self._command_results = dict(snapshot.command_results)
        self._journal = list(journal)
        for record in sorted(journal, key=lambda r: r.sequence):
            if record.sequence > snapshot.sequence:
                self._apply(record.kind, record.payload)
```

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_store.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/store.py test/unit/agent_kernel/test_store.py
git commit -m "feat(agent-kernel): add graph store as linearization point"
```

---

## Task 3: AgentSession — memory、mailbox 游标与事件 journal

**Files:**
- Create: `src/athena/core/agent_kernel/session.py`
- Test: `test/unit/agent_kernel/test_session.py`

**Interfaces:**
- Consumes: Task 1/2 的类型与 store。
- Produces: `SessionResources`（`memory`、`aclose`）、`SessionResourcesFactory`（Protocol）、`InMemoryResourcesFactory`、`AgentSession`：
  - `receive_messages() -> list[AgentMessage]`（未提交窗口 `[committed, len)`）
  - `checkpoint()`（推进 committed_cursor，直接提交 store，不经序列器）
  - `append_message(msg)`（受 active run / interrupted 门禁）
  - `set_active_run / clear_active_run / interrupt / close`
  - `append_event / append_terminal_event`
  - `events(after_sequence)`（整条 Session journal，供 `handle.events()`，§2.3）
  - `run_events(run_id, after_sequence)`（单 Run 事件，供 `run.events()`）
  - `active_run_id`、`memory`、`context_ref`、`wait_agents(...)`（PARKED 等待委托，Task 6 用）

- [ ] **Step 1: 写失败测试**

`test/unit/agent_kernel/test_session.py`:
```python
import asyncio

from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.core.agent_kernel.session import AgentSession, InMemoryResourcesFactory
from athena.core.agent_kernel.store import AgentGraphStore
from athena.core.agent_kernel.types import AgentMessage, AgentSpec


def _spec() -> AgentSpec:
    return AgentSpec(runner=object(), codec=object(), role="debater")


def _session(store: AgentGraphStore) -> AgentSession:
    resources = InMemoryResourcesFactory().create("root", _spec())
    return AgentSession(agent_id="root", spec=_spec(), resources=resources, store=store)


async def _collect(source, n):
    """从订阅式事件流收集恰好 n 条后终止（事件流为订阅式，不会自然结束）。"""
    events = []
    async for event in source:
        events.append(event)
        if len(events) >= n:
            break
    return events


def test_receive_messages_returns_uncommitted_window() -> None:
    store = AgentGraphStore()
    store.commit(command_id="m1", kind="mailbox",
                 payload={"agent_id": "root",
                          "message": AgentMessage(source="x", content="a", sequence=1)})
    store.commit(command_id="m2", kind="mailbox",
                 payload={"agent_id": "root",
                          "message": AgentMessage(source="x", content="b", sequence=2)})
    session = _session(store)
    assert [m.content for m in session.receive_messages()] == ["a", "b"]
    session.checkpoint()
    assert session.receive_messages() == []
    store.commit(command_id="m3", kind="mailbox",
                 payload={"agent_id": "root",
                          "message": AgentMessage(source="x", content="c", sequence=3)})
    assert [m.content for m in session.receive_messages()] == ["c"]


def test_event_append_guarded_by_active_run() -> None:
    store = AgentGraphStore()
    session = _session(store)

    async def scenario() -> None:
        await session.append_event("r1", "agent/text_delta", "event://1")
        session.set_active_run("r1")
        await session.append_event("r1", "agent/text_delta", "event://2")
        session.clear_active_run()
        await session.append_event("r1", "agent/text_delta", "event://3")
        events = await _collect(session.events(after_sequence=0), 1)
        assert [e.event_ref for e in events] == ["event://2"]

    asyncio.run(scenario())


def test_run_events_filters_to_one_run() -> None:
    store = AgentGraphStore()
    session = _session(store)

    async def scenario() -> None:
        session.set_active_run("r1")
        await session.append_event("r1", "agent/step", "event://r1-1")
        await session.append_event("r1", "agent/step", "event://r1-2")
        session.clear_active_run()
        session.set_active_run("r2")
        await session.append_event("r2", "agent/step", "event://r2-1")
        run1 = await _collect(session.run_events("r1", after_sequence=0), 2)
        run2 = await _collect(session.run_events("r2", after_sequence=0), 1)
        assert [e.event_ref for e in run1] == ["event://r1-1", "event://r1-2"]
        assert [e.event_ref for e in run2] == ["event://r2-1"]

    asyncio.run(scenario())


def test_subscriber_wakes_on_terminal_event_appended_live() -> None:
    store = AgentGraphStore()
    session = _session(store)

    async def scenario() -> None:
        session.set_active_run("r1")
        reader = asyncio.create_task(_collect(session.events(after_sequence=0), 1))
        await asyncio.sleep(0)   # 让 reader 先进入等待
        session.append_terminal_event("r1", "run_completed", "event://done")
        events = await asyncio.wait_for(reader, timeout=2)
        assert [e.event_ref for e in events] == ["event://done"]

    asyncio.run(scenario())


def test_append_message_guarded_after_interrupt() -> None:
    store = AgentGraphStore()
    session = _session(store)
    session.set_active_run("r1")
    session.append_message(ModelRequest(parts=[UserPromptPart(content="keep")]))
    session.interrupt()
    session.append_message(ModelRequest(parts=[UserPromptPart(content="drop")]))
    assert len(session.memory.items) == 1


def test_rollback_reverts_memory_to_snapshot() -> None:
    store = AgentGraphStore()
    session = _session(store)
    session.set_active_run("r1")
    before = session.memory.snapshot()[0]
    session.append_message(ModelRequest(parts=[UserPromptPart(content="tmp")]))
    session.memory.rollback(before)
    assert session.memory.items == []
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_session.py -q`
Expected: 收集错误。

- [ ] **Step 3: 实现 session**

`src/athena/core/agent_kernel/session.py`:
```python
"""AgentSession — memory、mailbox 游标与事件 journal（设计 §1.2、§3.3）。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from athena.core.agent_kernel.store import AgentGraphStore
from athena.core.agent_kernel.types import (
    AgentEvent,
    AgentId,
    AgentMessage,
    AgentSpec,
    ArtifactRef,
    RunId,
)
from athena.memory.context_manager import ContextManager


class SessionResources:
    """单个 Session 的资源绑定。工厂注入，不由 Kernel 硬编码。"""

    def __init__(self, *, memory: ContextManager | None = None) -> None:
        self._memory = memory or ContextManager()
        self.closed = False

    @property
    def memory(self) -> ContextManager:
        return self._memory

    async def aclose(self) -> None:
        self.closed = True


class SessionResourcesFactory(Protocol):
    """为每个 Agent Session 构造独立资源的工厂。"""

    def create(self, agent_id: AgentId, spec: AgentSpec) -> SessionResources: ...
    async def aclose(self) -> None: ...


class InMemoryResourcesFactory:
    """默认工厂：每个 Session 一个独立 ContextManager。"""

    def __init__(self) -> None:
        self._resources: list[SessionResources] = []

    def create(self, agent_id: AgentId, spec: AgentSpec) -> SessionResources:
        del agent_id, spec
        resources = SessionResources()
        self._resources.append(resources)
        return resources

    async def aclose(self) -> None:
        for resources in self._resources:
            await resources.aclose()


class AgentSession:
    """单个 Agent 的会话态：memory、mailbox 游标、journal、interrupt。"""

    def __init__(
        self,
        *,
        agent_id: AgentId,
        spec: AgentSpec,
        resources: SessionResources,
        store: AgentGraphStore,
        kernel: Any = None,
    ) -> None:
        self._agent_id = agent_id
        self._spec = spec
        self._resources = resources
        self._store = store
        self._kernel = kernel
        self._events: list[AgentEvent] = []
        self._next_event_sequence = 1
        self._wakeup = asyncio.Event()
        self._active_run_id: RunId | None = None
        self._interrupted = False
        self._closed = False

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def memory(self) -> ContextManager:
        return self._resources.memory

    @property
    def context_ref(self) -> str:
        return f"context://{self._agent_id}"

    @property
    def active_run_id(self) -> RunId | None:
        return self._active_run_id

    def set_active_run(self, run_id: RunId) -> None:
        """标记当前运行 Run；其事件与 memory 写入生效。"""
        self._active_run_id = run_id
        self._interrupted = False

    def clear_active_run(self) -> None:
        self._active_run_id = None

    def interrupt(self) -> None:
        """中断后旧 runner 不可再写 memory 或事件（§3.5）。"""
        self._interrupted = True
        self._active_run_id = None

    def close(self) -> None:
        self._closed = True
        self._interrupted = True
        self._active_run_id = None

    def receive_messages(self) -> list[AgentMessage]:
        """返回未提交（[committed, len)）的 mailbox 窗口。"""
        committed = self._store.mailbox_committed(self._agent_id)
        return list(self._store.mailbox(self._agent_id)[committed:])

    def checkpoint(self) -> None:
        """推进 committed_cursor：可见消息已耐久消费，不再重投。"""
        self._store.commit(
            command_id=f"ckpt:{self._agent_id}:{self._store.sequence + 1}",
            kind="mailbox_committed",
            payload={"agent_id": self._agent_id},
        )

    def append_message(self, message: ModelMessage) -> None:
        """受门禁的 memory 写入；中断或关闭后丢弃。"""
        if self._interrupted or self._closed or self._active_run_id is None:
            return
        self.memory.append(message)

    def _next_sequence(self) -> int:
        seq = self._next_event_sequence
        self._next_event_sequence += 1
        return seq

    async def append_event(
        self, run_id: RunId, kind: str, event_ref: ArtifactRef,
        data: dict[str, Any] | None = None,
    ) -> None:
        """追加当前 Run 事件；非活跃 Run 的事件丢弃。"""
        if self._active_run_id != run_id or self._closed:
            return
        event = AgentEvent(run_id, self._next_sequence(), kind, event_ref, data)
        self._events.append(event)
        self._wakeup.set()

    def append_terminal_event(
        self, run_id: RunId, kind: str, event_ref: ArtifactRef,
        data: dict[str, Any] | None = None,
    ) -> None:
        """追加终态事件；由 Kernel 在终态提交时调用（有意不受门禁，并唤醒订阅者）。"""
        event = AgentEvent(run_id, self._next_sequence(), kind, event_ref, data)
        self._events.append(event)
        self._wakeup.set()

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """返回整条 Session journal（跨 Run，订阅式：持续等待新事件，由调用方决定何时终止读取，§5.1）。"""
        return self._event_iterator(after_sequence, run_id=None)

    def run_events(self, run_id: RunId, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """返回指定 Run 的事件流（订阅式），供 run.events()。"""
        return self._event_iterator(after_sequence, run_id=run_id)

    async def _event_iterator(
        self, after_sequence: int, run_id: RunId | None
    ) -> AsyncIterator[AgentEvent]:
        index = max(0, after_sequence)
        while True:
            while index >= len(self._events):
                self._wakeup.clear()
                if index < len(self._events):
                    break
                await self._wakeup.wait()
            event = self._events[index]
            index += 1
            if run_id is None or event.run_id == run_id:
                yield event

    async def wait_agents(
        self, target_ids: list[AgentId], *, timeout: float | None = None
    ) -> Any:
        """由运行中的 runner 调用：等待目标终结，期间释放本 Run 的 lease（§3.6）。"""
        if self._kernel is None:
            raise RuntimeError("session has no kernel binding")
        return await self._kernel.wait_agent_parked(
            target_ids, parking_run_id=self._active_run_id, timeout=timeout
        )
```

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_session.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/session.py test/unit/agent_kernel/test_session.py
git commit -m "feat(agent-kernel): add agent session with mailbox cursors and event journal"
```

---

## Task 4: AgentKernel 骨架 — 序列器、调度器、注册表与 spawn

**Files:**
- Create: `src/athena/core/agent_kernel/kernel.py`
- Test: `test/unit/agent_kernel/test_kernel.py`
- Create: `test/unit/agent_kernel/_support.py`

**Interfaces:**
- Consumes: Task 1-3 全部。
- Produces（本 Task）：
  - `AgentScheduler(max_agents, max_active_runs)`：`try_reserve / release_resident / enqueue / dequeue / try_dispatch / release_active / park / acquire_lease`
  - `AgentRegistry(store)`：`child_path / has_child_named / post_order / has_live_descendants / spec / is_under_fence`
  - `KernelCommand`、`AgentKernel`：`start / aclose`、`_enqueue`、`_serializer_loop`、`_dispatch`、`create_root / spawn`、查询方法
  - `AgentKernel(*, resources_factory, max_agents=32, max_active_runs=8, max_spawn_depth=4, store=None)`（§4.4）
  - `_pump_scheduler()` 本 Task 为空实现，Task 5 填充执行体

- [ ] **Step 1: 写失败测试**

`test/unit/agent_kernel/_support.py`:
```python
"""测试共享的 codec 与 runner 替身。"""

import asyncio
import json


class JsonCodec:
    """请求/响应以 JSON 字符串作为 ArtifactRef。"""

    def encode_request(self, value: object) -> str:
        return json.dumps(value)

    def decode_request(self, ref: str) -> object:
        return json.loads(ref)

    def encode_response(self, value: object) -> str:
        return json.dumps(value)

    def decode_response(self, ref: str) -> object:
        return json.loads(ref)


class EchoRunner:
    """回显请求；可选等待。"""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.block: asyncio.Event | None = None

    async def run(self, request: object, *, session, emit) -> dict:
        self.calls.append(request)
        await emit("agent/step", "event://step", {"n": len(self.calls)})
        if self.block is not None:
            await self.block.wait()
        return {"echo": request}


class BlockingRunner:
    """启动后阻塞直到 release，用于中断/并发测试。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self, request: object, *, session, emit) -> dict:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"done": True}


async def collect_events(source, n):
    """从订阅式事件流收集恰好 n 条后终止（事件流为订阅式，不会自然结束）。"""
    events = []
    async for event in source:
        events.append(event)
        if len(events) >= n:
            break
    return events


async def collect_until_terminal(source):
    """收集到首个 ``run_`` 终态事件为止。"""
    events = []
    async for event in source:
        events.append(event)
        if event.kind.startswith("run_"):
            break
    return events
```

`test/unit/agent_kernel/test_kernel.py`:
```python
import pytest

from athena.core.agent_kernel.kernel import AgentKernel, AgentRegistry, AgentScheduler
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.store import AgentGraphStore
from athena.core.agent_kernel.types import (
    AgentCommandError, AgentSpec, AgentStatus, ErrorCode, RunStatus,
)

from ._support import (
    BlockingRunner, EchoRunner, JsonCodec, collect_events,
)


def _spec(runner=None, role="agent") -> AgentSpec:
    return AgentSpec(runner=runner or EchoRunner(), codec=JsonCodec(), role=role)


def _kernel(**kw) -> AgentKernel:
    return AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw)


def test_scheduler_bounds_and_fifo() -> None:
    sched = AgentScheduler(max_agents=2, max_active_runs=1)
    assert sched.try_reserve("a") and sched.try_reserve("b")
    assert not sched.try_reserve("c")
    sched.release_resident("a")
    assert sched.try_reserve("c")
    sched.enqueue("r1")
    sched.enqueue("r2")
    assert sched.try_dispatch() == "r1"
    assert sched.try_dispatch() is None
    sched.release_active("r1")
    assert sched.try_dispatch() == "r2"


def test_scheduler_park_releases_lease() -> None:
    sched = AgentScheduler(max_agents=8, max_active_runs=1)
    sched.enqueue("parent")
    assert sched.try_dispatch() == "parent"
    sched.park("parent")
    sched.enqueue("child")
    assert sched.try_dispatch() == "child"


@pytest.mark.asyncio
async def test_acquire_lease_wakes_when_capacity_frees() -> None:
    sched = AgentScheduler(max_agents=8, max_active_runs=1)
    sched.enqueue("busy")
    sched.try_dispatch()
    waiter = asyncio.create_task(sched.acquire_lease("waiting"))
    await asyncio.sleep(0)
    assert not waiter.done()   # 容量满 → 等待
    sched.release_active("busy")
    await asyncio.wait_for(waiter, timeout=1)
    assert sched.active_count == 1


def test_registry_path_and_subtree() -> None:
    store = AgentGraphStore()
    registry = AgentRegistry(store)
    assert registry.child_path(None, "root") == ("root",)
    assert registry.child_path("root", "c") == ("root", "c")


def test_kernel_defaults_match_spec_config() -> None:
    kernel = _kernel()
    assert kernel._scheduler.max_agents == 32
    assert kernel._scheduler.max_active_runs == 8
    assert kernel._max_spawn_depth == 4


@pytest.mark.asyncio
async def test_serializer_executes_spawn_and_idempotency() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_spec(), {"q": 1})
    assert agent_id == "root"
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    assert kernel._store.run(run_id).status == RunStatus.QUEUED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_command_id_is_idempotent() -> None:
    kernel = _kernel()
    await kernel.start()
    first = await kernel._enqueue(
        "spawn", {"parent_id": None, "spec": _spec(), "task": {}, "name": "root"},
        command_id="cid-1",
    )
    second = await kernel._enqueue(
        "spawn", {"parent_id": None, "spec": _spec(), "task": {}, "name": "other"},
        command_id="cid-1",
    )
    assert first == second
    assert kernel.agent_status("root") == AgentStatus.IDLE
    assert kernel.agent_status("other") is None
    await kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_rejects_duplicate_sibling() -> None:
    kernel = _kernel()
    await kernel.start()
    await kernel.create_root(_spec(), {})
    await kernel.spawn("root", _spec(), {}, name="c")
    with pytest.raises(AgentCommandError) as raised:
        await kernel.spawn("root", _spec(), {}, name="c")
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    await kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_rejects_when_max_agents_exhausted() -> None:
    kernel = _kernel(max_agents=1)
    await kernel.start()
    await kernel.create_root(_spec(), {})
    with pytest.raises(AgentCommandError) as raised:
        await kernel.spawn("root", _spec(), {})
    assert raised.value.code == ErrorCode.LIMIT_REACHED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_rejects_beyond_max_depth() -> None:
    kernel = _kernel(max_spawn_depth=2)
    await kernel.start()
    await kernel.create_root(_spec(), {})
    await kernel.spawn("root", _spec(), {}, name="c1")
    await kernel.spawn("root/c1", _spec(), {}, name="c2")
    with pytest.raises(AgentCommandError) as raised:
        await kernel.spawn("root/c1/c2", _spec(), {}, name="c3")
    assert raised.value.code == ErrorCode.LIMIT_REACHED
    await kernel.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: 收集错误。

- [ ] **Step 3: 实现内核骨架**

`src/athena/core/agent_kernel/kernel.py`:
```python
"""AgentKernel — 整棵根 Agent 树的生命周期事务与状态机（设计 §3）。

单一命令序列器：所有 spawn/send_message/followup/interrupt/close 与 Run
terminal commit 均经 ``_command_queue`` 单任务处理；线性化点为
``AgentGraphStore.commit``。模型执行在序列器外运行，以 run_finished 命令回送。
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from athena.core.agent_kernel.session import (
    AgentSession,
    InMemoryResourcesFactory,
    SessionResourcesFactory,
)
from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    JournalRecord,
    OutboxRecord,
    RunRecord,
    StoreSnapshot,
)
from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentCommandError,
    AgentId,
    AgentPath,
    AgentSnapshot,
    AgentSpec,
    AgentStatus,
    AgentWaitResult,
    ErrorCode,
    ForkPolicy,
    ReturnWhen,
    RunId,
    RunStatus,
    RunSummary,
)

logger = logging.getLogger(__name__)


class AgentScheduler:
    """总 Agent 容量与同时运行容量；FIFO ready 队列（§3.6）。"""

    def __init__(self, *, max_agents: int, max_active_runs: int) -> None:
        if max_agents <= 0 or max_active_runs <= 0:
            raise ValueError("capacities must be positive")
        self._max_agents = max_agents
        self._max_active_runs = max_active_runs
        self._resident: set[AgentId] = set()
        self._active: set[RunId] = set()
        self._ready: deque[RunId] = deque()
        self._lease_available = asyncio.Event()

    @property
    def max_agents(self) -> int:
        return self._max_agents

    @property
    def max_active_runs(self) -> int:
        return self._max_active_runs

    @property
    def resident_count(self) -> int:
        return len(self._resident)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def try_reserve(self, agent_id: AgentId) -> bool:
        """预留 Agent 总量槽位；超过上限返回 False。"""
        if len(self._resident) >= self._max_agents:
            return False
        self._resident.add(agent_id)
        return True

    def release_resident(self, agent_id: AgentId) -> None:
        self._resident.discard(agent_id)

    def enqueue(self, run_id: RunId) -> None:
        self._ready.append(run_id)

    def dequeue(self, run_id: RunId) -> None:
        try:
            self._ready.remove(run_id)
        except ValueError:
            # 中断 QUEUED 时可能已派发 → 无需移除
            pass

    def try_dispatch(self) -> RunId | None:
        """有容量且 ready 非空时派发一个 Run 并占用 lease。"""
        if len(self._active) >= self._max_active_runs:
            return None
        while self._ready:
            run_id = self._ready.popleft()
            self._active.add(run_id)
            return run_id
        return None

    def release_active(self, run_id: RunId) -> None:
        self._active.discard(run_id)
        self._lease_available.set()

    def park(self, run_id: RunId) -> None:
        """释放 lease 进入 PARKED phase；公共 RunStatus 仍为 RUNNING（§3.6）。"""
        self._active.discard(run_id)
        self._lease_available.set()

    async def acquire_lease(self, run_id: RunId) -> None:
        """阻塞等待容量后占用 lease（PARKED 恢复路径）。"""
        while len(self._active) >= self._max_active_runs:
            self._lease_available.clear()
            if len(self._active) < self._max_active_runs:
                break
            await self._lease_available.wait()
        self._active.add(run_id)


class AgentRegistry:
    """稳定 AgentPath、父子树与子树查询。状态迁移由 Kernel 执行。"""

    def __init__(self, store: AgentGraphStore) -> None:
        self._store = store

    def child_path(self, parent_id: AgentId | None, name: str) -> AgentPath:
        if not name or "/" in name:
            raise ValueError("agent name must be non-empty and contain no '/'")
        if parent_id is None:
            return (name,)
        return (*parent_id.split("/"), name)

    def has_child_named(self, parent_id: AgentId | None, name: str) -> bool:
        return self._store.agent("/".join(self.child_path(parent_id, name))) is not None

    def children(self, agent_id: AgentId) -> list[AgentId]:
        return sorted(
            a.agent_id for a in self._store.agents().values() if a.parent_id == agent_id
        )

    def post_order(self, agent_id: AgentId) -> list[AgentId]:
        """后序遍历子树：先子孙后自身。"""
        result: list[AgentId] = []
        for child in self.children(agent_id):
            result.extend(self.post_order(child))
        result.append(agent_id)
        return result

    def has_live_descendants(self, agent_id: AgentId) -> bool:
        return any(
            self._store.agent(c).status != AgentStatus.CLOSED
            for c in self.post_order(agent_id)[:-1]
        )

    def spec(self, agent_id: AgentId) -> AgentSpec:
        agent = self._store.agent(agent_id)
        if agent is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return agent.spec

    def is_under_fence(self, agent_id: AgentId, fences: set[AgentId]) -> bool:
        return any(agent_id == f or agent_id.startswith(f + "/") for f in fences)


@dataclass
class KernelCommand:
    """经序列器处理的一条命令。reply 在事务后解析。"""

    command_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    reply: asyncio.Future[Any] = field(default_factory=asyncio.Future)


class AgentKernel:
    """统一 Agent 底层。内部类型，不进入日常业务调用面。"""

    def __init__(
        self,
        *,
        resources_factory: SessionResourcesFactory | None = None,
        max_agents: int = 32,
        max_active_runs: int = 8,
        max_spawn_depth: int = 4,
        store: AgentGraphStore | None = None,
    ) -> None:
        self._resources_factory = resources_factory or InMemoryResourcesFactory()
        self._store = store or AgentGraphStore()
        self._registry = AgentRegistry(self._store)
        self._scheduler = AgentScheduler(
            max_agents=max_agents, max_active_runs=max_active_runs
        )
        self._max_spawn_depth = max_spawn_depth
        self._sessions: dict[AgentId, AgentSession] = {}
        self._runner_tasks: dict[RunId, asyncio.Task[None]] = {}
        self._command_queue: asyncio.Queue[KernelCommand] = asyncio.Queue()
        self._command_results: dict[str, asyncio.Future[Any]] = {}
        self._run_waiters: dict[RunId, asyncio.Future[RunSummary]] = {}
        self._fences: set[AgentId] = set()
        self._serializer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动命令序列器。"""
        if self._serializer_task is not None:
            return
        self._serializer_task = asyncio.create_task(
            self._serializer_loop(), name="agent-kernel-serializer"
        )

    async def aclose(self) -> None:
        """关闭整棵根树与 Kernel 资源。"""
        for agent_id in list(self._store.agents()):
            self._close_one(agent_id)
        if self._serializer_task is not None:
            self._serializer_task.cancel()
            try:
                await self._serializer_task
            except asyncio.CancelledError:
                # 序列器被取消 → 预期行为，忽略
                pass
            self._serializer_task = None
        for task in list(self._runner_tasks.values()):
            task.cancel()
        if self._runner_tasks:
            await asyncio.gather(*self._runner_tasks.values(), return_exceptions=True)
        self._runner_tasks.clear()
        await self._resources_factory.aclose()

    async def _serializer_loop(self) -> None:
        while True:
            command = await self._command_queue.get()
            if command.kind == "kernel_shutdown":
                return
            try:
                result = self._dispatch(command)
                self._store.record_result(command.command_id, CommandResult(value=result))
                if not command.reply.done():
                    command.reply.set_result(result)
            except AgentCommandError as exc:
                self._store.record_result(command.command_id, CommandResult(error=exc))
                if not command.reply.done():
                    command.reply.set_exception(exc)
            except Exception as exc:
                logger.exception("kernel command failed: %s", command.kind)
                if not command.reply.done():
                    command.reply.set_exception(exc)

    def _enqueue(
        self, kind: str, payload: dict[str, Any], command_id: str | None = None
    ) -> asyncio.Future[Any]:
        """入队命令并返回 reply future；同 command_id 重试返回原 future。"""
        cid = command_id or f"cmd:{self._store.sequence + 1}:{kind}"
        future = self._command_results.get(cid)
        if future is not None:
            return future
        cached = self._store.result_for(cid)
        if cached is not None:
            future = asyncio.get_event_loop().create_future()
            self._command_results[cid] = future
            if cached.error is not None:
                future.set_exception(cached.error)
            else:
                future.set_result(cached.value)
            return future
        command = KernelCommand(command_id=cid, kind=kind, payload=payload)
        self._command_results[cid] = command.reply
        self._command_queue.put_nowait(command)
        return command.reply

    async def _run_command(self, kind: str, payload: dict[str, Any]) -> Any:
        future = self._enqueue(kind, payload)
        return await asyncio.shield(future)

    def _dispatch(self, command: KernelCommand) -> Any:
        kind = command.kind
        if kind == "spawn":
            return self._spawn(command)
        if kind == "send_message":
            self._send_message(command)
            return None
        if kind == "followup":
            return self._followup(command)
        if kind == "run_finished":
            self._run_finished(command)
            return None
        if kind == "interrupt":
            self._interrupt(command)
            return None
        if kind == "cancel_run":
            self._cancel_run(command)
            return None
        if kind == "close":
            self._close(command)
            return None
        raise ValueError(f"unknown command kind: {kind}")

    async def create_root(
        self, spec: AgentSpec, task: object, *, name: str = "root"
    ) -> tuple[AgentId, RunId]:
        """创建根 Agent 并返回 (agent_id, 首个 Run id)。"""
        future = self._enqueue(
            "spawn", {"parent_id": None, "spec": spec, "task": task, "name": name}
        )
        return await asyncio.shield(future)

    async def spawn(
        self,
        parent_id: AgentId,
        spec: AgentSpec,
        task: object,
        *,
        name: str | None = None,
        fork: ForkPolicy = ForkPolicy.none(),
    ) -> tuple[AgentId, RunId]:
        """在 parent 下创建子 Agent。fork 历史复制（§4.6）待后续，当前仅接受。"""
        del fork
        future = self._enqueue(
            "spawn",
            {"parent_id": parent_id, "spec": spec, "task": task, "name": name},
        )
        return await asyncio.shield(future)

    def _spawn(self, command: KernelCommand) -> tuple[AgentId, RunId]:
        payload = command.payload
        parent_id = payload["parent_id"]
        spec = payload["spec"]
        name = payload["name"] or "agent"
        if parent_id is not None:
            parent = self._store.agent(parent_id)
            if parent is None:
                raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown parent: {parent_id}")
            if parent.status == AgentStatus.CLOSED:
                raise AgentCommandError(ErrorCode.CLOSED, f"parent closed: {parent_id}")
            if self._registry.is_under_fence(parent_id, self._fences):
                raise AgentCommandError(ErrorCode.CLOSED, f"parent is closing: {parent_id}")
            if self._registry.has_child_named(parent_id, name):
                raise AgentCommandError(ErrorCode.INVALID_REQUEST, "child name already exists")
            if len(parent.path) > self._max_spawn_depth:
                raise AgentCommandError(ErrorCode.LIMIT_REACHED, "max_spawn_depth exceeded")
        path = self._registry.child_path(parent_id, name)
        agent_id = "/".join(path)
        # 可失败步骤（资源、编码）先于预留执行；预留失败只产生可回收的孤儿资源
        resources = self._resources_factory.create(agent_id, spec)
        try:
            request_ref = spec.codec.encode_request(payload["task"])
        except Exception as exc:
            # 编解码失败 → CODEC_ERROR，失败命令零状态变更
            raise AgentCommandError(ErrorCode.CODEC_ERROR, str(exc)) from exc
        if not self._scheduler.try_reserve(agent_id):
            raise AgentCommandError(ErrorCode.LIMIT_REACHED, "max_agents exceeded")
        session = AgentSession(
            agent_id=agent_id, spec=spec, resources=resources, store=self._store,
            kernel=self,
        )
        self._sessions[agent_id] = session
        run_id = f"{agent_id}:r:{self._store.sequence + 1}"
        parent_run_id = None
        if parent_id is not None:
            parent_run_id = self._store.agent(parent_id).pending_run_id
        self._store.commit(command_id=command.command_id, kind="spawn", payload={
            "agent": AgentRecord(
                agent_id=agent_id, path=path, name=name, role=spec.role,
                parent_id=parent_id, status=AgentStatus.IDLE, spec=spec,
                created_sequence=self._store.sequence + 1,
            ),
            "run": RunRecord(
                run_id=run_id, agent_id=agent_id, parent_run_id=parent_run_id,
                status=RunStatus.QUEUED, generation=0, request_ref=request_ref,
            ),
        })
        self._scheduler.enqueue(run_id)
        self._pump_scheduler()
        return agent_id, run_id

    def _pump_scheduler(self) -> None:
        """派发 ready 队列中可运行的 Run。Task 5 填充执行体。"""

    def _close_one(self, agent_id: AgentId) -> None:
        """关闭单个 Agent：中断其 Run、关闭会话、写 CLOSED tombstone。"""

    def _check_open(self, target_id: AgentId) -> None:
        agent = self._store.agent(target_id)
        if agent is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
        if agent.status == AgentStatus.CLOSED:
            raise AgentCommandError(ErrorCode.CLOSED, f"agent closed: {target_id}")
        if self._registry.is_under_fence(target_id, self._fences):
            raise AgentCommandError(ErrorCode.CLOSED, f"agent is closing: {target_id}")

    def agent_status(self, agent_id: AgentId) -> AgentStatus | None:
        agent = self._store.agent(agent_id)
        return agent.status if agent is not None else None

    def agent_snapshot(self, agent_id: AgentId) -> AgentSnapshot | None:
        return self._store.agent_snapshot(agent_id)

    def agent_path(self, agent_id: AgentId) -> AgentPath | None:
        agent = self._store.agent(agent_id)
        return agent.path if agent is not None else None

    def registry_spec(self, agent_id: AgentId) -> AgentSpec:
        return self._registry.spec(agent_id)

    def list_agents(self, path_prefix: AgentPath | None = None) -> list[AgentSnapshot]:
        prefix = "/".join(path_prefix) if path_prefix else ""
        return [
            snap
            for snap in (self._store.agent_snapshot(aid) for aid in self._store.agents())
            if snap is not None and snap.agent_id.startswith(prefix)
        ]
```

`CommandResult` 需在 `kernel.py` 顶部从 store 导入：`from athena.core.agent_kernel.store import CommandResult`。Task 4 中 `_send_message`、`_followup`、`_run_finished`、`_interrupt`、`_cancel_run`、`_close` 尚未定义，先补占位定义（Task 5-8 填实）：
```python
    def _send_message(self, command: KernelCommand) -> None:
        raise NotImplementedError

    def _followup(self, command: KernelCommand) -> RunId:
        raise NotImplementedError

    def _run_finished(self, command: KernelCommand) -> None:
        raise NotImplementedError

    def _interrupt(self, command: KernelCommand) -> None:
        raise NotImplementedError

    def _cancel_run(self, command: KernelCommand) -> None:
        raise NotImplementedError

    def _close(self, command: KernelCommand) -> None:
        raise NotImplementedError
```

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/kernel.py test/unit/agent_kernel/test_kernel.py test/unit/agent_kernel/_support.py
git commit -m "feat(agent-kernel): add kernel serializer, scheduler, registry, and spawn"
```

---

## Task 5: 调度派发、runner 执行与终态提交

**Files:**
- Modify: `src/athena/core/agent_kernel/kernel.py`
- Test: `test/unit/agent_kernel/test_kernel.py`

**Interfaces:**
- Consumes: Task 4 的 `_pump_scheduler` 空实现。
- Produces：
  - `_pump_scheduler()`：循环 `try_dispatch()`，提交 `run_running` 并创建 `_execute_run` 任务
  - `_execute_run(run_id, generation)`：解码请求、`set_active_run`、调用 runner、编码响应、回送 `run_finished`
  - `_run_finished(command)`：终态 CAS、terminal event、release lease、resolve waiter
  - `_resolve_run_waiter / wait_run / run_events / run_summary / session_events`
  - 失败时 `session.memory.rollback(before)`

- [ ] **Step 1: 写失败测试**

追加到 `test/unit/agent_kernel/test_kernel.py`：
```python
@pytest.mark.asyncio
async def test_run_completes_and_resolves_wait() -> None:
    runner = EchoRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_spec(runner), {"q": "hi"})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert summary.response_ref == '{"echo": {"q": "hi"}}'
    assert runner.calls == [{"q": "hi"}]
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    events = await collect_events(kernel.run_events(run_id, after_sequence=0), 2)
    assert [e.kind for e in events] == ["agent/step", "run_completed"]
    await kernel.aclose()


@pytest.mark.asyncio
async def test_runner_failure_marks_run_failed_and_agent_idle() -> None:
    class FailingRunner:
        async def run(self, request, *, session, emit) -> dict:
            raise ValueError("bad request")

    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_spec(FailingRunner()), {})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.FAILED
    assert summary.error == "ValueError: bad request"
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_followup_after_completion_creates_new_run() -> None:
    runner = EchoRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run1 = await kernel.create_root(_spec(runner), {"q": 1})
    await kernel.wait_run(run1, timeout=2)
    run2 = await kernel.followup(agent_id, {"q": 2})
    assert run2 != run1
    summary = await kernel.wait_run(run2, timeout=2)
    assert summary.response_ref == '{"echo": {"q": 2}}'
    await kernel.aclose()


@pytest.mark.asyncio
async def test_session_events_span_runs() -> None:
    runner = EchoRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run1 = await kernel.create_root(_spec(runner), {"q": 1})
    await kernel.wait_run(run1, timeout=2)
    run2 = await kernel.followup(agent_id, {"q": 2})
    await kernel.wait_run(run2, timeout=2)
    events = await collect_events(kernel.session_events(agent_id, after_sequence=0), 4)
    assert len(events) == 4  # 两轮各：agent/step + run_completed
    assert events[0].run_id == run1
    assert events[2].run_id == run2
    await kernel.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: FAIL（wait_run / session_events 未定义；`_followup` 抛 NotImplementedError）。

- [ ] **Step 3: 实现派发、执行与终态**

在 `kernel.py` 中替换 `_pump_scheduler` 占位并新增方法：
```python
    def _pump_scheduler(self) -> None:
        """派发 ready 队列中可运行的 Run；无容量或空队列时立即返回。"""
        while True:
            run_id = self._scheduler.try_dispatch()
            if run_id is None:
                return
            generation = self._store.run(run_id).generation + 1
            self._store.commit(
                command_id=f"disp:{run_id}:{generation}", kind="run_running",
                payload={"run_id": run_id, "generation": generation},
            )
            session = self._sessions[self._store.run(run_id).agent_id]
            session.set_active_run(run_id)
            task = asyncio.create_task(
                self._execute_run(run_id, generation), name=f"run-{run_id}"
            )
            self._runner_tasks[run_id] = task
            task.add_done_callback(
                lambda t, rid=run_id: self._runner_tasks.pop(rid, None)
            )

    async def _execute_run(self, run_id: RunId, generation: int) -> None:
        run = self._store.run(run_id)
        agent = self._store.agent(run.agent_id)
        session = self._sessions[run.agent_id]
        before = session.memory.snapshot()[0]

        async def emit(kind: str, event_ref: str, data=None) -> None:
            await session.append_event(run_id, kind, event_ref, data)

        try:
            request = agent.spec.codec.decode_request(run.request_ref)
            response = await agent.spec.runner.run(request, session=session, emit=emit)
            response_ref = agent.spec.codec.encode_response(response)
            outcome = {"status": RunStatus.COMPLETED, "response_ref": response_ref}
        except asyncio.CancelledError:
            # 外部取消（interrupt/close）→ 终态已由中断方提交，直接传播
            session.memory.rollback(before)
            raise
        except Exception as exc:
            # Runner 领域失败 → 终态 FAILED，Agent 回 IDLE
            session.memory.rollback(before)
            outcome = {"status": RunStatus.FAILED, "error": f"{type(exc).__name__}: {exc}"}
        future = self._enqueue("run_finished", {
            "run_id": run_id, "generation": generation, **outcome,
        })
        await future

    def _run_finished(self, command: KernelCommand) -> None:
        payload = command.payload
        run_id = payload["run_id"]
        run = self._store.run(run_id)
        if run is None or run.generation != payload["generation"] or \
                run.status in TERMINAL_RUN_STATUSES:
            return  # CAS 失败：迟到信号或已终态，first-writer-wins
        status = payload["status"]
        session = self._sessions[run.agent_id]
        session.clear_active_run()
        kind = {
            RunStatus.COMPLETED: "run_completed",
            RunStatus.FAILED: "run_failed",
            RunStatus.INTERRUPTED: "run_interrupted",
        }[status]
        session.append_terminal_event(run_id, kind, f"athena-event:{run_id}", payload)
        self._store.commit(command_id=command.command_id, kind="run_terminal", payload={
            "run_id": run_id, "status": status,
            "response_ref": payload.get("response_ref"),
            "error": payload.get("error"), "reason": payload.get("reason"),
        })
        self._scheduler.release_active(run_id)
        summary = self._store.run_summary(run_id)
        self._resolve_run_waiter(run_id, summary)
        self._pump_scheduler()

    def _resolve_run_waiter(self, run_id: RunId, summary: RunSummary) -> None:
        future = self._run_waiters.get(run_id)
        if future is not None and not future.done():
            future.set_result(summary)

    async def wait_run(self, run_id: RunId, timeout: float | None = None) -> RunSummary:
        """等待 Run 达到终态并返回其摘要。"""
        run = self._store.run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        future = self._run_waiters.setdefault(
            run_id, asyncio.get_event_loop().create_future()
        )
        if run.status in TERMINAL_RUN_STATUSES:
            self._resolve_run_waiter(run_id, self._store.run_summary(run_id))
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)

    def run_events(self, run_id: RunId, after_sequence: int = 0):
        """返回单 Run 事件流（§2.3 run.events）。"""
        run = self._store.run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        return self._sessions[run.agent_id].run_events(run_id, after_sequence)

    def session_events(self, agent_id: AgentId, after_sequence: int = 0):
        """返回整条 Session journal（跨 Run），供 handle.events()（§2.3）。"""
        session = self._sessions.get(agent_id)
        if session is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return session.events(after_sequence)

    def run_summary(self, run_id: RunId) -> RunSummary | None:
        return self._store.run_summary(run_id)

    def _followup(self, command: KernelCommand) -> RunId:
        payload = command.payload
        target_id = payload["target_id"]
        self._check_open(target_id)
        agent = self._store.agent(target_id)
        pending = agent.pending_run_id
        run = self._store.run(pending) if pending else None
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            raise AgentBusyError(f"agent busy with run: {pending}")
        request_ref = agent.spec.codec.encode_request(payload["task"])
        run_id = f"{target_id}:r:{self._store.sequence + 1}"
        self._store.commit(command_id=command.command_id, kind="run_queued", payload={
            "run": RunRecord(
                run_id=run_id, agent_id=target_id, parent_run_id=None,
                status=RunStatus.QUEUED, generation=0, request_ref=request_ref,
            ),
        })
        self._scheduler.enqueue(run_id)
        self._pump_scheduler()
        return run_id
```
`AgentBusyError` 需在 `kernel.py` 顶部从 types 导入。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/kernel.py test/unit/agent_kernel/test_kernel.py
git commit -m "feat(agent-kernel): add dispatch, runner execution, and terminal CAS"
```

---

## Task 6: mailbox、send_message、followup 与完成通知

**Files:**
- Modify: `src/athena/core/agent_kernel/kernel.py`
- Test: `test/unit/agent_kernel/test_kernel.py`

**Interfaces:**
- Consumes: Task 5 的 `_check_open`。
- Produces：
  - `async send_message(target_id, message) -> None`（只投递，不触发 Turn）
  - `_send_message(command)`：`_check_open` + `mailbox` 提交
  - `_commit_child_completion(run_id, summary)`：outbox 去重 + completion 消息投父 mailbox
  - `wait_agent`：冻结目标非终态 run_id；FIRST/ALL/timeout 返回部分状态，不取消目标
  - `_run_finished` 末尾接入 `_commit_child_completion`

- [ ] **Step 1: 写失败测试**

追加到 `test/unit/agent_kernel/test_kernel.py`：
```python
from athena.core.agent_kernel.types import AgentBusyError, ReturnWhen


class InboxRunner:
    """记录每轮 runner 看到的 mailbox 未提交窗口。"""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def run(self, request, *, session, emit) -> dict:
        msgs = [str(m.content) for m in session.receive_messages()]
        self.seen.append(msgs)
        return {"seen": msgs}


@pytest.mark.asyncio
async def test_send_message_then_followup_delivers_message() -> None:
    runner = InboxRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_spec(runner), {})
    await kernel.send_message(agent_id, "hello")
    run_id = await kernel.followup(agent_id, {"task": 1})
    summary = await kernel.wait_run(run_id, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    assert runner.seen == [["hello"]]
    await kernel.aclose()


@pytest.mark.asyncio
async def test_busy_followup_raises_and_changes_nothing() -> None:
    runner = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_spec(runner), {})
    await runner.started.wait()
    runs_before = {rid: r.status for rid, r in kernel._store.runs().items()}
    mailbox_before = list(kernel._store.mailbox(agent_id))
    with pytest.raises(AgentBusyError):
        await kernel.followup(agent_id, {"task": 2})
    assert {rid: r.status for rid, r in kernel._store.runs().items()} == runs_before
    assert kernel._store.mailbox(agent_id) == mailbox_before
    runner.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_child_completion_delivers_once_to_parent_mailbox() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    child_id, child_run = await kernel.spawn(parent_id, _spec(EchoRunner()), {}, name="c")
    await kernel.wait_run(child_run, timeout=2)
    completions = [m for m in kernel._store.mailbox(parent_id) if m.is_control]
    assert len(completions) == 1
    assert completions[0].content["child_run_id"] == child_run
    await kernel.aclose()


@pytest.mark.asyncio
async def test_wait_agent_all_completed_and_timeout() -> None:
    kernel = _kernel(max_active_runs=4)
    await kernel.start()
    parent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    children = [
        await kernel.spawn(parent_id, _spec(EchoRunner()), {}, name=f"c{i}")
        for i in range(3)
    ]
    result = await kernel.wait_agent(
        [cid for cid, _ in children], return_when=ReturnWhen.ALL_COMPLETED, timeout=3
    )
    assert result.timed_out is False
    assert set(result.completed) == {cid for cid, _ in children}

    blocker = BlockingRunner()
    agent_id, _ = await kernel.create_root(_spec(blocker), {"block": 1})
    await blocker.started.wait()
    timeout_result = await kernel.wait_agent([agent_id], timeout=0.05)
    assert timeout_result.timed_out is True
    assert agent_id not in timeout_result.completed
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_wait_agent_first_completed_success_is_not_timeout() -> None:
    kernel = _kernel(max_active_runs=4)
    await kernel.start()
    parent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    fast_id, _ = await kernel.spawn(parent_id, _spec(EchoRunner()), {}, name="fast")
    blocker = BlockingRunner()
    slow_id, _ = await kernel.spawn(parent_id, _spec(blocker), {}, name="slow")
    await blocker.started.wait()
    result = await kernel.wait_agent(
        [fast_id, slow_id], return_when=ReturnWhen.FIRST_COMPLETED, timeout=3
    )
    assert result.timed_out is False   # fast 已满足 FIRST_COMPLETED，不算超时
    assert fast_id in result.completed
    assert slow_id not in result.completed
    blocker.release.set()
    await kernel.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: FAIL（send_message / wait_agent 未定义）。

- [ ] **Step 3: 实现 mailbox、completion 与 wait_agent**

在 `kernel.py` 中移除 `_send_message` 占位并新增：
```python
    async def send_message(self, target_id: AgentId, message: object) -> None:
        """向目标 mailbox 投递消息；只投递，不触发 Turn（§3.3）。"""
        future = self._enqueue(
            "send_message", {"target_id": target_id, "message": message}
        )
        await asyncio.shield(future)

    def _send_message(self, command: KernelCommand) -> None:
        payload = command.payload
        target_id = payload["target_id"]
        self._check_open(target_id)
        self._store.commit(command_id=command.command_id, kind="mailbox", payload={
            "agent_id": target_id,
            "message": AgentMessage(
                source=None, content=payload["message"],
                sequence=self._store.sequence + 1,
            ),
        })

    def _commit_child_completion(self, run_id: RunId, summary: RunSummary) -> None:
        run = self._store.run(run_id)
        agent = self._store.agent(run.agent_id)
        parent_id = agent.parent_id
        if parent_id is None or self._store.outbox_for(run_id) is not None:
            return  # 无父或已投递 → 去重
        record = OutboxRecord(
            child_run_id=run_id, child_agent_id=agent.agent_id,
            parent_agent_id=parent_id, summary=summary,
        )
        self._store.commit(command_id=f"outbox:{run_id}", kind="outbox",
                           payload={"record": record})
        if self._store.agent(parent_id).status == AgentStatus.CLOSED:
            return  # 父已关闭 → 只留审计记录，不投递 mailbox
        message = AgentMessage(
            source=None, sequence=self._store.sequence + 1,
            content={
                "op": "child_completed", "child_run_id": run_id,
                "child_agent_id": agent.agent_id, "status": summary.status.value,
                "result_ref": summary.response_ref, "error": summary.error,
            },
        )
        self._store.commit(command_id=f"mailbox:{run_id}", kind="mailbox",
                           payload={"agent_id": parent_id, "message": message})

    async def wait_agent(
        self,
        target_ids: list[AgentId],
        *,
        return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        """等待目标当前 Run 终结；timeout 返回部分状态，不取消目标（§3.6）。"""
        frozen: dict[AgentId, RunId | None] = {}
        for target_id in target_ids:
            agent = self._store.agent(target_id)
            if agent is None:
                raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
            run_id = agent.pending_run_id
            run = self._store.run(run_id) if run_id else None
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                frozen[target_id] = None  # idle → 立即完成
            else:
                frozen[target_id] = run_id
        completed: dict[AgentId, RunSummary] = {}
        waits: dict[AgentId, asyncio.Task[RunSummary]] = {}
        for target_id, run_id in frozen.items():
            if run_id is None:
                continue
            run = self._store.run(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                completed[target_id] = self._store.run_summary(run_id)
            else:
                waits[target_id] = asyncio.create_task(self.wait_run(run_id))
        if not waits:
            return AgentWaitResult(completed=completed, timed_out=False)
        flag = asyncio.FIRST_COMPLETED if return_when == ReturnWhen.FIRST_COMPLETED else asyncio.ALL_COMPLETED
        done, pending = await asyncio.wait(waits.values(), return_when=flag, timeout=timeout)
        for task in pending:
            task.cancel()
        for task in done:
            target_id = next(t for t, tk in waits.items() if tk is task)
            completed[target_id] = task.result()
        # FIRST_COMPLETED 早满足时 pending 非空，但不算超时（§3.6）
        return AgentWaitResult(completed=completed, timed_out=not done and bool(pending))

    async def wait_agent_parked(
        self, target_ids: list[AgentId], *, parking_run_id: RunId | None,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        """runner 经 session 等待：期间释放本 Run 的 lease，完成后恢复（§3.6）。"""
        if parking_run_id is not None:
            self._scheduler.park(parking_run_id)
            self._pump_scheduler()
        try:
            return await self.wait_agent(target_ids, timeout=timeout)
        finally:
            # 中断/关闭可能使本 Run 先终结；终态后不再恢复 lease（§3.5 修正，Task 7 评审）
            if parking_run_id is not None:
                run = self._store.run(parking_run_id)
                if run is None or run.status not in TERMINAL_RUN_STATUSES:
                    await self._scheduler.acquire_lease(parking_run_id)
```
`_run_finished` 末尾改为：
```python
        self._resolve_run_waiter(run_id, summary)
        self._commit_child_completion(run_id, summary)
        self._pump_scheduler()
```
`AgentMessage` 需在 `kernel.py` 顶部从 types 导入。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/kernel.py test/unit/agent_kernel/test_kernel.py
git commit -m "feat(agent-kernel): add mailbox send, busy followup, and wait_agent"
```

---

## Task 7: interrupt 与 cancel_run

**Files:**
- Modify: `src/athena/core/agent_kernel/kernel.py`
- Test: `test/unit/agent_kernel/test_kernel.py`

**Interfaces:**
- Consumes: Task 5 的 `_resolve_run_waiter`、Task 6 的 `_commit_child_completion`。
- Produces：
  - `async interrupt(target_id, reason)`（提交 INTERRUPTED 终态后取消 runner，返回前 runner 已停止）
  - `async cancel_run(run_id, reason)`（精确 run id）
  - `_interrupt(command)` / `_cancel_run(command)` + 共享的 `_commit_interrupted(run, reason)`
  - idle / 重复 interrupt 幂等 no-op；QUEUED 中断不启动 runner

- [ ] **Step 1: 写失败测试**

追加到 `test/unit/agent_kernel/test_kernel.py`：
```python
from athena.core.agent_kernel.types import AgentCommandError, AgentRunInterrupted


@pytest.mark.asyncio
async def test_interrupt_running_run_stops_runner() -> None:
    runner = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_spec(runner), {})
    await runner.started.wait()
    await kernel.interrupt(agent_id, "stop it")
    assert runner.cancelled
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    assert kernel._store.run(run_id).status == RunStatus.INTERRUPTED
    assert kernel._store.run(run_id).reason == "stop it"
    await kernel.aclose()


@pytest.mark.asyncio
async def test_interrupt_queued_run_never_starts_runner() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_runs=1)
    await kernel.start()
    other_id, other_run = await kernel.create_root(
        _spec(EchoRunner()), {}, name="other"
    )
    await kernel.wait_run(other_run, timeout=2)  # other 先回 IDLE
    await kernel.create_root(_spec(blocker), {})  # blocker 占用唯一容量
    await blocker.started.wait()
    # 全局容量满 → other（空闲）的 followup Run 保持 QUEUED（§3.2）
    run2 = await kernel.followup(other_id, {})
    assert kernel._store.run(run2).status == RunStatus.QUEUED
    await kernel.interrupt(other_id, "stop queued")
    assert kernel._store.run(run2).status == RunStatus.INTERRUPTED
    blocker.release.set()
    await kernel.aclose()


@pytest.mark.asyncio
async def test_idle_and_duplicate_interrupt_are_noops() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    await kernel.interrupt(agent_id, "idle")
    await kernel.interrupt(agent_id, "again")
    assert kernel.agent_status(agent_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_cancel_run_binds_exact_run_id() -> None:
    runner = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_spec(runner), {})
    await runner.started.wait()
    await kernel.cancel_run(run_id, reason="caller_cancelled")
    assert kernel._store.run(run_id).status == RunStatus.INTERRUPTED
    assert kernel._store.run(run_id).reason == "caller_cancelled"
    runner.release.set()
    await kernel.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: FAIL（interrupt / cancel_run 未定义）。

- [ ] **Step 3: 实现 interrupt 与 cancel_run**

在 `kernel.py` 中移除占位并新增：
```python
    async def interrupt(self, target_id: AgentId, reason: str) -> None:
        """中断目标当前 Run；返回前 runner 已停止（§3.5）。"""
        agent = self._store.agent(target_id)
        run_id = agent.pending_run_id if agent else None
        # 入队前捕获：_commit_interrupted 会从 _runner_tasks 弹出并取消 task（§3.5 修正，Task 7 评审）
        task = self._runner_tasks.get(run_id) if run_id else None
        future = self._enqueue(
            "interrupt", {"target_id": target_id, "reason": reason}
        )
        await asyncio.shield(future)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # 旧 runner 已取消 → 预期行为，忽略
                pass

    async def cancel_run(self, run_id: RunId, reason: str = "caller_cancelled") -> None:
        """按精确 run id 取消一个 Run。"""
        future = self._enqueue(
            "cancel_run", {"run_id": run_id, "reason": reason}
        )
        await asyncio.shield(future)

    def _commit_interrupted(self, run: RunRecord, reason: str) -> None:
        session = self._sessions[run.agent_id]
        if run.status == RunStatus.QUEUED:
            self._scheduler.dequeue(run.run_id)
        else:
            self._scheduler.release_active(run.run_id)
        session.interrupt()
        session.append_terminal_event(
            run.run_id, "run_interrupted", f"athena-event:{run.run_id}",
            {"reason": reason},
        )
        self._store.commit(command_id=f"int:{run.run_id}:{run.generation}",
                           kind="run_terminal", payload={
                               "run_id": run.run_id, "status": RunStatus.INTERRUPTED,
                               "response_ref": None, "error": None, "reason": reason,
                           })
        summary = self._store.run_summary(run.run_id)
        self._resolve_run_waiter(run.run_id, summary)
        self._commit_child_completion(run.run_id, summary)
        task = self._runner_tasks.pop(run.run_id, None)
        if task is not None:
            task.cancel()

    def _interrupt(self, command: KernelCommand) -> None:
        payload = command.payload
        target_id = payload["target_id"]
        agent = self._store.agent(target_id)
        if agent is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
        if agent.status == AgentStatus.CLOSED:
            raise AgentCommandError(ErrorCode.CLOSED, f"agent closed: {target_id}")
        run_id = agent.pending_run_id
        run = self._store.run(run_id) if run_id else None
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return  # idle / 重复 interrupt → 幂等 no-op
        self._commit_interrupted(run, payload.get("reason", "interrupted"))
        self._pump_scheduler()

    def _cancel_run(self, command: KernelCommand) -> None:
        payload = command.payload
        run_id = payload["run_id"]
        run = self._store.run(run_id)
        if run is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown run: {run_id}")
        if run.status in TERMINAL_RUN_STATUSES:
            return
        self._commit_interrupted(run, payload.get("reason", "caller_cancelled"))
        self._pump_scheduler()
```
注意：`_commit_interrupted` 内部已 `task.cancel()`；interrupt 公开协程须在**入队前**捕获 task 再 `await`，否则 `_commit_interrupted` 弹出后拿不到引用（§3.5「返回前 runner 已停止」）。移除测试中未用的 `AgentRunInterrupted` 导入。测试 `test_interrupt_queued_run_never_starts_runner` 须用「另一空闲 agent + 全局容量满」构造 QUEUED（对 busy agent 的 followup 按 §3.3 抛 `AgentBusyError`，见 Task 6 修正）。已知 minor（Task 7 评审）：interrupt 快照 pending_run_id 与线性化点之间若当前 Run 自然终结且新 followup 已派发，公开协程等待的是旧 Run 的 task（已 done，立即返回），而非实际被中断的新 Run——极罕见竞态，为简洁接受。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/kernel.py test/unit/agent_kernel/test_kernel.py
git commit -m "feat(agent-kernel): add interrupt and exact-run cancel"
```

---

## Task 8: close 与 aclose

**Files:**
- Modify: `src/athena/core/agent_kernel/kernel.py`
- Test: `test/unit/agent_kernel/test_kernel.py`

**Interfaces:**
- Consumes: Task 7 的 `_commit_interrupted`。
- Produces：
  - `async close(target_id, *, recursive=False)`：recursive=False 遇活子孙 → `INVALID_REQUEST` 零变更；recursive=True 设子树 fence、后序关闭
  - `_close(command)` / `_close_one(agent_id)`（Task 4 占位填实）
  - `CLOSED` 后拒绝新消息/Run/子 Agent（`_check_open` 已覆盖）

- [ ] **Step 1: 写失败测试**

追加到 `test/unit/agent_kernel/test_kernel.py`：
```python
@pytest.mark.asyncio
async def test_close_nonrecursive_fails_with_live_descendants() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    child_id, _ = await kernel.spawn(parent_id, _spec(EchoRunner()), {}, name="c")
    with pytest.raises(AgentCommandError) as raised:
        await kernel.close(parent_id)
    assert raised.value.code == ErrorCode.INVALID_REQUEST
    assert kernel.agent_status(parent_id) == AgentStatus.IDLE
    assert kernel.agent_status(child_id) == AgentStatus.IDLE
    await kernel.aclose()


@pytest.mark.asyncio
async def test_close_recursive_closes_postorder_and_rejects_new_commands() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    child_id, _ = await kernel.spawn(parent_id, _spec(EchoRunner()), {}, name="c")
    await kernel.close(parent_id, recursive=True)
    assert kernel.agent_status(child_id) == AgentStatus.CLOSED
    assert kernel.agent_status(parent_id) == AgentStatus.CLOSED
    assert kernel._scheduler.resident_count == 0
    with pytest.raises(AgentCommandError) as raised:
        await kernel.followup(child_id, {})
    assert raised.value.code == ErrorCode.CLOSED
    with pytest.raises(AgentCommandError) as raised:
        await kernel.send_message(parent_id, "late")
    assert raised.value.code == ErrorCode.CLOSED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_duplicate_close_is_idempotent() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    await kernel.close(agent_id)
    await kernel.close(agent_id)
    assert kernel.agent_status(agent_id) == AgentStatus.CLOSED
    await kernel.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_all_agents() -> None:
    kernel = _kernel()
    await kernel.start()
    parent_id, _ = await kernel.create_root(_spec(EchoRunner()), {})
    await kernel.spawn(parent_id, _spec(EchoRunner()), {}, name="c")
    await kernel.aclose()
    assert kernel._scheduler.resident_count == 0
    assert all(a.status == AgentStatus.CLOSED for a in kernel._store.agents().values())


@pytest.mark.asyncio
async def test_close_queued_agent_never_dispatches() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_runs=1)
    await kernel.start()
    await kernel.create_root(_spec(blocker), {})
    await blocker.started.wait()
    # 容量满 → 第二棵 root 的初始 Run 保持 QUEUED（§3.2 pending_run_id 已记录）
    other_id, other_run = await kernel.create_root(_spec(EchoRunner()), {})
    assert kernel._store.run(other_run).status == RunStatus.QUEUED
    await kernel.close(other_id, recursive=True)
    assert kernel.agent_status(other_id) == AgentStatus.CLOSED
    assert kernel._store.run(other_run).status == RunStatus.INTERRUPTED
    blocker.release.set()
    await asyncio.sleep(0)  # 释放容量后该 Run 不得被派发
    assert kernel._store.run(other_run).status == RunStatus.INTERRUPTED
    assert kernel.agent_status(other_id) == AgentStatus.CLOSED
    await kernel.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: FAIL（close 未定义）。

- [ ] **Step 3: 实现 close**

在 `kernel.py` 中移除 `_close` 占位、填实 `_close_one`：
```python
    async def close(self, target_id: AgentId, *, recursive: bool = False) -> None:
        """关闭 Agent 子树；recursive=False 遇活子孙时失败（§3.5）。"""
        future = self._enqueue(
            "close", {"target_id": target_id, "recursive": recursive}
        )
        await asyncio.shield(future)

    def _close(self, command: KernelCommand) -> None:
        payload = command.payload
        target_id = payload["target_id"]
        recursive = payload.get("recursive", False)
        agent = self._store.agent(target_id)
        if agent is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {target_id}")
        if agent.status == AgentStatus.CLOSED:
            return  # 重复 close → 幂等 no-op
        if not recursive:
            if self._registry.has_live_descendants(target_id):
                raise AgentCommandError(
                    ErrorCode.INVALID_REQUEST,
                    "cannot close agent with live descendants; use recursive=True",
                )
        subtree = self._registry.post_order(target_id)
        self._fences.update(subtree)
        for aid in subtree:
            self._close_one(aid)
        self._fences.difference_update(subtree)  # 子树已 CLOSED，清除整棵 fence（Task 8 评审 M1）
        self._pump_scheduler()  # 关闭释放的容量唤醒排队 Run（与 interrupt 一致）

    def _close_one(self, agent_id: AgentId) -> None:
        """关闭单个 Agent：中断其 Run、关闭会话、写 CLOSED tombstone。"""
        agent = self._store.agent(agent_id)
        if agent is None or agent.status == AgentStatus.CLOSED:
            return
        run = self._store.run(agent.pending_run_id) if agent.pending_run_id else None
        if run is not None and run.status not in TERMINAL_RUN_STATUSES:
            self._commit_interrupted(run, "agent closed")
        session = self._sessions.get(agent_id)
        if session is not None:
            session.close()
        self._store.commit(command_id=f"closed:{agent_id}", kind="agent_closed",
                           payload={"agent_id": agent_id})
        self._scheduler.release_resident(agent_id)
```
`aclose` 使用 `_close_one`（Task 4 已写入 `for agent_id in list(...): self._close_one(agent_id)`，此时填实后生效）。
注意（Task 8 评审 B1 修正）：`_spawn` 的初始 `AgentRecord` 必须设 `pending_run_id=run_id`（§3.2「QUEUED 时记录唯一 pending_run_id」），否则初始 Run 尚未派发时 `_close_one`/`_followup`/`_interrupt` 都找不到它——close 会留下 ready 队列中的 stale Run，容量释放后把 CLOSED Agent 复活成 RUNNING/IDLE。修正在 `_spawn` 的 spawn commit 中为 AgentRecord 增加 `pending_run_id=run_id`。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/kernel.py test/unit/agent_kernel/test_kernel.py
git commit -m "feat(agent-kernel): add close with subtree fence and closed tombstone"
```

---

## Task 9: PARKED 等待与 crash recovery

**Files:**
- Modify: `src/athena/core/agent_kernel/kernel.py`
- Test: `test/unit/agent_kernel/test_kernel.py`

**Interfaces:**
- Consumes: Task 6 的 `wait_agent_parked`、Task 5 的 `_resolve_run_waiter`。
- Produces：
  - 父 Run 等子 Run 在 `max_active_runs == 1` 时不死锁（`session.wait_agents` 已在 Task 3 定义）
  - `@classmethod async from_snapshot(snapshot, journal, resources_factory, max_agents, max_active_runs)`：重放 journal、重建 session、RUNNING→`FAILED(kernel_restarted)`、QUEUED 重入 ready

- [ ] **Step 1: 写失败测试**

追加到 `test/unit/agent_kernel/test_kernel.py`：
```python
from athena.core.agent_kernel.store import AgentGraphStore, StoreSnapshot
from athena.core.agent_kernel.types import AgentWaitResult


class ParentWaiter:
    """父 runner：spawn 子 Agent 后经 session.wait_agents 等它完成。"""

    def __init__(self, kernel: AgentKernel) -> None:
        self.kernel = kernel
        self.result: AgentWaitResult | None = None

    async def run(self, request, *, session, emit) -> dict:
        _, child_run = await self.kernel.spawn(
            session.agent_id, _spec(EchoRunner()), {}, name="kid"
        )
        self.result = await session.wait_agents(
            [session.agent_id + "/kid"], timeout=5
        )
        return {"child": self.result.completed[session.agent_id + "/kid"].status.value}


@pytest.mark.asyncio
async def test_parent_waits_child_without_deadlock_at_one_slot() -> None:
    kernel = _kernel(max_active_runs=1)
    await kernel.start()
    runner = ParentWaiter(kernel)
    parent_id, parent_run = await kernel.create_root(_spec(runner), {})
    summary = await kernel.wait_run(parent_run, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert summary.response_ref == '{"child": "completed"}'
    assert runner.result is not None
    await kernel.aclose()


@pytest.mark.asyncio
async def test_recover_replays_journal_for_completed_run() -> None:
    kernel = _kernel()
    await kernel.start()
    agent_id, run1 = await kernel.create_root(_spec(EchoRunner()), {"q": 1})
    await kernel.wait_run(run1, timeout=2)
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    await kernel.aclose()

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot, journal=journal,
        resources_factory=InMemoryResourcesFactory(), max_agents=32, max_active_runs=8,
    )
    await recovered.start()
    assert recovered.agent_status(agent_id) == AgentStatus.IDLE
    assert recovered.run_summary(run1).status == RunStatus.COMPLETED
    await recovered.aclose()


@pytest.mark.asyncio
async def test_recover_marks_crashed_running_run_as_failed() -> None:
    blocker = BlockingRunner()
    kernel = _kernel()
    await kernel.start()
    agent_id, run_id = await kernel.create_root(_spec(blocker), {})
    await blocker.started.wait()
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot, journal=journal,
        resources_factory=InMemoryResourcesFactory(), max_agents=32, max_active_runs=8,
    )
    await recovered.start()
    summary = recovered.run_summary(run_id)
    assert summary is not None
    assert summary.status == RunStatus.FAILED
    assert summary.error == "kernel_restarted"
    assert recovered.agent_status(agent_id) == AgentStatus.IDLE

    blocker.release.set()
    await kernel.aclose()
    await recovered.aclose()


@pytest.mark.asyncio
async def test_recover_resumes_queued_run() -> None:
    blocker = BlockingRunner()
    kernel = _kernel(max_active_runs=1)
    await kernel.start()
    other_id, other_run = await kernel.create_root(
        _spec(EchoRunner()), {}, name="other"
    )
    await kernel.wait_run(other_run, timeout=2)  # other 先回 IDLE
    await kernel.create_root(_spec(blocker), {})  # blocker 占用唯一容量
    await blocker.started.wait()
    # 全局容量满 → other（空闲）的 followup Run 保持 QUEUED（§3.2）
    queued = await kernel.followup(other_id, {})
    assert kernel._store.run(queued).status == RunStatus.QUEUED
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    blocker.release.set()
    await kernel.aclose()

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot, journal=journal,
        resources_factory=InMemoryResourcesFactory(), max_agents=32, max_active_runs=1,
    )
    await recovered.start()
    summary = await recovered.wait_run(queued, timeout=2)
    assert summary.status == RunStatus.COMPLETED
    await recovered.aclose()
```
说明：该测试必须先建 idle 的 `other` 并等其回 IDLE，再建 blocker 占用唯一容量；否则 `other_run` 永远 QUEUED，`wait_run(other_run)` 在 setup 阶段超时（Task 7 同款构造）。


@pytest.mark.asyncio
async def test_recover_reserves_all_agents() -> None:
    kernel = _kernel(max_agents=4)
    await kernel.start()
    parent_id, parent_run = await kernel.create_root(_spec(EchoRunner()), {})
    child_id, child_run = await kernel.spawn(
        parent_id, _spec(EchoRunner()), {}, name="c"
    )
    await kernel.wait_run(parent_run, timeout=2)
    await kernel.wait_run(child_run, timeout=2)
    snapshot = kernel._store.snapshot()
    journal = kernel._store.journal
    await kernel.aclose()

    recovered = await AgentKernel.from_snapshot(
        snapshot=snapshot, journal=journal,
        resources_factory=InMemoryResourcesFactory(), max_agents=2, max_active_runs=8,
    )
    await recovered.start()
    assert recovered._scheduler.resident_count == 2  # root + child 都计数（Task 9 评审修正）
    with pytest.raises(AgentCommandError):
        await recovered.create_root(_spec(EchoRunner()), {})  # 超限
    await recovered.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: FAIL（from_snapshot 未定义；死锁测试超时）。

- [ ] **Step 3: 实现恢复**

在 `kernel.py` 中新增：
```python
    @classmethod
    async def from_snapshot(
        cls,
        *,
        snapshot: StoreSnapshot,
        journal: list[JournalRecord],
        resources_factory: SessionResourcesFactory,
        max_agents: int,
        max_active_runs: int,
    ) -> "AgentKernel":
        """从快照 + journal 重建内核（§3.7）。"""
        store = AgentGraphStore()
        store.load(snapshot, journal)
        kernel = cls(
            resources_factory=resources_factory,
            max_agents=max_agents, max_active_runs=max_active_runs, store=store,
        )
        kernel._rebuild_sessions()
        kernel._recover()
        return kernel

    def _rebuild_sessions(self) -> None:
        for agent_id, agent in self._store.agents().items():
            if agent.status == AgentStatus.CLOSED:
                continue
            try:
                resources = self._resources_factory.create(agent_id, agent.spec)
            except Exception:
                # 资源重建失败 → Agent 进入 ERROR，不阻断其它恢复
                self._store.commit(command_id=f"err:{agent_id}", kind="agent_error",
                                   payload={"agent_id": agent_id})
                continue
            session = AgentSession(
                agent_id=agent_id, spec=agent.spec, resources=resources,
                store=self._store, kernel=self,
            )
            self._sessions[agent_id] = session
            self._scheduler.try_reserve(agent_id)  # 每个非 CLOSED Agent 都计数（与 _spawn 一致，Task 9 评审修正）

    def _recover(self) -> None:
        for run_id, run in self._store.runs().items():
            if run.status == RunStatus.QUEUED:
                self._scheduler.enqueue(run_id)
            elif run.status == RunStatus.RUNNING:
                # crash 时仍 RUNNING 且无 terminal marker → 新 generation FAILED
                session = self._sessions.get(run.agent_id)
                if session is not None:
                    session.interrupt()
                run.generation += 1
                self._store.commit(command_id=f"restart:{run_id}", kind="run_terminal",
                                   payload={"run_id": run_id, "status": RunStatus.FAILED,
                                            "response_ref": None, "error": "kernel_restarted",
                                            "reason": None})
                self._scheduler.release_active(run_id)
                self._resolve_run_waiter(run_id, self._store.run_summary(run_id))
            elif run.status in TERMINAL_RUN_STATUSES:
                self._resolve_run_waiter(run_id, self._store.run_summary(run_id))
        self._pump_scheduler()  # 重放的 QUEUED Run 恢复执行（容量已清空）
```
说明：`run.status == RunStatus.RUNNING` 时 store 中 agent 状态为 RUNNING；`run_terminal` 的 `_apply` 会把 agent 置回 IDLE（仅当 `agent.status == RUNNING`）。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_kernel.py -q`
Expected: PASS（含父等子不死锁与两个恢复测试）。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/kernel.py test/unit/agent_kernel/test_kernel.py
git commit -m "feat(agent-kernel): add parked wait and crash recovery"
```

---

## Task 10: AgentControl 门面与 AgentHandle / AgentRun

**Files:**
- Create: `src/athena/core/agent_kernel/control.py`
- Test: `test/unit/agent_kernel/test_control.py`

**Interfaces:**
- Consumes: Task 1-9 全部。
- Produces：
  - `AgentControl(kernel)`：`create_root / spawn / send_message / followup / wait_agent / interrupt / list_agents / close / aclose`（签名对齐设计 §2.4）
  - `AgentHandle(agent_id, control)`：`path / name / role / status`（实时查询 Kernel）、`events(after_sequence)`（跨 Run journal，§2.3）
  - `AgentRun(run_id, control, codec)`：`wait()`（解码响应；INTERRUPTED→`AgentRunInterrupted`、FAILED→`AgentRunFailed`）、`cancel()`、`events()`、`summary()`

- [ ] **Step 1: 写失败测试**

`test/unit/agent_kernel/test_control.py`:
```python
import pytest

from athena.core.agent_kernel.control import AgentControl, AgentHandle, AgentRun
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import AgentRunFailed, AgentRunInterrupted, AgentSpec

from ._support import BlockingRunner, EchoRunner, JsonCodec, collect_events


def _spec(runner=None) -> AgentSpec:
    return AgentSpec(runner=runner or EchoRunner(), codec=JsonCodec(), role="debater")


def _control(**kw) -> AgentControl:
    return AgentControl(AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw))


@pytest.mark.asyncio
async def test_create_root_returns_handle_and_run() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_spec(), {"q": 1})
    assert isinstance(handle, AgentHandle)
    assert isinstance(run, AgentRun)
    assert handle.agent_id == "root"
    assert handle.name == "root"
    assert handle.path == ("root",)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_decodes_response() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_spec(), {"q": 1})
    response = await run.wait(timeout=2)
    assert response == {"echo": {"q": 1}}
    summary = await run.summary()
    assert summary.status.value == "completed"
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_followup_and_run_events_round_trip() -> None:
    control = _control()
    await control.kernel.start()
    parent, _ = await control.create_root(_spec(), {})
    child, child_run = await control.spawn(parent, _spec(), {}, name="kid")
    assert child.path == ("root", "kid")
    follow = await control.followup(child, {"again": 2})
    resp = await follow.wait(timeout=2)
    assert resp == {"echo": {"again": 2}}
    events = await collect_events(follow.events(after_sequence=0), 2)
    assert [e.kind for e in events] == ["agent/step", "run_completed"]
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_handle_events_span_runs() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_spec(), {"q": 1})
    await run.wait(timeout=2)
    follow = await control.followup(handle, {"q": 2})
    await follow.wait(timeout=2)
    events = await collect_events(handle.events(after_sequence=0), 4)
    assert len(events) == 4  # 两轮各：agent/step + run_completed
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_raises_on_interrupt() -> None:
    control = _control()
    await control.kernel.start()
    runner = BlockingRunner()
    handle, run = await control.create_root(_spec(runner), {})
    await runner.started.wait()
    await control.interrupt(handle, "stop")
    with pytest.raises(AgentRunInterrupted):
        await run.wait(timeout=2)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_raises_on_failed() -> None:
    class FailingRunner:
        async def run(self, request, *, session, emit) -> dict:
            raise RuntimeError("boom")

    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_spec(FailingRunner()), {})
    with pytest.raises(AgentRunFailed):
        await run.wait(timeout=2)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_list_agents_with_prefix() -> None:
    control = _control()
    await control.kernel.start()
    parent, _ = await control.create_root(_spec(), {})
    await control.spawn(parent, _spec(), {}, name="a")
    await control.spawn(parent, _spec(), {}, name="b")
    ids = {s.agent_id for s in control.list_agents(path_prefix=("root",))}
    assert ids == {"root", "root/a", "root/b"}
    await control.kernel.aclose()
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `uv run pytest test/unit/agent_kernel/test_control.py -q`
Expected: 收集错误。

- [ ] **Step 3: 实现 control**

`src/athena/core/agent_kernel/control.py`:
```python
"""AgentControl — 面向调用方的能力门面（设计 §1.2、§2.4）。"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Generic

from athena.core.agent_kernel.types import (
    AgentEvent,
    AgentId,
    AgentPath,
    AgentSnapshot,
    AgentSpec,
    AgentStatus,
    AgentWaitResult,
    ForkPolicy,
    ResponseT,
    ReturnWhen,
    RunId,
    RunSummary,
    RunStatus,
    AgentRunFailed,
    AgentRunInterrupted,
)

if TYPE_CHECKING:
    from athena.core.agent_kernel.kernel import AgentKernel


class AgentHandle:
    """指向稳定 Agent 身份的 capability；状态实时查询 Kernel（§2.3）。"""

    def __init__(self, agent_id: AgentId, control: "AgentControl") -> None:
        self.agent_id = agent_id
        self._control = control

    @property
    def path(self) -> AgentPath:
        return self._snapshot().path

    @property
    def name(self) -> str:
        return self._snapshot().name

    @property
    def role(self) -> str:
        return self._snapshot().role

    @property
    def status(self) -> AgentStatus:
        status = self._control.kernel.agent_status(self.agent_id)
        if status is None:
            raise KeyError(f"unknown agent: {self.agent_id}")
        return status

    async def snapshot(self) -> AgentSnapshot:
        return self._snapshot()

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        """读取本 Agent 的 Session journal（跨 Run，§2.3）。"""
        return self._control.kernel.session_events(self.agent_id, after_sequence)

    def _snapshot(self) -> AgentSnapshot:
        snap = self._control.kernel.agent_snapshot(self.agent_id)
        if snap is None:
            raise KeyError(f"unknown agent: {self.agent_id}")
        return snap


class AgentRun(Generic[ResponseT]):
    """指向一次 Run 的 capability；不拥有执行 task（§2.3）。"""

    def __init__(self, run_id: RunId, control: "AgentControl", codec: Any) -> None:
        self.run_id = run_id
        self._control = control
        self._codec = codec

    async def wait(self, timeout: float | None = None) -> ResponseT:
        """等待 Run 终态并解码响应；INTERRUPTED/FAILED 抛领域异常。"""
        summary = await self._control.kernel.wait_run(self.run_id, timeout=timeout)
        if summary.status == RunStatus.INTERRUPTED:
            raise AgentRunInterrupted(summary.reason or "run interrupted")
        if summary.status == RunStatus.FAILED:
            raise AgentRunFailed(summary.error or "run failed")
        if summary.response_ref is None:
            raise RuntimeError("completed run has no response reference")
        return self._codec.decode_response(summary.response_ref)

    async def cancel(self, reason: str = "caller_cancelled") -> None:
        await self._control.kernel.cancel_run(self.run_id, reason=reason)

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        return self._control.kernel.run_events(self.run_id, after_sequence)

    async def summary(self) -> RunSummary:
        summary = self._control.kernel.run_summary(self.run_id)
        if summary is None:
            raise KeyError(f"unknown run: {self.run_id}")
        return summary


class AgentControl:
    """对 Agent 树执行命令的唯一公开控制面（§2.4）。"""

    def __init__(self, kernel: "AgentKernel") -> None:
        self.kernel = kernel

    async def create_root(
        self, spec: AgentSpec, task: object, *, name: str = "root"
    ) -> tuple[AgentHandle, AgentRun]:
        agent_id, run_id = await self.kernel.create_root(spec, task, name=name)
        return AgentHandle(agent_id, self), AgentRun(run_id, self, spec.codec)

    async def spawn(
        self,
        parent: AgentHandle,
        spec: AgentSpec,
        task: object,
        *,
        name: str | None = None,
        fork: ForkPolicy = ForkPolicy.none(),
    ) -> tuple[AgentHandle, AgentRun]:
        agent_id, run_id = await self.kernel.spawn(
            parent.agent_id, spec, task, name=name, fork=fork
        )
        return AgentHandle(agent_id, self), AgentRun(run_id, self, spec.codec)

    async def send_message(self, target: AgentHandle, message: object) -> None:
        await self.kernel.send_message(target.agent_id, message)

    async def followup(self, target: AgentHandle, task: object) -> AgentRun:
        run_id = await self.kernel.followup(target.agent_id, task)
        spec = self.kernel.registry_spec(target.agent_id)
        return AgentRun(run_id, self, spec.codec)

    async def wait_agent(
        self,
        targets: list[AgentHandle],
        *,
        return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED,
        timeout: float | None = None,
    ) -> AgentWaitResult:
        return await self.kernel.wait_agent(
            [t.agent_id for t in targets], return_when=return_when, timeout=timeout
        )

    async def interrupt(self, target: AgentHandle, reason: str) -> None:
        await self.kernel.interrupt(target.agent_id, reason)

    def list_agents(self, path_prefix: AgentPath | None = None) -> list[AgentSnapshot]:
        return self.kernel.list_agents(path_prefix)

    async def close(self, target: AgentHandle, *, recursive: bool = False) -> None:
        await self.kernel.close(target.agent_id, recursive=recursive)

    async def aclose(self) -> None:
        await self.kernel.aclose()
```
（说明：`control.py` 的导入顺序按字母排序；`AgentRunFailed`/`AgentRunInterrupted` 与其它 types 一并从 `athena.core.agent_kernel.types` 导入。）

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_control.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/athena/core/agent_kernel/control.py test/unit/agent_kernel/test_control.py
git commit -m "feat(agent-kernel): add control facade with handles and runs"
```

---

## Task 11: 包导出、验收不变式与文档

**Files:**
- Modify: `src/athena/core/agent_kernel/__init__.py`
- Create: `test/unit/agent_kernel/test_invariants.py`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: Task 1-10 全部。
- Produces: 包公开面 `__all__`；§3.8 验收不变式端到端测试。

- [ ] **Step 1: 写失败测试**

`src/athena/core/agent_kernel/__init__.py`:
```python
"""Athena 统一 Agent 内核（设计 §1-§3）。公开面只含调用方能力与契约。"""

from athena.core.agent_kernel.control import AgentControl, AgentHandle, AgentRun
from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentBusyError,
    AgentCommandError,
    AgentError,
    AgentMessage,
    AgentRunFailed,
    AgentRunInterrupted,
    AgentSnapshot,
    AgentSpec,
    AgentStatus,
    AgentWaitResult,
    ErrorCode,
    ForkPolicy,
    ReturnWhen,
    RunStatus,
    RunSummary,
)

__all__ = [
    "AgentBusyError",
    "AgentCommandError",
    "AgentControl",
    "AgentError",
    "AgentHandle",
    "AgentMessage",
    "AgentRun",
    "AgentRunFailed",
    "AgentRunInterrupted",
    "AgentSnapshot",
    "AgentSpec",
    "AgentStatus",
    "AgentWaitResult",
    "ErrorCode",
    "ForkPolicy",
    "ReturnWhen",
    "RunStatus",
    "RunSummary",
    "TERMINAL_RUN_STATUSES",
]
```

`test/unit/agent_kernel/test_invariants.py`:
```python
"""§3.8 验收不变式的端到端验证。"""

import pytest

from athena.core.agent_kernel import __all__ as kernel_exports
from athena.core.agent_kernel.control import AgentControl
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import AgentBusyError, AgentCommandError, AgentSpec

from ._support import BlockingRunner, EchoRunner, JsonCodec, collect_until_terminal


def _spec(runner=None, role="agent") -> AgentSpec:
    return AgentSpec(runner=runner or EchoRunner(), codec=JsonCodec(), role=role)


def _control(**kw) -> AgentControl:
    return AgentControl(AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw))


def test_public_exports_are_exact() -> None:
    assert kernel_exports == [
        "AgentBusyError", "AgentCommandError", "AgentControl", "AgentError",
        "AgentHandle", "AgentMessage", "AgentRun", "AgentRunFailed",
        "AgentRunInterrupted", "AgentSnapshot", "AgentSpec", "AgentStatus",
        "AgentWaitResult", "ErrorCode", "ForkPolicy", "ReturnWhen",
        "RunStatus", "RunSummary", "TERMINAL_RUN_STATUSES",
    ]


@pytest.mark.asyncio
async def test_invariant_same_agent_never_has_two_nonterminal_runs() -> None:
    control = _control()
    await control.kernel.start()
    runner = BlockingRunner()
    handle, run = await control.create_root(_spec(runner), {})
    await runner.started.wait()
    with pytest.raises(AgentBusyError):
        await control.followup(handle, {})
    runner.release.set()
    await run.wait(timeout=2)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_each_run_has_one_terminal_event() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_spec(), {})
    await run.wait(timeout=2)
    events = await collect_until_terminal(run.events())
    terminal = [
        e for e in events
        if e.kind in ("run_completed", "run_failed", "run_interrupted")
    ]
    assert len(terminal) == 1
    # 事件流为订阅式：终态后短超时探测下一事件，确认无第二个终态事件（Task 11 评审修正）
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run.events(after_sequence=events[-1].sequence).__anext__(), timeout=0.2
        )
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_busy_followup_changes_nothing() -> None:
    control = _control()
    await control.kernel.start()
    runner = BlockingRunner()
    handle, run = await control.create_root(_spec(runner), {})
    await runner.started.wait()
    runs_before = {rid: r.status for rid, r in control.kernel._store.runs().items()}
    mailbox_before = list(control.kernel._store.mailbox(handle.agent_id))
    with pytest.raises(AgentBusyError):
        await control.followup(handle, {"x": 1})
    assert {rid: r.status for rid, r in control.kernel._store.runs().items()} == runs_before
    assert control.kernel._store.mailbox(handle.agent_id) == mailbox_before
    runner.release.set()
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_parent_wait_child_no_deadlock_at_one_slot() -> None:
    control = _control(max_active_runs=1)
    await control.kernel.start()

    async def spawn_child(parent_id):
        return await control.kernel.spawn(parent_id, _spec(), {}, name="kid")

    class ParentWaiter:
        def __init__(self):
            self.result = None

        async def run(self, request, *, session, emit):
            _, _ = await spawn_child(session.agent_id)
            self.result = await session.wait_agents([session.agent_id + "/kid"], timeout=5)
            return {"ok": True}

    runner = ParentWaiter()
    parent_handle, parent_run = await control.create_root(_spec(runner), {})
    await parent_run.wait(timeout=5)
    assert runner.result is not None
    assert runner.result.timed_out is False  # 子 Run 已完成而非超时（Task 11 评审修正）
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_invariant_closed_agent_rejects_new_messages_runs_and_children() -> None:
    control = _control()
    await control.kernel.start()
    handle, _ = await control.create_root(_spec(), {})
    await control.close(handle)
    with pytest.raises(AgentCommandError):
        await control.send_message(handle, "late")
    with pytest.raises(AgentCommandError):
        await control.followup(handle, {})
    with pytest.raises(AgentCommandError):
        await control.kernel.spawn(handle.agent_id, _spec(), {})
    await control.kernel.aclose()
```

注意（Task 11 评审修正）：`_support.py` 的 `collect_until_terminal` 原以首个 `run_` 前缀事件为终态，导致「恰一个终态事件」断言恒真。改为只在 `run_completed`/`run_failed`/`run_interrupted` 三种终态 kind 中断，使其真正可被重复终态事件击穿。`__init__.py` docstring「公开面只含调用方能力与契约」略有夸大（`AgentRunner`/`AgentCodec`/`AgentEvent` 未重导出）——按最终措辞收敛即可，`__all__` 保持不动（`test_public_exports_are_exact` 锁定）。

- [ ] **Step 2: 运行测试确认 GREEN**

Run: `uv run pytest test/unit/agent_kernel/test_invariants.py -q`
Expected: PASS（若 `__all__` 顺序不符则按断言校正）。

- [ ] **Step 3: 全量回归**

Run:
```bash
uv run pytest test/unit/agent_kernel -q
uv run pytest tests/test_ideator.py test/unit/test_agent.py test/unit/app_server -q
```
Expected: 全部 PASS，既有实现无回归。

- [ ] **Step 4: 格式与 diff 检查**

Run:
```bash
uv run black --check src/athena/core/agent_kernel test/unit/agent_kernel
git diff --check -- src/athena/core/agent_kernel test/unit/agent_kernel
```
Expected: 通过。

- [ ] **Step 5: 更新 docs/README.md**

在「设计规格」表「统一 Agent 内核设计」行下方追加：
```markdown
| [统一 Agent 内核实现计划](superpowers/plans/2026-08-05-athena-unified-agent-kernel.md) | current | 内核核心（§1-§3 + §4.4/§4.5 契约）分步实现 |
```

- [ ] **Step 6: 提交**

```bash
git add src/athena/core/agent_kernel/__init__.py test/unit/agent_kernel/test_invariants.py docs/README.md docs/superpowers/plans/2026-08-05-athena-unified-agent-kernel.md
git commit -m "feat(agent-kernel): export public surface and verify acceptance invariants"
```

---

## Self-Review（对照 2026-08-05 版规格）

- **§3.1 单序列器 + command_id 幂等** → Task 4（`_serializer_loop`、`_enqueue` 幂等）、Task 2（`record_result`/`result_for`）。
- **§3.2 状态机 + pending_run_id busy 判定** → Task 4/5（spawn、run_running/run_terminal）；busy 以「存在非终态 Run」判定。
- **§3.3 mailbox + followup 合一事务 + AgentBusyError** → Task 3（游标）、Task 6（send/followup/busy）。
- **§3.4 终态 CAS + 单 terminal event + outbox 父子通知** → Task 5（CAS）、Task 6（outbox 去重 + completion）。
- **§3.5 interrupt/close 同一终态 CAS + 幂等 no-op + CLOSED** → Task 7、Task 8。
- **§3.6 调度容量分离 + PARKED 父等子不死锁 + wait 冻结/超时** → Task 4（scheduler）、Task 6（wait_agent）、Task 9（parked）。
- **§3.7 crash recovery** → Task 9（from_snapshot、kernel_restarted、资源失败→ERROR）。
- **§3.8 验收不变式** → Task 11（test_invariants.py）。
- **§2.3 handle.events() 跨 Run journal / run.events() 单 Run** → Task 3、Task 5、Task 10。
- **§4.5 错误层（AgentCommandError + code + retryable + details）** → Task 1（ErrorCode 与异常）、Task 4-8（NOT_FOUND/CLOSED/LIMIT_REACHED/INVALID_REQUEST/BUSY）。
- **§4.4 默认运行配置（32/8/4）** → Task 4（构造参数与 max_spawn_depth 校验）。

**不在本计划范围（后续）**：§4.1 能力模型与鉴权矩阵、§4.7 SQLite 持久化、§4.6 fork 继承、§4.3 BudgetConfig、§4.2 限流与数据边界、§5 迁移与旧实现删除。

---

## 说明

- `AgentSession.checkpoint()` 直接提交 store（同步无 await），不经序列器；store 无 await 点，单线程下与序列器互不穿插。
- 资源构造等可失败步骤先于 `try_reserve`；预留失败只产生可回收的孤儿资源（`aclose` 统一清理）。`encode_request` 的失败以 `try/except` 映射为 `CODEC_ERROR`（有明确触发场景，符合规范）。
- `AgentSession.events()` / `run_events()` 是**订阅式** live 迭代器（持续等待新事件，§5.1 app-server Thread 级续读语义，与现有 `EventJournal.read_from` 一致），不会自然结束。测试收集事件必须用有界收集：`_support.collect_events(source, n)` 收 n 条后终止，或 `collect_until_terminal(source)` 收到 `run_` 终态事件为止；Task 3 的测试文件用文件内局部 `_collect(source, n)` 助手。
- `AgentScheduler` 的 lease 唤醒与 Session 事件同样用 `asyncio.Event`（`release_active`/`park` 同步调用 `set()`；`acquire_lease` 用 check–clear–recheck–wait 循环）。不要用 `asyncio.Condition.notify_all()` —— 它要求持有锁，序列器同步调用时无法持有，会导致 PARKED 恢复路径永久死锁（Python 3.10+ 上还会直接抛 `RuntimeError`）。
- 每模块按 `docs/代码规范.md` 编写：导入置顶、字符串前向引用、`except` 注释触发场景、嵌套 ≤ 3 层、公开函数 docstring。
