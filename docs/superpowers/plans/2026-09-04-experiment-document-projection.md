# Experiment Document Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `exp_docs.py` with a strict, injected, canonical-first experiment-document projector whose files are atomic, idempotent, recoverable, and non-fatal to research execution.

**Architecture:** A new `athena.research.experiment_documents` package separates strict records, evaluator metric discovery, pure rendering, atomic storage, and orchestration. `ExperimentDocumentProjector` is injected through `SupervisorRuntime`; it snapshots canonical state, writes a digest manifest last, and returns a sanitized outcome instead of allowing derived-document failures to alter Supervisor state.

**Tech Stack:** Python 3.11+, Pydantic 2, `pathlib`, stdlib JSON/hashlib/os/threading, pytest, pytest-asyncio.

## Global Constraints

- Read `codex_docs/CURRENT.md` and this complete plan before implementation; update the checkbox for every completed step.
- Priority override (2026-09-05): the user explicitly promoted this plan ahead of
  the transactional-search-resume backend and frontend plans. `CURRENT.md` must name
  only this plan as active; both predecessor plans remain paused and their missing
  completion reports are recorded rather than fabricated.
- Preserve unrelated worktree changes. Stage and commit only the exact task-owned paths listed in each task.
- Add no dependency and no GUI, `ResearchTree`, `ResearchState`, artifact-store, or event-schema change.
- Keep `ResearchTree` plus Supervisor `ResearchState` canonical; experiment documents never authorize recovery, phase, or SOTA behavior.
- At the selected base commit, `SupervisorMutationCoordinator` does not exist.
  Preserve the current canonical `ResearchTree`/`ResearchState` save boundaries and
  move every projection after the relevant tree/state saves complete. A later
  transactional-resume integration must adapt those boundaries to the injected
  projector instead of restoring derived `.athena/exp_docs` bytes to transactions.
- Delete `src/athena/research/exp_docs.py`; do not add an import shim for `task_metric_name`, `write_stage_doc`, or `write_reports`.
- Preserve `.athena/exp_docs/{runs,baseline.json,search.json,final.json,latest.json,FINAL_REPORT.md,OPTIMIZATION.md}` and never bulk-rewrite or delete unknown run archives.
- New models use `ConfigDict(extra="forbid", frozen=True, strict=True)` and reject unsafe identifiers, unknown fields, blank references, and non-finite metrics.
- The metric-name order is frozen evaluator `evaluate/metric.json`, legacy flat evaluator `metric.json`, confirmed task understanding, then `primary`; never read `.athena/evaluator_spec.json`.
- Preserve current manual-validation, skipped-validation, resume, and report-artifact
  behavior. The paused predecessor's future completed-search/control behavior is out
  of scope for this branch and must integrate with the projector after this branch
  lands; no nonexistent control transaction is claimed or emulated here.
- Render every batch fully before target replacement. Use unique sibling temp files, flush/fsync, atomic `os.replace`, and replace `latest.json` last.
- Same run ID plus the same normalized record is idempotent. Same run ID plus different semantics raises an internal conflict and preserves the archive and prior manifest.
- Projection and rebuild catch ordinary `Exception`, retain detailed operator logs, and expose only `experiment_documents_stale` plus the exact warning text from the approved spec.
- The concurrency guarantee is per Supervisor runtime; cross-process writers remain outside Athena's supported single-writer model.
- Follow TDD: create the focused failing test, observe the expected failure, implement the minimum complete behavior, rerun the focused test, then commit.
- Use the path-specific `uv run pytest` commands below and `uv run python -c` for
  import smoke so commands use the repository environment.
- Final delivery is mandatory: after feature and closeout commits pass fresh tests,
  merge `feat/experiment-document-projection` into `main`, verify the merged tree,
  push `main`, remove only this plan's isolated worktree, and delete only this
  temporary feature branch. Preserve unrelated main-worktree changes and all other
  branches/worktrees.

---

## File Structure

### New production files

- `src/athena/research/experiment_documents/__init__.py` — narrow package exports only.
- `src/athena/research/experiment_documents/models.py` — strict immutable record and outcome contracts.
- `src/athena/research/experiment_documents/metric.py` — authoritative metric-name resolution.
- `src/athena/research/experiment_documents/render.py` — deterministic JSON and Markdown byte rendering.
- `src/athena/research/experiment_documents/store.py` — immutable archives, atomic batch commit, digest manifest, and locking.
- `src/athena/research/experiment_documents/projector.py` — safe facade, canonical snapshotting, normal projection, and rebuild.

### New focused tests

- `test/unit/research/experiment_documents/__init__.py` — test package marker.
- `test/unit/research/experiment_documents/test_models.py` — record validation and outcome invariants.
- `test/unit/research/experiment_documents/test_metric.py` — metric authority and fallback order.
- `test/unit/research/experiment_documents/test_render.py` — stable JSON, reports, escaping, and GUI parity.
- `test/unit/research/experiment_documents/test_store.py` — archive conflicts, manifests, atomic failures, cleanup, and concurrency.
- `test/unit/research/experiment_documents/test_projector.py` — safe normal projection and canonical rebuild.
- `test/support/__init__.py` — shared test-support package marker.
- `test/support/experiment_documents.py` — no-op/recording projector doubles for existing Supervisor tests.
- `test/unit/research/supervisor/test_document_projection.py` — canonical-first ordering, warning isolation, and recovery wiring.

### Existing production files to modify or delete

- `src/athena/research/supervisor/deps.py` — require `DocumentProjector` in `SupervisorRuntime`.
- `src/athena/research/runtime/bootstrap.py` — build and inject the concrete projector with configured evaluator roots.
- `src/athena/research/supervisor/supervisor.py` — rebuild on recovery and centralize sanitized warning publication.
- `src/athena/research/supervisor/phases.py` — migrate PREPARE/VALIDATE/failure projections to canonical-save-first order.
- `src/athena/research/supervisor/settlement.py` — project SEARCH only after its canonical tree/state saves.
- Delete `src/athena/research/exp_docs.py`.

### Existing tests to modify

- `test/unit/research/supervisor/test_supervisor.py`
- `test/unit/research/supervisor/test_phase_suspend.py`
- `test/unit/research/supervisor/test_ideator_wiring.py`
- `test/unit/research/supervisor/test_plan_turn_streaming.py`
- `test/unit/research/test_data_contract_reaches_candidates.py`
- `test/unit/research/test_plan_hypothesis_prompt.py`
- `test/integration/research/test_search_recovery.py`
- `test/integration/research/test_rolling_search.py`
- `test/integration/research/test_autonomous_research.py`
- Delete `test/unit/research/test_exp_docs.py` after migrating its relevant coverage.

### Workflow documents

- `docs/superpowers/specs/2026-09-04-experiment-document-projection-design.md` — mark approved after plan creation.
- `codex_docs/CURRENT.md` — point to this plan during execution; remove it at closeout.
- Create `codex_docs/2026-09-04-experiment-document-projection-completion-report.md` only after all acceptance checks pass.
- Delete this implementation plan only during verified closeout, as required by `AGENTS.md`.

---

## Execution Preflight and Priority Override

This plan was authored at commit `446aba2`. On 2026-09-05 the user explicitly moved
it ahead of the queued transactional-search-resume backend and frontend plans. The
implementation therefore targets the current direct-save Supervisor interfaces at
base commit `1eae06a`; it does not invent the absent coordinator/control APIs. The
later transactional work must preserve this injected projection boundary.

- [x] Confirm `codex_docs/CURRENT.md` names only this plan as active. Record that the
  two predecessor plan files remain paused and their completion reports do not exist
  because this is an explicit priority override.

- [x] Record base commit `1eae06a1c3dda38f7e4c5dd7f7cc315fd78c9e7b`, existing
  worktrees, clean staged paths, and isolated worktree
  `.worktrees/experiment-document-projection` on
  `feat/experiment-document-projection`.

- [x] Audit changes and actual integration surfaces before editing:

  ```powershell
  git diff --stat 446aba2..HEAD -- src/athena/core/persistence.py src/athena/research/report.py src/athena/research/exp_docs.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/supervisor.py src/athena/research/supervisor/phases.py src/athena/research/supervisor/settlement.py src/athena/research/runtime/bootstrap.py src/athena/research/control test/unit/research/supervisor test/integration/research
  rg -n -e "athena\.research\.exp_docs" -e "task_metric_name" -e "write_stage_doc" -e "write_reports" src test
  rg -n -e "MutationEffect" -e "documents=" -e "commit_plan_settlement" -e "coordinator\.checkpoint" src/athena/research
  ```

- [x] Confirm `SupervisorMutationCoordinator`, `MutationEffect`,
  `ResearchControlService`, `commit_plan_settlement`, and `research/control` are
  absent. Task 7 is amended below to require successful current tree/state saves
  before projection and to keep a narrow injection point for later coordinator work.

- [x] Identify the complete current write surface: only
  `supervisor/phases.py` and `supervisor/settlement.py` call the three legacy free
  functions. No transaction-owned `.athena/exp_docs` bytes exist on this base.

- [x] Run the executable current baseline before Task 1:

  ```powershell
  uv run pytest test/unit/research/supervisor test/unit/research/test_report.py test/integration/research/test_autonomous_research.py test/integration/research/test_search_recovery.py test/integration/research/test_rolling_search.py -q
  ```

  Result: `382 passed in 129.93s`; import smoke for
  `athena.research.supervisor` and `athena.cli` exited 0. The original command's
  nonexistent `test/unit/research/control` and
  `test/integration/research/test_completed_search_resume.py` paths were removed and
  are not reported as passing tests.

The package tasks below are stable across the predecessor work. For Task 3, port the
then-current optimization renderer, including any exploratory-validation label added
by the backend; do not copy the older `446aba2` wording over newer policy.

---

## Acceptance Ownership

| Approved design criterion | Owning task(s) |
| --- | --- |
| Focused package replaces `exp_docs.py`; no free-function shim remains | 5, 7, 8 |
| Existing disk layout and unknown historical run archives are preserved | 4, 6, 8 |
| Frozen evaluator metric path and fallback priority are correct | 2, 5 |
| Records, identifiers, paths, and metrics are strict | 1, 4 |
| Same-run idempotency and different-content conflict preserve history | 4, 6 |
| Canonical tree/state saves precede every normal stage projection | 7 |
| Projection failure is sanitized, observable, and non-fatal | 5, 7 |
| Every target is pre-rendered and atomically replaced; manifest is last | 3, 4 |
| One runtime's projections cannot interleave | 4, 5, 7 |
| Recovery rebuild repairs derivable views without deleting history | 6, 7 |
| Current recovery and skipped-validation flows refresh after canonical saves | 7 |
| GUI and disk final-report bytes are identical | 3, 8 |
| Supervisor/CLI imports and focused/full suites pass | 8 |

---

### Task 1: Define strict document contracts

**Files:**
- Create: `src/athena/research/experiment_documents/__init__.py`
- Create: `src/athena/research/experiment_documents/models.py`
- Create: `test/unit/research/experiment_documents/__init__.py`
- Create: `test/unit/research/experiment_documents/test_models.py`

**Interfaces:**
- Consumes: Pydantic 2 only.
- Produces: `Direction`, `StageName`, `MetricObservation`, `MetricRecord`, `ReasonRecord`, `ProvenanceRecord`, `StageEvent`, `StageRecord`, `LatestManifest`, `ProjectionOutcome`, `DOCUMENTS_STALE_CODE`, and `DOCUMENTS_STALE_MESSAGE`.

- [ ] **Step 1: Write the model validation tests**

Create a reusable valid payload and enumerate each rejected boundary explicitly:

```python
from math import inf, nan

import pytest
from pydantic import ValidationError

from athena.research.experiment_documents.models import (
    DOCUMENTS_STALE_CODE,
    DOCUMENTS_STALE_MESSAGE,
    LatestManifest,
    ProjectionOutcome,
    StageEvent,
    StageRecord,
)


def _event_payload() -> dict[str, object]:
    return {
        "run_id": "exp_search-1",
        "stage": "search",
        "status": "SUCCEEDED",
        "metric": {
            "primary": 0.81,
            "reference": 0.78,
            "generalization_gap": None,
            "secondary": {"f1": 0.79},
        },
        "artifacts": {"evidence": "sha256:evidence"},
        "reason": {"kind": "trusted_score", "summary": "Trusted score won."},
        "provenance": {
            "experiment_id": "exp_search-1",
            "hypothesis_id": "search-1",
            "commit": "abc123",
        },
    }


@pytest.mark.parametrize(
    "run_id",
    ["", "../escape", "a..b", "a/b", r"a\\b", "a:", "name.", "CON"],
)
def test_stage_event_rejects_unsafe_run_ids(run_id: str) -> None:
    payload = _event_payload()
    payload["run_id"] = run_id
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


@pytest.mark.parametrize("value", [nan, inf, -inf])
@pytest.mark.parametrize("field", ["primary", "reference", "generalization_gap"])
def test_stage_event_rejects_non_finite_metrics(field: str, value: float) -> None:
    payload = _event_payload()
    payload["metric"][field] = value
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


def test_stage_event_rejects_non_finite_secondary_metric() -> None:
    payload = _event_payload()
    payload["metric"]["secondary"] = {"f1": nan}
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("stage",), "prepare"),
        (("status",), "succeeded"),
        (("artifacts",), {"": "ref"}),
        (("reason", "summary"), ""),
        (("provenance", "commit"), "\n"),
    ],
)
def test_stage_event_rejects_invalid_nested_values(path, value) -> None:
    payload = _event_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


def test_models_forbid_extra_fields_at_every_level() -> None:
    payload = _event_payload()
    payload["metric"]["surprise"] = 1
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


def test_stage_record_adds_resolved_metric_contract() -> None:
    event = StageEvent.model_validate(_event_payload())
    record = StageRecord.from_event(event, name="accuracy", direction="maximize")
    assert record.schema_version == 1
    assert record.metric.name == "accuracy"
    assert record.metric.direction == "maximize"


def test_latest_manifest_requires_stage_identity_only_for_stage_kind() -> None:
    digest = "a" * 64
    stage = LatestManifest(
        schema_version=1,
        projection_id=digest,
        kind="stage",
        stage="search",
        run_id="exp_search-1",
        files={"search.json": digest},
    )
    assert stage.run_id == "exp_search-1"
    invalid = stage.model_dump(mode="json")
    invalid["run_id"] = None
    with pytest.raises(ValidationError):
        LatestManifest.model_validate(invalid)


def test_projection_outcome_never_carries_internal_failure_details() -> None:
    failed = ProjectionOutcome.stale()
    assert failed.model_dump() == {
        "ok": False,
        "projection_id": None,
        "warning_code": DOCUMENTS_STALE_CODE,
        "warning_message": DOCUMENTS_STALE_MESSAGE,
    }
```

Use a small nested-assignment helper instead of relying on `dict` type inference if
the type checker objects. Also test blank metric names, invalid directions, empty
provenance, extra top-level/reason/provenance fields, malformed SHA-256 values, and
the inverse `LatestManifest`/`ProjectionOutcome` invariants.

- [ ] **Step 2: Run the new model test and observe the missing package failure**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_models.py -q
```

Expected: collection fails with `ModuleNotFoundError` for
`athena.research.experiment_documents`.

- [ ] **Step 3: Implement the strict models**

Use one private strict base model and field/model validators; do not silently coerce
or sanitize caller values:

```python
Direction = Literal["maximize", "minimize"]
StageName = Literal["baseline", "search", "final"]
ProjectionKind = Literal["stage", "rebuild"]
DOCUMENTS_STALE_CODE = "experiment_documents_stale"
DOCUMENTS_STALE_MESSAGE = (
    "Experiment documents could not be refreshed; canonical research state is "
    "safe and the documents will be rebuilt on recovery."
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MetricObservation(_StrictModel):
    primary: float | None = None
    reference: float | None = None
    generalization_gap: float | None = None
    secondary: dict[str, float] = Field(default_factory=dict)


class MetricRecord(MetricObservation):
    name: str
    direction: Direction


class ReasonRecord(_StrictModel):
    kind: str
    summary: str


class ProvenanceRecord(_StrictModel):
    phase: Literal["PREPARE", "SEARCH", "VALIDATE"] | None = None
    experiment_id: str | None = None
    hypothesis_id: str | None = None
    commit: str | None = None
    sota_experiment_id: str | None = None
    sota_commit: str | None = None
    validation_commit: str | None = None


class StageEvent(_StrictModel):
    run_id: str
    stage: StageName
    status: str
    metric: MetricObservation
    artifacts: dict[str, str] = Field(default_factory=dict)
    reason: ReasonRecord
    provenance: ProvenanceRecord


class StageRecord(_StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    stage: StageName
    status: str
    metric: MetricRecord
    artifacts: dict[str, str] = Field(default_factory=dict)
    reason: ReasonRecord
    provenance: ProvenanceRecord

    @classmethod
    def from_event(
        cls, event: StageEvent, *, name: str, direction: Direction
    ) -> "StageRecord":
        return cls(
            **event.model_dump(exclude={"metric"}),
            metric=MetricRecord(
                **event.metric.model_dump(), name=name, direction=direction
            ),
        )
```

Apply these exact validation rules:

- run ID regex `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`, no `..`, no trailing `.`, and
  reject `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, and `LPT1`-`LPT9` before the first
  dot, case-insensitively;
- status regex `^[A-Z][A-Z0-9_-]{0,63}$`;
- reason kind regex `^[a-z][a-z0-9_]{0,63}$`, stripped summary length 1-1000;
- metric names, artifact keys/values, and provenance strings must already have no
  leading/trailing whitespace and must be single-line, nonblank, and bounded (128 for
  names/keys, 4096 for references); reject instead of rewriting invalid caller text;
- at least one provenance field must be non-null;
- all metric floats, including every secondary value, pass `math.isfinite`;
- manifest paths are safe relative POSIX paths, digests/projection ID are lowercase
  64-character hex, and kind/stage/run conditional fields match;
- `ProjectionOutcome.success(id)` and `.stale()` are the only constructors used by
  production; a model validator enforces their mutually exclusive fields.

Keep `__init__.py` to a package docstring for now; Task 5 adds the final exports after
the projector exists.

- [ ] **Step 4: Run model tests and the formatter check**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_models.py -q
uv run black --check src/athena/research/experiment_documents/models.py test/unit/research/experiment_documents/test_models.py
```

Expected: all model tests pass and Black reports both files unchanged.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- src/athena/research/experiment_documents/__init__.py src/athena/research/experiment_documents/models.py test/unit/research/experiment_documents/__init__.py test/unit/research/experiment_documents/test_models.py
git commit -m "feat(research): define experiment document contracts"
```

---

### Task 2: Resolve the actual frozen evaluator metric

**Files:**
- Create: `src/athena/research/experiment_documents/metric.py`
- Create: `test/unit/research/experiment_documents/test_metric.py`

**Interfaces:**
- Consumes: `athena.research.evaluation.spec.load_evaluator_spec` and `load_metric_json`.
- Produces: `MetricResolver(evaluator_roots: Iterable[Path])` and
  `resolve(task_understanding: Mapping[str, object] | None) -> str`.

- [ ] **Step 1: Write authority-order and invalid-file tests**

```python
import json
from pathlib import Path

from athena.research.experiment_documents.metric import MetricResolver


def _write_metric(root: Path, payload: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metric.json").write_text(json.dumps(payload), encoding="utf-8")


def _v2_metric(name: str) -> dict[str, object]:
    return {
        "contract_version": 2,
        "task_id": "demo",
        "task_type": "regression",
        "primary_metric": name,
        "class_labels": [],
        "prediction_file": "predictions__demo.csv",
        "prediction_id_column": "__athena_row_id",
        "prediction_column": "prediction",
        "probability_columns": [],
        "metrics_file": "metrics_public_test.csv",
        "eval_script": "eval_metrics.py",
        "prediction_format": "tabular_csv",
    }


def test_frozen_evaluate_metric_wins_over_every_fallback(tmp_path: Path) -> None:
    evaluate = tmp_path / "workspaces" / "evaluator" / "evaluate"
    flat = evaluate.parent
    _write_metric(evaluate, _v2_metric("roc_auc"))
    _write_metric(flat, {"primary_metric": "accuracy"})
    resolver = MetricResolver((evaluate, flat))
    assert resolver.resolve({"primary_metric": "f1"}) == "roc_auc"


def test_legacy_flat_metric_wins_when_evaluate_is_absent(tmp_path: Path) -> None:
    flat = tmp_path / "workspaces" / "evaluator"
    _write_metric(flat, {"primary_metric": "mae"})
    resolver = MetricResolver((flat / "evaluate", flat))
    assert resolver.resolve({"primary_metric": "rmse"}) == "mae"


def test_invalid_evaluator_falls_through_with_diagnostic(tmp_path, caplog) -> None:
    evaluate = tmp_path / "evaluate"
    _write_metric(evaluate, {"task_id": "partial", "primary_metric": "bad"})
    assert MetricResolver((evaluate,)).resolve({"primary_metric": "f1"}) == "f1"
    assert "invalid evaluator metric" in caplog.text


def test_task_understanding_then_literal_fallback(tmp_path: Path) -> None:
    resolver = MetricResolver((tmp_path / "missing",))
    assert resolver.resolve({"primary_metric": " balanced_accuracy "}) == "balanced_accuracy"
    assert resolver.resolve({"primary_metric": " "}) == "primary"
    assert resolver.resolve(None) == "primary"
```

Also test malformed JSON, a non-object JSON payload, a blank legacy
`primary_metric`, and a valid second evaluator candidate after an invalid first one.
Assert no test creates `.athena/evaluator_spec.json`.

- [ ] **Step 2: Run the resolver test and observe the missing module failure**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_metric.py -q
```

Expected: collection fails because `experiment_documents.metric` does not exist.

- [ ] **Step 3: Implement `MetricResolver`**

```python
class MetricResolver:
    def __init__(self, evaluator_roots: Iterable[Path]) -> None:
        self._roots = tuple(Path(root) for root in evaluator_roots)

    def resolve(self, task_understanding: Mapping[str, object] | None) -> str:
        for root in self._roots:
            if not (root / "metric.json").is_file():
                continue
            try:
                payload = load_metric_json(root)
                if "task_id" in payload:
                    spec = load_evaluator_spec(root, legacy_ok=False)
                    assert spec is not None
                    return spec.primary_metric.strip()
                name = payload.get("primary_metric")
                if isinstance(name, str) and name.strip():
                    return name.strip()
                raise ValueError("primary_metric is blank or missing")
            except (OSError, TypeError, ValueError):
                logger.warning("invalid evaluator metric at %s", root, exc_info=True)
        if isinstance(task_understanding, Mapping):
            name = task_understanding.get("primary_metric")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "primary"
```

Do not catch a valid first result and continue. Do catch invalid metadata and try the
next configured evaluator candidate before task-understanding fallback.

- [ ] **Step 4: Run focused resolver and evaluator-spec tests**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_metric.py test/unit/research/test_evaluator_spec.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- src/athena/research/experiment_documents/metric.py test/unit/research/experiment_documents/test_metric.py
git commit -m "feat(research): resolve projected metric authority"
```

---

### Task 3: Render deterministic document bytes

**Files:**
- Create: `src/athena/research/experiment_documents/render.py`
- Create: `test/unit/research/experiment_documents/test_render.py`

**Interfaces:**
- Consumes: `StageRecord`, `LatestManifest`, `ResearchTree`, and
  `athena.research.report.build_final_report`.
- Produces: `render_stage_record`, `build_latest_manifest`,
  `render_latest_manifest`, `render_final_report`, and
  `render_optimization_report`, all returning immutable values/bytes.

- [ ] **Step 1: Write deterministic JSON and manifest tests**

```python
import hashlib
import json

from athena.research.experiment_documents.render import (
    build_latest_manifest,
    render_latest_manifest,
    render_stage_record,
)


def test_stage_json_is_sorted_utf8_lf_and_final_newline(stage_record) -> None:
    rendered = render_stage_record(stage_record)
    assert rendered.endswith(b"\n")
    assert b"\r\n" not in rendered
    assert json.loads(rendered) == stage_record.model_dump(mode="json")
    assert rendered.index(b'"artifacts"') < rendered.index(b'"metric"')


def test_manifest_hashes_exact_effective_bytes_and_is_content_derived() -> None:
    files = {"runs/exp_1.json": b"old bytes\n", "search.json": b"new bytes\n"}
    first = build_latest_manifest(
        kind="stage", stage="search", run_id="exp_1", files=files
    )
    second = build_latest_manifest(
        kind="stage", stage="search", run_id="exp_1", files=dict(reversed(files.items()))
    )
    assert first == second
    assert first.files["runs/exp_1.json"] == hashlib.sha256(files["runs/exp_1.json"]).hexdigest()
    assert json.loads(render_latest_manifest(first))["projection_id"] == first.projection_id
```

Add a rebuild-manifest test proving `stage` and `run_id` are null and path order does
not change `projection_id`.

- [ ] **Step 2: Write report parity, sorting, and escaping tests**

Build a small tree with two scored experiments, one failed experiment, and one SOTA.
Assert:

```python
def test_disk_final_report_is_exact_shared_builder_bytes(tree, validation) -> None:
    assert render_final_report(
        tree, validation, validation_skipped=False
    ) == build_final_report(
        tree, validation, validation_skipped=False
    ).encode("utf-8")


def test_optimization_report_sorts_for_both_directions(tree) -> None:
    maximize = render_optimization_report(
        tree, None, metric_name="score", direction="maximize", validation_skipped=False
    ).decode()
    minimize = render_optimization_report(
        tree, None, metric_name="score", direction="minimize", validation_skipped=False
    ).decode()
    assert maximize.index("`exp_high`") < maximize.index("`exp_low`")
    assert minimize.index("`exp_low`") < minimize.index("`exp_high`")


def test_optimization_report_escapes_dynamic_markdown_cells(tree) -> None:
    rendered = render_optimization_report(
        tree,
        None,
        metric_name="score|unsafe\nline",
        direction="maximize",
        validation_skipped=False,
    ).decode()
    assert "score\\|unsafe line" in rendered
```

Also retain the unscored section, SOTA mark, six-decimal score formatting, VALIDATE
fields, and terminal newline from the old renderer.

- [ ] **Step 3: Run render tests and observe missing functions**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_render.py -q
```

Expected: collection fails because `render.py` does not exist.

- [ ] **Step 4: Implement pure renderers**

Use deterministic JSON and digest construction:

```python
def _json_bytes(payload: Mapping[str, object]) -> bytes:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return f"{text}\n".encode("utf-8")


def render_stage_record(record: StageRecord) -> bytes:
    return _json_bytes(record.model_dump(mode="json"))


def build_latest_manifest(*, kind, stage, run_id, files) -> LatestManifest:
    digests = {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(files.items())
    }
    identity = _json_bytes(
        {"schema_version": 1, "kind": kind, "stage": stage,
         "run_id": run_id, "files": digests}
    )
    return LatestManifest(
        schema_version=1,
        projection_id=hashlib.sha256(identity).hexdigest(),
        kind=kind,
        stage=stage,
        run_id=run_id,
        files=digests,
    )


def render_final_report(
    tree: ResearchTree,
    validation: Mapping[str, object] | None,
    *,
    validation_skipped: bool,
) -> bytes:
    return build_final_report(
        tree, validation, validation_skipped=validation_skipped
    ).encode("utf-8")
```

Pass the same required `validation_skipped` flag through the optimization renderer so
both reports preserve the current explicit skip disclosure. Port the current
optimization-report content without importing the old module. Escape
backslashes first, then pipes, CR, and LF in dynamic Markdown text. Do not add a final
newline to `FINAL_REPORT.md`; shared-builder byte parity is the contract.

- [ ] **Step 5: Run render and shared-report tests**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_render.py test/unit/research/test_report.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- src/athena/research/experiment_documents/render.py test/unit/research/experiment_documents/test_render.py
git commit -m "feat(research): render experiment document batches"
```

---

### Task 4: Commit immutable run archives and atomic batches

**Files:**
- Create: `src/athena/research/experiment_documents/store.py`
- Create: `test/unit/research/experiment_documents/test_store.py`

**Interfaces:**
- Consumes: `StageRecord`, `LatestManifest`, `render_stage_record`,
  `build_latest_manifest`, and `render_latest_manifest`.
- Produces: `RunDocumentConflict`, `RunCandidate`, `ProjectionBatch`, and
  `DocumentStore.commit(batch: ProjectionBatch) -> str`.

- [ ] **Step 1: Write layout, idempotency, legacy, and conflict tests**

Use helpers that build one `StageRecord`, its rendered bytes, and this batch:

```python
batch = ProjectionBatch(
    kind="stage",
    stage="search",
    run_id=record.run_id,
    runs=(RunCandidate(record=record, content=render_stage_record(record)),),
    aliases={"search": record.run_id},
    reports={
        "FINAL_REPORT.md": b"report",
        "OPTIMIZATION.md": b"optimization\n",
    },
)
```

Assert the complete behavior:

```python
def test_commit_creates_layout_and_digest_valid_latest(tmp_path, batch) -> None:
    store = DocumentStore(tmp_path / ".athena" / "exp_docs")
    projection_id = store.commit(batch)
    root = store.root
    assert (root / "runs" / "exp_search-1.json").read_bytes() == batch.runs[0].content
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["projection_id"] == projection_id
    for relative, digest in latest["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest


def test_same_semantics_preserve_existing_archive_bytes(tmp_path, batch) -> None:
    store = DocumentStore(tmp_path / "docs")
    store.commit(batch)
    run_path = store.root / "runs" / "exp_search-1.json"
    reformatted = json.dumps(json.loads(run_path.read_text()), separators=(",", ":"))
    run_path.write_text(reformatted, encoding="utf-8")
    store.commit(batch)
    assert run_path.read_text(encoding="utf-8") == reformatted
    latest = json.loads((store.root / "latest.json").read_text())
    assert latest["files"]["runs/exp_search-1.json"] == hashlib.sha256(
        reformatted.encode()
    ).hexdigest()


def test_unversioned_restored_shape_is_semantically_idempotent(tmp_path, batch) -> None:
    legacy = batch.runs[0].record.model_dump(mode="json", exclude={"schema_version"})
    # Pre-create legacy JSON, commit, then assert bytes are unchanged and manifest hashes them.


def test_same_run_with_different_content_never_overwrites(tmp_path, batch) -> None:
    store = DocumentStore(tmp_path / "docs")
    store.commit(batch)
    before_run = (store.root / "runs" / "exp_search-1.json").read_bytes()
    before_latest = (store.root / "latest.json").read_bytes()
    changed = batch_with_primary(batch, 0.99)
    with pytest.raises(RunDocumentConflict):
        store.commit(changed)
    assert (store.root / "runs" / "exp_search-1.json").read_bytes() == before_run
    assert (store.root / "latest.json").read_bytes() == before_latest
```

Also assert malformed/extra-field legacy records conflict, integer legacy metrics are
normalized only by the compatibility parser, unknown run files remain byte-identical,
absolute/`..`/backslash paths are rejected, aliases can reference only a run in the
same batch, and reports accept exactly `FINAL_REPORT.md` and `OPTIMIZATION.md`.

- [ ] **Step 2: Write failure-injection and temp cleanup tests**

Monkeypatch `athena.research.experiment_documents.store.os.replace` with a counted
wrapper. Establish one successful old batch first, then fail:

- before the first target replace;
- after the new run but before the alias;
- after the alias but before reports complete;
- on the final `latest.json` replace.

For every case assert the old `latest.json` bytes are unchanged and no file matching
`*.tmp` or `.*.tmp` remains below the store root. For a pre-replace temp-write/fsync
failure, assert no target bytes changed. The test must not claim rollback of targets
already replaced before a mid-batch failure; it instead proves their hashes do not
match the still-authoritative old manifest.

- [ ] **Step 3: Write the concurrent serialization test**

Use two Python threads, a `Barrier`, and an instrumented `os.replace`. Block the first
thread during its first replace, start the second, then release the first. Record each
target replacement and assert all replacements for batch A are contiguous, followed
by all replacements for batch B; `latest.json` is last in each group and the final
manifest identifies B.

```python
with ThreadPoolExecutor(max_workers=2) as pool:
    first = pool.submit(store.commit, batch_a)
    entered_first_replace.wait(timeout=2)
    second = pool.submit(store.commit, batch_b)
    release_first_replace.set()
    assert first.result(timeout=2)
    final_id = second.result(timeout=2)

assert json.loads((store.root / "latest.json").read_text())["projection_id"] == final_id
```

- [ ] **Step 4: Run store tests and observe the missing module failure**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_store.py -q
```

Expected: collection fails because `store.py` does not exist.

- [ ] **Step 5: Implement batch validation and legacy semantic comparison**

Use frozen dataclasses for persistence inputs:

```python
@dataclass(frozen=True)
class RunCandidate:
    record: StageRecord
    content: bytes
    match_mode: Literal["exact", "recoverable"] = "exact"


@dataclass(frozen=True)
class ProjectionBatch:
    kind: Literal["stage", "rebuild"]
    stage: StageName | None
    run_id: str | None
    runs: Sequence[RunCandidate]
    aliases: Mapping[StageName, str]
    reports: Mapping[Literal["FINAL_REPORT.md", "OPTIMIZATION.md"], bytes]


class RunDocumentConflict(ValueError):
    pass
```

Validate the kind/identity invariant, require both report names, reject duplicate run
IDs, and require every alias to reference a run candidate in
`ProjectionBatch.__post_init__`. Defensively copy `runs`, `aliases`, and `reports`
there (tuples plus read-only mapping proxies) so callers cannot mutate a prepared
batch while another thread waits for the store lock.
For each run path, parse existing JSON as `StageRecord`; when `schema_version` is
absent, add `1` to a copy before validation. Reject unknown fields. Normalize only
legacy integer metric values to floats because the old writer serialized caller
numbers without strict typing.

Exact matching compares complete `model_dump(mode="json")` values. Recoverable
matching, needed by Task 6, compares run/stage/status, primary/secondary/artifacts,
every non-null reconstructed metric reference/gap, and every non-null reconstructed
provenance field; it intentionally ignores display name, direction, and reason.

- [ ] **Step 6: Implement locked preflight, temp staging, and ordered replace**

```python
class DocumentStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def commit(self, batch: ProjectionBatch) -> str:
        with self._lock:
            effective, replacements = self._preflight(batch)
            manifest = build_latest_manifest(
                kind=batch.kind,
                stage=batch.stage,
                run_id=batch.run_id,
                files=effective,
            )
            latest_bytes = render_latest_manifest(manifest)
            ordered = [*replacements, ("latest.json", latest_bytes)]
            temporary: dict[str, Path] = {}
            try:
                for relative, content in ordered:
                    temporary[relative] = self._write_temp(relative, content)
                for relative, _content in ordered:
                    os.replace(temporary[relative], self._target(relative))
            finally:
                for path in temporary.values():
                    path.unlink(missing_ok=True)
            return manifest.projection_id
```

Implementation requirements:

- resolve and retain the absolute root once;
- reject absolute paths, `..`, backslashes, and resolved targets outside root;
- create `runs/` and parent directories without deleting anything;
- use `.<target-name>.<uuid4 hex>.tmp` opened with `xb` beside each target;
- write bytes, flush, and `os.fsync` every temp before the first replace;
- order new run archives by run ID, then aliases in baseline/search/final order, then
  `FINAL_REPORT.md`, `OPTIMIZATION.md`, and finally `latest.json`;
- omit semantically identical existing run archives from replacements but put their
  actual existing bytes in the manifest's effective file map;
- build every alias replacement from the selected run's *effective* bytes, so a
  compatible enriched/legacy archive is also the exact alias content during rebuild;
- catch no exception in `DocumentStore`; the safe projector boundary owns logging and
  sanitized outcomes.

- [ ] **Step 7: Run all storage-focused tests repeatedly**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_store.py -q
uv run pytest test/unit/research/experiment_documents/test_store.py -q
uv run pytest test/unit/research/experiment_documents/test_store.py -q
uv run pytest test/unit/research/experiment_documents/test_store.py -q
uv run pytest test/unit/research/experiment_documents/test_store.py -q
```

Expected: all five runs pass with no intermittent concurrency failure. Do not add a
repeat-test dependency.

- [ ] **Step 8: Commit Task 4**

```powershell
git add -- src/athena/research/experiment_documents/store.py test/unit/research/experiment_documents/test_store.py
git commit -m "feat(research): atomically commit experiment documents"
```

---

### Task 5: Project one stage through a safe facade

**Files:**
- Modify: `src/athena/research/experiment_documents/__init__.py`
- Create: `src/athena/research/experiment_documents/projector.py`
- Create: `test/unit/research/experiment_documents/test_projector.py`

**Interfaces:**
- Consumes: Tasks 1-4 contracts, renderers, resolver, store, and `ResearchTree`.
- Produces: `DocumentProjector` protocol and
  `ExperimentDocumentProjector(document_root: Path, evaluator_roots: Iterable[Path])`.

- [ ] **Step 1: Write normal projection and safe-failure tests**

```python
def test_project_stage_writes_archive_alias_reports_and_valid_manifest(
    tmp_path, research_tree
) -> None:
    projector = ExperimentDocumentProjector(
        tmp_path / ".athena" / "exp_docs",
        (tmp_path / "workspaces" / "evaluator" / "evaluate",
         tmp_path / "workspaces" / "evaluator"),
    )
    outcome = projector.project_stage(
        valid_search_event(),
        tree=research_tree,
        validation=None,
        validation_skipped=False,
        task_understanding={"primary_metric": "f1"},
        direction="maximize",
    )
    assert outcome.ok is True
    assert json.loads((tmp_path / ".athena/exp_docs/search.json").read_text())[
        "metric"
    ]["name"] == "f1"
    assert (tmp_path / ".athena/exp_docs/runs/exp_search-1.json").is_file()
    assert_manifest_matches_files(tmp_path / ".athena/exp_docs/latest.json")


def test_invalid_event_returns_only_sanitized_failure_and_writes_nothing(
    tmp_path, research_tree, caplog
) -> None:
    projector = ExperimentDocumentProjector(
        tmp_path / "docs", (tmp_path / "missing",)
    )
    outcome = projector.project_stage(
        {**valid_search_event(), "run_id": "../escape"},
        tree=research_tree,
        validation=None,
        validation_skipped=False,
        task_understanding=None,
        direction="maximize",
    )
    assert outcome == ProjectionOutcome.stale()
    assert not (tmp_path / "docs").exists()
    assert "../escape" not in outcome.warning_message
    assert "document projection failed" in caplog.text
```

Also test a store conflict and injected `os.replace` failure return `.stale()` rather
than raising, frozen evaluator metadata wins, and the input tree/validation mappings
are copied before rendering by mutating originals from an injected store callback.

- [ ] **Step 2: Run the projector test and observe missing symbols**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_projector.py -q
```

Expected: collection fails because the projector symbols do not exist.

- [ ] **Step 3: Implement the protocol and normal-stage facade**

Use the approved signature verbatim:

```python
class DocumentProjector(Protocol):
    def project_stage(
        self,
        event: Mapping[str, object],
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        validation_skipped: bool,
        task_understanding: Mapping[str, object] | None,
        direction: Direction,
    ) -> ProjectionOutcome:
        raise NotImplementedError

    def rebuild(
        self,
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        validation_skipped: bool,
        task_understanding: Mapping[str, object] | None,
        direction: Direction,
    ) -> ProjectionOutcome:
        raise NotImplementedError
```

The concrete implementation performs all risky work inside one `try/except Exception`:

```python
def project_stage(
    self,
    event: Mapping[str, object],
    *,
    tree: ResearchTree,
    validation: Mapping[str, object] | None,
    validation_skipped: bool,
    task_understanding: Mapping[str, object] | None,
    direction: Direction,
) -> ProjectionOutcome:
    try:
        parsed = StageEvent.model_validate(event)
        tree_copy = ResearchTree.from_dict(copy.deepcopy(tree.to_dict()))
        validation_copy = copy.deepcopy(dict(validation)) if validation else None
        task_copy = copy.deepcopy(dict(task_understanding)) if task_understanding else None
        metric_name = self._metrics.resolve(task_copy)
        record = StageRecord.from_event(parsed, name=metric_name, direction=direction)
        record_bytes = render_stage_record(record)
        batch = ProjectionBatch(
            kind="stage",
            stage=record.stage,
            run_id=record.run_id,
            runs=(RunCandidate(record=record, content=record_bytes),),
            aliases={record.stage: record.run_id},
            reports={
                "FINAL_REPORT.md": render_final_report(
                    tree_copy,
                    validation_copy,
                    validation_skipped=validation_skipped,
                ),
                "OPTIMIZATION.md": render_optimization_report(
                    tree_copy,
                    validation_copy,
                    metric_name=metric_name,
                    direction=direction,
                    validation_skipped=validation_skipped,
                ),
            },
        )
        return ProjectionOutcome.success(self._store.commit(batch))
    except Exception:
        logger.warning(
            "document projection failed for stage=%r run_id=%r",
            event.get("stage"),
            event.get("run_id"),
            exc_info=True,
        )
        return ProjectionOutcome.stale()
```

`rebuild` can raise `NotImplementedError` only inside its safe `try` and return stale
temporarily; Task 6 replaces it before Supervisor integration. Do not expose the
store, resolver, internal exception, or target paths in `ProjectionOutcome`.

- [ ] **Step 4: Export only the narrow package API**

`__init__.py` exports:

```python
from .models import ProjectionOutcome, StageEvent
from .projector import DocumentProjector, ExperimentDocumentProjector

__all__ = [
    "DocumentProjector",
    "ExperimentDocumentProjector",
    "ProjectionOutcome",
    "StageEvent",
]
```

It must not export storage classes, render helpers, metric helpers, or any old free
function name.

- [ ] **Step 5: Run projector and all package tests**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents -q
```

Expected: all tests pass except no skipped or xfailed rebuild tests; rebuild-specific
tests are added in Task 6 rather than pre-created as skips.

- [ ] **Step 6: Commit Task 5**

```powershell
git add -- src/athena/research/experiment_documents/__init__.py src/athena/research/experiment_documents/projector.py test/unit/research/experiment_documents/test_projector.py
git commit -m "feat(research): add experiment document projector"
```

---

### Task 6: Rebuild derivable views from canonical state

**Files:**
- Modify: `src/athena/research/experiment_documents/projector.py`
- Modify: `test/unit/research/experiment_documents/test_projector.py`

**Interfaces:**
- Consumes: `DocumentStore` recoverable matching and the canonical tree/validation
  snapshots.
- Produces: complete `ExperimentDocumentProjector.rebuild` behavior returning
  `ProjectionOutcome`.

- [ ] **Step 1: Add rebuild selection and preservation tests**

Create a tree in insertion order containing `exp_baseline`, two terminal SEARCH
experiments, one running SEARCH experiment, and validation. Assert:

```python
def test_rebuild_restores_all_derivable_runs_and_latest_aliases(
    tmp_path, completed_tree, validation
) -> None:
    root = tmp_path / ".athena" / "exp_docs"
    unknown = root / "runs" / "historical-unknown.json"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b'{"opaque": true}\n')
    projector = projector_for(tmp_path)

    outcome = projector.rebuild(
        tree=completed_tree,
        validation=validation,
        validation_skipped=False,
        task_understanding={"primary_metric": "accuracy"},
        direction="maximize",
    )

    assert outcome.ok
    assert unknown.read_bytes() == b'{"opaque": true}\n'
    assert (root / "runs" / "exp_baseline.json").is_file()
    assert (root / "runs" / "exp_search-1.json").is_file()
    assert (root / "runs" / "exp_search-2.json").is_file()
    assert not (root / "runs" / "exp_running.json").exists()
    assert json.loads((root / "search.json").read_text())["run_id"] == "exp_search-2"
    assert json.loads((root / "latest.json").read_text())["kind"] == "rebuild"
```

Add exact tests for:

- baseline alias is `exp_baseline` only;
- terminal means `SUCCEEDED`, `FAILED`, or `CANCELLED`, never `PENDING`/`RUNNING`;
- final is generated only for terminal validation (`status` plus `result_id`, or a
  legacy terminal validation with score/status and fallback ID `final`), not a
  checkpoint containing only `result_ref`;
- `validation_skipped=True` reconstructs the explicit skipped-validation report and
  final alias from the canonical SOTA without inventing validation metrics;
- existing enriched compatible run bytes are retained under recoverable matching;
- an existing run contradicting canonical status/primary/artifacts/provenance aborts
  the batch and retains its bytes, the old alias, and the old manifest;
- reconstructed SEARCH reference is null and reason kind is
  `reconstructed_from_canonical_state`;
- phase-failure records are never invented;
- empty tree plus absent document root is a no-op and does not create directories;
- empty tree plus existing document root refreshes the two reports and a rebuild
  manifest without deleting files.

- [ ] **Step 2: Run only rebuild tests and observe failures**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents/test_projector.py -q -k rebuild
```

Expected: tests fail because `rebuild` still returns the temporary stale outcome.

- [ ] **Step 3: Implement deterministic reconstruction helpers**

Keep helpers private to `projector.py`:

```python
_TERMINAL_EXPERIMENTS = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _experiment_record(
    experiment_id: str,
    experiment: Mapping[str, object],
    *,
    stage: Literal["baseline", "search"],
    metric_name: str,
    direction: Direction,
) -> StageRecord:
    evaluation = experiment.get("eval") or {}
    return StageRecord(
        schema_version=1,
        run_id=experiment_id,
        stage=stage,
        status=str(experiment["status"]),
        metric=MetricRecord(
            name=metric_name,
            direction=direction,
            primary=evaluation.get("primary"),
            reference=None,
            generalization_gap=None,
            secondary=evaluation.get("secondary") or {},
        ),
        artifacts=experiment.get("artifacts") or {},
        reason=ReasonRecord(
            kind="reconstructed_from_canonical_state",
            summary=experiment.get("error") or "Recovered from canonical ResearchTree.",
        ),
        provenance=ProvenanceRecord(
            experiment_id=experiment_id,
            hypothesis_id=experiment["hypothesis_id"],
            commit=experiment["commit"],
        ),
    )
```

Build the final record from validated terminal validation fields, preserving final
score, search reference, gap, `*_ref` artifacts, SOTA ID/commit, and validation
commit. Iterate the ordered `tree.to_dict()["experiments"]` mapping, add every
derivable run candidate with `match_mode="recoverable"`, and update each alias to the
selected record bytes.

- [ ] **Step 4: Implement safe rebuild commit and no-op behavior**

Inside `rebuild`'s `try` block:

1. snapshot the inputs exactly as `project_stage` does;
2. return `ProjectionOutcome.success(NOOP_PROJECTION_ID)` without creating the root
   when no experiment/final record is derivable and `DocumentStore.root` is absent;
3. render reports and select each alias's run ID in memory; the store resolves aliases
   to effective immutable-run bytes under its lock;
4. commit one rebuild `ProjectionBatch` with `stage=None`, `run_id=None`, the complete
   run candidate sequence, selected aliases, and both rendered reports;
5. return success with the committed manifest ID;
6. on any ordinary exception, log the traceback and return `.stale()`.

Define `NOOP_PROJECTION_ID` as SHA-256 of the fixed ASCII string
`athena:experiment-documents:no-op:v1`; it is an operation identity only and is never
written as `latest.json`.

- [ ] **Step 5: Run package tests and repeat the rebuild conflict case**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents -q
uv run pytest test/unit/research/experiment_documents/test_projector.py -q -k "rebuild and conflict"
```

Expected: all tests pass and no archive changes in the conflict test.

- [ ] **Step 6: Commit Task 6**

```powershell
git add -- src/athena/research/experiment_documents/projector.py test/unit/research/experiment_documents/test_projector.py
git commit -m "feat(research): rebuild experiment documents from state"
```

---

### Task 7: Inject the projector and migrate Supervisor call order

**Files:**
- Create: `test/support/__init__.py`
- Create: `test/support/experiment_documents.py`
- Create: `test/unit/research/supervisor/test_document_projection.py`
- Modify: `src/athena/research/supervisor/deps.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/supervisor/supervisor.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `src/athena/research/supervisor/settlement.py`
- Delete: `test/unit/research/test_exp_docs.py`
- Modify: `test/unit/research/supervisor/test_supervisor.py`
- Modify: `test/unit/research/supervisor/test_phase_suspend.py`
- Modify: `test/unit/research/supervisor/test_ideator_wiring.py`
- Modify: `test/unit/research/supervisor/test_plan_turn_streaming.py`
- Modify: `test/unit/research/test_data_contract_reaches_candidates.py`
- Modify: `test/unit/research/test_plan_hypothesis_prompt.py`
- Modify: `test/integration/research/test_search_recovery.py`
- Modify: `test/integration/research/test_rolling_search.py`
- Delete: `src/athena/research/exp_docs.py`

**Interfaces:**
- Consumes: `DocumentProjector` and `ProjectionOutcome` from Task 5.
- Produces: required `SupervisorRuntime.documents`, canonical-save-first stage calls,
  lifecycle/restart rebuild, deterministic phase-failure identity,
  skipped-validation projection, and one sanitized output event on projection
  failure.

- [ ] **Step 1: Add shared recording/no-op projector doubles**

```python
@dataclass
class RecordingDocumentProjector:
    outcome: ProjectionOutcome = field(
        default_factory=lambda: ProjectionOutcome.success("a" * 64)
    )
    stage_calls: list[dict[str, object]] = field(default_factory=list)
    rebuild_calls: list[dict[str, object]] = field(default_factory=list)

    def project_stage(self, event, **context) -> ProjectionOutcome:
        self.stage_calls.append({"event": dict(event), **context})
        return self.outcome

    def rebuild(self, **context) -> ProjectionOutcome:
        self.rebuild_calls.append(context)
        return self.outcome
```

Find every manually constructed runtime on the selected base:

```powershell
rg -l "SupervisorRuntime\(" src test tests
```

Add `documents=RecordingDocumentProjector()` to every test construction returned by
that search (the files listed above are the known minimum). Use keyword arguments
even where the constructor is positional. Run the affected files and expect them to
fail until the production dataclass accepts the new field.

- [ ] **Step 2: Write canonical ordering and warning tests**

In `test_document_projection.py`, use a projector double whose `project_stage` reads
the saved canonical files and shared objects and asserts that they already contain
the expected baseline, settled SEARCH Plan removal/SOTA, completed validation, or
FAILED phase state. Projection observes successfully saved tree/state bytes, never a
pre-save in-memory candidate.

Add tests that return `ProjectionOutcome.stale()` and assert:

- baseline remains in the tree and state has `evaluator_ref`;
- SEARCH Plan stays removed, hypothesis status and SOTA stay settled;
- VALIDATE remains `phase=status=COMPLETED`;
- a real phase failure remains FAILED and its original exception is still the error;
- exactly one existing `output` event has `channel="error"` and the exact fixed
  warning; no internal exception/path is present;
- a publish callback that raises while sending the warning is logged and does not
  alter or replace the research result.

Add skipped-validation assertions: canonical skipped-validation fields and COMPLETED
state are saved before final projection. A stale projection outcome or raising
warning publisher leaves that successful finalization intact and exposes no internal
path or exception text.

For phase failure, assert two different exceptions generate two IDs matching
`baseline-phase-failure-[0-9a-f]{12}` and repeating one exception reproduces its ID.

- [ ] **Step 3: Write lifecycle rebuild tests**

Assert a PREPARE lifecycle calls `rebuild` before the prepare adapter and a resumed
SEARCH lifecycle calls Plan recovery before `rebuild` before new search work. A stale
rebuild outcome emits the same sanitized warning and leaves phase/status unchanged.

- [ ] **Step 4: Run the new Supervisor tests and observe missing injection behavior**

Run:

```powershell
uv run pytest test/unit/research/supervisor/test_document_projection.py -q
```

Expected: collection or construction fails because `SupervisorRuntime.documents` and
the migrated calls do not exist.

- [ ] **Step 5: Add the required dependency and production composition**

In `deps.py`, import `DocumentProjector` under `TYPE_CHECKING` and add this required
field to the current frozen `SupervisorRuntime` without removing existing fields:

```python
documents: "DocumentProjector"
```

In `wire_workflow`, construct exactly one projector and inject that instance into
`SupervisorRuntime`:

```python
documents = ExperimentDocumentProjector(
    document_root=config.paths.root / ".athena" / "exp_docs",
    evaluator_roots=(
        config.paths.workspaces / "evaluator" / "evaluate",
        config.paths.workspaces / "evaluator",
    ),
)
```

Add `documents=documents` to the existing `SupervisorRuntime` constructor. Do not add
an optional/default projector or construct one in Supervisor code.

- [ ] **Step 6: Add the Supervisor warning boundary and recovery rebuild**

Add private Supervisor methods:

```python
async def _publish_document_outcome(self, outcome: ProjectionOutcome) -> None:
    if outcome.ok:
        return
    try:
        await self._deps.phases.publish(
            "output",
            {
                "source": "supervisor",
                "channel": "error",
                "text": outcome.warning_message,
            },
        )
    except Exception:
        logger.warning("failed to publish document projection warning", exc_info=True)


async def _rebuild_documents(self) -> None:
    outcome = self._deps.runtime.documents.rebuild(
        tree=self.tree,
        validation=self.state.validation,
        validation_skipped=self.state.validation_skipped,
        task_understanding=self.state.task_understanding,
        direction=self._deps.search.direction,
    )
    await self._publish_document_outcome(outcome)
```

`Supervisor.recover()` awaits Plan recovery, then `_rebuild_documents()`. For a
PREPARE start, `Supervisor.start()` calls `_rebuild_documents()` before delegating to
`PhaseMachine.start()`. Non-PREPARE start must not call it twice.

- [ ] **Step 7: Migrate PREPARE, VALIDATE, and phase-failure projection**

Remove the old import and `_metric_name`/`_write_reports`. Make record helpers async
and call only `self._deps.runtime.documents.project_stage`, followed by
`await self._owner._publish_document_outcome(outcome)`. Derived document failures
must not enter or replace canonical tree/state save handling; retain current
content-addressed report artifacts.

Order PREPARE exactly:

1. complete evaluator/report/artifact preparation outside the projection boundary;
2. install the baseline tree/SOTA and `state.evaluator_ref`, then successfully save
   both canonical tree and state;
3. build the baseline event from the committed shared tree/state and project it;
4. publish the sanitized projection warning when needed;
5. publish PREPARE completion;
6. perform the existing transition to SEARCH through its durable state save.

Order VALIDATE exactly:

1. create and store the content-addressed shared report artifact outside the lock;
2. install validation, report reference, phase, and status and successfully save the
   canonical state;
3. build the final event from the committed shared state and project it;
4. publish the sanitized projection warning when needed;
5. publish the final result/state using the current event semantics.

In the outer phase exception handler, save the FAILED canonical state first, then
request failure projection from the saved shared state. A canonical save failure does
not project. Generate the run ID with a private helper:

```python
def _phase_failure_run_id(stage: str, error: Exception) -> str:
    error_type = f"{type(error).__module__}.{type(error).__qualname__}"
    normalized = f"{error_type}:{' '.join(str(error).split())}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{stage}-phase-failure-{digest}"
```

The local failure document can retain the normalized original phase error summary;
only a *document projection* failure uses the fixed sanitized warning. Reuse the
existing canonical save entry points instead of adding a second save path.

- [ ] **Step 8: Migrate SEARCH settlement to canonical-first order**

Keep evidence loading, report rendering, Git operations, and Agent work outside the
derived projection boundary. After calculating the settlement input, perform this
order:

1. install the experiment, hypothesis, priority, SOTA, and Plan removal, then
   successfully save the canonical tree and state in the existing lifecycle order;
2. use the now-saved shared state/tree to construct the SEARCH event;
3. call `documents.project_stage`;
4. await sanitized warning publication if needed;
5. run `on_plan_settled` and Agent reap using the current ordering.

Do not restore a Plan or SOTA when steps 3-4 fail, and do not catch canonical save
errors as projection failures. Keep the current direct saves only as the canonical
boundary that the paused transactional backend will later replace.

- [ ] **Step 9: Preserve skipped-validation and current resume behavior**

Move `_finalize_without_validation` to canonical-save-first ordering and project its
explicit `SKIPPED` final event only after the completed state is durable. Recovery
uses the injected `rebuild` path after existing Plan recovery. A projection or
warning-publication failure must not change the successful skipped-validation result,
current resume result, or phase/status.

- [ ] **Step 10: Delete the legacy module and prove all callers migrated**

Delete `src/athena/research/exp_docs.py`. Run:

```powershell
rg -n -e "athena\.research\.exp_docs" -e "\btask_metric_name\b" -e "\bwrite_stage_doc\b" -e "(^|[^A-Za-z0-9_])write_reports\(" src test
```

Delete `test/unit/research/test_exp_docs.py` after its still-relevant expectations
have moved into the focused package tests. Expected: no matches. A nonzero `rg` exit
code with empty output is the expected success condition.

- [ ] **Step 11: Run affected Supervisor, control, and recovery tests**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents test/unit/research/supervisor/test_document_projection.py test/unit/research/supervisor/test_supervisor.py test/unit/research/supervisor/test_phase_suspend.py test/unit/research/supervisor/test_ideator_wiring.py test/unit/research/supervisor/test_plan_turn_streaming.py test/unit/research/test_data_contract_reaches_candidates.py test/unit/research/test_plan_hypothesis_prompt.py test/integration/research/test_search_recovery.py test/integration/research/test_rolling_search.py -q
```

Expected: all tests pass with no pre-save projection, unhandled projection warning,
changed phase result, or constructor error.

- [ ] **Step 12: Commit Task 7**

Stage only the listed production, test-support, and affected test files, then verify
the staged name list before committing:

```powershell
git diff --cached --name-status
git commit -m "refactor(research): inject document projection"
```

Expected commit includes deletion of `exp_docs.py` and no unrelated plan or worktree
file.

---

### Task 8: Prove the external contract and close the active plan

**Files:**
- Modify: `test/integration/research/test_autonomous_research.py`
- Modify if a dedicated smoke assertion is clearer: `test/unit/test_cli.py`
- Create after verification: `codex_docs/2026-09-04-experiment-document-projection-completion-report.md`
- Modify after verification: `codex_docs/CURRENT.md`
- Delete after verification: `docs/superpowers/plans/2026-09-04-experiment-document-projection.md`

**Interfaces:**
- Consumes: the completed package and migrated Supervisor.
- Produces: end-to-end disk-contract evidence, import evidence, full-suite evidence,
  completion report, and a closed `CURRENT.md` pointer.

- [ ] **Step 1: Extend the autonomous research contract test**

Keep existing assertions and add:

```python
latest = json.loads((exp_docs / "latest.json").read_text(encoding="utf-8"))
assert latest["kind"] == "stage"
assert latest["stage"] == "final"
assert latest["run_id"] == "validation-key"
for relative, digest in latest["files"].items():
    content = (exp_docs / relative).read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest

assert (exp_docs / "FINAL_REPORT.md").read_bytes() == build_final_report(
    runtime.tree,
    runtime.state.validation,
    validation_skipped=runtime.state.validation_skipped,
).encode("utf-8")
```

Also assert the baseline archive, final archive, stage aliases, and optimization
report still exist, and final metric remains `pytest.approx(0.70)`.

- [ ] **Step 2: Run the end-to-end contract and import smoke**

Run:

```powershell
uv run pytest test/integration/research/test_autonomous_research.py -q
uv run python -c "import athena.research.supervisor; import athena.cli"
uv run Athena-cli --help
```

Expected: integration test passes, imports exit 0, and CLI help exits 0 without
`ModuleNotFoundError`.

- [ ] **Step 3: Commit the end-to-end test change**

```powershell
git add -- test/integration/research/test_autonomous_research.py test/unit/test_cli.py
git diff --cached --name-status
git commit -m "test(research): verify experiment document projection"
```

Omit `test/unit/test_cli.py` from staging when no source edit was needed.

- [ ] **Step 4: Run the focused verification matrix**

Run:

```powershell
uv run pytest test/unit/research/experiment_documents -q
uv run pytest test/unit/research/supervisor -q
uv run pytest test/integration/research/test_autonomous_research.py test/integration/research/test_search_recovery.py test/integration/research/test_rolling_search.py test/architecture/test_supervisor_surface.py -q
rg -n -e "athena\.research\.exp_docs" -e "\btask_metric_name\b" -e "\bwrite_stage_doc\b" -e "(^|[^A-Za-z0-9_])write_reports\(" src test
rg -n "evaluator_spec\.json" src
git diff --check
```

Expected: every pytest command passes; both `rg` commands print nothing and exit 1;
`git diff --check` prints nothing and exits 0.

- [ ] **Step 5: Run the full default suite**

Run:

```powershell
uv run pytest -q
```

Expected: all default tests pass. If an optional environment integration fails solely
because PowerShell 7 (`pwsh`) or another declared external executable is absent,
capture the exact test/command/error separately, run every unaffected test, and do not
describe the environment failure as a product pass or product regression.

- [ ] **Step 6: Re-check every acceptance criterion against fresh evidence**

Before closeout, record evidence for each item:

- old module/imports absent;
- required injected projector in production and tests;
- evaluator priority and no nonexistent spec path;
- strict unsafe/non-finite/extra-field rejection;
- semantic idempotency and conflict preservation;
- canonical tree/state bytes observed before every stage projection, with no new
  competing save path;
- skipped-validation and current recovery behavior remain successful on projection
  or warning-publication failure;
- sanitized warning with state/SOTA/phase isolation;
- atomic temp/replace behavior and old manifest on injected failures;
- concurrent non-interleaving and last-completed manifest;
- rebuild repairs derivable views while retaining unknown history;
- GUI/disk report byte parity;
- CLI/Supervisor import and full-suite result.

Do not close the plan if any product acceptance criterion lacks current passing
evidence.

- [ ] **Step 7: Write the completion report**

Create `codex_docs/2026-09-04-experiment-document-projection-completion-report.md`
with these exact headings and evidence requirements:

```markdown
# Experiment Document Projection Completion Report

Date: 2026-09-04
Design: `docs/superpowers/specs/2026-09-04-experiment-document-projection-design.md`

## Delivered

State the package files added, old module removed, callers migrated, ordering change,
and recovery behavior in concrete past tense.

## Acceptance evidence

| Criterion | Command or test | Result |
|---|---|---|

Include one row for every bullet in Step 6. Use the exact command/test node and the
observed pass count or empty-search exit code in each row.

## Full-suite result

Record `uv run pytest -q`, its exact passed/failed/skipped counts, and the exact error
for any environment-only limitation.

## Repository state

List every Task 1-8 implementation commit ID and state which unrelated worktree paths
were preserved. Add the merge commit (or fast-forward range), merged-tree verification
result, pushed remote ref, removed worktree path, and deleted temporary branch after
those delivery actions complete.
```

The prose instructions above are not copied into the completion report; replace them
with the observed repository facts and command output.

- [ ] **Step 8: Close `CURRENT.md` and remove the completed plan**

Only after Step 6 passes:

1. mark every remaining plan checkbox complete;
2. update `codex_docs/CURRENT.md` so `Active implementation plan` is `None`;
3. add the new completion report first under `Most recent completed work`;
4. add the experiment-document design under `Design spec` if it is not already there;
5. delete this plan file;
6. preserve all parallel and paused plan entries unchanged.

- [ ] **Step 9: Verify and commit closeout documentation**

Run:

```powershell
rg -n -e "T[B]D" -e "T[O]DO" -e "\[[A-Z][^]]+\]" codex_docs/2026-09-04-experiment-document-projection-completion-report.md
git diff --check -- codex_docs/CURRENT.md codex_docs/2026-09-04-experiment-document-projection-completion-report.md docs/superpowers/plans/2026-09-04-experiment-document-projection.md
git status --short
```

Expected: placeholder scan has no output; diff check has no output; status shows only
the intended completion report, `CURRENT.md`, plan deletion, and any clearly unrelated
pre-existing changes.

Commit only the closeout paths:

```powershell
git add -- codex_docs/CURRENT.md codex_docs/2026-09-04-experiment-document-projection-completion-report.md docs/superpowers/plans/2026-09-04-experiment-document-projection.md
git commit -m "docs: close experiment document projection"
```

- [ ] **Step 10: Merge, verify, push, and clean the temporary branch**

Use `finishing-a-development-branch`. Preserve any unrelated dirty files in the main
worktree while integrating. Merge `feat/experiment-document-projection` into `main`,
run the full default suite on the exact merged tree, and stop without pushing or
cleaning if that merged-tree suite fails. When it passes, push `main`, remove
`.worktrees/experiment-document-projection`, prune worktree metadata, and delete the
local feature branch. Never force-push and never delete another worktree or branch.

- [ ] **Step 11: Final repository handoff check**

Run:

```powershell
git status --short --branch
git log -10 --oneline --decorate
```

Report implementation commits, exact test evidence, merge/push/cleanup evidence, any
environment-only limitation, and any unrelated preserved main-worktree files. Do not
claim the whole main worktree is clean unless `git status --short` is empty.
