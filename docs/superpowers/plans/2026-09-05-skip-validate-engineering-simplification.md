# Skip-Validate Engineering Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the completed skip-VALIDATE behavior while collapsing its internal boolean plumbing into one validation-policy owner, one typed gateway factory boundary, one GUI preference owner, and one transactional finalization path.

**Architecture:** The existing GUI wire format and exported `ResearchRuntime` keyword arguments remain compatibility adapters. Runtime composition converts them once into an immutable `ValidationPolicy`; phase code reads only its derived `ValidationMode`. Gateway construction uses one `RuntimeLaunch` protocol, reporting accepts one validated outcome value, and the active transaction coordinator atomically commits skipped-validation terminal state and derived documents.

**Tech Stack:** Python 3.11+, dataclasses, Pydantic research state, asyncio, pytest, Ruff/Black, React 18, TypeScript, Vitest, Tauri/Rust.

## Global Constraints

- Follow `docs/superpowers/specs/2026-09-05-skip-validate-engineering-simplification-design.md`.
- Execute this refinement inside `docs/superpowers/plans/2026-09-04-supervisor-transactional-search-resume-backend.md`; it does not replace that active plan.
- Before Task 1, the transactional backend worktree must contain commit `710406b` and the completed skip-validation lineage ending at `1eae06a`.
- Do not stash, discard, overwrite, or commit unrelated worktree changes.
- Keep GUI RPC keys `auto_validate` and `skip_validate` and their snake-case spelling unchanged.
- Keep the exported `ResearchRuntime(auto_validate=True, skip_validate=False)` keyword shape.
- Keep `validation_skipped` readable from legacy resume metadata; do not add a new core `state.json` field.
- Enabling skip must preserve the stored automatic-validation choice.
- The skip path must not invoke the held-out evaluator or claim an independent final score.
- Do not add another transaction coordinator, report subsystem, or model-facing tool.
- Only the transaction coordinator may commit research state, tree, or derived report documents.
- Every task uses red-green-refactor, updates its own checkboxes, and ends with a focused commit.

## Integration Gate

Run these checks in the transactional backend worktree before implementation:

```powershell
git status --short --branch
git merge-base --is-ancestor 1eae06a HEAD
git merge-base --is-ancestor 710406b HEAD
```

Expected: the worktree is clean and both ancestry commands exit `0`. If the
worktree contains an unowned modification, leave it intact and resolve ownership
before merging or editing. If either ancestry command exits nonzero, merge current
`main` into the transactional branch, resolve by preserving both the active
transaction work and the skip-validation behavior, and rerun the focused suites
named in Tasks 1-4 before continuing.

The active plan's Tasks 1-9 and their acceptance evidence must be complete before
this plan's Task 1. Tasks 1-2 below refine active Task 10. Tasks 3-4 refine active
Task 11. Task 5 is included in active Task 14's final verification and closure.

---

### Task 1: Canonical Validation Policy and Runtime Composition

**Files:**
- Create: `src/athena/research/validation_policy.py`
- Modify: `src/athena/research/config.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `src/athena/research/runtime/services.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/settings.py`
- Modify: `src/athena/research/supervisor/deps.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `src/athena/research/supervisor/search_loop.py`
- Modify: `src/athena/research/supervisor/supervisor.py`
- Create: `test/unit/research/test_validation_policy.py`
- Modify: `test/unit/research/test_runtime_settings.py`
- Modify: `test/unit/research/supervisor/test_supervisor.py`
- Modify: `test/architecture/test_supervisor_surface.py`

**Interfaces:**
- Produces: `ValidationMode(StrEnum)` with `MANUAL`, `AUTOMATIC`, and `SKIP`.
- Produces: immutable `ValidationPolicy(auto_validate: bool = False, skip_validate: bool = False)` and its read-only `mode: ValidationMode` property.
- Produces: `RuntimeOptions.validation_policy: ValidationPolicy` as the only live process policy owner.
- Produces: `PhaseActions.validation_mode: Callable[[], ValidationMode]`; raw validation booleans are removed from `PhaseActions`.
- Consumes: active-plan `SupervisorMutationCoordinator.checkpoint(action, mutate, *, tree=False) -> T` through the existing `runtime_settings` checkpoint.
- Extends: active-plan `MutationCandidate.runtime` with `validation_policy: ValidationPolicy`, copied and committed with other process-local runtime settings.
- Preserves: `ResearchConfig.auto_validate`, `ResearchConfig.skip_validate`, and the public runtime constructor keywords as read-only compatibility projections.

- [ ] **Step 1: Add failing policy and structural tests**

Create `test/unit/research/test_validation_policy.py` with the complete mode matrix:

```python
import pytest

from athena.research.validation_policy import ValidationMode, ValidationPolicy


@pytest.mark.parametrize(
    ("auto_validate", "skip_validate", "expected"),
    [
        (False, False, ValidationMode.MANUAL),
        (True, False, ValidationMode.AUTOMATIC),
        (False, True, ValidationMode.SKIP),
        (True, True, ValidationMode.SKIP),
    ],
)
def test_validation_mode_matrix(auto_validate, skip_validate, expected):
    policy = ValidationPolicy(
        auto_validate=auto_validate,
        skip_validate=skip_validate,
    )

    assert policy.mode is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [("auto_validate", 1), ("skip_validate", "true")],
)
def test_policy_rejects_non_boolean_preferences(field, value):
    values = {"auto_validate": False, "skip_validate": False, field: value}

    with pytest.raises(TypeError, match=rf"{field} must be a bool"):
        ValidationPolicy(**values)
```

Replace the skip-specific composition assertions in
`test/unit/research/test_runtime_settings.py` with assertions for one owner and
the compatibility projections:

```python
def test_research_runtime_composes_one_validation_policy(tmp_path):
    runtime = ResearchRuntime(
        project_root=tmp_path,
        auto_validate=True,
        skip_validate=True,
    )

    policy = runtime.session.options.validation_policy
    assert policy == ValidationPolicy(auto_validate=True, skip_validate=True)
    assert policy.mode is ValidationMode.SKIP
    assert runtime.config.auto_validate is True
    assert runtime.config.skip_validate is True
    assert not hasattr(runtime.supervisor._deps.phases, "auto_validate")
    assert not hasattr(runtime.supervisor._deps.phases, "skip_validate")
    assert runtime.settings()["auto_validate"] is True
    assert runtime.settings()["skip_validate"] is True
```

Add this preservation test:

```python
@pytest.mark.asyncio
async def test_skip_toggle_preserves_automatic_preference(tmp_path):
    runtime = ResearchRuntime(project_root=tmp_path, auto_validate=True)

    await runtime.apply_settings({"skip_validate": True})
    assert runtime.session.options.validation_policy.mode is ValidationMode.SKIP
    assert runtime.settings()["auto_validate"] is True

    await runtime.apply_settings({"skip_validate": False})
    assert runtime.session.options.validation_policy.mode is ValidationMode.AUTOMATIC
    assert runtime.settings()["auto_validate"] is True
```

Extend `test/architecture/test_supervisor_surface.py`:

```python
def test_phase_actions_has_one_validation_mode_reader():
    fields = PhaseActions.__dataclass_fields__

    assert "validation_mode" in fields
    assert "auto_validate" not in fields
    assert "skip_validate" not in fields
```

- [ ] **Step 2: Run the focused tests and capture the red state**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/supervisor/test_supervisor.py test/architecture/test_supervisor_surface.py
```

Expected: collection fails because `athena.research.validation_policy` and
`PhaseActions.validation_mode` do not exist, and current runtime options still
expose both raw booleans.

- [ ] **Step 3: Implement the immutable policy value**

Create `src/athena/research/validation_policy.py`:

```python
"""Canonical runtime policy for entering or skipping final validation."""

from dataclasses import dataclass
from enum import StrEnum


class ValidationMode(StrEnum):
    """Effective workflow behavior derived from persisted GUI preferences."""

    MANUAL = "manual"
    AUTOMATIC = "automatic"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Preserve both preferences while exposing one effective workflow mode."""

    auto_validate: bool = False
    skip_validate: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("auto_validate", self.auto_validate),
            ("skip_validate", self.skip_validate),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")

    @property
    def mode(self) -> ValidationMode:
        if self.skip_validate:
            return ValidationMode.SKIP
        if self.auto_validate:
            return ValidationMode.AUTOMATIC
        return ValidationMode.MANUAL


__all__ = ["ValidationMode", "ValidationPolicy"]
```

- [ ] **Step 4: Normalize once during runtime composition**

In `ResearchConfig`, replace the two stored fields with one initial policy and
retain read-only compatibility properties:

```python
validation_policy: ValidationPolicy = field(default_factory=ValidationPolicy)

@property
def auto_validate(self) -> bool:
    return self.validation_policy.auto_validate

@property
def skip_validate(self) -> bool:
    return self.validation_policy.skip_validate
```

In `ResearchRuntime.__init__`, construct the value at the public boundary and
pass only `validation_policy` to `build_config`:

```python
validation_policy = ValidationPolicy(
    auto_validate=auto_validate,
    skip_validate=skip_validate,
)
```

Change `RuntimeOptions` to:

```python
@dataclass
class RuntimeOptions:
    ideation: Literal["ideageneration", "baseline", "debate"] = "ideageneration"
    direction: Literal["maximize", "minimize"] = "maximize"
    tolerance: float = 0.0
    validation_policy: ValidationPolicy = field(default_factory=ValidationPolicy)
    kaggle: KaggleStack | None = None
```

`build_services` assigns `config.validation_policy` to that field. Define a
module-private default reader in `supervisor/deps.py`, replace both phase booleans
with one callback, and freeze `PhaseActions` now that it contains only callables:

```python
ValidationModeReader = Callable[[], ValidationMode]


def _manual_validation_mode() -> ValidationMode:
    return ValidationMode.MANUAL


@dataclass(frozen=True)
class PhaseActions:
    publish: Publish
    prepare: PreparePhase | None = None
    validation: ValidationPhase | None = None
    publish_agent_event: PublishAgentEvent | None = None
    on_plan_settled: Callable[[str], Awaitable[None]] | None = None
    prepare_resume_is_attested: _PrepareResumeIsAttested | None = None
    validation_mode: ValidationModeReader = _manual_validation_mode
```

Wire it once in `bootstrap.wire_workflow`:

```python
validation_mode=lambda: runtime.session.options.validation_policy.mode,
```

Update phase and search scheduling branches to compare the returned
`ValidationMode`; remove validation fields and mutation branches from
`Supervisor.configure_options`.

- [ ] **Step 5: Apply validation settings through the single owner**

In `SettingsController`, validate both optional patch members before constructing
the replacement value:

```python
def _next_validation_policy(
    current: ValidationPolicy,
    patch: dict[str, Any],
) -> ValidationPolicy:
    values = {
        "auto_validate": current.auto_validate,
        "skip_validate": current.skip_validate,
    }
    for name in values:
        if name not in patch:
            continue
        value = patch[name]
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a bool")
        values[name] = value
    return ValidationPolicy(**values)
```

Project snapshots from `rt.session.options.validation_policy`. Inside the active
plan's `runtime_settings` checkpoint, replace only
`candidate.runtime.validation_policy`; after coordinator commit, the canonical
`RuntimeOptions.validation_policy` reflects the committed candidate. Delete both
calls that previously synchronized validation fields through
`Supervisor.configure_options`.

- [ ] **Step 6: Run policy, settings, Supervisor, and architecture tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_human_plan_boundary.py test/architecture/test_supervisor_surface.py
.venv\Scripts\ruff.exe check src/athena/research/validation_policy.py src/athena/research/config.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/settings.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py src/athena/research/supervisor/search_loop.py src/athena/research/supervisor/supervisor.py test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/supervisor/test_supervisor.py test/architecture/test_supervisor_surface.py
```

Expected: all tests pass; Ruff reports no findings in changed files; the public
constructor and settings snapshot still expose the two compatibility keys while
phase dependencies do not.

- [ ] **Step 7: Commit the policy boundary**

```powershell
git add src/athena/research/validation_policy.py src/athena/research/config.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/settings.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py src/athena/research/supervisor/search_loop.py src/athena/research/supervisor/supervisor.py test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/supervisor/test_supervisor.py test/architecture/test_supervisor_surface.py
git commit -m "refactor: centralize validation policy"
```

---

### Task 2: Typed Gateway Runtime Launch and GUI Preference Ownership

**Files:**
- Create: `src/gui_gateway/runtime_factory.py`
- Modify: `src/gui_gateway/__main__.py`
- Modify: `src/gui_gateway/handler.py`
- Modify: `src/gui_gateway/state_store.py`
- Modify: `tests/test_gui_gateway_main.py`
- Modify: `tests/test_gui_gateway_handler.py`
- Modify: `tests/test_gui_gateway_transport.py`
- Modify: `test/unit/gui/test_state_store.py`
- Modify: `test/architecture/test_supervisor_surface.py`

**Interfaces:**
- Produces: immutable `RuntimeLaunch(project_root: Path, state_root: Path | None, validation: ValidationPolicy)`.
- Produces: `RuntimeLaunch.session_id: str`, derived as `state_root.name` or `"default"`.
- Produces: `RuntimeFactory(Protocol).__call__(launch: RuntimeLaunch) -> ResearchRuntime`.
- Produces: `GuiStateStore.set_active_project(project_root)`, `remember_session(project_root, session_id)`, and `remember_skip_validate(project_root, enabled)`; each returns the committed `GuiState`.
- Removes: handler-level signature inspection, variadic runtime-factory typing, and full `GuiState` reconstruction.

- [ ] **Step 1: Add failing one-shape factory and state-owner tests**

Add to `test/unit/gui/test_state_store.py`:

```python
def test_named_updates_preserve_unrelated_gui_state(tmp_path):
    project_a = (tmp_path / "a").resolve()
    project_b = (tmp_path / "b").resolve()
    project_a.mkdir()
    project_b.mkdir()
    store = GuiStateStore(tmp_path / "gui-state.json")
    store.save(
        GuiState(
            active_project_root=str(project_a),
            last_sessions={str(project_a): "session-a"},
            skip_validate_by_project={str(project_b): True},
        )
    )

    store.remember_session(project_b, "session-b")
    store.remember_skip_validate(project_a, True)
    committed = store.set_active_project(project_b)

    assert committed == GuiState(
        active_project_root=str(project_b),
        last_sessions={
            str(project_a): "session-a",
            str(project_b): "session-b",
        },
        skip_validate_by_project={
            str(project_a): True,
            str(project_b): True,
        },
    )
```

Add to `tests/test_gui_gateway_handler.py`:

```python
@pytest.mark.asyncio
async def test_runtime_swap_uses_one_typed_launch(tmp_path):
    launches = []

    def factory(launch):
        launches.append(launch)
        return SettingsRuntime(str(launch.project_root), launch.validation.skip_validate)

    store = GuiStateStore(tmp_path / "gui-state.json")
    store.remember_skip_validate(tmp_path, True)
    handler = GuiRequestHandler(SettingsRuntime(str(tmp_path)), factory, state_store=store)

    await handler.session_switch("session-1")

    assert len(launches) == 1
    assert launches[0].project_root == tmp_path.resolve()
    assert launches[0].session_id == "session-1"
    assert launches[0].validation == ValidationPolicy(True, True)
```

Add an architecture assertion:

```python
def test_gateway_runtime_factory_has_no_signature_reflection():
    source = Path("src/gui_gateway/handler.py").read_text(encoding="utf-8")

    assert "import inspect" not in source
    assert "inspect.signature" not in source
    assert "RuntimeFactory = Callable" not in source
```

- [ ] **Step 2: Run gateway tests and verify the old dual call paths fail**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/gui/test_state_store.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/architecture/test_supervisor_surface.py
```

Expected: new imports/methods fail, the handler still introspects factory
signatures, and the old test factories do not receive `RuntimeLaunch`.

- [ ] **Step 3: Implement the typed launch boundary**

Create `src/gui_gateway/runtime_factory.py`:

```python
"""Typed construction boundary for GUI-owned research runtimes."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from athena.research import ResearchRuntime
from athena.research.validation_policy import ValidationPolicy


@dataclass(frozen=True, slots=True)
class RuntimeLaunch:
    project_root: Path
    state_root: Path | None
    validation: ValidationPolicy

    @property
    def session_id(self) -> str:
        return self.state_root.name if self.state_root is not None else "default"


class RuntimeFactory(Protocol):
    def __call__(self, launch: RuntimeLaunch) -> ResearchRuntime:
        raise NotImplementedError


__all__ = ["RuntimeFactory", "RuntimeLaunch"]
```

Replace `_build_runtime` with one direct call. The handler constructs
`ValidationPolicy(auto_validate=True, skip_validate=stored.skip_validate_for(root))`
because the GUI's existing default is automatic validation. Use resolved `Path`
values in every launch.

In `gui_gateway.__main__`, replace the broad factory protocol with a private
`_GatewayRuntimeFactory` that stores the `HumanRequestBroker` and whose
`__call__(launch)` builds and binds `ResearchRuntime` with:

```python
runtime = ResearchRuntime(
    project_root=launch.project_root,
    state_root=launch.state_root,
    session_id=launch.session_id,
    model=settings.model_name(),
    auto_validate=launch.validation.auto_validate,
    skip_validate=launch.validation.skip_validate,
    task_confirmation_gate=True,
    auto_confirm=False,
    ask_user=self._broker.ask,
    broker=self._broker,
)
self._broker.bind(launch.session_id, "runtime", "runtime")
return runtime
```

Change `start_server` to accept `make_runtime: RuntimeFactory | None = None`, use
`_GatewayRuntimeFactory(broker)` when it is absent, and build the optional initial
runtime through that same object. Initial construction and every project/session
swap use the same `RuntimeLaunch` path.

- [ ] **Step 4: Move GUI mutations into the store**

Change `GuiStateStore._lock` to `threading.RLock` and add one private update
primitive:

```python
def _update(self, transform: Callable[[GuiState], GuiState]) -> GuiState:
    with self._lock:
        committed = transform(self.load())
        self.save(committed)
        return committed
```

Implement the three named operations with `dataclasses.replace`; normalize every
project key with `Path(project_root).expanduser().resolve()`. Validate `enabled`
with `isinstance(enabled, bool)` and raise `TypeError("enabled must be a bool")`
before touching disk. Replace handler `_remember_session`,
`_remember_skip_validate`, and full `GuiState` construction with these store
operations.

- [ ] **Step 5: Run gateway persistence, restart, and architecture tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/gui/test_state_store.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py test/architecture/test_supervisor_surface.py
.venv\Scripts\ruff.exe check src/gui_gateway/runtime_factory.py src/gui_gateway/__main__.py src/gui_gateway/handler.py src/gui_gateway/state_store.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/gui/test_state_store.py test/architecture/test_supervisor_surface.py
```

Expected: all pass; a project preference survives session switches and gateway
restart; no inspected or fallback factory path remains; Ruff reports no findings.

- [ ] **Step 6: Commit gateway ownership**

```powershell
git add src/gui_gateway/runtime_factory.py src/gui_gateway/__main__.py src/gui_gateway/handler.py src/gui_gateway/state_store.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/gui/test_state_store.py test/architecture/test_supervisor_surface.py
git commit -m "refactor: type GUI runtime construction"
```

---

### Task 3: One Validated Report Input

**Files:**
- Modify: `src/athena/research/report.py`
- Modify: `src/athena/research/exp_docs.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `test/unit/research/test_report.py`
- Modify: `test/unit/research/test_exp_docs.py`
- Modify: `test/unit/research/supervisor/test_supervisor.py`

**Interfaces:**
- Produces: `ValidationReportStatus(StrEnum)` with `ABSENT`, `SKIPPED`, and `VALIDATED`.
- Produces: immutable `ValidationReportInput(status, result=None)` with class constructors `absent()`, `skipped()`, and `validated(result)`.
- Changes: `build_final_report(tree, validation: ValidationReportInput | None = None) -> str`.
- Changes: `build_optimization_report(tree, validation: ValidationReportInput | None = None, *, metric_name, direction) -> str`.
- Changes: `write_reports(root, tree, validation: ValidationReportInput | None = None, *, metric_name, direction) -> tuple[Path, Path]`.
- Removes: every internal `validation_skipped` keyword parameter.

- [ ] **Step 1: Write failing report-state tests**

Add to `test/unit/research/test_report.py`:

```python
def test_validation_report_input_rejects_impossible_states():
    with pytest.raises(ValueError, match="skipped validation cannot have a result"):
        ValidationReportInput(ValidationReportStatus.SKIPPED, {"final_test_score": 1.0})
    with pytest.raises(ValueError, match="validated report requires a result"):
        ValidationReportInput(ValidationReportStatus.VALIDATED)


def test_validated_report_input_copies_the_result():
    result = {"final_test_score": 0.91}
    report_input = ValidationReportInput.validated(result)

    result["final_test_score"] = 0.10
    assert report_input.result == {"final_test_score": 0.91}


def test_skipped_report_uses_one_outcome_value():
    report = build_final_report(_tree_with_sota(), ValidationReportInput.skipped())

    assert VALIDATION_SKIPPED_NOTICE in report
    assert "final_test_score" not in report


def test_validated_report_uses_one_outcome_value():
    report = build_final_report(
        _tree_with_sota(),
        ValidationReportInput.validated({"final_test_score": 0.91}),
    )

    assert "0.9100" in report
    assert VALIDATION_SKIPPED_NOTICE not in report
```

Update exp-doc tests to call `ValidationReportInput.skipped()` and
`ValidationReportInput.validated(result)`; remove calls that pass both a mapping and
`validation_skipped`.

- [ ] **Step 2: Run report tests and verify the parallel-parameter API fails**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/supervisor/test_supervisor.py
```

Expected: imports or constructors fail because the validated report input does
not exist and current builders still accept parallel state.

- [ ] **Step 3: Implement the report input invariant once**

Add to `report.py`:

```python
class ValidationReportStatus(StrEnum):
    ABSENT = "absent"
    SKIPPED = "skipped"
    VALIDATED = "validated"


@dataclass(frozen=True, slots=True)
class ValidationReportInput:
    status: ValidationReportStatus
    result: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status is ValidationReportStatus.SKIPPED and self.result is not None:
            raise ValueError("skipped validation cannot have a result")
        if self.status is ValidationReportStatus.ABSENT and self.result is not None:
            raise ValueError("absent validation cannot have a result")
        if self.status is ValidationReportStatus.VALIDATED and self.result is None:
            raise ValueError("validated report requires a result")
        if self.result is not None:
            object.__setattr__(self, "result", MappingProxyType(dict(self.result)))

    @classmethod
    def absent(cls) -> "ValidationReportInput":
        return cls(ValidationReportStatus.ABSENT)

    @classmethod
    def skipped(cls) -> "ValidationReportInput":
        return cls(ValidationReportStatus.SKIPPED)

    @classmethod
    def validated(cls, result: Mapping[str, Any]) -> "ValidationReportInput":
        return cls(ValidationReportStatus.VALIDATED, result)
```

Use `None` as each public function's default and normalize it to
`ValidationReportInput.absent()` inside the function; do not place a constructed
object in a function default. Render only by `status`, then read `result` after
the validated-status check. Import `MappingProxyType` from `types`, `dataclass`
from `dataclasses`, and `StrEnum` from `enum`; add both report-input types to
`report.__all__` because `exp_docs` is their supported cross-module consumer.

- [ ] **Step 4: Migrate all report callers**

Change `exp_docs` builders and writers to accept the value object and pass it
unchanged. In phase code, convert durable run state in one helper:

```python
def _validation_report_input(self) -> ValidationReportInput:
    if self._state.validation_skipped:
        return ValidationReportInput.skipped()
    if self._state.validation is not None:
        return ValidationReportInput.validated(self._state.validation)
    return ValidationReportInput.absent()
```

The skip path explicitly passes `ValidationReportInput.skipped()`. The normal
validation path passes `ValidationReportInput.validated(validation)`. Delete all
`validation_skipped=` forwarding and its repeated contradictory-state checks.

- [ ] **Step 5: Run report, exp-doc, resume, and formatting checks**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_autonomous_research.py
.venv\Scripts\ruff.exe check src/athena/research/report.py src/athena/research/exp_docs.py src/athena/research/supervisor/phases.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/supervisor/test_supervisor.py
```

Expected: all pass; one constructor enforces report invariants; Ruff reports no
findings.

- [ ] **Step 6: Commit the report boundary**

```powershell
git add src/athena/research/report.py src/athena/research/exp_docs.py src/athena/research/supervisor/phases.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/supervisor/test_supervisor.py
git commit -m "refactor: model validation report state once"
```

---

### Task 4: Transactional Skipped-Validation Finalization

**Files:**
- Modify: `src/athena/research/control/coordinator.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `src/athena/research/exp_docs.py`
- Modify: `test/unit/research/control/test_coordinator.py`
- Modify: `test/unit/research/supervisor/test_supervisor.py`
- Modify: `test/integration/research/test_autonomous_research.py`
- Modify: `test/integration/research/test_validate_agent_contract.py`
- Modify: `test/architecture/test_supervisor_surface.py`

**Interfaces:**
- Extends: `SupervisorMutationCoordinator.checkpoint(action, mutate, *, tree=False, documents=None, expected_context_version=None) -> T`.
- Produces: named internal mutation `skip_validation_finalize`.
- Consumes: active-plan `ControlRejected(code, message)` for typed checkpoint failures; this plan adds no second checkpoint exception type.
- Consumes: `ValidationReportInput.skipped()` from Task 3.
- Produces: `render_report_documents(root, tree, validation, *, metric_name, direction) -> dict[Path, bytes]`.
- Produces: `render_stage_documents(root, document) -> dict[Path, bytes]`.
- Preserves: post-commit event publication and durable `validation_skipped=True`.
- Removes: phase-local state snapshots, direct `save_state`, and direct report writes from `_finalize_without_validation`.

- [ ] **Step 1: Add failing atomicity and no-evaluator tests**

Extend coordinator tests with prepared document bytes and an injected install
failure:

```python
@pytest.mark.asyncio
async def test_checkpoint_documents_roll_back_with_state(coordinator, tmp_path):
    final_report = tmp_path / "FINAL_REPORT.md"
    final_report.write_text("old\n", encoding="utf-8")
    before = coordinator.snapshot()
    coordinator.fail_install_at(final_report)

    with pytest.raises(ControlRejected) as caught:
        await coordinator.checkpoint(
            "skip_validation_finalize",
            lambda candidate: setattr(candidate.state, "status", "COMPLETED"),
            documents={final_report: b"new\n"},
            expected_context_version=coordinator.context_version,
        )

    assert caught.value.code == "persistence_failed"
    assert coordinator.snapshot() == before
    assert final_report.read_bytes() == b"old\n"
```

Add integration assertions around the existing skip run:

```python
assert runtime.state.phase == "COMPLETED"
assert runtime.state.status == "COMPLETED"
assert runtime.state.validation_skipped is True
assert validation_calls == 0
assert "VALIDATE" not in heldout_agent_calls
assert VALIDATION_SKIPPED_NOTICE in final_report.read_text(encoding="utf-8")
```

Add failure injection between report-document install and state commit; assert
the old state, old report bytes, tree identity, and SOTA remain exact. Add a
publication callback that raises after commit; assert terminal state and report
bytes remain committed and a warning is logged.

- [ ] **Step 2: Run finalization tests and capture direct-write failures**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_coordinator.py test/unit/research/supervisor/test_supervisor.py test/integration/research/test_autonomous_research.py test/integration/research/test_validate_agent_contract.py test/architecture/test_supervisor_surface.py
```

Expected: new coordinator arguments are rejected and phase finalization still
writes reports before manually mutating/saving state.

- [ ] **Step 3: Extend internal checkpoints with prepared documents**

Update `checkpoint` to forward an immutable copy of `documents` into the same
journal/install path used by model mutations. When
`expected_context_version` is not `None`, compare it under the coordinator lock
before building the candidate; raise
`ControlRejected("stale_context", "research context changed")`
without creating a journal or touching documents. Preserve the existing behavior
when both new optional arguments are omitted.

The implementation path is:

```python
async with self._lock:
    self._owner.assert_held()
    if (
        expected_context_version is not None
        and expected_context_version != self._context.version()
    ):
        raise ControlRejected("stale_context", "research context changed")
    candidate = self._snapshot_shared().to_candidate()
    result = mutate(candidate)
    effect = MutationEffect(result=result, documents=dict(documents or {}))
    return await self._commit_checkpoint(action, candidate, effect, tree=tree)
```

Do not await report rendering or event publication while holding the coordinator
lock.

- [ ] **Step 4: Separate report rendering from document installation**

In `exp_docs.py`, make document preparation pure and keep the current direct-write
functions as thin compatibility adapters for callers not yet migrated:

```python
def render_report_documents(
    root: str | Path,
    tree: ResearchTree,
    validation: ValidationReportInput | None = None,
    *,
    metric_name: str = "primary",
    direction: str = "maximize",
) -> dict[Path, bytes]:
    outcome = validation or ValidationReportInput.absent()
    directory = Path(root) / ".athena" / "exp_docs"
    return {
        directory / "FINAL_REPORT.md": build_final_report(tree, outcome).encode("utf-8"),
        directory / "OPTIMIZATION.md": build_optimization_report(
            tree,
            outcome,
            metric_name=metric_name,
            direction=direction,
        ).encode("utf-8"),
    }


def render_stage_documents(
    root: str | Path,
    document: Mapping[str, object],
) -> dict[Path, bytes]:
    payload = _document(document)
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    directory = Path(root) / ".athena" / "exp_docs"
    return {
        directory / "runs" / f"{payload['run_id']}.json": content,
        directory / f"{payload['stage']}.json": content,
        directory / "latest.json": content,
    }
```

`write_reports` and `write_stage_doc` iterate over these mappings and call the
existing atomic writer. `write_reports` returns the Final and Optimization paths
in that order; `write_stage_doc` returns the `runs/<run_id>.json` path, preserving
their current contracts. Add unit tests that compare each adapter's installed
bytes with its pure renderer's bytes. The skipped transaction path consumes the
pure renderers and never calls the adapters.

- [ ] **Step 5: Replace manual skip finalization with one checkpoint**

In `_finalize_without_validation`, first capture the coordinator context version,
SOTA id, trusted score, and rendered report/stage-document bytes outside the lock.
Then submit `skip_validation_finalize`; its mutator rechecks:

```python
def mutate(candidate: MutationCandidate) -> None:
    if candidate.state.phase != "SEARCH" or candidate.state.status != "RUNNING":
        raise ControlRejected("invalid_transition", "skip requires running SEARCH")
    if candidate.state.validation is not None:
        raise ControlRejected("invalid_transition", "validation already exists")
    if tuple(self._run.running_ids()):
        raise ControlRejected("plans_active", "SEARCH plans are still running")
    if candidate.tree.best_experiment_id() != expected_sota_id:
        raise ControlRejected("stale_context", "SEARCH SOTA changed")
    sota = candidate.tree.get_experiment(expected_sota_id)
    if sota.eval is None or sota.eval.primary != expected_sota_score:
        raise ControlRejected("stale_context", "SEARCH SOTA score changed")
    candidate.state.phase = "COMPLETED"
    candidate.state.status = "COMPLETED"
    candidate.state.validation_skipped = True
```

Pass the prepared Final report, optimization report, final-skipped stage document,
and latest document bytes through `documents`. Delete the phase-local snapshot,
direct `save_state`, `_record_skipped_final` write, and `_write_reports` write from
this path. Publish `SKIPPED_VALIDATION_OUTPUT` and state only after a successful
checkpoint; retain the existing warning-only behavior for subscriber failures.

- [ ] **Step 6: Add the structural single-writer guard**

Extend `test/architecture/test_supervisor_surface.py` to parse
`supervisor/phases.py` and assert `_finalize_without_validation` contains neither
`save_state` nor `write_reports` calls. Assert the stable action name
`skip_validation_finalize` is present exactly once in production code outside the
coordinator/journal implementation.

- [ ] **Step 7: Run atomicity, scientific-validity, and regression suites**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_coordinator.py test/unit/research/supervisor/test_supervisor.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_autonomous_research.py test/integration/research/test_validate_agent_contract.py test/integration/research/test_search_recovery.py test/architecture/test_supervisor_surface.py
.venv\Scripts\ruff.exe check src/athena/research/control/coordinator.py src/athena/research/supervisor/phases.py src/athena/research/exp_docs.py test/unit/research/control/test_coordinator.py test/unit/research/supervisor/test_supervisor.py test/integration/research/test_autonomous_research.py test/integration/research/test_validate_agent_contract.py test/architecture/test_supervisor_surface.py
```

Expected: all pass; every injected failure preserves exact old bytes/state; no
held-out evaluator call occurs; changed files are Ruff-clean.

- [ ] **Step 8: Commit transactional finalization**

```powershell
git add src/athena/research/control/coordinator.py src/athena/research/supervisor/phases.py src/athena/research/exp_docs.py test/unit/research/control/test_coordinator.py test/unit/research/supervisor/test_supervisor.py test/integration/research/test_autonomous_research.py test/integration/research/test_validate_agent_contract.py test/architecture/test_supervisor_surface.py
git commit -m "refactor: transact skipped validation finalization"
```

---

### Task 5: Full Verification, Documentation, and Plan Closure

**Files:**
- Modify: `docs/superpowers/plans/2026-09-04-supervisor-transactional-search-resume-backend.md`
- Modify: `docs/superpowers/plans/2026-09-04-experiment-document-projection.md`
- Modify: `docs/superpowers/specs/2026-09-05-skip-validate-engineering-simplification-design.md`
- Create: `codex_docs/2026-09-05-skip-validate-engineering-simplification-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete after all gates pass: `docs/superpowers/plans/2026-09-05-skip-validate-engineering-simplification.md`

**Interfaces:**
- Records: exact commits, commands, pass counts, reviewed warnings, and retained compatibility surface.
- Updates: queued experiment-document projection to consume `ValidationReportInput` instead of recreating parallel skip flags.
- Closes: this supplemental plan only; the transactional backend plan remains active until its own acceptance criteria pass.

- [ ] **Step 1: Run diff, API-surface, and formatting gates**

Run:

```powershell
git diff --check
.venv\Scripts\python.exe -m black --check src/athena/research/validation_policy.py src/athena/research/config.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/settings.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py src/athena/research/supervisor/search_loop.py src/athena/research/supervisor/supervisor.py src/athena/research/control/coordinator.py src/athena/research/report.py src/athena/research/exp_docs.py src/gui_gateway/runtime_factory.py src/gui_gateway/__main__.py src/gui_gateway/handler.py src/gui_gateway/state_store.py test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/test_breakpoint_resume.py test/unit/research/supervisor/test_supervisor.py test/unit/research/control/test_coordinator.py test/unit/gui/test_state_store.py test/integration/research/test_autonomous_research.py test/integration/research/test_human_plan_boundary.py test/integration/research/test_validate_agent_contract.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py test/architecture/test_supervisor_surface.py
.venv\Scripts\ruff.exe check src/athena/research/validation_policy.py src/athena/research/config.py src/athena/research/runtime/facade.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/settings.py src/athena/research/supervisor/deps.py src/athena/research/supervisor/phases.py src/athena/research/supervisor/search_loop.py src/athena/research/supervisor/supervisor.py src/athena/research/control/coordinator.py src/athena/research/report.py src/athena/research/exp_docs.py src/gui_gateway/runtime_factory.py src/gui_gateway/__main__.py src/gui_gateway/handler.py src/gui_gateway/state_store.py test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/test_breakpoint_resume.py test/unit/research/supervisor/test_supervisor.py test/unit/research/control/test_coordinator.py test/unit/gui/test_state_store.py test/integration/research/test_autonomous_research.py test/integration/research/test_human_plan_boundary.py test/integration/research/test_validate_agent_contract.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py test/architecture/test_supervisor_surface.py
rg -n "inspect\.signature|Callable\[\.\.\., ResearchRuntime\]|phases\.(auto_validate|skip_validate)|validation_skipped=" src test tests
```

Expected: diff and format/lint gates pass. The search returns no gateway factory
reflection, no phase boolean fields, and no report-call keyword; remaining
`validation_skipped` occurrences are limited to durable state migration,
scientific disclosure, and tests for those boundaries.

- [ ] **Step 2: Run focused Python acceptance**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/test_validation_policy.py test/unit/research/test_runtime_settings.py test/unit/research/test_report.py test/unit/research/test_exp_docs.py test/unit/research/test_breakpoint_resume.py test/unit/research/supervisor/test_state.py test/unit/research/supervisor/test_supervisor.py test/unit/research/control/test_coordinator.py test/unit/gui/test_state_store.py test/integration/research/test_autonomous_research.py test/integration/research/test_human_plan_boundary.py test/integration/research/test_validate_agent_contract.py test/integration/research/test_search_recovery.py tests/test_gui_gateway_main.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_e2e.py test/architecture/test_supervisor_surface.py
```

Expected: all pass with a recorded exact test count.

- [ ] **Step 3: Run the complete Python suite**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

Expected: all tests pass. Investigate every failure against `main` and the active
transactional plan; do not classify failures as unrelated without reproducing
them on the exact predecessor commit.

- [ ] **Step 4: Run frontend and Rust gates**

Run:

```powershell
Push-Location athena-gui
npm test -- --run src/components/__tests__/SettingsPanel.test.tsx
npm test
npm run build
Push-Location src-tauri
cargo test
cargo check
Pop-Location
Pop-Location
```

Expected: focused and full Vitest suites pass, TypeScript/Vite build succeeds,
and Rust test/check gates succeed. The SettingsPanel still disables automatic
validation while preserving its checked value.

- [ ] **Step 5: Review the queued projection contract**

Update `docs/superpowers/plans/2026-09-04-experiment-document-projection.md` so
its predecessor audit explicitly imports and reuses `ValidationReportInput`,
preserves the skipped disclosure, and removes obsolete adapters only after all
callers have migrated. Add an exact test command covering skipped, absent, and
validated projections.

- [ ] **Step 6: Write the completion report from fresh evidence**

Create
`codex_docs/2026-09-05-skip-validate-engineering-simplification-completion-report.md`
with these headings and concrete evidence:

```markdown
# Skip-Validate Engineering Simplification Completion Report

## Delivered Architecture
## Compatibility Preserved
## Removed Interfaces and Duplication
## Transaction and Scientific Invariants
## Verification Evidence
## Remaining Warnings
## Commits
```

List exact command lines, exit codes, pass counts, changed-file Ruff/Black results,
frontend build output, Rust results, and any accepted warning with owner and reason.
Do not copy historical counts from the prior feature completion report.

- [ ] **Step 7: Close this supplemental plan**

Check every completed box in this plan and the corresponding refinement notes in
active Tasks 10, 11, and 14. Verify every criterion against current files and fresh
outputs. Then delete this plan, remove its active-refinement pointer from
`codex_docs/CURRENT.md`, add the completion report under most recent completed
work, and keep the transactional backend plan as the active plan if it still has
unfinished criteria.

Commit only closure documentation:

```powershell
git add docs/superpowers/plans/2026-09-04-supervisor-transactional-search-resume-backend.md docs/superpowers/plans/2026-09-04-experiment-document-projection.md docs/superpowers/specs/2026-09-05-skip-validate-engineering-simplification-design.md codex_docs/2026-09-05-skip-validate-engineering-simplification-completion-report.md codex_docs/CURRENT.md
git add -u -- docs/superpowers/plans/2026-09-05-skip-validate-engineering-simplification.md
git commit -m "docs: close skip validate simplification"
```
