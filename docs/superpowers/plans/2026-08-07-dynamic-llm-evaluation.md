# Dynamic LLM Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make evaluation metric selection LLM-driven from the task description, supporting hybrid/composite metrics, while keeping the comparison layer opaque and the protocol frozen.

**Architecture:** The metric implementation (`eval.py`) becomes part of the frozen `EvalSpec` (`eval_script` field) so every experiment worktree runs byte-identical evaluation code — no commit-inheritance dependency. A `MetricPlanner` (LLM, schema-forced) builds the `EvalSpec` at PREPARE, falling back to deterministic `task_type` defaults when the LLM is unavailable. `EvalResult` gains `fold_scores`; the `Comparator` uses them for CV protocols. `run_experiment.py` writes `predictions.csv`/`labels.csv` (+`fold_ids.csv` for CV); `eval.py` reads them and emits `eval_result.json`.

**Tech Stack:** Python 3.12, pydantic v2, pydantic-ai (structured agent, same pattern as `Ideator`), pandas, scikit-learn, scipy, pytest, asyncio.

## Global Constraints

- `EvalSpec` stays `frozen=True`; `MetricDef` fields `{name, direction, description}` stay unchanged.
- The eval protocol is decided once at PREPARE and frozen; never changed mid-SEARCH.
- `eval.py` must be self-contained: stdlib + pandas + sklearn only (worktrees are not guaranteed to have Athena installed).
- `eval.py` reads `ATHENA_EXPERIMENT_ID` from the environment (shared script, cannot embed a per-experiment ID).
- The comparison layer only consumes scalars / fold values — never the metric formula.
- No Athena-side metric registry; no LLM-as-judge (YAGNI, per spec).
- Follow existing pydantic-ai structured-agent pattern from `athena/ideator/ideator.py`.
- Baseline candidates share the same protocol → `run_experiment.py` for CV must write `fold_ids.csv`.

---

### Task 1: Data model extensions (protocol, fold_scores, description, eval_script)

**Files:**
- Modify: `src/athena/evaluation/types.py`
- Modify: `src/athena/research/models.py`
- Modify: `src/athena/evaluation/evaluator.py:206-216`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Consumes: existing `MetricDef`, `ArtifactRef`.
- Produces:
  - `EvalProtocol(method: Literal["holdout","cv"], n_folds: int = 5)`
  - `EvalSpec(primary, secondary=[], protocol=EvalProtocol(), eval_script="", split_seed=42, test_ratio=0.2)` — still frozen
  - `EvalResult(experiment_id, primary, secondary={}, fold_scores=[], per_sample)` — `fold_scores` added
  - `TaskMetaData(..., description: str = "")` — field added

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_evaluation.py` (imports already cover `EvalSpec`, `EvalResult`, `MetricDef`; add `EvalProtocol` to the existing import line):

```python
def test_eval_spec_defaults_to_holdout_protocol() -> None:
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="F1")
    )
    assert spec.protocol.method == "holdout"


def test_eval_spec_accepts_cv_protocol() -> None:
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="F1"),
        protocol=EvalProtocol(method="cv", n_folds=5),
    )
    assert spec.protocol.n_folds == 5


def test_eval_result_fold_scores_default_empty() -> None:
    result = EvalResult(
        experiment_id="e", primary=0.9, per_sample="artifact://samples/e"
    )
    assert result.fold_scores == []


def test_evaluator_reads_fold_scores_from_payload(tmp_path) -> None:
    import json

    from athena.evaluation.evaluator import Evaluator

    result_dir = tmp_path / "artifact_ref"
    result_dir.mkdir(parents=True)
    (result_dir / "eval_result.json").write_text(
        json.dumps(
            {
                "experiment_id": "exp_1",
                "primary": 0.85,
                "secondary": {"accuracy": 0.83},
                "fold_scores": [0.8, 0.85, 0.9, 0.87, 0.86],
            }
        ),
        encoding="utf-8",
    )
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="F1")
    )
    result = Evaluator(spec).evaluate(f"artifact://{result_dir}")
    assert result.fold_scores == [0.8, 0.85, 0.9, 0.87, 0.86]
```

Add to `tests/test_prepare_workflow.py` (imports already cover `TaskMetaData`, `MetricSpec`):

```python
def test_task_metadata_description_defaults_to_empty() -> None:
    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    assert task.description == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evaluation.py::test_eval_spec_defaults_to_holdout_protocol tests/test_evaluation.py::test_eval_spec_accepts_cv_protocol tests/test_evaluation.py::test_eval_result_fold_scores_default_empty tests/test_evaluation.py::test_evaluator_reads_fold_scores_from_payload tests/test_prepare_workflow.py::test_task_metadata_description_defaults_to_empty -v`
Expected: FAIL — `EvalProtocol` undefined / `protocol` not a field of `EvalSpec` / `fold_scores` not a field / `description` not a field.

- [ ] **Step 3: Implement the models**

In `src/athena/evaluation/types.py`, add `EvalProtocol` before `EvalSpec`, add the `protocol` and `eval_script` fields to `EvalSpec`, and add `fold_scores` to `EvalResult`:

```python
class EvalProtocol(BaseModel):
    """评估协议：holdout 用最终测试集，cv 用 K 折 OOF。"""

    method: Literal["holdout", "cv"] = "holdout"
    n_folds: int = 5


class EvalSpec(BaseModel):
    """冻结的评估协议。在 PREPARE 阶段构建，在 SEARCH 阶段不可变。"""

    model_config = {"frozen": True}

    primary: MetricDef
    secondary: list[MetricDef] = Field(default_factory=list)
    protocol: EvalProtocol = Field(default_factory=EvalProtocol)
    eval_script: str = ""
    split_seed: int = 42
    test_ratio: float = 0.2


class EvalResult(BaseModel):
    """在预测上运行 eval.py 的输出结果。"""

    experiment_id: str
    primary: float
    secondary: dict[str, float] = Field(default_factory=dict)
    fold_scores: list[float] = Field(default_factory=list)
    per_sample: ArtifactRef
```

In `src/athena/evaluation/types.py`, update `__all__` to include `EvalProtocol`:

```python
__all__ = ["ComparisonVerdict", "EvalProtocol", "EvalResult", "EvalSpec", "MetricDef"]
```

In `src/athena/research/models.py`, add the `description` field to `TaskMetaData`:

```python
class TaskMetaData(BaseModel):
    """任务元数据 — 描述 ML 任务类型、数据格式和评估约束。"""

    task_type: str
    data_type: str
    target_vars: list[str] = Field(default_factory=list)
    primary_metric: MetricSpec
    constraints: list[str] = Field(default_factory=list)
    description: str = ""
```

In `src/athena/evaluation/evaluator.py`, make `Evaluator.evaluate` read `fold_scores`:

```python
        return EvalResult(
            experiment_id=data["experiment_id"],
            primary=data["primary"],
            secondary=data.get("secondary", {}),
            fold_scores=data.get("fold_scores", []),
            per_sample=f"{predictions_ref}/per_sample.csv",
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_evaluation.py tests/test_prepare_workflow.py -v`
Expected: PASS (new tests pass, existing tests still green).

- [ ] **Step 5: Run the full suite and fix any field-list assertions**

Run: `uv run pytest -q`
Expected: PASS. If a test asserts the exact field list of `EvalSpec` or `TaskMetaData` (e.g. `model_fields`), update that assertion to include the new fields. No other failures.

- [ ] **Step 6: Commit**

```bash
git add src/athena/evaluation/types.py src/athena/research/models.py src/athena/evaluation/evaluator.py tests/test_evaluation.py tests/test_prepare_workflow.py
git commit -m "feat(evaluation): add EvalProtocol, fold_scores, task description, eval_script"
```

---

### Task 2: Deterministic default eval.py template

**Files:**
- Modify: `src/athena/evaluation/factory.py`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Consumes: `EvalSpec` (from Task 1).
- Produces: `default_eval_script(spec: EvalSpec) -> str` — a self-contained Python program implementing `spec` (catalog metrics only; holdout or CV per `spec.protocol`). Reads `predictions.csv`, `labels.csv`, and `fold_ids.csv` (CV only); writes `eval_result.json` with `{experiment_id, primary, secondary, fold_scores}`. Reads `experiment_id` from env `ATHENA_EXPERIMENT_ID`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_evaluation.py`:

```python
def test_default_eval_script_computes_classification_primary(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys

    import pandas as pd

    from athena.evaluation.factory import default_eval_script

    spec = create_eval_spec("classification")
    script = default_eval_script(spec)
    assert "predictions.csv" in script and "fold_ids.csv" not in script

    pd.DataFrame({"pred": [0, 1, 1, 1, 0]}).to_csv(
        tmp_path / "predictions.csv", index=False
    )
    pd.DataFrame({"label": [0, 1, 0, 1, 0]}).to_csv(
        tmp_path / "labels.csv", index=False
    )
    (tmp_path / "eval.py").write_text(script, encoding="utf-8")
    env = {**os.environ, "ATHENA_EXPERIMENT_ID": "exp-smoke"}
    proc = subprocess.run(
        [sys.executable, "eval.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((tmp_path / "eval_result.json").read_text(encoding="utf-8"))
    assert payload["experiment_id"] == "exp-smoke"
    assert payload["primary"] == pytest.approx(0.8)
    assert payload["secondary"]["accuracy"] == pytest.approx(3 / 5)


def test_default_eval_script_emits_fold_scores_for_cv(tmp_path) -> None:
    import json
    import os
    import subprocess
    import sys

    import pandas as pd

    from athena.evaluation.factory import default_eval_script

    spec = create_eval_spec("classification")
    cv_spec = spec.model_copy(
        update={"protocol": EvalProtocol(method="cv", n_folds=5)}
    )
    script = default_eval_script(cv_spec)
    assert "fold_ids.csv" in script

    rows = 20
    labels = [0, 1] * 10
    preds = [float((i % 3) % 2) for i in range(rows)]
    pd.DataFrame({"pred": preds}).to_csv(tmp_path / "predictions.csv", index=False)
    pd.DataFrame({"label": labels}).to_csv(tmp_path / "labels.csv", index=False)
    pd.DataFrame({"fold": [i % 5 for i in range(rows)]}).to_csv(
        tmp_path / "fold_ids.csv", index=False
    )
    (tmp_path / "eval.py").write_text(script, encoding="utf-8")
    env = {**os.environ, "ATHENA_EXPERIMENT_ID": "exp-cv"}
    proc = subprocess.run(
        [sys.executable, "eval.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((tmp_path / "eval_result.json").read_text(encoding="utf-8"))
    assert len(payload["fold_scores"]) == 5
    assert all(isinstance(value, float) for value in payload["fold_scores"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evaluation.py::test_default_eval_script_computes_classification_primary tests/test_evaluation.py::test_default_eval_script_emits_fold_scores_for_cv -v`
Expected: FAIL with `ImportError: cannot import name 'default_eval_script'`.

- [ ] **Step 3: Implement `default_eval_script`**

In `src/athena/evaluation/factory.py`, add `import json` at the top and append the function:

```python
def default_eval_script(spec: EvalSpec) -> str:
    """Deterministic bundled eval.py implementing a catalog-metric protocol."""
    primary_name = json.dumps(spec.primary.name)
    secondary_names = json.dumps([metric.name for metric in spec.secondary])
    cv = spec.protocol.method == "cv"
    return f'''"""Deterministic default evaluator generated by Athena. Do not edit."""
import json
import math
import os

import pandas as pd

from sklearn.metrics import (
    accuracy_score,
    f1_score,
    mean_absolute_error,
    mean_squared_error,
    precision_score,
    r2_score,
    recall_score,
    roc_auc_score,
)

predictions = pd.read_csv("predictions.csv")
labels = pd.read_csv("labels.csv")
y_pred = predictions.iloc[:, 0]
y_true = labels.iloc[:, 0]


def _metric(name, y_true, y_pred):
    if name == "accuracy":
        return float(accuracy_score(y_true, y_pred))
    if name == "f1_macro":
        return float(f1_score(y_true, y_pred, average="macro", zero_division=0))
    if name == "precision_macro":
        return float(precision_score(y_true, y_pred, average="macro", zero_division=0))
    if name == "recall_macro":
        return float(recall_score(y_true, y_pred, average="macro", zero_division=0))
    if name == "f1":
        return float(f1_score(y_true, y_pred, zero_division=0))
    if name == "precision":
        return float(precision_score(y_true, y_pred, zero_division=0))
    if name == "recall":
        return float(recall_score(y_true, y_pred, zero_division=0))
    if name == "roc_auc":
        return float(roc_auc_score(y_true, y_pred))
    if name == "rmse":
        return float(math.sqrt(mean_squared_error(y_true, y_pred)))
    if name == "mse":
        return float(mean_squared_error(y_true, y_pred))
    if name == "mae":
        return float(mean_absolute_error(y_true, y_pred))
    if name == "r2":
        return float(r2_score(y_true, y_pred))
    raise ValueError(f"unsupported metric: {{name}}")


def _fold_scores(y_true, y_pred):
    folds = pd.read_csv("fold_ids.csv").iloc[:, 0]
    return [
        _metric({primary_name}, y_true[folds == k], y_pred[folds == k])
        for k in sorted(set(folds))
    ]


primary = _metric({primary_name}, y_true, y_pred)
secondary = {{name: _metric(name, y_true, y_pred) for name in {secondary_names}}}
fold_scores = _fold_scores(y_true, y_pred) if {cv!r} else []
with open("eval_result.json", "w", encoding="utf-8") as stream:
    json.dump({{
        "experiment_id": os.environ.get("ATHENA_EXPERIMENT_ID", "unknown"),
        "primary": primary,
        "secondary": secondary,
        "fold_scores": fold_scores,
    }}, stream, indent=2)
'''
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_evaluation.py::test_default_eval_script_computes_classification_primary tests/test_evaluation.py::test_default_eval_script_emits_fold_scores_for_cv -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/athena/evaluation/factory.py tests/test_evaluation.py
git commit -m "feat(evaluation): add deterministic default eval.py template"
```

---

### Task 3: MetricPlanner (LLM + fallback)

**Files:**
- Create: `src/athena/workflows/prepare/metric_planner.py`
- Modify: `src/athena/workflows/prepare/evaluator_factory.py` (compat alias)
- Test: `tests/test_metric_planner.py`

**Interfaces:**
- Consumes: `TaskMetaData` (with `description`), `DataProfile`, `create_eval_spec` + `default_eval_script` from `athena.evaluation.factory`, `MetricDef`/`EvalSpec`.
- Produces:
  - `MetricPlan(spec: EvalSpec, rationale: str)` — `spec.eval_script` carries the metric implementation.
  - `default_spec(task: TaskMetaData, profile: DataProfile) -> EvalSpec` — deterministic fallback, honors an explicit user `primary_metric`, fills `eval_script`.
  - `smoke_test_eval_script(script: str, *, cv: bool = False) -> dict` — runs the script on synthetic CSVs, validates output contract, returns the payload (raises on failure).
  - `MetricPlanner(agent=None)` with `async plan(task, profile) -> MetricPlan` — LLM first, fallback on any error.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metric_planner.py`:

```python
import math

import pytest

from athena.data.types import DataProfile
from athena.evaluation.factory import default_eval_script
from athena.research.models import MetricSpec, TaskMetaData
from athena.workflows.prepare.metric_planner import (
    MetricPlan,
    MetricPlanner,
    default_spec,
    smoke_test_eval_script,
)


def _task(**overrides) -> TaskMetaData:
    fields = dict(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
        description="Predict survival on the Titanic from tabular features.",
    )
    fields.update(overrides)
    return TaskMetaData(**fields)


def _profile() -> DataProfile:
    return DataProfile(row_count=100, col_count=5, task_type_hint="classification")


def test_default_spec_uses_task_type_defaults() -> None:
    spec = default_spec(_task(), _profile())
    assert spec.primary.name == "f1_macro"
    assert spec.primary.direction == "maximize"
    assert "predictions.csv" in spec.eval_script


def test_default_spec_honors_explicit_user_metric() -> None:
    spec = default_spec(
        _task(primary_metric=MetricSpec(name="roc_auc", direction="maximize")),
        _profile(),
    )
    assert spec.primary.name == "roc_auc"
    assert spec.primary.direction == "maximize"


def test_smoke_test_validates_default_holdout_script() -> None:
    spec = default_spec(_task(), _profile())
    payload = smoke_test_eval_script(spec.eval_script)
    assert payload["experiment_id"] == "smoke"
    assert math.isfinite(payload["primary"])


class _FakeAgent:
    def __init__(self, plan: MetricPlan) -> None:
        self._plan = plan

    async def run(self, prompt: str, *, output_type):
        assert output_type is MetricPlan
        return self._plan


@pytest.mark.asyncio
async def test_planner_uses_agent_plan_when_valid() -> None:
    spec = default_spec(_task(), _profile())
    plan = MetricPlan(spec=spec, rationale="hybrid: 0.7*auc + 0.3*f1")
    result = await MetricPlanner(agent=_FakeAgent(plan)).plan(_task(), _profile())
    assert result.rationale == "hybrid: 0.7*auc + 0.3*f1"
    assert result.spec.primary.name == "f1_macro"


@pytest.mark.asyncio
async def test_planner_falls_back_when_agent_script_is_broken() -> None:
    bad = MetricPlan(
        spec=default_spec(_task(), _profile()).model_copy(
            update={"eval_script": "raise RuntimeError('boom')\n"}
        ),
        rationale="broken",
    )
    result = await MetricPlanner(agent=_FakeAgent(bad)).plan(_task(), _profile())
    assert result.rationale.startswith("fallback")
    assert "predictions.csv" in result.spec.eval_script


@pytest.mark.asyncio
async def test_planner_falls_back_without_agent() -> None:
    plan = await MetricPlanner(agent=None).plan(_task(), _profile())
    assert plan.spec.primary.name == "f1_macro"
    assert "predictions.csv" in plan.spec.eval_script
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metric_planner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'athena.workflows.prepare.metric_planner'`.

- [ ] **Step 3: Implement the planner**

Create `src/athena/workflows/prepare/metric_planner.py`:

```python
"""LLM 驱动的指标规划器：根据任务描述生成冻结的 EvalSpec 与 eval.py。"""

import asyncio
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol

import pandas as pd
from pydantic import BaseModel

from athena.data.types import DataProfile
from athena.evaluation.factory import create_eval_spec, default_eval_script
from athena.evaluation.types import EvalSpec, MetricDef
from athena.research.models import TaskMetaData


class MetricPlan(BaseModel):
    """Planner 输出：冻结的评估协议（内含 eval_script）+ 审计理由。"""

    spec: EvalSpec
    rationale: str


class _StructuredAgent(Protocol):
    async def run(
        self, prompt: str, *, output_type: type[BaseModel]
    ) -> object: ...


def default_spec(task: TaskMetaData, profile: DataProfile) -> EvalSpec:
    """确定性兜底：按 task_type 选默认指标，尊重用户显式指定的主指标。"""
    spec = create_eval_spec(task.task_type)
    user = task.primary_metric
    if user.name and user.name != spec.primary.name:
        spec = EvalSpec(
            primary=MetricDef(
                name=user.name,
                direction=user.direction,
                description=user.name,
            ),
            secondary=spec.secondary,
            protocol=spec.protocol,
            split_seed=spec.split_seed,
            test_ratio=spec.test_ratio,
        )
    return spec.model_copy(update={"eval_script": default_eval_script(spec)})


def _write_synthetic_data(directory: Path, *, cv: bool) -> None:
    rows = 20
    labels = [0, 1] * 10
    predictions = [float((i % 3) % 2) for i in range(rows)]
    pd.DataFrame({"pred": predictions}).to_csv(
        directory / "predictions.csv", index=False
    )
    pd.DataFrame({"label": labels}).to_csv(directory / "labels.csv", index=False)
    if cv:
        pd.DataFrame({"fold": [i % 5 for i in range(rows)]}).to_csv(
            directory / "fold_ids.csv", index=False
        )


def smoke_test_eval_script(script: str, *, cv: bool = False) -> dict:
    """在合成数据上运行 eval.py，校验输出契约，返回 eval_result.json 内容。"""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "eval.py").write_text(script, encoding="utf-8")
        _write_synthetic_data(directory, cv=cv)
        env = {**os.environ, "ATHENA_EXPERIMENT_ID": "smoke"}
        proc = subprocess.run(
            [sys.executable, "eval.py"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if proc.returncode != 0:
            raise ValueError(
                f"eval.py smoke test failed: {proc.stderr or proc.stdout}"
            )
        payload = json.loads(
            (directory / "eval_result.json").read_text(encoding="utf-8")
        )
        primary = float(payload["primary"])
        if not math.isfinite(primary):
            raise ValueError("eval.py primary must be finite")
        if cv and not payload.get("fold_scores"):
            raise ValueError("cv eval.py must emit fold_scores")
        return payload


class MetricPlanner:
    """PREPARE 阶段的一次性指标规划：LLM 优先，失败回退默认。"""

    def __init__(self, agent: _StructuredAgent | None = None) -> None:
        self._agent = agent

    async def plan(self, task: TaskMetaData, profile: DataProfile) -> MetricPlan:
        if self._agent is not None:
            try:
                return await self._plan_with_agent(task, profile)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        spec = default_spec(task, profile)
        return MetricPlan(
            spec=spec,
            rationale="fallback: task_type defaults, LLM planner unavailable",
        )

    async def _plan_with_agent(
        self, task: TaskMetaData, profile: DataProfile
    ) -> MetricPlan:
        result = await self._agent.run(self._prompt(task, profile), output_type=MetricPlan)
        plan = MetricPlan.model_validate(getattr(result, "output", result))
        if not plan.spec.eval_script.strip():
            raise ValueError("MetricPlan must include an eval_script")
        smoke_test_eval_script(
            plan.spec.eval_script, cv=plan.spec.protocol.method == "cv"
        )
        return plan

    @staticmethod
    def _prompt(task: TaskMetaData, profile: DataProfile) -> str:
        task_payload = {
            "description": task.description,
            "task_type": task.task_type,
            "data_type": task.data_type,
            "target_vars": task.target_vars,
            "constraints": task.constraints,
            "primary_metric": task.primary_metric.model_dump(mode="json"),
        }
        return (
            "Design the evaluation metric protocol for this ML task.\n"
            "Frozen task metadata:\n"
            + json.dumps(task_payload, ensure_ascii=False, sort_keys=True)
            + "\n\nData profile:\n"
            + json.dumps(
                profile.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            )
            + "\n\nRules:\n"
            "- primary.name may be a catalog metric or a composite name; put the exact "
            "formula in primary.description (e.g. '0.7*roc_auc + 0.3*f1_macro').\n"
            "- primary.direction is 'maximize' or 'minimize'.\n"
            "- If primary_metric differs from the task_type default, honor it as a hard "
            "constraint.\n"
            "- protocol.method is 'holdout' (held-out test) or 'cv' (5-fold OOF on "
            "train); set protocol.n_folds=5 for cv.\n"
            "- spec.eval_script must be a self-contained Python program using only "
            "stdlib, pandas, and sklearn. It reads predictions.csv and labels.csv (and "
            "fold_ids.csv when cv) and writes eval_result.json with keys experiment_id "
            "(from os.environ['ATHENA_EXPERIMENT_ID']), primary, secondary, fold_scores.\n"
            "- rationale explains the metric choice in one short paragraph.\n"
        )
```

Rewrite `src/athena/workflows/prepare/evaluator_factory.py` as a compatibility alias:

```python
"""兼容别名：确定性默认 EvalSpec 构造器（无 LLM）。"""

from athena.evaluation.types import EvalSpec
from athena.data.types import DataProfile
from athena.research.models import TaskMetaData
from athena.workflows.prepare.metric_planner import (
    MetricPlan,
    MetricPlanner,
    default_spec,
)


class EvaluatorFactory:
    """Legacy 同步默认构造器。新代码应使用 MetricPlanner.plan()。"""

    @staticmethod
    def build(task: TaskMetaData, data_profile: DataProfile) -> EvalSpec:
        return default_spec(task, data_profile)


__all__ = ["EvaluatorFactory", "MetricPlan", "MetricPlanner"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_metric_planner.py tests/test_prepare_workflow.py tests/test_e2e_ai4ml.py -v`
Expected: PASS — new planner tests pass and the compat `EvaluatorFactory` keeps existing tests green.

- [ ] **Step 5: Commit**

```bash
git add src/athena/workflows/prepare/metric_planner.py src/athena/workflows/prepare/evaluator_factory.py tests/test_metric_planner.py
git commit -m "feat(prepare): LLM-driven MetricPlanner with deterministic fallback"
```

---

### Task 4: eval.py lifecycle in CodeAgent

**Files:**
- Modify: `src/athena/workflows/search/code_agent.py`
- Test: `tests/test_search_workflow.py`

**Interfaces:**
- Consumes: `EvalSpec` (with `protocol`, `eval_script`), `default_eval_script` from `athena.evaluation.factory`.
- Produces:
  - `_run_python(script, cwd, timeout_s, env=None) -> ProcessResult` — passes `env` through.
  - `_ensure_evaluator(wt_path: Path, eval_spec: EvalSpec) -> None` — writes `eval_spec.eval_script or default_eval_script(eval_spec)` only when `eval.py` is missing.
  - `_load_evaluation(wt_path, experiment_id, logs_ref, eval_spec) -> EvalResult` — reads `fold_scores`, validates CV fold count.
  - `CodeAgent.execute` — sets `ATHENA_EXPERIMENT_ID` env for subprocesses.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search_workflow.py` (update the two existing `fake_run` signatures first — see Step 2). Add these tests:

```python
@pytest.mark.asyncio
async def test_code_agent_writes_frozen_eval_script_from_spec(
    tmp_path, monkeypatch
) -> None:
    async def fake_run(script: str, cwd, timeout_s: float, env=None) -> ProcessResult:
        if script == code_agent_module.EVALUATION_ENTRYPOINT:
            assert env["ATHENA_EXPERIMENT_ID"] == "exp-frozen"
            (cwd / "predictions.csv").write_text("prediction\n0\n", encoding="utf-8")
            (cwd / "eval_result.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp-frozen",
                        "primary": 0.5,
                        "secondary": {},
                        "fold_scores": [],
                    }
                ),
                encoding="utf-8",
            )
        return ProcessResult(returncode=0, output="ok")

    monkeypatch.setattr(code_agent_module, "_run_python", fake_run)
    hypothesis = Hypothesis(
        id="hyp-test",
        statement="Use a baseline",
        intervention="Train one deterministic model",
        expected_effect="Establish the reference metric",
    )
    spec = EvalSpec(
        primary=MetricDef(
            name="composite", direction="maximize", description="0.7*auc + 0.3*f1"
        ),
        eval_script="CUSTOM_EVAL_SCRIPT",
    )
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    await CodeAgent().execute(
        "exp-frozen", hypothesis, experiment_plan(), "a" * 40, spec, worktree
    )

    written = (tmp_path / "eval.py").read_text(encoding="utf-8")
    assert written == "CUSTOM_EVAL_SCRIPT"
    assert (tmp_path / "eval.py").is_file()


@pytest.mark.asyncio
async def test_code_agent_keeps_existing_eval_script(tmp_path, monkeypatch) -> None:
    (tmp_path / "eval.py").write_text("FROZEN", encoding="utf-8")

    async def fake_run(script: str, cwd, timeout_s: float, env=None) -> ProcessResult:
        if script == code_agent_module.EVALUATION_ENTRYPOINT:
            (cwd / "predictions.csv").write_text("prediction\n0\n", encoding="utf-8")
            (cwd / "eval_result.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp-keep",
                        "primary": 0.5,
                        "secondary": {},
                    }
                ),
                encoding="utf-8",
            )
        return ProcessResult(returncode=0, output="ok")

    monkeypatch.setattr(code_agent_module, "_run_python", fake_run)
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="F1"),
        eval_script="SHOULD_NOT_OVERWRITE",
    )
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    await CodeAgent().execute(
        "exp-keep", Hypothesis(
            id="hyp-test",
            statement="s",
            intervention="i",
            expected_effect="e",
        ), experiment_plan(), "a" * 40, spec, worktree
    )

    assert (tmp_path / "eval.py").read_text(encoding="utf-8") == "FROZEN"


@pytest.mark.asyncio
async def test_code_agent_validates_cv_fold_counts(tmp_path, monkeypatch) -> None:
    async def fake_run(script: str, cwd, timeout_s: float, env=None) -> ProcessResult:
        if script == code_agent_module.EVALUATION_ENTRYPOINT:
            (cwd / "predictions.csv").write_text("prediction\n0\n1\n2\n3\n", encoding="utf-8")
            (cwd / "eval_result.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp-cv",
                        "primary": 0.5,
                        "secondary": {},
                        "fold_scores": [0.5, 0.5],  # expected 5
                    }
                ),
                encoding="utf-8",
            )
        return ProcessResult(returncode=0, output="ok")

    monkeypatch.setattr(code_agent_module, "_run_python", fake_run)
    spec = EvalSpec(
        primary=MetricDef(name="f1_macro", direction="maximize", description="F1"),
        protocol=EvalProtocol(method="cv", n_folds=5),
        eval_script="CUSTOM_EVAL_SCRIPT",
    )
    worktree = GitWorkBranch(
        path=str(tmp_path), branch="exp/test", base_commit="a" * 40
    )

    with pytest.raises(CodeExecutionError, match="must report 5 fold scores"):
        await CodeAgent().execute(
            "exp-cv",
            Hypothesis(
                id="hyp-test", statement="s", intervention="i", expected_effect="e"
            ),
            experiment_plan(),
            "a" * 40,
            spec,
            worktree,
        )
```

- [ ] **Step 2: Update existing `fake_run` signatures**

In `tests/test_search_workflow.py`, all three `fake_run` definitions (lines ~474, ~546, ~583) take `(script, cwd, timeout_s)`. Change each to `(script, cwd, timeout_s, env=None)`. The first test (`test_code_agent_uses_a_fixed_entrypoint_without_fixing_model_filename`) must also write `fold_scores` into its `eval_result.json` or omit it — omit is fine since `_load_evaluation` defaults it to `[]` and the default protocol is holdout.

Also add imports to the top of `tests/test_search_workflow.py` if `EvalProtocol` is not already imported:

```python
from athena.evaluation.types import EvalProtocol
```

Run these to confirm the existing tests still fail *for the right reason* before implementing (expected: FAIL — `_load_evaluation` signature changed / `_run_python` does not accept `env` / `TypeError`).

Run: `uv run pytest tests/test_search_workflow.py::test_code_agent_uses_a_fixed_entrypoint_without_fixing_model_filename tests/test_search_workflow.py::test_code_agent_rejects_process_failures_with_log_evidence tests/test_search_workflow.py::test_code_agent_rejects_missing_evaluation_output tests/test_search_workflow.py::test_code_agent_writes_frozen_eval_script_from_spec tests/test_search_workflow.py::test_code_agent_keeps_existing_eval_script tests/test_search_workflow.py::test_code_agent_validates_cv_fold_counts -v`

- [ ] **Step 3: Implement the CodeAgent changes**

In `src/athena/workflows/search/code_agent.py`:

Add `import os` to the top (after `import math`).

Change `_run_python` to accept and forward `env`:

```python
async def _run_python(
    script: str, cwd: Path, timeout_s: float, env=None
) -> ProcessResult:
    """Run one Python entrypoint and retain its exit status and output."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        script,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
```

In `execute()`, replace the `_write_evaluator` call and thread the env + spec into evaluation:

```python
        await self._ensure_evaluator(wt_path, eval_spec)
        await self._ensure_experiment_entrypoint(
            wt_path, experiment_id, hypothesis, plan, eval_spec
        )

        run_env = {**os.environ, "ATHENA_EXPERIMENT_ID": experiment_id}
        process_results: list[tuple[str, ProcessResult]] = []
        for script, timeout_s in (
            (EXPERIMENT_ENTRYPOINT, 300),
            (EVALUATION_ENTRYPOINT, 60),
        ):
            process_result = await _run_python(script, wt_path, timeout_s, env=run_env)
            process_results.append((script, process_result))
            await self._write_logs(log_path, process_results)
            if process_result.returncode != 0:
                message = (
                    process_result.output
                    if process_result.returncode == -1
                    else f"{script} exited with code {process_result.returncode}"
                )
                raise CodeExecutionError(message, logs=logs_ref)

        evaluation = await self._load_evaluation(
            wt_path, experiment_id, logs_ref, eval_spec
        )
```

Replace `_write_evaluator` with `_ensure_evaluator`:

```python
    @staticmethod
    async def _ensure_evaluator(wt_path: Path, eval_spec: EvalSpec) -> None:
        entrypoint = wt_path / EVALUATION_ENTRYPOINT
        if entrypoint.is_file():
            return
        from athena.evaluation.factory import default_eval_script

        script = eval_spec.eval_script or default_eval_script(eval_spec)
        await asyncio.to_thread(
            entrypoint.write_text, script, encoding="utf-8"
        )
```

Change `_load_evaluation` signature and add `fold_scores` + CV validation:

```python
    @staticmethod
    async def _load_evaluation(
        wt_path: Path,
        experiment_id: str,
        logs_ref: ArtifactRef,
        eval_spec: EvalSpec,
    ) -> EvalResult:
        result_path = wt_path / "eval_result.json"
        if not result_path.is_file():
            raise CodeExecutionError(
                "evaluation did not produce eval_result.json", logs=logs_ref
            )
        samples_path = wt_path / "predictions.csv"
        if not samples_path.is_file():
            raise CodeExecutionError(
                "evaluation did not produce per-sample predictions", logs=logs_ref
            )
        try:
            payload = json.loads(
                await asyncio.to_thread(result_path.read_text, encoding="utf-8")
            )
            evaluated_id = payload.get("experiment_id")
            if evaluated_id != experiment_id:
                raise ValueError(f"evaluation experiment id mismatch: {evaluated_id!r}")
            result = EvalResult.model_validate(
                {
                    **payload,
                    "per_sample": f"artifact://{samples_path}",
                    "fold_scores": payload.get("fold_scores", []),
                }
            )
            if not math.isfinite(result.primary):
                raise ValueError("evaluation primary metric must be finite")
            if (
                eval_spec.protocol.method == "cv"
                and len(result.fold_scores) != eval_spec.protocol.n_folds
            ):
                raise ValueError(
                    f"cv evaluation must report {eval_spec.protocol.n_folds} "
                    f"fold scores, got {len(result.fold_scores)}"
                )
            return result
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise CodeExecutionError(
                f"invalid evaluation output: {exc}", logs=logs_ref
            ) from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_search_workflow.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/athena/workflows/search/code_agent.py tests/test_search_workflow.py
git commit -m "feat(search): write frozen eval_script per worktree, validate CV fold counts"
```

---

### Task 5: Comparator fold-level branch

**Files:**
- Modify: `src/athena/evaluation/comparator.py`
- Test: `tests/test_evaluation.py`

**Interfaces:**
- Consumes: `EvalResult` (with `fold_scores`), `ComparisonVerdict`.
- Produces:
  - `_paired_fold_samples(baseline, candidate) -> tuple[tuple[float,...], tuple[float,...]] | None` — raises on mismatched/non-finite fold scores when either side has them; returns `None` when neither does.
  - `compare_results(baseline, candidate, *, direction, read_samples=None, alpha=0.05)` — fold path → per-sample path → direct-scalar path.
  - `Comparator.compare` — delegates to `compare_results`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_evaluation.py`:

```python
def test_comparison_uses_fold_scores_when_present() -> None:
    baseline = EvalResult(
        experiment_id="base",
        primary=0.82,
        fold_scores=[0.8, 0.75, 0.9, 0.85, 0.8],
        per_sample="artifact://samples/base",
    )
    candidate = EvalResult(
        experiment_id="cand",
        primary=0.89,
        fold_scores=[0.9, 0.85, 0.95, 0.9, 0.88],
        per_sample="artifact://samples/cand",
    )

    verdict = compare_results(baseline, candidate, direction="maximize")

    assert verdict.winner == "candidate"
    assert verdict.p_value < 0.05


def test_comparison_rejects_mismatched_fold_scores() -> None:
    baseline = EvalResult(
        experiment_id="base",
        primary=0.8,
        fold_scores=[0.8, 0.8, 0.8, 0.8, 0.8],
        per_sample="artifact://samples/base",
    )
    candidate = EvalResult(
        experiment_id="cand",
        primary=0.9,
        fold_scores=[0.9, 0.9, 0.9],
        per_sample="artifact://samples/cand",
    )

    with pytest.raises(ValueError, match="paired fold scores"):
        compare_results(baseline, candidate, direction="maximize")


def test_comparison_rejects_non_finite_fold_scores() -> None:
    baseline = EvalResult(
        experiment_id="base",
        primary=0.8,
        fold_scores=[0.8, math.nan, 0.8, 0.8, 0.8],
        per_sample="artifact://samples/base",
    )
    candidate = EvalResult(
        experiment_id="cand",
        primary=0.9,
        fold_scores=[0.9, 0.9, 0.9, 0.9, 0.9],
        per_sample="artifact://samples/cand",
    )

    with pytest.raises(ValueError, match="fold scores must be finite"):
        compare_results(baseline, candidate, direction="maximize")


def test_comparator_fold_path_respects_minimize_direction() -> None:
    baseline = EvalResult(
        experiment_id="base",
        primary=0.2,
        fold_scores=[0.2, 0.25, 0.15, 0.2, 0.22],
        per_sample="artifact://samples/base",
    )
    candidate = EvalResult(
        experiment_id="cand",
        primary=0.1,
        fold_scores=[0.1, 0.12, 0.08, 0.11, 0.09],
        per_sample="artifact://samples/cand",
    )

    verdict = Comparator().compare(baseline, candidate, direction="minimize")

    assert verdict.winner == "candidate"
    assert verdict.p_value < 0.05
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_evaluation.py::test_comparison_uses_fold_scores_when_present tests/test_evaluation.py::test_comparison_rejects_mismatched_fold_scores tests/test_evaluation.py::test_comparison_rejects_non_finite_fold_scores tests/test_evaluation.py::test_comparator_fold_path_respects_minimize_direction -v`
Expected: FAIL — fold scores ignored, comparisons fall to the per-sample/direct path (mismatch test won't raise).

- [ ] **Step 3: Implement the comparator changes**

Rewrite `src/athena/evaluation/comparator.py`:

```python
"""Comparator: paired statistical comparison of experiments."""

import math
import warnings
from collections.abc import Callable, Sequence
from typing import Literal

from scipy import stats

from athena.core.contracts import ArtifactRef
from athena.evaluation.types import ComparisonVerdict, EvalResult


def finite_samples(samples: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(sample) for sample in samples)
    if not values:
        raise ValueError("paired samples must not be empty")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("paired samples must be finite")
    return values


def _paired_fold_samples(
    baseline: EvalResult, candidate: EvalResult
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    baseline_samples = tuple(float(sample) for sample in baseline.fold_scores)
    candidate_samples = tuple(float(sample) for sample in candidate.fold_scores)
    if not baseline_samples and not candidate_samples:
        return None
    if not baseline_samples or not candidate_samples or len(baseline_samples) != len(
        candidate_samples
    ):
        raise ValueError("paired fold scores must both be present and equal in length")
    if not all(
        math.isfinite(value) for value in baseline_samples + candidate_samples
    ):
        raise ValueError("fold scores must be finite")
    return baseline_samples, candidate_samples


def _verdict(
    delta: float,
    p_value: float,
    direction: Literal["maximize", "minimize"],
    alpha: float,
) -> ComparisonVerdict:
    if not math.isfinite(p_value) or p_value >= alpha or delta == 0:
        return ComparisonVerdict(
            winner="tie",
            p_value=1.0 if not math.isfinite(p_value) else p_value,
        )
    candidate_wins = delta > 0 if direction == "maximize" else delta < 0
    return ComparisonVerdict(
        winner="candidate" if candidate_wins else "baseline",
        p_value=p_value,
    )


def _paired_p_value(baseline_samples, candidate_samples) -> float:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return float(stats.ttest_rel(candidate_samples, baseline_samples).pvalue)


def compare_results(
    baseline: EvalResult,
    candidate: EvalResult,
    *,
    direction: Literal["maximize", "minimize"],
    read_samples: Callable[[ArtifactRef], Sequence[float]] | None = None,
    alpha: float = 0.05,
) -> ComparisonVerdict:
    """Compare experiments: fold scores, then paired per-sample, then direct scalar."""
    fold = _paired_fold_samples(baseline, candidate)
    if fold is not None:
        baseline_samples, candidate_samples = fold
        p_value = _paired_p_value(baseline_samples, candidate_samples)
        delta = sum(candidate_samples) / len(candidate_samples) - sum(
            baseline_samples
        ) / len(baseline_samples)
        return _verdict(delta, p_value, direction, alpha)
    if read_samples is not None:
        baseline_samples = finite_samples(read_samples(baseline.per_sample))
        candidate_samples = finite_samples(read_samples(candidate.per_sample))
        if len(baseline_samples) != len(candidate_samples):
            raise ValueError("paired sample lengths differ")
        p_value = _paired_p_value(baseline_samples, candidate_samples)
        delta = candidate.primary - baseline.primary
        return _verdict(delta, p_value, direction, alpha)
    delta = candidate.primary - baseline.primary
    if abs(delta) < 1e-6:
        return ComparisonVerdict(winner="tie", p_value=1.0)
    candidate_wins = delta > 0 if direction == "maximize" else delta < 0
    return ComparisonVerdict(
        winner="candidate" if candidate_wins else "baseline",
        p_value=0.01,
    )


class Comparator:
    """Pairwise statistical comparison of two experiments."""

    def compare(
        self,
        baseline: EvalResult,
        candidate: EvalResult,
        *,
        direction: Literal["maximize", "minimize"] = "maximize",
        read_samples: Callable[[ArtifactRef], Sequence[float]] | None = None,
        alpha: float = 0.05,
    ) -> ComparisonVerdict:
        """Compare candidate vs baseline on the primary metric, return verdict."""
        return compare_results(
            baseline,
            candidate,
            direction=direction,
            read_samples=read_samples,
            alpha=alpha,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_evaluation.py -v`
Expected: PASS — new fold tests and all existing comparator tests.

- [ ] **Step 5: Commit**

```bash
git add src/athena/evaluation/comparator.py tests/test_evaluation.py
git commit -m "feat(evaluation): fold-level paired comparison for CV protocols"
```

---

### Task 6: Integration wiring (description capture + examples)

**Files:**
- Modify: `src/athena/research/runtime.py:186-233`
- Modify: `examples/trial_run.py`
- Modify: `examples/ai4ml_pipeline.py`
- Test: `tests/test_research_runtime.py`

**Interfaces:**
- Consumes: `TaskMetaData.description`, `MetricPlanner`.
- Produces: `PARSE_INTENT` result includes `description`; `TASK_CONFIGURE` stores it; examples build the spec via `MetricPlanner`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_research_runtime.py`:

```python
def test_parse_intent_captures_description() -> None:
    result = ResearchRuntime._parse_intent({"message": "regression on stock prices"})
    assert result["description"] == "regression on stock prices"


@pytest.mark.asyncio
async def test_configure_stores_task_description() -> None:
    runtime = ResearchRuntime()
    params = {**_task_params(), "description": "predict house prices"}
    result = await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, params)
    assert result["task"]["description"] == "predict house prices"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_research_runtime.py::test_parse_intent_captures_description tests/test_research_runtime.py::test_configure_stores_task_description -v`
Expected: FAIL — `description` missing from the parse result / task.

- [ ] **Step 3: Implement the runtime changes**

In `src/athena/research/runtime.py`, `_parse_intent` — add `description` to the returned dict:

```python
        return {
            "task_type": task_type,
            "data_type": data_type,
            "target_vars": [],
            "primary_metric": primary,
            "direction": (
                "minimize" if primary in {"rmse", "mae", "mse"} else "maximize"
            ),
            "description": message,
            "needs_configuration": True,
        }
```

In `_configure`, add `description` to the `TaskMetaData` construction:

```python
        task = TaskMetaData.model_validate(
            {
                "task_type": params.get("task_type", "classification"),
                "data_type": params.get("data_type", "tabular"),
                "target_vars": params.get("target_vars", []),
                "primary_metric": MetricSpec.model_validate(metric),
                "constraints": params.get("constraints", []),
                "description": params.get("description", ""),
            }
        )
```

- [ ] **Step 4: Run the runtime tests**

Run: `uv run pytest tests/test_research_runtime.py -v`
Expected: PASS.

- [ ] **Step 5: Update `examples/trial_run.py`**

Replace the `EvaluatorFactory` import (line 34) and the Step 4 spec build (lines 136-139):

```python
from athena.workflows.prepare.metric_planner import MetricPlanner
```

```python
    # 第 4 步：构建评估规格（LLM 规划器，不可用时回退默认）
    print("\n" + "=" * 60)
    print("STEP 4: Build Evaluation Spec")
    print("=" * 60)

    planner_model = os.environ.get("ATHENA_IDEATOR_MODEL")
    planner = MetricPlanner(
        agent=PydanticAgent(planner_model) if planner_model else None
    )
    plan = await planner.plan(task, profile)
    spec = plan.spec
    print(f"Primary:   {spec.primary.name} ({spec.primary.direction})")
    print(f"Protocol:  {spec.protocol.method}")
    print(f"Rationale: {plan.rationale}")
```

Keep the `create_baseline` call unchanged — `eval_script` now travels inside `spec`.

- [ ] **Step 6: Update `examples/ai4ml_pipeline.py`**

Replace the `EvaluatorFactory` import and Step 3 build:

```python
from athena.workflows.prepare.metric_planner import MetricPlanner
```

```python
    # 3. 构建评估规格（LLM 规划器，不可用时回退默认）
    print("\n" + "=" * 60)
    print("STEP 3: Build Evaluation Spec")
    print("=" * 60)
    planner_model = os.environ.get("ATHENA_IDEATOR_MODEL")
    planner = MetricPlanner(
        agent=PydanticAgent(planner_model) if planner_model else None
    )
    plan = await planner.plan(task, profile)
    spec = plan.spec
    print(f"  Primary metric     : {spec.primary.name}")
    print(f"  Direction          : {spec.primary.direction}")
    print(f"  Protocol           : {spec.protocol.method}")
    print(f"  Secondary metrics  : {[m.name for m in spec.secondary]}")
    print(f"  Rationale          : {plan.rationale}")
```

- [ ] **Step 7: Run the full suite and verify examples import**

Run: `uv run pytest -q`
Expected: PASS.

Run: `uv run python -c "import ast; ast.parse(open('examples/trial_run.py').read()); ast.parse(open('examples/ai4ml_pipeline.py').read()); print('examples parse OK')"`
Expected: `examples parse OK`

- [ ] **Step 8: Commit**

```bash
git add src/athena/research/runtime.py tests/test_research_runtime.py examples/trial_run.py examples/ai4ml_pipeline.py
git commit -m "feat(research): capture task description, wire MetricPlanner into examples"
```
