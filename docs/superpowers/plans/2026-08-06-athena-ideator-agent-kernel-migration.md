# Athena Ideator AgentKernel Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Ideator 多 Agent 辩论迁至共享 AgentKernel，新增薄 AgentTeam，以有界匿名互评、完整论文/模型/ResearchTree 输入及唯一裁决生成可审计假说。

**Architecture:** composition root 唯一拥有 `AgentKernel/AgentControl`，Ideator 只获得共享 control 与工作流父 Handle。`AgentTeam` 负责成员 spawn/followup、并发等待、单次 arbiter 与自有 Handle 清理；Ideator 继续拥有 proposal/review/revision/judge、匿名分配、quorum、lineage、科学结构校验与 audit。

**Tech Stack:** Python >=3.11、asyncio、Pydantic v2、pydantic-ai message types、Athena AgentKernel、ArtifactStore、pytest/pytest-asyncio、Black 26.5.1。

## Global Constraints

- 不新增第三方依赖；Python 最低版本保持 `>=3.11`。
- 单行不超过 88 字符，并通过仓库 Black 与 pre-commit 门禁。
- 生产代码不得继续引用 `RuntimeThreadManager`、`AthenaTurn`、旧 `AgentOutcome` 或 `_DebateRunner`。
- 不设 feature flag、compat runtime、shadow write、双 journal 或模板 fallback。
- `AgentTeam` 不得调用共享 `AgentControl.aclose()`，不得关闭注入的父 Handle。
- 普通辩者数量由 `IdeatorConfig.debater_count` 控制；arbiter 恰为一个且不计入 quorum。
- 正常匿名审查量为 `N * H`；一次故障重派后总量不得超过 `2 * N * H`。
- GraphStore 只接收 ArtifactRef，不保存 prompt、研究树正文或模型输出正文。
- 每项生产改动先写能因缺失行为而失败的测试，确认失败原因后再写最小实现。
- 不改 ranking、budget、Supervisor、Evaluator、ResearchTree schema 或 app-server 协议。

## File Map

```text
src/athena/core/agent_kernel/team.py
    通用 Team 成员表、spawn/followup、并发等待、单次 arbiter、自有清理。

src/athena/core/agent_kernel/__init__.py
    公开导出 AgentTeam，不增加 Ideator 领域类型。

src/athena/ideator/types.py
    严格科学 draft、阶段输出、版本化 transcript/failure/audit 私有 DTO。

src/athena/ideator/ideator.py
    ArtifactRef codec、结构化 Runner、有界匿名分配及四阶段领域编排。

src/athena/workflows/search/search_loop.py
    注入 ReferenceSearch，并发取得论文与模型后交给 Ideator。

test/unit/agent_kernel/test_team.py
    AgentTeam 的通用生命周期、并发、timeout、取消与关闭所有权测试。

tests/test_ideator.py
    Ideator 的科学 schema、连续 Agent memory、有界匿名互评、quorum、唯一裁决与 audit 测试。

tests/test_search_workflow.py
    SearchLoop 论文/模型并发检索与原样注入测试。

examples/trial_run.py
examples/ai4ml_pipeline.py
    显式 composition root、共享 control/父 Handle 与最终 Kernel 清理示例。

docs/architecture/current.md
docs/architecture/target.md
    更新 Ideator 已接入统一 Kernel 的当前事实与删除旧 runtime 的边界。
```

---

### Task 1: AgentTeam 构造、首次 Run 与 Followup

**Files:**
- Create: `src/athena/core/agent_kernel/team.py`
- Modify: `src/athena/core/agent_kernel/__init__.py`
- Create: `test/unit/agent_kernel/test_team.py`

**Interfaces:**
- Consumes: `AgentControl.spawn(parent, spec, task, name=...)`、`AgentControl.followup(handle, task)`、`AgentRun.wait(timeout)`、`AgentSpec`、`AgentHandle`。
- Produces: `AgentTeam(control, parent, member_specs, arbiter_spec, quorum, name)`、`run_members(tasks, timeout)`、`arbitrate(task, timeout)`、`aclose()`。

- [ ] **Step 1: 写构造约束与公开导出的失败测试**

```python
class PassThroughRunner:
    async def run(self, request, *, session, emit):
        return request


def _spec(runner=None, *, role: str = "debater") -> AgentSpec:
    return AgentSpec(
        runner=runner or PassThroughRunner(),
        codec=JsonCodec(),
        role=role,
    )


async def _started_control_and_parent() -> tuple[AgentControl, AgentHandle]:
    kernel = AgentKernel()
    await kernel.start()
    control = AgentControl(kernel)
    parent, run = await control.create_root(
        _spec(role="research"), "workflow", name="research"
    )
    assert await run.wait() == "workflow"
    return control, parent


def _team(
    control: AgentControl,
    parent: AgentHandle,
    *,
    member_count: int = 2,
    quorum: int = 2,
    member_specs: list[AgentSpec] | None = None,
) -> AgentTeam:
    specs = member_specs or [_spec() for _ in range(member_count)]
    return AgentTeam(
        control=control,
        parent=parent,
        member_specs=specs,
        arbiter_spec=_spec(role="arbiter"),
        quorum=quorum,
        name="test-team",
    )


def test_team_rejects_empty_members_and_invalid_quorum() -> None:
    control = object()
    parent = object()
    spec = object()

    with pytest.raises(ValueError, match="member_specs must not be empty"):
        AgentTeam(
            control=control,
            parent=parent,
            member_specs=[],
            arbiter_spec=spec,
            quorum=1,
            name="debate",
        )

    with pytest.raises(ValueError, match="quorum must be between 1 and member count"):
        AgentTeam(
            control=control,
            parent=parent,
            member_specs=[spec, spec],
            arbiter_spec=spec,
            quorum=3,
            name="debate",
        )


def test_agent_kernel_exports_agent_team() -> None:
    assert "AgentTeam" in agent_kernel_exports
```

- [ ] **Step 2: 运行测试并确认因类型尚不存在而失败**

Run:

```bash
uv run pytest test/unit/agent_kernel/test_team.py -q
```

Expected: collection fails because `AgentTeam` cannot be imported.

- [ ] **Step 3: 写最小构造与公开导出**

```python
class AgentTeam:
    def __init__(
        self,
        *,
        control: AgentControl,
        parent: AgentHandle,
        member_specs: Sequence[AgentSpec],
        arbiter_spec: AgentSpec,
        quorum: int,
        name: str,
    ) -> None:
        if not member_specs:
            raise ValueError("member_specs must not be empty")
        if not 1 <= quorum <= len(member_specs):
            raise ValueError("quorum must be between 1 and member count")
        if not name or "/" in name:
            raise ValueError("team name must be non-empty and contain no '/'")
        self._control = control
        self._parent = parent
        self._member_specs = tuple(member_specs)
        self._arbiter_spec = arbiter_spec
        self._quorum = quorum
        self._name = name
        self._members: dict[int, AgentHandle] = {}
        self._active_runs: dict[str, AgentRun] = {}
        self._arbiter: AgentHandle | None = None
        self._arbitrated = False
        self._closed = False
```

Add `AgentTeam` to `athena.core.agent_kernel.__all__`.

- [ ] **Step 4: 写首次运行与后续运行的失败测试**

```python
@pytest.mark.asyncio
async def test_team_spawns_once_then_uses_followup() -> None:
    control, parent = await _started_control_and_parent()
    team = AgentTeam(
        control=control,
        parent=parent,
        member_specs=[_spec(), _spec()],
        arbiter_spec=_spec(role="arbiter"),
        quorum=2,
        name="debate-a",
    )

    first = await team.run_members({0: {"stage": 1}, 1: {"stage": 1}}, timeout=1)
    member_ids = [snapshot.agent_id for snapshot in control.list_agents() if snapshot.role == "debater"]
    second = await team.run_members({0: {"stage": 2}, 1: {"stage": 2}}, timeout=1)

    assert first == {0: {"stage": 1}, 1: {"stage": 1}}
    assert second == {0: {"stage": 2}, 1: {"stage": 2}}
    assert [snapshot.agent_id for snapshot in control.list_agents() if snapshot.role == "debater"] == member_ids
    await team.aclose()
    await control.aclose()
```

- [ ] **Step 5: 运行新测试并确认 `run_members` 缺失**

Run: `uv run pytest test/unit/agent_kernel/test_team.py::test_team_spawns_once_then_uses_followup -q`

Expected: FAIL because `AgentTeam.run_members` is not defined.

- [ ] **Step 6: 实现首次全员 spawn、后续 subset followup 与稳定结果顺序**

```python
async def run_members(
    self, tasks: Mapping[int, object], *, timeout: float | None
) -> dict[int, object | BaseException]:
    self._require_open()
    indices = set(tasks)
    if not self._members and indices != set(range(len(self._member_specs))):
        raise ValueError("first member run must include every member index")
    if not indices.issubset(range(len(self._member_specs))):
        raise ValueError("unknown team member index")

    if not self._members:
        runs = await self._spawn_members(tasks)
    else:
        runs = await self._followup_members(tasks)
    outcomes = await asyncio.gather(
        *(self._wait_member(index, run, timeout) for index, run in runs.items())
    )
    return dict(sorted(outcomes))
```

`_spawn_members()` must run all `control.spawn()` calls concurrently. It stores returned Handles in a local dictionary and assigns `self._members` only after every spawn command succeeds. If any spawn command raises, close every Handle returned by successful spawn commands, await all close calls, then re-raise the first spawn exception. A runner failure occurs in `_wait_member()` after all Handles exist and is returned as that member's exception rather than tearing down healthy members.

`_followup_members()` only accepts indices already present in `_members`; attempting to revive a member whose initial Handle was never created raises `ValueError("team member was not created")`.

- [ ] **Step 7: 运行 AgentTeam 定向测试**

Run: `uv run pytest test/unit/agent_kernel/test_team.py -q`

Expected: PASS.

- [ ] **Step 8: 提交 Task 1**

```bash
git add src/athena/core/agent_kernel/team.py src/athena/core/agent_kernel/__init__.py test/unit/agent_kernel/test_team.py
git commit -m "feat(agent-kernel): add bounded agent team"
```

---

### Task 2: AgentTeam Timeout、唯一 Arbiter 与关闭所有权

**Files:**
- Modify: `src/athena/core/agent_kernel/team.py`
- Modify: `test/unit/agent_kernel/test_team.py`

**Interfaces:**
- Consumes: Task 1 的 `AgentTeam.run_members()` 与 `_active_runs`。
- Produces: 精确 Run timeout/cancel、单次 `arbitrate()`、幂等且不关闭共享 control/父 Handle 的 `aclose()`。

- [ ] **Step 1: 写 timeout 只取消精确 Run 的失败测试**

```python
@pytest.mark.asyncio
async def test_member_timeout_cancels_only_timed_out_run() -> None:
    control, parent = await _started_control_and_parent()
    blocker = BlockingRunner()
    team = AgentTeam(
        control=control,
        parent=parent,
        member_specs=[_spec(blocker), _spec()],
        arbiter_spec=_spec(role="arbiter"),
        quorum=1,
        name="timeout-team",
    )

    outcomes = await team.run_members({0: "slow", 1: "fast"}, timeout=0.01)

    assert isinstance(outcomes[0], TimeoutError)
    assert outcomes[1] == "fast"
    members = {
        item.name: item
        for item in control.list_agents()
        if item.parent_id == parent.agent_id
    }
    assert members["timeout-team-member-0"].status is AgentStatus.IDLE
    assert members["timeout-team-member-1"].status is AgentStatus.IDLE
    await team.aclose()
    await control.aclose()
```

- [ ] **Step 2: 运行测试并确认超时取消行为缺失**

Run: `uv run pytest test/unit/agent_kernel/test_team.py::test_member_timeout_cancels_only_timed_out_run -q`

Expected: FAIL because timeout is not converted into a per-member outcome or the Run remains active.

- [ ] **Step 3: 实现 per-Run 等待与 timeout 取消**

```python
async def _wait_member(
    self, index: int, run: AgentRun, timeout: float | None
) -> tuple[int, object | BaseException]:
    self._active_runs[run.run_id] = run
    try:
        return index, await run.wait(timeout)
    except TimeoutError as exc:
        await run.cancel("agent team stage timeout")
        return index, exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return index, exc
    finally:
        self._active_runs.pop(run.run_id, None)
```

- [ ] **Step 4: 写 arbiter 恰调用一次的失败测试**

```python
@pytest.mark.asyncio
async def test_team_creates_one_arbiter_and_rejects_second_call() -> None:
    control, parent = await _started_control_and_parent()
    team = _team(control, parent, member_count=2, quorum=2)
    await team.run_members({0: "a", 1: "b"}, timeout=1)

    assert await team.arbitrate("decision", timeout=1) == "decision"
    with pytest.raises(AgentCommandError) as error:
        await team.arbitrate("second", timeout=1)

    assert error.value.code is ErrorCode.INVALID_REQUEST
    assert len([item for item in control.list_agents() if item.role == "arbiter"]) == 1
    await team.aclose()
    await control.aclose()
```

- [ ] **Step 5: 实现惰性且不可重复的 arbiter**

```python
async def arbitrate(self, task: object, *, timeout: float | None) -> object:
    self._require_open()
    if self._arbitrated:
        raise AgentCommandError(ErrorCode.INVALID_REQUEST, "arbiter already invoked")
    self._arbitrated = True
    handle, run = await self._control.spawn(
        self._parent,
        self._arbiter_spec,
        task,
        name=f"{self._name}-arbiter",
    )
    self._arbiter = handle
    _, outcome = await self._wait_member(-1, run, timeout)
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome
```

- [ ] **Step 6: 写关闭范围、幂等及外层取消测试**

```python
@pytest.mark.asyncio
async def test_team_close_preserves_parent_and_shared_control() -> None:
    control, parent = await _started_control_and_parent()
    team = _team(control, parent, member_count=2, quorum=2)
    await team.run_members({0: "a", 1: "b"}, timeout=1)

    await asyncio.gather(team.aclose(), team.aclose())

    assert parent.status is AgentStatus.IDLE
    assert all(
        item.status is AgentStatus.CLOSED
        for item in control.list_agents()
        if item.parent_id == parent.agent_id
    )
    extra, run = await control.spawn(parent, _spec(), "still usable", name="extra")
    assert await run.wait() == "still usable"
    await control.close(extra)
    await control.aclose()


@pytest.mark.asyncio
async def test_cancelled_member_batch_is_cleaned_by_team_close() -> None:
    control, parent = await _started_control_and_parent()
    first_blocker = BlockingRunner()
    second_blocker = BlockingRunner()
    team = _team(
        control,
        parent,
        member_specs=[_spec(first_blocker), _spec(second_blocker)],
    )
    batch = asyncio.create_task(team.run_members({0: "a", 1: "b"}, timeout=None))
    await asyncio.gather(first_blocker.started.wait(), second_blocker.started.wait())

    batch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch
    await team.aclose()

    assert all(
        item.status is not AgentStatus.RUNNING
        for item in control.list_agents()
        if item.parent_id == parent.agent_id
    )
    assert parent.status is AgentStatus.IDLE
    await control.aclose()
```

- [ ] **Step 7: 实现共享关闭 Task 与自有 Handle 清理**

```python
async def aclose(self) -> None:
    if self._close_task is None:
        self._close_task = asyncio.create_task(
            self._close_owned_agents(), name=f"agent-team-close:{self._name}"
        )
    await asyncio.shield(self._close_task)


async def _close_owned_agents(self) -> None:
    self._closed = True
    runs = list(self._active_runs.values())
    settle_results = await asyncio.gather(
        *(self._cancel_and_settle(run) for run in runs), return_exceptions=True
    )
    handles = [*self._members.values()]
    if self._arbiter is not None:
        handles.append(self._arbiter)
    close_results = await asyncio.gather(
        *(self._control.close(handle, recursive=True) for handle in handles),
        return_exceptions=True,
    )
    first_error = next(
        (
            result
            for result in [*settle_results, *close_results]
            if isinstance(result, BaseException)
        ),
        None,
    )
    if first_error is not None:
        raise first_error


async def _cancel_and_settle(self, run: AgentRun) -> None:
    try:
        await run.cancel("agent team closed")
    except AgentCommandError as exc:
        if exc.code not in {ErrorCode.NOT_FOUND, ErrorCode.CLOSED}:
            raise
    try:
        await run.wait()
    except (AgentRunInterrupted, AgentRunFailed):
        return
```

- [ ] **Step 8: 运行 Team 与 Kernel 回归**

Run:

```bash
uv run pytest test/unit/agent_kernel/test_team.py test/unit/agent_kernel -q
```

Expected: all tests PASS.

- [ ] **Step 9: 提交 Task 2**

```bash
git add src/athena/core/agent_kernel/team.py test/unit/agent_kernel/test_team.py
git commit -m "fix(agent-team): enforce timeout and owned shutdown"
```

---

### Task 3: 科学 Draft 与版本化 Audit DTO

**Files:**
- Modify: `src/athena/ideator/types.py`
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: `NonBlankText`、`Hypothesis`、Pydantic `ConfigDict` 与 `Field`。
- Produces: `_Draft.intervention`、`_Draft.expected_effect` 规范属性，严格 `_DebateAudit` 及相关私有 DTO。

- [ ] **Step 1: 写单一干预、有限正效果与严格字段测试**

```python
@pytest.mark.parametrize(
    "patch",
    [
        {"interventions": []},
        {"interventions": ["change a", "change b"]},
        {"metric": " "},
        {"minimum_effect": 0},
        {"minimum_effect": float("inf")},
        {"sources": [" "]},
    ],
)
def test_draft_rejects_non_falsifiable_structure(patch: dict[str, object]) -> None:
    payload = {
        "statement": "A controlled change improves accuracy",
        "interventions": ["enable calibration"],
        "metric": "validation accuracy",
        "direction": "increase",
        "minimum_effect": 0.01,
        "sources": ["paper:calibration"],
    }
    payload.update(patch)

    with pytest.raises(ValidationError):
        _Draft.model_validate(payload)


def test_draft_normalizes_hypothesis_fields() -> None:
    draft = _Draft(
        statement="Calibration improves validation accuracy",
        interventions=["enable calibration"],
        metric="validation accuracy",
        direction="increase",
        minimum_effect=0.01,
        sources=["paper:calibration"],
    )

    assert draft.intervention == "enable calibration"
    assert draft.expected_effect == "validation accuracy must increase by at least 0.01"
```

- [ ] **Step 2: 运行测试并确认旧 `_Draft` schema 不接受新结构**

Run: `uv run pytest tests/test_ideator.py -k "draft_rejects or draft_normalizes" -q`

Expected: FAIL because `_Draft` still exposes free-text `intervention/expected_effect`.

- [ ] **Step 3: 实现严格科学 schema**

```python
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Draft(_StrictModel):
    statement: NonBlankText
    interventions: list[NonBlankText] = Field(min_length=1, max_length=1)
    metric: NonBlankText
    direction: Literal["increase", "decrease"]
    minimum_effect: float = Field(gt=0, allow_inf_nan=False)
    sources: list[NonBlankText] = Field(min_length=1)

    @property
    def intervention(self) -> str:
        return self.interventions[0]

    @property
    def expected_effect(self) -> str:
        effect = format(self.minimum_effect, "g")
        return f"{self.metric} must {self.direction} by at least {effect}"


_Stage = Literal["proposal", "review", "revision", "judge"]
_Role = Literal["debater", "judge"]
```

Make `_Candidate`, `_ProposalBatch`, `_Critique`, `_ReviewBatch`, `_RevisionBatch`, `_JudgeDecision`, `_JudgedDraft`, `_JudgeOutput` and `_TurnRequest` inherit `_StrictModel`. Update all fake structured-agent outputs in `tests/test_ideator.py` to use `interventions/metric/direction/minimum_effect/sources`.

- [ ] **Step 4: 写 audit schema 版本、严格字段及序列化测试**

```python
def test_debate_audit_is_versioned_and_forbids_unknown_fields() -> None:
    audit = _DebateAudit(
        context={"tree": {"hypotheses": {}, "experiments": {}}},
        hypotheses=[
            Hypothesis(
                id=f"hyp-{index}",
                statement=f"Claim {index}",
                intervention=f"Change {index}",
                expected_effect=f"Accuracy must increase by at least {index}%",
                sources=[f"source-{index}"],
            )
            for index in range(1, 4)
        ],
        transcript=[],
        failures=[],
        judge_decisions=[],
        hypothesis_lineage=[],
    )

    assert audit.schema_version == 1
    assert audit.codec_id == "athena.ideator.debate.v1"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _DebateAudit.model_validate({**audit.model_dump(), "unknown": True})
```

- [ ] **Step 5: 实现 audit 私有 DTO**

```python
class _TranscriptEntry(_StrictModel):
    stage: Literal["proposal", "review", "revision", "judge"]
    agent: NonBlankText
    result: Any


class _DebateFailure(_StrictModel):
    agent: NonBlankText
    stage: Literal["proposal", "review", "revision", "judge"]
    error: NonBlankText


class _HypothesisLineage(_StrictModel):
    hypothesis_id: NonBlankText
    candidate_keys: list[NonBlankText] = Field(min_length=1)


class _DebateAudit(_StrictModel):
    schema_version: Literal[1] = 1
    codec_id: Literal["athena.ideator.debate.v1"] = "athena.ideator.debate.v1"
    context: dict[str, Any]
    hypotheses: list[Hypothesis]
    transcript: list[_TranscriptEntry]
    failures: list[_DebateFailure]
    judge_decisions: list[_JudgeDecision]
    hypothesis_lineage: list[_HypothesisLineage]
```

- [ ] **Step 6: 运行 Ideator 类型与既有领域测试**

Run: `uv run pytest tests/test_ideator.py -q`

Expected: PASS after fixture payloads and `_validated_hypotheses()` use the normalized properties.

- [ ] **Step 7: 提交 Task 3**

```bash
git add src/athena/ideator/types.py src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "feat(ideator): require structured falsifiable drafts"
```

---

### Task 4: ArtifactRef Codec 与无私有 History 的结构化 Runner

**Files:**
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: `AgentSpec[ArtifactRef, ArtifactRef]`、`RunSession.memory/items`、`RunSession.append_message()`、`ArtifactStore.put_text/get_text()`。
- Produces: 私有 `_ArtifactRefCodec`、`_StructuredAgentRunner.run(request_ref, session, emit)`、`Ideator._spec(role, index)`。

- [ ] **Step 1: 写 ArtifactRef 原样编解码测试**

```python
def test_artifact_ref_codec_is_stateless_identity() -> None:
    codec = _ArtifactRefCodec()
    ref = "sha256:" + "a" * 64

    assert codec.encode_request(ref) == ref
    assert codec.decode_request(ref) == ref
    assert codec.encode_response(ref) == ref
    assert codec.decode_response(ref) == ref
    assert vars(codec) == {}
```

- [ ] **Step 2: 运行测试并确认 codec 尚不存在**

Run: `uv run pytest tests/test_ideator.py::test_artifact_ref_codec_is_stateless_identity -q`

Expected: collection FAIL because `_ArtifactRefCodec` is missing.

- [ ] **Step 3: 实现无状态 codec**

```python
class _ArtifactRefCodec:
    def encode_request(self, value: ArtifactRef) -> ArtifactRef:
        return value

    def decode_request(self, ref: ArtifactRef) -> ArtifactRef:
        return ref

    def encode_response(self, value: ArtifactRef) -> ArtifactRef:
        return value

    def decode_response(self, ref: ArtifactRef) -> ArtifactRef:
        return ref
```

- [ ] **Step 4: 写同一 Agent followup 使用 Session memory 的失败测试**

```python
@pytest.mark.asyncio
async def test_structured_runner_uses_session_memory_across_runs(tmp_path) -> None:
    artifacts = LocalArtifactStore(tmp_path)
    agent = RecordingAgent("debater", 0)
    runner = _StructuredAgentRunner(
        agent=agent,
        artifacts=artifacts,
        role="debater",
        agent_index=0,
    )
    spec = AgentSpec(runner=runner, codec=_ArtifactRefCodec(), role="debater")
    control = AgentControl(AgentKernel())
    await control.kernel.start()

    first_ref = await artifacts.put_text(
        _TurnRequest(
            stage="proposal",
            role="debater",
            agent_index=0,
            prompt="first prompt",
        ).model_dump_json()
    )
    handle, first = await control.create_root(spec, first_ref, name="runner-memory")
    await first.wait()
    second_ref = await artifacts.put_text(
        _TurnRequest(
            stage="review",
            role="debater",
            agent_index=0,
            prompt="second prompt",
        ).model_dump_json()
    )
    await (await control.followup(handle, second_ref)).wait()

    assert agent.message_histories[0] == []
    prompts = [
        part.content
        for message in agent.message_histories[1]
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert prompts == ["first prompt"]
    await control.aclose()
```

Add a second test that submits a `_TurnRequest(role="judge", agent_index=0, ...)`
to this debater Handle and asserts `AgentRunFailed("RuntimeError")`. This proves a caller cannot
change an Agent's role/index binding through a forged request artifact.

- [ ] **Step 5: 运行测试并确认新 Runner 缺失**

Run: `uv run pytest tests/test_ideator.py::test_structured_runner_uses_session_memory_across_runs -q`

Expected: FAIL because `_StructuredAgentRunner` is missing.

- [ ] **Step 6: 实现单实例 Runner 并只用 Session memory**

```python
class _StructuredAgentRunner:
    def __init__(
        self,
        *,
        agent: _StructuredAgent,
        artifacts: ArtifactStore,
        role: _Role,
        agent_index: int,
    ) -> None:
        self._agent = agent
        self._artifacts = artifacts
        self._role = role
        self._agent_index = agent_index

    async def run(self, request_ref, *, session, emit) -> ArtifactRef:
        request = _TurnRequest.model_validate_json(
            await self._artifacts.get_text(request_ref)
        )
        if (request.role, request.agent_index) != (
            self._role,
            self._agent_index,
        ):
            raise RuntimeError("agent request binding changed")
        schema = _OUTPUT_TYPES[request.stage]
        history = list(session.memory.items)
        result = await self._agent.run(
            request.prompt,
            output_type=schema,
            message_history=history,
        )
        output = schema.model_validate(getattr(result, "output", result))
        output_json = output.model_dump_json()
        messages = (
            result.new_messages()
            if isinstance(result, AgentRunResult)
            else [
                ModelRequest(parts=[UserPromptPart(content=request.prompt)]),
                ModelResponse(parts=[TextPart(content=output_json)]),
            ]
        )
        for message in messages:
            session.append_message(message)
        result_ref = await self._artifacts.put_text(output_json)
        await emit("ideator/stage_completed", result_ref)
        return result_ref
```

Add `logger = logging.getLogger(__name__)` at module scope for Task 6's type-only sanitized warnings. Do not keep a dict keyed by agent id, thread id or run id. `Ideator._spec(role, index)` calls the injected `agent_factory` exactly once for that spec and returns `AgentSpec(runner=_StructuredAgentRunner(agent=agent, artifacts=self._artifacts, role=role, agent_index=index), codec=_ArtifactRefCodec(), role=role)`.

- [ ] **Step 7: 运行 Runner、Kernel 及 Ideator 回归**

Run:

```bash
uv run pytest tests/test_ideator.py -k "artifact_ref_codec or structured_runner" -q
uv run pytest test/unit/agent_kernel -q
```

Expected: PASS.

- [ ] **Step 8: 提交 Task 4**

```bash
git add src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "refactor(ideator): run structured agents through kernel sessions"
```

---

### Task 5: 有界匿名 Review 分配与一次故障重派

**Files:**
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: proposal survivor indices and each author's candidate dictionaries.
- Produces: `_review_assignment(survivors)`、`_review_tasks(...)`、`_uncovered_authors(...)`，正常 `NH`、故障时最多 `2NH`。

- [ ] **Step 1: 写五辩者环形匿名分配测试**

```python
def test_review_assignment_is_self_excluding_complete_and_linear() -> None:
    survivors = [0, 1, 2, 3, 4]

    assignment = Ideator._review_assignment(survivors)

    assert assignment == {0: 4, 1: 0, 2: 1, 3: 2, 4: 3}
    assert all(reviewer != author for reviewer, author in assignment.items())
    assert sorted(assignment.values()) == survivors
```

- [ ] **Step 2: 运行测试并确认现实现仍为 all-to-all**

Run: `uv run pytest tests/test_ideator.py::test_review_assignment_is_self_excluding_complete_and_linear -q`

Expected: FAIL because `_review_assignment` does not exist.

- [ ] **Step 3: 实现确定性环形分配**

```python
@staticmethod
def _review_assignment(survivors: list[int]) -> dict[int, int]:
    if len(survivors) < 2:
        return {}
    ordered = sorted(survivors)
    return {
        reviewer: ordered[index - 1]
        for index, reviewer in enumerate(ordered)
    }
```

`_review_tasks()` 给 reviewer 的 prompt 只含所分配作者的候选，并把内部 key 映射为该 reviewer 局部的 `candidate-1..H`。Prompt JSON 不含作者 id、reviewer id 或 `idea-` 内部 key。

- [ ] **Step 4: 写一次故障重派的纯分配测试**

```python
def test_review_retry_assignment_is_single_self_excluding_and_stable() -> None:
    retry = Ideator._review_retry_assignment(
        uncovered_authors=[0, 2],
        successful_reviewers=[0, 2],
        primary_reviewers={0: 1, 2: 0},
    )

    assert retry == {2: [0]}
    assert all(reviewer not in authors for reviewer, authors in retry.items())
```

- [ ] **Step 5: 实现单次稳定重派 helper**

```python
@staticmethod
def _review_retry_assignment(
    *,
    uncovered_authors: list[int],
    successful_reviewers: list[int],
    primary_reviewers: dict[int, int],
) -> dict[int, list[int]]:
    assignments: dict[int, list[int]] = {}
    ordered = sorted(successful_reviewers)
    for author in sorted(uncovered_authors):
        failed_reviewer = primary_reviewers[author]
        eligible = [
            reviewer
            for reviewer in ordered
            if reviewer != author and reviewer != failed_reviewer
        ]
        if not eligible:
            continue
        after_failed = [reviewer for reviewer in eligible if reviewer > failed_reviewer]
        reviewer = (after_failed or eligible)[0]
        assignments.setdefault(reviewer, []).append(author)
    return assignments
```

- [ ] **Step 6: 固定 prompt 与覆盖约束的消费契约**

`_review_tasks()` 给 reviewer 的 prompt 只含所分配作者的候选，并把内部 key 映射为该
reviewer 局部的 `candidate-1..H`。Prompt JSON 不含作者 id、reviewer id 或 `idea-`
内部 key。Task 6 对 primary review 只调用一次 `_review_assignment()`；失败后只调用一次
`_review_retry_assignment()`，不得从 retry 结果再次产生 retry。

- [ ] **Step 7: 运行有界 review 测试**

Run:

```bash
uv run pytest tests/test_ideator.py -k "review_assignment or review_retry_assignment" -q
```

Expected: PASS.

- [ ] **Step 8: 提交 Task 5**

```bash
git add src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "feat(ideator): bound anonymous peer review"
```

---

### Task 6: Ideator 全流程迁移、唯一裁决与 Audit

**Files:**
- Modify: `src/athena/ideator/ideator.py`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: `AgentTeam`、Task 3 私有 DTO、Task 4 Runner/spec、Task 5 review helpers。
- Produces: 新 `Ideator(control, parent, agent_factory, artifacts, config)` 与完全不依赖 Thread runtime 的 `generate()`。

- [ ] **Step 1: 将测试 fixture 改为共享 Kernel 与父 Handle，并先保持红灯**

```python
class _ParentCodec:
    def encode_request(self, value: str) -> str:
        return value

    def decode_request(self, ref: str) -> str:
        return ref

    def encode_response(self, value: str) -> str:
        return value

    def decode_response(self, ref: str) -> str:
        return ref


class _ParentRunner:
    async def run(self, request: str, *, session, emit) -> str:
        return request


@pytest_asyncio.fixture
async def kernel_parent():
    kernel = AgentKernel()
    await kernel.start()
    control = AgentControl(kernel)
    parent, parent_run = await control.create_root(
        AgentSpec(
            runner=_ParentRunner(),
            codec=_ParentCodec(),
            role="research",
        ),
        "workflow:test-ideator",
        name="research",
    )
    await parent_run.wait()
    yield control, parent
    await control.aclose()


async def _generate_successfully(
    tmp_path,
    kernel_parent,
    *,
    debater_count: int = 3,
    blocked: bool = False,
    duplicate_revision_keys: bool = False,
    invalid_review_agent: int | None = None,
    invalid_review_mode: str | None = None,
    invalid_judge_mode: str | None = None,
    quorum: int = 2,
    stage_timeout_seconds: float = 120.0,
    stage_failures: dict[tuple[str, int, str], BaseException] | None = None,
    stage_blocks: dict[tuple[str, int, str], StageBlock] | None = None,
):
    control, parent = kernel_parent
    barrier = ProposalBarrier(debater_count, blocked=blocked)
    factory = DebateAgentFactory(
        debater_count,
        barrier,
        duplicate_revision_keys=duplicate_revision_keys,
        invalid_review_agent=invalid_review_agent,
        invalid_review_mode=invalid_review_mode,
        invalid_judge_mode=invalid_judge_mode,
        stage_failures=stage_failures,
        stage_blocks=stage_blocks,
    )
    artifacts = RecordingArtifactStore(tmp_path)
    ideator = Ideator(
        control=control,
        parent=parent,
        agent_factory=factory,
        artifacts=artifacts,
        config=IdeatorConfig(
            debater_count=debater_count,
            quorum=quorum,
            stage_timeout_seconds=stage_timeout_seconds,
            max_hypotheses=4,
        ),
    )
    profile, papers, models, tree, expected_context = _debate_inputs()
    task = asyncio.create_task(ideator.generate(profile, papers, models, tree))
    return task, barrier, factory, artifacts, tree, expected_context
```

Change `DebateAgent._draft()` to derive its topic from `(agent_index, ordinal)` rather than indexing a three-row tuple so the five-member bounded-review test is valid.

Run: `uv run pytest tests/test_ideator.py::test_generate_runs_proposals_concurrently_and_reuses_agents -q`

Expected: FAIL because the current constructor does not accept `control` or `parent` and still creates a manager.

- [ ] **Step 2: 改造构造与请求 artifact 边界**

```python
class Ideator:
    def __init__(
        self,
        *,
        control: AgentControl,
        parent: AgentHandle,
        agent_factory,
        artifacts: ArtifactStore,
        config: IdeatorConfig | None = None,
    ) -> None:
        self._control = control
        self._parent = parent
        self._agent_factory = agent_factory
        self._artifacts = artifacts
        self._config = config or IdeatorConfig()

    async def _request_ref(
        self, stage: _Stage, role: _Role, agent_index: int, prompt: str
    ) -> ArtifactRef:
        request = _TurnRequest(
            stage=stage,
            role=role,
            agent_index=agent_index,
            prompt=prompt,
        )
        return await self._artifacts.put_text(request.model_dump_json())
```

Create exactly `debater_count` debater specs and one arbiter spec before constructing the Team. Use `name=f"ideator-{new_id('debate')}"` so repeated rounds under one parent cannot collide.

- [ ] **Step 3: 以 Team 改写 proposal、review 与 revision**

Core stage flow:

```python
team = AgentTeam(
    control=self._control,
    parent=self._parent,
    member_specs=[self._spec("debater", index) for index in range(count)],
    arbiter_spec=self._spec("judge", 0),
    quorum=self._config.quorum,
    name=f"ideator-{new_id('debate')}",
)
try:
    proposal_raw = await team.run_members(proposal_tasks, timeout=timeout)
    proposals = await self._collect_stage(
        "proposal", proposal_raw, _ProposalBatch, transcript, failures
    )
    self._require_quorum(proposals)

    review_raw = await team.run_members(review_tasks, timeout=timeout)
    reviews, revision_authors = await self._collect_reviews_with_one_retry(
        team, review_raw, proposals, context_json, transcript, failures
    )
    self._require_quorum(revision_authors)

    revision_raw = await team.run_members(revision_tasks, timeout=timeout)
    revisions = await self._collect_stage(
        "revision", revision_raw, _RevisionBatch, transcript, failures
    )
    self._require_quorum(revisions)
finally:
    await team.aclose()
```

The actual `finally` wraps the entire judge/audit flow; the snippet places it after the member stages only to show ordering. `_collect_stage()` reads each successful ArtifactRef from ArtifactStore, validates the stage schema, records `_DebateFailure` on read/schema/runner errors, and re-raises `asyncio.CancelledError`.

Implement `_collect_reviews_with_one_retry()` with one explicit primary pass and one explicit retry pass:

```python
async def _collect_reviews_with_one_retry(
    self,
    team: AgentTeam,
    primary_raw: dict[int, object | BaseException],
    proposals: dict[int, list[dict[str, Any]]],
    context_json: str,
    transcript: list[_TranscriptEntry],
    failures: list[_DebateFailure],
) -> tuple[dict[int, list[dict[str, Any]]], list[int]]:
    assignment = self._review_assignment(sorted(proposals))
    primary_aliases = self._review_aliases(assignment, proposals)
    primary_batches = await self._collect_stage(
        "review", primary_raw, _ReviewBatch, transcript, failures
    )
    reviews, covered_authors, successful_reviewers = self._validate_reviews(
        primary_batches, assignment, primary_aliases, failures
    )

    primary_reviewers = {
        author: reviewer for reviewer, author in assignment.items()
    }
    uncovered = sorted(set(proposals) - covered_authors)
    retry_assignment = self._review_retry_assignment(
        uncovered_authors=uncovered,
        successful_reviewers=sorted(successful_reviewers),
        primary_reviewers=primary_reviewers,
    )
    if retry_assignment:
        retry_tasks, retry_aliases = await self._review_task_refs(
            retry_assignment, proposals, context_json
        )
        retry_raw = await team.run_members(
            retry_tasks, timeout=self._config.stage_timeout_seconds
        )
        retry_batches = await self._collect_stage(
            "review", retry_raw, _ReviewBatch, transcript, failures
        )
        retry_reviews, retry_covered, _ = self._validate_retry_reviews(
            retry_batches, retry_assignment, retry_aliases, failures
        )
        for reviewer, critiques in retry_reviews.items():
            reviews.setdefault(reviewer, []).extend(critiques)
        covered_authors.update(retry_covered)

    revision_authors = sorted(successful_reviewers & covered_authors)
    return reviews, revision_authors
```

`_validate_reviews()` and `_validate_retry_reviews()` both require every offered local alias exactly once, translate aliases back to internal keys only after validation, and omit invalid batches while adding one sanitized failure record. `_validate_retry_reviews()` returns directly; it never schedules more work.

- [ ] **Step 4: 写线性匿名审查与一次重派的端到端失败测试**

```python
@pytest.mark.asyncio
async def test_generate_reviews_each_candidate_once_without_author_identity(
    tmp_path, kernel_parent
) -> None:
    task, _, factory, _, _, _ = await _generate_successfully(
        tmp_path,
        kernel_parent,
        debater_count=5,
    )

    result = await task
    review_prompts = [
        prompt
        for agent in factory.agents
        for prompt, output_type in agent.calls
        if output_type is _ReviewBatch
    ]
    candidate_counts = [
        len(json.loads(prompt.split("Anonymous candidates:\n\n", 1)[1]))
        for prompt in review_prompts
    ]
    assert sum(candidate_counts) == 15
    assert candidate_counts == [3, 3, 3, 3, 3]
    assert all("debater-" not in prompt for prompt in review_prompts)
    assert all("idea-" not in prompt for prompt in review_prompts)
    assert result.failures == []


@pytest.mark.asyncio
async def test_failed_reviewer_is_reassigned_only_once(
    tmp_path, kernel_parent
) -> None:
    task, _, factory, _, _, _ = await _generate_successfully(
        tmp_path,
        kernel_parent,
        stage_failures={
            ("debater", 1, "review"): RuntimeError("review failed")
        },
    )

    result = await task
    reviewed_candidates = sum(
        len(json.loads(prompt.split("Anonymous candidates:\n\n", 1)[1]))
        for agent in factory.agents
        for prompt, output_type in agent.calls
        if output_type is _ReviewBatch
    )
    assert reviewed_candidates == 12
    assert reviewed_candidates <= 2 * 3 * 3
    assert result.failures == [
        {"agent": "debater-1", "stage": "review", "error": "RuntimeError"}
    ]
```

Run: `uv run pytest tests/test_ideator.py -k "reviews_each or reassigned_only_once" -q`

Expected: FAIL until `generate()` uses Task 5 assignments and performs at most one retry batch.

- [ ] **Step 5: 写低于 quorum 不创建 arbiter、达到 quorum 只调用一次的失败测试**

```python
@pytest.mark.asyncio
async def test_quorum_controls_the_single_arbiter(tmp_path, kernel_parent) -> None:
    task, _, factory, _, _, _ = await _generate_successfully(
        tmp_path,
        kernel_parent,
        stage_failures={
            ("debater", 1, "review"): RuntimeError("failed"),
            ("debater", 2, "review"): RuntimeError("failed"),
        },
    )

    with pytest.raises(RuntimeError, match="ideator quorum not met"):
        await task
    assert factory.judge_calls == 0
    assert all(agent.role != "judge" for agent in factory.agents)


@pytest.mark.asyncio
async def test_success_invokes_exactly_one_arbiter_once(
    tmp_path, kernel_parent
) -> None:
    task, _, factory, _, _, _ = await _generate_successfully(
        tmp_path, kernel_parent
    )

    await task

    judges = [agent for agent in factory.agents if agent.role == "judge"]
    assert len(judges) == 1
    assert factory.judge_calls == 1
```

- [ ] **Step 6: 实现唯一 judge、lineage 门禁与版本化 audit**

```python
judge_ref = await self._request_ref(
    "judge", "judge", 0, self._judge_prompt(context_json, debate_payload)
)
judge_error: str | None = None
try:
    judge_result_ref = await team.arbitrate(judge_ref, timeout=timeout)
    judge_output = _JudgeOutput.model_validate_json(
        await self._artifacts.get_text(judge_result_ref)
    )
    hypotheses = self._validated_hypotheses(judge_output, parent_id)
except asyncio.CancelledError:
    raise
except Exception as exc:
    logger.warning("ideator judge failed: %s", type(exc).__name__)
    judge_error = type(exc).__name__
if judge_error is not None:
    raise RuntimeError("ideator judge failed")

audit = _DebateAudit(
    context=frozen_context,
    hypotheses=hypotheses,
    transcript=transcript,
    failures=failures,
    judge_decisions=judge_output.decisions,
    hypothesis_lineage=lineage,
)
audit_ref = await self._artifacts.put_text(audit.model_dump_json())
```

Preserve the current exact candidate coverage rules: every surviving revision key appears in exactly one judge decision; rejected keys appear in no final lineage; selected/merged keys appear exactly once across final hypotheses; unknown and duplicate keys fail.

- [ ] **Step 7: 写外层取消只关闭 Team 的失败测试**

```python
@pytest.mark.asyncio
async def test_cancel_generate_closes_owned_agents_but_preserves_shared_control(
    tmp_path,
    kernel_parent,
) -> None:
    block = StageBlock()
    generation, _, _, _, _, _ = await _generate_successfully(
        tmp_path,
        kernel_parent,
        stage_blocks={
            ("debater", 0, "proposal"): block,
            ("debater", 1, "proposal"): block,
            ("debater", 2, "proposal"): block,
        },
    )
    control, parent = kernel_parent
    await block.started.wait()

    generation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await generation

    assert parent.status is AgentStatus.IDLE
    assert all(
        item.status is AgentStatus.CLOSED
        for item in control.list_agents()
        if item.parent_id == parent.agent_id
    )
    handle, run = await control.spawn(
        parent,
        AgentSpec(
            runner=_ParentRunner(), codec=_ParentCodec(), role="research"
        ),
        "usable",
        name="usable",
    )
    assert await run.wait() == "usable"
    await control.close(handle)
```

- [ ] **Step 8: 保证清理异常不掩盖原始失败**

Wrap the complete member/judge/audit flow as follows:

```python
try:
    result = await self._run_debate(team, context_json, parent_id)
except BaseException:
    try:
        await team.aclose()
    except BaseException as cleanup_error:
        logger.warning("ideator team cleanup failed: %s", type(cleanup_error).__name__)
    raise
else:
    await team.aclose()
    return result
```

The cleanup warning exposes only the exception type. Raising occurs outside the cleanup `except`, so the public business error retains its original traceback and is not replaced by cleanup failure.

- [ ] **Step 9: 删除旧 Runtime 代码与测试耦合**

Delete from production:

```text
from athena.app_server.thread_manager import RuntimeThreadManager
from athena.core.agent import AgentOutcome
class _DebateRunner
Ideator._invoke(manager, runner, thread_id, ...)
manager.start/submit/wait_turn/interrupt/aclose calls
```

Delete or rewrite tests importing `_DebateRunner`, `RuntimeThreadManager` or `AthenaTurn`, the `RecordingRuntimeThreadManager` fixture, manager error-string race tests, and private `_histories` assertions. Keep their domain intent through Agent/Run tests: concurrency, continuous memory, exact Run timeout, cancellation, quorum, single judge, lineage and audit.

- [ ] **Step 10: 运行 Ideator、Team 与 Kernel 测试**

Run:

```bash
uv run pytest test/unit/agent_kernel tests/test_ideator.py -q
```

Expected: PASS, and no test imports old Thread runtime.

- [ ] **Step 11: 静态确认 Ideator 已单轨**

Run:

```bash
rg -n "RuntimeThreadManager|AthenaTurn|AgentOutcome|_DebateRunner|run_with_context" src/athena/ideator tests/test_ideator.py
```

Expected: no matches.

- [ ] **Step 12: 提交 Task 6**

```bash
git add src/athena/ideator/ideator.py tests/test_ideator.py
git commit -m "refactor(ideator): migrate debate to shared agent kernel"
```

---

### Task 7: SearchLoop 并发注入论文与模型

**Files:**
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `tests/test_search_workflow.py`

**Interfaces:**
- Consumes: `PaperSearch.search(query)`、`PaperSearch.search_models(query)`、`generate_hypotheses(..., papers, models, ideator)`。
- Produces: 可注入 `reference_search` 与同查询并发检索的 `_pending_hypotheses()`。

- [ ] **Step 1: 写论文与模型均被原样注入且并发的失败测试**

```python
class BarrierReferenceSearch:
    def __init__(self) -> None:
        self.paper_queries: list[str] = []
        self.model_queries: list[str] = []
        self.papers = [object()]
        self.models = [object()]
        self._entered = 0
        self._active = 0
        self.max_active = 0
        self._release = asyncio.Event()

    async def _barrier(self) -> None:
        self._entered += 1
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        if self._entered == 2:
            self._release.set()
        try:
            await self._release.wait()
        finally:
            self._active -= 1

    async def search(self, query: str):
        self.paper_queries.append(query)
        await self._barrier()
        return self.papers

    async def search_models(self, query: str):
        self.model_queries.append(query)
        await self._barrier()
        return self.models


@pytest.mark.asyncio
async def test_search_retrieves_papers_and_models_concurrently(tmp_path) -> None:
    search = BarrierReferenceSearch()
    ideator = RecordingIdeator()
    tree = ResearchTree()
    loop = SearchLoop(
        tree,
        _Workspace(),
        EvalSpec(
            primary=MetricDef(
                name="accuracy", direction="maximize", description="Accuracy"
            )
        ),
        BudgetSnapshot(remaining=1),
        data_profile=TEST_PROFILE,
        ideator=ideator,
        reference_search=search,
    )

    pending = await loop._pending_hypotheses()

    assert search.max_active == 2
    assert search.paper_queries == ["machine learning accuracy"]
    assert search.model_queries == ["machine learning accuracy"]
    _, papers, models, received_tree = ideator.calls[0]
    assert papers is search.papers
    assert models is search.models
    assert received_tree is tree
    assert pending
```

- [ ] **Step 2: 运行测试并确认构造参数或模型输入失败**

Run: `uv run pytest tests/test_search_workflow.py::test_search_retrieves_papers_and_models_concurrently -q`

Expected: FAIL because `reference_search` is not accepted or Ideator receives `models=[]`.

- [ ] **Step 3: 实现依赖注入与并发检索**

```python
def __init__(
    self,
    tree: ResearchTree,
    workspace: GitWorkspace,
    eval_spec: EvalSpec,
    budget: BudgetSnapshot,
    mode: RunMode = RunMode(),
    *,
    data_profile: DataProfile,
    ideator: Ideator | None = None,
    reference_search: PaperSearch | None = None,
    ranker: HypothesisRanker | None = None,
    proximity: ProximityGraph | None = None,
    supervisor: Supervisor | None = None,
    comparator: Comparator | None = None,
    code_agent: CodeAgent | None = None,
    before_iteration: Callable[[], Awaitable[None]] | None = None,
) -> None:
    self._reference_search = reference_search or PaperSearch()
```

In `_pending_hypotheses()`:

```python
query = "machine learning " + self._eval_spec.primary.name
papers, models = await asyncio.gather(
    self._reference_search.search(query),
    self._reference_search.search_models(query),
)
await generate_hypotheses(
    data_profile=self._data_profile,
    papers=papers,
    models=models,
    tree=self._tree,
    ideator=self._ideator,
)
```

Do not use `return_exceptions=True`: a retrieval exception must remain distinguishable from a legitimate empty result.

- [ ] **Step 4: 保留已有 pending 与缺 Ideator 的短路测试**

```python
@pytest.mark.asyncio
async def test_pending_hypotheses_skip_both_reference_searches(tmp_path) -> None:
    search = BarrierReferenceSearch()
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="hyp-pending",
            statement="Pending",
            intervention="Change one setting",
            expected_effect="Increase accuracy",
        )
    )
    loop = SearchLoop(
        tree,
        _Workspace(),
        EvalSpec(
            primary=MetricDef(
                name="accuracy", direction="maximize", description="Accuracy"
            )
        ),
        BudgetSnapshot(remaining=1),
        data_profile=TEST_PROFILE,
        ideator=RecordingIdeator(),
        reference_search=search,
    )

    await loop._pending_hypotheses()

    assert search.paper_queries == []
    assert search.model_queries == []
```

The existing “SEARCH requires an Ideator” test must continue to assert that failure occurs before either retrieval call.

- [ ] **Step 5: 运行 Search 与 Ideator 集成测试**

Run:

```bash
uv run pytest tests/test_search_workflow.py tests/test_ideator.py -q
```

Expected: PASS.

- [ ] **Step 6: 提交 Task 7**

```bash
git add src/athena/workflows/search/search_loop.py tests/test_search_workflow.py
git commit -m "feat(search): inject paper and model evidence into ideator"
```

---

### Task 8: Composition Root 示例、文档与全量退旧门禁

**Files:**
- Modify: `examples/trial_run.py`
- Modify: `examples/ai4ml_pipeline.py`
- Modify: `docs/architecture/current.md`
- Modify: `docs/architecture/target.md`
- Modify: `tests/test_ideator.py`

**Interfaces:**
- Consumes: 新 `Ideator(control, parent, agent_factory, artifacts, config)` 与共享 `AgentControl`。
- Produces: 可运行示例、准确架构文档及零旧 runtime 引用门禁。

- [ ] **Step 1: 写静态迁移测试**

```python
def test_ideator_source_has_no_legacy_runtime_references() -> None:
    source = Path("src/athena/ideator/ideator.py").read_text(encoding="utf-8")
    forbidden = {
        "RuntimeThreadManager",
        "AthenaTurn",
        "AgentOutcome",
        "_DebateRunner",
        "run_with_context",
    }

    assert all(symbol not in source for symbol in forbidden)
```

- [ ] **Step 2: 运行静态测试并确认示例仍使用旧构造参数**

Run:

```bash
uv run pytest tests/test_ideator.py::test_ideator_source_has_no_legacy_runtime_references -q
rg -n "Ideator\(" examples src tests
```

Expected: the static production check passes after Task 6; the search output identifies examples that still lack `control` and `parent`.

- [ ] **Step 3: 更新两个示例的 composition root**

Each example must perform the same explicit lifecycle:

```python
class _ExampleArtifactRefCodec:
    def encode_request(self, value: ArtifactRef) -> ArtifactRef:
        return value

    def decode_request(self, ref: ArtifactRef) -> ArtifactRef:
        return ref

    def encode_response(self, value: ArtifactRef) -> ArtifactRef:
        return value

    def decode_response(self, ref: ArtifactRef) -> ArtifactRef:
        return ref


class _ExampleParentRunner:
    async def run(self, request: ArtifactRef, *, session, emit) -> ArtifactRef:
        return request


kernel = AgentKernel()
await kernel.start()
control = AgentControl(kernel)
parent_ref = await artifacts.put_text('{"workflow":"research"}')
parent, parent_run = await control.create_root(
    AgentSpec(
        runner=_ExampleParentRunner(),
        codec=_ExampleArtifactRefCodec(),
        role="research",
    ),
    parent_ref,
    name="research",
)
await parent_run.wait()
ideator = Ideator(
    control=control,
    parent=parent,
    agent_factory=lambda role, index: PydanticAgent(model),
    artifacts=artifacts,
)
try:
    hypotheses = await generate_hypotheses(profile, papers, models, tree, ideator=ideator)
finally:
    await control.aclose()
```

`_ExampleParentRunner.run()` returns the ArtifactRef request unchanged. `_ExampleArtifactRefCodec` implements the four identity methods exactly as Task 4's private codec; examples define these two small helpers locally because the Ideator-private codec must not become public merely for demo setup.

- [ ] **Step 4: 更新架构文档事实**

In `docs/architecture/current.md`, replace the statement that Ideator owns independent Thread lifecycle with:

```text
IdeaGeneration 由 athena.ideator.Ideator 唯一负责。Ideator 通过共享 AgentControl 与薄 AgentTeam
并发运行可配置辩者、一次有界匿名互评、修案和唯一裁决；Agent 生命周期、memory、调度与
关闭由统一 AgentKernel 所有，Ideator 一轮结束只关闭自有 Team。
```

In `docs/architecture/target.md`, mark the Ideator migration bullet complete and retain app-server migration as a separate uncompleted ownership move. Do not claim the entire repository has deleted `RuntimeThreadManager` while app-server still uses it.

- [ ] **Step 5: 运行示例语法与静态扫描**

Run:

```bash
uv run python -m py_compile examples/trial_run.py examples/ai4ml_pipeline.py
rg -n "RuntimeThreadManager|AthenaTurn|AgentOutcome|_DebateRunner|run_with_context" src/athena/ideator tests/test_ideator.py
```

Expected: compile succeeds; `rg` has no matches.

- [ ] **Step 6: 运行定向测试、格式与 diff 门禁**

Run:

```bash
uv run pytest test/unit/agent_kernel tests/test_ideator.py tests/test_search_workflow.py -q
uv run black --check src/athena/core/agent_kernel src/athena/ideator src/athena/workflows/search/search_loop.py test/unit/agent_kernel tests/test_ideator.py tests/test_search_workflow.py examples/trial_run.py examples/ai4ml_pipeline.py
git diff --check
```

Expected: all tests PASS, Black reports unchanged files, and `git diff --check` exits 0.

- [ ] **Step 7: 提交 Task 8**

```bash
git add examples/trial_run.py examples/ai4ml_pipeline.py docs/architecture/current.md docs/architecture/target.md tests/test_ideator.py
git commit -m "docs: complete ideator kernel migration"
```

---

## Final Verification

- [ ] **Step 1: 运行完整 Python 测试**

Run:

```bash
uv run pytest -q
```

Expected: all tests and subtests PASS with no unhandled task warnings.

- [ ] **Step 2: 运行全相关 Black 检查**

Run:

```bash
uv run black --check src test tests examples
```

Expected: Black exits 0.

- [ ] **Step 3: 检查旧引用、diff 与工作树**

Run:

```bash
rg -n "RuntimeThreadManager|AthenaTurn|AgentOutcome|_DebateRunner|run_with_context" src/athena/ideator tests/test_ideator.py
git diff --check
git status --short
```

Expected: `rg` has no matches, `git diff --check` exits 0, and only deliberate implementation files are present before the final commit or review handoff.

- [ ] **Step 4: 审核提交边界**

Run:

```bash
git log --oneline --decorate -10
git diff HEAD~8 --stat
```

Expected: each task is represented by a focused commit; no app-server、ranking、budget、Supervisor、Evaluator or ResearchTree schema changes appear.
