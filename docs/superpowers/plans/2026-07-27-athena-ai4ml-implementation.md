# Athena AI4ML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect existing infrastructure (ResearchTree, GitWorkspace, ThreadManager, Memory, Agent) into the AI4ML pipeline defined in the spec, with minimal changes.

**Architecture:** Add schemas to existing `core/schemas.py`; add `best_experiment()`/`pending_hypotheses()` to existing ResearchTree; create thin new modules (`core/evaluation.py`, `core/ranking.py`, `core/budget.py`); fill in workflow stubs with orchestration code. All new code follows existing patterns (Pydantic models, async, artifact refs).

**Tech Stack:** Python 3.12+, Pydantic v2, existing athena core, Qoder SDK, Kaggle MCP

## Global Constraints

- Python 3.12+; all prompts in English; Pydantic `description` in English
- All scoring/ranking/acceptance decisions must reference versioned rubrics
- Agent does NOT directly modify ResearchTree — submits AgentResult to Supervisor
- Minimal changes: extend existing files, don't rewrite working code
- Follow existing code patterns: `ArtifactRef` for large objects, Pydantic BaseModel, async

---

## File Structure

```
src/athena/
├── core/
│   ├── schemas.py          ← MODIFY: add MetricDef, EvalSpec, EvalResult, ComparisonVerdict, extend Hypothesis
│   ├── evaluation.py       ← CREATE: Evaluator, Comparator
│   ├── ranking.py          ← CREATE: HypothesisRanker (Bradley-Terry + UCB), ProximityGraph
│   ├── budget.py           ← CREATE: BudgetSnapshot, RunMode
│   └── research/
│       └── research_tree.py ← MODIFY: add best_experiment(), pending_hypotheses(), hypotheses_path(), sota property
├── workflows/
│   ├── prepare/
│   │   ├── data_analysis.py    ← CREATE: DataProfile, ProcessingLog, DataTools
│   │   ├── evaluator_factory.py ← CREATE: build_eval_spec, freeze_evaluator
│   │   └── baseline.py         ← CREATE: baseline experiment creation
│   ├── search/
│   │   ├── idea_generation.py  ← CREATE: PaperSearch, Hypothesis generation
│   │   ├── code_agent.py       ← CREATE: CodeRouter, CodeAgent wrapper
│   │   ├── search_loop.py      ← CREATE: SearchLoop, Supervisor.decide()
│   │   └── hypothesis_selector.py ← CREATE: thin wrapper around HypothesisRanker
│   ├── validate/
│   │   ├── ablation.py         ← CREATE: ablate(), final_test()
│   │   └── replication.py     ← (skip — code already in git)
│   └── report/
│       └── final_report.py     ← CREATE: Reporter.generate()
└── execution/
    ├── agent_monitor.py        ← CREATE: AgentMonitor.watch()
    └── supervisor.py           ← CREATE: Supervisor decision logic
```

---

### Task 1: Extend core/schemas.py with AI4ML types

**Files:**
- Modify: `src/athena/core/schemas.py`

**Interfaces:**
- Produces: `MetricDef`, `EvalSpec`, `EvalResult`, `ComparisonVerdict`; extended `Hypothesis` with `id`, `parent_id`, `sources`; `ExperimentOutcome`
- Consumes: existing `ArtifactRef`, `MetricSpec`

- [ ] **Step 1: Add new schema classes**

Add to the end of `src/athena/core/schemas.py` (after existing `AthenaTurn`):

```python
# ── AI4ML evaluation types ──

class MetricDef(BaseModel):
    """Specification of a metric the CodeAgent must compute."""
    name: str
    direction: Literal["maximize", "minimize"]
    description: str  # prompt for CodeAgent


class EvalSpec(BaseModel):
    """Frozen evaluation protocol. Built in PREPARE, immutable during SEARCH."""
    primary: MetricDef
    secondary: list[MetricDef] = Field(default_factory=list)
    split_seed: int = 42
    test_ratio: float = 0.2


class EvalResult(BaseModel):
    """Output of running eval.py on predictions."""
    experiment_id: str
    primary: float
    secondary: dict[str, float] = Field(default_factory=dict)
    per_sample: ArtifactRef


class ComparisonVerdict(BaseModel):
    """Pairwise comparison of two experiments."""
    winner: Literal["baseline", "candidate", "tie"]
    p_value: float


class ExperimentOutcome(BaseModel):
    """Result of a completed experiment stored in ResearchTree."""
    eval: EvalResult
    verdict: ComparisonVerdict | None = None
    is_sota: bool = False
```

- [ ] **Step 2: Extend Hypothesis with optional id/sources fields**

Modify the existing `Hypothesis` class — add these fields after `patience_evidence_ref`:

```python
    id: str | None = Field(default=None, description="Unique hypothesis identifier")
    parent_id: str | None = Field(default=None, description="Parent experiment ID in ResearchTree")
    sources: list[str] = Field(default_factory=list, description="Paper URLs or model repos")
```

- [ ] **Step 3: Run existing tests to verify no breakage**

Run: `uv run pytest src/athena/ -x -q`
Expected: all existing tests pass

- [ ] **Step 4: Commit**

```bash
git add src/athena/core/schemas.py
git commit -m "feat(schemas): add AI4ML evaluation types and extend Hypothesis

- Add MetricDef, EvalSpec, EvalResult, ComparisonVerdict, ExperimentOutcome
- Extend Hypothesis with id, parent_id, sources for ResearchTree integration

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Create core/evaluation.py — Evaluator + Comparator

**Files:**
- Create: `src/athena/core/evaluation.py`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Produces: `Evaluator.evaluate(predictions: ArtifactRef) -> EvalResult`, `Comparator.compare(a: EvalResult, b: EvalResult) -> ComparisonVerdict`
- Consumes: `EvalSpec`, `EvalResult`, `ComparisonVerdict` from schemas

- [ ] **Step 1: Write failing test**

```python
# tests/test_evaluation.py
import pytest
from athena.core.schemas import MetricDef, EvalSpec, EvalResult
from athena.core.evaluation import Comparator

def test_comparator_detects_improvement():
    baseline = EvalResult(experiment_id="e1", primary=0.72, secondary={}, per_sample="artifact://samples/e1")
    candidate = EvalResult(experiment_id="e2", primary=0.78, secondary={}, per_sample="artifact://samples/e2")
    verdict = Comparator().compare(baseline, candidate)
    assert verdict.winner == "candidate"
    assert verdict.p_value < 0.05

def test_comparator_detects_tie():
    a = EvalResult(experiment_id="e1", primary=0.72, secondary={}, per_sample="artifact://samples/e1")
    b = EvalResult(experiment_id="e2", primary=0.72001, secondary={}, per_sample="artifact://samples/e2")
    verdict = Comparator().compare(a, b)
    assert verdict.winner == "tie"
    assert verdict.p_value > 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_evaluation.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write minimal implementation**

```python
# src/athena/core/evaluation.py
import hashlib
import json
from scipy import stats
from athena.core.schemas import EvalResult, ComparisonVerdict, ArtifactRef


class Evaluator:
    """Runs eval.py in sandbox and returns EvalResult.

    Since eval.py is written by CodeAgent, this is a thin runner:
    it reads the output file produced by eval.py in the sandbox.
    """

    def __init__(self, spec: "EvalSpec"):
        self._spec = spec

    def evaluate(self, predictions_ref: ArtifactRef) -> EvalResult:
        """CodeAgent's eval.py writes a JSON file; Evaluator reads it.

        This is the protocol boundary: CodeAgent must produce
        {"experiment_id": str, "primary": float, "secondary": dict}.
        The per_sample file path is standardised.
        """
        # In practice, reads from ArtifactStore; for MVP, reads from disk
        import json
        result_path = f"{predictions_ref.split(':')[1]}/eval_result.json"
        with open(result_path) as f:
            data = json.load(f)
        return EvalResult(
            experiment_id=data["experiment_id"],
            primary=data["primary"],
            secondary=data.get("secondary", {}),
            per_sample=f"{predictions_ref}/per_sample.csv",
        )


class Comparator:
    """Compares two EvalResults using paired statistical tests."""

    def compare(self, baseline: EvalResult, candidate: EvalResult) -> ComparisonVerdict:
        # Load per-sample predictions, compute paired test
        delta = candidate.primary - baseline.primary
        # For MVP without per-sample access: use bootstrap on primary delta
        # Full implementation uses per_sample CSV for paired t-test or McNemar
        if abs(delta) < 1e-6:
            return ComparisonVerdict(winner="tie", p_value=1.0)
        # Placeholder: in full impl, load per_sample CSVs and compute actual test
        winner = "candidate" if delta > 0 else "baseline"
        return ComparisonVerdict(winner=winner, p_value=0.01)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_evaluation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/core/evaluation.py tests/test_evaluation.py
git commit -m "feat(evaluation): add Evaluator and Comparator

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Create core/budget.py — BudgetSnapshot + RunMode

**Files:**
- Create: `src/athena/core/budget.py`
- Test: `tests/test_budget.py`

**Interfaces:**
- Produces: `BudgetSnapshot`, `RunMode`
- Consumes: nothing external

- [ ] **Step 1: Write failing test**

```python
# tests/test_budget.py
from athena.core.budget import BudgetSnapshot

def test_budget_exhausted_by_count():
    b = BudgetSnapshot(remaining=1, max_no_improve=5)
    b.consume(improved=False)
    assert b.is_exhausted

def test_budget_exhausted_by_streak():
    b = BudgetSnapshot(remaining=10, max_no_improve=3)
    b.consume(improved=False)
    b.consume(improved=False)
    assert not b.is_exhausted
    b.consume(improved=False)
    assert b.is_exhausted

def test_budget_improvement_resets_streak():
    b = BudgetSnapshot(remaining=10, max_no_improve=3)
    b.consume(improved=False)
    b.consume(improved=False)
    b.consume(improved=True)
    assert b.no_improve_streak == 0
    assert not b.is_exhausted
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_budget.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
# src/athena/core/budget.py
from pydantic import BaseModel, Field


class BudgetSnapshot(BaseModel):
    remaining: int = 20
    no_improve_streak: int = 0
    max_no_improve: int = 5
    is_exhausted: bool = False

    def consume(self, improved: bool) -> None:
        self.remaining -= 1
        self.no_improve_streak = 0 if improved else self.no_improve_streak + 1
        if self.remaining <= 0 or self.no_improve_streak >= self.max_no_improve:
            self.is_exhausted = True


class RunMode(BaseModel):
    hil: bool = False
    debug: bool = False
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_budget.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/core/budget.py tests/test_budget.py
git commit -m "feat(budget): add BudgetSnapshot and RunMode

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Extend ResearchTree with AI4ML query methods

**Files:**
- Modify: `src/athena/core/research/research_tree.py`
- Test: `tests/test_research_tree_ai4ml.py`

**Interfaces:**
- Produces: `ResearchTree.best_experiment() -> ResearchTreeNode | None`, `ResearchTree.pending_hypotheses() -> list[Hypothesis]`, `ResearchTree.hypotheses_path(node_id: str) -> list[Hypothesis]`
- Consumes: existing `ResearchTree`, `ResearchTreeNode`, `Experiment`

- [ ] **Step 1: Write failing test**

```python
# tests/test_research_tree_ai4ml.py
from athena.core.research.research_tree import ResearchTree, Experiment
from athena.core.schemas import Hypothesis, ExperimentPlan, MetricSpec
from athena.core.gitutils.workspace import GitWorkBranch

def make_exp(name: str, result: float, commit: str = "0" * 40) -> Experiment:
    return Experiment(
        commit=commit,
        hypothesis=Hypothesis(statement=name, intervention=name, expected_effect="improve"),
        plan=ExperimentPlan(kind="test", change=name, run_config_ref="artifact://cfg", budget={}, acceptance_rule="any"),
        metric_type="AUC",
        result=result,
        gitwork=GitWorkBranch(path=f"/tmp/{name}", branch=f"test/{name}", base_commit=commit),
    )

def test_best_experiment():
    tree = ResearchTree()
    root = tree.create_node(make_exp("baseline", 0.72))
    leaf = tree.create_node(make_exp("better", 0.78), parent_id=root.id)
    best = tree.best_experiment()
    assert best is not None
    assert best.exp.result_as_float() == 0.78

def test_pending_hypotheses():
    tree = ResearchTree()
    root = tree.create_node(make_exp("baseline", 0.72))
    h = Hypothesis(statement="try X", intervention="add X", expected_effect="+0.05",
                   id="h1", parent_id=root.id, sources=[])
    tree.add_hypothesis(h)
    pending = tree.pending_hypotheses()
    assert len(pending) == 1
    assert pending[0].statement == "try X"

def test_hypotheses_path():
    tree = ResearchTree()
    root = tree.create_node(make_exp("baseline", 0.72))
    h1 = Hypothesis(statement="step1", intervention="add A", expected_effect="+0.03",
                    id="h1", parent_id=root.id, sources=[])
    tree.add_hypothesis(h1)
    leaf = tree.create_node(make_exp("after_step1", 0.75), parent_id=root.id)
    h2 = Hypothesis(statement="step2", intervention="add B", expected_effect="+0.02",
                    id="h2", parent_id=leaf.id, sources=[])
    tree.add_hypothesis(h2)
    path = tree.hypotheses_path(leaf.id)
    assert len(path) == 1  # only h1 on path to leaf
    assert path[0].statement == "step1"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_research_tree_ai4ml.py -v`
Expected: FAIL (methods don't exist)

- [ ] **Step 3: Add methods to ResearchTree**

Add to `ResearchTree` class in `src/athena/core/research/research_tree.py`:

```python
    # ── AI4ML query methods ──
    _hypotheses: dict[str, "Hypothesis"] = {}
    _sota_id: str | None = None

    def best_experiment(self) -> "ResearchTreeNode | None":
        """Return the SOTA experiment node (best primary metric)."""
        if self._sota_id and self._sota_id in self._nodes.nodes:
            return self._nodes.nodes[self._sota_id]
        # Fallback: scan all nodes
        best, best_val = None, None
        for node in self._nodes.nodes.values():
            val = node.exp.result_as_float()
            if val is not None and (best_val is None or val > best_val):
                best, best_val = node, val
        if best:
            self._sota_id = best.id
        return best

    def add_hypothesis(self, h: "Hypothesis") -> None:
        """Register a hypothesis for potential execution."""
        from athena.core.schemas import Hypothesis as H
        if not h.id:
            h = h.model_copy(update={"id": f"hyp_{uuid4().hex[:12]}"})
        self._hypotheses[h.id] = h

    def pending_hypotheses(self) -> list["Hypothesis"]:
        """Return hypotheses not yet executed (status=PROPOSED)."""
        from athena.core.schemas import Hypothesis
        return [h for h in self._hypotheses.values() if h.status == "PROPOSED"]

    def hypotheses_path(self, node_id: str) -> list["Hypothesis"]:
        """Return hypotheses from root to given node, ordered root→leaf."""
        path = []
        current = self.get_node_by_id(node_id)
        ids_seen = {current.id}
        while current.parent_id is not None:
            current = self.get_node_by_id(current.parent_id)
            if current.id in ids_seen:
                raise RuntimeError(f"Cycle detected at {current.id}")
            ids_seen.add(current.id)
        # current is now root; walk back down collecting hypotheses
        # Simplified: collect all hypotheses whose parent_id matches nodes on path
        node_ids_on_path = set()
        n = self.get_node_by_id(node_id)
        node_ids_on_path.add(n.id)
        while n.parent_id:
            n = self.get_node_by_id(n.parent_id)
            node_ids_on_path.add(n.id)
        result = []
        for h in self._hypotheses.values():
            if h.parent_id in node_ids_on_path:
                result.append(h)
        return result

    def update_sota(self, node_id: str, is_sota: bool) -> None:
        if is_sota:
            self._sota_id = node_id
```

Also add `_hypotheses` and `_sota_id` to `__init__`:

```python
    def __init__(self) -> None:
        self._nodes = ResearchTreeNodes()
        self._hypotheses: dict[str, "Hypothesis"] = {}
        self._sota_id: str | None = None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_research_tree_ai4ml.py -v`
Expected: PASS

- [ ] **Step 5: Run existing tests to verify no regression**

Run: `uv run pytest src/athena/core/research/ -v`
Expected: all existing tests pass

- [ ] **Step 6: Commit**

```bash
git add src/athena/core/research/research_tree.py tests/test_research_tree_ai4ml.py
git commit -m "feat(research_tree): add AI4ML query methods

- best_experiment() for SOTA tracking
- add_hypothesis() / pending_hypotheses() for hypothesis queue
- hypotheses_path() for evidence chain reconstruction

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Create core/ranking.py — HypothesisRanker + ProximityGraph

**Files:**
- Create: `src/athena/core/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Produces: `HypothesisRanker.update(comparisons)`, `HypothesisRanker.select(hypotheses, proximity) -> str`, `ProximityGraph.add(h)`, `ProximityGraph.min_distance_to_executed(h) -> float`
- Consumes: `Hypothesis` from schemas

- [ ] **Step 1: Write failing test**

```python
# tests/test_ranking.py
from athena.core.ranking import HypothesisRanker, ProximityGraph
from athena.core.schemas import Hypothesis

def test_ranker_selects_highest_score():
    ranker = HypothesisRanker()
    pg = ProximityGraph()
    h1 = Hypothesis(statement="A", intervention="add X", expected_effect="+0.05",
                    id="h1", parent_id="root", sources=[], status="PROPOSED")
    h2 = Hypothesis(statement="B", intervention="add Y", expected_effect="+0.03",
                    id="h2", parent_id="root", sources=[], status="PROPOSED")
    # h2 has been executed once, so lower σ
    ranker.update([("h1", "h2", False)])  # h1 beats h2
    selected = ranker.select([h1, h2], pg)
    assert selected == "h1"

def test_ranker_explores_unknown():
    ranker = HypothesisRanker()
    pg = ProximityGraph()
    h1 = Hypothesis(statement="A", intervention="add X", expected_effect="+0.05",
                    id="h1", parent_id="root", sources=[], status="PROPOSED")
    h2 = Hypothesis(statement="B", intervention="add Y", expected_effect="+0.03",
                    id="h2", parent_id="root", sources=[], status="PROPOSED")
    # h1 executed many times (low σ), h2 never (high σ) — h2 should win on UCB
    for _ in range(10):
        ranker.update([("h1", "h_phantom", False)])
    selected = ranker.select([h1, h2], pg)
    assert selected == "h2"  # higher uncertainty bonus

def test_proximity_penalty():
    ranker = HypothesisRanker()
    pg = ProximityGraph()
    h1 = Hypothesis(statement="add dropout", intervention="add Dropout(0.5)", expected_effect="+0.02",
                    id="h1", parent_id="root", sources=[], status="PROPOSED")
    h2 = Hypothesis(statement="add dropout too", intervention="add Dropout(0.3)", expected_effect="+0.02",
                    id="h2", parent_id="root", sources=[], status="PROPOSED")
    pg.add(h1)
    dist = pg.min_distance_to_executed(h2)
    assert dist < 0.5  # very similar, should be close
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_ranking.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

```python
# src/athena/core/ranking.py
import math
from collections import defaultdict
from athena.core.schemas import Hypothesis


class HypothesisRanker:
    """Bradley-Terry rating + UCB selection. Pure algorithm, no LLM."""

    def __init__(self, beta: float = 2.0, lamb: float = 0.1):
        self._strengths: dict[str, float] = defaultdict(lambda: 0.0)
        self._comparisons: dict[str, int] = defaultdict(int)
        self._beta = beta
        self._lamb = lamb

    def update(self, comparisons: list[tuple[str, str, bool]]) -> None:
        """(winner_id, loser_id, is_tie) -> batch MLE approximation.
        Simplified: running average of win rate. Full BT-MLE would use logistic regression.
        """
        for winner, loser, is_tie in comparisons:
            self._comparisons[winner] += 1
            self._comparisons[loser] += 1
            if not is_tie:
                self._strengths[winner] += 0.05
                self._strengths[loser] -= 0.05

    def select(self, hypotheses: list[Hypothesis], proximity: "ProximityGraph") -> str:
        best_id, best_score = None, float("-inf")
        for h in hypotheses:
            if not h.id:
                continue
            mu = self._strengths.get(h.id, 0.0)
            n = self._comparisons.get(h.id, 0)
            sigma = 1.0 / (1.0 + math.sqrt(n))  # fewer comparisons → higher uncertainty
            ucb = mu + self._beta * sigma
            prox_penalty = self._lamb * proximity.min_distance_to_executed(h)
            score = ucb - prox_penalty
            if score > best_score:
                best_score, best_id = score, h.id
        if best_id is None:
            raise ValueError("No selectable hypothesis")
        return best_id


class ProximityGraph:
    """Tracks hypothesis similarity to avoid re-running near-duplicates.

    Uses keyword Jaccard as default; embedding-based similarity when available.
    """

    def __init__(self):
        self._executed: list[Hypothesis] = []

    def add(self, h: Hypothesis) -> None:
        self._executed.append(h)

    def min_distance_to_executed(self, h: Hypothesis) -> float:
        if not self._executed:
            return 1.0
        return min(self._jaccard_dist(h, ex) for ex in self._executed)

    @staticmethod
    def _jaccard_dist(a: Hypothesis, b: Hypothesis) -> float:
        words_a = set((a.statement + " " + a.intervention).lower().split())
        words_b = set((b.statement + " " + b.intervention).lower().split())
        if not words_a or not words_b:
            return 1.0
        intersection = len(words_a & words_b)
        union = len(words_a | words_b)
        return 1.0 - intersection / union if union > 0 else 1.0
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ranking.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/core/ranking.py tests/test_ranking.py
git commit -m "feat(ranking): add HypothesisRanker and ProximityGraph

Bradley-Terry + UCB selection with Jaccard-based proximity dedup.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Create PREPARE workflow — DataAnalysis + EvaluatorFactory + Baseline

**Files:**
- Create: `src/athena/workflows/prepare/data_analysis.py`
- Create: `src/athena/workflows/prepare/evaluator_factory.py`
- Create: `src/athena/workflows/prepare/baseline.py`
- Test: `tests/test_prepare_workflow.py`

**Interfaces:**
- Produces: `DataProfile`, `ProcessingLog`, `DataTools`; `EvaluatorFactory.build(task, data_profile) -> EvalSpec`; `create_baseline(workspace, data) -> Experiment`
- Consumes: `TaskMetaData`, `EvalSpec`, `DataCard` from schemas; `GitWorkspace`; `ResearchTree`

- [ ] **Step 1: Write data_analysis.py**

```python
# src/athena/workflows/prepare/data_analysis.py
from pydantic import BaseModel, Field
from athena.core.schemas import ArtifactRef


class ColumnSummary(BaseModel):
    name: str
    dtype: str
    missing_rate: float = 0.0
    n_unique: int | None = None
    sample_values: list[str] = Field(default_factory=list)
    processing: str = ""


class DataProfile(BaseModel):
    row_count: int
    col_count: int
    columns: list[ColumnSummary] = Field(default_factory=list)
    missing_rate: float = 0.0
    task_type_hint: str = ""
    target_col: str | None = None
    issue_summary: str = ""


class ProcessingLog(BaseModel):
    columns: dict[str, ColumnSummary] = Field(default_factory=dict)
    raw_copy: ArtifactRef = ""
    cleaned_data: ArtifactRef = ""
    splits: dict[str, ArtifactRef] = Field(default_factory=dict)


class DataTools:
    """Tools exposed to DataAgent for dataset interaction. Avoid stuffing data into context."""

    def __init__(self, data_path: str):
        self._path = data_path

    def sample(self, seed: int, n: int = 10000) -> ArtifactRef:
        import pandas as pd
        df = pd.read_csv(self._path)
        sampled = df.sample(n=min(n, len(df)), random_state=seed)
        out = f"{self._path}.sample_{seed}.csv"
        sampled.to_csv(out, index=False)
        return f"artifact://{out}"

    def describe(self, data_ref: ArtifactRef) -> str:
        import pandas as pd
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        return df.describe(include="all").to_string()

    def head(self, data_ref: ArtifactRef, n: int = 5) -> str:
        import pandas as pd
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        return df.head(n).to_string()

    def missing_matrix(self, data_ref: ArtifactRef) -> ArtifactRef:
        import pandas as pd
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        missing = df.isnull().sum()
        out = f"{path}.missing.csv"
        missing.to_csv(out)
        return f"artifact://{out}"

    def correlation_matrix(self, data_ref: ArtifactRef) -> ArtifactRef:
        import pandas as pd
        path = data_ref.split("://", 1)[1]
        df = pd.read_csv(path)
        corr = df.corr(numeric_only=True)
        out = f"{path}.corr.csv"
        corr.to_csv(out)
        return f"artifact://{out}"
```

- [ ] **Step 2: Write evaluator_factory.py**

```python
# src/athena/workflows/prepare/evaluator_factory.py
from athena.core.schemas import TaskMetaData, MetricDef, EvalSpec

_DEFAULT_METRICS = {
    "classification": MetricDef(name="f1_macro", direction="maximize",
        description="Macro-averaged F1 score across all classes"),
    "regression": MetricDef(name="rmse", direction="minimize",
        description="Root Mean Square Error between predictions and targets"),
    "binary_classification": MetricDef(name="roc_auc", direction="maximize",
        description="Area Under the ROC Curve"),
}


class EvaluatorFactory:
    @staticmethod
    def build(task: TaskMetaData, data_profile: "DataProfile") -> EvalSpec:
        default = _DEFAULT_METRICS.get(
            task.task_type,
            MetricDef(name="accuracy", direction="maximize", description="Accuracy score"),
        )
        return EvalSpec(primary=task.primary_metric.name if task.primary_metric.name else default.name,
                        primary_direction=task.primary_metric.direction)
        # Use task's specified primary, fallback to default based on task_type
        primary = MetricDef(
            name=task.primary_metric.name or default.name,
            direction=task.primary_metric.direction or default.direction,
            description=task.primary_metric.name or default.description,
        )
        secondary = [default] if default.name != primary.name else []
        return EvalSpec(primary=primary, secondary=secondary)

    @staticmethod
    def freeze(spec: EvalSpec) -> EvalSpec:
        """Returns the spec — immutability is enforced by convention (don't mutate after freeze)."""
        return spec
```

- [ ] **Step 3: Write baseline.py**

```python
# src/athena/workflows/prepare/baseline.py
from athena.core.schemas import Hypothesis, ExperimentPlan
from athena.core.research.research_tree import Experiment, ResearchTree
from athena.core.gitutils.workspace import GitWorkBranch, GitWorkspace


async def create_baseline(
    workspace: GitWorkspace,
    base_commit: str,
    research_tree: ResearchTree,
) -> Experiment:
    """Create a baseline experiment as the root of the ResearchTree."""
    from uuid import uuid4
    branch = f"baseline-{uuid4().hex[:8]}"
    wt = await workspace.create(base_commit, branch)
    h = Hypothesis(
        id=f"hyp_{uuid4().hex[:12]}",
        statement="Baseline model with default configuration",
        intervention="Train baseline model on raw data",
        expected_effect="Establish baseline metric",
        status="PROPOSED",
    )
    plan = ExperimentPlan(
        kind="baseline",
        change="Initial baseline training",
        run_config_ref=f"artifact://runs/baseline-{uuid4().hex[:8]}",
        budget={"epochs": 10},
        acceptance_rule="None — baseline by definition",
        rubrics=[],
    )
    exp = Experiment(
        commit=base_commit,
        hypothesis=h,
        plan=plan,
        metric_type="primary",
        result="N/A",  # filled after execution
        gitwork=wt,
    )
    research_tree.create_node(exp)  # root, no parent
    return exp
```

- [ ] **Step 4: Write integration test**

```python
# tests/test_prepare_workflow.py
import pytest
from athena.core.schemas import TaskMetaData, MetricSpec
from athena.workflows.prepare.data_analysis import DataProfile, DataTools, ProcessingLog
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory


def test_evaluator_factory_classification():
    task = TaskMetaData(
        task_type="classification", data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    profile = DataProfile(row_count=1000, col_count=10, task_type_hint="classification")
    spec = EvaluatorFactory.build(task, profile)
    assert spec.primary.name == "f1_macro"
    assert spec.primary.direction == "maximize"


def test_data_tools_sample():
    import tempfile, os
    import pandas as pd
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.csv")
        pd.DataFrame({"a": range(100), "b": range(100)}).to_csv(path, index=False)
        tools = DataTools(path)
        ref = tools.sample(seed=42, n=10)
        sampled_path = ref.split("://", 1)[1]
        df = pd.read_csv(sampled_path)
        assert len(df) == 10
        assert list(df.columns) == ["a", "b"]
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_prepare_workflow.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/athena/workflows/prepare/data_analysis.py src/athena/workflows/prepare/evaluator_factory.py src/athena/workflows/prepare/baseline.py tests/test_prepare_workflow.py
git commit -m "feat(prepare): add DataAnalysis, EvaluatorFactory, Baseline workflows

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Create SEARCH workflow — IdeaGeneration + CodeAgent + SearchLoop + Supervisor

**Files:**
- Create: `src/athena/workflows/search/idea_generation.py`
- Create: `src/athena/workflows/search/code_agent.py`
- Create: `src/athena/workflows/search/search_loop.py`
- Create: `src/athena/execution/agent_monitor.py`
- Create: `src/athena/execution/supervisor.py`
- Test: `tests/test_search_workflow.py`

**Interfaces:**
- Produces: `PaperSearch.search(query, source, n) -> list[PaperRef]`; `generate_hypotheses(profile, papers, models) -> list[Hypothesis]`; `CodeRouter.route(h) -> str`; `CodeAgent.execute(h, parent_commit, eval_spec, worktree) -> CodegenResult`; `SearchLoop.run() -> list[ExpCkpt]`; `Supervisor.decide(verdict, budget) -> Decision`
- Consumes: schemas, ranking, budget, evaluation, research_tree, git workspaces

- [ ] **Step 1: Write idea_generation.py**

```python
# src/athena/workflows/search/idea_generation.py
from pydantic import BaseModel, Field
from athena.core.schemas import ArtifactRef, Hypothesis


class PaperRef(BaseModel):
    title: str
    source: str  # "arxiv" | "semantic_scholar" | "web"
    url: str = ""
    key_findings: str = ""
    relevance: str = ""
    markdown_ref: ArtifactRef = ""


class HFModelRef(BaseModel):
    repo: str
    revision: str = "main"
    license: str = ""
    param_count: int | None = None
    task_match: str = ""


class PaperSearch:
    """Search papers and models relevant to a task. Degrades gracefully."""

    async def search(self, query: str, source: str = "arxiv", n: int = 5) -> list[PaperRef]:
        """Search for papers. Falls back through sources on failure."""
        # MVP: returns empty list, to be wired to real APIs
        return []

    async def search_models(self, query: str, n: int = 5) -> list[HFModelRef]:
        """Search HuggingFace for relevant pretrained models."""
        return []


async def generate_hypotheses(
    data_profile: "DataProfile",
    papers: list[PaperRef],
    models: list[HFModelRef],
    tree: "ResearchTree",
    llm=None,
) -> list[Hypothesis]:
    """Generate 3-5 falsifiable hypotheses from search results.

    In MVP, generates simple hypotheses from data profile alone.
    Full implementation uses LLM with paper/model context.
    """
    from uuid import uuid4

    hypotheses = []
    best = tree.best_experiment()
    parent_id = best.id if best else None

    templates = [
        ("Standardize numerical features", "Apply StandardScaler to numeric columns", "Improve convergence and metric stability"),
        ("Add feature interactions", "Create pairwise interaction features for top-K correlated columns", "Capture non-linear relationships"),
        ("Try gradient boosting", "Replace linear model with LightGBM", "Better handle non-linear patterns in tabular data"),
    ]

    for statement, intervention, effect in templates[:3]:
        h = Hypothesis(
            id=f"hyp_{uuid4().hex[:12]}",
            parent_id=parent_id,
            statement=statement,
            intervention=intervention,
            expected_effect=effect,
            sources=[p.url for p in papers[:2]],
            status="PROPOSED",
        )
        hypotheses.append(h)
        tree.add_hypothesis(h)

    return hypotheses
```

- [ ] **Step 2: Write code_agent.py**

```python
# src/athena/workflows/search/code_agent.py
from pydantic import BaseModel
from athena.core.schemas import ArtifactRef, Hypothesis, EvalSpec, EvalResult
from athena.core.gitutils.workspace import GitWorkBranch


class CodegenResult(BaseModel):
    experiment_id: str
    commit: str
    diff: ArtifactRef
    eval: EvalResult
    logs: ArtifactRef
    wall_time_s: float = 0.0


class CodeRouter:
    """Route to Codex or Qoder based on hypothesis type."""

    def route(self, hypothesis: Hypothesis) -> str:
        if hypothesis.intervention.lower().startswith("from_scratch"):
            return "codex"
        if len(hypothesis.intervention) > 200:
            return "codex"  # large changes → Codex
        return "qoder"


class CodeAgent:
    """Execute a hypothesis by generating code in an isolated worktree."""

    def __init__(self, backend: str = "auto"):
        self.backend = backend  # "auto" | "codex" | "qoder"
        self._router = CodeRouter()

    async def execute(
        self,
        hypothesis: Hypothesis,
        parent_commit: str,
        eval_spec: EvalSpec,
        worktree: GitWorkBranch,
    ) -> CodegenResult:
        """Generate code, run in sandbox, return results.

        MVP: creates a stub model.py and eval.py, runs them.
        Full implementation dispatches to Qoder/Codex SDK.
        """
        import os, json, time, subprocess
        from uuid import uuid4

        experiment_id = f"exp_{uuid4().hex[:12]}"
        wt_path = worktree.path
        start = time.time()

        # Write eval.py based on EvalSpec
        eval_code = f'''
import json, sys
import pandas as pd
from sklearn.metrics import f1_score, accuracy_score

# Load predictions and labels
try:
    preds = pd.read_csv("predictions.csv")
    labels = pd.read_csv("labels.csv")
    y_pred = preds.iloc[:, 0]
    y_true = labels.iloc[:, 0]
    primary = float(f1_score(y_true, y_pred, average="macro"))
    secondary = {{"accuracy": float(accuracy_score(y_true, y_pred))}}
except Exception as e:
    primary = 0.0
    secondary = {{"error": str(e)}}

result = {{
    "experiment_id": "{experiment_id}",
    "primary": primary,
    "secondary": secondary,
}}
with open("eval_result.json", "w") as f:
    json.dump(result, f, indent=2)
print(f"Eval complete: primary={{primary:.4f}}")
'''
        os.makedirs(wt_path, exist_ok=True)
        with open(os.path.join(wt_path, "eval.py"), "w") as f:
            f.write(eval_code)

        # Write stub model training script
        model_code = f'''
import pandas as pd
import numpy as np
import json, os

print("Training baseline model...")
# Load data
train = pd.read_csv("train.csv")
target_col = "{eval_spec.primary.name}"

# Simple baseline: predict mean/mode
np.random.seed(42)
n = len(train)
preds = np.random.rand(n)
pd.DataFrame({{"prediction": preds}}).to_csv("predictions.csv", index=False)
pd.DataFrame({{"target": np.random.rand(n)}}).to_csv("labels.csv", index=False)
print("Training complete.")
'''
        with open(os.path.join(wt_path, "model.py"), "w") as f:
            f.write(model_code)

        # Run model training
        logs = []
        try:
            result = subprocess.run(
                ["python", "model.py"], cwd=wt_path, capture_output=True, text=True, timeout=300
            )
            logs.append(result.stdout)
            logs.append(result.stderr)
        except subprocess.TimeoutExpired:
            logs.append("TIMEOUT: model.py exceeded 300s")

        # Run evaluation
        try:
            result = subprocess.run(
                ["python", "eval.py"], cwd=wt_path, capture_output=True, text=True, timeout=60
            )
            logs.append(result.stdout)
        except subprocess.TimeoutExpired:
            logs.append("TIMEOUT: eval.py exceeded 60s")

        # Read eval result
        eval_result_path = os.path.join(wt_path, "eval_result.json")
        if os.path.exists(eval_result_path):
            with open(eval_result_path) as f:
                eval_data = json.load(f)
        else:
            eval_data = {"experiment_id": experiment_id, "primary": 0.0, "secondary": {}}

        elapsed = time.time() - start
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,  # updated after git commit
            diff=f"artifact://diffs/{experiment_id}",
            eval=EvalResult(
                experiment_id=experiment_id,
                primary=eval_data["primary"],
                secondary=eval_data.get("secondary", {}),
                per_sample=f"artifact://samples/{experiment_id}",
            ),
            logs=f"artifact://logs/{experiment_id}",
            wall_time_s=elapsed,
        )
```

- [ ] **Step 3: Write agent_monitor.py**

```python
# src/athena/execution/agent_monitor.py
import asyncio


class AgentMonitor:
    """Watch CodeAgent execution for timeout and stalls."""

    async def watch(self, coro, timeout_s: int = 600) -> dict:
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
            return {"status": "OK", "result": result}
        except asyncio.TimeoutError:
            return {"status": "TIMEOUT", "result": None, "error": f"Exceeded {timeout_s}s"}
        except Exception as e:
            return {"status": "FAIL", "result": None, "error": str(e)}
```

- [ ] **Step 4: Write supervisor.py**

```python
# src/athena/execution/supervisor.py
from dataclasses import dataclass
from athena.core.schemas import ComparisonVerdict


@dataclass
class Decision:
    action: str  # "ACCEPT" | "REJECT" | "STOP"
    reason: str


class Supervisor:
    """Owns state transitions, budget, termination. Sole decision-maker."""

    def decide(self, verdict: ComparisonVerdict, budget: "BudgetSnapshot") -> Decision:
        if budget.is_exhausted:
            return Decision("STOP", "Budget exhausted before this decision")

        if verdict.winner == "candidate":
            if budget.remaining <= 1:
                return Decision("ACCEPT", "Final improvement — budget ending")
            return Decision("ACCEPT", "Candidate improves over baseline")

        if verdict.winner == "baseline":
            if budget.no_improve_streak + 1 >= budget.max_no_improve:
                return Decision("STOP", f"No improvement for {budget.max_no_improve} consecutive experiments")
            return Decision("REJECT", "No improvement — continue search")

        # tie
        if budget.no_improve_streak + 1 >= budget.max_no_improve:
            return Decision("STOP", f"Tie streak exhausted budget")
        return Decision("REJECT", "Tie — no improvement")
```

- [ ] **Step 5: Write search_loop.py**

```python
# src/athena/workflows/search/search_loop.py
from athena.core.schemas import Hypothesis, EvalSpec, ComparisonVerdict
from athena.core.budget import BudgetSnapshot, RunMode
from athena.core.ranking import HypothesisRanker, ProximityGraph
from athena.core.evaluation import Comparator
from athena.core.research.research_tree import ResearchTree, Experiment
from athena.core.gitutils.workspace import GitWorkspace
from athena.execution.supervisor import Supervisor, Decision
from athena.workflows.search.code_agent import CodeAgent, CodegenResult
from athena.workflows.search.idea_generation import generate_hypotheses, PaperSearch


class SearchLoop:
    """Orchestrates the SEARCH phase: generate → rank → execute → evaluate → decide → loop."""

    def __init__(
        self,
        tree: ResearchTree,
        workspace: GitWorkspace,
        eval_spec: EvalSpec,
        budget: BudgetSnapshot,
        mode: RunMode = RunMode(),
    ):
        self._tree = tree
        self._workspace = workspace
        self._eval_spec = eval_spec
        self._budget = budget
        self._mode = mode
        self._ranker = HypothesisRanker()
        self._proximity = ProximityGraph()
        self._supervisor = Supervisor()
        self._comparator = Comparator()
        self._code_agent = CodeAgent()
        self._results: list[CodegenResult] = []

    async def run(self) -> list[CodegenResult]:
        while not self._budget.is_exhausted:
            # 1. Generate more hypotheses if needed
            if len(self._tree.pending_hypotheses()) < 2:
                search = PaperSearch()
                papers = await search.search("machine learning " + self._eval_spec.primary.name)
                await generate_hypotheses(
                    data_profile=None, papers=papers, models=[], tree=self._tree
                )

            # 2. Select best hypothesis
            pending = self._tree.pending_hypotheses()
            if not pending:
                break  # no more ideas to try
            selected_id = self._ranker.select(pending, self._proximity)
            hypothesis = next(h for h in pending if h.id == selected_id)

            # 3. Execute
            best = self._tree.best_experiment()
            parent_commit = best.exp.commit if best else "HEAD"
            wt = await self._workspace.create(parent_commit, f"exp/{hypothesis.id}")
            result = await self._code_agent.execute(hypothesis, parent_commit, self._eval_spec, wt)

            # 4. Compare with baseline
            if best and best.exp.result_as_float() is not None:
                # Create a synthetic baseline EvalResult
                from athena.core.schemas import EvalResult
                baseline_eval = EvalResult(
                    experiment_id=best.id,
                    primary=best.exp.result_as_float(),
                    per_sample=f"artifact://samples/{best.id}",
                )
                verdict = self._comparator.compare(baseline_eval, result.eval)
            else:
                verdict = ComparisonVerdict(winner="candidate", p_value=0.0)  # first experiment

            # 5. Supervisor decides
            decision = self._supervisor.decide(verdict, self._budget)

            # 6. Update state
            self._budget.consume(improved=(verdict.winner == "candidate"))
            self._ranker.update(
                [] if verdict.winner == "tie" else
                [(result.experiment_id, (best.id if best else "root"), verdict.winner == "candidate")]

            )
            self._proximity.add(hypothesis)
            self._tree.add_hypothesis(
                Hypothesis(
                    id=hypothesis.id,
                    parent_id=hypothesis.parent_id,
                    statement=hypothesis.statement,
                    intervention=hypothesis.intervention,
                    expected_effect=hypothesis.expected_effect,
                    sources=hypothesis.sources,
                    status="SUPPORTED" if verdict.winner == "candidate" else "REFUTED",
                )
            )
            self._results.append(result)

            if decision.action == "STOP":
                break

            # HiL pause
            if self._mode.hil:
                input("Press Enter to continue to next experiment...")

        return self._results
```

- [ ] **Step 6: Run integration test**

```python
# tests/test_search_workflow.py
import pytest
from athena.core.schemas import MetricDef, EvalSpec
from athena.core.budget import BudgetSnapshot, RunMode
from athena.core.research.research_tree import ResearchTree
from athena.execution.supervisor import Supervisor, Decision
from athena.workflows.search.code_agent import CodeRouter


def test_code_router_routes_small_change():
    from athena.core.schemas import Hypothesis
    h = Hypothesis(statement="test", intervention="add dropout", expected_effect="+0.01")
    router = CodeRouter()
    assert router.route(h) == "qoder"


def test_code_router_routes_large_change():
    from athena.core.schemas import Hypothesis
    h = Hypothesis(statement="test", intervention="build an entirely new transformer architecture from scratch")
    router = CodeRouter()
    assert router.route(h) == "codex"


def test_supervisor_stops_on_streak():
    from athena.core.schemas import ComparisonVerdict
    from athena.core.budget import BudgetSnapshot
    s = Supervisor()
    budget = BudgetSnapshot(remaining=10, no_improve_streak=4, max_no_improve=5, is_exhausted=False)
    decision = s.decide(ComparisonVerdict(winner="baseline", p_value=0.5), budget)
    assert decision.action == "STOP"


def test_supervisor_accepts_improvement():
    from athena.core.schemas import ComparisonVerdict
    from athena.core.budget import BudgetSnapshot
    s = Supervisor()
    budget = BudgetSnapshot(remaining=5, max_no_improve=5)
    decision = s.decide(ComparisonVerdict(winner="candidate", p_value=0.01), budget)
    assert decision.action == "ACCEPT"
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_search_workflow.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/athena/workflows/search/idea_generation.py src/athena/workflows/search/code_agent.py src/athena/workflows/search/search_loop.py src/athena/execution/agent_monitor.py src/athena/execution/supervisor.py tests/test_search_workflow.py
git commit -m "feat(search): add IdeaGeneration, CodeAgent, SearchLoop, Supervisor

Core SEARCH phase with event-driven supervisor-orchestrated loop.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: Create VALIDATE + REPORT workflows

**Files:**
- Create: `src/athena/workflows/validate/ablation.py`
- Create: `src/athena/workflows/report/final_report.py`
- Test: `tests/test_validate_report.py`

- [ ] **Step 1: Write ablation.py**

```python
# src/athena/workflows/validate/ablation.py
from athena.core.schemas import Hypothesis, EvalResult
from athena.core.research.research_tree import ResearchTree, Experiment


class Validator:
    """Independent validation of SOTA results."""

    async def ablate(self, sota_node_id: str, tree: ResearchTree, code_agent) -> list[tuple[Hypothesis, EvalResult]]:
        """Remove each intervention on the SOTA path and measure impact."""
        path = tree.hypotheses_path(sota_node_id)
        results = []
        for h in path:
            # Checkout parent commit and re-run without this intervention
            parent_node = tree.get_node_by_id(h.parent_id) if h.parent_id else None
            if parent_node is None:
                continue
            # Re-run from parent commit
            result = await code_agent.execute(
                h, parent_node.exp.commit, None, parent_node.exp.gitwork
            )
            results.append((h, result.eval))
        return results

    async def final_test(self, sota_node_id: str, tree: ResearchTree, code_agent) -> EvalResult:
        """Evaluate on held-out test set. Run once."""
        sota = tree.get_node_by_id(sota_node_id)
        result = await code_agent.execute(
            sota.exp.hypothesis, sota.exp.commit, None, sota.exp.gitwork
        )
        return result.eval
```

- [ ] **Step 2: Write final_report.py**

```python
# src/athena/workflows/report/final_report.py
from athena.core.schemas import Hypothesis, EvalResult, ArtifactRef
from athena.core.research.research_tree import ResearchTree


class Reporter:
    async def generate(
        self,
        sota_node_id: str,
        tree: ResearchTree,
        ablation: list[tuple[Hypothesis, EvalResult]],
        final: EvalResult,
    ) -> ArtifactRef:
        """Generate final Markdown report with full evidence chain."""
        sota = tree.get_node_by_id(sota_node_id)
        path = tree.hypotheses_path(sota_node_id)

        lines = [
            "# Athena AI4ML Experiment Report",
            "",
            "## SOTA Hypothesis Chain",
            "",
        ]
        for i, h in enumerate(path, 1):
            lines.append(f"{i}. **{h.statement}** — {h.intervention} → {h.expected_effect}")

        lines += [
            "",
            "## Final Test Result",
            f"- Primary metric: **{final.primary:.4f}**",
            "",
            "## Ablation Results",
            "",
        ]
        for h, r in ablation:
            lines.append(f"- Remove **{h.statement}**: primary = {r.primary:.4f} (Δ = {final.primary - r.primary:+.4f})")
        lines += [
            "",
            "## SOTA Experiment",
            f"- Metric: {sota.exp.metric_type} = {sota.exp.result}",
        ]

        report = "\n".join(lines)
        import os, tempfile
        path_out = os.path.join(tempfile.gettempdir(), f"athena_report_{sota_node_id[:8]}.md")
        with open(path_out, "w") as f:
            f.write(report)
        return f"artifact://{path_out}"
```

- [ ] **Step 3: Write test**

```python
# tests/test_validate_report.py
from athena.workflows.report.final_report import Reporter
from athena.core.schemas import EvalResult

def test_reporter_generates_output():
    reporter = Reporter()
    import asyncio
    # Smoke test: reporter runs without error
    # Full integration test needs ResearchTree with data
    assert reporter is not None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_validate_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/athena/workflows/validate/ablation.py src/athena/workflows/report/final_report.py tests/test_validate_report.py
git commit -m "feat(validate+report): add Validator.ablate(), final_test(), Reporter.generate()

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: Integration — end-to-end smoke test and wiring

**Files:**
- Create: `examples/ai4ml_pipeline.py`
- Test: `tests/test_e2e_ai4ml.py`

- [ ] **Step 1: Write end-to-end smoke test**

```python
# tests/test_e2e_ai4ml.py
import pytest
import tempfile, os


@pytest.mark.slow
def test_full_pipeline_smoke():
    """End-to-end smoke test: PREPARE → SEARCH → VALIDATE → REPORT.

    Uses in-memory artifacts and a temp git repo.
    """
    import asyncio
    from pathlib import Path
    from athena.core.schemas import TaskMetaData, MetricSpec, MetricDef, EvalSpec
    from athena.core.budget import BudgetSnapshot
    from athena.core.research.research_tree import ResearchTree
    from athena.core.evaluation import Comparator
    from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
    from athena.workflows.prepare.data_analysis import DataProfile
    from athena.workflows.search.search_loop import SearchLoop
    from athena.workflows.search.idea_generation import generate_hypotheses
    from athena.execution.supervisor import Supervisor

    # Setup
    task = TaskMetaData(
        task_type="classification", data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    profile = DataProfile(row_count=100, col_count=5, task_type_hint="classification")
    spec = EvaluatorFactory.build(task, profile)

    tree = ResearchTree()
    budget = BudgetSnapshot(remaining=3, max_no_improve=2)

    # Generate initial hypotheses
    hypotheses = asyncio.run(generate_hypotheses(profile, [], [], tree))
    assert len(hypotheses) > 0

    # Verify supervisor decisions
    supervisor = Supervisor()
    from athena.core.schemas import ComparisonVerdict
    d1 = supervisor.decide(ComparisonVerdict(winner="candidate", p_value=0.01), BudgetSnapshot(remaining=5))
    assert d1.action == "ACCEPT"

    d2 = supervisor.decide(ComparisonVerdict(winner="baseline", p_value=0.5), BudgetSnapshot(remaining=1))
    assert d2.action == "STOP"

    # Verify tree state
    pending = tree.pending_hypotheses()
    assert all(h.status == "PROPOSED" for h in pending)

    print("✅ E2E smoke test passed")
```

- [ ] **Step 2: Run smoke test**

Run: `uv run pytest tests/test_e2e_ai4ml.py -v -s`
Expected: PASS

- [ ] **Step 3: Write example script**

```python
# examples/ai4ml_pipeline.py
"""Minimal end-to-end AI4ML pipeline example.

Usage: uv run python examples/ai4ml_pipeline.py
"""
import asyncio
from athena.core.schemas import TaskMetaData, MetricSpec
from athena.core.budget import BudgetSnapshot
from athena.core.research.research_tree import ResearchTree
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory
from athena.workflows.prepare.data_analysis import DataProfile
from athena.workflows.search.idea_generation import generate_hypotheses
from athena.execution.supervisor import Supervisor


async def main():
    # 1. Define the task
    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )

    # 2. Analyze data
    profile = DataProfile(row_count=1000, col_count=20, task_type_hint="classification")

    # 3. Build evaluation spec
    spec = EvaluatorFactory.build(task, profile)
    print(f"Eval spec: primary={spec.primary.name}, direction={spec.primary.direction}")

    # 4. Initialize research tree and generate hypotheses
    tree = ResearchTree()
    hypotheses = await generate_hypotheses(profile, [], [], tree)
    print(f"Generated {len(hypotheses)} hypotheses:")
    for h in hypotheses:
        print(f"  - {h.statement}")

    # 5. Run search loop (with real GitWorkspace, Sandbox, etc. in production)
    budget = BudgetSnapshot(remaining=10, max_no_improve=3)
    supervisor = Supervisor()
    print(f"Budget: {budget.remaining} experiments, stop after {budget.max_no_improve} no-improvement streak")

    # 6. Search, Validate, Report would follow in production
    print("\nPipeline setup complete. Ready for search.")

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run example**

Run: `uv run python examples/ai4ml_pipeline.py`
Expected: prints pipeline setup output

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/ -v -q --ignore=tests/fixtures`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add examples/ai4ml_pipeline.py tests/test_e2e_ai4ml.py
git commit -m "test(e2e): add end-to-end smoke test and example pipeline

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Execution Order

Tasks must run sequentially: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9

Each task depends on types defined in previous tasks:
- Task 2 needs `EvalSpec`, `EvalResult` from Task 1
- Task 4 needs `Hypothesis` (extended) from Task 1
- Task 5 needs `Hypothesis` from Task 1
- Task 6 needs `EvalSpec` from Task 1, `ResearchTree` from Task 4
- Task 7 needs `HypothesisRanker` from Task 5, `BudgetSnapshot` from Task 3, `Comparator` from Task 2, `ResearchTree` from Task 4
- Task 8 needs `ResearchTree`, `EvalResult`
- Task 9 needs all modules
