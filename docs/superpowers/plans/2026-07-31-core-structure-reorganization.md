# Core Structure Reorganization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize `src/athena/core` around real functional boundaries while preserving behavior, ResearchTree v2 data, and the public `athena.core` and `athena.core.agent` APIs.

**Architecture:** Keep lightweight cross-domain contracts and ResearchTree in `athena.core`; retain `core.agent` as a real multi-module package; flatten one-module wrappers; move Ranking, Budget, Evaluation, and the local Git implementation to their canonical owners. Migrate repository-owned imports directly instead of leaving compatibility modules for obsolete deep paths.

**Tech Stack:** Python 3.11+, Pydantic 2, asyncio, OpenAI SDK, PydanticAI, pytest, pytest-asyncio, Black, Git subprocesses.

## Global Constraints

- `ResearchTree` remains in `athena.core`.
- Preserve `athena.core` and `athena.core.agent` public exports.
- Do not preserve obsolete deep imports such as `athena.core.schemas`, `athena.core.research`, or `athena.core.gitutils`.
- Preserve ResearchTree `SAVE_VERSION == 2`, its JSON field names, lifecycle rules, SOTA rules, exception behavior, and atomic persistence.
- Preserve Agent signatures, event kinds, scheduling, cancellation, and prompt asset lookup.
- Preserve Tool signatures, event kinds, dispatch ordering, cancellation, and truncation.
- Preserve LocalGitWorkspace review-freeze, tamper detection, commit, and cleanup behavior.
- Preserve the user's unrelated worktree changes; the comment in `core/agent/agent.py` is resolved by the schema/model split and may be removed with that file.
- Remove executable demonstrations from production library modules; examples belong under `examples/`.
- Use canonical imports in production, tests, examples, scripts, and docs; do not add empty wrapper packages.

---

### Task 1: Split Shared Models Into Canonical Owners

**Files:**
- Create: `src/athena/core/contracts.py`
- Create: `src/athena/core/thread_models.py`
- Create: `src/athena/core/research_models.py`
- Create: `src/athena/research/models.py`
- Modify: `src/athena/evaluation/types.py`
- Modify: `src/athena/data/types.py`
- Modify temporarily: `src/athena/core/schemas.py`
- Modify: `tests/test_ai4ml_protocol.py`
- Create: `tests/test_core_model_contracts.py`

**Interfaces:**
- Produces: `core.contracts.{NonBlankText, ArtifactRef, CommitHash, ExecutionId, RunId, HypothesisId, ExperimentId, new_id, EventEnvelope, ErrorRecord}`.
- Produces: `core.thread_models.{AgentTask, AthenaThread, AthenaTurn}`.
- Produces: `core.research_models.{HypothesisStatus, Hypothesis, ExperimentPlan, ExperimentOutcome}`.
- Produces: `research.models.{MetricSpec, TaskMetaData}`.
- Produces: `evaluation.types.{MetricDef, EvalSpec, EvalResult, ComparisonVerdict}`.
- Produces: `data.types.DataCard`.
- Temporary bridge: `core.schemas` re-exports these exact symbols only until Task 6 updates all callers and deletes the file.

- [ ] **Step 1: Record the pre-refactor baseline**

Run before changing any source or test file:

```powershell
uv run pytest -q tests test/unit
```

Expected: pass. If it does not pass, save the exact failing node IDs and error
messages in the task notes; later tasks may proceed only when their targeted
tests are green and the final suite introduces no failure beyond this recorded
set.

- [ ] **Step 2: Add canonical import and behavior tests that fail before the split**

Create `tests/test_core_model_contracts.py`:

```python
from athena.core.contracts import EventEnvelope, new_id
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.thread_models import AgentTask, AthenaThread, AthenaTurn
from athena.data.types import DataCard
from athena.evaluation.types import ComparisonVerdict, EvalResult, EvalSpec, MetricDef
from athena.research.models import MetricSpec, TaskMetaData


def test_canonical_contracts_construct_real_domain_records() -> None:
    assert new_id("run").startswith("run_")
    envelope = EventEnvelope(
        kind="experiment.started",
        source="orchestrator",
        payload={"ref": "artifact://input"},
        state_version=0,
    )
    assert envelope.payload["ref"] == "artifact://input"

    task = AgentTask(task_id="task-1", agent_type="planner", command="plan")
    thread = AthenaThread(
        thread_id="thread-1",
        session_id="session-1",
        status="running",
        context_ref="artifact://context",
    )
    turn = AthenaTurn(
        turn_id="turn-1",
        thread_id=thread.thread_id,
        request_ref="artifact://request",
        status="running",
    )
    assert task.context_refs == []
    assert turn.thread_id == thread.thread_id


def test_domain_models_retain_validation_and_serialization() -> None:
    hypothesis = Hypothesis(
        statement="Use stronger regularization",
        intervention="Increase weight decay",
        expected_effect="Improve validation accuracy",
    )
    plan = ExperimentPlan(
        kind="search",
        change="Increase weight decay",
        run_config_ref="artifact://run-config",
        budget={"trials": 1},
        acceptance_rule="Primary metric improves",
    )
    evaluation = EvalResult(
        experiment_id="experiment-1",
        primary=0.8,
        per_sample="artifact://samples",
    )
    verdict = ComparisonVerdict(winner="candidate", p_value=0.01)
    assert hypothesis.status == "PROPOSED"
    assert plan.kind == "search"
    assert evaluation.primary == 0.8
    assert verdict.winner == "candidate"
```

Update `tests/test_ai4ml_protocol.py` to import identifiers and generic models
from `athena.core.contracts`, and `AgentTask` from
`athena.core.thread_models`.

- [ ] **Step 3: Run the new tests and verify the modules do not exist**

Run:

```powershell
uv run pytest -q tests/test_core_model_contracts.py tests/test_ai4ml_protocol.py
```

Expected: collection fails with `ModuleNotFoundError` for the new owner modules.

- [ ] **Step 4: Create the canonical model modules**

Move definitions without changing fields, defaults, validators, descriptions,
or methods. Use these exact dependency imports:

```python
# core/research_models.py
from athena.core.contracts import ArtifactRef, NonBlankText
from athena.evaluation.types import ComparisonVerdict, EvalResult

# evaluation/types.py
from athena.core.contracts import ArtifactRef

# data/types.py
from athena.core.contracts import ArtifactRef
```

During this task, replace `core/schemas.py` with explicit imports and an
`__all__` containing every former symbol. This is a migration bridge only; do
not copy any model implementation into it.

- [ ] **Step 5: Run model and protocol tests**

Run:

```powershell
uv run pytest -q tests/test_core_model_contracts.py tests/test_ai4ml_protocol.py tests/test_evaluation.py tests/test_retrieval_brainstorm.py
```

Expected: all pass.

- [ ] **Step 6: Commit the canonical model split**

```powershell
git add src/athena/core/contracts.py src/athena/core/thread_models.py src/athena/core/research_models.py src/athena/research/models.py src/athena/evaluation/types.py src/athena/data/types.py src/athena/core/schemas.py tests/test_ai4ml_protocol.py tests/test_core_model_contracts.py
git commit -m "refactor(core): split shared model owners"
```

---

### Task 2: Separate Workspace Contracts From Local Git Execution

**Files:**
- Create: `src/athena/core/workspace.py`
- Create: `src/athena/git_workspace.py`
- Delete: `src/athena/core/gitutils/__init__.py`
- Delete: `src/athena/core/gitutils/workspace.py`
- Modify: `src/athena/core/research/research_tree.py`
- Modify: `src/athena/workflows/prepare/baseline.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `src/athena/workflows/search/code_agent.py`
- Modify: `src/athena/workflows/validate/ablation.py`
- Modify: `tests/test_prepare_workflow.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_validate_report.py`
- Modify: `tests/test_research_tree_v2.py`
- Modify: `test/unit/test_git_workspace.py`
- Modify: `examples/trial_run.py`

**Interfaces:**
- Produces: `core.workspace.BinaryDiffWriter`, `GitWorkspaceError`, `GitWorkBranch`, and abstract `GitWorkspace` with unchanged signatures.
- Produces: `git_workspace.LocalGitWorkspace(GitWorkspace)` with unchanged constructor and async methods.
- Consumes: `core.contracts.{ArtifactRef, CommitHash}`.

- [ ] **Step 1: Update the Git workspace test to the intended owners**

Replace its old import with:

```python
from athena.core.workspace import GitWorkspaceError, GitWorkBranch
from athena.git_workspace import LocalGitWorkspace
```

Update ResearchTree tests and workflow tests to import `GitWorkBranch` or
`GitWorkspace` from `athena.core.workspace`.

- [ ] **Step 2: Run workspace tests and verify import failure**

Run:

```powershell
uv run pytest -q test/unit/test_git_workspace.py tests/test_research_tree_v2.py
```

Expected: collection fails because `athena.core.workspace` and
`athena.git_workspace` do not exist.

- [ ] **Step 3: Extract contracts and move the implementation**

Create `core/workspace.py` from the alias, error, branch model, and abstract
class currently at the top of `core/gitutils/workspace.py`. Create
`git_workspace.py` from `LocalGitWorkspace`, importing its boundary as:

```python
from athena.core.contracts import ArtifactRef, CommitHash
from athena.core.workspace import (
    BinaryDiffWriter,
    GitWorkBranch,
    GitWorkspace,
    GitWorkspaceError,
)
```

Do not move the `__main__` demonstration. Update every caller listed above,
then delete both `core/gitutils` Python files so the directory disappears from
the tracked tree.

- [ ] **Step 4: Run workspace and workflow regression tests**

Run:

```powershell
uv run pytest -q test/unit/test_git_workspace.py tests/test_research_tree_v2.py tests/test_prepare_workflow.py tests/test_search_workflow.py tests/test_validate_report.py
```

Expected: all pass.

- [ ] **Step 5: Commit the workspace boundary**

```powershell
git add src/athena/core/workspace.py src/athena/git_workspace.py src/athena/core/gitutils src/athena/core/research/research_tree.py src/athena/workflows test/unit/test_git_workspace.py tests/test_prepare_workflow.py tests/test_search_workflow.py tests/test_validate_report.py tests/test_research_tree_v2.py examples/trial_run.py
git commit -m "refactor(core): separate git workspace boundary"
```

---

### Task 3: Flatten ResearchTree Without Changing V2 State

**Files:**
- Create by move: `src/athena/core/research_tree.py`
- Delete by move: `src/athena/core/research/research_tree.py`
- Delete: `src/athena/core/research/__init__.py`
- Modify: `src/athena/experiment/__init__.py`
- Modify: `src/athena/research/runtime.py`
- Modify: `src/athena/workflows/prepare/baseline.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `src/athena/workflows/validate/ablation.py`
- Modify: `src/athena/workflows/report/final_report.py`
- Modify: `tests/test_prepare_workflow.py`
- Modify: `tests/test_research_runtime.py`
- Modify: `tests/test_research_tree_v2.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_validate_report.py`
- Modify: `examples/ai4ml_pipeline.py`
- Modify: `examples/trial_run.py`

**Interfaces:**
- Produces: `core.research_tree.{SAVE_VERSION, ExperimentStatus, Experiment, ResearchTree}`.
- Consumes: `core.research_models`, `core.workspace.GitWorkBranch`, and `evaluation.types`.
- Preserves: v2 payload and all public methods on `ResearchTree`.

- [ ] **Step 1: Change the ResearchTree regression test to the flat owner**

Use:

```python
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.evaluation.types import ComparisonVerdict, EvalResult
```

- [ ] **Step 2: Run the test and verify the flat module is missing**

Run:

```powershell
uv run pytest -q tests/test_research_tree_v2.py
```

Expected: collection fails for `athena.core.research_tree`.

- [ ] **Step 3: Move the implementation and update imports**

Run:

```powershell
git mv src/athena/core/research/research_tree.py src/athena/core/research_tree.py
```

Replace the moved module's imports with:

```python
from athena.core.research_models import (
    ExperimentPlan,
    Hypothesis,
    HypothesisStatus,
)
from athena.core.workspace import GitWorkBranch
from athena.evaluation.types import ComparisonVerdict, EvalResult
```

Update every listed caller, preserve the existing `athena.experiment` public
re-export, delete `core/research/__init__.py`, and do not modify tree logic.

- [ ] **Step 4: Verify graph behavior and exact fixture round-trip**

Run:

```powershell
uv run pytest -q tests/test_research_tree_v2.py tests/test_research_runtime.py tests/test_prepare_workflow.py tests/test_search_workflow.py tests/test_validate_report.py
```

Expected: all pass, including exact comparison with
`test/fixtures/research_tree_v2.json`.

- [ ] **Step 5: Commit the flattened ResearchTree**

```powershell
git add src/athena/core/research_tree.py src/athena/core/research src/athena/experiment/__init__.py src/athena/research/runtime.py src/athena/workflows tests examples
git commit -m "refactor(core): flatten research tree module"
```

---

### Task 4: Split Agent Models, Runtime, Provider, And Control

**Files:**
- Create: `src/athena/core/agent/models.py`
- Create by move: `src/athena/core/agent/runtime.py`
- Create by move: `src/athena/core/agent/control.py`
- Delete by move: `src/athena/core/agent/agent.py`
- Delete by move: `src/athena/core/agent/subagent.py`
- Modify: `src/athena/core/agent/provider.py`
- Modify: `src/athena/core/agent/__init__.py`
- Modify: `src/athena/core/__init__.py`
- Modify: `test/unit/test_agent.py`
- Modify: `test/unit/app_server/test_thread_runtime.py`
- Modify: `agent_tool_example.py`

**Interfaces:**
- Produces: `agent.models.{AgentConfig, AgentOutcome, StepOutcome, ToolCall, AgentContext}`.
- Produces: `agent.runtime.{BaseAgent, Agent, agent_runner, create_agent, create_code_agent}`.
- Produces: `agent.control.{AgentResult, AgentEvent, AgentHandle, AgentControl}`.
- Preserves: all current `athena.core.agent.__all__` symbols and all current `athena.core.__all__` symbols.

- [ ] **Step 1: Verify the intended private modules are initially unavailable**

Run this temporary import smoke check; do not add it to pytest:

```powershell
uv run python -c "import athena.core.agent.models, athena.core.agent.runtime, athena.core.agent.control"
```

Expected: `ModuleNotFoundError` before the split.

- [ ] **Step 2: Run Agent characterization tests before the move**

Run:

```powershell
uv run pytest -q test/unit/test_agent.py tests/test_code_agent_assets.py test/unit/app_server/test_thread_runtime.py
```

Expected: all existing behavior tests pass.

- [ ] **Step 3: Extract models and rename implementation modules**

Run:

```powershell
git mv src/athena/core/agent/agent.py src/athena/core/agent/runtime.py
git mv src/athena/core/agent/subagent.py src/athena/core/agent/control.py
```

Move the five dataclasses from `runtime.py` into `models.py` without field
changes. Import Thread/Turn through `core.thread_models` and Tool contracts
through `core.tool_types`. Update dependencies exactly as follows:

```python
# agent/provider.py TYPE_CHECKING
from athena.core.agent.models import AgentConfig

# agent/runtime.py
from athena.core.agent.models import (
    AgentConfig,
    AgentContext,
    AgentOutcome,
    StepOutcome,
    ToolCall,
)

# agent/control.py
from athena.core.agent.models import AgentContext, AgentOutcome
```

Keep `_sampling_loop`, `_dispatch_tool_call`, `agent_runner`, factories, and
formatting helpers together in `runtime.py`. Remove executable demonstration
blocks from all three modules. Update `agent/__init__.py` to expose the same
names from their new owners.

- [ ] **Step 4: Run Agent, Tool, and app-server tests**

Run:

```powershell
uv run pytest -q test/unit/test_agent.py test/unit/test_tool.py tests/test_code_agent_assets.py test/unit/app_server
```

Expected: all pass and prompt discovery still finds the three Markdown assets.

- [ ] **Step 5: Commit the Agent split**

```powershell
git add src/athena/core/agent src/athena/core/__init__.py test/unit/test_agent.py test/unit/app_server/test_thread_runtime.py agent_tool_example.py
git commit -m "refactor(core): split agent runtime responsibilities"
```

---

### Task 5: Move Ranking, Budget, And Evaluation To Their Owners

**Files:**
- Replace implementation: `src/athena/experiment/ranking.py`
- Delete: `src/athena/core/ranking.py`
- Create by move: `src/athena/research/budget.py`
- Delete by move: `src/athena/core/budget.py`
- Delete: `src/athena/core/evaluation.py`
- Modify: `src/athena/research/runtime.py`
- Modify: `src/athena/workflows/search/search_loop.py`
- Modify: `src/athena/execution/supervisor.py`
- Modify: `src/athena/experiment/supervisor.py`
- Modify: `tests/test_budget.py`
- Modify: `tests/test_ranking.py`
- Modify: `tests/test_search_workflow.py`
- Modify: `tests/test_evaluation.py`
- Modify: `examples/trial_run.py`

**Interfaces:**
- Produces: canonical `experiment.ranking.{fit_bradley_terry, selection_score, HypothesisRanker, ProximityGraph}`.
- Produces: canonical `research.budget.{BudgetSnapshot, RunMode}`.
- Consumes: Evaluator and Comparator only through `athena.evaluation`.

- [ ] **Step 1: Update canonical imports**

Remove the old core/experiment identity assertion from `tests/test_ranking.py`.
Change `tests/test_budget.py` to:

```python
from athena.research.budget import BudgetSnapshot
```

- [ ] **Step 2: Run tests and verify the owner expectations fail**

Run:

```powershell
uv run pytest -q tests/test_ranking.py tests/test_budget.py tests/test_evaluation.py
```

Expected: Budget collection fails because its canonical module does not exist;
the existing Ranking behavior tests remain green through the move.

- [ ] **Step 3: Move the canonical implementations and update callers**

Move Ranking's implementation into `experiment/ranking.py`, importing
`Hypothesis` from `core.research_models`. Move `core/budget.py` to
`research/budget.py`. Remove both `__main__` blocks. Delete
`core/evaluation.py` and change the search workflow to:

```python
from athena.evaluation import Comparator
from athena.experiment.ranking import HypothesisRanker, ProximityGraph
from athena.research.budget import BudgetSnapshot, RunMode
```

Update every listed caller; do not leave re-exports in core.

- [ ] **Step 4: Run policy, evaluation, runtime, and workflow tests**

Run:

```powershell
uv run pytest -q tests/test_ranking.py tests/test_budget.py tests/test_evaluation.py tests/test_research_runtime.py tests/test_search_workflow.py
```

Expected: all pass.

- [ ] **Step 5: Commit owner migration**

```powershell
git add src/athena/core/ranking.py src/athena/core/budget.py src/athena/core/evaluation.py src/athena/experiment/ranking.py src/athena/research/budget.py src/athena/research/runtime.py src/athena/workflows/search/search_loop.py src/athena/execution/supervisor.py src/athena/experiment/supervisor.py tests examples/trial_run.py
git commit -m "refactor(core): move domain policy to owners"
```

---

### Task 6: Remove The Schema Bridge And Finish Repository Migration

**Files:**
- Delete: `src/athena/core/schemas.py`
- Modify: every remaining Python caller found by the ownership search below
- Modify: `src/athena/core/tool.py`
- Modify: `src/athena/core/tool_types.py`
- Modify: `docs/architecture/current.md`
- Modify: `docs/architecture/target.md`
- Modify: `docs/architecture/research-runtime-v2.md`
- Modify: `docs/design_research_tree.md`
- Modify: `docs/README.md`
- Create: `tests/test_core_public_api.py`

**Interfaces:**
- Consumes: all canonical owners created in Tasks 1-5.
- Produces: final source tree with no obsolete core module or package.

- [ ] **Step 1: Add an explicit public API regression test**

Create `tests/test_core_public_api.py`:

```python
import athena.core as core
import athena.core.agent as agent


def test_supported_public_surfaces_remain_available() -> None:
    core_names = {
        "BaseTool", "EmitEvent", "ToolContext", "ToolRegistry", "ToolResult",
        "ToolSpec", "Agent", "AgentConfig", "AgentContext", "AgentControl",
        "AgentEvent", "AgentOutcome", "BaseAgent", "StreamEvent", "ToolCall",
        "agent_runner", "create_agent",
    }
    agent_names = {
        "Agent", "AgentConfig", "AgentContext", "AgentControl", "AgentEvent",
        "AgentHandle", "AgentOutcome", "AgentResult", "BaseAgent",
        "ResponsesProvider", "StepOutcome", "StreamEvent", "ToolCall",
        "agent_runner", "create_agent", "create_code_agent",
    }
    assert core_names <= set(core.__all__)
    assert agent_names <= set(agent.__all__)
    for name in core_names:
        assert getattr(core, name) is not None
    for name in agent_names:
        assert getattr(agent, name) is not None
```

- [ ] **Step 2: Run the API test before final cleanup**

Run:

```powershell
uv run pytest -q tests/test_core_public_api.py
```

Expected: pass before cleanup, proving the supported public API baseline.

- [ ] **Step 3: Replace every remaining legacy import**

Run the inventory:

```powershell
rg -n "athena\.core\.(schemas|gitutils|research|ranking|budget|evaluation)|athena\.core\.agent\.(agent|subagent)" src tests test examples scripts agent_tool_example.py
```

Apply this mapping to every result:

```text
ArtifactRef/CommitHash/IDs/EventEnvelope/ErrorRecord/new_id -> core.contracts
AgentTask/AthenaThread/AthenaTurn -> core.thread_models
Hypothesis/HypothesisStatus/ExperimentPlan/ExperimentOutcome -> core.research_models
MetricSpec/TaskMetaData -> research.models
DataCard -> data.types
MetricDef/EvalSpec/EvalResult/ComparisonVerdict -> evaluation.types
Experiment/ExperimentStatus/ResearchTree -> core.research_tree
GitWorkBranch/GitWorkspace/GitWorkspaceError -> core.workspace
LocalGitWorkspace -> git_workspace
Ranking symbols -> experiment.ranking
BudgetSnapshot/RunMode -> research.budget
Evaluator/Comparator -> evaluation
Agent symbols -> core.agent
```

Delete `core/schemas.py`. Remove the executable demonstration blocks from
`core/tool.py` and `core/tool_types.py`. Update architecture owner tables,
source-of-truth paths, dependency diagrams, and verification searches in the
listed documentation files.

- [ ] **Step 4: Verify imports, formatting, and focused regressions**

Run:

```powershell
uv run pytest -q tests/test_core_public_api.py tests/test_ai4ml_protocol.py tests/test_core_model_contracts.py test/unit/test_agent.py test/unit/test_tool.py test/unit/test_git_workspace.py tests/test_research_tree_v2.py tests/test_ranking.py tests/test_budget.py tests/test_evaluation.py
uv run black --check src/athena/core src/athena/git_workspace.py src/athena/experiment/ranking.py src/athena/research/budget.py src/athena/research/models.py src/athena/evaluation/types.py src/athena/data/types.py tests/test_core_public_api.py tests/test_core_model_contracts.py
```

Expected: all tests and formatting checks pass.

- [ ] **Step 5: Confirm the legacy import inventory is empty**

Run:

```powershell
rg -n "athena\.core\.(schemas|gitutils|research|ranking|budget|evaluation)|athena\.core\.agent\.(agent|subagent)" src tests test examples scripts agent_tool_example.py
```

Expected: no matches and `rg` exit code 1.

- [ ] **Step 6: Commit cleanup and documentation**

```powershell
git add src tests test examples scripts agent_tool_example.py docs/README.md docs/architecture docs/design_research_tree.md
git commit -m "refactor(core): remove obsolete module paths"
```

---

### Task 7: Run Full Verification And Inspect The Final Diff

**Files:**
- Verify only; modify a task-owned file only if a newly exposed regression is traced to this reorganization.

**Interfaces:**
- Verifies: the complete Python application, all public imports, the final tree, formatting, and repository hygiene.

- [ ] **Step 1: Run the full Python test gate**

Run:

```powershell
uv run pytest -q tests test/unit
```

Expected: all tests pass, or only failures already recorded in the Task 1
baseline remain and are explicitly reported.

- [ ] **Step 2: Run the full formatting gate**

Run:

```powershell
uv run black --check src/athena tests test/unit examples scripts
```

Expected: pass.

- [ ] **Step 3: Verify structure and ownership searches**

Run:

```powershell
Get-ChildItem src/athena/core -Recurse -File | Select-Object -ExpandProperty FullName
rg -n "athena\.core\.(schemas|gitutils|research|ranking|budget|evaluation)|athena\.core\.agent\.(agent|subagent)" src tests test examples scripts agent_tool_example.py
rg -n "TODO\(advanced-ranking\)" src/athena
```

Expected: the file list matches the design, the legacy-path search is empty,
and exactly one advanced-ranking marker exists in
`src/athena/experiment/ranking.py`.

- [ ] **Step 4: Inspect only this task's diff and whitespace**

Run:

```powershell
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; status contains the planned reorganization plus
the user's pre-existing unrelated changes, with none of those unrelated changes
reverted or newly staged.

- [ ] **Step 5: Record the verification result**

Do not create an empty verification commit. Report exact pass counts, any
recorded baseline failures, the final canonical module map, and the commits
created by Tasks 1-6.
