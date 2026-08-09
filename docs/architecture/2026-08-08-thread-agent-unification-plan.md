# Thread 归一的 Agent 运行时统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Agent 执行统一到 `app_server` 的 Thread 模型上(Codex 风格),退役 `agent_kernel`,同时保留其高层抽象(`AgentControl`/`AgentHandle`/`AgentRun`/types/registry/codec)并迁入 `athena.core.agent`。

**Architecture:** 每个逻辑 Agent = 一条 `ThreadRuntime`(`agent_id == thread_id`)。新 `AgentRuntime`(core/agent/agent_runtime.py)作为门面协调层包装一个 `RuntimeThreadManager`,提供 kernel 原有实例方法(mailbox / WaitRegistry / run↔turn 映射 / status 投影)。app_server 只加两个最小挂钩:turn 终态回调 + 确定性 rollout 路径。迁移顺序:先建门面(临时引用仍存在的 `agent_kernel` 符号)→ 接线 ProjectRuntime → 最后一次性把抽象文件 `git mv` 进 core.agent 并删除 agent_kernel 剩余文件。**不建任何 re-export 垫片,不改将被删除的 kernel 文件内部**。

**Tech Stack:** Python 3.12, asyncio, pydantic, app_server ThreadRuntime(Codex 风格), memory(ContextManager/RolloutRecorder), pytest-asyncio(auto 模式)。

## Global Constraints

- **COMPAT 标注**:任何兼容/迁移层代码必须以 `# COMPAT:` 注释开头,写清保留的旧契约与清理条件;禁止无标注兼容分支。
- **无全局并发上限**:仅每 Thread 串行;不得新增全局信号量/调度器。
- **持久化**:线程对话经 `RolloutRecorder` JSONL,确定性路径 `{project}/.athena/sessions/{agent_id}.jsonl`;`project.json` 只存确定性事实,去掉 `store` 字段;活 wait/human-wait 不跨重启。
- **兼容面**:`response_ref` 必须保持 `json.loads(summary.response_ref)["result_ref"]` 可解;`BaseAgentRunner` 主体不动;业务 Agent(`BaseAgent.run(AgentContext)`)零改动。
- **测试基建**:`test/unit/agent/` 需创建 `__init__.py` 与 `_support.py`(仿 `test/unit/app_server/_support.py` 的相对导入惯例);pytest-asyncio `asyncio_mode="auto"` 已启用。
- 提交前 `git status` 核对(可能并行进程在动 `src/athena/core/agent_kernel`),只 add 本次任务涉及文件。

---

## Phase 1 — app_server 挂钩

### Task 1: `ThreadRuntime.on_turn_terminal` 终态回调

**Files:**
- Modify: `src/athena/app_server/thread_runtime.py`
- Test: `test/unit/app_server/test_thread_runtime_terminal_hook.py` (Create)

**Interfaces:**
- Produces: `ThreadRuntime.__init__(..., on_turn_terminal: Callable[[str, TurnTerminalState], None] | None = None)`;回调在 `commit_completed/commit_failed/commit_interrupted` 三处同步调用,参数 `(turn_id, TurnTerminalState)`。

- [ ] **Step 1: 写失败测试**

```python
# test/unit/app_server/test_thread_runtime_terminal_hook.py
import asyncio

from athena.app_server.submissions import StartTurn, Submission
from athena.app_server.thread_runtime import ThreadRuntime


class _DoneRunner:
    """run_with_context 立即返回终态。"""
    async def run_with_context(self, thread, turn, emit, memory, cancel):
        return _Outcome()


class _Outcome:
    result_ref = "art:result"
    next_context_ref = "art:ctx"


async def test_terminal_hook_fires_on_completion():
    calls: list[tuple[str, TurnTerminalState]] = []
    rt = ThreadRuntime(
        thread_id="t1", session_id="s1", context_ref="c1",
        runner=_DoneRunner(), on_turn_terminal=lambda tid, ts: calls.append((tid, ts)),
    )
    await rt.start()
    try:
        turn_id = "turn1"
        await rt.submission_queue.put(
            Submission(id=turn_id, op=StartTurn(turn_id=turn_id, request_ref="req1"))
        )
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "on_turn_terminal never fired"
        tid, terminal = calls[0]
        assert tid == turn_id
        assert terminal.result_ref == "art:result"
    finally:
        await rt.force_close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest test/unit/app_server/test_thread_runtime_terminal_hook.py -v`
Expected: FAIL — `TypeError: ThreadRuntime.__init__() got an unexpected keyword argument 'on_turn_terminal'`

- [ ] **Step 3: 实现钩子**

在 `src/athena/app_server/thread_runtime.py`:
1. `__init__` 签名追加参数并保存:
```python
on_turn_terminal: Callable[[str, "TurnTerminalState"], None] | None = None,
```
```python
        self._on_turn_terminal = on_turn_terminal
```
2. 新增私有方法:
```python
    def _fire_turn_terminal(self, turn_id: str, terminal: "TurnTerminalState") -> None:
        """终态回调:同步、轻量,内部不得 await;供门面调度异步唤醒。"""
        if self._on_turn_terminal is not None:
            self._on_turn_terminal(turn_id, terminal)
```
3. 三处终态提交末尾(各自 `await self._execution_observer.xxx` 之后)调用:
- `commit_completed`: `self._fire_turn_terminal(turn_id, TurnTerminalState(result_ref=result_ref, next_context_ref=next_context_ref))`
- `commit_failed`: `self._fire_turn_terminal(turn_id, TurnTerminalState(exception_type=exception_type))`
- `commit_interrupted`: `self._fire_turn_terminal(turn_id, TurnTerminalState(cancelled=True))`
- 确保 `TurnTerminalState` 已从 `athena.app_server.submissions` 导入。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest test/unit/app_server/ -v`
Expected: 新测试 PASS,既有 ~17 个测试模块无回归。

- [ ] **Step 5: 提交**

```bash
git add src/athena/app_server/thread_runtime.py test/unit/app_server/test_thread_runtime_terminal_hook.py
git commit -m "feat: ThreadRuntime on_turn_terminal hook for facade wait resolution"
```

---

### Task 2: `RuntimeThreadManager` 确定性 rollout 路径 + 恢复 + 透传钩子

**Files:**
- Modify: `src/athena/app_server/thread_manager.py`
- Test: `test/unit/app_server/test_thread_manager_rollout_resume.py` (Create)

**Interfaces:**
- Produces: `RuntimeThreadManager.__init__(..., rollout_dir: Path | None = None, on_turn_terminal: Callable | None = None)`;当 `rollout_dir` 设置时每线程 rollout 文件为 `{rollout_dir}/{thread_id}.jsonl`,文件非空则 `resume_context_sync` 恢复内存;`on_turn_terminal` 透传每个 `ThreadRuntime`。

- [ ] **Step 1: 写失败测试**

```python
# test/unit/app_server/test_thread_manager_rollout_resume.py
import asyncio
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from athena.app_server.thread_manager import RuntimeThreadManager
from athena.memory.context_manager import ContextManager
from athena.memory.rollout import resume_context_sync


class _Outcome:
    def __init__(self, ref):
        self.result_ref = ref
        self.next_context_ref = ref


class _MemoryRunner:
    """把触发消息写进 memory 并返回固定 result_ref。"""
    def __init__(self):
        self.refs = []

    async def run_with_context(self, thread, turn, emit, memory, cancel):
        if memory is not None:
            memory.append(ModelRequest(parts=[UserPromptPart(content="echo")]))
        ref = f"art:{len(self.refs)}"
        self.refs.append(ref)
        return _Outcome(ref)


async def test_deterministic_rollout_resumes_context(tmp_path: Path):
    # 必须传 ctx:否则 memory=None,runner 不写 memory,rollout 文件保持空
    manager = RuntimeThreadManager(
        _MemoryRunner(), ctx=ContextManager(), rollout_dir=tmp_path / "sessions"
    )
    thread = await manager.start("agent-1", "ctx1")
    await manager.submit(thread.thread_id, "req1")
    await asyncio.sleep(0.05)  # 让 turn 跑完

    path = tmp_path / "sessions" / "agent-1.jsonl"
    assert path.exists(), f"deterministic rollout missing: {path}"
    assert path.stat().st_size > 0
    ctx = resume_context_sync(path)
    assert any("echo" in repr(m) for m in ctx.items)

    await manager.aclose("done")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest test/unit/app_server/test_thread_manager_rollout_resume.py -v`
Expected: FAIL — `TypeError: RuntimeThreadManager.__init__() got an unexpected keyword argument 'rollout_dir'`

- [ ] **Step 3: 实现**

在 `src/athena/app_server/thread_manager.py`:
1. `__init__` 追加参数并保存:
```python
        rollout_dir: Path | None = None,
        on_turn_terminal: Any | None = None,
```
```python
        self._rollout_dir = Path(rollout_dir) if rollout_dir is not None else None
        self._on_turn_terminal = on_turn_terminal
```
2. `_make_runtime` 中 rollout 分支改为(注意:**`rollout_dir` 本身即隐式开启 rollout**,无需额外 `rollout=` 哨兵;否则 Task 3 门面/本测试不传 `rollout` 时持久化会被静默跳过):
```python
        if self._memory_kwargs["rollout"] is not None or self._rollout_dir is not None:
            recorder = RolloutRecorder(self._project_root)
            if self._rollout_dir is not None:
                # 确定性路径:同一 agent_id(thread_id)重启复用同一 JSONL
                path = self._rollout_dir / f"{thread_id}.jsonl"
                recorder.open_sync(thread_id, append_to=path)
                if path.exists() and path.stat().st_size > 0:
                    # COMPAT: 重开会话恢复记忆;清理条件:Codex 风格会话恢复并入 ThreadRuntime 后。
                    kwargs["ctx"] = resume_context_sync(path)
            kwargs["rollout"] = recorder
```
3. `ThreadRuntime(...)` 构造追加 `on_turn_terminal=self._on_turn_terminal`。
4. 确保导入 `resume_context_sync`(来自 `athena.memory.rollout`)。

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m pytest test/unit/app_server/ -v` → 全过。

- [ ] **Step 5: 提交**

```bash
git add src/athena/app_server/thread_manager.py test/unit/app_server/test_thread_manager_rollout_resume.py
git commit -m "feat: ThreadManager deterministic per-agent rollout path + auto-resume + terminal hook passthrough"
```

---

## Phase 2 — AgentRuntime 门面(临时引用 agent_kernel 符号)

### Task 3: `RunSession` 视图 + `AgentRuntime` 核心

**Files:**
- Create: `src/athena/core/agent/session.py`
- Create: `src/athena/core/agent/agent_runtime.py`
- Modify: `src/athena/core/agent/__init__.py` (导出 `AgentRuntime`)
- Test: `test/unit/agent/__init__.py`, `test/unit/agent/_support.py`, `test/unit/agent/test_session.py`, `test/unit/agent/test_agent_runtime.py` (全部 Create)

**Interfaces:**
- Produces:
  - `RunSession(*, agent_id, kernel, context_ref, memory: ContextManager, mailbox: list[AgentMessage])`: `agent_id/kernel/context_ref/memory(.raw)/receive_messages()/checkpoint()`
  - `AgentRuntime(*, type_registry: AgentTypeRegistry, project_root: Path | None = None, rollout_dir: Path | None = None)`;`start()/aclose()/pause()/resume()/has_agent(agent_id)`;`async create_root(agent_type, task, *, name="root") -> (agent_id, run_id)`;`async spawn(parent_id, agent_type, task, *, name=None) -> (agent_id, run_id)`;`async followup(agent_id, task) -> run_id`;`async send_message(agent_id, content, context_refs=None, *, source=None)`;`async wait_run(run_id, *, timeout=None) -> RunSummary`;`async interrupt(agent_id, reason)`;`async cancel_run(run_id, *, reason=...)`;`async close(agent_id, *, recursive=False)`;`async resume_agent(agent_id, *, agent_type, name=None)`;`list_agents(path_prefix=None)`;`agent_status/agent_snapshot/agent_path/registry_spec/run_summary/run_events/session_events`;内部 `_on_turn_terminal(turn_id, TurnTerminalState)`。
- 说明:本任务 **`agent_runtime.py`、`session.py`、全部测试**均从 `athena.core.agent_kernel.{types,registry,codec}` 导入符号(**临时**,agent_kernel 仍在;Task 6 `git mv` 后统一改指向 `core.agent`)。不要从尚不存在的 `athena.core.agent.{types,registry,codec}` 导入。
- Consumes: `RuntimeThreadManager(runner, project_root, rollout_dir, ctx=ContextManager(), on_turn_terminal)`。

- [ ] **Step 1: 建测试基建 `_support.py` 与 `__init__.py`**

```bash
mkdir -p test/unit/agent
touch test/unit/agent/__init__.py
```
```python
# test/unit/agent/_support.py
import asyncio
import json
from pathlib import Path

from athena.agents.base_runner import BaseAgentRunner
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.runtime import BaseAgent
# COMPAT: Task 6 迁移后改 from athena.core.agent.{codec,registry,types} import ...
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.registry import AgentTypeRegistry
from athena.core.agent_kernel.types import AgentSpec
from athena.core.artifact_store import LocalArtifactStore


class StubAgent(BaseAgent):
    """把 input_text 写为 Artifact 的确定性 agent。"""
    def __init__(self, store):
        self._store = store

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        ref = await self._store.put_text(json.dumps({"result": ctx.input_text}))
        return AgentOutcome(result_ref=ref)


class BlockingAgent(BaseAgent):
    """置位 gate 后阻塞,等待被中断。"""
    def __init__(self, gate: asyncio.Event):
        self._gate = gate

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        self._gate.set()
        await asyncio.sleep(30)
        return AgentOutcome(result_ref="art:never")


def make_runtime(tmp_path: Path) -> AgentRuntime:
    """构造只注册 ``stub`` 类型的门面。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "stub",
        lambda aid, cfg=None: AgentSpec(
            runner=BaseAgentRunner(StubAgent(store)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / "sessions",
    )
    rt.start()
    return rt
```
(注意:此处 `agent_runtime.py` 尚不存在,Step 3 实现后 import 才可用。)

- [ ] **Step 2: 写失败测试**

```python
# test/unit/agent/test_session.py
import pytest

from athena.core.agent.session import RunSession
from athena.core.agent_kernel.types import AgentMessage
from athena.memory.context_manager import ContextManager


def test_session_view_exposes_kernel_contract():
    memory = ContextManager()
    mailbox = [AgentMessage(source="user", content="hi", context_refs=[])]
    kernel = object()
    sess = RunSession(
        agent_id="a1", kernel=kernel, context_ref="art:ctx",
        memory=memory, mailbox=mailbox,
    )
    assert sess.agent_id == "a1"
    assert sess.kernel is kernel
    assert sess.context_ref == "art:ctx"
    assert sess.memory.raw is memory  # BaseAgentRunner 依赖 .raw
    unread = sess.receive_messages()
    assert [m.content for m in unread] == ["hi"]
    assert sess.receive_messages() == []  # 读即清(=checkpoint)
    sess.checkpoint()  # 空操作,不抛
    with pytest.raises(AttributeError):
        sess.memory.append(None)  # 只读视图禁止写
```
```python
# test/unit/agent/test_agent_runtime.py
import asyncio
import json

from ._support import BlockingAgent, make_runtime

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.models import AgentOutcome
# COMPAT: Task 6 迁移后改 from athena.core.agent.{codec,registry,types} import ...
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.registry import AgentTypeRegistry
from athena.core.agent_kernel.types import AgentSpec, AgentStatus, RunStatus
from athena.agents.base_runner import BaseAgentRunner
from athena.core.artifact_store import LocalArtifactStore


async def test_create_root_and_followup_run_turns(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "hello"})
    assert agent_id and run_id
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    payload = json.loads(summary.response_ref)
    store = LocalArtifactStore(tmp_path / "artifacts")
    result = json.loads(await store.get_text(payload["result_ref"]))
    assert result["result"] == "hello"

    run2 = await rt.followup(agent_id, {"content": "world"})
    summary2 = await rt.wait_run(run2, timeout=5)
    payload2 = json.loads(summary2.response_ref)
    result2 = json.loads(await store.get_text(payload2["result_ref"]))
    assert result2["result"] == "world"
    await rt.aclose()


async def test_send_message_delivers_to_next_turn(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "first"})
    await rt.wait_run(run_id, timeout=5)
    await rt.send_message(agent_id, "queued-note")
    run2 = await rt.followup(agent_id, {"content": "second"})
    summary2 = await rt.wait_run(run2, timeout=5)
    payload2 = json.loads(summary2.response_ref)
    store = LocalArtifactStore(tmp_path / "artifacts")
    result2 = json.loads(await store.get_text(payload2["result_ref"]))
    assert result2["result"] == "second"  # trigger 仍是本轮 content
    await rt.aclose()


async def test_interrupt_marks_run_interrupted(tmp_path):
    gate = asyncio.Event()
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    registry.register(
        "block",
        lambda aid, cfg=None: AgentSpec(
            runner=BaseAgentRunner(BlockingAgent(gate)), codec=JsonCodec()
        ),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    agent_id, run_id = await rt.create_root("block", {"content": "x"})
    await asyncio.wait_for(gate.wait(), timeout=2)  # 等 turn 进入运行
    assert rt.agent_status(agent_id) == AgentStatus.RUNNING
    await rt.interrupt(agent_id, "test stop")
    summary = await rt.wait_run(run_id, timeout=5)
    assert summary.status == RunStatus.INTERRUPTED
    await rt.aclose()


async def test_spawn_records_parent(tmp_path):
    rt = make_runtime(tmp_path)
    parent, run1 = await rt.create_root("stub", {"content": "p"})
    await rt.wait_run(run1, timeout=5)
    child, run2 = await rt.spawn(parent, "stub", {"content": "c"})
    await rt.wait_run(run2, timeout=5)
    snaps = {s.agent_id: s for s in rt.list_agents()}
    assert snaps[child].parent_id == parent
    assert snaps[child].path == ("stub", "stub")  # 名称缺省 = agent_type
    await rt.aclose()
```

- [ ] **Step 3: 运行确认失败**

Run: `python -m pytest test/unit/agent/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'athena.core.agent.session'` / `'athena.core.agent.agent_runtime'`

- [ ] **Step 4: 实现 `core/agent/session.py`**

```python
"""RunSession — 门面构造的每 turn 受限视图(COMPAT: 保留 kernel RunSession 消费接口)。

清理条件: AgentRunner 协议统一为线程 runner、BaseAgentRunner 不再消费 session 后。
mailbox 读即清,checkpoint 为空操作。
"""

from typing import Any

from pydantic_ai.messages import ModelMessage

# COMPAT: Task 6 迁移后改 from athena.core.agent.types import AgentId, AgentMessage
from athena.core.agent_kernel.types import AgentId, AgentMessage
from athena.memory.context_manager import ContextManager


class _MemoryView:
    """ContextManager 只读视图;``raw`` 暴露底层 ContextManager(BaseAgent 适配器用)。"""

    def __init__(self, memory: ContextManager, *, allow_rollback: bool = True) -> None:
        self._memory = memory
        self._allow_rollback = allow_rollback

    @property
    def raw(self) -> ContextManager:
        return self._memory

    @property
    def items(self):
        return self._memory.items

    @property
    def tokens(self):
        return self._memory.tokens

    @property
    def version(self):
        return self._memory.version

    @property
    def limit(self):
        return self._memory.limit

    def token_margin(self, ratio: float = 0.85):
        return self._memory.token_margin(ratio)

    def snapshot(self):
        return self._memory.snapshot()

    def items_since(self, idx: int):
        return self._memory.items_since(idx)

    def rollback(self, idx: int) -> None:
        if not self._allow_rollback:
            raise AttributeError("rollback is runtime-internal")
        self._memory.rollback(idx)

    def append(self, msg: ModelMessage) -> None:
        raise AttributeError("memory writes go through ThreadRuntime")

    def replace_range(self, *args, **kwargs) -> None:
        raise AttributeError("memory writes go through ThreadRuntime")


class RunSession:
    """每 turn 的受限 session 视图;未读 mailbox 读即清(=checkpoint)。"""

    def __init__(
        self,
        *,
        agent_id: AgentId,
        kernel: Any,
        context_ref: str,
        memory: ContextManager,
        mailbox: list[AgentMessage],
    ) -> None:
        self._agent_id = agent_id
        self._kernel = kernel
        self._context_ref = context_ref
        self._memory_view = _MemoryView(memory, allow_rollback=False)
        self._mailbox = mailbox

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @property
    def kernel(self) -> Any:
        return self._kernel

    @property
    def context_ref(self) -> str:
        return self._context_ref

    @property
    def memory(self) -> _MemoryView:
        return self._memory_view

    def receive_messages(self) -> list[AgentMessage]:
        unread = list(self._mailbox)
        self._mailbox.clear()
        return unread

    def checkpoint(self) -> None:
        pass  # COMPAT: mailbox 读即清,游标推进为空操作
```

- [ ] **Step 5: 实现 `core/agent/agent_runtime.py`(完整文件)**

```python
"""AgentRuntime — Thread 模型的 Agent 门面(方案 A:门面协调层)。

把 kernel 的 Agent 树语义重新表达在 app_server 的 Thread 模型上:每个逻辑 Agent =
一条 ThreadRuntime(agent_id == thread_id)。公共契约(AgentControl/AgentHandle/AgentRun/
types)迁自 athena.core.agent_kernel,调用方(ProjectRuntime/编排工具)接口不变。
mailbox 与 WaitRegistry 为门面持有(内存);对话经 rollout JSONL 确定性持久化。
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from athena.app_server.exceptions import ClosedError
from athena.app_server.submissions import TurnTerminalState
from athena.app_server.thread_manager import RuntimeThreadManager
from athena.core.agent.models import AgentOutcome
from athena.core.agent.session import RunSession
# COMPAT: 以下符号暂从 agent_kernel 导入;Task 6 git mv 后改指向 athena.core.agent.*。
# 清理条件: agent_kernel 包删除后。
from athena.core.agent_kernel.registry import AgentTypeRegistry
from athena.core.agent_kernel.types import (
    TERMINAL_RUN_STATUSES,
    AgentBusyError,
    AgentCommandError,
    AgentEvent,
    AgentId,
    AgentMessage,
    AgentPath,
    AgentSnapshot,
    AgentStatus,
    AgentWaitResult,
    ArtifactRef,
    ErrorCode,
    ReturnWhen,
    RunId,
    RunStatus,
    RunSummary,
)
from athena.core.thread_models import AthenaThread
from athena.memory.context_manager import ContextManager

logger = logging.getLogger(__name__)


@dataclass
class _FacadeRecord:
    agent_id: AgentId
    agent_type: str
    spec: Any  # AgentSpec
    parent_id: AgentId | None
    name: str
    path: AgentPath
    mailbox: deque = field(default_factory=deque)


class _ThreadRunner:
    """把 kernel AgentRunner 协议(经 RunSession 视图)接到 thread run_with_context。

    COMPAT: BaseAgentRunner 主体不动,只在此适配;空唤醒({} 请求)不生成假 trigger
    由 BaseAgentRunner 现有逻辑处理。清理条件: AgentRunner 协议统一为线程 runner 后。
    """

    def __init__(self, runtime: "AgentRuntime") -> None:
        self._runtime = runtime

    async def run(self, thread: AthenaThread, turn, emit) -> AgentOutcome:
        return await self.run_with_context(thread, turn, emit, None, asyncio.Event())

    async def run_with_context(self, thread, turn, emit, memory, cancel) -> AgentOutcome:
        if cancel.is_set():
            raise asyncio.CancelledError
        record = self._runtime._records.get(thread.thread_id)
        if record is None:
            raise RuntimeError(f"unknown agent thread: {thread.thread_id}")
        request = record.spec.codec.decode_request(turn.request_ref)
        session = RunSession(
            agent_id=record.agent_id,
            kernel=self._runtime,
            context_ref=thread.context_ref,
            memory=memory,
            mailbox=record.mailbox,
        )
        raw = await record.spec.runner.run(request, session=session, emit=emit)
        response_ref = record.spec.codec.encode_response(raw)
        return AgentOutcome(result_ref=response_ref, next_context_ref=thread.context_ref)


def _terminal_to_run_status(terminal: TurnTerminalState) -> RunStatus:
    if terminal.cancelled:
        return RunStatus.INTERRUPTED
    if terminal.exception_type is not None:
        return RunStatus.FAILED
    return RunStatus.COMPLETED


class AgentRuntime:
    """Agent 树语义的 Thread 门面;方法签名对齐退役前的 AgentKernel。"""

    def __init__(
        self,
        *,
        type_registry: AgentTypeRegistry,
        project_root: Path | None = None,
        rollout_dir: Path | None = None,
    ) -> None:
        self._registry = type_registry
        root = Path(project_root) if project_root is not None else Path.cwd()
        self._manager = RuntimeThreadManager(
            _ThreadRunner(self),
            project_root=root,
            rollout_dir=rollout_dir,
            ctx=ContextManager(),  # 每线程默认空记忆;rollout_dir 存在时由 _make_runtime 恢复覆盖
            on_turn_terminal=self._on_turn_terminal,
        )
        self._records: dict[AgentId, _FacadeRecord] = {}
        self._run_agent: dict[RunId, AgentId] = {}
        self._run_summaries: dict[RunId, RunSummary] = {}
        self._active_turn: dict[AgentId, RunId] = {}
        self._last_terminal: dict[AgentId, RunStatus] = {}
        self._human_waits: dict[str, AgentId] = {}
        self._agent_waits: dict[AgentId, list[AgentId]] = {}
        self._paused = False
        self._closed = False

    # ---- 生命周期 ----

    def start(self) -> None:
        """兼容入口;Thread 模型无需预热。"""
        if self._closed:
            raise AgentCommandError(ErrorCode.CLOSED, "runtime is closed")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._manager.aclose("runtime close")

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def has_agent(self, agent_id: AgentId) -> bool:
        return agent_id in self._records

    def _check_open(self) -> None:
        if self._closed:
            raise AgentCommandError(ErrorCode.CLOSED, "runtime is closed")
        if self._paused:
            # COMPAT: kernel 的 pause 曾排队派发;无全局队列下退化为直接报错。
            raise AgentCommandError(ErrorCode.CLOSED, "runtime paused")

    # ---- 创建 / 消息 / 续跑 ----

    async def create_root(self, agent_type: str, task: object, *, name: str = "root"):
        return await self._spawn_agent(None, agent_type, task, name=name)

    async def spawn(self, parent_id: AgentId, agent_type: str, task: object, *, name: str | None = None):
        if parent_id not in self._records:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown parent: {parent_id}")
        return await self._spawn_agent(parent_id, agent_type, task, name=name)

    async def _spawn_agent(self, parent_id, agent_type, task, *, name):
        self._check_open()
        if not self._registry.contains(agent_type):
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent_type: {agent_type}")
        agent_id = uuid4().hex
        spec = self._registry.require_spec(agent_type, agent_id=agent_id)
        req_ref = spec.codec.encode_request(task)
        await self._manager.start(agent_id, req_ref)
        parent = self._records.get(parent_id)
        path = parent.path + (name or agent_type,) if parent else (name or agent_type,)
        self._records[agent_id] = _FacadeRecord(
            agent_id=agent_id, agent_type=agent_type, spec=spec,
            parent_id=parent_id, name=name or agent_type, path=path,
        )
        run_id = await self._start_run(agent_id, req_ref)
        return agent_id, run_id

    async def resume_agent(self, agent_id: AgentId, *, agent_type: str, name: str | None = None) -> None:
        """重开会话:经确定性 rollout 自动恢复记忆,不触发 turn。

        COMPAT: context_ref 用 agent_id 占位(不持久化旧 context_ref)。清理条件:
        会话恢复把 context_ref 一并持久化后。
        """
        if agent_id in self._records:
            return
        spec = self._registry.require_spec(agent_type, agent_id=agent_id)
        await self._manager.start(agent_id, agent_id)
        self._records[agent_id] = _FacadeRecord(
            agent_id=agent_id, agent_type=agent_type, spec=spec,
            parent_id=None, name=name or agent_type, path=(name or agent_type,),
        )

    async def followup(self, agent_id: AgentId, task: object) -> RunId:
        self._check_open()
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        req_ref = record.spec.codec.encode_request(task)
        return await self._start_run(agent_id, req_ref)

    async def _start_run(self, agent_id: AgentId, req_ref: ArtifactRef) -> RunId:
        self._check_open()
        try:
            turn = await self._manager.submit(agent_id, req_ref)
        except ClosedError as exc:
            raise AgentCommandError(ErrorCode.CLOSED, str(exc)) from exc
        except RuntimeError as exc:
            if "active turn" in str(exc):
                raise AgentBusyError() from exc
            raise
        run_id = turn.turn_id
        self._run_agent[run_id] = agent_id
        self._active_turn[agent_id] = run_id
        return run_id

    async def send_message(
        self, agent_id: AgentId, content: str, context_refs: list[ArtifactRef] | None = None, *, source: AgentId | None = None
    ) -> None:
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        record.mailbox.append(AgentMessage(source=source, content=content, context_refs=context_refs or []))

    # ---- 等待 ----

    async def wait_run(self, run_id: RunId, *, timeout: float | None = None) -> RunSummary:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        try:
            result_ref = await asyncio.wait_for(self._manager.wait_turn(agent_id, run_id), timeout)
        except asyncio.CancelledError:
            return RunSummary(run_id=run_id, agent_id=agent_id, status=RunStatus.INTERRUPTED, reason="run interrupted")
        except RuntimeError as exc:
            return RunSummary(run_id=run_id, agent_id=agent_id, status=RunStatus.FAILED, error=str(exc))
        summary = RunSummary(run_id=run_id, agent_id=agent_id, status=RunStatus.COMPLETED, response_ref=result_ref)
        self._run_summaries[run_id] = summary
        return summary

    async def wait_agent(
        self, targets: Collection[AgentId], *, return_when: ReturnWhen = ReturnWhen.FIRST_COMPLETED, timeout: float | None = None
    ) -> AgentWaitResult:
        """等待一组 Agent 当前 run 终结(每线程串行下即等待各自活跃 turn)。

        COMPAT: 原 kernel 的 park/lease 语义不再存在,退化为普通并发等待。清理条件:
        等待语义内建到 thread 后。
        """
        target_ids = list(targets)
        async def _wait_one(agent_id: AgentId):
            run_id = self._active_turn.get(agent_id)
            if run_id is None:
                return self._last_terminal_summary(agent_id)
            return await self.wait_run(run_id)

        tasks = {t: asyncio.ensure_future(_wait_one(t)) for t in target_ids}
        if return_when == ReturnWhen.ALL_COMPLETED:
            done, pending = await asyncio.wait(list(tasks.values()), timeout=timeout)
        else:
            done, pending = await asyncio.wait(list(tasks.values()), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        completed: dict[AgentId, RunSummary] = {}
        for agent_id, task in tasks.items():
            if task in done and not task.cancelled():
                completed[agent_id] = task.result()
        return AgentWaitResult(completed=completed, timed_out=bool(pending))

    def _last_terminal_summary(self, agent_id: AgentId) -> RunSummary:
        for run_id, summary in self._run_summaries.items():
            if summary.agent_id == agent_id:
                return summary
        raise AgentCommandError(ErrorCode.NOT_FOUND, f"no run for agent: {agent_id}")

    def run_summary(self, run_id: RunId) -> RunSummary | None:
        cached = self._run_summaries.get(run_id)
        if cached is not None:
            return cached
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            return None
        status = RunStatus.RUNNING if self._active_turn.get(agent_id) == run_id else RunStatus.QUEUED
        return RunSummary(run_id=run_id, agent_id=agent_id, status=status)

    # ---- 中断 / 关闭 ----

    async def interrupt(self, agent_id: AgentId, reason: str) -> None:
        run_id = self._active_turn.get(agent_id)
        if run_id is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"no active run for {agent_id}")
        await self._manager.interrupt(agent_id, run_id, reason)

    async def cancel_run(self, run_id: RunId, *, reason: str = "caller_cancelled") -> None:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        await self._manager.interrupt(agent_id, run_id, reason)

    def _descendants(self, agent_id: AgentId) -> list[AgentId]:
        out: list[AgentId] = []
        for aid, rec in self._records.items():
            if rec.parent_id == agent_id:
                out.append(aid)
                out.extend(self._descendants(aid))
        return out

    async def close(self, agent_id: AgentId, *, recursive: bool = False) -> None:
        if agent_id not in self._records:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        if not recursive:
            live = [c for c in self._descendants(agent_id) if self.agent_status(c) != AgentStatus.CLOSED]
            if live:
                raise AgentCommandError(ErrorCode.BUSY, "has live descendants")
        order = [agent_id] + (self._descendants(agent_id) if recursive else [])
        for aid in order:
            handle = await self._manager.get(aid)
            await handle.shutdown_and_wait("agent close")

    # ---- 查询 / 投影 ----

    def list_agents(self, path_prefix: AgentPath | None = None) -> list[AgentSnapshot]:
        snaps = []
        for agent_id, rec in self._records.items():
            if path_prefix is not None and rec.path[: len(path_prefix)] != path_prefix:
                continue
            snaps.append(self._snapshot(rec))
        return sorted(snaps, key=lambda s: s.path)

    def _snapshot(self, rec: _FacadeRecord) -> AgentSnapshot:
        return AgentSnapshot(
            agent_id=rec.agent_id, path=rec.path, name=rec.name,
            agent_type=rec.agent_type, status=self.agent_status(rec.agent_id),
            parent_id=rec.parent_id, pending_run_id=self._active_turn.get(rec.agent_id),
        )

    def agent_status(self, agent_id: AgentId) -> AgentStatus | None:
        if any(a == agent_id for a in self._human_waits.values()):
            return AgentStatus.WAITING_FOR_HUMAN
        if agent_id in self._agent_waits:
            return AgentStatus.WAITING
        try:
            thread = self._manager.get_thread(agent_id)
        except KeyError:
            return AgentStatus.CLOSED
        if thread.status == "closed":
            return AgentStatus.CLOSED
        if thread.status == "running":
            return AgentStatus.RUNNING
        if self._last_terminal.get(agent_id) == RunStatus.FAILED:
            return AgentStatus.ERROR
        return AgentStatus.IDLE

    def agent_snapshot(self, agent_id: AgentId) -> AgentSnapshot | None:
        rec = self._records.get(agent_id)
        return self._snapshot(rec) if rec is not None else None

    def agent_path(self, agent_id: AgentId) -> AgentPath | None:
        rec = self._records.get(agent_id)
        return rec.path if rec is not None else None

    def registry_spec(self, agent_id: AgentId):
        rec = self._records.get(agent_id)
        if rec is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return rec.spec

    # ---- 事件 ----

    def run_events(self, run_id: RunId, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        return self._session_events(agent_id, run_id, after_sequence)

    def session_events(self, agent_id: AgentId, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        return self._session_events(agent_id, None, after_sequence)

    def _session_events(self, agent_id: AgentId, run_id: RunId | None, after_sequence: int):
        """COMPAT: EventJournal(Event) → AgentEvent(run_id/sequence/kind/event_ref/data)。"""
        async def _gen():
            handle = await self._manager.get(agent_id)
            async for ev in handle.events(after_sequence=after_sequence):
                if run_id is not None and ev.turn_id != run_id:
                    continue
                yield AgentEvent(run_id=ev.turn_id, sequence=ev.sequence, kind=ev.kind, event_ref=ev.event_ref, data=ev.data)
        return _gen()

    # ---- 终态回调(供 ThreadManager 透传) ----

    def _on_turn_terminal(self, turn_id: str, terminal: TurnTerminalState) -> None:
        """同步、无 await;更新状态并调度 wait 解析。"""
        agent_id = self._run_agent.get(turn_id)
        if agent_id is None:
            return
        self._active_turn.pop(agent_id, None)
        status = _terminal_to_run_status(terminal)
        self._last_terminal[agent_id] = status
        self._run_summaries[turn_id] = RunSummary(
            run_id=turn_id, agent_id=agent_id, status=status,
            response_ref=terminal.result_ref,
            error=terminal.exception_type,
            reason="run interrupted" if terminal.cancelled else None,
        )
        if any(agent_id in targets for targets in self._agent_waits.values()):
            asyncio.create_task(self._resolve_agent_waits())
```

- [ ] **Step 6: `core/agent/__init__.py` 追加导出**

在现有 `__all__` 中加:
```python
from athena.core.agent.agent_runtime import AgentRuntime
```
并把 `"AgentRuntime"` 加入 `__all__`。

- [ ] **Step 7: 运行确认通过**

Run: `python -m pytest test/unit/agent/test_session.py test/unit/agent/test_agent_runtime.py -v`
Expected: 全部 PASS(4 个 runtime 用例 + 1 个 session 用例)。

- [ ] **Step 8: 提交**

```bash
git add src/athena/core/agent/session.py src/athena/core/agent/agent_runtime.py src/athena/core/agent/__init__.py test/unit/agent/
git commit -m "feat: AgentRuntime thread facade + RunSession view (create_root/followup/send_message/wait_run/interrupt/status)"
```

---

### Task 4: AgentRuntime 等待语义(wait_for / wait_for_human / human_reply)

**Files:**
- Modify: `src/athena/core/agent/agent_runtime.py`
- Test: `test/unit/agent/test_agent_runtime_waits.py` (Create)

**Interfaces:**
- Produces: `async wait_for(agent_id, target_ids)`, `async wait_for_human(agent_id, content, context_refs=None) -> request_id`, `async human_reply(request_id, reply) -> run_id`。

- [ ] **Step 1: 写失败测试**

```python
# test/unit/agent/test_agent_runtime_waits.py
import asyncio

import pytest

from ._support import make_runtime

# COMPAT: Task 6 迁移后改 from athena.core.agent.types import ...
from athena.core.agent_kernel.types import AgentCommandError, AgentStatus, ErrorCode, RunStatus


async def test_wait_for_resolves_when_target_terminal(tmp_path):
    rt = make_runtime(tmp_path)
    waiter, run_w = await rt.create_root("stub", {"content": "w"})
    target, run_t = await rt.create_root("stub", {"content": "t"})
    await rt.wait_for(waiter, [target])
    assert rt.agent_status(waiter) == AgentStatus.WAITING
    await rt.wait_run(run_t, timeout=5)  # target 完成 → 终态回调 → 解析 → 空唤醒 waiter
    for _ in range(100):
        if rt.agent_status(waiter) != AgentStatus.WAITING:
            break
        await asyncio.sleep(0.01)
    assert rt.agent_status(waiter) != AgentStatus.WAITING
    await rt.aclose()


async def test_wait_for_human_then_reply_wakes(tmp_path):
    rt = make_runtime(tmp_path)
    agent_id, run_id = await rt.create_root("stub", {"content": "q"})
    request_id = await rt.wait_for_human(agent_id, "need input")
    assert rt.agent_status(agent_id) == AgentStatus.WAITING_FOR_HUMAN
    new_run = await rt.human_reply(request_id, "answer")
    summary = await rt.wait_run(new_run, timeout=5)
    assert summary.status == RunStatus.COMPLETED
    assert rt.agent_status(agent_id) != AgentStatus.WAITING_FOR_HUMAN
    await rt.aclose()


async def test_human_reply_unknown_raises(tmp_path):
    rt = make_runtime(tmp_path)
    with pytest.raises(AgentCommandError) as ei:
        await rt.human_reply("nope", "x")
    assert ei.value.code == ErrorCode.NOT_FOUND
    await rt.aclose()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest test/unit/agent/test_agent_runtime_waits.py -v`
Expected: FAIL — `AttributeError: 'AgentRuntime' object has no attribute 'wait_for'`

- [ ] **Step 3: 修复 Task 3 审查发现的两个 Important 缺陷(计划代码 bug,Task 4 一并处理)**

在 `agent_runtime.py`:

1. `_last_terminal_summary` 现在返回**最旧**而非最新摘要(按插入序迭代返回首个匹配)。改为反向迭代取最新:
```python
    def _last_terminal_summary(self, agent_id: AgentId) -> RunSummary:
        for run_id in reversed(list(self._run_summaries.keys())):
            summary = self._run_summaries[run_id]
            if summary.agent_id == agent_id:
                return summary
        raise AgentCommandError(ErrorCode.NOT_FOUND, f"no run for agent: {agent_id}")
```

2. `wait_run` 现在把**调用方取消**(如 wait_agent 取消 pending 任务、外层 task.cancel)误当 turn 终态中断吞掉 → shutdown 悬挂。改为:捕获 `CancelledError` 后让终态回调落地(`await asyncio.sleep(0)`),查 run 摘要确为 INTERRUPTED 才返回,否则重抛:
```python
    async def wait_run(self, run_id: RunId, *, timeout: float | None = None) -> RunSummary:
        agent_id = self._run_agent.get(run_id)
        if agent_id is None:
            raise KeyError(f"unknown run: {run_id}")
        try:
            result_ref = await asyncio.wait_for(self._manager.wait_turn(agent_id, run_id), timeout)
        except asyncio.CancelledError:
            # 区分 turn 终态中断与调用方取消:让终态回调先落地再判断,真实取消则传播。
            await asyncio.sleep(0)
            cached = self._run_summaries.get(run_id)
            if cached is not None and cached.status == RunStatus.INTERRUPTED:
                return cached
            raise
        except RuntimeError as exc:
            return RunSummary(run_id=run_id, agent_id=agent_id, status=RunStatus.FAILED, error=str(exc))
        summary = RunSummary(run_id=run_id, agent_id=agent_id, status=RunStatus.COMPLETED, response_ref=result_ref)
        self._run_summaries[run_id] = summary
        return summary
```
> 注:终态回调 `_on_turn_terminal` 在 `commit_interrupted` 中 `set_result` **之后**、同一事件循环内同步触发;`await asyncio.sleep(0)` 给 submission_loop 一次机会落地摘要。若摘要仍未出现,说明确实非终态中断,重抛 CancelledError(调用方取消)。

- [ ] **Step 4: 在 `agent_runtime.py` 追加等待方法(放在 `cancel_run` 之后)**

```python
    # ---- 等待注册 / 唤醒 ----

    async def wait_for(self, agent_id: AgentId, target_ids: list[AgentId]) -> None:
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        for t in target_ids:
            if t not in self._records:
                raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown target: {t}")
        if all(self._is_terminal(t) for t in target_ids):
            return  # 目标已终态 → 当前 turn 由 runner 干净结束
        self._agent_waits[agent_id] = list(target_ids)

    async def wait_for_human(self, agent_id: AgentId, content: str, context_refs: list[ArtifactRef] | None = None) -> str:
        record = self._records.get(agent_id)
        if record is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown agent: {agent_id}")
        del content, context_refs  # COMPAT: 内容待后续人工确认持久化
        request_id = uuid4().hex
        self._human_waits[request_id] = agent_id
        return request_id

    async def human_reply(self, request_id: str, reply: str) -> RunId:
        agent_id = self._human_waits.pop(request_id, None)
        if agent_id is None:
            raise AgentCommandError(ErrorCode.NOT_FOUND, f"unknown wait request: {request_id}")
        record = self._records[agent_id]
        record.mailbox.append(AgentMessage(source="user", content=reply, context_refs=[]))
        req_ref = record.spec.codec.encode_request({})  # 空唤醒:无假 trigger
        return await self._start_run(agent_id, req_ref)

    def _is_terminal(self, agent_id: AgentId) -> bool:
        if self.agent_status(agent_id) == AgentStatus.CLOSED:
            return True
        return self._last_terminal.get(agent_id) in TERMINAL_RUN_STATUSES

    async def _resolve_agent_waits(self) -> None:
        for agent_id, target_ids in list(self._agent_waits.items()):
            if all(self._is_terminal(t) for t in target_ids):
                del self._agent_waits[agent_id]
                try:
                    await self._wake(agent_id)
                except Exception as exc:  # noqa: BLE001 — 唤醒失败仅记日志,不阻断解析
                    logger.warning("wake %s failed: %s", agent_id, type(exc).__name__)

    async def _wake(self, agent_id: AgentId) -> None:
        record = self._records.get(agent_id)
        if record is None:
            return
        req_ref = record.spec.codec.encode_request({})  # COMPAT: 空唤醒,无假 trigger
        await self._start_run(agent_id, req_ref)
```

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest test/unit/agent/ -v`
Expected: 全过。若 `test_wait_for_resolves_when_target_terminal` 偶发不稳,把轮询 sleep 提到 0.05。

- [ ] **Step 6: 提交**

```bash
git add src/athena/core/agent/agent_runtime.py test/unit/agent/test_agent_runtime_waits.py
git commit -m "feat: AgentRuntime wait_for/wait_for_human/human_reply via in-memory WaitRegistry; fix last-terminal-summary + wait_run cancellation"
```

---

## Phase 3 — 接线

### Task 5: `ProjectRuntime` 切换门面 + 持久化改造

**Files:**
- Modify: `src/athena/research/project_runtime.py`
- Test: `test/unit/test_project_runtime.py` (更新)

**Interfaces:**
- Consumes: `AgentRuntime(type_registry=..., project_root=..., rollout_dir=...)`,方法与旧 `self._kernel` 同名。
- Produces: `_save_state()` 去掉 `store` 字段;`_load_state()` 去 store 恢复与 `_recover/_rebuild_sessions`;`open()` 改 resume 路径。

- [ ] **Step 1: 改构造与 import**

```python
from athena.core.agent.agent_runtime import AgentRuntime
# COMPAT: Task 6 迁移后改 from athena.core.agent.{codec,registry} import ...
from athena.core.agent_kernel.codec import JsonCodec
from athena.core.agent_kernel.registry import AgentTypeRegistry
```
删除旧 `agent_kernel` 深层 import(`AgentKernel`/`AgentGraphStore`/`load_store_json`/`store_to_json`/`RolloutResourcesFactory`/`SessionResourcesFactory`)。

`__init__` 改为:
```python
        self._registry = AgentTypeRegistry()
        self._runtime = AgentRuntime(
            type_registry=self._registry,
            project_root=self._root,
            rollout_dir=self._root / ".athena" / "sessions",
        )
```
删除 `resources_factory` 参数、`self._resources_factory`、`self._kernel`。所有 `self._kernel.` 改 `self._runtime.`。

- [ ] **Step 2: `_save_state` 去 store 字段**

删除 `_save_state` 里 `"store": store_to_json(self._kernel._store),` 一行。

- [ ] **Step 3: `_load_state` 去 store 恢复**

删除 `_load_state` 中 `store = AgentGraphStore()` / `load_store_json(...)` / root 校验与修复块 / `self._kernel = AgentKernel(...)` / `_rebuild_sessions()` / `_recover()`;只保留事实字段反序列化。`_load_state` 不再重建 runtime。

- [ ] **Step 4: `open()` 改 resume 路径**

```python
    async def open(self, *, message: str | None = None) -> AgentId:
        """创建或恢复 root Supervisor;重开会话从 rollout 恢复记忆。"""
        self._load_state()
        if self._supervisor_id is None:
            self._supervisor_id, _ = await self._runtime.create_root(
                "supervisor", {"content": message or ""}, name="supervisor"
            )
            self._save_state()
        else:
            await self._runtime.resume_agent(
                self._supervisor_id, agent_type="supervisor", name="supervisor"
            )
            if message is not None:
                await self._runtime.followup(self._supervisor_id, {"content": message})
        return self._supervisor_id
```

- [ ] **Step 5: 更新测试**

`test/unit/test_project_runtime.py` 现有用例应直接跑通(方法同名)。补一条确定性 rollout 恢复:
```python
async def test_open_resume_restores_supervisor_rollout(tmp_path):
    from athena.research.project_runtime import ProjectRuntime

    pr1 = ProjectRuntime(tmp_path)
    pr1.register_defaults()  # 同步方法,勿 await
    sid = await pr1.open(message="hello")
    rollout = tmp_path / ".athena" / "sessions" / f"{sid}.jsonl"
    assert rollout.exists()
    await pr1.close()

    pr2 = ProjectRuntime(tmp_path)
    pr2.register_defaults()
    sid2 = await pr2.open(message="again")
    assert sid2 == sid  # 同一 supervisor id 恢复
    await pr2.close()
```
> 注:`register_defaults` 是同步方法,不要 await;`ProjectRuntime` 是否自动调用取决于现状,若未自动则测试里显式调用。

- [ ] **Step 6: 运行 + 提交**

Run: `python -m pytest test/unit/test_project_runtime.py -v` → PASS。
```bash
git add src/athena/research/project_runtime.py test/unit/test_project_runtime.py
git commit -m "refactor: ProjectRuntime on AgentRuntime facade, Codex-style light persistence"
```

---

## Phase 4 — 退役 kernel

### Task 6: 迁移抽象文件、删除 `agent_kernel`、迁移/删除测试

**Files:**
- Move: `src/athena/core/agent_kernel/{types,control,registry,codec}.py` → `src/athena/core/agent/`
- Delete: `src/athena/core/agent_kernel/{kernel,session,store,store_json,__init__}.py`
- Modify: 幸存文件 import 改向 core.agent(见 Step 2)
- Test: 迁移 `test/unit/agent_kernel/` 语义测试,删除 store/invariants 测试

**Interfaces:**
- 无(删除 + 纯改名)。

- [ ] **Step 1: `git mv` 四个抽象文件**

```bash
cd "C:/Users/80163/Desktop/挑战杯_2026/Athena"
git mv src/athena/core/agent_kernel/types.py src/athena/core/agent/types.py
git mv src/athena/core/agent_kernel/control.py src/athena/core/agent/control.py
git mv src/athena/core/agent_kernel/registry.py src/athena/core/agent/registry.py
git mv src/athena/core/agent_kernel/codec.py src/athena/core/agent/codec.py
```
> 现有 `src/athena/core/agent/{types,control,registry,codec}.py` 若已存在(首次迁移则不存在)先删除旧文件再 mv。注意:`agent_runtime.py`/`session.py` 在 Task 3 里已 import 这些符号——Step 2 一并改。

- [ ] **Step 2: 修幸存文件的 import(全仓 grep)**

```bash
grep -rn "agent_kernel" src/ test/ tests/ 2>/dev/null
```
逐一改为 `athena.core.agent.*`:
- `src/athena/core/agent/agent_runtime.py`: `athena.core.agent_kernel.{registry,types}` → `athena.core.agent.{registry,types}`,并删除该处 COMPAT 注释。
- `src/athena/core/agent/session.py`: `athena.core.agent_kernel.types` → `athena.core.agent.types`。
- `src/athena/core/agent/models.py`: TYPE_CHECKING 的 `athena.core.agent_kernel.types.AgentMessage` → `athena.core.agent.types.AgentMessage`。
- `src/athena/agents/base_runner.py`: `athena.core.agent_kernel.types.AgentMessage` → `athena.core.agent.types.AgentMessage`。
- `src/athena/agents/orchestration.py`: `athena.core.agent_kernel.{kernel,session,types}` → `athena.core.agent.{agent_runtime,session,types}`(`AgentKernel` → `AgentRuntime`)。
- `src/athena/core/agent/control.py`: `athena.core.agent_kernel.types` → `athena.core.agent.types`;TYPE_CHECKING 的 `AgentKernel` → `AgentRuntime`(import 路径改 `athena.core.agent.agent_runtime`)。
- `src/athena/core/agent/__init__.py`: 追加导出 `AgentControl`/`AgentHandle`/`AgentRun`(来自 `core.agent.control`)。
- 其余 grep 命中逐一处理。

- [ ] **Step 3: 删除 agent_kernel 剩余文件**

```bash
git rm src/athena/core/agent_kernel/kernel.py src/athena/core/agent_kernel/session.py src/athena/core/agent_kernel/store.py src/athena/core/agent_kernel/store_json.py src/athena/core/agent_kernel/__init__.py
rmdir src/athena/core/agent_kernel 2>/dev/null || true
```
> 不改这些文件内部(它们本来就 import 自己的兄弟模块,随包一起删除)。

- [ ] **Step 4: 迁移/删除 kernel 测试**

迁移(改 import `agent_kernel` → `agent`,构造改 `AgentRuntime`):
- `test_kernel.py` → `test/unit/agent/test_kernel_facade.py`(create_root/followup/interrupt/status/events 断言原样,构造用 `make_runtime`)
- `test_waits.py` → `test/unit/agent/test_waits.py`(改用 Task 4 的 WaitRegistry 语义)
- `test_review_loop.py` → `test/unit/agent/test_review_loop.py`
- `test_orchestration.py` → `test/unit/agent/test_orchestration.py`
- `test_base_agent_runner.py` → `test/unit/agent/test_base_agent_runner.py`(session 用 `RunSession` 视图)
- `test_simple_agents.py` / `test_data_agent.py` → `test/unit/agent/` 同名(仅 import)
- `test_memory_restore.py` → `test/unit/agent/test_memory_restore.py`(改验证确定性 rollout 恢复)
- `test_types.py` / `test_registry.py` → `test/unit/agent/` 同名(仅 import)

删除(Codex 风格下无对应物):
- `test_store.py`, `test_store_json.py`, `test_session.py`(被 Task 3 新视图测试取代), `test_invariants.py`(kernel 不变式多数由 ThreadRuntime 保证;若想保留,只留「同一 Agent 同时最多一个活跃 turn」「终态后 followup 可继续」两条并改写走 `AgentRuntime`)

```bash
git rm test/unit/agent_kernel/test_store.py test/unit/agent_kernel/test_store_json.py test/unit/agent_kernel/test_session.py test/unit/agent_kernel/test_invariants.py
```

- [ ] **Step 5: 全量测试**

Run: `python -m pytest test/unit/ tests/ -v`
Expected: 全过;若有失败按失败文件修 import/断言。

- [ ] **Step 6: 提交**

```bash
git add -A src/athena/core/agent src/athena/agents/base_runner.py src/athena/agents/orchestration.py test/unit/agent test/unit/agent_kernel
git commit -m "refactor: retire agent_kernel package, move abstractions into core.agent"
```

---

### Task 7: COMPAT 注释核验 + 文档收尾

**Files:**
- Modify: 需核验的兼容层(见下)
- Modify: `docs/architecture/current.md`, `docs/architecture/target.md`, `docs/architecture/2026-08-08-agent-runtime-framework-design.md`

- [ ] **Step 1: COMPAT 核验**

Run: `grep -rn "COMPAT:" src/athena/core/agent/ src/athena/app_server/ src/athena/agents/base_runner.py src/athena/core/agent/models.py`
Expected: 以下位置均带 `# COMPAT:` 且写明清理条件:
- `agent_runtime.py` `_ThreadRunner`(dispatcher)、`_check_open`(pause 降级)、`resume_agent`(context_ref 占位)、`wait_agent`(park 退化)、`_session_events`、`_wake`(空唤醒)
- `core/agent/session.py` `RunSession`/`_MemoryView`
- `thread_manager.py` rollout 恢复分支
- `core/agent/models.py` `AgentOutcome.next_context_ref`(迁移字段注释保留)
- `thread_runtime.py` `_run_turn` 双签名检测(既有注释补 `# COMPAT:` 前缀)

- [ ] **Step 2: 更新文档**

- `docs/architecture/current.md`:运行主链补「AgentRuntime(Thread 门面)」;规范 Owner 表把 `athena.core.agent_kernel` 条目改为 `athena.core.agent`。
- `docs/architecture/target.md`:把 Thread control 行更新为「app_server Thread 运行时 + core.agent AgentRuntime 门面」。
- `docs/architecture/2026-08-08-agent-runtime-framework-design.md`:标注 Superseded(指向 `2026-08-08-thread-agent-unification-design.md`)。

- [ ] **Step 3: 全量回归 + 提交**

Run: `python -m pytest test/unit/ tests/ -v` → 全过。
```bash
git add -A docs src/athena
git commit -m "docs: unify agent runtime onto app_server (retire agent_kernel), verify COMPAT annotations"
```
