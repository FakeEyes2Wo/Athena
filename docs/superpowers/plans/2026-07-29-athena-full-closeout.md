# Athena Full Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete every still-open Athena contract under `docs/superpowers/specs/`, remove all legacy Python namespaces, preserve IDE/App Server/Rust compatibility, and deliver the A2.2 Research Studio UI.

**Architecture:** Canonical domain packages own their types and behavior, while `ExperimentStore` is the only mutable research-state boundary. Compatibility is implemented at persistence and RPC adapters instead of duplicating fields in domain models; all derived graph state is computed. Work proceeds in dependency order, uses deterministic fake backends for tests, and deletes legacy packages only after a zero-import proof.

**Tech Stack:** Python 3.11, Pydantic 2, asyncio, pandas, scipy, pytest, React 18, TypeScript, Vite, Vitest, React Flow, Tauri 2, Rust, Playwright.

---

## Global Constraints

- Treat `docs/superpowers/specs/2026-07-29-athena-full-closeout-design.md` as the integration contract. For existing schema fields and ownership, `docs/superpowers/specs/2026-07-29-athena-existing-schema-first-design.md` takes precedence.
- Keep each domain class at seven or fewer persistent attributes unless the task review records a concrete, tested exception.
- `Experiment` has exactly `id`, `parent_id`, `commit`, `hypothesis`, `plan`, `status`, and `outcome`.
- `ExperimentOutcome` has exactly `eval`, `verdict`, and `is_sota`.
- Existing `core.schemas` field names, defaults, validation, and serialization are normative. Ownership changes move or re-export the same type object; they do not redesign it.
- `Agent` keeps exactly `model`, `tools`, `system_prompt`, and `config`; `name` and `description` are derived properties.
- `AgentConfig` keeps exactly `max_turns`, `max_tokens`, `temperature`, and `name`. New or modified public functions and constructors default to at most four direct parameters.
- `InMemoryExperimentStore` keeps only `_experiments`, `_hypotheses`, `_sota_id`, and `_lock`; children, descendants, pending items, and paths are derived.
- Prefer pure module functions for stateless behavior. Do not add single-field wrappers, duplicate DTOs, speculative protocols, or I/O in `__init__`.
- Keep App Server methods/events, IDE RPC envelopes, ResearchTree v1 JSON, default `.athena/research_tree.json`, and Python fallback behavior externally compatible.
- Do not modify `memory/`. Make only ownership/import changes required in `core/`, `app_server/`, and `ide/`.
- Do not delete a legacy namespace until production, test, and example imports have migrated and the static scan is empty.
- Do not commit `.claude/`, `.superpowers/brainstorm/`, `.superpowers/sdd/`, generated screenshots, or local runtime state. Stage exact paths only.
- Preserve unrelated user edits. Re-read a dirty file immediately before patching it.
- Keep pure Python formatting in Task 20, separate from behavioral commits. Before behavioral edits, record the current format-only path list and AST hashes so later staging can preserve provenance.
- For a Python file already in that format-only list, each behavioral task stages only its new semantic hunks and verifies `git diff --cached`; the pre-existing formatting hunks remain unstaged until Task 20 even when a task's sample command names the whole file.

## File Structure Map

### Canonical Python ownership

- `src/athena/code/types.py`: `ExecutionOutput`, `GenerationResult`, `EngineResult`.
- `src/athena/code/backends/{base,codex,qoder}.py`: backend contract and optional adapters.
- `src/athena/code/{engine,runner,monitor,review}.py`: generation loop, execution, monitoring, deterministic review.
- `src/athena/core/agent/{agent,provider}.py`: the only Python Agent implementation, model boundary, and construction interface.
- `src/athena/core/agent/prompts/`: code, data, and plot system prompts selected explicitly by the environment.
- `src/athena/code/output_specs.py`: code execution output constraints, independent of Agent state.
- `src/athena/data/{types,tools,operations}.py`: profiles, processing records, deterministic preparation.
- `src/athena/retrieval/{types,store}.py`: source DTOs and ordered async fallback.
- `src/athena/brainstorm/{types,generate}.py`: generation input/result, validation, fallbacks.
- `src/athena/core/schemas.py`: authoritative existing evaluation protocol and `ExperimentOutcome` until an acyclic, behavior-preserving move is justified.
- `src/athena/evaluation/types.py`: compatibility re-exports of core evaluation types; evaluator, comparator, and factory own evaluation behavior.
- `src/athena/experiment/types.py`: `ExperimentStatus` and `Experiment`, plus a compatibility re-export of core `ExperimentOutcome`.
- `src/athena/integrations/kaggle/{types,client}.py`: optional Kaggle boundary.
- `src/athena/app_server/thread_manager.py`: canonical manager protocol plus runtime implementation.
- `src/athena/ide/handler.py`: RPC orchestration over public experiment APIs only.

### Frontend and Tauri ownership

- `athena-ide/src/lib/presentation.ts`: pure four-state and phase presentation mapping.
- `athena-ide/src/components/shell/ResearchMasthead.tsx`: brand, title, single status presentation.
- Existing shell, conversation, right-rail, and context components: A2.2 presentation only.
- `athena-ide/src-tauri/src/commands/tree.rs`: `tree_get`, `tree_save`, and `tree_load` bridge commands.
- `athena-ide/tests/visual/workbench.spec.ts`: desktop viewport and accessibility smoke checks.

### Final deletions

- `src/athena/execution/`
- `src/athena/workflows/`
- `src/athena/core/research/`
- `src/athena/research/`
- `src/athena/knowledge/` when present
- `src/athena/agents/`

## Specification Coverage

| Specification | Implementation tasks |
|---|---|
| `2026-07-25-athena-rust-design.md` | Existing Rust baseline retained; Task 20 reruns fixtures, fmt, clippy, and workspace tests. |
| `2026-07-27-athena-ai4ml-design.md` | Task 0 public protocol; Tasks 7-12 evaluation, data, retrieval/HF/single-turn, ranking, code execution, orchestration, validation, and report. |
| `2026-07-28-athena-backend-ai4ml-implementation.md` | Tasks 1-17 canonical packages, ownership, adapters, store, persistence, integrations, migration, and deletion. |
| `2026-07-28-athena-ide-design.md` | Tasks 6, 14, and 15 preserve Python RPC, v1 snapshots, bridge behavior, and Tauri command registration. |
| `2026-07-28-athena-ide-frontend-redesign.md` | Existing simplified shell/lifecycle baseline plus Tasks 18-19. Superseded TopBar/viewModel/useEvents compatibility requests stay removed per the integration design. |
| `2026-07-28-athena-ide-visual-redesign.md` | Tasks 18-19 implement the later A2.2 visual direction where visual details conflict. |
| `2026-07-29-athena-full-closeout-design.md` | Global constraints and Tasks 0-20. |
| `2026-07-29-athena-agent-interface-consolidation-design.md` | Task 2 consolidates the Agent interface into `athena.core.agent`. |
| `2026-07-29-athena-ide-research-studio-design.md` | Tasks 18-19, including four visible states and both required viewports. |

## Task 0: Public AI4ML Protocol

**Files:**
- Modify: `src/athena/core/schemas.py`
- Create: `tests/test_ai4ml_protocol.py`

- [ ] **Step 0: Record the pre-existing Python formatting patch outside commits**

Run: `New-Item -ItemType Directory -Force '.superpowers/sdd/2026-07-29-athena-full-closeout' | Out-Null`

Run: `git diff --name-only -- '*.py' | Set-Content -Encoding UTF8 '.superpowers/sdd/2026-07-29-athena-full-closeout/preexisting-python-format-paths.txt'`

Run: `git diff --binary -- '*.py' | Set-Content -Encoding UTF8 'C:\tmp\athena-preexisting-python-format.patch'`

Expected: both files capture only the already-reviewed formatting baseline. They are runtime evidence and must not be staged.

- [ ] **Step 1: Add failing ID, event, task, and error tests**

```python
def test_new_id_uses_requested_prefix() -> None:
    assert re.fullmatch(r"run_[0-9a-f]{12}", new_id("run"))


def test_event_and_error_round_trip() -> None:
    event = EventEnvelope(kind="experiment/checkpoint", source="search", payload={"id": "exp_1"}, state_version=1)
    error = ErrorRecord(severity="degrade", code="retrieval_unavailable", message="web fallback", retry_count=0)
    assert EventEnvelope.model_validate_json(event.model_dump_json()) == event
    assert ErrorRecord.model_validate_json(error.model_dump_json()) == error
```

- [ ] **Step 2: Run the protocol tests**

Run: `uv run pytest tests/test_ai4ml_protocol.py -q`

Expected: FAIL because the public protocol names are missing.

- [ ] **Step 3: Add the minimal shared protocol without duplicating domain DTOs**

```python
RunId = NewType("RunId", str)
HypothesisId = NewType("HypothesisId", str)
ExperimentId = NewType("ExperimentId", str)


def new_id(prefix: str) -> str:
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("prefix must be non-empty and alphanumeric")
    return f"{prefix}_{uuid4().hex[:12]}"


class EventEnvelope(BaseModel):
    kind: str
    source: str
    payload: dict[str, object]
    state_version: int = Field(ge=0)


class AgentTask(BaseModel):
    task_id: str
    agent_type: str
    command: str
    context_refs: list[ArtifactRef] = Field(default_factory=list)


class ErrorRecord(BaseModel):
    severity: Literal["retry", "degrade", "fatal"]
    code: str
    message: str
    retry_count: int = Field(default=0, ge=0)
```

Keep `ArtifactRef` and existing public DTOs stable. Do not define a second `AgentResult`, evaluation type, or experiment type in `core.schemas`.

- [ ] **Step 4: Verify public protocol behavior**

Run: `uv run pytest tests/test_ai4ml_protocol.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/core/schemas.py tests/test_ai4ml_protocol.py
git commit -m "feat(core): define minimal AI4ML protocol"
```

## Task 1: Canonical Code Types

**Files:**
- Create: `src/athena/code/types.py`
- Modify: `src/athena/code/backends/base.py`
- Modify: `src/athena/code/engine.py`
- Modify: `src/athena/code/runner.py`
- Modify: `src/athena/code/__init__.py`
- Create: `tests/test_code_types.py`

- [ ] **Step 1: Write the failing ownership test**

```python
from athena.code.types import EngineResult, ExecutionOutput, GenerationResult
from athena.code.backends.base import ExecutionOutput as BackendExecutionOutput


def test_code_types_have_one_owner() -> None:
    assert BackendExecutionOutput is ExecutionOutput
    assert EngineResult(rounds=0).success is False
    assert GenerationResult().files_created == []
```

- [ ] **Step 2: Run it and confirm the canonical module is missing**

Run: `uv run pytest tests/test_code_types.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'athena.code.types'`.

- [ ] **Step 3: Move the three dataclasses and re-export them**

```python
@dataclass
class ExecutionOutput:
    stdout: str
    stderr: str
    returncode: int
    files: list[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    output: str = ""


@dataclass
class EngineResult:
    rounds: int = 0
    final_output: ExecutionOutput | None = None
    files: list[str] = field(default_factory=list)
    success: bool = False
```

Import these definitions from `athena.code.types` in every consumer; do not retain duplicate class bodies.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_code_types.py tests/test_code_backend.py tests/test_code_runner.py tests/test_code_engine.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit exact files**

```powershell
git add src/athena/code/types.py src/athena/code/backends/base.py src/athena/code/engine.py src/athena/code/runner.py src/athena/code/__init__.py tests/test_code_types.py
git commit -m "refactor(code): centralize execution result types"
```

## Task 2: Consolidate the Code Agent Interface in Core

**Files:**
- Modify: `src/athena/core/agent/agent.py`
- Modify: `src/athena/core/agent/provider.py`
- Modify: `src/athena/core/agent/subagent.py`
- Modify: `src/athena/core/agent/__init__.py`
- Create: `src/athena/core/agent/prompts/code_agent.md`
- Create: `src/athena/core/agent/prompts/data_agent.md`
- Create: `src/athena/core/agent/prompts/plot_agent.md`
- Create: `src/athena/code/output_specs.py`
- Delete: `src/athena/agents/`
- Modify: `agent_tool_example.py`
- Modify: `test/unit/test_agent.py`
- Modify: `test/unit/test_agent_tool_example.py`
- Create: `tests/test_code_agent_assets.py`
- Delete: `tests/test_agent_factory.py`
- Delete: `tests/test_agent_registry.py`

**Interfaces:**
- Consumes: existing `ToolRegistry`, `AsyncOpenAI`, Agent loop, `agent_runner`, and App Server `ThreadRuntime` behavior.
- Produces: `ResponsesProvider(model: str, *, client: AsyncOpenAI | None = None)`, `AgentConfig(max_turns=20, max_tokens=4096, temperature=0.1, name="code-agent")`, and `create_code_agent(model, tools, system_prompt, config=None) -> Agent`.

- [ ] **Step 1: Add failing ownership, signature, state, and three-Agent tests**

Add these assertions to `test/unit/test_agent.py` and move the asset assertions into `tests/test_code_agent_assets.py`:

```python
from dataclasses import fields
from inspect import signature

from athena.core.agent import Agent, AgentConfig, ResponsesProvider, create_code_agent
from athena.core.tool import ToolRegistry


def test_code_agent_interface_has_four_parameters_and_four_fields() -> None:
    assert list(signature(create_code_agent).parameters) == [
        "model", "tools", "system_prompt", "config"
    ]
    assert [field.name for field in fields(AgentConfig)] == [
        "max_turns", "max_tokens", "temperature", "name"
    ]


def test_model_combines_name_and_client() -> None:
    client = object()
    model = ResponsesProvider("test-model", client=client)
    assert model.model_name == "test-model"
    assert model.client is client


def test_environment_builds_three_independent_core_agents() -> None:
    model = ResponsesProvider("test-model", client=object())
    agents = [
        create_code_agent(
            model,
            ToolRegistry(),
            f"{name} prompt",
            AgentConfig(name=f"{name}-agent"),
        )
        for name in ("code", "data", "plot")
    ]
    assert all(type(agent) is Agent for agent in agents)
    assert [agent.name for agent in agents] == [
        "code-agent", "data-agent", "plot-agent"
    ]
    assert all(set(vars(agent)) == {"model", "tools", "system_prompt", "config"} for agent in agents)
    assert len({id(agent) for agent in agents}) == 3
```

```python
from pathlib import Path

import athena.core.agent as core_agent
from athena.code.output_specs import CODE_AGENT_SPEC, DATA_AGENT_SPEC, PLOT_AGENT_SPEC


def test_code_agent_assets_have_canonical_owners() -> None:
    prompt_dir = Path(core_agent.__file__).parent / "prompts"
    assert {path.name for path in prompt_dir.glob("*_agent.md")} == {
        "code_agent.md", "data_agent.md", "plot_agent.md"
    }
    assert CODE_AGENT_SPEC.must_exist == ["REPORT.md"]
    assert DATA_AGENT_SPEC.must_exist == ["EDA.md", "feature_process.csv"]
    assert PLOT_AGENT_SPEC.format_check == {"*.png": "300dpi"}
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest test/unit/test_agent.py tests/test_code_agent_assets.py -q`

Expected: FAIL because `create_code_agent` is missing, `ResponsesProvider` does not accept a model name, and the assets still belong to `athena.agents`.

- [ ] **Step 3: Implement the four-parameter interface without changing the loop algorithm**

Refactor the existing classes to these state boundaries:

```python
@dataclass(slots=True, frozen=True)
class AgentConfig:
    max_turns: int = 20
    max_tokens: int = 4096
    temperature: float = 0.1
    name: str = "code-agent"


class Agent(BaseAgent):
    def __init__(
        self,
        model: ResponsesProvider,
        tools: ToolRegistry,
        system_prompt: str,
        config: AgentConfig | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.config = config or AgentConfig()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def description(self) -> str:
        return f"Agent: {self.model.model_name}"


def create_code_agent(
    model: ResponsesProvider,
    tools: ToolRegistry,
    system_prompt: str,
    config: AgentConfig | None = None,
) -> Agent:
    return Agent(model, tools, system_prompt, config)
```

Make `ResponsesProvider` the existing model boundary:

```python
class ResponsesProvider:
    def __init__(
        self,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model_name = model
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model_name
```

Keep `_sampling_loop` branches and event behavior unchanged. Pass the owning Agent to the helper instead of expanding its four fields into parameters:

```python
outcome = await _sampling_loop(self, ctx)

async for event in agent.model.stream(
    agent.config,
    agent.tools,
    mem.items,
    ctx.cancel,
):
    match event.kind:
        # This is the existing match statement; only rename ev to event if needed.
        case "text_delta":
            text = event.data.get("accumulated", text + event.data.get("delta", ""))

tool = agent.tools.resolve(tool_call.name)

request = {
    "model": self.model_name,
    "messages": api_messages,
    "max_tokens": config.max_tokens,
    "temperature": config.temperature,
    "stream": True,
}
```

Change the helper signature exactly to `_sampling_loop(agent: Agent, ctx: AgentContext) -> StepOutcome`. Change `ResponsesProvider.stream` parameters exactly to `(self, config: AgentConfig, tools: ToolRegistry, messages: list[ModelMessage], cancel: asyncio.Event)`. Retain every current `text_delta`, `function_call`, `response_completed`, error, cancellation, tool barrier, and memory write branch. In `Agent.run`, inject `self.system_prompt`, iterate `range(self.config.max_turns)`, and call `_sampling_loop(self, ctx)`. Resolve tools through `agent.tools`. Update `subagent.py` and `agent_runner` consumers from `agent.config.tools` to `agent.tools`.

Retain the common legacy construction call as a compatibility wrapper with no custom description state:

```python
def create_agent(
    model: str,
    tools: ToolRegistry,
    system_prompt: str,
    *,
    client: AsyncOpenAI | None = None,
    max_turns: int = 20,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    name: str = "agent",
) -> Agent:
    provider = ResponsesProvider(model, client=client)
    config = AgentConfig(max_turns, max_tokens, temperature, name)
    return create_code_agent(provider, tools, system_prompt, config)
```

- [ ] **Step 4: Move assets, migrate consumers, and prove the old package is unused**

Move the three existing prompt files without changing their contents. Move `OutputSpec` and its three constants to `athena.code.output_specs`. Update `agent_tool_example.py` and tests to read `agent.tools`, while keeping its existing `create_agent` call valid. Delete `athena.agents` only after this scan is empty:

Run: `rg -n --glob '*.py' '(from|import) athena\.agents' src test tests examples agent_tool_example.py`

Expected: exit code 1 and no output.

- [ ] **Step 5: Run core Agent, example, App Server, and asset tests**

Run: `uv run pytest test/unit/test_agent.py test/unit/test_agent_tool_example.py test/unit/app_server tests/test_code_agent_assets.py -q`

Run: `uv run black --check src/athena/core/agent/agent.py src/athena/core/agent/provider.py src/athena/core/agent/subagent.py src/athena/core/agent/__init__.py src/athena/code/output_specs.py test/unit/test_agent.py test/unit/test_agent_tool_example.py tests/test_code_agent_assets.py`

Expected: all tests PASS; Black reports every listed Python file unchanged.

- [ ] **Step 6: Commit the consolidation with exact paths**

```powershell
git add src/athena/core/agent/agent.py src/athena/core/agent/provider.py src/athena/core/agent/subagent.py src/athena/core/agent/__init__.py src/athena/core/agent/prompts src/athena/code/output_specs.py agent_tool_example.py test/unit/test_agent.py test/unit/test_agent_tool_example.py tests/test_code_agent_assets.py
git add -A src/athena/agents tests/test_agent_factory.py tests/test_agent_registry.py
git commit -m "refactor(core): consolidate code Agent interface"
```

## Task 3: Canonical Domain Type Ownership

**Files:**
- Modify: `src/athena/data/types.py`
- Modify: `src/athena/data/operations.py`
- Modify: `src/athena/brainstorm/types.py`
- Modify: `src/athena/brainstorm/generate.py`
- Modify: `src/athena/evaluation/types.py`
- Modify: `tests/test_data_operations.py`
- Modify: `tests/test_retrieval_brainstorm.py`
- Modify: `tests/test_evaluation.py`

- [ ] **Step 1: Add failing identity and existing-contract tests**

```python
from dataclasses import fields
from datetime import datetime


def test_processing_record_is_owned_by_data_types() -> None:
    from athena.data.operations import ProcessingRecord as imported
    from athena.data.types import ProcessingRecord as owned

    assert imported is owned
    assert [field.name for field in fields(owned)] == [
        "col", "operation", "params", "timestamp"
    ]


def test_processing_record_keeps_existing_defaults() -> None:
    record = ProcessingRecord(col="age", operation="fillna")
    assert record.params == {}
    datetime.fromisoformat(record.timestamp)


def test_evaluation_types_reexport_existing_core_contract() -> None:
    from athena.core.schemas import (
        ComparisonVerdict as core_verdict,
        EvalResult as core_result,
        EvalSpec as core_spec,
        MetricDef as core_metric,
    )
    from athena.evaluation.types import (
        ComparisonVerdict,
        EvalResult,
        EvalSpec,
        MetricDef,
    )

    assert MetricDef is core_metric
    assert EvalSpec is core_spec
    assert EvalResult is core_result
    assert ComparisonVerdict is core_verdict
    assert list(MetricDef.model_fields) == ["name", "direction", "description"]


def test_eval_spec_is_frozen() -> None:
    spec = EvalSpec(
        primary=MetricDef(
            name="rmse", direction="minimize", description="root mean square error"
        )
    )
    with pytest.raises(ValidationError):
        spec.test_ratio = 0.3
```

- [ ] **Step 2: Run the domain tests**

Run: `uv run pytest tests/test_data_operations.py tests/test_retrieval_brainstorm.py tests/test_evaluation.py -q`

Expected: FAIL because `ProcessingRecord` and `FalsifiabilityError` still live in behavior modules and `evaluation.types` defines a second set of model classes.

- [ ] **Step 3: Move only local definitions and re-export existing core models**

```python
@dataclass
class ProcessingRecord:
    col: str
    operation: str
    params: dict = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class FalsifiabilityError(ValueError):
    """Hypothesis is not falsifiable or missing required fields."""


from athena.core.schemas import (
    ComparisonVerdict,
    EvalResult,
    EvalSpec,
    MetricDef,
)

__all__ = ["ComparisonVerdict", "EvalResult", "EvalSpec", "MetricDef"]
```

Move the existing `ProcessingRecord` class body from `data.operations` to `data.types` without changing its dataclass type, fields, defaults, or timestamp representation. Move the existing `FalsifiabilityError` class body from `brainstorm.generate` to `brainstorm.types` without changing its base class or runtime behavior. Delete the four duplicate evaluation class bodies; `evaluation.types` only re-exports the existing core objects. Do not modify retrieval types.

- [ ] **Step 4: Verify identity, schema generation, and focused tests**

Run: `uv run python -c "from athena.core.schemas import EvalSpec as C; from athena.evaluation.types import EvalSpec as E; assert C is E; C.model_json_schema()"`

Run: `uv run pytest tests/test_data_operations.py tests/test_retrieval_brainstorm.py tests/test_evaluation.py -q`

Run: `rg -n '^class (MetricDef|EvalSpec|EvalResult|ComparisonVerdict)' src/athena/core/schemas.py src/athena/evaluation/types.py`

Expected: commands succeed, tests PASS, and each evaluation class body appears only in `core.schemas.py`.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/data/types.py src/athena/data/operations.py src/athena/brainstorm/types.py src/athena/brainstorm/generate.py src/athena/evaluation/types.py tests/test_data_operations.py tests/test_retrieval_brainstorm.py tests/test_evaluation.py
git commit -m "refactor(domain): assign canonical type ownership"
```

## Task 4: Seven-Field Experiment Model

**Files:**
- Modify: `src/athena/experiment/types.py`
- Modify: `src/athena/experiment/__init__.py`
- Create: `tests/test_experiment_types.py`

- [ ] **Step 1: Add failing exact-field, identity, and round-trip tests**

```python
def test_experiment_has_exact_canonical_fields(experiment) -> None:
    assert list(Experiment.model_fields) == [
        "id", "parent_id", "commit", "hypothesis", "plan", "status", "outcome"
    ]
    assert list(ExperimentOutcome.model_fields) == ["eval", "verdict", "is_sota"]
    assert Experiment.model_validate_json(experiment.model_dump_json()) == experiment


def test_experiment_outcome_reexports_existing_core_type() -> None:
    from athena.core.schemas import ExperimentOutcome as CoreExperimentOutcome

    assert ExperimentOutcome is CoreExperimentOutcome


def test_status_keeps_existing_fields_and_default() -> None:
    assert list(ExperimentStatus.model_fields) == ["state", "error"]
    assert ExperimentStatus().model_dump() == {"state": "PENDING", "error": None}
```

- [ ] **Step 2: Verify the current model fails**

Run: `uv run pytest tests/test_experiment_types.py -q`

Expected: FAIL because the current `Experiment` lacks `id` and `parent_id`, stores `metric_type`/`result`, and `experiment.types` defines a second `ExperimentOutcome` class.

- [ ] **Step 3: Implement the exact Pydantic contract**

```python
class ExperimentStatus(BaseModel):
    state: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"] = "PENDING"
    error: str | None = None


class Experiment(BaseModel):
    id: str
    parent_id: str | None = None
    commit: CommitHash
    hypothesis: Hypothesis
    plan: ExperimentPlan
    status: ExperimentStatus = Field(default_factory=ExperimentStatus)
    outcome: ExperimentOutcome | None = None
```

Import `ExperimentOutcome` from `athena.core.schemas` and re-export that same object from `athena.experiment.types`; do not retain a second class body. Preserve the existing `ExperimentStatus` fields and uppercase state values. Only `Experiment` gains stable IDs and drops the duplicate persistence-only `metric_type` and `result` fields.

- [ ] **Step 4: Verify schema and round trip**

Run: `uv run python -c "from athena.experiment.types import Experiment, ExperimentOutcome; Experiment.model_json_schema(); ExperimentOutcome.model_json_schema()"`

Run: `uv run pytest tests/test_experiment_types.py -q`

Expected: commands succeed and tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/experiment/types.py src/athena/experiment/__init__.py tests/test_experiment_types.py
git commit -m "refactor(experiment): minimize canonical experiment state"
```

## Task 5: Async Experiment Store

**Files:**
- Modify: `src/athena/experiment/store.py`
- Modify: `src/athena/experiment/local_store.py`
- Modify: `src/athena/experiment/__init__.py`
- Create: `tests/test_experiment_store.py`

- [ ] **Step 1: Add failing store behavior tests**

```python
async def test_store_uses_stable_ids_and_bfs(store, root, child, grandchild) -> None:
    for exp in (root, child, grandchild):
        await store.upsert_experiment(exp)
    assert await store.list_children(root.id) == [child.id]
    assert await store.list_descendants(root.id) == [child.id, grandchild.id]


async def test_store_keeps_only_minimal_state(store) -> None:
    assert set(vars(store)) == {"_experiments", "_hypotheses", "_sota_id", "_lock"}
```

Also cover duplicate upsert, missing parent, cycle rejection, status filters, unknown IDs, SOTA set/unset, pending hypotheses, and concurrent calls in one event loop.

- [ ] **Step 2: Run and confirm interface mismatch**

Run: `uv run pytest tests/test_experiment_store.py -q`

Expected: FAIL because `upsert_experiment`, `list_descendants`, `set_sota`, and status updates are absent.

- [ ] **Step 3: Implement the exact store surface**

```python
class ExperimentStore(ABC):
    @abstractmethod
    async def upsert_experiment(self, experiment: Experiment) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        raise NotImplementedError

    @abstractmethod
    async def list_children(self, parent_id: str, status_filter: ExperimentStatus | None = None) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def list_descendants(self, root_id: str, status_filter: ExperimentStatus | None = None) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def best_experiment(self) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def set_sota(self, experiment_id: str, is_sota: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    async def add_hypothesis(self, hypothesis: Hypothesis) -> None:
        raise NotImplementedError

    @abstractmethod
    async def pending_hypotheses(self) -> list[Hypothesis]:
        raise NotImplementedError

    @abstractmethod
    async def update_hypothesis_status(self, hypothesis_id: str, status: str) -> None:
        raise NotImplementedError
```

Use one `asyncio.Lock`; validate parent existence and cycles before assignment. Preserve dict insertion order and compute BFS, pending, and children from `_experiments`/`_hypotheses`.

- [ ] **Step 4: Add and test the pure root-path helper**

```python
async def experiment_path(store: ExperimentStore, experiment_id: str) -> list[Experiment]:
    path: list[Experiment] = []
    seen: set[str] = set()
    current_id: str | None = experiment_id
    while current_id is not None:
        if current_id in seen:
            raise ValueError("experiment parent cycle")
        seen.add(current_id)
        current = await store.get_experiment(current_id)
        if current is None:
            raise KeyError(current_id)
        path.append(current)
        current_id = current.parent_id
    return list(reversed(path))
```

Run: `uv run pytest tests/test_experiment_store.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/experiment/store.py src/athena/experiment/local_store.py src/athena/experiment/__init__.py tests/test_experiment_store.py
git commit -m "feat(experiment): implement minimal async experiment store"
```

## Task 6: ResearchTree v1 Persistence Adapter

**Files:**
- Create: `src/athena/experiment/persistence.py`
- Create: `test/fixtures/research_tree_v1.json`
- Create: `tests/test_experiment_persistence.py`

- [ ] **Step 1: Add failing v1 compatibility and failure-safety tests**

```python
async def test_v1_fixture_round_trips_without_domain_field_pollution(tmp_path) -> None:
    store = InMemoryExperimentStore()
    await load_v1_snapshot(store, Path("test/fixtures/research_tree_v1.json"))
    target = tmp_path / "research_tree.json"
    await save_v1_snapshot(store, target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert {"id", "parent_id", "children_ids", "exp"} <= set(next(iter(payload["nodes"].values())))
    assert "gitwork" in next(iter(payload["nodes"].values()))["exp"]


async def test_failed_replace_keeps_last_valid_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "research_tree.json"
    target.write_text('{"version":1,"nodes":{},"hypotheses":{}}', encoding="utf-8")
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        await save_v1_snapshot(InMemoryExperimentStore(), target)
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 1
```

- [ ] **Step 2: Verify persistence APIs are absent**

Run: `uv run pytest tests/test_experiment_persistence.py -q`

Expected: FAIL importing `athena.experiment.persistence`.

- [ ] **Step 3: Implement stateless v1 mapping and atomic writes**

```python
DEFAULT_RESEARCH_TREE_PATH = Path(".athena/research_tree.json")


async def snapshot_v1(
    store: InMemoryExperimentStore,
    *,
    gitwork_by_id: Mapping[str, GitWorkBranch] | None = None,
) -> dict[str, object]:
    async with store._lock:
        experiments = list(store._experiments.values())
        hypotheses = list(store._hypotheses.values())
        sota_id = store._sota_id
    return {
        "version": 1,
        "sota_id": sota_id,
        "nodes": {
            exp.id: encode_v1_node(exp, experiments, (gitwork_by_id or {}).get(exp.id))
            for exp in experiments
        },
        "hypotheses": encode_v1_hypotheses(hypotheses),
    }


async def restore_v1(store: InMemoryExperimentStore, payload: Mapping[str, object]) -> None:
    if payload.get("version") != 1:
        raise ValueError(f"unsupported research tree version: {payload.get('version')}")
    for experiment in decode_v1_experiments(payload):
        await store.upsert_experiment(experiment)
    for hypothesis in decode_v1_hypotheses(payload):
        await store.add_hypothesis(hypothesis)
    if payload.get("sota_id"):
        await store.set_sota(str(payload["sota_id"]), True)


async def save_v1_snapshot(store: InMemoryExperimentStore, path: Path = DEFAULT_RESEARCH_TREE_PATH, *, gitwork_by_id: Mapping[str, GitWorkBranch] | None = None) -> Path:
    payload = await snapshot_v1(store, gitwork_by_id=gitwork_by_id)
    return atomic_write_json(path, payload)


async def load_v1_snapshot(store: InMemoryExperimentStore, path: Path = DEFAULT_RESEARCH_TREE_PATH) -> None:
    await restore_v1(store, json.loads(path.read_text(encoding="utf-8")))
```

Validate `version == 1`; map `metric_type`, scalar `result`, and `gitwork` only in local variables. The persistence adapter may read the concrete in-memory store's four fields while holding its lock; neither IDE nor orchestration code may do so. Write a sibling temporary file, flush it, then call `os.replace`; remove only the temporary file on failure.

- [ ] **Step 4: Run persistence and store tests**

Run: `uv run pytest tests/test_experiment_store.py tests/test_experiment_persistence.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/experiment/persistence.py test/fixtures/research_tree_v1.json tests/test_experiment_persistence.py
git commit -m "feat(experiment): preserve research tree v1 snapshots"
```

## Task 7: Frozen Evaluation and Paired Comparison

**Files:**
- Modify: `src/athena/evaluation/evaluator.py`
- Modify: `src/athena/evaluation/comparator.py`
- Modify: `src/athena/evaluation/factory.py`
- Modify: `tests/test_evaluation.py`

- [ ] **Step 1: Add failing behavioral tests**

```python
def test_comparison_respects_minimize_direction(sample_store) -> None:
    baseline = result("base", 0.5, sample_store.put([0.6, 0.4]))
    candidate = result("cand", 0.3, sample_store.put([0.4, 0.2]))
    verdict = compare_results(baseline, candidate, direction="minimize", read_samples=sample_store.get)
    assert verdict.winner == "candidate"


def test_equal_samples_are_a_tie(sample_store) -> None:
    ref = sample_store.put([1.0, 1.0, 1.0])
    assert compare_results(result("a", 1, ref), result("b", 1, ref), direction="maximize", read_samples=sample_store.get).winner == "tie"
```

- [ ] **Step 2: Run the evaluation suite**

Run: `uv run pytest tests/test_evaluation.py -q`

Expected: at least the paired-sample or direction test FAILS.

- [ ] **Step 3: Implement pure evaluation functions**

```python
def evaluate_predictions(experiment_id: str, spec: EvalSpec, y_true: Sequence[float], y_pred: Sequence[float], write_samples: Callable[[Sequence[float]], ArtifactRef]) -> EvalResult:
    primary, secondary, per_sample = metric_values(spec, y_true, y_pred)
    return EvalResult(
        experiment_id=experiment_id,
        primary=primary,
        secondary=secondary,
        per_sample=write_samples(per_sample),
    )


def compare_results(baseline: EvalResult, candidate: EvalResult, *, direction: Literal["maximize", "minimize"], read_samples: Callable[[ArtifactRef], Sequence[float]], alpha: float = 0.05) -> ComparisonVerdict:
    baseline_samples = finite_samples(read_samples(baseline.per_sample))
    candidate_samples = finite_samples(read_samples(candidate.per_sample))
    if len(baseline_samples) != len(candidate_samples):
        raise ValueError("paired sample lengths differ")
    p_value = float(stats.ttest_rel(candidate_samples, baseline_samples).pvalue)
    delta = candidate.primary - baseline.primary
    if not math.isfinite(p_value) or p_value >= alpha or delta == 0:
        return ComparisonVerdict(winner="tie", p_value=1.0 if not math.isfinite(p_value) else p_value)
    candidate_wins = delta > 0 if direction == "maximize" else delta < 0
    return ComparisonVerdict(winner="candidate" if candidate_wins else "baseline", p_value=p_value)
```

Reject mismatched or non-finite sample arrays. Use a paired test from scipy, map non-significant results to `tie`, and keep default metric creation in a pure `create_eval_spec(task_type)` function.

- [ ] **Step 4: Verify deterministic evaluation**

Run: `uv run pytest tests/test_evaluation.py -q`

Expected: all tests PASS twice with identical results.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/evaluation tests/test_evaluation.py
git commit -m "feat(evaluation): add frozen paired evaluation"
```

## Task 8: Deterministic PREPARE Data Flow

**Files:**
- Modify: `src/athena/data/types.py`
- Modify: `src/athena/data/tools.py`
- Modify: `src/athena/data/operations.py`
- Modify: `src/athena/experiment/pipeline.py`
- Modify: `tests/test_data_tools.py`
- Modify: `tests/test_data_operations.py`
- Modify: `tests/test_prepare_workflow.py`

- [ ] **Step 1: Add failing sampling, trace, and split tests**

```python
def test_large_dataset_uses_three_fixed_samples(frame_100001) -> None:
    refs = create_analysis_samples(frame_100001, put_frame=fake_store.put)
    assert [item.seed for item in refs] == [17, 42, 97]


def test_split_manifest_is_disjoint(prepared) -> None:
    train, validation, test = map(set, prepared.index_sets())
    assert not (train & validation or train & test or validation & test)
    assert train | validation | test == set(range(prepared.row_count))
```

- [ ] **Step 2: Run PREPARE tests**

Run: `uv run pytest tests/test_data_tools.py tests/test_data_operations.py tests/test_prepare_workflow.py -q`

Expected: FAIL on fixed three-sample or manifest behavior.

- [ ] **Step 3: Implement deterministic module functions**

```python
ANALYSIS_SEEDS = (17, 42, 97)


def create_analysis_samples(frame: pd.DataFrame, *, put_frame: Callable[[pd.DataFrame], ArtifactRef], sample_size: int = 10_000) -> list[SampleRef]:
    seeds = ANALYSIS_SEEDS if len(frame) > 100_000 else (ANALYSIS_SEEDS[0],)
    return [SampleRef(seed=seed, artifact=put_frame(frame.sample(min(sample_size, len(frame)), random_state=seed))) for seed in seeds]


def apply_operation(frame: pd.DataFrame, *, column: str, operation: str, params: Mapping[str, object], now: Callable[[], datetime]) -> tuple[pd.DataFrame, ProcessingRecord]:
    transformed = OPERATION_HANDLERS[operation](frame.copy(), column, params)
    return transformed, ProcessingRecord(col=column, operation=operation, params=dict(params), timestamp=now().isoformat())


def split_dataset(frame: pd.DataFrame, *, seed: int, validation_ratio: float, test_ratio: float, put_frame: Callable[[pd.DataFrame], ArtifactRef]) -> SplitManifest:
    train, validation, test = deterministic_split(frame, seed, validation_ratio, test_ratio)
    validate_disjoint_indices(train, validation, test, expected=set(frame.index))
    return SplitManifest(train=put_frame(train), validation=put_frame(validation), test=put_frame(test))
```

Copy raw and cleaned frames into the artifact store. Keep the final test reference out of SEARCH inputs and record UTC-aware timestamps.

- [ ] **Step 4: Run deterministic tests twice**

Run: `uv run pytest tests/test_data_tools.py tests/test_data_operations.py tests/test_prepare_workflow.py -q`

Expected: all tests PASS twice without network or LLM calls.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/data src/athena/experiment/pipeline.py tests/test_data_tools.py tests/test_data_operations.py tests/test_prepare_workflow.py
git commit -m "feat(data): make prepare artifacts deterministic"
```

## Task 9: Ordered Retrieval and Falsifiable Brainstorming

**Files:**
- Modify: `src/athena/retrieval/store.py`
- Modify: `src/athena/brainstorm/generate.py`
- Modify: `tests/test_retrieval_brainstorm.py`

- [ ] **Step 1: Add failing async fallback and malformed-output tests**

```python
async def test_retrieval_falls_back_in_declared_order() -> None:
    calls: list[str] = []
    papers = await retrieve_papers("tabular", adapters=failing_then_web(calls))
    assert calls == ["arxiv", "semantic_scholar", "web"]
    assert papers[0].source == "web"


async def test_malformed_llm_output_uses_sourced_templates() -> None:
    result = await generate_hypotheses(input_case, llm=returns("not-json"))
    assert result.source == "template_fallback"
    assert 3 <= len(result.hypotheses) <= 5
    assert all(h.sources for h in result.hypotheses)


async def test_hf_download_is_revision_pinned(fake_hf) -> None:
    ref = await download_model("org/model", "a1b2c3", client=fake_hf, put_path=fake_store.put_path)
    assert fake_hf.calls == [("org/model", "a1b2c3", False)]
    assert ref.revision == "a1b2c3"
    assert ref.license and ref.digest


async def test_single_turn_keeps_no_history() -> None:
    client = RecordingTextClient(["first", "second"])
    assert await single_turn("one", client=client) == "first"
    assert await single_turn("two", client=client) == "second"
    assert client.requests == [{"prompt": "one"}, {"prompt": "two"}]
```

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/test_retrieval_brainstorm.py -q`

Expected: fallback order or malformed JSON test FAILS.

- [ ] **Step 3: Implement non-blocking adapters and one-experiment validation**

```python
async def retrieve_papers(query: str, adapters: Sequence[RetrievalAdapter]) -> list[PaperRef]:
    for adapter in adapters:
        try:
            papers = await adapter.search(query)
        except RecoverableRetrievalError:
            continue
        if papers:
            return papers
    return [common_knowledge_ref(query)]


async def generate_hypotheses(data: HypothesisInput, *, llm: AsyncTextClient | None = None) -> BrainStormResult:
    hypotheses = parse_hypotheses(await llm.complete(prompt_for(data))) if llm else []
    source = "llm"
    if not hypotheses:
        hypotheses = template_hypotheses(data)
        source = "template_fallback"
    for hypothesis in hypotheses:
        validate_falsifiable(hypothesis)
    return BrainStormResult(hypotheses=hypotheses[:5], source=source)


def validate_falsifiable(hypothesis: Hypothesis) -> None:
    if not hypothesis.intervention.strip() or not hypothesis.expected_effect.strip() or not hypothesis.sources:
        raise FalsifiabilityError("hypothesis requires one intervention, measurable effect, and source")


async def download_model(repo: str, revision: str, *, client: HFClient, put_path: Callable[[Path], ArtifactRef]) -> HFModelArtifact:
    if not revision.strip():
        raise ValueError("revision must be pinned")
    metadata = await client.metadata(repo, revision)
    path = await client.download(repo, revision, trust_remote_code=False)
    return HFModelArtifact(
        repo=repo,
        revision=revision,
        license=metadata.license,
        digest=sha256_path(path),
        artifact=put_path(path),
    )


async def single_turn(prompt: str, *, client: AsyncTextClient, model: str = "haiku") -> str:
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")
    return await client.complete(prompt=prompt, model=model, history=None)
```

Call synchronous clients with `asyncio.to_thread`; continue on declared integration errors; mark common-knowledge fallback explicitly. Reject empty source, intervention, or measurable expected effect.

- [ ] **Step 4: Verify without live services**

Run: `uv run pytest tests/test_retrieval_brainstorm.py -q`

Expected: all tests PASS with mocked adapters.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/retrieval/store.py src/athena/brainstorm/generate.py tests/test_retrieval_brainstorm.py
git commit -m "feat(search): add ordered retrieval and hypothesis fallback"
```

## Task 10: Deterministic Bradley-Terry Ranking

**Files:**
- Modify: `src/athena/experiment/ranking.py`
- Modify: `tests/test_ranking.py`

- [ ] **Step 1: Add failing exploration and proximity tests**

```python
def test_near_duplicate_is_penalized_more_than_diverse_candidate() -> None:
    ranker = HypothesisRanker(seed=7)
    chosen = ranker.select([near_duplicate, diverse], proximity, beta=2.0)
    assert chosen == diverse.id


def test_fixed_seed_produces_stable_selection() -> None:
    assert make_ranker(11).select(items, graph) == make_ranker(11).select(items, graph)
```

- [ ] **Step 2: Run ranking tests**

Run: `uv run pytest tests/test_ranking.py -q`

Expected: proximity or stability test FAILS.

- [ ] **Step 3: Implement BT update plus UCB selection as pure calculations**

```python
class HypothesisRanker:
    def update(self, comparisons: list[tuple[str, str, bool]]) -> None:
        decisive = [(winner, loser) for winner, loser, is_tie in comparisons if not is_tie]
        self._strengths = fit_bradley_terry(self._strengths, decisive)

    def select(self, hypotheses: list[Hypothesis], proximity: ProximityGraph, beta: float = 2.0) -> str:
        if not hypotheses:
            raise ValueError("no hypotheses to select")
        scored = (
            (selection_score(*self.estimate(item.id), proximity.min_distance_to_executed(item), beta=beta), item.id)
            for item in hypotheses
        )
        return max(scored, key=lambda item: (item[0], item[1]))[1]


def selection_score(mean: float, uncertainty: float, min_distance: float, *, beta: float, proximity_weight: float = 0.1) -> float:
    return mean + beta * uncertainty - proximity_weight * (1.0 - min_distance)
```

Ignore ties during BT updates. Keep seeded bootstrap/RNG local to the ranker and do not cache candidate scores.

- [ ] **Step 4: Verify convergence and stability**

Run: `uv run pytest tests/test_ranking.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/experiment/ranking.py tests/test_ranking.py
git commit -m "feat(experiment): implement deterministic hypothesis ranking"
```

## Task 11: Code Execution, Monitoring, Review, and Optional Adapters

**Files:**
- Create: `src/athena/code/backends/codex.py`
- Create: `src/athena/code/backends/qoder.py`
- Modify: `src/athena/code/backends/__init__.py`
- Modify: `src/athena/code/engine.py`
- Modify: `src/athena/code/monitor.py`
- Modify: `src/athena/code/review.py`
- Modify: `tests/test_code_backend.py`
- Modify: `tests/test_code_engine.py`
- Modify: `tests/test_code_monitor.py`
- Modify: `tests/test_code_review.py`

- [ ] **Step 1: Add failing exhaustion, cancellation, and review tests**

```python
async def test_engine_does_not_fabricate_success_after_all_rounds_fail(engine) -> None:
    result = await engine.run(prompt="x", target_dir=".", max_rounds=2)
    assert result.rounds == 2
    assert result.success is False


def test_review_requires_revision_for_undeclared_dependency() -> None:
    verdict = review_diff("+import xgboost", allowed_files={"model.py"}, declared_dependencies=set())
    assert verdict.action == "revise"
```

Also test timeout cancellation, ten-turn stall signal, forbidden `eval.py`/split changes, scope escape, missing output files, adapter import without SDK, and fake adapter routing.

- [ ] **Step 2: Run execution tests**

Run: `uv run pytest tests/test_code_backend.py tests/test_code_engine.py tests/test_code_monitor.py tests/test_code_review.py -q`

Expected: repeated-failure test FAILS because the current engine returns `success=True`.

- [ ] **Step 3: Implement explicit boundaries**

```python
def load_backend(name: Literal["codex", "qoder"], **config: object) -> CodeBackend:
    factories = {"codex": CodexBackend.from_config, "qoder": QoderBackend.from_config}
    return factories[name](config)


def review_diff(diff: str, *, allowed_files: set[str], declared_dependencies: set[str]) -> ReviewVerdict:
    changes = parse_diff(diff)
    if changes.paths & {"eval.py", "splits.json"}:
        return ReviewVerdict(action="reject", reasons=["protected evaluation file changed"])
    reasons = scope_and_dependency_violations(changes, allowed_files, declared_dependencies)
    return ReviewVerdict(action="revise" if reasons else "approve", reasons=reasons)


class AgentMonitor:
    async def watch(self, operation: Awaitable[ExecutionOutput], timeout_s: int = 600) -> WatchResult:
        task = asyncio.create_task(operation)
        try:
            return WatchResult(status="OK", result=await asyncio.wait_for(task, timeout_s))
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return WatchResult(status="TIMEOUT", error=f"timed out after {timeout_s}s")
```

Lazy-import optional SDKs inside adapter construction and raise a stable `BackendUnavailableError`. On timeout cancel and await the task. Return failed `EngineResult` when all rounds fail; pass deterministic review failures into the next revision round.

- [ ] **Step 4: Run all code/agent tests**

Run: `uv run pytest tests/test_code_backend.py tests/test_code_engine.py tests/test_code_monitor.py tests/test_code_review.py -q`

Expected: all tests PASS and no external CLI/service runs.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/code tests/test_code_backend.py tests/test_code_engine.py tests/test_code_monitor.py tests/test_code_review.py
git commit -m "feat(code): enforce execution and review failures"
```

## Task 12: Store-Backed Experiment Orchestration

**Files:**
- Modify: `src/athena/experiment/baseline.py`
- Modify: `src/athena/experiment/pipeline.py`
- Modify: `src/athena/experiment/search_loop.py`
- Modify: `src/athena/experiment/supervisor.py`
- Modify: `src/athena/experiment/validate.py`
- Modify: `src/athena/experiment/report.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_validate_report.py`
- Modify: `tests/test_e2e_ai4ml.py`

- [ ] **Step 1: Add failing checkpoint and phase-boundary tests**

```python
async def test_candidate_tie_and_failure_create_monotonic_checkpoints(orchestrator) -> None:
    events = await orchestrator.run(fake_results=[candidate_win, tie, failed_run])
    assert [event.state_version for event in events] == [1, 2, 3]
    assert [event.payload["status"] for event in events] == ["succeeded", "succeeded", "failed"]


async def test_search_never_receives_test_split(orchestrator) -> None:
    await orchestrator.run_one()
    assert "test" not in orchestrator.fake_backend.context_refs
```

Add pause/resume/stop, budget exhaustion, no-improvement, SOTA update, single active experiment, root-to-SOTA ablation, one final test, and report evidence/p-value tests.

- [ ] **Step 2: Run orchestration tests**

Run: `uv run pytest tests/test_search_workflow.py tests/test_validate_report.py tests/test_e2e_ai4ml.py -q`

Expected: FAIL on store checkpoint or monotonic state version.

- [ ] **Step 3: Implement a small state owner and event values**

```python
@dataclass
class SearchControl:
    pause_gate: asyncio.Event
    stop_requested: bool = False
    state_version: int = 0
    events: list[EventEnvelope] = field(default_factory=list)


class SearchLoop:
    def __init__(self, store: ExperimentStore, ranker: HypothesisRanker, execute: ExecuteExperiment, compare: CompareResults, budget: BudgetSnapshot, mode: RunMode) -> None:
        self._store = store
        self._ranker = ranker
        self._execute = execute
        self._compare = compare
        self._budget = budget
        self._mode = mode
        self._control = SearchControl(pause_gate=ready_event())

    async def run(self) -> list[EventEnvelope]:
        while not self._control.stop_requested and not self._budget.is_exhausted:
            await self._control.pause_gate.wait()
            await run_next_experiment(self)
        return list(self._control.events)

    async def pause(self) -> None:
        self._control.pause_gate.clear()

    async def resume(self) -> None:
        self._control.pause_gate.set()

    async def stop(self) -> None:
        self._control.stop_requested = True
        self._control.pause_gate.set()
```

Supervisor alone translates candidate/baseline/tie/failure into experiment and hypothesis states. Emit the Task 0 `EventEnvelope` with `experiment_id` and status in `payload`; increment `SearchControl.state_version` for each persisted checkpoint. Do not duplicate the version on `Experiment` or the store.

- [ ] **Step 4: Implement validation and report as functions over public APIs**

```python
async def ablate_sota(store: ExperimentStore, execute: ExecuteAblation) -> list[tuple[Hypothesis, EvalResult]]:
    sota_id = await store.best_experiment()
    if sota_id is None:
        raise ValueError("cannot ablate without a SOTA experiment")
    return [(experiment.hypothesis, await execute(experiment)) for experiment in (await experiment_path(store, sota_id))[1:]]


async def run_final_test(store: ExperimentStore, execute: ExecuteFinal) -> EvalResult:
    sota_id = await store.best_experiment()
    if sota_id is None:
        raise ValueError("cannot run final test without a SOTA experiment")
    experiment = await store.get_experiment(sota_id)
    assert experiment is not None
    return await execute(experiment)


async def generate_report(store: ExperimentStore, ablation: Sequence[tuple[Hypothesis, EvalResult]], final: EvalResult, write_text: Callable[[str], ArtifactRef]) -> ArtifactRef:
    return write_text(render_report(await collect_experiments(store), ablation, final))
```

Run: `uv run pytest tests/test_search_workflow.py tests/test_validate_report.py tests/test_e2e_ai4ml.py -q`

Expected: all tests PASS using fake backends only.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/experiment tests/test_search_workflow.py tests/test_validate_report.py tests/test_e2e_ai4ml.py
git commit -m "feat(experiment): orchestrate store-backed research runs"
```

## Task 13: App Server Owns ThreadManager

**Files:**
- Modify: `src/athena/app_server/thread_manager.py`
- Modify: `src/athena/app_server/__init__.py`
- Modify: `test/unit/app_server/test_thread_manager.py`
- Modify: `test/unit/app_server/test_lifecycle.py`
- Delete: `test/unit/test_handlers.py`

- [ ] **Step 1: Add a failing no-legacy-import contract test**

```python
def test_thread_manager_contract_is_owned_by_app_server() -> None:
    source = inspect.getsource(athena.app_server.thread_manager)
    assert "athena.execution" not in source
    assert issubclass(RuntimeThreadManager, ThreadManager)
```

- [ ] **Step 2: Run the App Server suite**

Run: `uv run pytest test/unit/app_server -q`

Expected: the ownership test FAILS on `athena.execution.handlers`.

- [ ] **Step 3: Define the structural contract beside the runtime**

```python
class ThreadManager(ABC):
    @abstractmethod
    async def start(self, session_id: str, context_ref: ArtifactRef) -> AthenaThread:
        raise NotImplementedError

    @abstractmethod
    async def submit(self, thread_id: str, request_ref: ArtifactRef) -> AthenaTurn:
        raise NotImplementedError

    @abstractmethod
    async def fork(self, thread_id: str, after_turn_id: str | None = None) -> AthenaThread:
        raise NotImplementedError

    @abstractmethod
    async def interrupt(self, thread_id: str, turn_id: str, reason: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def events(self, thread_id: str) -> AsyncIterator[ArtifactRef]:
        raise NotImplementedError

    @abstractmethod
    async def get(self, thread_id: str) -> ThreadHandle:
        raise NotImplementedError

    @abstractmethod
    async def aclose(self, reason: str = "server_shutdown") -> None:
        raise NotImplementedError
```

Keep `RuntimeThreadManager` method behavior, event order, race handling, and idempotent close unchanged. Do not migrate the unused legacy queue loop.

- [ ] **Step 4: Run all App Server tests**

Run: `uv run pytest test/unit/app_server -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/app_server/thread_manager.py src/athena/app_server/__init__.py test/unit/app_server/test_thread_manager.py test/unit/app_server/test_lifecycle.py test/unit/test_handlers.py
git commit -m "refactor(app-server): own thread manager contract"
```

## Task 14: IDE Uses Public Persistence APIs

**Files:**
- Modify: `src/athena/ide/handler.py`
- Modify: `tests/test_ide_handler.py`
- Modify: `tests/test_ide_transport.py`
- Modify: `tests/test_ide_e2e.py`

- [ ] **Step 1: Add failing public-boundary and envelope tests**

```python
async def test_tree_save_does_not_read_private_store_state(handler, tmp_path) -> None:
    saved = await handler.dispatch("tree_save", {"path": str(tmp_path / "tree.json")})
    assert saved == {"saved": True, "path": str(tmp_path / "tree.json")}
    assert handler.private_store_accesses == []


async def test_add_node_keeps_stable_experiment_id(handler) -> None:
    result = await handler.dispatch("tree_add_node", {"experiment_id": "exp-7", **node_params})
    assert result["node_id"] == "exp-7"
```

- [ ] **Step 2: Run IDE Python tests**

Run: `uv run pytest tests/test_ide_handler.py tests/test_ide_transport.py tests/test_ide_e2e.py -q`

Expected: FAIL because the handler imports legacy ResearchTree and reads `_sota_id`.

- [ ] **Step 3: Replace tree ownership with store and persistence calls**

```python
class IDEHandler:
    def __init__(self, save_dir: str | None = None, emit: EmitFn | None = None, store: ExperimentStore | None = None):
        self._store = store or InMemoryExperimentStore()
        self._save_path = Path(save_dir or ".athena") / "research_tree.json"
        self._emit = emit
```

Build `Experiment` with the request's stable ID, call `upsert_experiment`, `set_sota`, `snapshot_v1`, `save_v1_snapshot`, and `load_v1_snapshot`. Preserve all four RPC response keys and emit `tree/updated` after save/add.

- [ ] **Step 4: Verify RPC and transport behavior**

Run: `uv run pytest tests/test_ide_handler.py tests/test_ide_transport.py tests/test_ide_e2e.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/athena/ide/handler.py tests/test_ide_handler.py tests/test_ide_transport.py tests/test_ide_e2e.py
git commit -m "refactor(ide): use canonical experiment persistence"
```

## Task 15: Tauri ResearchTree Commands

**Files:**
- Create: `athena-ide/src-tauri/src/commands/tree.rs`
- Modify: `athena-ide/src-tauri/src/commands/mod.rs`
- Modify: `athena-ide/src-tauri/src/lib.rs`
- Modify: `athena-ide/src/lib/tauri-bridge.ts`
- Modify: `athena-ide/src/lib/__tests__/tauri-bridge.test.ts`

- [ ] **Step 1: Add failing command-registration and bridge tests**

```rust
#[test]
fn tree_methods_keep_python_rpc_names() {
    assert_eq!(TREE_GET, "tree_get");
    assert_eq!(TREE_SAVE, "tree_save");
    assert_eq!(TREE_LOAD, "tree_load");
}
```

```ts
it("uses registered tree commands", async () => {
  await treeGet();
  await treeSave("x.json");
  await treeLoad("x.json");
  expect(invoke).toHaveBeenNthCalledWith(1, "tree_get");
});
```

- [ ] **Step 2: Run Rust and bridge tests**

Run: `cargo test --manifest-path athena-ide/src-tauri/Cargo.toml`

Run: `npm test -- --run src/lib/__tests__/tauri-bridge.test.ts` in `athena-ide`.

Expected: FAIL because tree commands are not registered.

- [ ] **Step 3: Add thin passthrough commands**

```rust
#[tauri::command]
pub async fn tree_get(state: State<'_, PythonBridge>) -> Result<Value, String> {
    state.request("tree_get", json!({})).await.map_err(|error| error.to_string())
}
```

Implement `tree_save(path: Option<String>)` and `tree_load(path: Option<String>)` identically, passing only the documented params. Register all three in `generate_handler!`; do not change Python envelopes.

- [ ] **Step 4: Run Tauri and bridge tests**

Run: `cargo test --manifest-path athena-ide/src-tauri/Cargo.toml`

Run: `npm test -- --run src/lib/__tests__/tauri-bridge.test.ts` in `athena-ide`.

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add athena-ide/src-tauri/src/commands/tree.rs athena-ide/src-tauri/src/commands/mod.rs athena-ide/src-tauri/src/lib.rs athena-ide/src/lib/tauri-bridge.ts athena-ide/src/lib/__tests__/tauri-bridge.test.ts
git commit -m "feat(ide): register research tree commands"
```

## Task 16: Optional Integrations and Locked Dependencies

**Files:**
- Modify: `src/athena/integrations/kaggle/client.py`
- Modify: `src/athena/code/runner.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_kaggle_client.py`
- Modify: `tests/test_code_runner.py`

- [ ] **Step 1: Add failing optional-import tests**

```python
def test_core_imports_without_optional_sdks(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "kagglehub", None)
    monkeypatch.setitem(sys.modules, "qoder_agent_sdk", None)
    import athena.code
    import athena.integrations.kaggle


def test_missing_kaggle_dependency_has_stable_error() -> None:
    with pytest.raises(IntegrationUnavailableError, match="kaggle"):
        KaggleClient().get_competition_info()
```

- [ ] **Step 2: Run optional integration tests**

Run: `uv run pytest tests/test_code_runner.py tests/test_kaggle_client.py -q`

Expected: missing dependency behavior or test module FAILS.

- [ ] **Step 3: Declare extras and lazy imports**

```toml
[project.optional-dependencies]
kaggle = ["kagglehub>=0.3"]
notebook = ["nbformat>=5.10", "nbconvert>=7.16"]
qoder = ["qoder-agent-sdk>=0.1"]
```

Import these packages only inside adapter methods. Keep script execution available with base dependencies and test every adapter with fakes.

- [ ] **Step 4: Lock and verify the environment**

Run: `uv lock`

Run: `uv sync --locked`

Run: `uv pip check`

Run: `uv run pytest tests/test_code_runner.py tests/test_kaggle_client.py -q`

Expected: lock/sync/check succeed and tests PASS.

- [ ] **Step 5: Commit dependency files separately from later formatting**

```powershell
git add src/athena/integrations/kaggle/client.py src/athena/code/runner.py tests/test_kaggle_client.py tests/test_code_runner.py pyproject.toml uv.lock
git commit -m "build: lock optional runtime integrations"
```

## Task 17: Canonical Examples, Fake E2E, and Legacy Deletion

**Files:**
- Modify: `examples/ai4ml_pipeline.py`
- Modify: `examples/trial_run.py`
- Modify: `tests/test_e2e_ai4ml.py`
- Modify: `tests/test_prepare_workflow.py`
- Modify: `tests/test_research_tree_ai4ml.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_validate_report.py`
- Delete: `src/athena/execution/`
- Delete: `src/athena/workflows/`
- Delete: `src/athena/core/research/`
- Delete: `src/athena/research/`
- Delete: `src/athena/knowledge/` when present

- [ ] **Step 1: Add failing negative-import and resumed-E2E tests**

```python
@pytest.mark.parametrize("name", [
    "athena.execution", "athena.workflows", "athena.core.research",
    "athena.research", "athena.knowledge", "athena.agents",
])
def test_legacy_namespaces_are_absent(name: str) -> None:
    assert importlib.util.find_spec(name) is None


async def test_two_experiment_run_resumes_from_v1_snapshot(tmp_path) -> None:
    first = await run_fake_pipeline(max_experiments=1)
    await save_v1_snapshot(first.store, tmp_path / "tree.json")
    resumed = await resume_fake_pipeline(tmp_path / "tree.json", max_experiments=1)
    assert len(await resumed.store.list_descendants(first.root_id)) == 2
```

- [ ] **Step 2: Prove deletion is not yet safe**

Run: `rg -n --glob '*.py' '(from|import) athena\.(execution|workflows|core\.research|research|knowledge|agents)' src test tests examples`

Expected: existing import matches are printed.

- [ ] **Step 3: Migrate all consumers, then rerun the static proof**

Use only imports from `athena.core.agent`, `athena.code`, `athena.data`, `athena.retrieval`, `athena.brainstorm`, `athena.evaluation`, `athena.experiment`, `athena.app_server`, and `athena.ide`.

Run: `rg -n --glob '*.py' '(from|import) athena\.(execution|workflows|core\.research|research|knowledge|agents)' src test tests examples`

Expected: exit code 1 and no output.

- [ ] **Step 4: Delete exact legacy paths and verify E2E**

Delete the remaining listed namespaces after Step 3 is clean; `athena.agents` was already removed in Task 2. Do not remove `core/`, `app_server/`, `ide/`, or `memory/`.

Run: `uv run pytest tests/test_e2e_ai4ml.py -m slow -q`

Run: `uv run python examples/ai4ml_pipeline.py`

Run: `uv run python examples/trial_run.py`

Expected: the fake E2E, both examples, and negative-import tests PASS.

- [ ] **Step 5: Commit migrated consumers and deletions**

```powershell
git add examples/ai4ml_pipeline.py examples/trial_run.py tests/test_e2e_ai4ml.py tests/test_prepare_workflow.py tests/test_research_tree_ai4ml.py tests/test_search_workflow.py tests/test_validate_report.py src/athena/execution src/athena/workflows src/athena/core/research src/athena/research
git commit -m "refactor: remove legacy research namespaces"
```

## Task 18: A2.2 Narrow-Ink-Spine Research Studio

**Files:**
- Create: `athena-ide/src/lib/presentation.ts`
- Create: `athena-ide/src/lib/__tests__/presentation.test.ts`
- Create: `athena-ide/src/components/shell/ResearchMasthead.tsx`
- Create: `athena-ide/src/components/conversation/StageProgress.tsx`
- Modify: `athena-ide/src/App.tsx`
- Modify: `athena-ide/src/App.css`
- Modify: `athena-ide/src/styles.css`
- Modify: `athena-ide/src/main.tsx`
- Modify: `athena-ide/src/components/shell/AppShell.tsx`
- Modify: `athena-ide/src/components/shell/SessionSidebar.tsx`
- Modify: `athena-ide/src/components/conversation/ConversationPane.tsx`
- Modify: `athena-ide/src/components/conversation/MessageList.tsx`
- Modify: `athena-ide/src/components/conversation/Composer.tsx`
- Modify: `athena-ide/src/components/cards/IntentPreviewCard.tsx`
- Modify: `athena-ide/src/components/cards/ErrorCard.tsx`
- Modify: `athena-ide/src/components/right-rail/RightRail.tsx`
- Modify: `athena-ide/src/components/context/ContextSurface.tsx`
- Modify: `athena-ide/src/components/__tests__/app-shell.test.tsx`
- Create: `athena-ide/src/components/__tests__/session-sidebar.test.tsx`
- Modify: `athena-ide/src/components/__tests__/conversation-pane.test.tsx`
- Modify: `athena-ide/src/components/__tests__/right-rail.test.tsx`
- Modify: `athena-ide/src/components/__tests__/context-surface.test.tsx`
- Modify: `athena-ide/package.json`
- Modify: `athena-ide/package-lock.json`

- [ ] **Step 1: Add failing pure mapping tests**

```ts
expect(presentStatus("idle")).toEqual({ label: "就绪", tone: "neutral" });
expect(presentStatus("running")).toEqual({ label: "运行中", tone: "active" });
expect(presentStatus("completed")).toEqual({ label: "已完成", tone: "complete" });
expect(presentStatus("paused")).toEqual({ label: "需要处理", tone: "attention" });
expect(presentStatus("error")).toEqual({ label: "需要处理", tone: "attention" });
expect(presentPhase("CODE_GENERATION").current).toBe(2);
expect(presentPhase("UNKNOWN").current).toBeNull();
```

- [ ] **Step 2: Run frontend tests and confirm missing mappings/components**

Run: `npm test -- --run` in `athena-ide`.

Expected: FAIL importing `presentation.ts` or `ResearchMasthead`.

- [ ] **Step 3: Implement pure presentation mapping and slot-only shell**

```ts
const statusPresentation: Record<PipelineStatus, StatusPresentation> = {
  idle: { label: "就绪", tone: "neutral" },
  running: { label: "运行中", tone: "active" },
  completed: { label: "已完成", tone: "complete" },
  paused: { label: "需要处理", tone: "attention" },
  error: { label: "需要处理", tone: "attention" },
};

export function presentStatus(status: PipelineStatus): StatusPresentation {
  return statusPresentation[status];
}

export function presentPhase(phase: PipelinePhase | string | null): PhasePresentation {
  const current = phase === "PREPARE" || phase === "DATA_ANALYSIS" ? 0
    : phase === "IDEA_GENERATION" ? 1
    : phase === "SEARCH" || phase === "CODE_GENERATION" || phase === "EVALUATE" ? 2
    : phase === "VALIDATE" || phase === "REPORT" ? 3
    : null;
  return { current, label: current === null && phase ? "研究进行中" : stageLabels[current ?? 0] };
}

export function AppShell({ masthead, sidebar, conversation, rail, context }: AppShellProps) {
  return <div className="app-shell">{masthead}<div className="workbench">{sidebar}{conversation}{rail}{context}</div></div>;
}
```

`App` remains the sole `usePipeline` consumer and passes narrow display props. Do not add `AppShell.viewModel`, compatibility aliases, a `TopBar`, or another research-tree hook.

- [ ] **Step 4: Implement A2.2 components and accessibility contracts**

Use `ResearchMasthead`, a 176px spine, flexible paper canvas, 240px continuous ledger, and `ContextSurface` returning `null` when closed. Use local `@fontsource/noto-serif-sc`, `lucide-react`, semantic regions, accessible names/tooltips, focus restoration, `:focus-visible`, and reduced motion. Keep cards at 6px or less and do not nest cards.

```tsx
export function ContextSurface({ activePanel, openPanel, closePanel, openerRef }: Props) {
  if (activePanel === null) return null;
  return (
    <section aria-label="研究详情">
      <ContextTabs activePanel={activePanel} onSelect={openPanel} />
      <PanelContent panel={activePanel} />
      <button aria-label="关闭研究详情" title="关闭研究详情" onClick={() => {
        closePanel();
        openerRef.current?.focus();
      }}><X aria-hidden="true" /></button>
    </section>
  );
}
```

- [ ] **Step 5: Run tests and production build**

Run: `npm test -- --run` in `athena-ide`.

Run: `npm run build` in `athena-ide`.

Expected: all tests PASS and the production build succeeds.

- [ ] **Step 6: Commit the visual implementation**

```powershell
git add athena-ide/src/lib/presentation.ts athena-ide/src/lib/__tests__/presentation.test.ts athena-ide/src/components/shell/ResearchMasthead.tsx athena-ide/src/components/conversation/StageProgress.tsx athena-ide/src/App.tsx athena-ide/src/App.css athena-ide/src/styles.css athena-ide/src/main.tsx athena-ide/src/components/shell/AppShell.tsx athena-ide/src/components/shell/SessionSidebar.tsx athena-ide/src/components/conversation/ConversationPane.tsx athena-ide/src/components/conversation/MessageList.tsx athena-ide/src/components/conversation/Composer.tsx athena-ide/src/components/cards/IntentPreviewCard.tsx athena-ide/src/components/cards/ErrorCard.tsx athena-ide/src/components/right-rail/RightRail.tsx athena-ide/src/components/context/ContextSurface.tsx athena-ide/src/components/__tests__/app-shell.test.tsx athena-ide/src/components/__tests__/session-sidebar.test.tsx athena-ide/src/components/__tests__/conversation-pane.test.tsx athena-ide/src/components/__tests__/right-rail.test.tsx athena-ide/src/components/__tests__/context-surface.test.tsx athena-ide/package.json athena-ide/package-lock.json
git commit -m "feat(ide): implement narrow ink spine research studio"
```

## Task 19: Visual QA and Screenshot Review

**Files:**
- Create: `athena-ide/playwright.config.ts`
- Create: `athena-ide/tests/visual/workbench.spec.ts`
- Modify: `athena-ide/package.json`
- Modify: `athena-ide/package-lock.json`
- Modify: `athena-ide/src/App.css` only for defects proven by screenshots

- [ ] **Step 1: Add viewport, overflow, and console tests**

```ts
for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 720 }]) {
  test(`workbench ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("console", message => message.type() === "error" && errors.push(message.text()));
    await page.setViewportSize(viewport);
    await page.goto("/");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    await expect(page).toHaveScreenshot(`workbench-${viewport.width}x${viewport.height}.png`);
  });
}
```

- [ ] **Step 2: Run visual tests before fixes**

Run: `npm run test:visual` in `athena-ide`.

Expected: initial screenshot baselines are produced or a concrete overflow/console assertion FAILS.

- [ ] **Step 3: Inspect both screenshots and fix only observed defects**

Check overlap, horizontal overflow, truncation, layout shift, interaction obstruction, central-canvas dominance, dark-area proportion, continuous-ledger appearance, and detail workspace width. Keep desktop tracks stable with `minmax(0, 1fr)` and responsive rules near 1024px.

- [ ] **Step 4: Re-run screenshots and frontend gates**

Run: `npm run test:visual` in `athena-ide`.

Run: `npm test -- --run` in `athena-ide`.

Run: `npm run build` in `athena-ide`.

Expected: screenshots match approved baselines, no console errors occur, tests PASS, and build succeeds.

- [ ] **Step 5: Open the review artifacts in VS Code and commit**

Run: `code athena-ide/tests/visual athena-ide/src/App.css`

```powershell
git add athena-ide/playwright.config.ts athena-ide/tests/visual/workbench.spec.ts athena-ide/package.json athena-ide/package-lock.json athena-ide/src/App.css
git commit -m "test(ide): add research studio visual gates"
```

## Task 20: Separate Python Formatting and Full Release Gate

**Files:**
- Modify: Python files selected by `black --check` only
- Create: `docs/superpowers/reviews/2026-07-29-athena-full-closeout.md`

- [ ] **Step 1: Capture behavior before formatting**

Run: `uv run pytest -q`

Expected: all Python tests and subtests PASS. Record the exact counts in the review report.

- [ ] **Step 2: Format Python in a standalone change and prove AST equivalence**

Run: `uv run black src test tests examples agent_tool_example.py scripts/export_rust_contract_fixtures.py`

Run: `uv run pytest -q`

Expected: tests retain the same passing behavior.

- [ ] **Step 3: Commit only formatting paths**

Stage only files changed by Black, inspect `git diff --cached --stat`, then commit:

```powershell
git commit -m "style(python): apply project formatting"
```

- [ ] **Step 4: Run the complete validation matrix**

Run: `uv sync --locked`

Run: `uv run pytest`

Run: `rg -n --glob '*.py' '(from|import) athena\.(execution|workflows|core\.research|research|knowledge|agents)' src test tests examples`

Expected: Python tests PASS; the import scan exits 1 with no matches.

Run in `athena-ide`: `npm test -- --run`

Run in `athena-ide`: `npm run build`

Run: `cargo fmt --manifest-path athena-ide/src-tauri/Cargo.toml -- --check`

Run: `cargo clippy --manifest-path athena-ide/src-tauri/Cargo.toml --all-targets -- -D warnings`

Run: `cargo test --manifest-path athena-ide/src-tauri/Cargo.toml`

Run in `athena-rust`: `cargo fmt --all --check`

Run in `athena-rust`: `cargo clippy --workspace --all-targets -- -D warnings`

Run in `athena-rust`: `cargo test --workspace`

Run: `git diff --check`

Expected: every command succeeds.

- [ ] **Step 5: Audit simplicity and all specs**

The review report must list:

```markdown
- Canonical domain classes and persistent attribute counts
- Any count above seven with its tested justification (expected: none)
- Duplicate-state, wrapper-type, stateless-service, and cross-file-hop findings
- Legacy import/deletion proof
- ResearchTree v1 fixture and IDE/Tauri compatibility results
- 1440x900 and 1024x720 screenshot findings
- Python, frontend, Tauri, and Rust command results
- Remaining blockers (expected: none)
```

- [ ] **Step 6: Request independent spec and quality reviews**

Use one reviewer for spec coverage and one for code quality/simplicity. Save both outcomes in `docs/superpowers/reviews/2026-07-29-athena-full-closeout.md`, then run:

`code docs/superpowers/reviews/2026-07-29-athena-full-closeout.md`

Expected: both reviews approve with no unresolved blocker. Fix any finding through the original task implementer, rerun its focused tests, and repeat the relevant review before proceeding.

- [ ] **Step 7: Commit the final review report**

```powershell
git add docs/superpowers/reviews/2026-07-29-athena-full-closeout.md
git commit -m "docs: record Athena full closeout verification"
```

## Final Completion Conditions

- All canonical packages and AI4ML behavior are implemented and tested.
- App Server and IDE import no deleted namespace while preserving their external contracts.
- ResearchTree v1 reads/writes, stable node IDs, atomic replacement, events, and Tauri commands pass.
- The five legacy namespace paths and old agent stub directories are absent.
- A2.2 passes automated, accessibility, and screenshot review at both required viewports.
- Python, frontend, Tauri, and Rust validation is green.
- The final review reports no unresolved blocker and no unjustified domain class over seven persistent attributes.
