# Skip VALIDATE and Finalize After SEARCH Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-persistent GUI option that skips the independent VALIDATE evaluator, writes an explicitly unvalidated final report from the SEARCH SOTA, and completes the run directly after SEARCH.

**Architecture:** A new `skip_validate` boolean is threaded through the existing runtime configuration and settings layers without changing `auto_validate`. The GUI gateway persists the preference per resolved project root, while the phase machine gives skip mode highest priority after SEARCH and executes one idempotent finalizer that records the skipped evaluation honestly before committing `COMPLETED`.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, pytest/pytest-asyncio, React 18, TypeScript 5, Vitest/Testing Library, Tauri 2/Rust.

## Global Constraints

- The authoritative behavior is `docs/superpowers/specs/2026-09-04-skip-validate-final-design.md`; do not weaken its persistence, audit, recovery, or report-disclosure contracts.
- Use the exact public setting name `skip_validate` and default it to `false` in every non-GUI entry point.
- Preserve `auto_validate`: with skip disabled, `true` still runs VALIDATE automatically and `false` still parks at the human gate.
- Skip mode must never call the validation adapter, enter the durable VALIDATE phase, populate `state.validation`, or fabricate final-test/generalization metrics.
- Do not add a durable FINAL phase. Successful skip mode moves from SEARCH to `phase="COMPLETED", status="COMPLETED"` only after its final documents exist.
- The GUI preference is project-scoped, shared across that project's sessions, persisted across gateway restarts, and isolated from other projects.
- The actual run records `validation_skipped=true` only in backward-compatible `resume.json` metadata so later report generation is independent of the current GUI preference.
- A settings save does not itself resume or terminate a `SEARCH/WAITING` run. A running SEARCH reads the new policy at natural completion; an entered VALIDATE run continues unchanged.
- Ordinary output/subscriber failure after the terminal state save is best-effort and cannot roll back completion. `asyncio.CancelledError` continues to propagate.
- Preserve unrelated worktree changes. The dirty main worktree is out of bounds until the explicit integration task, and every commit must stage only paths named by its task.
- Do not use networked model providers in tests. Update this plan's checkboxes only after fresh RED/GREEN or verification evidence exists.
- The shared root virtual environment is editable-installed against the main
  checkout. In every PowerShell process that runs Python from this linked
  worktree, first set `$env:PYTHONPATH = (Resolve-Path 'src').Path`; otherwise
  pytest silently mixes this worktree's tests with main's production modules.

## File Map

- Modify `src/athena/research/config.py`: immutable `ResearchConfig.skip_validate` default.
- Modify `src/athena/research/runtime/services.py`: mutable `RuntimeOptions.skip_validate`.
- Modify `src/athena/research/runtime/facade.py`: `ResearchRuntime(skip_validate=False)` composition input.
- Modify `src/athena/research/runtime/bootstrap.py`: wire the flag into session options and `PhaseActions`.
- Modify `src/athena/research/runtime/settings.py`: GUI snapshot, whitelist, strict bool update.
- Modify `src/athena/research/supervisor/deps.py`: append `PhaseActions.skip_validate` without changing historical positional fields.
- Modify `src/athena/research/supervisor/supervisor.py`: live `configure_options(skip_validate=...)` update.
- Modify `src/gui_gateway/state_store.py`: project-keyed skip-preference persistence and tolerant parsing.
- Modify `src/gui_gateway/__main__.py`: restore the project preference into every GUI runtime factory call.
- Modify `src/gui_gateway/handler.py`: persist a successfully applied preference while preserving other GUI state.
- Modify `src/athena/research/supervisor/state.py`: optional resume-only `validation_skipped` audit marker.
- Modify `src/athena/research/report.py`: explicit skipped-validation report section.
- Modify `src/athena/research/exp_docs.py`: pass skip disclosure through final/optimization document writers.
- Modify `src/athena/gui/service.py`: regenerate reports from the durable run marker.
- Modify `src/athena/research/supervisor/search_loop.py`: treat skip mode as auto-settlement for completed Plan turns.
- Modify `src/athena/research/supervisor/phases.py`: direct, idempotent SEARCH finalization and transition priority.
- Modify `athena-gui/src/lib/tauri-bridge.ts`: typed/defaulted `GuiSettings.skip_validate`.
- Modify `athena-gui/src/components/SettingsPanel.tsx`: new switch, save patch, and disabled auto-validation control.
- Modify `athena-gui/src/components/SettingsPanel.module.css`: disabled switch and disclosure layout.
- Create `athena-gui/src/components/__tests__/SettingsPanel.test.tsx`: component-level settings behavior.
- Modify focused Python tests under `test/unit/gui/`, `test/unit/research/`, `test/unit/research/supervisor/`, `test/integration/research/`, and `tests/`.
- Modify `docs/athena-gui-design.md` and `docs/athena-guide/06-workflow.md`: user-facing settings and lifecycle behavior.
- Update this plan, the supporting design status, `codex_docs/CURRENT.md`, and the completion report only at their named gates.

## Execution Preflight

- [x] Record `git status --short --branch`, `git diff --cached --name-status`, `git log -1 --oneline`, and `git worktree list --porcelain` in this isolated worktree. Confirm only the committed design/planning files differ from base and do not touch the dirty main worktree. Evidence: clean `feat/skip-validate-final` at planning commit `0cf0580`; linked-worktree git dir differs from the common dir; main was read only.
- [x] Install worktree-local frontend dependencies with `npm --prefix athena-gui ci`; record npm's own audit summary separately from test results. Evidence: exit 0, 420 packages installed; npm reported one `whatwg-encoding` deprecation and no audit summary. A separate `npm audit --audit-level=moderate` produced no response from the registry and was interrupted, so no vulnerability result is claimed.
- [x] Run the focused backend baseline:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/gui/test_state_store.py `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py
```

Expected: exit 0. Record exact pass/fail/warning counts; ACL-owned pytest cache warnings are environment evidence, not feature failures.

Evidence: after applying the mandatory worktree `PYTHONPATH`, exit 0 with
`130 passed in 15.67s`. A diagnostic run without that override imported
production modules from dirty main and produced three false mixed-checkout
failures; it is environment evidence, not a repository baseline failure.

- [x] Run the focused frontend baseline:

```powershell
npm --prefix athena-gui test -- --run `
  src/lib/__tests__/tauri-bridge.test.ts `
  src/__tests__/App.test.tsx `
  src/hooks/__tests__/useWorkspace.test.tsx
```

Expected: all listed files pass before adding the new Settings-panel test.

Evidence: exit 0; 3 files and 25 tests passed in 3.70s.

---

### Task 1: Runtime and live-settings policy contract

**Files:**
- Modify: `src/athena/research/config.py`
- Modify: `src/athena/research/runtime/services.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/settings.py`
- Modify: `src/athena/research/supervisor/deps.py`
- Modify: `src/athena/research/supervisor/supervisor.py`
- Test: `test/unit/research/test_runtime_settings.py`
- Test: `test/unit/research/supervisor/test_supervisor.py`

**Interfaces:**
- Produces: `ResearchRuntime(..., skip_validate: bool = False)`.
- Produces: `ResearchConfig.skip_validate`, `RuntimeOptions.skip_validate`, and `PhaseActions.skip_validate`, all `bool = False`.
- Changes: `Supervisor.configure_options(..., skip_validate: bool | None = None) -> None`.
- Changes: GUI settings snapshot/patch accepts exactly `skip_validate` and rejects non-booleans.
- Preserves: historical positional construction of `PhaseActions` by appending the new defaulted field after all existing fields.

- [x] **Step 1: Write failing constructor, snapshot, mutation, and validation tests**

Add these focused cases to `test_runtime_settings.py`:

```python
def test_skip_validate_defaults_off_and_projects_to_settings(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    assert runtime.config.skip_validate is False
    assert runtime.session.options.skip_validate is False
    assert runtime.settings()["skip_validate"] is False


@pytest.mark.asyncio
async def test_apply_settings_updates_skip_validate_without_changing_auto_validate(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path, auto_validate=True, skip_validate=False
    )

    snapshot = await runtime.apply_settings({"skip_validate": True})

    assert snapshot["skip_validate"] is True
    assert runtime.session.options.skip_validate is True
    assert runtime.supervisor._deps.phases.skip_validate is True
    assert runtime.session.options.auto_validate is True
    assert runtime.supervisor._deps.phases.auto_validate is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, "true", None, []])
async def test_apply_settings_rejects_non_boolean_skip_validate(
    tmp_path, value
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    with pytest.raises(ValueError, match="skip_validate must be a bool"):
        await runtime.apply_settings({"skip_validate": value})
```

Extend `test_configure_options_updates_focused_dependencies` so one call supplies
`skip_validate=True` and asserts both phase flags. Extend the positional
`PhaseActions` regression to assert its old sixth and seventh arguments retain
their meanings and the appended `skip_validate` defaults to `False`.

- [x] **Step 2: Run the tests and capture RED evidence**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_supervisor.py
```

Expected: FAIL because the constructor, session options, phase dependency, and settings whitelist do not yet expose `skip_validate`.

- [x] **Step 3: Thread the minimal policy through composition**

Add the fields without changing defaults for current callers:

```python
# config.py
@dataclass(frozen=True)
class ResearchConfig:
    # existing fields...
    auto_validate: bool = False
    skip_validate: bool = False


# runtime/services.py
@dataclass
class RuntimeOptions:
    # existing fields...
    auto_validate: bool = False
    skip_validate: bool = False


# supervisor/deps.py -- append after prepare_resume_is_attested
@dataclass
class PhaseActions:
    # all existing fields in their current order...
    prepare_resume_is_attested: _PrepareResumeIsAttested | None = None
    skip_validate: bool = False
```

Add `skip_validate` to `ResearchRuntime.__init__`, the `build_config` field map,
`RuntimeOptions(...)`, and keyword construction of `PhaseActions(...)`.

- [x] **Step 4: Implement the strict live settings projection**

Add `skip_validate` to `SETTINGS_WHITELIST` and `snapshot()`. Extend the policy
branch exactly as follows:

```python
if "skip_validate" in patch:
    skip_validate = patch["skip_validate"]
    if not isinstance(skip_validate, bool):
        raise ValueError("skip_validate must be a bool")
    rt.session.options.skip_validate = skip_validate
    rt.supervisor.configure_options(skip_validate=skip_validate)
```

Extend `Supervisor.configure_options` with an optional keyword and update only
`self._deps.phases.skip_validate` when it is non-`None`. Do not derive or mutate
`auto_validate`.

- [x] **Step 5: Run, format, check, update this task, and commit**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_supervisor.py
..\..\.venv\Scripts\python.exe -m black `
  src/athena/research/config.py `
  src/athena/research/runtime/services.py `
  src/athena/research/runtime/facade.py `
  src/athena/research/runtime/bootstrap.py `
  src/athena/research/runtime/settings.py `
  src/athena/research/supervisor/deps.py `
  src/athena/research/supervisor/supervisor.py `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_supervisor.py
git diff --check -- `
  src/athena/research/config.py `
  src/athena/research/runtime/services.py `
  src/athena/research/runtime/facade.py `
  src/athena/research/runtime/bootstrap.py `
  src/athena/research/runtime/settings.py `
  src/athena/research/supervisor/deps.py `
  src/athena/research/supervisor/supervisor.py `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_supervisor.py
```

After fresh GREEN evidence, check every Task 1 box. Stage only Task 1 paths and
this plan, then commit:

```powershell
git commit -m "feat: define skip validate runtime policy"
```

---

### Task 2: Project-scoped GUI preference persistence

**Files:**
- Modify: `src/gui_gateway/state_store.py`
- Modify: `src/gui_gateway/__main__.py`
- Modify: `src/gui_gateway/handler.py`
- Test: `test/unit/gui/test_state_store.py`
- Test: `tests/test_gui_gateway_main.py`
- Test: `tests/test_gui_gateway_handler.py`

**Interfaces:**
- Produces: `GuiState.skip_validate_by_project: dict[str, bool] | None = None`.
- Produces: `GuiState.skip_validate_for(project_root: str | Path | None) -> bool` using a resolved project key and a false fallback.
- Changes: `_make_runtime(..., skip_validate: bool = False) -> ResearchRuntime`.
- Changes: the injectable `start_server(make_runtime=...)` seam receives the
  validated `skip_validate` keyword in addition to its existing keywords;
  injected factories must accept the expanded keyword contract.
- Changes: every gateway-created session runtime receives the active project's persisted preference.
- Changes: a successful `settings_set` persists the returned boolean for `self._project_root` without dropping `active_project_root` or `last_sessions`.

- [x] **Step 1: Write failing tolerant-store tests**

Add these cases to `test_state_store.py`:

```python
def test_skip_validate_preferences_round_trip_and_are_project_scoped(tmp_path) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    store = GuiStateStore(tmp_path / "gui_state.json")
    store.save(
        GuiState(
            active_project_root=str(project_a),
            last_sessions={str(project_a.resolve()): "s-1"},
            skip_validate_by_project={str(project_a.resolve()): True},
        )
    )

    restored = store.load()

    assert restored.skip_validate_for(project_a) is True
    assert restored.skip_validate_for(project_b) is False
    assert restored.last_sessions == {str(project_a.resolve()): "s-1"}


def test_malformed_skip_validate_entries_are_dropped(tmp_path) -> None:
    path = tmp_path / "gui_state.json"
    path.write_text(
        json.dumps(
            {
                "skip_validate_by_project": {
                    str(tmp_path.resolve()): True,
                    "/bad/int": 1,
                    "/bad/string": "true",
                }
            }
        ),
        encoding="utf-8",
    )

    restored = GuiStateStore(path).load()

    assert restored.skip_validate_by_project == {str(tmp_path.resolve()): True}
```

Extend missing/legacy/corrupt cases to assert `skip_validate_for(...) is False`.

- [x] **Step 2: Write failing gateway restore and preservation tests**

In `tests/test_gui_gateway_main.py`, patch `ResearchRuntime`, call
`_make_runtime(..., skip_validate=True)`, and assert the option is passed without
changing the existing `auto_validate=True` GUI default. Add a `start_server`
factory-capture case whose injected `make_runtime` accepts `**options` and
asserts `options["skip_validate"]` matches the active project's stored value;
this makes the expanded injection seam explicit.

In `tests/test_gui_gateway_handler.py`, add a runtime double whose
`apply_settings` returns its updated snapshot, then prove:

```python
result = await handler.dispatch("settings_set", {"patch": {"skip_validate": True}})
saved = store.load()
assert result["skip_validate"] is True
assert saved.skip_validate_for(tmp_path) is True
assert saved.active_project_root == str(tmp_path.resolve())
assert saved.last_sessions == {str(tmp_path.resolve()): "s-1"}
```

Switch to another named session through a factory that reads
`stored.skip_validate_for(root)` and assert the replacement runtime receives
`True`. Add a second project root with `False` to prove isolation. Extend
`test_set_project_root_keeps_remembered_sessions` and
`test_handler_remembers_the_last_session_per_workspace` so both also preserve
the preference map.

- [x] **Step 3: Run the tests and capture RED evidence**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/gui/test_state_store.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py
```

Expected: FAIL because the stored map, factory argument, and handler persistence
do not exist.

- [x] **Step 4: Implement tolerant project lookup and atomic storage**

Add a boolean-map parser parallel to `_parse_last_sessions`:

```python
def _parse_skip_validate_by_project(raw: object) -> dict[str, bool] | None:
    if not isinstance(raw, dict):
        return None
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, bool)
    }
```

Add the dataclass field and lookup:

```python
def skip_validate_for(self, project_root: str | Path | None) -> bool:
    key = str(Path(project_root or ".").expanduser().resolve())
    return (self.skip_validate_by_project or {}).get(key, False)
```

Load and save the map in `GuiStateStore`. Every existing `GuiState(...)`
reconstruction in `handler.py` must copy `stored.skip_validate_by_project` so a
project or session switch cannot erase it.

- [x] **Step 5: Restore and persist the preference through the gateway**

Add `skip_validate` to `_make_runtime` and pass it to `ResearchRuntime`. In
`start_server.factory`, load the current `GuiState` for every runtime creation
and pass `stored.skip_validate_for(project_root)` to `make_runtime`.

Add one handler helper:

```python
def _remember_skip_validate(self, enabled: bool) -> None:
    stored = self._state_store.load()
    preferences = dict(stored.skip_validate_by_project or {})
    preferences[str(self._project_root.resolve())] = enabled
    self._state_store.save(
        GuiState(
            active_project_root=stored.active_project_root,
            last_sessions=stored.last_sessions,
            skip_validate_by_project=preferences,
        )
    )
```

For `settings_set`, await the service first. Only if the input patch contains
`skip_validate`, read the validated boolean from the returned snapshot and call
the helper. Return the snapshot unchanged. Do not persist a raw request value.

- [x] **Step 6: Run, format, check, update this task, and commit**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/gui/test_state_store.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py
..\..\.venv\Scripts\python.exe -m black `
  src/gui_gateway/state_store.py `
  src/gui_gateway/__main__.py `
  src/gui_gateway/handler.py `
  test/unit/gui/test_state_store.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py
git diff --check -- `
  src/gui_gateway/state_store.py `
  src/gui_gateway/__main__.py `
  src/gui_gateway/handler.py `
  test/unit/gui/test_state_store.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py
```

After GREEN evidence, check every Task 2 box. Stage only Task 2 paths and this
plan, then commit:

```powershell
git commit -m "feat: persist skip validate GUI preference"
```

---

### Task 3: Honest skipped-validation report and durable run marker

**Files:**
- Modify: `src/athena/research/supervisor/state.py`
- Modify: `src/athena/research/report.py`
- Modify: `src/athena/research/exp_docs.py`
- Modify: `src/athena/gui/service.py`
- Test: `test/unit/research/supervisor/test_state.py`
- Test: `test/unit/research/test_report.py`
- Test: `test/unit/research/test_exp_docs.py`

**Interfaces:**
- Produces: `ResearchState.validation_skipped: bool | None = None`, stored only through `RESUME_FIELDS`.
- Produces: `VALIDATION_SKIPPED_NOTICE = "VALIDATE 已跳过。本报告仅使用 SEARCH 结果，不包含独立最终测试分数或泛化差距。"`.
- Changes: `report.build_final_report(..., *, validation_skipped: bool = False) -> str`.
- Changes: `exp_docs.build_final_report`, `build_optimization_report`, and `write_reports` gain the same defaulted keyword.
- Preserves: all existing report callers and output when `validation_skipped=False`.

- [x] **Step 1: Write failing resume-only marker tests**

Extend `test_state.py`:

```python
def test_validation_skipped_round_trips_only_through_resume_metadata(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = _search_state().model_copy(update={"validation_skipped": True})

    state.save(path)

    core = json.loads(path.read_text(encoding="utf-8"))
    resume = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert "validation_skipped" not in core
    assert resume["validation_skipped"] is True
    assert ResearchState.load(path).validation_skipped is True
```

Extend the legacy-state test to assert the marker defaults to `None`, and the
stale-resume test to prove a stale `validation_skipped=true` is ignored with the
rest of the stale metadata.

Add a crash-window regression: first save a SEARCH state without the marker,
then prepare a COMPLETED copy with `validation_skipped=True`; monkeypatch
`atomic_write_json` to raise only when the target is `resume.json`. The failed
save must leave the previously readable SEARCH core authoritative, and loading
it must yield `phase="SEARCH"` and `validation_skipped is None`. This test fails
under the current core-first ordering because it exposes a terminal core without
the audit marker.

- [x] **Step 2: Write failing disclosure and contradiction tests**

In `test_report.py` add:

```python
def test_report_discloses_skipped_validate_without_final_metrics() -> None:
    report = build_final_report(
        _tree_with_sota(), None, validation_skipped=True
    )

    assert "VALIDATE 已跳过" in report
    assert "仅使用 SEARCH 结果" in report
    assert "final_test_score" not in report
    assert "generalization_gap" not in report


def test_report_rejects_skip_marker_with_real_validation() -> None:
    with pytest.raises(ValueError, match="cannot be both skipped and present"):
        build_final_report(
            _tree_with_sota(),
            {"final_test_score": 0.79},
            validation_skipped=True,
        )
```

In `test_exp_docs.py`, call `write_reports(..., validation_skipped=True)` and
assert both generated documents mention the missing independent validation and
neither contains a final score/generalization claim.

- [x] **Step 3: Run the tests and capture RED evidence**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/test_report.py `
  test/unit/research/test_exp_docs.py
```

Expected: FAIL because the marker and keyword contracts are absent.

- [x] **Step 4: Add the backward-compatible marker and report keyword**

Append `"validation_skipped"` to `RESUME_FIELDS` and add:

```python
validation_skipped: bool | None = None
```

Do not put it in core `state.json`. In `report.py`, reject the contradictory
combination of a truthy validation mapping and `validation_skipped=True`, then
render `VALIDATION_SKIPPED_NOTICE` as the validation section instead of metric
rows. Export the constant for the experiment-document wrapper.

Close the two-file crash window in `ResearchState.save()`: build both payloads
first and, when resume metadata is non-empty, atomically replace `resume.json`
before atomically replacing its digest-matched core `state.json`. If the resume
write fails, the old core remains authoritative; if the core write then fails,
the new resume digest is stale against the old core and is ignored. Keep the
empty-resume path core-first followed by `resume.json` removal because any stale
resume is already rejected by the new core digest.

Thread the keyword through `exp_docs.build_final_report`,
`build_optimization_report`, and `write_reports`. The optimization report gets
the same notice before its evidence-based SEARCH guidance. Existing calls omit
the keyword and retain byte-for-byte behavior.

- [x] **Step 5: Make GUI report regeneration use run history**

Change `_build_report(runtime)` in `src/athena/gui/service.py` to call:

```python
return build_final_report(
    runtime.tree,
    runtime.state.validation,
    validation_skipped=runtime.state.validation_skipped is True,
)
```

The current preference must not be consulted here.

- [x] **Step 6: Run, format, check, update this task, and commit**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/test_report.py `
  test/unit/research/test_exp_docs.py
..\..\.venv\Scripts\python.exe -m black `
  src/athena/research/supervisor/state.py `
  src/athena/research/report.py `
  src/athena/research/exp_docs.py `
  src/athena/gui/service.py `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/test_report.py `
  test/unit/research/test_exp_docs.py
git diff --check -- `
  src/athena/research/supervisor/state.py `
  src/athena/research/report.py `
  src/athena/research/exp_docs.py `
  src/athena/gui/service.py `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/test_report.py `
  test/unit/research/test_exp_docs.py
```

After GREEN evidence, check every Task 3 box. Stage only Task 3 paths and this
plan, then commit:

```powershell
git commit -m "feat: disclose skipped validation in final reports"
```

---

### Task 4: SEARCH-to-COMPLETED lifecycle without validation

**Files:**
- Modify: `src/athena/research/supervisor/search_loop.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Test: `test/integration/research/test_autonomous_research.py`
- Test: `test/integration/research/test_human_plan_boundary.py`
- Test: `test/unit/research/supervisor/test_supervisor.py`

**Interfaces:**
- Produces privately: `PhaseMachine._finalize_without_validation() -> Awaitable[None]`.
- Produces privately: `PhaseMachine._record_skipped_final(sota_id, sota) -> None`.
- Produces: `SKIPPED_VALIDATION_OUTPUT = "SEARCH 已完成；VALIDATE 已跳过。Final 报告仅使用 SEARCH 结果。"`.
- Changes privately: `PhaseMachine._write_reports(validation=None, *, validation_skipped: bool = False) -> None` and forwards both arguments to `exp_docs.write_reports`.
- Changes: post-SEARCH priority is skip, automatic validation, then existing human gate.
- Changes: Plan settlement treats `auto_validate or skip_validate` as automatic completion policy.

- [x] **Step 1: Write the failing end-to-end skip test**

Add a test beside `test_all_phases_share_one_durable_state` using its local
`PrepareResult` pattern:

```python
@pytest.mark.asyncio
async def test_skip_validate_completes_from_search_without_calling_validation(
    tmp_path: Path,
) -> None:
    validation_calls: list[tuple[str, float]] = []
    evidence_ref = "sha256:" + "1" * 64
    evaluator_ref = "sha256:" + "2" * 64
    predictions_ref = "sha256:" + "3" * 64
    report_ref = "sha256:" + "4" * 64

    async def prepare() -> PrepareResult:
        return PrepareResult(
            evaluator_ref=evaluator_ref,
            metric=0.71,
            commit="prepare-commit",
            predictions_ref=predictions_ref,
            evidence_ref=evidence_ref,
            report_ref=report_ref,
        )

    async def validate(commit: str, metric: float):
        validation_calls.append((commit, metric))
        raise AssertionError("VALIDATE must not run")

    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="improve the trusted baseline",
        search_limit=0,
        auto_validate=True,
        skip_validate=True,
        prepare_phase=prepare,
        validation_phase=validate,
    )
    events: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, data: events.append((kind, data)))

    lifecycle = await runtime.start()
    await asyncio.wait_for(asyncio.shield(lifecycle), timeout=5)

    assert validation_calls == []
    assert runtime.state.phase == "COMPLETED"
    assert runtime.state.status == "COMPLETED"
    assert runtime.state.validation is None
    assert runtime.state.validation_skipped is True
```

Assert distinct state
phases are exactly `PREPARE, SEARCH, COMPLETED`, the output includes
`VALIDATE 已跳过`, both report files exist, and `final.json` has
`status="SKIPPED"`, `metric.primary is None`, and the SEARCH score only in
`metric.reference`.

- [x] **Step 2: Write failing precedence, wait, and recovery tests**

Add cases proving:

1. `skip_validate=True` wins when `auto_validate=True`.
2. Both false retain the existing SEARCH/human wait path and produce no final
   skipped document.
3. A runtime restored at `VALIDATE/RUNNING` calls validation even when the new
   preference is true.
4. An already `COMPLETED` run does not call either finalizer again.
5. A `SEARCH/WAITING` run stays waiting until `resume_current_task()`; after
   resume and normal search exhaustion it completes through skip mode.

In the completed-Plan settlement test, construct dependencies with
`auto_validate=False, skip_validate=True`, return a `wait` settlement, and
assert the Plan is settled instead of parking SEARCH. Retain the existing
`auto_validate=False, skip_validate=False` assertion.

- [x] **Step 3: Write the failing atomicity/idempotence test**

Monkeypatch `athena.research.supervisor.phases.write_reports` to raise
`OSError("final report unavailable")` on the first call. Run the skip lifecycle
and assert it finishes at `phase="SEARCH", status="FAILED"`, with no durable
COMPLETED state. Restore the writer, invoke `resume_current_task()`, await the
new lifecycle task, and assert one stable `runs/final-skipped.json`, terminal
COMPLETED state, and no validation call.

Add a state-save failure at the direct-finalizer boundary and assert the helper
restores the in-memory phase/status/marker before propagating; after retry, the
same derived file paths are overwritten rather than duplicated.

- [x] **Step 4: Run the focused tests and capture RED evidence**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py
```

Expected: FAIL because the skip branch and direct finalizer do not exist.

- [x] **Step 5: Implement automatic settlement and transition precedence**

In `SearchLoop._apply_completed_turn`, change only the wait settlement policy:

```python
if self._deps.phases.auto_validate or self._deps.phases.skip_validate:
    await self._plans.settle_plan(plan_id, state.best_ref, completed.result)
else:
    self._state.status = "WAITING"
    self._plans.save_state()
```

In `PhaseMachine.continue_phase`, immediately after `run_search()` and the stop
guard, evaluate `skip_validate` before `auto_validate`. Return after direct
finalization so the later VALIDATE block cannot run.

- [x] **Step 6: Implement the honest direct finalizer**

Use these preconditions before writing anything:

```python
if self._state.phase != "SEARCH" or self._state.status != "RUNNING":
    raise RuntimeError("skip finalization requires a running SEARCH")
if tuple(self._run.running_ids()):
    raise RuntimeError("skip finalization requires all SEARCH plans to settle")
if self._state.validation is not None:
    raise RuntimeError("cannot skip VALIDATE with a validation checkpoint")
sota_id = self._tree.best_experiment_id()
if sota_id is None:
    raise RuntimeError("skip finalization requires a trusted SEARCH SOTA")
sota = self._tree.get_experiment(sota_id)
if sota.eval is None:
    raise RuntimeError("skip finalization requires a trusted SEARCH score")
```

First extend `_write_reports` with the defaulted keyword shown in this task's
interface and forward it to `exp_docs.write_reports`. Then call
`self._write_reports(validation_skipped=True)` and write this stable stage
record through `write_stage_doc`:

```python
{
    "run_id": "final-skipped",
    "stage": "final",
    "status": "SKIPPED",
    "metric": {
        "name": self._metric_name,
        "direction": self._deps.search.direction,
        "primary": None,
        "reference": sota.eval.primary,
    },
    "reason": {
        "kind": "validation_skipped",
        "summary": "Independent VALIDATE was skipped by project policy.",
    },
    "provenance": {
        "sota_experiment_id": sota_id,
        "sota_commit": sota.commit,
    },
}
```

Snapshot `phase`, `status`, and
`validation_skipped`; set the terminal values and call `self._plans.save_state()`
inside `try/except BaseException`, restoring the snapshot on failure.

After a successful save, publish `SKIPPED_VALIDATION_OUTPUT` and then
`self._plans.publish_state()`. Catch and log ordinary `Exception` separately for
each notification, but do not catch `asyncio.CancelledError` or change the
durable state after notification failure.

- [x] **Step 7: Run, format, check, update this task, and commit**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py
..\..\.venv\Scripts\python.exe -m black `
  src/athena/research/supervisor/search_loop.py `
  src/athena/research/supervisor/phases.py `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py
git diff --check -- `
  src/athena/research/supervisor/search_loop.py `
  src/athena/research/supervisor/phases.py `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py
```

After GREEN evidence, check every Task 4 box. Stage only Task 4 paths and this
plan, then commit:

```powershell
git commit -m "feat: finalize search without validation"
```

---

### Task 5: Frontend Settings-panel control

**Files:**
- Modify: `athena-gui/src/lib/tauri-bridge.ts`
- Modify: `athena-gui/src/components/SettingsPanel.tsx`
- Modify: `athena-gui/src/components/SettingsPanel.module.css`
- Create: `athena-gui/src/components/__tests__/SettingsPanel.test.tsx`

**Interfaces:**
- Changes: `GuiSettings.skip_validate: boolean` and `DEFAULT_GUI_SETTINGS.skip_validate=false`.
- Changes: the existing settings patch always includes the normalized boolean.
- Produces: accessible checkbox name `跳过 VALIDATE` and disclosure text `不会运行独立最终评估`.
- Preserves: `form.auto_validate` while its checkbox is disabled by skip mode.

- [ ] **Step 1: Write the failing component test**

Create hoist-safe bridge mocks before importing the component:

```tsx
const bridgeMocks = vi.hoisted(() => ({
  settingsGet: vi.fn(),
  settingsSet: vi.fn(),
  setProjectRoot: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  settingsGet: bridgeMocks.settingsGet,
  settingsSet: bridgeMocks.settingsSet,
  setProjectRoot: bridgeMocks.setProjectRoot,
}));
```

Use this core behavior case:

```tsx
it("saves skip validate while preserving the automatic validation choice", async () => {
  bridgeMocks.settingsGet.mockResolvedValue({
    ...DEFAULT_GUI_SETTINGS,
    project_root: "C:/project",
    auto_validate: true,
    skip_validate: false,
  });
  bridgeMocks.settingsSet.mockImplementation(async (patch) => ({
    ...DEFAULT_GUI_SETTINGS,
    ...patch,
  }));
  render(<SettingsPanel onClose={vi.fn()} />);

  const skip = await screen.findByRole("checkbox", { name: /跳过 VALIDATE/i });
  const automatic = screen.getByRole("checkbox", { name: /自动验证/i });
  fireEvent.click(skip);

  expect(automatic).toBeDisabled();
  expect(automatic).toBeChecked();
  expect(screen.getByText(/不会运行独立最终评估/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /保存/ }));
  await waitFor(() => expect(bridgeMocks.settingsSet).toHaveBeenCalled());
  expect(bridgeMocks.settingsSet.mock.calls.at(-1)?.[0]).toMatchObject({
    auto_validate: true,
    skip_validate: true,
  });

  fireEvent.click(skip);
  expect(automatic).toBeEnabled();
  expect(automatic).toBeChecked();
});
```

Import and use `fireEvent` from `@testing-library/react`; do not add a new
dependency. Add a second case for a loaded `skip_validate=true` snapshot.

- [ ] **Step 2: Run the component test and capture RED evidence**

Run:

```powershell
npm --prefix athena-gui test -- --run src/components/__tests__/SettingsPanel.test.tsx
```

Expected: FAIL because the typed field and control are absent.

- [ ] **Step 3: Implement the typed setting and accessible UI**

Add `skip_validate` next to `auto_validate` in `GuiSettings`, defaults,
`WritableField`, and the save patch. Render the new checkbox in the switch
section:

```tsx
<label className="switch">
  <input
    type="checkbox"
    checked={form.skip_validate}
    onChange={(event) => patchField("skip_validate", event.target.checked)}
    aria-describedby="skip-validate-help"
  />
  <span className="switch__track" />
  跳过 VALIDATE
</label>
```

Add `disabled={form.skip_validate}` to the existing auto-validation checkbox;
do not update `form.auto_validate` when skip changes. Add a help paragraph with
`id="skip-validate-help"` stating that SEARCH proceeds to Final without an
independent final evaluation or final/generalization metrics.

Add a module class to lower opacity and use a not-allowed cursor for the disabled
auto-validation label; retain keyboard focus and the existing switch markup.

- [ ] **Step 4: Run focused frontend tests/build and capture GREEN evidence**

Run:

```powershell
npm --prefix athena-gui test -- --run `
  src/components/__tests__/SettingsPanel.test.tsx `
  src/lib/__tests__/tauri-bridge.test.ts `
  src/__tests__/App.test.tsx `
  src/hooks/__tests__/useWorkspace.test.tsx
npm --prefix athena-gui run build
git diff --check -- `
  athena-gui/src/lib/tauri-bridge.ts `
  athena-gui/src/components/SettingsPanel.tsx `
  athena-gui/src/components/SettingsPanel.module.css `
  athena-gui/src/components/__tests__/SettingsPanel.test.tsx
```

Expected: focused Vitest files and the TypeScript/Vite production build pass.

- [ ] **Step 5: Update this task and commit**

After fresh GREEN evidence, check every Task 5 box. Stage only Task 5 paths and
this plan, then commit:

```powershell
git commit -m "feat(gui): add skip validate setting"
```

---

### Task 6: Cross-layer persistence and recovery proof

**Files:**
- Modify: `tests/test_gui_gateway_main.py`
- Modify: `tests/test_gui_gateway_handler.py`
- Modify: `tests/test_gui_gateway_transport.py`
- Modify: `test/unit/research/test_runtime_settings.py`
- Modify: `test/integration/research/test_autonomous_research.py`
- Modify: `test/unit/research/test_breakpoint_resume.py`

**Interfaces:**
- Proves: one `settings_set` patch crosses the WebSocket boundary, persists the validated project preference, and appears in the next session runtime snapshot.
- Proves: a durable skipped run regenerates its report from `validation_skipped`, not the mutable project preference.
- Proves: old SEARCH/VALIDATE checkpoints retain their documented resume behavior.

- [ ] **Step 1: Add a WebSocket settings round-trip test**

Extend the test transport runtime with a real in-memory settings mapping and an
`apply_settings` method. Send:

```json
{"request_id": 41, "method": "settings_set", "params": {"patch": {"skip_validate": true}}}
```

Assert the decoded response contains `skip_validate: true`, the fake runtime
received the exact snake_case key, and no Tauri/RPC method addition was needed.

- [ ] **Step 2: Add restart/session and historical-report integration tests**

Create two runtimes for the same project through the gateway factory with
different state roots and assert both inherit the stored `True` preference.
Recreate the factory/store to simulate gateway restart and assert the value is
still `True`; a second project remains `False`.

Complete one skip run, then change the project's live preference back to false,
reload the completed run from disk, call `GuiService.generate_report()`, and
assert the report still says `VALIDATE 已跳过` because the run marker is true.

- [ ] **Step 3: Add the resume matrix**

In `test_breakpoint_resume.py`, cover these exact cases with fake adapters:

- SEARCH/RUNNING + skip true resumes and eventually completes without validation;
- SEARCH/WAITING + skip true remains waiting until explicit resume;
- VALIDATE/RUNNING + skip true resumes validation and records real validation;
- COMPLETED + skip true is a no-op.

For every case assert both validation call counts and final
`phase/status/validation/validation_skipped` values.

- [ ] **Step 4: Run the cross-layer slice and fix only contract gaps**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py `
  test/unit/research/test_runtime_settings.py `
  test/integration/research/test_autonomous_research.py `
  test/unit/research/test_breakpoint_resume.py
```

Expected: all tests pass without a network provider. If a failure reveals a
contract gap, change only the smallest production file already owned by Tasks
1-4, rerun that task's focused test, and include the exact file in this task's
commit.

- [ ] **Step 5: Format, check, update this task, and commit**

Run Black on changed Python files and:

```powershell
git diff --check -- `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py `
  test/unit/research/test_runtime_settings.py `
  test/integration/research/test_autonomous_research.py `
  test/unit/research/test_breakpoint_resume.py
```

After GREEN evidence, check every Task 6 box. Stage only Task 6 paths, any
explicitly revalidated production gap fix, and this plan. Commit:

```powershell
git commit -m "test: prove skip validate persistence and recovery"
```

---

### Task 7: Documentation, verification, main integration, and closeout

**Files:**
- Modify: `docs/athena-gui-design.md`
- Modify: `docs/athena-guide/06-workflow.md`
- Modify after verification: `docs/superpowers/specs/2026-09-04-skip-validate-final-design.md`
- Create after verification: `codex_docs/2026-09-04-skip-validate-final-completion-report.md`
- Modify after verification: `codex_docs/CURRENT.md`
- Delete after verification: `docs/superpowers/plans/2026-09-04-skip-validate-final.md`

**Interfaces:**
- Documents: three distinct post-SEARCH policies, project persistence, missing validation metrics, and resume behavior.
- Integrates: the feature branch, including its universal-Continue prerequisite history, into dirty `main` only after preserving exact overlapping user files on a safety branch.
- Restores: the paused universal-Continue plan as `CURRENT.md`'s active plan after this feature closes.

- [ ] **Step 1: Update user-facing documentation**

Document the settings row as:

```text
skip_validate=false preserves automatic/manual VALIDATE behavior.
skip_validate=true takes precedence, skips the independent evaluator, writes a
SEARCH-only Final report, and enters COMPLETED. No final-test score or
generalization gap is available. The value is stored per project and shared by
that project's GUI sessions.
```

Document that an already entered VALIDATE run continues and a parked WAITING run
requires Continue. Do not describe COMPLETED as a new FINAL backend phase.

- [ ] **Step 2: Run backend formatting and static checks**

Run:

```powershell
..\..\.venv\Scripts\python.exe -m black --check `
  src/athena/research/config.py `
  src/athena/research/runtime/services.py `
  src/athena/research/runtime/facade.py `
  src/athena/research/runtime/bootstrap.py `
  src/athena/research/runtime/settings.py `
  src/athena/research/supervisor/deps.py `
  src/athena/research/supervisor/supervisor.py `
  src/athena/research/supervisor/state.py `
  src/athena/research/supervisor/search_loop.py `
  src/athena/research/supervisor/phases.py `
  src/athena/research/report.py `
  src/athena/research/exp_docs.py `
  src/athena/gui/service.py `
  src/gui_gateway/state_store.py `
  src/gui_gateway/__main__.py `
  src/gui_gateway/handler.py `
  test/unit/gui/test_state_store.py `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py `
  test/unit/research/test_breakpoint_resume.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py
..\..\.venv\Scripts\python.exe -m ruff check `
  src/athena/research `
  src/athena/gui/service.py `
  src/gui_gateway `
  test/unit/gui/test_state_store.py `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/supervisor/test_supervisor.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py `
  test/unit/research/test_breakpoint_resume.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py
..\..\.venv\Scripts\python.exe -m compileall -q src/athena src/gui_gateway
```

Expected: exit 0. If broad Ruff names an unrelated pre-existing file, record it
and rerun with every changed Python path explicitly; do not edit unrelated code.

- [ ] **Step 3: Run focused and broad Python verification**

Run in order:

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/gui/test_state_store.py `
  test/unit/research/test_runtime_settings.py `
  test/unit/research/supervisor/test_state.py `
  test/unit/research/supervisor/test_supervisor.py `
  test/unit/research/test_report.py `
  test/unit/research/test_exp_docs.py `
  test/unit/research/test_breakpoint_resume.py `
  test/integration/research/test_human_plan_boundary.py `
  test/integration/research/test_autonomous_research.py `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py
..\..\.venv\Scripts\python.exe -m pytest -q `
  test/unit/research `
  test/integration/research `
  tests/test_gui_gateway_main.py `
  tests/test_gui_gateway_handler.py `
  tests/test_gui_gateway_transport.py `
  tests/test_gui_gateway_e2e.py `
  tests/test_gui_protocol_contract.py
```

Record counts, durations, and warnings for both commands. Do not claim a broad
suite is green if it exits non-zero; isolate and report a baseline failure
without modifying unrelated baseline-authority work.

- [ ] **Step 4: Run complete frontend and Rust verification**

Run:

```powershell
npm --prefix athena-gui test -- --run
npm --prefix athena-gui run build
cargo test --manifest-path athena-gui/src-tauri/Cargo.toml
cargo check --manifest-path athena-gui/src-tauri/Cargo.toml
git diff --check
```

Record Vitest file/test counts, Vite module count, Rust test counts, and warnings.

- [ ] **Step 5: Audit the delivered boundary**

Run:

```powershell
rg -n "skip_validate|validation_skipped|VALIDATE 已跳过|final-skipped" `
  src athena-gui/src test tests docs/athena-gui-design.md docs/athena-guide/06-workflow.md
rg -n "final_test_score|generalization_gap|run_validation_phase" `
  src/athena/research/supervisor/phases.py `
  src/athena/research/report.py `
  src/athena/research/exp_docs.py
git diff --stat c815eaf...HEAD
git diff --name-status c815eaf...HEAD
```

Required evidence: the skip integration tests' raising validation adapter and
call-count assertions prove skip mode has no validation invocation; report
assertions prove the skip report has no fabricated metrics. The search results
confirm `auto_validate` paths remain, the setting map is project-keyed, and only
planned paths changed; they are inventory evidence rather than substitutes for
the behavioral tests.

- [ ] **Step 6: Commit verified feature documentation**

After Steps 2-5 have fresh evidence, check all implementation and documentation
boxes through this step. Stage only the two user guides, this plan, and any
test-backed clarification to the supporting spec. Commit:

```powershell
git commit -m "docs: document skip validate finalization"
```

- [ ] **Step 7: Preserve overlapping dirty main files and integrate**

In the main worktree, run `git status --short --branch` and compare every dirty
path against `git diff --name-only main...feat/skip-validate-final`. The user has
already authorized preserving overlaps on a safety branch. If overlaps remain:

1. create `safety/main-before-skip-validate-final-20260904` from current `main`;
2. stage only the exact overlapping modified/untracked paths, never the unrelated
   `.dsh_plugins`, task3 scratch directories, or unrelated plans/tests;
3. commit them as `wip: preserve pre-integration overlapping files`;
4. switch back to `main` and verify those copies are recoverable from the safety
   commit;
5. fast-forward `main` to `origin/main`, then fast-forward it to
   `feat/skip-validate-final`.

Do not stash, reset, checkout files, delete scratch paths, or push. If a new
uncommitted overlap appears after the comparison, stop before mutation and
report its exact path.

- [ ] **Step 8: Freshly verify merged main**

On merged `main`, rerun the focused Python command from Step 3, full frontend
suite/build, Rust tests/check, and `git diff --check`. Record new output; branch
verification is not a substitute for merged-main evidence.

- [ ] **Step 9: Write completion report and close only this plan**

Create `codex_docs/2026-09-04-skip-validate-final-completion-report.md` with:

```markdown
# Skip VALIDATE and Finalize After SEARCH Completion Report

Date: 2026-09-04
Implementation and integration commits:

## Delivered behavior
## Acceptance-criteria evidence
## Verification commands and results
## Persistence and recovery evidence
## Honest-report audit
## Compatibility
## Pre-existing warnings or unrelated failures
## Unrelated worktree changes preserved
```

Set the design status to `implemented and verified`, delete this completed plan,
and update `codex_docs/CURRENT.md` so it no longer names this plan/spec as active,
lists the new report, and restores
`docs/superpowers/plans/2026-09-03-universal-continue-resume.md` as the active
plan. Keep all other parallel/paused pointers. Commit only these closeout files:

```powershell
git commit -m "docs: close skip validate finalization"
```

- [ ] **Step 10: Remove the feature worktree and branch after closeout**

After the closeout commit exists on `main`, verify the registered worktree path
is exactly
`C:\Users\80163\Desktop\挑战杯_2026\Athena\.worktrees\skip-validate-final`
and clean. Remove that exact worktree with `git worktree remove`, then delete the
merged local branch with `git branch -d feat/skip-validate-final`.
