# LLM Task-Understanding Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make configured GUI clarification genuinely LLM-driven and show only validated, session-scoped public progress in the existing task-understanding activity region.

**Architecture:** A short-lived clarification `Agent` reuses Athena's provider loop, one narrow reporting tool, structured output, and artifact store. The clarification controller remains the transaction boundary: tool progress is live-only, while the final public update is emitted through the existing `output` path only after the matching draft transition is saved. Generic `session_id/scope/scope_id` metadata lets the current frontend reducer isolate and group output without a new event channel or renderer.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, Athena `Agent`/`BaseProvider`/`ToolRegistry`, pytest/pytest-asyncio, React 18, TypeScript, Vitest, Tauri/Rust.

## Global Constraints

- Before every implementation task, read `codex_docs/CURRENT.md`, this plan, and `docs/superpowers/specs/2026-09-03-llm-task-understanding-output-design.md`; update the current task's checkboxes as evidence is produced.
- Preserve unrelated worktree changes. Stage and commit only the paths named by the current task.
- A configured model must use one real `ResponsesProvider` instance for clarification and Supervisor registration; an in-process provider failure must never fall back to deterministic clarification.
- A runtime with no model/provider retains `DeterministicClarificationGenerator` and the existing schema-version-1 draft format.
- Reuse the existing `ClarificationQuestionStep`, `ClarificationFinalStep`, controller question cap, persistence transitions, revision behavior, and confirmation gate.
- Model-authored public summaries are NFKC-normalized, stripped of `Cc`/`Cf`/`Cs` characters, whitespace-collapsed, non-empty, at most 600 characters, and passed through the existing output redactor.
- Never publish raw model text deltas, structured JSON, prompts, function-call arguments, tool acknowledgements, exception text, provider-private reasoning, or hidden chain-of-thought.
- Progress-tool output uses the existing `output` subscription with `persist=False`; final/failure output uses `persist=True` only after the canonical draft save succeeds.
- Scoped output metadata is atomic: all of `session_id`, `scope`, and `scope_id` are non-empty, or all are absent/null for a legacy event. Partial metadata is invalid.
- The feature adds no task-specific event type, WebSocket channel, transcript table, progress ledger, persistent clarification Agent, provider protocol field, or new frontend renderer.
- Normal tests use scripted fake providers and make no external network calls.
- Post-implementation verification must distinguish pre-existing warnings from warnings introduced in changed files.
- After merge to `main`, remove Athena-generated test/build caches requested by the user; do not delete source, configuration, dependency manifests, or user data.

## File Map

- Modify `src/athena/research/clarification/generator.py`: strict public-update models, sink protocol, safe publication helper, and backward-compatible generator result normalization.
- Create `src/athena/research/clarification/llm_generator.py`: bounded canonical prompt, short-lived Agent, strict reporting tool, raw-event suppression, and result artifact readback.
- Modify `src/athena/research/clarification/controller.py`: save-before-publish ordering and fixed failure notice.
- Modify `src/athena/research/supervisor/events.py`: generic atomic scope metadata on `OutputEvent` and `EventProjector.output`.
- Modify `src/athena/research/runtime/events.py`: forward scope metadata through the existing live/transcript path.
- Modify `src/athena/research/runtime/bootstrap.py`: adapt `PublicProgressSink` to `RuntimeEvents` and select the injected generator.
- Modify `src/athena/research/runtime/facade.py`: construct one provider, pass it into services, and expose the complete generic output passthrough.
- Modify focused Python tests under `test/unit/research/clarification/`, `test/unit/research/supervisor/test_events.py`, and `test/integration/test_tui_resume.py`.
- Modify `tests/test_gui_gateway_transport.py`: prove generic scoped output survives WebSocket serialization unchanged.
- Modify `athena-gui/src/types/ui.ts`: add one canonical optional scope projection to `UIMessage`.
- Modify `athena-gui/src/lib/tauri-bridge.ts`: document the optional scope fields on replay records.
- Modify `athena-gui/src/hooks/usePipeline.ts`: parse atomic metadata once, filter live/replay output by session, and retain it on messages.
- Modify `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx` and `usePipeline.identity.test.tsx`: reducer, batching, replay, and stale-session coverage.
- Modify `athena-gui/src/components/conversation/MessageList.tsx`: scope-aware task-understanding grouping with legacy fallback.
- Modify `athena-gui/src/components/__tests__/conversation-pane.test.tsx`: DOM-level activity/tool rendering and mismatch coverage.
- Modify `docs/athena-guide/13-task-clarification-conversation.md`: document public LLM activity, event identity, and replay limits.
- Modify `docs/superpowers/specs/2026-09-03-llm-task-understanding-output-design.md`, create the completion report, update `codex_docs/CURRENT.md`, and delete this plan only during verified closeout.

## Execution Preflight

- [ ] Record `git status --short --branch`, `git diff --cached --name-status`, and `git log -1 --oneline` in this isolated worktree. Do not touch the dirty `main` worktree.
- [ ] Install the worktree's frontend dependencies with `npm --prefix athena-gui ci`; this is required because cache cleanup removed worktree-local dependencies.
- [ ] Run the backend baseline:

```powershell
uv run pytest -q `
  test/unit/research/clarification `
  test/unit/research/supervisor/test_events.py `
  test/integration/test_tui_resume.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py
```

Expected baseline: no failure. Record pass/skip/warning counts; the independent planning review observed `116 passed` plus one environment-owned pytest-cache warning.

- [ ] Run the frontend baseline:

```powershell
npm --prefix athena-gui test -- `
  src/hooks/__tests__/usePipeline.events.test.tsx `
  src/hooks/__tests__/usePipeline.identity.test.tsx `
  src/hooks/__tests__/usePipeline.test.tsx `
  src/components/__tests__/conversation-pane.test.tsx `
  src/lib/__tests__/tauri-bridge.test.ts
```

Expected baseline: no failure. Record pass counts; the independent planning review observed `62 passed` in its focused slice.

---

### Task 1: Strict clarification public-result contract

**Files:**
- Modify: `src/athena/research/clarification/generator.py`
- Modify: `test/unit/research/clarification/test_generator.py`

**Interfaces:**
- Produces: `ProgressStage = Literal["analysis", "question", "synthesis"]` and `ProgressSinkStage = ProgressStage | Literal["failure"]`.
- Produces: `PublicProgress(stage: ProgressStage, summary: str)` and `ClarificationModelOutput(public_update: PublicProgress, step: ClarificationStep)`.
- Produces: `PublicProgressSink.__call__(*, summary, stage, session_id, scope_id, source, persist) -> Awaitable[None]`.
- Produces: `ClarificationTurnResult(step, public_update=None)`, `generate_turn(generator, draft)`, and the existing compatible `generate_step(generator, draft)`.
- Produces: `publish_public_progress(...)`, which propagates cancellation, logs ordinary sink failures, and otherwise leaves canonical state unchanged.

- [ ] **Step 1: Write failing normalization and strict-schema tests**

Add concrete tests using the existing `new_draft` fixture pattern:

```python
def test_public_progress_normalizes_only_public_text() -> None:
    value = PublicProgress(stage="analysis", summary="  Ａ\u200b\x00   metric\nselected  ")
    assert value.summary == "A metric selected"


@pytest.mark.parametrize("summary", ["\u200b\x00", "x" * 601])
def test_public_progress_rejects_empty_or_over_limit(summary: str) -> None:
    with pytest.raises(ValidationError):
        PublicProgress(stage="analysis", summary=summary)


def test_public_progress_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PublicProgress.model_validate(
            {"stage": "analysis", "summary": "safe", "reasoning": "private"}
        )
```

Add a strict envelope case for both existing step variants and rejection of an unknown `kind`.

- [ ] **Step 2: Write failing compatibility and sink-isolation tests**

Prove that `generate_turn` wraps the deterministic raw step with `public_update is None`, accepts a `ClarificationModelOutput`, and that `generate_step` still returns the raw step. Add an async sink that raises `RuntimeError("display down")`; assert `publish_public_progress` returns normally and never includes that error in another callback. Add a sink that raises `asyncio.CancelledError` and assert cancellation propagates.

- [ ] **Step 3: Run the focused tests and confirm the contract is missing**

Run:

```powershell
uv run pytest -q test/unit/research/clarification/test_generator.py
```

Expected: FAIL during import or assertion because the public models and `generate_turn` do not exist.

- [ ] **Step 4: Implement the minimal strict models and result adapter**

Use one normalization function for model/tool public text:

```python
def normalize_public_summary(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("public summary must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    printable = "".join(
        " " if char.isspace() else char
        for char in normalized
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(printable.split())
```

Attach it with `@field_validator("summary", mode="before")`; let Pydantic enforce `min_length=1` and `max_length=600` afterward. Configure both new models with `ConfigDict(extra="forbid", frozen=True, strict=True)`.

Use this exact sink boundary:

```python
ProgressSource = Literal["agent", "tool"]

class PublicProgressSink(Protocol):
    async def __call__(
        self,
        *,
        summary: str,
        stage: ProgressSinkStage,
        session_id: str,
        scope_id: str,
        source: ProgressSource,
        persist: bool,
    ) -> None: ...
```

`generate_turn` awaits callables exactly as the current helper does, validates dictionary envelopes with `ClarificationModelOutput.model_validate`, and delegates raw dictionaries with `kind="question"/"final"` to the existing step models. `generate_step` becomes `return (await generate_turn(generator, draft)).step`.

- [ ] **Step 5: Run, format, and commit Task 1**

Run:

```powershell
uv run pytest -q test/unit/research/clarification/test_generator.py
uv run black src/athena/research/clarification/generator.py test/unit/research/clarification/test_generator.py
git diff --check -- src/athena/research/clarification/generator.py test/unit/research/clarification/test_generator.py
```

Expected: focused tests pass and diff checks are clean. Stage only these two files and commit with `git commit -m "feat: define clarification public output contract"`.

---

### Task 2: Generic scoped output event contract

**Files:**
- Modify: `src/athena/research/supervisor/events.py`
- Modify: `src/athena/research/runtime/events.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `test/unit/research/supervisor/test_events.py`
- Modify: `test/integration/test_tui_resume.py`

**Interfaces:**
- Changes: `OutputEvent` gains optional `session_id`, `scope`, and `scope_id` fields.
- Changes: `EventProjector.output`, `RuntimeEvents.publish_output`, and `ResearchRuntime.publish_output` accept and forward the three fields plus the existing `persist` and `message_id` controls.
- Preserves: all legacy output callers and transcript rows with all three fields absent/null.

- [ ] **Step 1: Write failing atomic-metadata projection tests**

Add these cases to `test_events.py`:

```python
def test_output_scope_metadata_is_atomic_and_redacted(tmp_path) -> None:
    projector = EventProjector(LocalArtifactStore(tmp_path / "artifacts"))
    event = projector.output(
        source="agent",
        channel="text",
        text="api_key=secret-value",
        session_id="session-1",
        scope="task_understanding",
        scope_id="draft-1",
    )
    assert (event.session_id, event.scope, event.scope_id) == (
        "session-1", "task_understanding", "draft-1"
    )
    assert "secret-value" not in event.text


@pytest.mark.parametrize(
    "metadata",
    [
        {"session_id": "session-1"},
        {"scope": "task_understanding", "scope_id": "draft-1"},
        {"session_id": "session-1", "scope": "", "scope_id": "draft-1"},
    ],
)
def test_output_rejects_partial_scope_metadata(tmp_path, metadata) -> None:
    projector = EventProjector(LocalArtifactStore(tmp_path / "artifacts"))
    with pytest.raises(ValidationError):
        projector.output(source="agent", channel="text", text="safe", **metadata)
```

Keep a legacy event assertion showing all three values are `None`.

- [ ] **Step 2: Write failing live-versus-transcript tests**

In `test_tui_resume.py`, subscribe to a real provider-less runtime, publish one scoped record with `persist=False` and one with `persist=True`, then assert both arrive live but only the second is returned by `replay_output_events()` with all metadata intact and the next sequence remains monotonic after restart.

- [ ] **Step 3: Run the focused tests and confirm missing keyword failures**

Run:

```powershell
uv run pytest -q `
  test/unit/research/supervisor/test_events.py `
  test/integration/test_tui_resume.py
```

Expected: FAIL because output projection and the facade do not accept scope metadata.

- [ ] **Step 4: Implement one atomic validator and forward the fields**

Add the fields to `OutputEvent` with default `None`. Its after-validator must accept `all(value is None for value in values)` or `all(isinstance(value, str) and value.strip() for value in values)` and raise `ValueError("output scope metadata must be all present or all absent")` otherwise.

Add the same optional keyword arguments to the three output methods and pass them unchanged into `OutputEvent`. Keep `model_dump(mode="json")`, `_publish`, redaction, sequence allocation, subscriber behavior, and transcript format unchanged. The facade method must also forward `persist` and `message_id` so tests and future scoped producers do not reach into private services.

- [ ] **Step 5: Run, format, and commit Task 2**

Run:

```powershell
uv run pytest -q test/unit/research/supervisor/test_events.py test/integration/test_tui_resume.py
uv run black src/athena/research/supervisor/events.py src/athena/research/runtime/events.py src/athena/research/runtime/facade.py test/unit/research/supervisor/test_events.py test/integration/test_tui_resume.py
git diff --check -- src/athena/research/supervisor/events.py src/athena/research/runtime/events.py src/athena/research/runtime/facade.py test/unit/research/supervisor/test_events.py test/integration/test_tui_resume.py
```

Expected: both focused files pass. Commit only Task 2 paths with `git commit -m "feat: scope runtime output events"`.

---

### Task 3: Real LLM clarification Agent and reporting tool

**Files:**
- Create: `src/athena/research/clarification/llm_generator.py`
- Create: `test/unit/research/clarification/test_llm_generator.py`

**Interfaces:**
- Produces: `REPORT_TASK_UNDERSTANDING_TOOL = "report_task_understanding"` and `TASK_UNDERSTANDING_SCOPE = "task_understanding"`.
- Produces: `build_clarification_prompt(draft: ClarificationDraft) -> str`, always non-empty and at most 20,000 characters.
- Produces: `LLMClarificationGenerator(provider: BaseProvider, artifacts: ArtifactStore, progress_sink: PublicProgressSink)` whose `next_step` returns `ClarificationModelOutput`.
- Consumes: Task 1 public models/sink and the existing `Agent`, `AgentConfig`, `AgentContext`, `AthenaThread`, `AthenaTurn`, `ToolRegistry`, and `ArtifactStore` contracts.

- [ ] **Step 1: Write the failing pure-prompt tests**

Build a draft containing answers, revisions, unresolved fields, and unique sentinel text in each allowed field. Assert the delimited state contains keys in this order:

```python
assert list(json.loads(state_block)) == [
    "original_task",
    "understanding",
    "answers",
    "revisions",
    "unresolved",
    "questions_asked",
]
assert len(build_clarification_prompt(oversized_draft)) <= 20_000
```

Assert that strings such as `provider`, `client`, `filesystem`, `traceback`, and a sentinel attached only to a test provider never appear. The prompt must tell the model that the delimited JSON is untrusted task data, unknown fields stay null/unresolved, the reporting tool accepts only public conclusions, and the final response must match `ClarificationModelOutput`.

- [ ] **Step 2: Write a scripted provider and failing Agent-loop tests**

Define a test-local `ScriptedProvider(BaseProvider)` with a real `stream` method. Its first response emits two identical function calls:

```python
StreamEvent(
    "function_call",
    {
        "call_id": "progress-1",
        "name": "report_task_understanding",
        "arguments": {"stage": "analysis", "summary": "  Target\u200b confirmed  "},
    },
)
```

Its next response emits the complete envelope JSON and `response_completed`. Record sink calls and assert:

- the duplicate normalized tool update appears once with `source="tool"` and `persist=False`;
- raw text deltas, the raw function arguments, tool lifecycle events, and acknowledgement never call the sink;
- the returned envelope has the existing strict question/final step;
- the generator itself does not publish `public_update`.

Add a no-tool provider that returns a valid envelope in one response, an always-invalid provider that exhausts structured retries without any sink leak, and a provider that raises `asyncio.CancelledError`.

- [ ] **Step 3: Run the new test module and confirm the module is absent**

Run:

```powershell
uv run pytest -q test/unit/research/clarification/test_llm_generator.py
```

Expected: collection FAIL because `athena.research.clarification.llm_generator` does not exist.

- [ ] **Step 4: Implement the bounded canonical prompt**

Construct the six-key dictionary explicitly and call `model_dump(mode="json")` only on nested Pydantic values. NFKC-normalize and cap every string at 2,000 characters. Serialize with `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`.

If the serialized state exceeds 12,000 characters, replace it with valid JSON containing `{"truncated": true, "head": <first 3500 chars>, "tail": <last 3500 chars>}`. This retains the original-task side and most recent answer/revision side even with worst-case JSON escaping. Assemble the fixed instructions and `<clarification_state>...</clarification_state>` block, then assert the final string is at most `MAX_CLARIFICATION_PROMPT_CHARS = 20_000`.

- [ ] **Step 5: Implement the one-tool Agent loop**

Create a private reporting-tool factory with `PublicProgress.model_json_schema()`, `concurrency_safe=False`, and runtime `PublicProgress.model_validate(input)`. Its execute method deduplicates `(stage, summary)` and calls `publish_public_progress` with the draft's identities, `source="tool"`, and `persist=False`; it returns only `{"acknowledged": True}`.

Create `ToolRegistry()` and register only that tool. Use:

```python
agent = Agent(
    self._provider,
    tools,
    _SYSTEM_PROMPT,
    AgentConfig(max_turns=6, temperature=0.1, name="clarification-agent"),
    output_type=ClarificationModelOutput,
    artifacts=self._artifacts,
)
```

Build a fresh UUID-based thread/turn and `AgentContext` whose `emit` callback intentionally returns without projecting every event. After `Agent.run`, load `outcome.result_ref` through `ArtifactStore.get_text` and revalidate it with `ClarificationModelOutput.model_validate_json`. Do not reuse Supervisor, `single_turn_chat`, `project_agent_event`, or any broad tool registry.

- [ ] **Step 6: Run, format, and commit Task 3**

Run:

```powershell
uv run pytest -q `
  test/unit/research/clarification/test_generator.py `
  test/unit/research/clarification/test_llm_generator.py
uv run black src/athena/research/clarification/llm_generator.py test/unit/research/clarification/test_llm_generator.py
git diff --check -- src/athena/research/clarification/llm_generator.py test/unit/research/clarification/test_llm_generator.py
```

Expected: the real Agent loop runs entirely against fakes and all tests pass. Commit only the new files with `git commit -m "feat: generate LLM task clarification"`.

---

### Task 4: Controller save-before-publish transaction boundary

**Files:**
- Modify: `src/athena/research/clarification/controller.py`
- Modify: `test/unit/research/clarification/test_controller.py`

**Interfaces:**
- Changes: `ClarificationController(..., progress_sink: PublicProgressSink | None = None)`.
- Consumes: `generate_turn` and `ClarificationTurnResult` from Task 1.
- Produces: `CLARIFICATION_FAILURE_NOTICE = "Task understanding failed. Retry to continue."`.
- Preserves: controller public RPC methods and deterministic behavior.

- [ ] **Step 1: Write failing question/final ordering tests**

Use a generator that returns `ClarificationModelOutput` and a sink that loads the store at call time. For a question, assert the observed store has a matching `pending_request` before the durable `public_update`; for a final, assert the observed store is already `READY_FOR_CONFIRMATION`. In both cases assert `source="agent"`, `persist=True`, `session_id` equals the draft session, and `scope_id` equals the draft id.

Use a two-result generator: it returns the question first and a final step after the broker's one choice response. Assert ordering for the first update and final-save ordering for the second, avoiding the unrelated eight-question loop.

- [ ] **Step 2: Write failing error and cancellation tests**

Add four assertions:

```python
assert store.load().status == "FAILED"
assert published == [{
    "summary": CLARIFICATION_FAILURE_NOTICE,
    "stage": "failure",
    "source": "agent",
    "persist": True,
    "session_id": "s-1",
    "scope_id": failed.draft_id,
}]
assert "model down" not in repr(published)
assert "sk-secret-value" not in repr(published)
```

Also prove a raising sink does not change the saved READY/FAILED result, a store save failure publishes nothing, and `asyncio.CancelledError` escapes while the last saved draft remains `CLARIFYING` rather than `FAILED`.

- [ ] **Step 3: Run the controller tests and confirm ordering failures**

Run:

```powershell
uv run pytest -q test/unit/research/clarification/test_controller.py
```

Expected: FAIL because the controller currently consumes only a raw step and has no progress sink.

- [ ] **Step 4: Implement commit ordering with small private helpers**

Replace the generation call with `turn = await generate_turn(...)`. For a final, compute and save `finalize(...)` before publishing. For a question, construct and save `set_pending(...)`, publish the turn update, and only then await `broker.ask`. Do not publish when `turn.public_update is None`.

Keep provider/schema/artifact handling in the existing `except Exception` block. Save `fail(...)` first, then call `publish_public_progress` exactly once with the fixed notice and `stage="failure"`. Because `CancelledError` inherits `BaseException`, it remains outside this handler. Keep broker failure semantics unchanged.

- [ ] **Step 5: Run the complete controller/state slice and commit Task 4**

Run:

```powershell
uv run pytest -q `
  test/unit/research/clarification/test_controller.py `
  test/unit/research/clarification/test_state.py `
  test/unit/research/clarification/test_store.py
uv run black src/athena/research/clarification/controller.py test/unit/research/clarification/test_controller.py
git diff --check -- src/athena/research/clarification/controller.py test/unit/research/clarification/test_controller.py
```

Expected: all tests pass, including the existing eight-question and revision tests. Commit only Task 4 paths with `git commit -m "feat: publish committed clarification updates"`.

---

### Task 5: Single-provider composition, replay, and transport proof

**Files:**
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `test/unit/research/clarification/test_runtime_rpc.py`
- Modify: `test/integration/test_tui_resume.py`
- Modify: `tests/test_gui_gateway_transport.py`

**Interfaces:**
- Changes: `build_services(config, broker, *, provider: BaseProvider | None = None)`.
- Consumes: `LLMClarificationGenerator`, `TASK_UNDERSTANDING_SCOPE`, and `REPORT_TASK_UNDERSTANDING_TOOL` from Task 3.
- Preserves: `build_services(config, broker)` compatibility when `config.model is None`.
- Guarantees: one provider instance is shared by clarification and Supervisor registration.

- [ ] **Step 1: Write failing composition tests**

Monkeypatch `athena.research.runtime.facade.ResponsesProvider` with a scripted `BaseProvider` factory that records constructed instances. Instantiate `ResearchRuntime(model="fake", broker=StubBroker(), task_confirmation_gate=True)` and assert:

```python
assert len(created) == 1
assert runtime.supervisor_provider is created[0]
controller = runtime.services.workflow.clarification
assert isinstance(controller._generator, LLMClarificationGenerator)
assert controller._generator._provider is created[0]
```

Add a provider-less runtime assertion for `DeterministicClarificationGenerator`. Directly call `build_services` with `config.model` non-null and no provider and assert a clear composition `ValueError`, proving there is no silent fallback. Confirm a schema-version-1 draft created in provider-less mode remains readable after constructing provider-backed services; no `generation_mode` field is added.

- [ ] **Step 2: Write a failing real-runtime success/failure event test**

Use a scripted provider that calls the progress tool once and then returns a final envelope. Subscribe before `task_clarification_start` and assert:

- the tool record arrives first with `persist=False` behavior (live but absent from replay);
- the agent summary arrives after the returned draft is saved as `READY_FOR_CONFIRMATION`;
- both records carry `session_id`, `scope="task_understanding"`, and `scope_id=draft_id`;
- replay contains exactly the durable agent summary.

Use a second provider that exposes `sk-secret-value` in its error. Assert the result is retryable `FAILED`, replay contains one fixed failure notice, no secret/error detail, and no deterministic question was asked.

- [ ] **Step 3: Write the generic WebSocket pass-through contract test**

Extend the existing fake runtime in `tests/test_gui_gateway_transport.py` to emit:

```python
{
    "type": "output",
    "seq": 1,
    "message_id": "msg-1",
    "source": "agent",
    "channel": "text",
    "text": "public update",
    "session_id": "session-1",
    "scope": "task_understanding",
    "scope_id": "draft-1",
}
```

Assert the WebSocket `data` object is byte-for-byte equivalent after JSON decoding. Production transport code should require no change.

- [ ] **Step 4: Run the focused tests and confirm deterministic selection remains**

Run:

```powershell
uv run pytest -q `
  test/unit/research/clarification/test_runtime_rpc.py `
  test/integration/test_tui_resume.py `
  tests/test_gui_gateway_transport.py
```

Expected: FAIL because `build_services` does not accept/inject a provider and scoped clarification output is absent.

- [ ] **Step 5: Implement the composition adapter**

In `ResearchRuntime.__init__`, construct before `build_services`:

```python
provider = ResponsesProvider(model, client=client) if model is not None else None
services, session = build_services(config, broker, provider=provider)
```

Register that same non-null provider with the Supervisor. In `build_services`, reject `config.model is not None and provider is None`. After constructing `store` and `events`, define one sink closure that maps `stage="failure"` to `channel="error"`, all other stages to `channel="text"`, tool source to `tool=REPORT_TASK_UNDERSTANDING_TOOL`, and always supplies the three scope fields. Pass the same closure to `LLMClarificationGenerator` and `ClarificationController`. Select deterministic only when provider is `None`.

- [ ] **Step 6: Run, format, and commit Task 5**

Run:

```powershell
uv run pytest -q `
  test/unit/research/clarification `
  test/unit/research/supervisor/test_events.py `
  test/integration/test_tui_resume.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py
uv run black src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py test/unit/research/clarification/test_runtime_rpc.py test/integration/test_tui_resume.py tests/test_gui_gateway_transport.py
git diff --check -- src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py test/unit/research/clarification/test_runtime_rpc.py test/integration/test_tui_resume.py tests/test_gui_gateway_transport.py
```

Expected: the affected backend/gateway slice passes without network access. Commit only Task 5 paths with `git commit -m "feat: wire LLM task clarification runtime"`.

---

### Task 6: Frontend atomic scope ingestion and session isolation

**Files:**
- Modify: `athena-gui/src/types/ui.ts`
- Modify: `athena-gui/src/lib/tauri-bridge.ts`
- Modify: `athena-gui/src/hooks/usePipeline.ts`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx`
- Modify: `athena-gui/src/hooks/__tests__/usePipeline.identity.test.tsx`

**Interfaces:**
- Changes: `UIMessage` gains optional canonical `sessionId`, `scope`, and `scopeId` fields.
- Changes: `SessionRecord` documents optional wire fields `session_id`, `scope`, and `scope_id`.
- Produces internally: `outputScopeOf(data)` returning `{ kind: "legacy" }`, `{ kind: "invalid" }`, or `{ kind: "scoped", sessionId, scope, scopeId }`.
- Changes: `restoreRecords(records, resetMessages, sessionId)` filters scoped replay before projection.

- [ ] **Step 1: Write failing live projection/filter tests**

In `usePipeline.events.test.tsx`, deliver matching scoped agent and tool output and assert their projected messages retain exactly:

```typescript
{
  sessionId: "default",
  scope: "task_understanding",
  scopeId: "draft-1",
}
```

Deliver another-session event and each partial combination; after flushing animation frames, assert neither messages nor the visible log list changed. Deliver an all-null/absent legacy event and assert it still renders. Reuse one `message_id` for two matching scoped deltas and assert batching still produces one ordered message.

- [ ] **Step 2: Write failing replay and switch-race tests**

In `usePipeline.identity.test.tsx`, return mixed replay records for the selected session and assert only matching scoped plus legacy records restore. After a successful session switch, invoke the old runtime callback with a delayed old-session scoped output; assert it never enters messages/logs. Matching scoped replay must retain `message_id` ordering and canonical metadata.

- [ ] **Step 3: Run focused Vitest and confirm metadata/filter failures**

Run:

```powershell
npm --prefix athena-gui test -- `
  src/hooks/__tests__/usePipeline.events.test.tsx `
  src/hooks/__tests__/usePipeline.identity.test.tsx
```

Expected: FAIL because output events are currently queued without session validation and `UIMessage` drops scope metadata.

- [ ] **Step 4: Implement one exact wire parser and use it everywhere**

Use exact output wire names only; do not alias human-request `scope_kind` or a task-specific `draft_id`:

```typescript
type OutputScope =
  | { kind: "legacy" }
  | { kind: "invalid" }
  | { kind: "scoped"; sessionId: string; scope: string; scopeId: string };

function outputScopeOf(data: Record<string, unknown>): OutputScope {
  const raw = [data.session_id, data.scope, data.scope_id];
  if (raw.every((value) => value == null)) return { kind: "legacy" };
  if (raw.every((value) => typeof value === "string" && value.trim())) {
    return {
      kind: "scoped",
      sessionId: raw[0] as string,
      scope: raw[1] as string,
      scopeId: raw[2] as string,
    };
  }
  return { kind: "invalid" };
}
```

Call it before live events enter `pendingOutputEventsRef` and before replay records reach `applyOutputEventToBatch`. Drop invalid or mismatched scoped records; all-absent/null legacy records remain compatible. Pass `target` during mount hydration and `id` during explicit session switch into `restoreRecords`. When projecting a valid scoped record, copy the three canonical camelCase fields onto every new `UIMessage`; preserve them on same-id delta updates.

- [ ] **Step 5: Run, typecheck, and commit Task 6**

Run:

```powershell
npm --prefix athena-gui test -- `
  src/hooks/__tests__/usePipeline.events.test.tsx `
  src/hooks/__tests__/usePipeline.identity.test.tsx `
  src/hooks/__tests__/usePipeline.test.tsx `
  src/lib/__tests__/tauri-bridge.test.ts
npm --prefix athena-gui run build
git diff --check -- athena-gui/src/types/ui.ts athena-gui/src/lib/tauri-bridge.ts athena-gui/src/hooks/usePipeline.ts athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx athena-gui/src/hooks/__tests__/usePipeline.identity.test.tsx
```

Expected: focused frontend tests and TypeScript production build pass. Commit only Task 6 paths with `git commit -m "feat: isolate scoped frontend output"`.

---

### Task 7: Scope-aware task-understanding activity grouping

**Files:**
- Modify: `athena-gui/src/components/conversation/MessageList.tsx`
- Modify: `athena-gui/src/components/__tests__/conversation-pane.test.tsx`

**Interfaces:**
- Consumes: Task 6 `UIMessage.scope` and `UIMessage.scopeId`.
- Produces internally: `belongsToTaskUnderstanding(msg, preview) -> boolean`.
- Preserves: current ideator lanes, tool renderer, user-message boundaries, stable segment keys, and legacy positional grouping.

- [ ] **Step 1: Write the failing behavior-matrix tests**

Add DOM tests for all six cases:

1. active `CLARIFYING` preview with `draftId="draft-1"` groups matching scoped output under `任务理解过程`;
2. optimistic preview with `draftId=""` groups the first task-understanding event;
3. canonical preview plus `scopeId="draft-other"` leaves the message ordinary;
4. no active preview leaves valid scoped history visible as ordinary output and creates no activity heading;
5. legacy output with no scope retains the existing positional grouping;
6. a matching `source="tool"`, `tool="report_task_understanding"` record renders through the existing `<details>` tool presentation inside the activity.

Also retain the existing READY/RUNNING non-grouping and interleaved-user tests unchanged.

- [ ] **Step 2: Run the component test and confirm current positional over-grouping**

Run:

```powershell
npm --prefix athena-gui test -- src/components/__tests__/conversation-pane.test.tsx
```

Expected: FAIL because the current segmenter groups every non-user message after an active preview, including a mismatched scope id.

- [ ] **Step 3: Implement the minimal grouping predicate**

Return both index and preview from the active-preview lookup. Use:

```typescript
function belongsToTaskUnderstanding(
  message: UIMessage,
  preview: ClarificationPreview,
): boolean {
  if (message.scope === "task_understanding") {
    return !preview.draftId || message.scopeId === preview.draftId;
  }
  return message.sessionId === undefined
    && message.scope === undefined
    && message.scopeId === undefined;
}
```

Apply it only after the existing checks for active status, position after the preview, non-user role, and non-preview kind. Any other scoped workflow remains ordinary. Do not change rendering markup or CSS.

- [ ] **Step 4: Run the frontend suite/build and commit Task 7**

Run:

```powershell
npm --prefix athena-gui test
npm --prefix athena-gui run build
git diff --check -- athena-gui/src/components/conversation/MessageList.tsx athena-gui/src/components/__tests__/conversation-pane.test.tsx
```

Expected: the full Vitest suite and production build pass. Commit only Task 7 paths with `git commit -m "feat: group task understanding output by scope"`.

---

### Task 8: Documentation, regression verification, main integration, and cache cleanup

**Files:**
- Modify: `docs/athena-guide/13-task-clarification-conversation.md`
- Modify: `docs/superpowers/specs/2026-09-03-llm-task-understanding-output-design.md`
- Create after fresh evidence: `codex_docs/2026-09-03-llm-task-understanding-output-completion-report.md`
- Modify after fresh evidence: `codex_docs/CURRENT.md`
- Delete after fresh evidence: `docs/superpowers/plans/2026-09-03-llm-task-understanding-output.md`

**Interfaces:**
- Documents: configured/provider-less behavior, public-versus-private model data, live versus durable progress, atomic event scope, optimistic preview association, replay behavior, retry, and confirmation boundary.
- Produces: acceptance evidence and a clean `CURRENT.md` pointer state.
- Integrates: `feat/llm-task-understanding-output` into `main` without stashing or overwriting unrelated main-worktree changes.

- [ ] **Step 1: Update the user-facing boundary guide**

Add sections with these exact rules:

```text
- Configured GUI runtimes use the LLM clarification Agent; provider-less callers use the deterministic compatibility generator.
- Only validated public summaries are displayed. Raw model JSON, prompts, tool arguments, and private reasoning are never UI output.
- Tool progress is live-only. A final summary is sent through persistent output only after the matching draft transition is saved.
- New output uses session_id + scope=task_understanding + scope_id. The three fields are atomic.
- An optimistic empty-id preview may accept the first matching-session update; after hydration, scope_id must equal draft_id.
- Replayed scoped output without an active preview remains ordinary visible history; it does not synthesize a clarification card.
```

Keep the guide's existing Q&A identity, stale reply, confirmation, retry, and card-placement rules.

- [ ] **Step 2: Run backend formatting/static checks**

Run:

```powershell
uv run black --check `
  src/athena/research/clarification/generator.py `
  src/athena/research/clarification/llm_generator.py `
  src/athena/research/clarification/controller.py `
  src/athena/research/supervisor/events.py `
  src/athena/research/runtime/events.py `
  src/athena/research/runtime/bootstrap.py `
  src/athena/research/runtime/facade.py `
  test/unit/research/clarification `
  test/unit/research/supervisor/test_events.py `
  test/integration/test_tui_resume.py `
  tests/test_gui_gateway_transport.py
uv run python -m compileall -q src/athena
git diff --check
```

Expected: zero exit status. If the global diff check names an unrelated pre-existing file, record it and rerun `git diff --check` with every task-owned path explicitly; do not repair unrelated content.

- [ ] **Step 3: Run focused and repository-wide Python verification**

Run in order:

```powershell
uv run pytest -q `
  test/unit/research/clarification `
  test/unit/research/supervisor/test_events.py `
  test/integration/test_tui_resume.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py `
  tests/test_gui_gateway_e2e.py
uv run pytest -q test/unit/research test/integration/research
uv run pytest -q
```

Expected: no new failure. Record each command, timestamp, pass/fail/skip counts, duration, and warning text. Do not call a suite green if it exits non-zero.

- [ ] **Step 4: Run complete frontend and Rust verification**

Run:

```powershell
npm --prefix athena-gui test
npm --prefix athena-gui run build
cargo test --manifest-path athena-gui/src-tauri/Cargo.toml
cargo check --manifest-path athena-gui/src-tauri/Cargo.toml
```

Expected: all commands exit zero. Record Vitest file/test counts, build module count, Rust test count, and any pre-existing warning separately.

- [ ] **Step 5: Audit scope, safety, and dead-code boundaries**

Run:

```powershell
rg -n "reasoning_content|agent/text_delta|agent/function_call|project_agent_event|shell_command|write_file|start_search" `
  src/athena/research/clarification/generator.py `
  src/athena/research/clarification/llm_generator.py `
  src/athena/research/clarification/controller.py
rg -n "session_id|scope_id|task_understanding|persist=False|persist=True" `
  src/athena/research/clarification `
  src/athena/research/runtime `
  athena-gui/src/hooks/usePipeline.ts `
  athena-gui/src/components/conversation/MessageList.tsx
git diff --stat main...HEAD
git diff --name-status main...HEAD
```

Required evidence: no broad tool/lifecycle authority in the LLM module; raw Agent events are dropped; all new task-understanding publications carry the atomic metadata; no obsolete helper/import introduced by this task remains; only planned files changed.

- [ ] **Step 6: Commit the guide and completed implementation checkboxes**

Stage only the guide and this plan's checkbox updates. Commit with `git commit -m "docs: document LLM task clarification output"`. Do not create the completion report or delete the plan until Steps 2-5 have fresh successful evidence.

- [ ] **Step 7: Integrate the verified feature branch into `main`**

From the main worktree, first run `git status --short --branch` and compare dirty paths with `git diff --name-only main...feat/llm-task-understanding-output`. Never stash, reset, checkout, or overwrite unrelated work. If owned paths are clean, merge with:

```powershell
git merge --no-ff feat/llm-task-understanding-output -m "merge: LLM task understanding output"
```

If concurrent committed work advanced `main`, rebase the feature branch onto that commit in its isolated worktree, rerun the focused suites, then merge. If uncommitted main changes overlap task-owned paths, stop before mutation and report the exact paths.

- [ ] **Step 8: Verify `main`, remove the merged worktree/branch, and clear caches**

On merged `main`, rerun the focused backend suite, full frontend suite/build, `cargo test`, `cargo check`, and `git diff --check`.

After verification, confirm that
`C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\llm-task-understanding-output`
is the registered, clean feature worktree, remove that exact path with
`git worktree remove "C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\llm-task-understanding-output"`,
and delete the merged local feature branch with
`git branch -d feat/llm-task-understanding-output`.

Resolve each cache candidate to an absolute path under the Athena repository before deletion. Remove task-generated `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.coverage`, `athena-gui/dist`, `athena-gui/node_modules/.vite`, and `athena-gui/src-tauri/target` paths with native PowerShell `Remove-Item -LiteralPath ... -Recurse -Force`. Do not remove `node_modules` or any directory outside Athena. Re-run a read-only cache inventory; record any ACL-locked paths exactly rather than changing their ownership or permissions.

- [ ] **Step 9: Write closeout records and remove the completed plan**

Create the completion report with:

```markdown
# LLM Task-Understanding Output Completion Report

Date: 2026-09-03
Implementation and merge commits:

## Delivered behavior
## Acceptance-criteria evidence
## Verification commands and results
## Public-output safety audit
## Compatibility and rollback
## Pre-existing warnings or inaccessible caches
## Unrelated worktree changes preserved
```

Set the design status to `implemented and verified`. Delete this plan. Update `codex_docs/CURRENT.md` so it no longer names this plan/spec as active, retains the parallel authoritative-baseline plan/spec and other still-active pointers, and lists the completion report under most recent completed work. Commit only closeout files with `git commit -m "docs: close LLM task understanding output"`.
