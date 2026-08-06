# Athena Ideator Multi-Agent Debate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 尽迁尽删 `athena.brainstorm`，以可配置独立辩者 Thread、三轮互辩及唯一裁决者所成之 `athena.ideator` 取而代之。

**Architecture:** `RuntimeThreadManager` 仅补 Turn 终态等待；私有 `_DebateRunner` 将结构化 Agent 输出写入 `ArtifactStore`。公开 `Ideator` 以同一辩者 Thread 连行立论、互评、修案，再生唯一裁决 Thread，终返可审计 `DebateResult`，由 IdeaGeneration 登记入 `ResearchTree`。

**Tech Stack:** Python 3.11+、asyncio、Pydantic v2、PydanticAI 兼容结构化 Agent、既有 `RuntimeThreadManager`、`ResearchTree`、`ArtifactStore`、pytest/pytest-asyncio

## Global Constraints

- `athena.ideator` 对外仅导出 `Ideator`、`IdeatorConfig`、`DebateResult`。
- 辩者数可配且不少于二；裁决者恒一。
- 每位辩者各有独立 Thread；同一辩者三阶段复用其 Thread 与 Agent 实例。
- 每阶段并发；存活者少于 `quorum` 即以稳定 `RuntimeError` 失败。
- 裁决失败即全轮失败；不得以模板假说、文本兼容客户端或第二裁决者降级。
- 所有 Agent 取得同一份 `ResearchTree.to_dict()` 冻结快照及数据、论文、模型输入。
- 大 artifact 只注入引用，不解引用日志、diff 或逐样本内容。
- 源码中 system prompt 与 Pydantic `description` 依项目规范使用英文；本计划叙述用文言。
- 不留 `athena.brainstorm` 重导出、弃用门面或双重事实源。
- 不改排名、预算、Supervisor、Evaluator 与实验执行策略。
- 勿回退工作树中既有他人改动；每次提交只暂存本任务所列文件。

---

## File Map

- Modify `src/athena/app_server/thread_runtime.py`：令每 Turn 可等待且返终态结果。
- Modify `src/athena/app_server/thread_manager.py`：公开唯一新增法 `wait_turn()`。
- Modify `test/unit/app_server/test_thread_runtime.py`：锁定成功、失败、中断与完成后再等待。
- Modify `test/unit/app_server/test_thread_manager.py`：锁定 manager 委托行为。
- Create `src/athena/ideator/types.py`：三公开类型及私有结构化轮次模型。
- Create `src/athena/ideator/ideator.py`：私有 runner、提示组装与辩论编排。
- Create `src/athena/ideator/__init__.py`：仅重导出三公开成员。
- Create `tests/test_ideator.py`：领域、并发、法定人数、裁决、取消与审计测试。
- Modify `src/athena/workflows/search/idea_generation.py`：调用 `Ideator` 并登记裁决结果。
- Modify `src/athena/workflows/search/search_loop.py`：注入真实 `DataProfile` 与 `Ideator`。
- Modify `tests/test_search_workflow.py`：锁定无 Ideator 明败及注入成功路径。
- Modify `tests/test_e2e_ai4ml.py`：以显式假 Ideator 作离线端到端契约测试。
- Move `tests/test_retrieval_brainstorm.py` to `tests/test_retrieval.py`：仅留检索测试。
- Modify `examples/ai4ml_pipeline.py`、`examples/trial_run.py`：显式组装真实 Ideator，不依模板降级。
- Modify `docs/architecture/current.md`、`docs/architecture/target.md`、`docs/冗余设计.md`：改 owner、能力与冗余处置。
- Delete `src/athena/brainstorm/__init__.py`、`generate.py`、`types.py`：旧包尽删。

---

### Task 1: 使 Thread Turn 可待终态

**Files:**
- Modify: `src/athena/app_server/thread_runtime.py`
- Modify: `src/athena/app_server/thread_manager.py`
- Test: `test/unit/app_server/test_thread_runtime.py`
- Test: `test/unit/app_server/test_thread_manager.py`

**Interfaces:**
- Consumes: 既有 `TurnRuntime.done: Future[TurnTerminalState]`、`RuntimeThreadManager.submit()`。
- Produces: `ThreadHandle.wait_turn(turn_id: str) -> ArtifactRef` 与 `RuntimeThreadManager.wait_turn(thread_id: str, turn_id: str) -> ArtifactRef`。

- [ ] **Step 1: 先书 ThreadHandle 失败测试**

于 `test_thread_runtime.py` 墹三测：

```python
async def test_wait_turn_returns_result_even_after_fast_completion(self) -> None:
    runtime = ThreadRuntime(
        "thread:1", "session:1", "artifact:context", immediate_runner
    )
    await runtime.start()
    handle = ThreadHandle(runtime)
    try:
        await handle.submit(StartTurn("turn:1", "artifact:request"))
        await eventually(lambda: runtime.state == "idle")
        self.assertEqual(await handle.wait_turn("turn:1"), "artifact:result")
    finally:
        await runtime.force_close()

async def test_wait_turn_raises_for_runner_failure(self) -> None:
    async def fail(*_args):
        raise LookupError("broken")

    runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", fail)
    await runtime.start()
    handle = ThreadHandle(runtime)
    try:
        await handle.submit(StartTurn("turn:1", "artifact:request"))
        with self.assertRaisesRegex(RuntimeError, "LookupError"):
            await handle.wait_turn("turn:1")
    finally:
        await runtime.force_close()

async def test_wait_turn_propagates_interrupt_as_cancellation(self) -> None:
    runner = BlockingRunner()
    runtime = ThreadRuntime("thread:1", "session:1", "artifact:context", runner)
    await runtime.start()
    handle = ThreadHandle(runtime)
    try:
        await handle.submit(StartTurn("turn:1", "artifact:request"))
        await asyncio.wait_for(runner.started.wait(), timeout=0.1)
        await handle.submit(InterruptTurn("turn:1", "test"))
        with self.assertRaises(asyncio.CancelledError):
            await handle.wait_turn("turn:1")
    finally:
        runner.release.set()
        await runtime.force_close()
```

- [ ] **Step 2: 运行测试，证其先败**

Run: `uv run pytest -q test/unit/app_server/test_thread_runtime.py -k "wait_turn"`

Expected: FAIL，因 `ThreadHandle` 尚无 `wait_turn`。

- [ ] **Step 3: 接通既有 TurnRuntime.done**

于 `ThreadRuntime.__init__` 增私有映射：

```python
self._turn_done: dict[str, asyncio.Future[TurnTerminalState]] = {}
```

并自 `athena.app_server.submissions` 显式导入既有 `TurnTerminalState`，不另定义终态类型。

于 `spawn_turn()` 登记既有 future：

```python
self._turn_done[turn.turn_id] = tr.done
```

于三终态提交法中，在 `_clear_active_turn()` 前分别置值：

```python
TurnTerminalState(result_ref=result_ref, next_context_ref=next_context_ref)
TurnTerminalState(exception_type=exception_type)
TurnTerminalState(cancelled=True)
```

再增：

```python
async def wait_turn(self, turn_id: str) -> ArtifactRef:
    try:
        done = self._turn_done[turn_id]
    except KeyError as exc:
        raise KeyError(f"unknown turn: {turn_id}") from exc
    terminal = await asyncio.shield(done)
    if terminal.cancelled:
        raise asyncio.CancelledError
    if terminal.exception_type is not None:
        raise RuntimeError(f"turn failed: {terminal.exception_type}")
    if terminal.result_ref is None:
        raise RuntimeError("completed turn has no result reference")
    return terminal.result_ref
```

`ThreadHandle.wait_turn()` 仅委托此法。不可轮询 `state`，不可读 journal 私有记录。

- [ ] **Step 4: 增 manager 委托测试与实现**

于 `test_thread_manager.py` 增：

```python
async def test_wait_turn_returns_runner_result(self) -> None:
    manager = RuntimeThreadManager(immediate_runner)
    try:
        thread = await manager.start("session:1", "artifact:context")
        turn = await manager.submit(thread.thread_id, "artifact:request")
        self.assertEqual(
            await manager.wait_turn(thread.thread_id, turn.turn_id),
            "artifact:result",
        )
    finally:
        await manager.aclose("test_cleanup")
```

于 `RuntimeThreadManager` 增：

```python
async def wait_turn(self, thread_id: str, turn_id: str) -> ArtifactRef:
    self._require_ref(thread_id, "thread_id")
    self._require_ref(turn_id, "turn_id")
    return await (await self.get(thread_id)).wait_turn(turn_id)
```

- [ ] **Step 5: 跑完 runtime 关联测试**

Run: `uv run pytest -q test/unit/app_server/test_thread_runtime.py test/unit/app_server/test_thread_manager.py`

Expected: PASS；成功、失败、中断、快速完成后再等待皆有定果。

- [ ] **Step 6: 提交 Turn 等待能力**

```powershell
git add src/athena/app_server/thread_runtime.py src/athena/app_server/thread_manager.py test/unit/app_server/test_thread_runtime.py test/unit/app_server/test_thread_manager.py
git commit -m "feat(app-server): expose turn completion results"
```

---

### Task 2: 建 Ideator 精简领域类型

**Files:**
- Create: `src/athena/ideator/types.py`
- Create: `src/athena/ideator/__init__.py`
- Create: `tests/test_ideator.py`

**Interfaces:**
- Consumes: `athena.core.schemas.Hypothesis`、`ArtifactRef`。
- Produces: `IdeatorConfig`、`DebateResult`；私有 `_ProposalBatch`、`_ReviewBatch`、`_RevisionBatch`、`_JudgeOutput`、`_TurnRequest`。

- [ ] **Step 1: 书配置与导出失败测试**

```python
import pytest
from pydantic import ValidationError

from athena.ideator import DebateResult, Ideator, IdeatorConfig


def test_ideator_exports_only_three_public_members() -> None:
    import athena.ideator as package

    assert package.__all__ == ["Ideator", "IdeatorConfig", "DebateResult"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"debater_count": 1},
        {"debater_count": 3, "quorum": 4},
        {"stage_timeout_seconds": 0},
        {"max_hypotheses": 2},
        {"max_hypotheses": 6},
    ],
)
def test_ideator_config_rejects_invalid_bounds(kwargs) -> None:
    with pytest.raises(ValidationError):
        IdeatorConfig(**kwargs)
```

- [ ] **Step 2: 运行测试，证模块尚无**

Run: `uv run pytest -q tests/test_ideator.py`

Expected: collection FAIL with `ModuleNotFoundError: athena.ideator`。

- [ ] **Step 3: 实作三公开类型及私有轮次模型**

`types.py` 以此为规范：

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from athena.core.schemas import ArtifactRef, Hypothesis


class IdeatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    debater_count: int = Field(default=3, ge=2)
    quorum: int = Field(default=2, ge=2)
    stage_timeout_seconds: float = Field(default=120.0, gt=0)
    max_hypotheses: int = Field(default=5, ge=3, le=5)

    @model_validator(mode="after")
    def validate_quorum(self) -> "IdeatorConfig":
        if self.quorum > self.debater_count:
            raise ValueError("quorum cannot exceed debater count")
        return self


class DebateResult(BaseModel):
    hypotheses: list[Hypothesis] = Field(min_length=3, max_length=5)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    artifact_ref: ArtifactRef
```

私有模型须具确切字段：

```python
class _Draft(BaseModel):
    statement: str = Field(min_length=1, description="A falsifiable claim.")
    intervention: str = Field(min_length=1, description="One testable change.")
    expected_effect: str = Field(min_length=1, description="A measurable effect.")
    sources: list[str] = Field(min_length=1, description="Evidence references.")

class _Candidate(_Draft):
    key: str = Field(min_length=1)

class _ProposalBatch(BaseModel):
    hypotheses: list[_Draft] = Field(min_length=3, max_length=5)

class _Critique(BaseModel):
    key: str
    concerns: list[str] = Field(min_length=1)
    recommendation: Literal["retain", "revise", "reject"]

class _ReviewBatch(BaseModel):
    critiques: list[_Critique] = Field(min_length=1)

class _RevisionBatch(BaseModel):
    hypotheses: list[_Candidate] = Field(min_length=3, max_length=5)

class _JudgeDecision(BaseModel):
    candidate_keys: list[str] = Field(min_length=1)
    disposition: Literal["selected", "rejected", "merged"]
    reason: str = Field(min_length=1)

class _JudgeOutput(BaseModel):
    hypotheses: list[_Draft] = Field(min_length=3, max_length=5)
    decisions: list[_JudgeDecision] = Field(min_length=1)

class _TurnRequest(BaseModel):
    stage: Literal["proposal", "review", "revision", "judge"]
    role: Literal["debater", "judge"]
    agent_index: int = Field(ge=0)
    prompt: str = Field(min_length=1)
```

- [ ] **Step 4: 建最小 `Ideator` 壳与严格导出**

于 `ideator.py` 先置可导入之 `Ideator` 壳，其构造签名即冻结为：

```python
class Ideator:
    def __init__(self, *, agent_factory, artifacts, config=None) -> None:
        self._agent_factory = agent_factory
        self._artifacts = artifacts
        self._config = config or IdeatorConfig()
```

`__init__.py` 仅导入三公开成员，并令 `__all__` 顺序与测试一致。

- [ ] **Step 5: 跑类型测试**

Run: `uv run pytest -q tests/test_ideator.py`

Expected: PASS。

- [ ] **Step 6: 提交类型边界**

```powershell
git add src/athena/ideator/__init__.py src/athena/ideator/types.py src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "feat(ideator): define compact debate contracts"
```

---

### Task 3: 以 RuntimeThreadManager 承结构化 Agent Turn

**Files:**
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: `RuntimeThreadManager.wait_turn()`、`ArtifactStore.put_text/get_text()`、私有 `_TurnRequest` 与各输出模型。
- Produces: 私有 `_DebateRunner`，其 callable 签名与 app-server `Runner` 相同；同一 Thread 恒复用同一 Agent 实例。

私有 Agent 契约定为：

```python
class _StructuredAgent(Protocol):
    async def run(self, prompt: str, *, output_type: type[BaseModel]) -> object:
        """Return an object or an object exposing a validated output attribute."""
```

- [ ] **Step 1: 书 runner 身份与结果 artifact 失败测试**

以 `RecordingAgentFactory` 记录每次所建对象。测试当断：两 Thread 生两 Agent，同一 Thread 连交 proposal/review 只用一个 Agent；返回 ref 可由 store 解成所需模型。

```python
@pytest.mark.asyncio
async def test_debate_runner_reuses_agent_per_thread(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    factory = RecordingAgentFactory()
    runner = _DebateRunner(factory, store)
    manager = RuntimeThreadManager(runner)
    try:
        context_ref = await store.put_text("{}")
        first = await manager.start("debate:1", context_ref)
        second = await manager.start("debate:1", context_ref)

        proposal_ref = await runner.put_request("proposal", "debater", 0, "propose")
        first_turn = await manager.submit(first.thread_id, proposal_ref)
        result_ref = await manager.wait_turn(first.thread_id, first_turn.turn_id)
        assert len((await runner.read_result("proposal", result_ref)).hypotheses) == 3

        review_ref = await runner.put_request("review", "debater", 0, "review")
        review_turn = await manager.submit(first.thread_id, review_ref)
        await manager.wait_turn(first.thread_id, review_turn.turn_id)

        other_ref = await runner.put_request("proposal", "debater", 1, "propose")
        other_turn = await manager.submit(second.thread_id, other_ref)
        await manager.wait_turn(second.thread_id, other_turn.turn_id)

        assert factory.created_for == [("debater", 0), ("debater", 1)]
    finally:
        await manager.aclose("test_cleanup")
```

- [ ] **Step 2: 运行测试，证私有 runner 尚无**

Run: `uv run pytest -q tests/test_ideator.py -k "debate_runner"`

Expected: FAIL，因 `_DebateRunner` 尚未定义。

- [ ] **Step 3: 实作 `_DebateRunner`**

其职责仅四事：存请求、按 stage 定输出型、按 Thread 绑定 Agent、存结构化结果。

```python
_OUTPUT_TYPES = {
    "proposal": _ProposalBatch,
    "review": _ReviewBatch,
    "revision": _RevisionBatch,
    "judge": _JudgeOutput,
}

class _DebateRunner:
    def __init__(self, agent_factory, artifacts) -> None:
        self._agent_factory = agent_factory
        self._artifacts = artifacts
        self._agents = {}
        self._bindings = {}

    async def put_request(self, stage, role, agent_index, prompt):
        request = _TurnRequest(
            stage=stage, role=role, agent_index=agent_index, prompt=prompt
        )
        return await self._artifacts.put_text(request.model_dump_json())

    async def read_result(self, stage, result_ref):
        schema = _OUTPUT_TYPES[stage]
        return schema.model_validate_json(await self._artifacts.get_text(result_ref))

    async def __call__(self, thread, turn, emit):
        return await self._run(thread, turn, emit, memory=None)

    async def run_with_context(self, thread, turn, emit, memory, cancel):
        if cancel.is_set():
            raise asyncio.CancelledError
        return await self._run(thread, turn, emit, memory=memory)

    async def _run(self, thread, turn, emit, memory):
        request = _TurnRequest.model_validate_json(
            await self._artifacts.get_text(turn.request_ref)
        )
        binding = (request.role, request.agent_index)
        prior = self._bindings.setdefault(thread.thread_id, binding)
        if prior != binding:
            raise RuntimeError("thread agent binding changed")
        agent = self._agents.get(thread.thread_id)
        if agent is None:
            agent = self._agent_factory(*binding)
            self._agents[thread.thread_id] = agent
        schema = _OUTPUT_TYPES[request.stage]
        if memory is not None:
            memory.append(ModelRequest(parts=[UserPromptPart(content=request.prompt)]))
        raw = await agent.run(request.prompt, output_type=schema)
        output = schema.model_validate(getattr(raw, "output", raw))
        if memory is not None:
            memory.append(
                ModelResponse(parts=[TextPart(content=output.model_dump_json())])
            )
        result_ref = await self._artifacts.put_text(output.model_dump_json())
        await emit("ideator/stage_completed", result_ref)
        return AgentOutcome(result_ref=result_ref, next_context_ref=thread.context_ref)
```

若 factory 所返对象无 async `run(prompt, output_type=<schema>)`，令自然 `AttributeError/TypeError` 进入 Turn 失败，不另吞之。

- [ ] **Step 4: 跑 runner 与 runtime 测试**

Run: `uv run pytest -q tests/test_ideator.py -k "debate_runner" test/unit/app_server/test_thread_manager.py`

Expected: PASS。

- [ ] **Step 5: 提交 runtime 适配**

```powershell
git add src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "feat(ideator): run structured agents in thread runtime"
```

---

### Task 4: 成并发立论、互评、修案与唯一裁决

**Files:**
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: `IdeatorConfig`、`_DebateRunner`、`DataProfile`、`PaperRef`、`HFModelRef`、`ResearchTree.to_dict()`。
- Produces: `Ideator.generate(profile, papers, models, tree) -> DebateResult`。

- [ ] **Step 1: 书成功辩局与并发失败测试**

测试 factory 以 `asyncio.Event` 屏障阻住所有 proposal；惟全部辩者皆已进入方释放。断言：

```python
result = await ideator.generate(profile, papers, models, tree)
assert len(result.hypotheses) == 3
assert len(factory.debater_instances) == config.debater_count
assert len(factory.judge_instances) == 1
assert factory.max_parallel_proposals == config.debater_count
assert {entry["stage"] for entry in result.transcript} == {
    "proposal", "review", "revision", "judge"
}
assert all(h.parent_id == tree.best_experiment_id() for h in result.hypotheses)
assert all(result.artifact_ref in h.evidence_refs for h in result.hypotheses)
audit = json.loads(await store.get_text(result.artifact_ref))
assert audit["transcript"] == result.transcript
assert audit["failures"] == result.failures
```

另记 factory 每实例所见 prompt，断言所有 proposal prompt 所含 `ResearchTree.to_dict()` JSON 完全相同；review prompt 不含作者索引，且不含该辩者己案 key。

- [ ] **Step 2: 运行成功路径测试，证 `generate` 尚无**

Run: `uv run pytest -q tests/test_ideator.py -k "concurrent or full_research or anonymous or audit"`

Expected: FAIL，因 `Ideator.generate` 尚未实现。

- [ ] **Step 3: 实作上下文与提示组装**

以 `profile.model_dump(mode="json")`、`dataclasses.asdict(paper/model)`、`tree.to_dict()` 合成 `context_payload`；只调用一次 `tree.to_dict()`，再以 `json.dumps(context_payload, ensure_ascii=False, sort_keys=True)` 供诸 prompt 共用。

四提示皆须有英文 system task，并含：

- proposal：全研究上下文、可证伪、单一干预、可量测效果、非空来源、不得重复旧假说；
- review：匿名他案、实验史冲突、重复性、可实现性、泄漏风险与建议；
- revision：己之原案、所得全部批评、研究上下文，且须保留 candidate key；
- judge：全研究上下文、所有原案/批评/修案/失败，选三至 `max_hypotheses` 案并逐项给 decision。

不得把 artifact 引用所指正文读入 prompt。

- [ ] **Step 4: 实作阶段调用与 key 管理**

私有调用法须在超时时中断该 Turn：

```python
async def _invoke(manager, runner, thread_id, request):
    turn = await manager.submit(thread_id, request)
    try:
        async with asyncio.timeout(self._config.stage_timeout_seconds):
            return await manager.wait_turn(thread_id, turn.turn_id)
    except TimeoutError:
        await manager.interrupt(thread_id, turn.turn_id, "ideator stage timeout")
        raise
```

proposal 成后由协调器以 `idea-{agent_index}-{ordinal}` 赋 key；作者映射只留在 coordinator，不入匿名 review prompt。每阶段以 `asyncio.gather(*stage_calls, return_exceptions=True)` 并发，并只将成功者送入下一阶段。

- [ ] **Step 5: 实作唯一裁决与审计 artifact**

裁决输出先转成带 `new_id("hyp")`、`status="PROPOSED"`、`parent_id=tree.best_experiment_id()` 的 `Hypothesis`，再行既有可证伪校验：字段非空、来源非空、案之间 statement 不重复。

审计 artifact 不得自含自身引用。正确次序为：

1. 先存无 `artifact_ref` 的 audit payload，得 `audit_ref`；
2. 将 `audit_ref` 添入每项 `Hypothesis.evidence_refs`；
3. 构造 `DebateResult(hypotheses=linked, transcript=transcript, failures=failures, artifact_ref=audit_ref)` 返回；
4. 测试读取 `audit_ref` 时比较其 transcript、failures 与未加链接前的 hypothesis 内容，不要求 artifact 自包含自身 ref。

- [ ] **Step 6: 跑成功与审计测试**

Run: `uv run pytest -q tests/test_ideator.py -k "not quorum and not failure and not cancel"`

Expected: PASS；辩者并发、同 Thread 三 Turn、唯一裁决、全树快照与审计引用皆有证。

- [ ] **Step 7: 提交成功辩局**

```powershell
git add src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "feat(ideator): orchestrate multi-agent debate"
```

---

### Task 5: 固定法定人数、裁决失败与取消语义

**Files:**
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: Task 4 的阶段执行器与 `failures` 记录。
- Produces: 稳定错误 `RuntimeError("ideator quorum not met")`、`RuntimeError("ideator judge failed")`；取消保持 `asyncio.CancelledError`。

- [ ] **Step 1: 书法定人数与裁决失败测试**

```python
@pytest.mark.asyncio
async def test_one_failed_debater_degrades_when_quorum_survives(
    ideator, factory, profile, tree
):
    factory.fail("debater", 2, "proposal")
    result = await ideator.generate(profile, [], [], tree)
    assert result.failures == [
        {"agent": "debater-2", "stage": "proposal", "error": "RuntimeError"}
    ]
    assert len(result.hypotheses) >= 3

@pytest.mark.asyncio
async def test_quorum_failure_has_no_judge_or_result_artifact(
    ideator, factory, profile, tree
):
    factory.fail("debater", 1, "review")
    factory.fail("debater", 2, "review")
    with pytest.raises(RuntimeError, match="ideator quorum not met"):
        await ideator.generate(profile, [], [], tree)
    assert factory.judge_instances == []

@pytest.mark.asyncio
async def test_judge_failure_has_no_fallback(
    ideator, factory, profile, tree
):
    factory.fail("judge", 0, "judge")
    with pytest.raises(RuntimeError, match="ideator judge failed"):
        await ideator.generate(profile, [], [], tree)
    assert factory.judge_calls == 1
```

- [ ] **Step 2: 书取消清理测试**

令所有 proposal 永候，外部取消 `generate()` task；断言抛 `CancelledError`、runner 中各 Agent Turn 均被取消，且 manager `state == "closed"`。测试不得以 `sleep()` 猜时，须用 started/cancelled Event 同步。

- [ ] **Step 3: 运行故障测试，证当前行为未全**

Run: `uv run pytest -q tests/test_ideator.py -k "quorum or failure or cancel"`

Expected: 至少一项 FAIL，因稳定错误映射与取消清理尚未齐。

- [ ] **Step 4: 实作统一阶段收敛**

每阶段将异常归一为：

```python
{"agent": f"debater-{index}", "stage": stage, "error": type(exc).__name__}
```

但 `asyncio.CancelledError` 不得收作普通失败，须立刻上抛。阶段结束执行：

```python
if len(survivors) < self._config.quorum:
    raise RuntimeError("ideator quorum not met")
```

裁决任何异常除 `CancelledError` 外，皆以 `RuntimeError("ideator judge failed") from exc` 抛出。

- [ ] **Step 5: 以 `finally` 唯一关闭 manager**

`generate()` 建 manager 后即入 `try/finally`：

```python
manager = RuntimeThreadManager(runner, ctx=ContextManager())
try:
    return await self._generate_with_manager(
        profile, papers, models, tree, manager, runner
    )
finally:
    await manager.aclose("ideator round finished")
```

Task 4 中 `_generate_with_manager` 之签名定为：

```python
async def _generate_with_manager(
    self, profile, papers, models, tree, manager, runner
) -> DebateResult:
    """Run the four fixed debate stages on an owned thread manager."""
```

不得另起背景清理任务；`aclose()` 负责中断活跃 Turn 并候其退出。

- [ ] **Step 6: 跑全 Ideator 测试**

Run: `uv run pytest -q tests/test_ideator.py`

Expected: PASS，且测试终了无 pending task 警告。

- [ ] **Step 7: 提交故障语义**

```powershell
git add src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "fix(ideator): enforce quorum and judge failures"
```

---

### Task 6: 迁移 IdeaGeneration 并尽删 Brainstorm

**Files:**
- Modify: `src/athena/workflows/search/idea_generation.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_e2e_ai4ml.py`
- Move: `tests/test_retrieval_brainstorm.py` -> `tests/test_retrieval.py`
- Modify: `examples/ai4ml_pipeline.py`
- Modify: `examples/trial_run.py`
- Modify: `docs/architecture/current.md`
- Modify: `docs/architecture/target.md`
- Modify: `docs/冗余设计.md`
- Delete: `src/athena/brainstorm/__init__.py`
- Delete: `src/athena/brainstorm/generate.py`
- Delete: `src/athena/brainstorm/types.py`

**Interfaces:**
- Consumes: `Ideator.generate()` 与 `DebateResult.hypotheses`。
- Produces: `generate_hypotheses(data_profile, papers, models, tree, *, ideator) -> list[Hypothesis]`；`SearchLoop` 增 keyword-only `data_profile: DataProfile` 与 `ideator: Ideator | None = None`。

- [ ] **Step 1: 先书 workflow 注入测试**

于 `tests/test_search_workflow.py` 增 `RecordingIdeator`；其 `generate()` 返三项带固定 ID、parent 与 `sha256:` evidence ref 的 `DebateResult`。断言：

```python
registered = await generate_hypotheses(
    profile, [], [], tree, ideator=recording_ideator
)
assert recording_ideator.calls == [(profile, [], [], tree)]
assert [item.id for item in registered] == ["hyp_1", "hyp_2", "hyp_3"]
assert [item.id for item in tree.pending_hypotheses()] == [
    "hyp_1", "hyp_2", "hyp_3"
]
```

再测 `SearchLoop` 于无 pending 且未注入 ideator 时，抛 `RuntimeError("SEARCH requires an Ideator")`；注入后则以构造时所给 `DataProfile` 调之，不造 `row_count=0` 画像。

- [ ] **Step 2: 运行 workflow 测试，证旧签名不合**

Run: `uv run pytest -q tests/test_search_workflow.py -k "ideator or generate_hypotheses"`

Expected: FAIL，因 workflow 仍收 `llm` 并调旧 brainstorm。

- [ ] **Step 3: 改 IdeaGeneration 与 SearchLoop**

`idea_generation.generate_hypotheses` 改为：

```python
async def generate_hypotheses(
    data_profile: DataProfile,
    papers: list[PaperRef],
    models: list[HFModelRef],
    tree: ResearchTree,
    *,
    ideator: Ideator,
) -> list[Hypothesis]:
    result = await ideator.generate(data_profile, papers, models, tree)
    for hypothesis in result.hypotheses:
        tree.add_hypothesis(hypothesis)
    return result.hypotheses
```

删 `llm=None` 与空 `DataProfile` 路径。`SearchLoop.__init__` 增 keyword-only `data_profile` 与 `ideator`；仅待树无 pending 时要求 ideator，随后把真实画像传入。

- [ ] **Step 4: 迁移端到端测试与例程**

`tests/test_e2e_ai4ml.py` 以显式 `RecordingIdeator` 返回三项已校验假说，不依网络。两例程从 `ATHENA_IDEATOR_MODEL` 读取模型名，以 `pydantic_ai.Agent(model)` 为 factory，并以 `.athena/artifacts` 下 `LocalArtifactStore` 构造 `Ideator`：

```python
model = os.environ.get("ATHENA_IDEATOR_MODEL")
if not model:
    raise RuntimeError("ATHENA_IDEATOR_MODEL is required for idea generation")
ideator = Ideator(
    agent_factory=lambda _role, _index: PydanticAgent(model),
    artifacts=LocalArtifactStore(Path(".athena/artifacts")),
)
```

两处 `generate_hypotheses` 皆显式传 `ideator=ideator`。不得在例程中复生固定模板。

- [ ] **Step 5: 拆检索测试并删旧包**

以 `Move-Item` 将 `tests/test_retrieval_brainstorm.py` 改名 `tests/test_retrieval.py`，删除其中所有 brainstorm imports、假说生成、single-turn 与模板降级测试；检索、HF 下载等测试原样保留。随后删 `src/athena/brainstorm/` 三文件。

- [ ] **Step 6: 更新架构与冗余文档**

`docs/architecture/current.md` 将 owner 改为 `athena.ideator.Ideator`，能力写为“可配置独立辩者 Thread、并发立论/互评/修案、唯一裁决、完整审计 artifact、无模板降级”。`target.md` 将迁移项 Brainstorm 改为 Ideator。`docs/冗余设计.md` 第 8 项记“已治理：旧包装类型随 brainstorm 删除，最终契约为 DebateResult”。

- [ ] **Step 7: 跑迁移关联测试与静态扫描**

Run:

```powershell
uv run pytest -q tests/test_retrieval.py tests/test_ideator.py tests/test_search_workflow.py tests/test_e2e_ai4ml.py
uv run python -m compileall -q examples/ai4ml_pipeline.py examples/trial_run.py
rg -n "from athena\.brainstorm|import athena\.brainstorm" src tests test examples
Test-Path src\athena\brainstorm
```

Expected: tests PASS；compileall 成功；`rg` 无匹配并以 1 退出；`Test-Path` 输出 `False`。

- [ ] **Step 8: 跑全量门禁**

Run:

```powershell
uv run pytest -q tests test/unit
git diff --check
```

Expected: 全量 PASS；`git diff --check` 无输出。若全量发现与本变更无关之既有失败，须记录失败测试与证据，不得扩大捕获或改无关模块掩之。

- [ ] **Step 9: 提交迁移与删除**

```powershell
git add src/athena/ideator src/athena/workflows/search/idea_generation.py src/athena/workflows/search/search_loop.py tests/test_ideator.py tests/test_retrieval.py tests/test_search_workflow.py tests/test_e2e_ai4ml.py examples/ai4ml_pipeline.py examples/trial_run.py docs/architecture/current.md docs/architecture/target.md docs/冗余设计.md
git add -u -- src/athena/brainstorm tests/test_retrieval_brainstorm.py
git commit -m "refactor(search): replace brainstorm with ideator debate"
```

---

## Batch Acceptance

全任务毕后，自仓根运行：

```powershell
uv run pytest -q tests/test_ideator.py `
  tests/test_retrieval.py `
  tests/test_search_workflow.py `
  tests/test_e2e_ai4ml.py `
  test/unit/app_server/test_thread_runtime.py `
  test/unit/app_server/test_thread_manager.py
uv run pytest -q tests test/unit
uv run python -m compileall -q src/athena examples
rg -n "from athena\.brainstorm|import athena\.brainstorm" src tests test examples
rg -n "template_hypotheses|BrainStormResult|HypothesisInput|single_turn" src tests test examples
Test-Path src\athena\brainstorm
git diff --check
```

验收当为：

- 关联及全量测试皆过；
- Python 编译检查过；
- 两次 `rg` 皆无匹配；
- 旧目录不存；
- `athena.ideator.__all__` 恰三项；
- 并发、法定人数、唯一裁决、全树快照、审计 artifact、取消清理皆有自动测试；
- 无模板降级、无第二 Agent runtime、无无关改动混入。
