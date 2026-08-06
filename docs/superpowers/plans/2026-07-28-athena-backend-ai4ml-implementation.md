# Athena AI4ML Backend Implementation Plan (Codex 对齐版)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the AI4ML pipeline backend with Codex-aligned naming and structure: CodeEngine with multi-round LLM iteration, agent registry/control/role pattern, retrieval + brainstorm, ExperimentStore trait, and orchestration layer.

**Architecture:** Codex-aligned structure: `code/` (execution engine), `agents/` (registry+control+role), `data/` (pure tools with types.py), `retrieval/` + `brainstorm/` (search + generation), `experiment/` (ExperimentStore trait + orchestration), `evaluation/` (metric specification), `integrations/` (external platforms).

**Tech Stack:** Python 3.11+, pydantic-ai, pandas, scipy, qoder-agent-sdk, kagglehub, nbformat, nbconvert

## Global Constraints

- Python >=3.11
- All prompts in English, Chinese only in report post-processing
- All Pydantic `description` fields in English
- `uv sync` must pass after each task
- `uv run pytest` must pass after each task
- No LLM in `data/` or `code/review.py` (deterministic only)
- Each new package MUST have `types.py` (Codex types pattern)
- Store traits use `ExperimentStore` naming (Codex AgentGraphStore pattern)
- `core/`, `app_server/`, `memory/`, `ide/` remain unchanged

## File Structure Map

```
src/athena/
├── code/                       (Task 1-5)
├── agents/                     (Task 6: registry + control + role)  
├── data/                       (Task 7-8: types.py + tools + operations)
├── retrieval/                  (Task 9: types.py + store.py)
├── brainstorm/                 (Task 10: types.py + generate.py)
├── evaluation/                 (Task 11: types.py + evaluator + comparator + factory)
├── experiment/                 (Task 12-16: types.py + store.py + local_store.py + ...)
├── integrations/kaggle/        (Task 17)
└── cleanup                     (Task 18)
```

---

### Task 1: code/types.py + backends/base.py

**Files:** `src/athena/code/types.py`, `src/athena/code/backends/base.py`, `tests/test_code_types.py`

Extract types into `types.py`:

```python
# src/athena/code/types.py
from dataclasses import dataclass, field

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

Update `backends/base.py` to import from `code/types.py`. Tests verify import paths.

**Commit:** `refactor(code): extract types.py (Codex types.rs pattern)`

---

### Task 2-5: code/ engine, runner, monitor, review

Same implementation as before, importing from `code/types.py`. Each gets its own commit.

---

### Task 6: agents/ — registry + control + role

**Files:** `src/athena/agents/types.py`, `registry.py`, `control.py`, `role.py`, `prompts/`

**Key change from old plan:** Split `factory.py` into `registry.py` (create_agent), `control.py` (Agent class with run/interrupt), `role.py` (AgentRole enum).

```python
# agents/types.py
@dataclass
class AgentResult:
    success: bool = False
    output: str = ""
    files: list[str] = field(default_factory=list)
    rounds: int = 0

# agents/role.py
class AgentRole(Enum):
    CODE = "code"
    DATA = "data"
    PLOT = "plot"

# agents/registry.py
def create_agent(role: AgentRole, target_dir: str, engine: CodeEngine) -> Agent: ...

# agents/control.py
class Agent:
    """Lifecycle control (Codex AgentControl pattern)"""
    async def run(self, task: str, context: dict | None = None) -> AgentResult: ...
    def interrupt(self, reason: str) -> None: ...
```

**Tests:** 5 tests verifying registry, control, role creation. Same logic as old plan but with new imports.

---

### Task 7: data/types.py

**Files:** `src/athena/data/types.py`

Extract `DataProfile`, `ColumnSummary`, `ProcessingLog`, `ProcessingRecord` from current `workflows/prepare/data_analysis.py` into `data/types.py`. Add deprecation re-export in old location.

---

### Task 8: data/ tools + operations + notebook

Same as old plan, importing from `data/types.py`.

---

### Task 9: retrieval/ — types.py + store.py

**Files:** `src/athena/retrieval/__init__.py`, `types.py`, `store.py`, `tests/test_retrieval.py`

Same logic as old `knowledge/retrieval.py`, renamed to `retrieval/store.py`.

```python
# retrieval/types.py
@dataclass
class PaperRef: ...
@dataclass
class HFModelRef: ...

# retrieval/store.py
class Retriever:
    async def search_papers(self, query: str, n: int = 5) -> list[PaperRef]: ...
    async def search_models(self, query: str, n: int = 3) -> list[HFModelRef]: ...
```

---

### Task 10: brainstorm/ — types.py + generate.py

**Files:** `src/athena/brainstorm/__init__.py`, `types.py`, `generate.py`, `tests/test_brainstorm.py`

Same logic as old `knowledge/brainstorm.py`.

---

### Task 11: evaluation/ — types.py + evaluator + comparator + factory

**Files:** Create `src/athena/evaluation/`

Merge `core/evaluation.py` + `workflows/prepare/evaluator_factory.py` into:
- `evaluation/types.py` — `MetricDef`, `EvalSpec`, `EvalResult`, `ComparisonVerdict`
- `evaluation/evaluator.py` — `Evaluator`
- `evaluation/comparator.py` — `Comparator`
- `evaluation/factory.py` — `EvaluatorFactory`

Add deprecation re-exports in old locations.

---

### Task 12: experiment/types.py

**Files:** `src/athena/experiment/types.py`

Define `Experiment`, `ExperimentStatus`, `ExperimentOutcome`.

```python
class ExperimentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

---

### Task 13: experiment/store.py + local_store.py

**Files:** `src/athena/experiment/store.py`, `local_store.py`

```python
class ExperimentStore(ABC):
    """Codex AgentGraphStore pattern"""
    @abstractmethod
    async def upsert_experiment(self, exp: Experiment) -> None: ...
    @abstractmethod
    async def list_children(self, parent_id: str, ...) -> list[str]: ...
    @abstractmethod
    async def list_descendants(self, root_id: str, ...) -> list[str]: ...
    @abstractmethod
    async def best_experiment(self) -> str | None: ...
    @abstractmethod
    async def add_hypothesis(self, h: Hypothesis) -> None: ...
    @abstractmethod
    async def pending_hypotheses(self) -> list[Hypothesis]: ...

class InMemoryExperimentStore(ExperimentStore):
    """Memory-backed implementation (Codex in_memory.rs pattern)"""
```

---

### Task 14: experiment/ pipeline + search_loop + validate + report + baseline + ranking

Migrate from `workflows/` and `research/` → `experiment/`. Update imports to use `ExperimentStore`.

---

### Task 15-18: integration + cleanup + deps + e2e

Same as old plan, updated import paths.

---

## Final Verification

- [ ] `uv run pytest` all pass
- [ ] All new packages have `types.py`
- [ ] `experiment/store.py` has `ExperimentStore` trait
- [ ] `agents/` has `registry.py`, `control.py`, `role.py`
- [ ] No imports from deleted directories
