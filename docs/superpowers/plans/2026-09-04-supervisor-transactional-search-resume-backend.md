# Supervisor Transactional Search Resume Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give completed Athena research sessions one safe, idempotent `resume_search` path while reducing the normal Supervisor surface to five tools and making every research-state write serialized, recoverable, and observable.

**Architecture:** A new `athena.research.control` package owns strict model-facing contracts, the bounded `ResearchContext`, the durable journal, the per-session mutation coordinator, and resume policy. `ResearchRuntime` acquires the existing OS byte lock before recovery/loading; existing Supervisor, scheduler, phase, settings, and background-checkpoint code delegate all `ResearchState`/`ResearchTree` writes to the coordinator. The GUI gateway and exact `/search +N` fallback call the same control service used by the Supervisor tool, while validation authority remains human-gated and per-session policy is stored in the digest-bound resume sidecar.

**Tech Stack:** Python 3.11+, Pydantic v2 strict models, asyncio, standard-library filesystem locking/logging/hashlib/json, pytest/pytest-asyncio, existing Athena AgentRuntime and ResearchTree.

## Global Constraints

- Read `codex_docs/CURRENT.md`, the approved design, and this plan before every implementation task; update the matching checkbox immediately after fresh evidence passes.
- Execute in an isolated worktree created with `using-git-worktrees`; never reset, stash, overwrite, or commit unrelated changes.
- Before modifying `src/athena/research/runtime/bootstrap.py`, `src/athena/research/runtime/facade.py`, or `src/gui_gateway/handler.py`, verify that the parallel authoritative-baseline closeout has no uncommitted edits to those paths. Stop and reconcile if it does.
- Python remains `>=3.11`; add no production dependency for locking, persistence, retries, logging, or transactions.
- The normal model-visible Supervisor surface is exactly `inspect_research`, `update_research`, `manage_hypotheses`, `dispatch_general`, and `request_user_input`. `kaggle_get_competition` is the only optional external addition.
- `update_research` and `manage_hypotheses` expose exactly `{context_version, action}` at the top level. `inspect_research` exposes only `section`, optional `cursor`, and `page_size`.
- Session IDs, provider call IDs, operation IDs, paths, journals, retry controls, runtime handles, origin permits, and rollback snapshots are framework-owned and never appear in model schemas.
- Strict discriminated actions replace generic patches. Unknown fields, coercion, empty updates, illegal enum values, and out-of-phase variants fail before persistence.
- The single internal Search safety owner is `MAX_SEARCH_ATTEMPTS = 1_000_000`; the existing limits remain `MAX_CONCURRENCY = 4`, `MAX_PLAN_TURNS = 12`, and `MAX_PATIENCE = 5`. None is a constructor or tool parameter.
- Core `state.json` retains its existing legacy shape. New validation/resume policy fields live only in digest-bound `resume.json`; runtime capability and control records are framework-owned sibling documents.
- Atomic replacement uses a unique same-directory temporary file, fsyncs file content, and retries only access/sharing violations with the internal delay sequence `(0.0, 0.02, 0.05, 0.1, 0.2)`.
- A model never receives raw filesystem write authority under `.athena/**`; path-bearing references are canonicalized and restricted to the session/artifact roots.
- A COMPLETED resume preserves the ResearchTree, SOTA, task/data/evaluator contracts, Git history, artifacts, pending hypotheses, and immutable reports; it clears only the current validation projection.
- Accepted additional budget is `terminal SEARCH experiments + active SEARCH Plans + additional_attempts`. It is capacity for new Plan starts, not a guarantee of successful scores.
- A resumed run durably disables automatic validation and ends at `phase=SEARCH,status=WAITING` after its allowance. Only a gateway-created human validation permit may later enter VALIDATE.
- A revealed final-evaluator generation is consumed. Its score is excluded from resumed Search prompts/history projections; reuse is labelled exploratory unless a fresh evaluator generation exists.
- Normal tests use fake providers and local files only. The Windows/WSL lock acceptance command is an explicit platform test, not part of ordinary offline pytest.
- Windows and WSL processes on a shared DrvFS path use the same Win32 share-denial primitive. WSL starts an internal PowerShell broker tied to the Python parent's pipe; unavailable interop fails closed and never falls back to incompatible `flock`.
- Every control failure records the full traceback, operation ID, action, versions, journal phase, rollback attempt, and rollback result in a file when either durable sink is writable; `logging_degraded` is additive and never replaces the primary error.
- No compatibility adapter may contain behavior: old Python method names delegate to the control service and are absent from the model registry.

---

## File map and ownership

### New production modules

- `src/athena/research/control/__init__.py`: deliberately small public import surface for control contracts and `ResearchControlService`.
- `src/athena/research/control/contracts.py`: strict action envelopes, response/error records, action availability, safety caps, internal origin enum, and schema projection.
- `src/athena/research/control/context.py`: allowlisted compact `ResearchContext`, digest construction, interaction-mode derivation, bounded pagination, and prompt rendering.
- `src/athena/research/control/diagnostics.py`: structured failover error records, bounded in-memory ring, redaction, and log-health reporting.
- `src/athena/research/control/journal.py`: allowlisted document images, active journal/ledger/resume-receipt stores, state-sidecar validation, install/restore, and startup recovery directives.
- `src/athena/research/control/coordinator.py`: one asyncio mutation lock, candidate copies, idempotency, journal phase machine, in-memory identity-preserving commit/rollback, and internal checkpoints.
- `src/athena/research/control/resume.py`: pure resume preconditions, blocker classification, attempt accounting, validation archival metadata, and COMPLETED/WAITING candidate transitions.
- `src/athena/research/control/service.py`: the one action router used by model tools, legacy Python adapters, scheduler/runtime checkpoints, gateway commands, and the General Agent saga checkpoint.
- `scripts/verify_cross_os_session_lock.py`: deterministic holder/probe helper for Windows-to-WSL shared-path acceptance.

### Modified production modules

- `src/athena/core/persistence.py`: unique-temp, bounded-retry atomic bytes/text/JSON primitives.
- `src/athena/core/project_lock.py`: reusable lifetime-owned `SessionOwnerLock`; retain `project_lock()` as a delegating compatibility context manager.
- `src/athena/core/windows_lock_broker.py`: internal Win32 share-denial handle, WSL path translation, PowerShell broker handshake/lifetime, and fail-closed health checks.
- `src/athena/core/research_tree.py`: identity-preserving `replace_from()` for committed/rolled-back candidates.
- `src/athena/core/agent/runtime.py`: preserve the real provider tool-call ID in `ToolContext.call_id`.
- `src/athena/agents/base_runner.py`: optional hidden per-turn context provider used only by Supervisor registration.
- `src/athena/agents/supervisor_agent.py`: five-tool registry, phase-aware schemas, internal call-ID forwarding, and no duplicated legacy schemas.
- `src/athena/agents/prompts/supervisor_agent.md`: context-is-data rule, consolidated tools, relative-budget examples, committed-result rule, and validation guard.
- `src/athena/research/config.py`: framework-owned control paths, without adding user-facing runtime knobs.
- `src/athena/research/supervisor/state.py`: new sidecar-only fields and checked load result while preserving tolerant legacy `load()`.
- `src/athena/research/supervisor/deps.py`: grouped control dependency and immediate resume-start callback.
- `src/athena/research/supervisor/supervisor.py`: attach `ResearchControlService`; keep legacy methods as delegation-only adapters.
- `src/athena/research/supervisor/plan_lifecycle.py`, `plan_runtime.py`, `settlement.py`, `search_loop.py`, `phases.py`, and `run_state.py`: route state/tree/transient changes through the coordinator and remove independent save paths.
- `src/athena/research/runtime/bootstrap.py`, `services.py`, `facade.py`, `control.py`, `settings.py`, `events.py`, and `event_projection.py`: acquire/release owner lock, recover before load, wire control services, apply sidecar override, re-arm Search, expose interaction mode, correlate errors, and transact runtime settings/checkpoints.
- `src/athena/research/runtime/clarification.py`, `src/athena/research/clarification/confirmation.py`, `src/athena/research/prepare/data.py`, `src/athena/research/prepare/eda.py`, `src/athena/research/runtime/survey.py`, `src/athena/research/turns/general.py`, and `src/athena/research/turns/ideator.py`: replace direct state writes with named internal coordinator checkpoints.
- `src/athena/research/exp_docs.py` and `src/athena/research/report.py`: atomic derived-report writes and explicit exploratory-validation labelling.
- `src/athena/gui/service.py`, `src/athena/gui/experiments.py`, `src/gui_gateway/handler.py`, `src/gui_gateway/transport.py`, and `src/gui_gateway/__main__.py`: transact GUI tree edits, pass transport request identity, provide a human-only validation permit, avoid same-session double construction, and initialize file error logging.
- `src/athena/cli.py` and `src/athena_tui/entrypoint.py`: rely on runtime-owned locking and initialize the same process error log.

### New focused tests and fixtures

- `test/unit/core/test_persistence.py`
- `test/unit/research/control/__init__.py`
- `test/unit/research/control/test_contracts.py`
- `test/unit/research/control/test_context.py`
- `test/unit/research/control/test_diagnostics.py`
- `test/unit/research/control/test_journal.py`
- `test/unit/research/control/test_coordinator.py`
- `test/unit/research/control/test_resume.py`
- `test/integration/research/test_transactional_control.py`
- `test/integration/research/test_completed_search_resume.py`
- `test/integration/research/test_session_owner.py`
- `test/fixtures/supervisor_tool_schemas.json`

### Existing tests updated in place

- `test/unit/core/test_project_lock.py`
- `test/unit/agent/test_agent_runtime.py`
- `test/unit/agent/test_supervisor_agent.py`
- `test/unit/kaggle/test_supervisor_gate.py`
- `test/unit/research/supervisor/test_state.py`
- `test/unit/research/supervisor/test_scheduler.py`
- `test/unit/research/supervisor/test_supervisor.py`
- `test/unit/research/test_runtime_settings.py`
- `test/unit/research/test_breakpoint_resume.py`
- `test/integration/research/test_rolling_search.py`
- `test/integration/research/test_search_recovery.py`
- `tests/test_gui_gateway_handler.py`
- `tests/test_gui_gateway_transport.py`
- `tests/test_gui_gateway_main.py`
- `test/architecture/test_supervisor_surface.py`

### Documentation and closeout

- `docs/research_core_mechanisms_ch.md`: control transaction, recovery, resume, validation policy, log locations, and downgrade procedure.
- `docs/athena-gui-design.md`: `interaction_mode`, request identity, `/search +N`, and human validation origin.
- `codex_docs/2026-09-04-supervisor-transactional-search-resume-backend-completion-report.md`: exact evidence and limitations.
- `codex_docs/CURRENT.md`: move from backend to the queued frontend plan after backend acceptance.

## Execution preflight

- [ ] Record `git status --short --branch`, `git diff --cached --name-status`, `git worktree list --porcelain`, and the current commit. Create `feat/supervisor-transactional-search-resume-backend` in a new worktree; do not use the residual unregistered `.worktrees/output-main-integration` directory.
- [ ] Inspect only the linked parallel plan's unchecked section and run `git status --short` in every registered worktree. If an uncommitted edit overlaps `bootstrap.py`, `facade.py`, or `handler.py`, record the owner and stop before editing that file.
- [ ] Run the pre-change baseline with cache disabled: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/core/test_project_lock.py test/unit/agent/test_agent_runtime.py test/unit/agent/test_supervisor_agent.py test/unit/kaggle/test_supervisor_gate.py test/unit/research/supervisor test/unit/research/test_runtime_settings.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_main.py`. Record exact counts and any unrelated failure before Task 1.

## Acceptance ownership

| Design criterion | Owning task(s) |
| --- | --- |
| 1. Five normal tools; Kaggle is the only optional sixth | 3, 8, 14 |
| 2. Bounded injected context plus one paginated reader | 4, 8 |
| 3. No arbitrary state or `.athena` writes | 3, 5, 9, 14 |
| 4. Strict, versioned, serialized, recoverable mutations | 1, 2, 5, 6, 10, 13 |
| 5. Honest rollback and retained recovery journal | 5, 6, 13 |
| 6. Permanent resume idempotency after ledger compaction | 5, 6, 7 |
| 7. COMPLETED +20 checkpoint resume with durable manual validation | 7, 8, 11, 12 |
| 8. Allowance ends at SEARCH/WAITING without auto-validation | 7, 10, 11, 12 |
| 9. Opening a completed session is read-only | 12 |
| 10. Empty groups hidden without mutation | Dependent frontend plan, Tasks 2-3 |
| 11. Full backend diagnostics and concise UI errors | 5, 6, 12, 14 |
| 12. Consumed final evaluator cannot leak into Search | 4, 7, 11 |
| 13. No Plan overshoot or Windows/WSL second writer | 2, 10, 13 |
| 14. Only gateway-originated human validation can bypass manual policy | 3, 11 |
| 15. Schema budget and one domain implementation path | 3, 8, 14 |

---

### Task 1: Collision-safe atomic persistence

**Files:**
- Create: `test/unit/core/test_persistence.py`
- Modify: `src/athena/core/persistence.py`
- Modify: `test/unit/research/supervisor/test_state.py`

**Interfaces:**
- Produces: `atomic_write_bytes(path: str | Path, payload: bytes) -> Path`.
- Produces: `atomic_write_text(path: str | Path, payload: str) -> Path`.
- Preserves: `atomic_write_json(path: str | Path, payload: Any) -> Path`.
- Internal constants: `_REPLACE_DELAYS = (0.0, 0.02, 0.05, 0.1, 0.2)` and retry classification limited to `PermissionError`, Windows error 5, Windows error 32, or `errno.EACCES`/`errno.EBUSY`.

- [ ] **Step 1: Write failing unique-temp, retry, fsync, and cleanup tests**

```python
def test_atomic_writers_never_share_the_fixed_dot_tmp_name(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    seen: list[str] = []
    real_replace = os.replace

    def capture(source, destination):
        seen.append(Path(source).name)
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", capture)
    atomic_write_json(target, {"writer": 1})
    atomic_write_json(target, {"writer": 2})
    assert len(set(seen)) == 2
    assert all(name.startswith(".state.json-") for name in seen)
    assert not list(tmp_path.glob(".state.json-*.tmp"))


def test_atomic_replace_retries_access_denied_without_losing_previous_bytes(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_text('{"old":true}\n', encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def denied_once(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(errno.EACCES, "access denied", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", denied_once)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    atomic_write_json(target, {"new": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert calls == 2
```

- [ ] **Step 2: Run the tests and verify the current fixed `state.json.tmp` implementation fails**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/core/test_persistence.py test/unit/research/supervisor/test_state.py`

Expected: the unique-name assertion fails before implementation; existing state tests remain green.

- [ ] **Step 3: Implement one byte primitive and make text/JSON thin encoders**

```python
def atomic_write_bytes(path: str | Path, payload: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, target)
        _fsync_parent(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def atomic_write_text(path: str | Path, payload: str) -> Path:
    return atomic_write_bytes(path, payload.encode("utf-8"))


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, encoded)
```

Implement `_replace_with_retry()` so only the listed access/sharing errors advance through `_REPLACE_DELAYS`; all other exceptions are raised immediately. `_fsync_parent()` opens and fsyncs the directory only where the platform supports it.

- [ ] **Step 4: Re-run focused persistence/state tests and a concurrent writer stress case**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/core/test_persistence.py test/unit/research/supervisor/test_state.py`

Expected: all pass; the target always parses as one complete JSON object and no owned temp file remains.

- [ ] **Step 5: Commit only the persistence slice**

```powershell
git add src/athena/core/persistence.py test/unit/core/test_persistence.py test/unit/research/supervisor/test_state.py
git commit -m "fix: harden atomic state replacement"
```

---

### Task 2: Runtime-owned per-session OS lock

**Files:**
- Modify: `src/athena/core/project_lock.py`
- Create: `src/athena/core/windows_lock_broker.py`
- Modify: `test/unit/core/test_project_lock.py`
- Modify: `src/athena/research/runtime/services.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `src/athena/cli.py`
- Modify: `src/gui_gateway/handler.py`
- Modify: `tests/test_gui_gateway_handler.py`
- Create: `test/integration/research/test_session_owner.py`

**Interfaces:**
- Produces: `SessionOwnerLock.acquire(state_root: Path, session_id: str) -> SessionOwnerLock` and idempotent `close() -> None`.
- Produces: internal `acquire_windows_share_lease(path: Path) -> WindowsShareLease`; the lease exposes `assert_held() -> None` and idempotent `close() -> None`.
- Produces: typed `SessionBusyError(code="session_busy")`, `CrossOsLockUnavailable(code="cross_os_lock_unavailable")`, and `SessionOwnershipLost(code="session_ownership_lost")` startup/write-boundary errors.
- On native Windows, `WindowsShareLease` holds one `CreateFileW(..., dwShareMode=0, OPEN_ALWAYS)` handle. On WSL DrvFS, it holds a `powershell.exe` child whose `FileStream` uses `FileShare.None` and whose stdin is a parent-lifetime pipe.
- On native non-WSL Linux filesystems, `SessionOwnerLock` retains one `flock`; WSL shared-path detection may not fall back to it.
- Preserves: `project_lock(athena_dir)` as a wrapper around `SessionOwnerLock` for external compatibility.
- `ResearchInfrastructure.owner` holds the acquired lock for `ResearchRuntime` lifetime.
- `ResearchRuntime.aclose()` releases it in `finally`; construction releases it if any later composition step raises.

- [ ] **Step 1: Write failing lifetime, competing-process, crash-release, and GUI same-session tests**

```python
def test_second_runtime_for_one_session_is_rejected_before_state_load(tmp_path):
    first = ResearchRuntime(project_root=tmp_path)
    with pytest.raises(SessionBusyError) as caught:
        ResearchRuntime(project_root=tmp_path)
    assert caught.value.code == "session_busy"
    assert not hasattr(caught.value, "state")
    asyncio.run(first.aclose())


@pytest.mark.asyncio
async def test_switching_to_the_already_active_session_does_not_rebuild(handler):
    original = handler.runtime
    result = await handler.session_switch("default")
    assert handler.runtime is original
    assert result["session_id"] == "default"
```

Extend the existing subprocess test to terminate a holder without graceful cleanup and assert a new process acquires the owner handle. Use distinct named-session roots to prove two sessions in one project do not block each other.

Add broker-specific unit cases with mocked process/Win32 boundaries:

```python
def test_wsl_shared_path_fails_closed_when_windows_broker_is_unavailable(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(lock_broker, "is_wsl", lambda: True)
    monkeypatch.setattr(lock_broker, "is_drvfs_path", lambda _: True)
    monkeypatch.setattr(lock_broker, "find_powershell", lambda: None)
    with pytest.raises(CrossOsLockUnavailable) as caught:
        SessionOwnerLock.acquire(tmp_path, "default")
    assert caught.value.code == "cross_os_lock_unavailable"


def test_dead_broker_revokes_write_authority(lock_harness):
    lease = lock_harness.acquire_wsl_broker()
    lock_harness.exit_broker(lease, returncode=9)
    with pytest.raises(SessionOwnershipLost):
        lease.assert_held()
```

- [ ] **Step 2: Run lock tests and confirm GUI/runtime construction currently permits a second writer**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/core/test_project_lock.py test/integration/research/test_session_owner.py tests/test_gui_gateway_handler.py`

Expected: new lifetime/runtime assertions fail before the central ownership change.

- [ ] **Step 3: Convert the context-manager implementation into a lifetime object and acquire before load**

```python
class SessionBusyError(RuntimeError):
    code = "session_busy"
    retryable = True


class SessionOwnerLock:
    def __init__(self, lease: OwnerLease, lock_path: Path, session_id: str):
        self._lease = lease
        self.lock_path = lock_path
        self.session_id = session_id

    @classmethod
    def acquire(cls, state_root: Path, session_id: str) -> "SessionOwnerLock":
        lock_path = state_root / LOCK_NAME
        lease = _acquire_owner_lease(lock_path)
        return cls(lease=lease, lock_path=lock_path, session_id=session_id)

    def assert_held(self) -> None:
        if self._lease is None:
            raise SessionOwnershipLost("session owner lock is closed")
        self._lease.assert_held()

    def close(self) -> None:
        if self._lease is None:
            return
        self._lease.close()
        self._lease = None


def _acquire_owner_lease(lock_path: Path) -> OwnerLease:
    if os.name == "nt" or is_wsl_drvfs(lock_path):
        return acquire_windows_share_lease(lock_path)
    return acquire_flock_lease(lock_path)
```

Implement `windows_lock_broker.py` as one internal platform adapter. Native Windows
uses `ctypes` to call `CreateFileW` with read/write access, `dwShareMode=0`, and
`OPEN_ALWAYS`; it closes that kernel handle on release. Under WSL, accept only a
canonical `/mnt/<drive>/...` DrvFS path, translate it to a drive-letter path, and
launch `powershell.exe -NoProfile -NonInteractive -EncodedCommand <internal script>`.
The internal script opens a `System.IO.FileStream` with `FileShare.None`, prints one
machine-readable `READY` line, then blocks on stdin. EOF or a release line disposes
the stream. The Python parent owns stdin, waits a bounded five seconds for the
handshake, and treats early exit, malformed output, or missing interop as
`CrossOsLockUnavailable`. A broker health check runs before every coordinator write;
if the child has exited, it raises `SessionOwnershipLost` before persistence. Never
put the script, timeout, executable path, or platform choice in a public constructor
or model schema.

Use this fixed broker protocol; encode the PowerShell source as UTF-16LE before
passing it to `-EncodedCommand`, and embed the canonical Windows path as a safely
quoted PowerShell literal:

```python
_BROKER_SOURCE = r"""
$ErrorActionPreference = 'Stop'
try {
  $stream = [System.IO.FileStream]::new(
    __LOCK_PATH__,
    [System.IO.FileMode]::OpenOrCreate,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None
  )
  [Console]::Out.WriteLine('{"status":"ready"}')
  [Console]::Out.Flush()
  [Console]::In.ReadLine() | Out-Null
  $stream.Dispose()
  exit 0
} catch [System.IO.IOException] {
  [Console]::Out.WriteLine('{"status":"busy"}')
  exit 73
} catch {
  [Console]::Error.WriteLine($_.Exception.ToString())
  exit 74
}
"""


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def encoded_broker_command(lock_path: str) -> str:
    source = _BROKER_SOURCE.replace(
        "__LOCK_PATH__", powershell_literal(lock_path)
    )
    return base64.b64encode(source.encode("utf-16le")).decode("ascii")
```

Exit `73` maps to `SessionBusyError`; exit `74`, timeout, absent executable, an
unsupported WSL path, or malformed handshake maps to `CrossOsLockUnavailable` and
includes captured stderr only in the backend log. `close()` writes `release\n`,
closes all three pipes, waits up to five seconds, then terminates only its owned
broker process. `assert_held()` maps any unexpected child exit to
`SessionOwnershipLost`.

In `ResearchRuntime.__init__`, resolve paths, acquire `SessionOwnerLock`, then enter journal recovery and state/tree loading. Remove the CLI's outer `project_lock()` so there is one owner path. Make `GuiRequestHandler.session_switch()` return replay/state for the current session without constructing a second runtime.

- [ ] **Step 4: Verify lock release across success, construction failure, `aclose`, and process death**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/core/test_project_lock.py test/integration/research/test_session_owner.py tests/test_gui_gateway_handler.py test/unit/research/test_breakpoint_resume.py`

Expected: exactly one writer per session, two different sessions may coexist, all released locks are reacquirable, and a missing/dead WSL broker removes write authority rather than falling back.

- [ ] **Step 5: Commit the ownership boundary**

```powershell
git add src/athena/core/project_lock.py src/athena/core/windows_lock_broker.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py src/athena/cli.py src/gui_gateway/handler.py test/unit/core/test_project_lock.py test/integration/research/test_session_owner.py tests/test_gui_gateway_handler.py
git commit -m "feat: enforce one runtime owner per session"
```

---

### Task 3: Strict consolidated control contracts and API budget

**Files:**
- Create: `src/athena/research/control/__init__.py`
- Create: `src/athena/research/control/contracts.py`
- Create: `test/unit/research/control/__init__.py`
- Create: `test/unit/research/control/test_contracts.py`
- Create: `test/fixtures/supervisor_tool_schemas.json`
- Modify: `test/architecture/test_supervisor_surface.py`

**Interfaces:**
- Produces strict action models: `ResumeSearch`, `ConfigureSearch`, `UpdatePlanBudget`, `SetPhase`, `SetManualMode`, `RecordGuidance`, `ConfigureKaggle`, `ProposeHypothesis`, and `SelectHypothesis`.
- Produces envelopes: `UpdateResearchRequest(context_version, action)`, `ManageHypothesesRequest(context_version, action)`, and `InspectResearchRequest(section, cursor=None, page_size=20)`.
- Produces: `ControlResponse`, `ControlProblem`, `ControlWarning`, `ControlOrigin`, `allowed_update_actions(phase, status, manual_validation)`, and `project_action_schema(envelope, allowed_models)`.
- `ControlOrigin` is internal Python data (`MODEL`, `GATEWAY`, `RUNTIME`) and never part of JSON schema.

- [ ] **Step 1: Write failing strict-model, top-level-schema, phase-projection, and snapshot tests**

```python
def test_mutation_envelopes_have_only_version_and_action_at_top_level():
    for model in (UpdateResearchRequest, ManageHypothesesRequest):
        schema = model.model_json_schema()
        assert set(schema["properties"]) == {"context_version", "action"}
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("bad", [0, -1, True, "20", 1_000_001])
def test_resume_search_rejects_non_strict_or_out_of_range_additions(bad):
    with pytest.raises(ValidationError):
        UpdateResearchRequest.model_validate(
            {
                "context_version": "sha256:current",
                "action": {"kind": "resume_search", "additional_attempts": bad},
            }
        )


def test_completed_projection_exposes_resume_but_not_validate():
    schema = update_schema_for("COMPLETED", "COMPLETED", manual_validation=True)
    kinds = action_kinds(schema)
    assert "resume_search" in kinds
    assert "set_phase" not in kinds
```

Snapshot both normal and Kaggle tool schemas in canonical sorted JSON. Assert no property named `session_id`, `operation_id`, `journal`, `path`, `retry`, `runtime`, `rollback`, `origin`, or `confirmation_token` appears.

- [ ] **Step 2: Run contract tests and verify the package/schemas do not exist**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_contracts.py test/architecture/test_supervisor_surface.py`

Expected: import/contract failures before implementation.

- [ ] **Step 3: Implement strict action unions and one schema projector**

```python
class ResumeSearch(StrictModel):
    kind: Literal["resume_search"] = "resume_search"
    additional_attempts: int = Field(strict=True, ge=1, le=MAX_SEARCH_ATTEMPTS)


UpdateAction = Annotated[
    ResumeSearch
    | ConfigureSearch
    | UpdatePlanBudget
    | SetPhase
    | SetManualMode
    | RecordGuidance
    | ConfigureKaggle,
    Field(discriminator="kind"),
]


class UpdateResearchRequest(StrictModel):
    context_version: str = Field(min_length=8, max_length=96)
    action: UpdateAction
```

Use `TypeAdapter(Annotated[variant_union, Field(discriminator="kind")])` to create the `action` schema for only the allowed models, then wrap it in the fixed two-property envelope. Add model validators for non-empty `configure_search`, mutually exclusive Plan budgets, and `search_limit >= attempts` as a service precondition rather than a schema parameter.

- [ ] **Step 4: Re-run contract snapshots and architecture assertions**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_contracts.py test/architecture/test_supervisor_surface.py`

Expected: strict rejection and canonical snapshots pass; no production tool registration changes yet.

- [ ] **Step 5: Commit the contract boundary**

```powershell
git add src/athena/research/control test/unit/research/control test/fixtures/supervisor_tool_schemas.json test/architecture/test_supervisor_surface.py
git commit -m "feat: define strict supervisor control contracts"
```

---

### Task 4: Bounded versioned ResearchContext and inspection

**Files:**
- Create: `src/athena/research/control/context.py`
- Create: `test/unit/research/control/test_context.py`
- Modify: `src/athena/agents/base_runner.py`
- Modify: `test/unit/agent/test_agent_runtime.py`
- Modify: `src/athena/research/runtime/event_projection.py`
- Modify: `src/athena/research/supervisor/events.py`

**Interfaces:**
- Produces: `ResearchContextProjector.project() -> ResearchContext` and `prompt_block() -> str`.
- Produces: `ResearchInspector.inspect(request: InspectResearchRequest) -> dict[str, object]`.
- Produces: `derive_interaction_mode(state: ResearchState, tree: ResearchTree) -> Literal["clarification", "supervisor"]`.
- Stable cursor payload is `{section, offset, context_version}` encoded as URL-safe base64; changed context returns `stale_cursor` without guessing an offset.
- `BaseAgentRunner(..., turn_context: Callable[[], str] | None = None)` is the only generic hook.

- [ ] **Step 1: Write failing digest, bound, allowlist, cursor, and injection tests**

```python
def test_compact_context_is_bounded_and_contains_no_secret_or_final_score(control_harness):
    control_harness.state.validation = {
        "final_test_score": 0.991,
        "api_key": "sk-secret-value",
    }
    projected = control_harness.context.project()
    encoded = projected.model_dump_json().encode("utf-8")
    assert len(encoded) <= 8192
    assert b"0.991" not in encoded
    assert b"sk-secret-value" not in encoded
    assert projected.sota.metric == 0.83


def test_cursor_is_stable_only_for_its_context_version(control_harness):
    first = control_harness.inspector.inspect(
        InspectResearchRequest(section="hypotheses", page_size=2)
    )
    control_harness.add_pending_hypothesis("new")
    second = control_harness.inspector.inspect(
        InspectResearchRequest(
            section="hypotheses", page_size=2, cursor=first["next_cursor"]
        )
    )
    assert second["ok"] is False
    assert second["error"]["code"] == "stale_cursor"
```

Add a runner test whose fake provider captures the first user prompt and asserts exactly one `[ATHENA RESEARCH CONTEXT v1]` block precedes the human text on each turn without replacing the ordinary Supervisor system prompt.

- [ ] **Step 2: Run focused tests and observe missing context/interaction fields**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_context.py test/unit/agent/test_agent_runtime.py test/unit/research/supervisor/test_events.py`

Expected: new tests fail before the context owner exists.

- [ ] **Step 3: Implement the allowlisted projection and hidden runner hook**

```python
def derive_interaction_mode(state: ResearchState, tree: ResearchTree) -> InteractionMode:
    has_checkpoint = any(
        (
            tree.best_experiment_id() is not None,
            bool(tree.experiments(kind="search")),
            bool(state.plans),
            state.task_understanding is not None,
            bool(state.task_text),
        )
    )
    return "supervisor" if has_checkpoint else "clarification"


def context_digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
```

Use only explicit fields from the approved context shape, at most three bounded hypothesis previews, and the frozen SEARCH evaluator metric from the SOTA experiment. `ResearchInspector` exposes `state`, `plans`, `hypotheses`, and sanitized `history`; history carries refs and evaluator status but never the raw final score.

In `BaseAgentRunner.run`, set `input_text` to `turn_context() + "\n\n[HUMAN MESSAGE]\n" + trigger.content` only when the hook returns a block. Do not append a second `SystemPromptPart`, because `Agent._has_system()` treats any system part as the installed base prompt.

- [ ] **Step 4: Verify deterministic hashes, pagination, prompt ordering, and state event projection**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_context.py test/unit/agent/test_agent_runtime.py test/unit/research/supervisor/test_events.py`

Expected: all pass; changing state/tree/sidecar policy changes `context_version`, while subscriber count and free text do not.

- [ ] **Step 5: Commit context projection separately**

```powershell
git add src/athena/research/control/context.py src/athena/agents/base_runner.py src/athena/research/runtime/event_projection.py src/athena/research/supervisor/events.py test/unit/research/control/test_context.py test/unit/agent/test_agent_runtime.py test/unit/research/supervisor/test_events.py
git commit -m "feat: project bounded supervisor research context"
```

---

### Task 5: Structured failover logging and durable control journal

**Files:**
- Create: `src/athena/research/control/diagnostics.py`
- Create: `src/athena/research/control/journal.py`
- Create: `test/unit/research/control/test_diagnostics.py`
- Create: `test/unit/research/control/test_journal.py`
- Modify: `src/athena/research/config.py`
- Modify: `src/athena/research/supervisor/state.py`
- Modify: `test/unit/research/supervisor/test_state.py`
- Modify: `src/gui_gateway/__main__.py`
- Modify: `src/athena/cli.py`
- Modify: `src/athena_tui/entrypoint.py`

**Interfaces:**
- `ControlPaths` derives `control/active.json`, `control/completed.json`, `control/resumes/`, `runtime-capabilities.json`, session `logs/backend-errors.jsonl`, and process fallback `~/.athena/logs/backend-errors.jsonl`.
- `ControlDiagnostics.record_failure(record, exc, rollback_exc=None) -> tuple[ControlWarning, ...]` tries session, process, stderr, then retains the sanitized record in a 100-entry ring.
- `ProcessErrorLogger.install(path) -> ProcessErrorLogger` idempotently installs one 5 MiB/three-backup redacting JSONL handler on the root logger plus `sys.excepthook`, `threading.excepthook`, and an asyncio loop exception handler; `close()` restores the prior hooks.
- `ControlJournalStore.capture(names)`, `prepare(record)`, `transition(phase, result=None, error=None)`, `install(images, *, assert_owner)`, `restore(images, *, assert_owner)`, `lookup(operation_id, arguments_digest)`, `commit_resume_receipt(record)`, and `recover_documents(*, assert_owner)` own all control files. `assert_owner: Callable[[], None]` runs before each canonical replacement.
- Sidecar-only `ResearchState` fields: `auto_validate_override`, `validation_policy`, `final_evaluator_generation`, `final_evaluator_status`, and `search_epoch`.

- [ ] **Step 1: Write failing journal phase, exact-byte restore, immutable receipt, sidecar, and log fallback tests**

```python
def test_prepared_recovery_restores_exact_before_images(tmp_path):
    store = make_control_store(tmp_path)
    before = store.capture({"state", "resume", "tree"})
    record = prepared_record(before=before, after=candidate_images())
    store.prepare(record)
    store.install(record.after, assert_owner=lambda: None)
    directive = store.recover_documents(assert_owner=lambda: None)
    assert directive.kind == "rolled_back"
    assert store.capture({"state", "resume", "tree"}) == before
    assert store.load_active().phase == "ROLLED_BACK"


def test_resume_receipt_survives_completion_ledger_compaction(tmp_path):
    store = make_control_store(tmp_path, ledger_limit=2)
    store.commit_resume_receipt(resume_receipt("resume-op"))
    for index in range(5):
        store.append_completed(completed_record(f"ordinary-{index}"))
    assert store.lookup_resume("resume-op").operation_id == "resume-op"


def test_logging_fallback_never_masks_the_primary_exception(tmp_path, monkeypatch):
    diagnostics = diagnostics_with_paths(tmp_path)
    monkeypatch.setattr(diagnostics, "_append_session", deny_access)
    warnings = diagnostics.record_failure(failure_record(), RuntimeError("boom"))
    assert warnings == ()
    assert "RuntimeError: boom" in diagnostics.process_path.read_text("utf-8")


def test_process_logger_records_unhandled_async_exception_with_traceback(tmp_path):
    installed = ProcessErrorLogger.install(tmp_path / "backend-errors.jsonl")
    error = RuntimeError("background boom")
    installed.handle_asyncio_exception(
        asyncio.get_event_loop(),
        {"message": "task failed", "exception": error},
    )
    record = json.loads((tmp_path / "backend-errors.jsonl").read_text("utf-8"))
    assert record["exception_type"] == "RuntimeError"
    assert "RuntimeError: background boom" in record["traceback"]
    installed.close()


def test_process_logger_is_idempotent_and_redacts_secrets(tmp_path):
    first = ProcessErrorLogger.install(tmp_path / "backend-errors.jsonl")
    second = ProcessErrorLogger.install(tmp_path / "backend-errors.jsonl")
    try:
        raise RuntimeError("failure")
    except RuntimeError:
        logging.getLogger("athena.test").exception("api_key=do-not-write")
    text = (tmp_path / "backend-errors.jsonl").read_text("utf-8")
    assert first is second
    assert text.count('"message"') == 1
    assert "do-not-write" not in text
    assert "[REDACTED]" in text
```

Also cover all sinks failing (`logging_degraded`), secret redaction, full chained traceback, bounded rotation, corrupt journal, symlink/path escape rejection, and resume-sidecar status values `absent`, `valid`, `corrupt`, and `digest_mismatch`.

- [ ] **Step 2: Run the new store/log/state tests and verify missing behavior**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_diagnostics.py test/unit/research/control/test_journal.py test/unit/research/supervisor/test_state.py tests/test_gui_gateway_main.py test/unit/athena_tui/test_entrypoint.py`

Expected: journal/log imports and new sidecar round trips fail before implementation.

- [ ] **Step 3: Implement allowlisted document images, phase records, and failover logs**

```python
class DocumentImage(StrictModel):
    name: Literal["state", "resume", "tree", "capabilities", "final_report", "optimization"]
    present: bool
    content_b64: str | None = None
    sha256: str | None = None


class ControlJournal(StrictModel):
    schema_version: Literal[1] = 1
    session_id: str
    operation_id: str
    arguments_digest: str
    action: str
    phase: JournalPhase
    before_version: str
    after_version: str
    before: tuple[DocumentImage, ...]
    after: tuple[DocumentImage, ...]
    runtime_before: RuntimeSnapshot
    resume_audit: ResumeAudit | None = None
    result: dict[str, object] | None = None
    error: dict[str, object] | None = None
```

Resolve document names through a fixed mapping created from `ResearchPaths`; never accept a path string from an action. Resume receipt filenames are `sha256(operation_id).hexdigest() + ".json"`. Store the original operation ID inside the strict receipt and use `O_EXCL` semantics so a conflicting pre-existing receipt is an `operation_mismatch`.

Extend `ResearchState.save/load` with the sidecar fields while keeping `state.json` unchanged. Add `load_checked()` so bootstrap can distinguish invalid sidecars; retain tolerant `load()` for legacy read-only callers. Implement `ProcessErrorLogger` with `logging.handlers.RotatingFileHandler(maxBytes=5 * 1024 * 1024, backupCount=3)` and a formatter that runs the existing `athena.research.supervisor.events.redact()` over message and traceback text before JSON serialization. Install it before runtime construction in GUI/CLI/TUI entrypoints, bind the active event loop once available, and configure the fixed process fallback path without new command-line options. Existing caught boundaries continue to call `logger.exception`; uncaught main-thread, worker-thread, and background-task exceptions flow through the installed hooks exactly once.

- [ ] **Step 4: Verify every journal phase, failover combination, and legacy state shape**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_diagnostics.py test/unit/research/control/test_journal.py test/unit/research/supervisor/test_state.py tests/test_gui_gateway_main.py test/unit/athena_tui/test_entrypoint.py`

Expected: all pass; existing core-state fixture JSON remains byte-schema compatible and no fixed `.tmp` files appear.

- [ ] **Step 5: Commit journal and observability foundations**

```powershell
git add src/athena/research/control/diagnostics.py src/athena/research/control/journal.py src/athena/research/config.py src/athena/research/supervisor/state.py src/gui_gateway/__main__.py src/athena/cli.py src/athena_tui/entrypoint.py test/unit/research/control/test_diagnostics.py test/unit/research/control/test_journal.py test/unit/research/supervisor/test_state.py tests/test_gui_gateway_main.py test/unit/athena_tui/test_entrypoint.py
git commit -m "feat: add recoverable control journal and error logs"
```

---

### Task 6: Candidate-based mutation coordinator and crash recovery

**Files:**
- Create: `src/athena/research/control/coordinator.py`
- Create: `test/unit/research/control/test_coordinator.py`
- Modify: `src/athena/core/research_tree.py`
- Modify: `tests/test_research_tree_v2.py`
- Modify: `src/athena/research/runtime/services.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/supervisor/deps.py`

**Interfaces:**
- Produces: `MutationCandidate(state, tree, runtime)` and `MutationEffect(result, documents, start=None, resume_receipt=None)`.
- Produces: `SupervisorMutationCoordinator.execute(request, operation_id, action, build_effect) -> ControlResponse`.
- Produces: `SupervisorMutationCoordinator.checkpoint(action, mutate, *, tree=False) -> T` for framework-owned, non-model mutations.
- Produces: `SupervisorMutationCoordinator.recover_before_load() -> RecoveryDirective` and `finish_recovery(start_effect) -> None`.
- Consumes: Task 2's `SessionOwnerLock.assert_held() -> None`; every model and framework mutation verifies ownership before journal creation and immediately before each canonical document install.
- `ResearchTree.replace_from(other) -> None` preserves the target object identity while deep-copying validated graph data.

- [ ] **Step 1: Write failing success, stale, replay, mismatch, rollback, cancellation, and serialization tests**

```python
@pytest.mark.asyncio
async def test_exact_replay_returns_recorded_result_before_stale_check(coordinator):
    request = resume_request(coordinator.context_version, 20)
    first = await coordinator.execute(request, "call-1", "resume_search", resume_effect)
    coordinator.state.search_limit += 1
    second = await coordinator.execute(request, "call-1", "resume_search", resume_effect)
    assert second == first
    assert coordinator.resume_apply_count == 1


@pytest.mark.asyncio
async def test_reused_operation_with_different_arguments_is_rejected(coordinator):
    first = resume_request(coordinator.context_version, 20)
    await coordinator.execute(first, "call-1", "resume_search", resume_effect)
    second = resume_request(coordinator.context_version, 21)
    result = await coordinator.execute(second, "call-1", "resume_search", resume_effect)
    assert result.error.code == "operation_mismatch"


@pytest.mark.asyncio
async def test_side_effect_start_failure_restores_disk_memory_and_identity(coordinator):
    state_id, tree_id = id(coordinator.state), id(coordinator.tree)
    before = coordinator.snapshot()
    result = await coordinator.execute(
        resume_request(coordinator.context_version, 20),
        "call-2",
        "resume_search",
        effect_with_failing_start,
    )
    assert result.error.code == "side_effect_start_failed"
    assert coordinator.snapshot() == before
    assert (id(coordinator.state), id(coordinator.tree)) == (state_id, tree_id)


@pytest.mark.asyncio
async def test_lost_session_ownership_prevents_journal_and_state_writes(coordinator):
    coordinator.owner.simulate_loss()
    before = coordinator.snapshot()
    result = await coordinator.execute(
        resume_request(coordinator.context_version, 20),
        "call-lost",
        "resume_search",
        resume_effect,
    )
    assert result.error.code == "session_ownership_lost"
    assert coordinator.snapshot() == before
    assert coordinator.journal_store.active() is None
```

Parameterize failure injection at preflight, PREPARED, each document install, STATE_COMMITTED, side-effect start, and rollback install. Cancel at every awaited boundary. Launch two mutation tasks behind a barrier and assert one commits while the other returns `stale_context`.

- [ ] **Step 2: Run coordinator/research-tree tests and observe missing transaction semantics**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_coordinator.py tests/test_research_tree_v2.py`

Expected: coordinator imports fail before implementation.

- [ ] **Step 3: Implement the journal phase machine without mutating shared objects before commit**

```python
async with self._lock:
    self._owner.assert_held()
    replay = self._journal.lookup(operation_id, arguments_digest)
    if replay is not None:
        return replay
    if request.context_version != self._context.version():
        return self._failure("stale_context", operation_id)
    before = self._snapshot_shared()
    candidate = before.to_candidate()
    effect = await build_effect(candidate)
    after = self._documents.encode(candidate, effect.documents)
    record = self._journal.prepare(self._record(before, after, effect))
    try:
        self._journal.install(after, assert_owner=self._owner.assert_held)
        self._commit_shared(candidate)
        self._journal.transition("STATE_COMMITTED")
        if effect.start is not None:
            await effect.start()
        if effect.resume_receipt is not None:
            self._journal.commit_resume_receipt(effect.resume_receipt)
        response = self._success(operation_id, effect.result)
        self._journal.transition("COMPLETED", result=response.model_dump(mode="json"))
        return response
    except BaseException as error:
        return await self._rollback(record, before, error)
```

Handle `CancelledError` explicitly, mark `ROLLBACK_PENDING` before compensation, cancel a newly created scheduler task, and shield the bounded rollback long enough to classify the journal. If restore fails, keep before-images in memory, retain `ROLLBACK_PENDING`, return `rollback_failed`, and do not publish/start Search. Publish state only after canonical commit; subscriber errors are logged and do not change the transaction result.

- [ ] **Step 4: Verify failure matrix, object identity, replay ordering, and concurrent calls**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_coordinator.py tests/test_research_tree_v2.py test/integration/research/test_search_recovery.py`

Expected: all pass; each injected failure has a classified journal and exact before/after policy.

- [ ] **Step 5: Commit the coordinator as an independently reviewed unit**

```powershell
git add src/athena/research/control/coordinator.py src/athena/core/research_tree.py src/athena/research/runtime/services.py src/athena/research/runtime/bootstrap.py src/athena/research/supervisor/deps.py test/unit/research/control/test_coordinator.py tests/test_research_tree_v2.py test/integration/research/test_search_recovery.py
git commit -m "feat: serialize research control transactions"
```

---

### Task 7: Resume policy for WAITING and normally COMPLETED sessions

**Files:**
- Create: `src/athena/research/control/resume.py`
- Create: `test/unit/research/control/test_resume.py`
- Create: `test/integration/research/test_completed_search_resume.py`
- Modify: `src/athena/research/supervisor/scheduling.py`
- Modify: `test/unit/research/supervisor/test_scheduler.py`
- Modify: `src/athena/research/exp_docs.py`

**Interfaces:**
- Produces: `prepare_resume(candidate, additional_attempts, runtime_facts) -> MutationEffect`.
- Produces: `classify_waiting_blocker(state, tree, running_ids) -> WaitingBlocker | None`.
- `attempts_at_acceptance(state, tree)` delegates to the single corrected `count_search_attempts()` owner.
- Resume result keys are exactly `attempts_before`, `additional_attempts`, `search_limit`, `phase`, `status`, `auto_validate`, `manual_blocker`, `plan_blockers`, and `context_version`.

- [ ] **Step 1: Write failing completed/WAITING transition and rejection tests**

```python
@pytest.mark.asyncio
async def test_completed_resume_adds_from_acceptance_and_archives_validation(harness):
    harness.complete_with_sota(
        terminal_search=37,
        validation={"final_test_score": 0.91, "report_ref": "artifact://final"},
    )
    result = await harness.resume(additional_attempts=20, operation_id="resume-20")
    assert result.ok is True
    assert result.result["attempts_before"] == 37
    assert result.result["search_limit"] == 57
    assert harness.state.phase == "SEARCH"
    assert harness.state.status == "RUNNING"
    assert harness.state.validation is None
    assert harness.state.auto_validate_override is False
    assert harness.state.validation_policy == "manual"
    assert harness.state.final_evaluator_status == "consumed"
    assert harness.resume_receipt("resume-20").prior_report_ref == "artifact://final"


@pytest.mark.parametrize(
    ("phase", "status", "code"),
    [("SEARCH", "FAILED", "not_resumable"), ("SEARCH", "STOPPED", "not_resumable")],
)
async def test_failed_and_stopped_runs_are_unchanged(harness, phase, status, code):
    harness.state.phase, harness.state.status = phase, status
    before = harness.snapshot()
    result = await harness.resume(additional_attempts=20, operation_id="blocked")
    assert result.error.code == code
    assert harness.snapshot() == before
```

Cover active Plans in the acceptance count, strict SOTA commit/eval, missing SEARCH evaluator, unresolved completed workers, Plan-budget exhaustion, manual mode, no viable hypotheses, cap overflow, replay after ledger compaction, and preservation of task/data/evaluator/tree/artifact refs.

- [ ] **Step 2: Run resume tests and verify COMPLETED currently has no legal transition**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_resume.py test/integration/research/test_completed_search_resume.py test/unit/research/supervisor/test_scheduler.py`

Expected: new tests fail while existing scheduler tests show current absolute-limit behavior.

- [ ] **Step 3: Implement pure preconditions and candidate mutation**

```python
def attempts_at_acceptance(state: ResearchState, tree: ResearchTree) -> int:
    terminal = sum(
        experiment.status in {ExperimentStatus.SUCCEEDED, ExperimentStatus.FAILED}
        for experiment in tree.experiments(kind="search")
    )
    active = sum(plan.kind == "SEARCH" for plan in state.plans.values())
    return terminal + active


def apply_completed_resume(candidate: MutationCandidate, additional: int) -> ResumeAudit:
    before = attempts_at_acceptance(candidate.state, candidate.tree)
    candidate.state.validation = None
    candidate.state.phase = "SEARCH"
    candidate.state.status = "RUNNING"
    candidate.state.search_limit = before + additional
    candidate.state.auto_validate_override = False
    candidate.state.validation_policy = "manual"
    candidate.state.final_evaluator_status = "consumed"
    candidate.state.search_epoch += 1
    candidate.runtime.auto_validate = False
    return ResumeAudit(attempts_before=before, additional_attempts=additional)
```

Validate the SOTA's nonblank commit, trusted evaluation, readable `evaluator_ref`, and absence of unreconciled live Plans before applying. For SEARCH/WAITING, do not change `manual_mode`; return explicit blocker fields. Generate new derived report bytes with `validation=None` inside the transaction while preserving the prior content-addressed report ref in the receipt.

- [ ] **Step 4: Verify resume state, permanent history, deterministic reports, and all rejected sources**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/control/test_resume.py test/integration/research/test_completed_search_resume.py test/unit/research/supervisor/test_scheduler.py test/unit/research/test_report.py`

Expected: all pass; no rejected action changes disk or shared memory.

- [ ] **Step 5: Commit resume semantics without exposing tools yet**

```powershell
git add src/athena/research/control/resume.py src/athena/research/supervisor/scheduling.py src/athena/research/exp_docs.py test/unit/research/control/test_resume.py test/integration/research/test_completed_search_resume.py test/unit/research/supervisor/test_scheduler.py
git commit -m "feat: define completed search resume semantics"
```

---

### Task 8: One control service and five Supervisor tools

**Files:**
- Create: `src/athena/research/control/service.py`
- Modify: `src/athena/research/control/__init__.py`
- Modify: `src/athena/agents/supervisor_agent.py`
- Modify: `src/athena/agents/prompts/supervisor_agent.md`
- Modify: `src/athena/research/supervisor/supervisor.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `test/unit/agent/test_supervisor_agent.py`
- Modify: `test/unit/kaggle/test_supervisor_gate.py`
- Modify: `test/unit/research/supervisor/test_supervisor.py`
- Modify: `test/fixtures/supervisor_tool_schemas.json`

**Interfaces:**
- `ResearchControlService.context_block()`, `inspect(request)`, `update(request, *, operation_id, origin)`, `manage(request, *, operation_id, origin)`, and `dispatch(task, *, operation_id)` are the only model-tool backends.
- `Supervisor.inspect_research`, `update_research`, `manage_hypotheses`, and `dispatch_general` delegate to that service.
- `_ValidatedTool` invokes `Callable[[BaseModel, ToolContext], Awaitable[dict[str, object]]]` so `ctx.call_id` reaches the coordinator internally.
- Legacy `configure_search`, `set_phase_decision`, `set_manual_mode`, `record_guidance`, `set_kaggle_enabled`, `propose_hypothesis`, and `select_next_hypothesis` remain Python-only adapters and create framework operation IDs.

- [ ] **Step 1: Replace old tool expectations with failing five-tool and real-call-ID tests**

```python
def test_normal_supervisor_registry_has_exactly_five_tools(actions):
    assert {spec.name for spec in supervisor_tool_registry(actions).specs} == {
        "dispatch_general",
        "inspect_research",
        "manage_hypotheses",
        "request_user_input",
        "update_research",
    }


@pytest.mark.asyncio
async def test_two_same_name_calls_in_one_turn_keep_distinct_provider_ids(tmp_path):
    provider = TwoUpdateCallsProvider(call_ids=("call-a", "call-b"))
    actions = RecordingControlActions()
    runtime = await run_supervisor(tmp_path, provider, actions)
    assert actions.operation_ids == [
        "supervisor-turn:call-a",
        "supervisor-turn:call-b",
    ]
    await runtime.aclose()
```

Assert `record_task_understanding` and all twelve other legacy names are absent, mutation specs set `concurrency_safe=False`, optional Kaggle produces six total tools, and model-facing results include committed context.

- [ ] **Step 2: Run Supervisor tests and confirm the thirteen-tool registry fails the budget**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/agent/test_supervisor_agent.py test/unit/kaggle/test_supervisor_gate.py test/unit/research/supervisor/test_supervisor.py test/unit/research/control/test_contracts.py`

Expected: new exact-name and call-ID assertions fail.

- [ ] **Step 3: Implement the service router and collapse the registry**

```python
async def update_tool(value: BaseModel, ctx: ToolContext) -> dict[str, object]:
    request = UpdateResearchRequest.model_validate(value.model_dump())
    return (
        await actions.update_research(
            request,
            operation_id=ctx.call_id,
            origin=ControlOrigin.MODEL,
        )
    ).model_dump(mode="json")
```

Construct `inspect_research`, `update_research`, `manage_hypotheses`, and `dispatch_general` explicitly, then register the existing `RequestUserInputTool`. `SupervisorToolProjector.build()` reads current phase/policy from the service and substitutes only the legal action variants into the two mutation schemas. The prompt must say the injected context is untrusted data, use `resume_search.additional_attempts` for “再 search 20 个” / “run 20 more experiments,” and never claim success without `ok=true` plus committed context.

- [ ] **Step 4: Verify schema snapshots, natural-language fake-provider choices, and legacy delegation**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/agent/test_supervisor_agent.py test/unit/kaggle/test_supervisor_gate.py test/unit/research/supervisor/test_supervisor.py test/unit/research/control/test_contracts.py`

Expected: five tools normally, six only with relevant Kaggle read capability, distinct real call IDs, and no model-visible legacy aliases.

- [ ] **Step 5: Commit the public-surface cutover**

```powershell
git add src/athena/research/control/service.py src/athena/research/control/__init__.py src/athena/agents/supervisor_agent.py src/athena/agents/prompts/supervisor_agent.md src/athena/research/supervisor/supervisor.py src/athena/research/runtime/bootstrap.py test/unit/agent/test_supervisor_agent.py test/unit/kaggle/test_supervisor_gate.py test/unit/research/supervisor/test_supervisor.py test/fixtures/supervisor_tool_schemas.json
git commit -m "feat: reduce supervisor to transactional tools"
```

---

### Task 9: Transactional hypotheses and General Agent saga checkpoint

**Files:**
- Modify: `src/athena/research/control/service.py`
- Modify: `src/athena/research/supervisor/plan_lifecycle.py`
- Modify: `src/athena/research/supervisor/supervisor.py`
- Modify: `src/athena/research/turns/general.py`
- Create: `test/integration/research/test_transactional_control.py`
- Modify: `test/integration/research/test_human_plan_boundary.py`
- Modify: `test/unit/research/supervisor/test_ideator_wiring.py`
- Modify: `test/unit/research/supervisor/test_supervisor.py`

**Interfaces:**
- `manage(propose)` builds/validates a candidate Hypothesis under the current SOTA and commits only the tree.
- `manage(select)` validates one pending Hypothesis, commits transient selection plus any WAITING-to-RUNNING state transition, then wakes Search after commit.
- `dispatch_general` performs external work with no state mutation, then calls `checkpoint_general(outcome, operation_id)` in one short transaction.
- A checkpoint failure returns `{artifact_ref, checkpoint_failed: true}` and never says the external work rolled back.

- [ ] **Step 1: Write failing tree rollback, stale selection, path-boundary, and saga tests**

```python
@pytest.mark.asyncio
async def test_hypothesis_persistence_failure_restores_tree(control_harness, monkeypatch):
    before = control_harness.tree.to_dict()
    monkeypatch.setattr(control_harness.journal, "install", fail_on_tree)
    result = await control_harness.manage(proposal_request(), "proposal-1")
    assert result.error.code == "persistence_failed"
    assert control_harness.tree.to_dict() == before


@pytest.mark.asyncio
async def test_general_checkpoint_failure_reports_nonrollbackable_artifact(control_harness):
    control_harness.general_result = {
        "artifact_ref": "artifact://general-result",
        "summary": "inspection complete",
    }
    control_harness.fail_checkpoint = True
    result = await control_harness.dispatch("inspect dataset", "general-1")
    assert result["artifact_ref"] == "artifact://general-result"
    assert result["checkpoint_failed"] is True
    assert "rolled back" not in json.dumps(result).lower()
```

Add tests for symlink/absolute/traversal artifact refs, concurrent proposal/select, General cache repair, and exact-operation replay of the short checkpoint only.

- [ ] **Step 2: Run focused tool/domain tests and observe direct tree/state saves**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_transactional_control.py test/integration/research/test_human_plan_boundary.py test/unit/research/supervisor/test_ideator_wiring.py test/unit/research/supervisor/test_supervisor.py`

Expected: new rollback/saga tests fail before routing.

- [ ] **Step 3: Move behavior into service actions and make legacy methods delegation-only**

```python
async def dispatch(self, task: str, *, operation_id: str) -> dict[str, object]:
    outcome = await self._general.run(task)
    artifact_ref = await self._artifacts.put_text(
        json.dumps(outcome.result, ensure_ascii=False)
    )
    try:
        await self._coordinator.checkpoint(
            "general_checkpoint",
            lambda candidate: checkpoint_general(candidate, task, outcome, artifact_ref),
        )
    except ControlFailure as error:
        return {
            **outcome.result,
            "artifact_ref": artifact_ref,
            "checkpoint_failed": True,
            "error": error.problem.model_dump(mode="json"),
        }
    return {**outcome.result, "artifact_ref": artifact_ref, "checkpoint_failed": False}
```

Do not hold the mutation lock while awaiting the General Agent, artifact storage, or hypothesis generation. Re-check `context_version` and ownership immediately before the short commit.

- [ ] **Step 4: Verify mutation rollback, General saga truthfulness, and existing scheduling behavior**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_transactional_control.py test/integration/research/test_human_plan_boundary.py test/unit/research/supervisor/test_ideator_wiring.py test/unit/research/supervisor/test_supervisor.py`

Expected: all pass; no model tool directly saves state/tree.

- [ ] **Step 5: Commit transactional hypotheses and saga semantics**

```powershell
git add src/athena/research/control/service.py src/athena/research/supervisor/plan_lifecycle.py src/athena/research/supervisor/supervisor.py src/athena/research/turns/general.py test/integration/research/test_transactional_control.py test/integration/research/test_human_plan_boundary.py test/unit/research/supervisor/test_ideator_wiring.py test/unit/research/supervisor/test_supervisor.py
git commit -m "refactor: transact supervisor domain actions"
```

---

### Task 10: Serialize Plan starts, settlement, phases, settings, and background checkpoints

> Required refinement: execute Tasks 1-2 from
> `docs/superpowers/plans/2026-09-05-skip-validate-engineering-simplification.md`
> as part of this task. They replace duplicated validation booleans and the
> reflective GUI runtime factory without changing the public wire contract.

**Files:**
- Modify: `src/athena/research/supervisor/plan_lifecycle.py`
- Modify: `src/athena/research/supervisor/plan_runtime.py`
- Modify: `src/athena/research/supervisor/settlement.py`
- Modify: `src/athena/research/supervisor/search_loop.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `src/athena/research/supervisor/run_state.py`
- Modify: `src/athena/research/runtime/settings.py`
- Modify: `src/athena/research/runtime/clarification.py`
- Modify: `src/athena/research/clarification/confirmation.py`
- Modify: `src/athena/research/prepare/data.py`
- Modify: `src/athena/research/prepare/eda.py`
- Modify: `src/athena/research/runtime/survey.py`
- Modify: `src/athena/research/turns/ideator.py`
- Modify: `src/athena/gui/service.py`
- Modify: `src/athena/gui/experiments.py`
- Modify: `test/integration/research/test_rolling_search.py`
- Modify: `test/integration/research/test_search_recovery.py`
- Modify: `test/unit/research/test_runtime_settings.py`
- Modify: `tests/test_gui_gateway_handler.py`
- Modify: `test/architecture/test_supervisor_surface.py`

**Interfaces:**
- Every short internal mutation calls `coordinator.checkpoint(<stable action name>, mutate, tree=<bool>)`.
- Plan preparation may await artifact/worktree creation before the lock, but `commit_plan_start()` atomically installs state+tree and re-checks attempt capacity inside the lock.
- Settlement loads evidence before the lock, then `commit_plan_settlement()` atomically updates experiment, hypothesis, SOTA, and active Plan removal.
- GUI experiment mutations become async service calls and use the same coordinator.

- [ ] **Step 1: Add failing overshoot, state/tree pair, settings rollback, and direct-write architecture tests**

```python
@pytest.mark.asyncio
async def test_two_final_slot_plan_starts_cannot_overshoot(harness):
    harness.state.search_limit = harness.attempts + 1
    first, second = await asyncio.gather(
        harness.start_plan("hyp-a"),
        harness.start_plan("hyp-b"),
        return_exceptions=True,
    )
    assert sum(isinstance(item, str) for item in (first, second)) == 1
    assert count_search_attempts(harness.state, harness.tree) == harness.state.search_limit


def test_research_writers_do_not_call_state_or_tree_save_directly():
    owners = [
        Path("src/athena/research/supervisor"),
        Path("src/athena/research/runtime/settings.py"),
        Path("src/athena/research/runtime/survey.py"),
        Path("src/athena/research/prepare/data.py"),
        Path("src/athena/research/prepare/eda.py"),
        Path("src/athena/research/turns/ideator.py"),
    ]
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for owner in owners
        for path in ([owner] if owner.is_file() else owner.glob("*.py"))
        if path.name != "state.py"
    )
    assert ".state.save(" not in text
    assert "._tree.save(" not in text
```

Inject a failure between state/tree installs during Plan start and settlement; assert exact pair rollback and preserved object identities. Add concurrent survey/settings/Search mutations and prove no lost updates.

- [ ] **Step 2: Run the internal-writer matrix and capture every current direct-save failure**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py test/unit/research/test_runtime_settings.py tests/test_gui_gateway_handler.py test/architecture/test_supervisor_surface.py`

Expected: new architecture and race tests fail until all named writers delegate.

- [ ] **Step 3: Extract preflight from commit and migrate each writer to named checkpoints**

```python
async def commit_plan_start(self, prepared: PreparedPlan) -> str:
    def mutate(candidate: MutationCandidate) -> str:
        if attempts_at_acceptance(candidate.state, candidate.tree) >= candidate.state.search_limit:
            raise ControlRejected("limit_exceeded", "SEARCH attempt limit reached")
        candidate.tree.add_experiment(prepared.experiment_id, prepared.experiment)
        candidate.tree.transition_experiment(
            prepared.experiment_id, ExperimentStatus.RUNNING
        )
        candidate.state.plans[prepared.plan_id] = prepared.plan_state
        return prepared.plan_id

    return await self._coordinator.checkpoint("plan_start", mutate, tree=True)
```

Use corresponding named mutations `plan_turn_checkpoint`, `plan_settlement`, `phase_transition`, `validation_checkpoint`, `runtime_settings`, `clarification_confirmation`, `prepare_data`, `prepare_eda`, `survey_corpus`, `ideator_handoff`, `gui_experiment_transition`, and `gui_set_sota`. Keep long Plan turns, scoring, report rendering, Git operations, remote compute, and Agent work outside the lock. When an external preflight leaves an orphan artifact/worktree after commit rejection, log it as an orphan reference; do not claim rollback of external effects.

- [ ] **Step 4: Run all scheduler/supervisor/settings/recovery tests and the no-direct-write gate**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/research/supervisor test/unit/research/test_runtime_settings.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py tests/test_gui_gateway_handler.py test/architecture/test_supervisor_surface.py`

Expected: all pass; attempt count cannot overshoot and state/tree pairs never diverge at injected points.

- [ ] **Step 5: Commit internal single-writer migration**

```powershell
git add src/athena/research/supervisor/plan_lifecycle.py src/athena/research/supervisor/plan_runtime.py src/athena/research/supervisor/settlement.py src/athena/research/supervisor/search_loop.py src/athena/research/supervisor/phases.py src/athena/research/supervisor/run_state.py src/athena/research/runtime/settings.py src/athena/research/runtime/clarification.py src/athena/research/clarification/confirmation.py src/athena/research/prepare/data.py src/athena/research/prepare/eda.py src/athena/research/runtime/survey.py src/athena/research/turns/ideator.py src/athena/gui/service.py src/athena/gui/experiments.py test/unit/research/test_runtime_settings.py test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py tests/test_gui_gateway_handler.py test/architecture/test_supervisor_surface.py
git commit -m "refactor: route research writes through coordinator"
```

---

### Task 11: Durable validation policy and final-evaluator consumption

> Required refinement: execute Tasks 3-4 from
> `docs/superpowers/plans/2026-09-05-skip-validate-engineering-simplification.md`
> as part of this task. Report state and skipped finalization must use the same
> transaction coordinator and cannot retain parallel boolean/report APIs.

**Files:**
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `src/athena/research/runtime/phase_runner.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/settings.py`
- Modify: `src/athena/research/control/service.py`
- Modify: `src/athena/research/control/context.py`
- Modify: `src/athena/research/report.py`
- Modify: `src/athena/research/exp_docs.py`
- Modify: `src/athena/research/supervisor/plan_runtime.py`
- Modify: `src/athena/research/turns/ideator.py`
- Modify: `test/integration/research/test_completed_search_resume.py`
- Modify: `test/integration/research/test_validate_agent_contract.py`
- Modify: `test/unit/research/test_runtime_settings.py`
- Modify: `test/unit/research/test_report.py`

**Interfaces:**
- `effective_auto_validate(config_default, state) -> bool` gives `auto_validate_override` precedence and fails closed when a committed resume receipt exists but sidecar status is invalid.
- `HumanValidationPermit` is constructed by the gateway/runtime command boundary from a real request identity; it is not serializable into a model tool request.
- Final evaluator generation is `sha256(final_evaluator_ref)`; checkpointing a different ref marks it `available`, revealing a result marks it `consumed` in the same transaction as current validation.
- Reusing a consumed generation with a human permit sets `validation_authority="exploratory_reuse"`; a fresh generation sets `validation_authority="independent"`.

- [ ] **Step 1: Write failing restart, corrupt-sidecar, bypass, consumption, and leakage tests**

```python
@pytest.mark.asyncio
async def test_committed_resume_override_survives_runtime_reconstruction(completed_run):
    await completed_run.resume(20, operation_id="resume-once")
    await completed_run.runtime.aclose()
    rebuilt = completed_run.rebuild(global_auto_validate=True)
    assert rebuilt.session.options.auto_validate is False
    assert rebuilt.state.phase == "SEARCH"


def test_corrupt_resume_sidecar_fails_closed(completed_run):
    completed_run.resume_path.write_text("{broken", encoding="utf-8")
    rebuilt = completed_run.rebuild(global_auto_validate=True)
    assert rebuilt.state.phase == "SEARCH"
    assert rebuilt.state.status == "WAITING"
    assert rebuilt.session.options.auto_validate is False
    assert rebuilt.control_startup_problem.code == "resume_metadata_invalid"


@pytest.mark.asyncio
async def test_model_cannot_validate_manual_only_resume(completed_run):
    result = await completed_run.model_set_phase("VALIDATE")
    assert result.error.code == "invalid_transition"
    assert completed_run.validation_calls == 0
```

Capture Plan, ordinary Ideator, and debate-Ideator prompts after resume. Assert neither the numeric final score nor final report text appears. Compact the completion ledger and assert consumption/history remains.

- [ ] **Step 2: Run validation-policy tests and confirm global GUI auto-validate currently wins**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_completed_search_resume.py test/integration/research/test_validate_agent_contract.py test/unit/research/test_runtime_settings.py test/unit/research/test_report.py`

Expected: new restart/guard/label tests fail before implementation.

- [ ] **Step 3: Apply sidecar policy before workflow wiring and consume generation atomically**

```python
def effective_auto_validate(config_default: bool, loaded: CheckedResearchState) -> bool:
    if loaded.committed_resume and loaded.resume_status != "valid":
        return False
    override = loaded.state.auto_validate_override
    return config_default if override is None else override


def validation_authority(state: ResearchState, permit: HumanValidationPermit) -> str:
    if state.final_evaluator_status == "available":
        return "independent"
    if permit.allows_exploratory_reuse:
        return "exploratory_reuse"
    raise ControlRejected(
        "invalid_transition", "final evaluator was already consumed"
    )
```

Make validation completion one coordinator checkpoint containing `validation`, report ref, phase/status, generation consumption, and derived-report bytes. The model-origin `set_phase(VALIDATE)` path cannot carry a permit. Update report headings and stage provenance so exploratory reuse cannot be rendered as independent final validation.

- [ ] **Step 4: Verify fail-closed restart, human-only validation, and prompt/report safety**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_completed_search_resume.py test/integration/research/test_validate_agent_contract.py test/unit/research/test_runtime_settings.py test/unit/research/test_report.py test/unit/research/supervisor/test_supervisor.py`

Expected: all pass; corrupt/missing resumed sidecar never reaches automatic VALIDATE.

- [ ] **Step 5: Commit scientific validity safeguards**

```powershell
git add src/athena/research/supervisor/phases.py src/athena/research/runtime/phase_runner.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/settings.py src/athena/research/control/service.py src/athena/research/control/context.py src/athena/research/report.py src/athena/research/exp_docs.py src/athena/research/supervisor/plan_runtime.py src/athena/research/turns/ideator.py test/integration/research/test_completed_search_resume.py test/integration/research/test_validate_agent_contract.py test/unit/research/test_runtime_settings.py test/unit/research/test_report.py
git commit -m "feat: enforce manual validation after resume"
```

---

### Task 12: Search re-arm, real request identity, and backend interaction routing

**Files:**
- Modify: `src/athena/research/runtime/control.py`
- Modify: `src/athena/research/runtime/facade.py`
- Modify: `src/athena/research/supervisor/search_loop.py`
- Modify: `src/athena/research/supervisor/phases.py`
- Modify: `src/athena/research/runtime/event_projection.py`
- Modify: `src/athena/research/supervisor/events.py`
- Modify: `src/athena/gui/service.py`
- Modify: `src/gui_gateway/handler.py`
- Modify: `src/gui_gateway/transport.py`
- Modify: `tests/test_gui_gateway_handler.py`
- Modify: `tests/test_gui_gateway_transport.py`
- Modify: `test/unit/research/test_breakpoint_resume.py`
- Modify: `test/integration/research/test_completed_search_resume.py`

**Interfaces:**
- `rearm_search(runtime, operation_id) -> asyncio.Task[None]` starts Agent infrastructure, clears a terminal lifecycle handle, and schedules `Supervisor.continue_phase()` without awaiting the full Search.
- `ResearchRuntime.message(text, *, request_id: str | None = None, human_origin: bool = False)` keeps framework identity internal.
- Exact command regex: `^/search \+([1-9][0-9]*)$`; it calls `ResearchControlService.resume_search()` and no other mutation path.
- `StateEvent.interaction_mode` is required on new backend events; `session_switch` also returns it as a summary hint.
- `GuiRequestHandler.dispatch(..., request_id=None)` receives `RequestEnvelope.request_id` from transport.

- [ ] **Step 1: Write failing exact command, prose, completed-turn, state-event, and parked-run tests**

```python
@pytest.mark.asyncio
async def test_exact_search_command_uses_transport_identity_and_one_service(runtime):
    response = await runtime.message(
        "/search +20", request_id="rpc-41", human_origin=True
    )
    assert response.search_limit == response.attempts_before + 20
    assert runtime.control.last_operation_id == "default:transport:rpc-41"


@pytest.mark.parametrize("text", ["search +20", "/search 20", "/search +0", "please /search +20"])
async def test_nonexact_search_text_reaches_supervisor(runtime, text):
    await runtime.message(text, request_id="rpc-42", human_origin=True)
    assert runtime.supervisor_messages == [text]


@pytest.mark.asyncio
async def test_completed_session_message_starts_agent_only_not_lifecycle(completed_runtime):
    await completed_runtime.message("再 search 20 个", request_id="rpc-43", human_origin=True)
    assert completed_runtime.agents_open is True
    assert completed_runtime.lifecycle_started_before_tool is False
    assert completed_runtime.resume_calls == [20]
```

Drive Search with fake Plan completions until the allowance is consumed, then assert `SEARCH/WAITING`, no validation call, one durable “已从断点恢复” Supervisor output, and one state event carrying updated attempts/limit/status plus `interaction_mode="supervisor"`.

- [ ] **Step 2: Run routing/resume tests and observe missing `/search` and completed-session re-arm**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_completed_search_resume.py test/unit/research/test_breakpoint_resume.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/research/supervisor/test_events.py`

Expected: new command, identity, and interaction-mode assertions fail.

- [ ] **Step 3: Thread request identity through the gateway and use one resume start callback**

```python
async def _control_command(runtime: Any, command: str, request_id: str | None):
    match = _SEARCH_MORE.fullmatch(command)
    if match is not None:
        operation_id = runtime.control.transport_operation_id(request_id)
        return await runtime.control.resume_search(
            additional_attempts=int(match.group(1)), operation_id=operation_id
        )
    return await _existing_exact_command(runtime, command)
```

Pass `req.request_id` from `WebSocketTransport.handle()` to `GuiRequestHandler.dispatch()`, then to `GuiService.message()` and `ResearchRuntime.message()`. Direct test/TUI callers without a request ID receive a fresh framework ID; a retrying transport must reuse its request ID. Before an authoritative completed-session Supervisor turn, call `agents.start()` only; do not invoke `Supervisor.start()` until the accepted resume effect re-arms Search.

Make normal Search completion set WAITING inside the lifecycle task that owns the loop; `_spawn_search()` must not leave RUNNING after an exhausted detached loop. Correlate the spawned task with its originating operation ID in logs.

- [ ] **Step 4: Verify one path for natural language and deterministic fallback through budget exhaustion**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_completed_search_resume.py test/unit/research/test_breakpoint_resume.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/research/supervisor/test_events.py test/integration/research/test_rolling_search.py`

Expected: all pass; merely opening/switching to COMPLETED remains read-only.

- [ ] **Step 5: Commit backend routing and lifecycle re-arm**

```powershell
git add src/athena/research/runtime/control.py src/athena/research/runtime/facade.py src/athena/research/supervisor/search_loop.py src/athena/research/supervisor/phases.py src/athena/research/runtime/event_projection.py src/athena/research/supervisor/events.py src/athena/gui/service.py src/gui_gateway/handler.py src/gui_gateway/transport.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_completed_search_resume.py test/unit/research/supervisor/test_events.py test/integration/research/test_rolling_search.py
git commit -m "feat: resume completed search through one control path"
```

---

### Task 13: Startup recovery, downgrade guard, and cross-OS ownership acceptance

**Files:**
- Modify: `src/athena/research/control/journal.py`
- Modify: `src/athena/research/control/coordinator.py`
- Modify: `src/athena/research/runtime/bootstrap.py`
- Modify: `src/athena/research/runtime/facade.py`
- Create: `scripts/verify_cross_os_session_lock.py`
- Modify: `test/integration/research/test_session_owner.py`
- Modify: `test/integration/research/test_search_recovery.py`
- Modify: `test/integration/research/test_completed_search_resume.py`

**Interfaces:**
- Runtime order is: acquire owner lock → recover active control documents → checked state/sidecar/tree load → derive effective policy → wire services → complete any committed start directive.
- `runtime-capabilities.json` records `{"control_transactions": 1, "manual_validation_override": 1}` after the first new-format commit.
- Current launchers reject writable startup when capabilities exceed their supported values; documented downgrade must resolve journals and park SEARCH/WAITING before removing the marker.
- `scripts/verify_cross_os_session_lock.py hold|probe <native-state-root>` adds the repository `src` directory to `sys.path`, prints one JSON record, mutates no research document, and exits `0` only when its acquire/reject expectation occurs.

- [ ] **Step 1: Add failing startup phase-table and supported Windows/WSL path tests**

```python
@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("PREPARED", "rolled_back"),
        ("STATE_COMMITTED", "finish_commit"),
        ("ROLLBACK_PENDING", "retry_rollback"),
        ("COMPLETED", "replay"),
        ("ROLLED_BACK", "verify_before"),
    ],
)
def test_runtime_resolves_journal_before_loading_state(runtime_factory, phase, expected):
    runtime_factory.write_journal(phase)
    runtime = runtime_factory.build()
    assert runtime.recovery.kind == expected
```

Add a subprocess test that holds the lock, kills the holder, and reacquires. The Windows/WSL helper must print machine-readable JSON containing `platform`, `path`, `acquired`, and `holder_pid` with no state mutation.

- [ ] **Step 2: Run startup recovery tests before implementing the final ordering**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_session_owner.py test/integration/research/test_search_recovery.py test/integration/research/test_completed_search_resume.py`

Expected: new order/capability assertions fail.

- [ ] **Step 3: Complete recovery ordering and the narrow cross-OS verifier**

```python
def recover_and_load(
    paths: ResearchPaths,
    session_id: str,
    owner: SessionOwnerLock,
) -> RecoveredResearch:
    journal = ControlJournalStore(ControlPaths.from_research(paths), session_id)
    directive = journal.recover_documents(assert_owner=owner.assert_held)
    loaded = ResearchState.load_checked(paths.state)
    tree = ResearchTree.load(paths.tree) if paths.tree.is_file() else ResearchTree()
    return RecoveredResearch(
        state=loaded.state,
        tree=tree,
        sidecar_status=loaded.resume_status,
        directive=directive,
    )
```

Do not delete unresolved journals. `STATE_COMMITTED` installs the complete after-image and defers only the resumable scheduler start until wiring; `ROLLBACK_PENDING` never starts Search. Capability mismatch is a typed startup failure with log location and downgrade instruction.

- [ ] **Step 4: Run automated process tests and the real Windows/WSL lock probe**

Run automated: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/integration/research/test_session_owner.py test/integration/research/test_search_recovery.py test/integration/research/test_completed_search_resume.py`

Run Windows holder in one PowerShell:

```powershell
.venv\Scripts\python.exe scripts\verify_cross_os_session_lock.py hold "C:\Users\80163\Desktop\挑战杯_2026\Athena\.test-lock-session"
```

While it holds, run WSL probe:

```powershell
wsl.exe -d Ubuntu -- python3 /mnt/c/Users/80163/Desktop/挑战杯_2026/Athena/scripts/verify_cross_os_session_lock.py probe /mnt/c/Users/80163/Desktop/挑战杯_2026/Athena/.test-lock-session
```

Expected: WSL reports `acquired=false`; after terminating the holder, the same probe reports `acquired=true`. Then run the reverse-direction holder in a WSL terminal:

```powershell
wsl.exe -d Ubuntu -- python3 /mnt/c/Users/80163/Desktop/挑战杯_2026/Athena/scripts/verify_cross_os_session_lock.py hold /mnt/c/Users/80163/Desktop/挑战杯_2026/Athena/.test-lock-session
```

While it holds, run the Windows probe:

```powershell
.venv\Scripts\python.exe scripts\verify_cross_os_session_lock.py probe "C:\Users\80163\Desktop\挑战杯_2026\Athena\.test-lock-session"
```

Expected: Windows reports `acquired=false`; after terminating only the WSL holder,
the same Windows probe reports `acquired=true`. Remove only the resolved
`.test-lock-session/run.lock`, `.test-lock-session/run.pid`, and empty
`.test-lock-session` test directory after both processes exit and after confirming
the resolved directory is the repository's exact `.test-lock-session` child.

- [ ] **Step 5: Commit recovery and cross-platform acceptance support**

```powershell
git add src/athena/research/control/journal.py src/athena/research/control/coordinator.py src/athena/research/runtime/bootstrap.py src/athena/research/runtime/facade.py scripts/verify_cross_os_session_lock.py test/integration/research/test_session_owner.py test/integration/research/test_search_recovery.py test/integration/research/test_completed_search_resume.py
git commit -m "feat: recover control transactions before runtime load"
```

---

### Task 14: Backend regression, security audit, documentation, and handoff

> Required refinement: include Task 5 from
> `docs/superpowers/plans/2026-09-05-skip-validate-engineering-simplification.md`
> in this task's verification and documentation closure.

**Files:**
- Modify: `docs/research_core_mechanisms_ch.md`
- Modify: `docs/athena-gui-design.md`
- Modify: `docs/superpowers/specs/2026-09-04-supervisor-transactional-search-resume-design.md`
- Create: `codex_docs/2026-09-04-supervisor-transactional-search-resume-backend-completion-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete after acceptance: `docs/superpowers/plans/2026-09-04-supervisor-transactional-search-resume-backend.md`

**Interfaces:**
- Documents exact files, journal phases, log fallbacks, error codes, `/search +N`, human validation origin, downgrade steps, and known platform limitations.
- Backend closeout activates `docs/superpowers/plans/2026-09-04-supervisor-search-resume-frontend.md`, preserves the queued experiment-document projection plan that follows it, and does not mark the whole cross-stack design implemented yet.

- [ ] **Step 1: Run formatting, diff, import, and API-budget gates**

```powershell
.venv\Scripts\python.exe -m black --check src/athena/core/persistence.py src/athena/core/project_lock.py src/athena/core/windows_lock_broker.py src/athena/research/control src/athena/research/supervisor src/athena/research/runtime src/athena/agents/base_runner.py src/athena/agents/supervisor_agent.py src/gui_gateway test/unit/research/control test/integration/research/test_transactional_control.py test/integration/research/test_completed_search_resume.py test/integration/research/test_session_owner.py
.venv\Scripts\python.exe -c "from athena.research.control import ResearchControlService; from athena.research import ResearchRuntime; print('imports ok')"
git diff --check
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/architecture/test_supervisor_surface.py test/unit/research/control/test_contracts.py
```

Expected: Black/import/diff pass; exactly five normal tools and the fixed top-level schemas match the committed snapshot.

- [ ] **Step 2: Run focused backend acceptance suites**

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test/unit/core/test_persistence.py test/unit/core/test_project_lock.py test/unit/agent/test_agent_runtime.py test/unit/agent/test_supervisor_agent.py test/unit/kaggle/test_supervisor_gate.py test/unit/research/control test/unit/research/supervisor test/unit/research/test_runtime_settings.py test/unit/research/test_breakpoint_resume.py test/integration/research/test_transactional_control.py test/integration/research/test_completed_search_resume.py test/integration/research/test_session_owner.py test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py test/integration/research/test_validate_agent_contract.py tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/test_gui_gateway_main.py
```

Expected: zero failures; record exact pass count, warnings, elapsed time, and the real Windows/WSL probe result.

- [ ] **Step 3: Run complete backend suite and inspect every failure before classification**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q test tests`

Expected: zero task-owned failures. Any unrelated pre-existing failure must be reproduced on the plan base commit before it may be recorded rather than fixed.

- [ ] **Step 4: Run explicit security and duplicate-path audit**

```powershell
rg -n "write_file|shell_command|rollback_state|operation_id|session_id|journal|retry" src/athena/agents/supervisor_agent.py test/fixtures/supervisor_tool_schemas.json
rg -n "propose_hypothesis|select_next_hypothesis|configure_search|update_waiting_plan_budget|set_phase_decision|set_manual_mode|configure_kaggle|record_task_understanding|read_hypotheses|read_state|read_plans|record_guidance" src/athena/agents/supervisor_agent.py src/athena/agents/prompts/supervisor_agent.md
rg -n "\.state\.save\(|\._tree\.save\(|tree\.save\(" src/athena/research src/athena/gui
```

Expected: no forbidden schema field or dangerous tool; legacy names occur only in delegation adapters/tests/migration docs; no active state/tree writer bypasses the coordinator. Manually inspect canonical-path checks, redaction, final-score exclusion, General saga wording, and model validation guard.

- [ ] **Step 5: Write docs and completion report from fresh evidence**

The report must include:

```markdown
## Delivered behavior
- Five-tool Supervisor surface and exact public schema budget.
- Candidate/journal/rollback semantics and permanent resume receipts.
- COMPLETED +20 transition, durable manual validation, and SEARCH/WAITING result.
- Session ownership and Windows/WSL evidence.
- Primary/fallback log paths and `logging_degraded` limit.

## Verification evidence
- Exact commands, pass counts, warnings, timings, and commit hashes.
- Failure-injection matrix and object-identity result.
- Natural-language and `/search +N` shared-service evidence.

## Remaining dependent work
- Frontend routing and empty-workspace hiding remain in the queued frontend plan.
```

Update the design status to `backend implemented and verified; frontend queued`. Document the honest limitation that an arbitrary historical binary launched outside a current guarded launcher cannot understand a future capability marker.

- [ ] **Step 6: Close the backend plan only after every backend acceptance criterion is green**

Delete this completed plan, update `codex_docs/CURRENT.md` so the frontend plan becomes the sole active plan, and stage only owned docs:

```markdown
Active implementation plan:
- `docs/superpowers/plans/2026-09-04-supervisor-search-resume-frontend.md`

Queued subsequent implementation plan:
- `docs/superpowers/plans/2026-09-04-experiment-document-projection.md`

Queued subsequent supporting design spec:
- `docs/superpowers/specs/2026-09-04-experiment-document-projection-design.md`

Most recent completed work:
- `codex_docs/2026-09-04-supervisor-transactional-search-resume-backend-completion-report.md`
```

Remove the now-empty `Queued dependent implementation plan` section and retain the
queued subsequent, parallel, and paused plan sections.

```powershell
git add docs/research_core_mechanisms_ch.md docs/athena-gui-design.md docs/superpowers/specs/2026-09-04-supervisor-transactional-search-resume-design.md codex_docs/2026-09-04-supervisor-transactional-search-resume-backend-completion-report.md codex_docs/CURRENT.md
git add -u docs/superpowers/plans/2026-09-04-supervisor-transactional-search-resume-backend.md
git commit -m "docs: complete transactional search resume backend"
```

Do not merge or push without a separate user instruction. Hand off the exact verified commit and the now-active frontend plan.
