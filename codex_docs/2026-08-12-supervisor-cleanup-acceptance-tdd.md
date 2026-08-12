# Supervisor Cleanup And Acceptance TDD

> Run only after SEARCH/Runtime, PREPARE, and VALIDATE commits are integrated.
> Assign one Agent each to deletion, deterministic verification, and later real
> acceptance. These three tasks are serial and never share an active claim.
> Task D reads technical-design sections 19-20; Tasks E/F read sections 3 and
> 21-24. No Agent reads another execution packet.

## Task D: Delete Replaced Mechanisms

**Ownership:**

- Create `test/architecture/test_supervisor_surface.py`.
- Delete after audit: `src/athena/research/supervisor/planning/` and `storage/`.
- Delete/reduce after audit: `coordinator.py`, `executor.py`, `messaging.py`,
  `validator.py`, legacy `contracts.py`.
- Delete after replacement: `test/unit/research/supervisor/planning/`,
  `test/unit/research/test_supervisor_journal.py`,
  `test/unit/research/test_supervisor_storage.py`,
  `test/unit/research/test_validation_planner.py`.
- Modify only for migrated public behavior:
  `test/unit/research/test_prepare_flow.py`,
  `test/unit/research/test_supervisor_core.py`,
  `test/unit/research/test_supervisor_tighten.py`,
  `test/unit/research/test_services.py`, `test/unit/research/test_validation.py`,
  `test/unit/test_cli.py`, Runtime/TUI/CLI legacy imports.

- [ ] **Inventory every non-test consumer**

  ```powershell
  rg -n "supervisor\.(planning|storage|coordinator|executor|messaging|validator)|SupervisorOperation|OperationType|PlanJournal|HumanRequest|REQUESTS_GET|HUMAN_REPLY|MESSAGES_GET" src scripts
  ```

  Record every replacement. Never delete before active consumers migrate in the
  same commit.

  Write the inventory in the Task 12 completion evidence as
  `legacy symbol -> replacement owner -> migrated consumer`. The inventory is
  evidence, not a new compatibility layer.

- [ ] **RED: architecture surface**

  ```python
  def test_supervisor_has_only_coarse_plan_modules():
      assert python_module_names(Path("src/athena/research/supervisor")) == {
          "__init__", "events", "experiment", "plans", "policy", "prepare",
          "recovery", "scheduler", "state", "supervisor", "validation",
      }


  def test_runtime_has_no_legacy_protocol_or_sqlite_imports():
      source = Path("src/athena/research/runtime.py").read_text(encoding="utf-8")
      for forbidden in (
          "PlanJournal", "SupervisorCoordinator", "ProjectStateStore",
          "HumanRequest", "REQUESTS_GET", "HUMAN_REPLY", "MESSAGES_GET",
      ):
          assert forbidden not in source
  ```

  Run: `uv run pytest test/architecture/test_supervisor_surface.py -q -p no:cacheprovider`

  Expected RED: legacy packages/imports still exist.

- [ ] **GREEN:** Remove Operation DAG, SQLite, phase planners, HumanRequest,
  session polling, responder, and replaced tests. Keep DataAgent/InitAgent only
  for proven non-Supervisor consumers; remove their Supervisor registration.

  Delete an obsolete test when its behavior is deleted. Migrate it only when
  the assertion describes a retained public behavior. Do not preserve an old
  class, adapter, RPC name, or fixture solely to keep a legacy test green.

- [ ] **Regression and static deletion proof**

  ```powershell
  uv run pytest test/architecture/test_supervisor_surface.py test/unit/test_cli.py test/unit/test_main.py test/unit/test_tui.py test/unit/research test/unit/athena_tui test/integration/research -q -p no:cacheprovider
  rg -n "sqlite3|SupervisorOperation|OperationType|PlanJournal|HumanRequest|REQUESTS_GET|HUMAN_REPLY|MESSAGES_GET|read_new_messages|\.athena.?sessions|WorkspaceManager" src/athena/research src/athena_tui
  rg -n "create_subprocess|subprocess\." src/athena/research/supervisor
  ```

  Expected: tests pass; forbidden searches have no active match. Git remains in
  `LocalGitWorkspace`; candidate commands remain in `ExecutionRuntime`.

- [ ] Return deletion list, consumer migrations, RED/GREEN, and files to
  `/root`. Proposed commit: `refactor: remove legacy supervisor runtime`.

Fresh architecture RED, GREEN, and the broad regression complete Task 12. No
separate review follows.

## Task E: Deterministic Verification

Task 13 has one claim and owns verification only. It does not edit product
code. A defect blocks Task 13 until `/root` creates a separate claimed TDD fix
task for the existing infrastructure owner; after that fix completes, Task 13
starts its verification sequence again from the first step.

- [ ] **Infrastructure audit:** Re-run the index baseline and Task D searches.
  Verify exactly one Agent runtime, artifact store, workspace owner, evaluator,
  scheduler, state writer, and TUI protocol.

- [ ] **Full deterministic verification**

  ```powershell
  uv run black --check src test
  uv run pytest -q -p no:cacheprovider
  ```

  No target Supervisor integration test may be skipped.

- [ ] **Deterministic headless:** Use `scripts/run_headless.py` and injected
  deterministic Agent doubles. Do not create another runner. Assert PREPARE ->
  rolling SEARCH -> VALIDATE -> COMPLETED, stable identities, and event kinds
  exactly `output`/`state`.

- [ ] **Return the Task 13 completion package:** Include the final
  infrastructure-owner matrix, static-search output, formatter result, full
  deterministic test count, and deterministic headless phase/event evidence.
  Fresh evidence completes Task 13 without a review phase.

## Task F: Real `.env` Acceptance And Closeout

Task 14 starts only after Task 13 completes. It owns the real run, generated
acceptance evidence, final report, deletion of completed plan/TDD documents,
and the `CURRENT.md` closeout. It does not edit product or test code.

- [ ] **Real `.env` preflight:** Through `athena.core.agent.settings`, report
  only credential presence, selected model, and base URL source. Production uses
  `ResponsesProvider`. No fake client, fixed solution, acceptance-only provider,
  root `model.py`, or alternate runner.

- [ ] **Real Titanic:** Fresh ignored multi-file project, target `Survived`,
  Search attempt limit 10, concurrency 4, production headless entrypoint.
  Continue until COMPLETED, explicit Human cancellation, execution safety
  timeout/cost limit, or unrecoverable provider failure after configured retry.

  Acceptance:

  ```text
  trusted PREPARE baseline
  SEARCH attempts <= 10
  trusted successful SEARCH Experiments >= 4 (baseline excluded)
  SOTA trusted metric/evidence/predictions/commit
  distinct original SOTA and validation commits
  final phase/status = COMPLETED
  event kinds = {output, state}
  no TUI session/log reads
  ```

  Four successes are acceptance only; production does not stop at four.

- [ ] **Defect loop:** For a product defect, create a new claimed fix task, add
  a deterministic failing test, verify RED, fix through the existing owner,
  rerun its focused suite and all deterministic gates, then restart real
  acceptance in a fresh project. Never patch generated acceptance output.

  The acceptance Agent reports the failing command, smallest reproduction, and
  existing infrastructure owner to `/root`; it does not edit a file owned by a
  completed task until `/root` creates and assigns the defect task.

- [ ] **Completion:** Write
  `codex_docs/2026-08-12-supervisor-autonomous-search-final-report.md` with
  commit, commands, metrics, attempts, outcomes, SOTA/validation commits,
  artifact refs, event proof, and infrastructure audit. After every criterion
  has fresh evidence, delete the completed active plan and TDD packets and make
  `CURRENT.md` point only to the final report. Proposed commit:
  `docs: complete autonomous supervisor plan`.

The final report must state the exact commands and observed counts. A planned
command, an old test result, or a generated-output patch is not acceptance
evidence. Once Task 14's real run and final deterministic rerun pass, the plan
is complete without another implementation review.
