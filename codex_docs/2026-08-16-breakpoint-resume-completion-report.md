# Breakpoint-Resume Fix — Completion Report (2026-08-16)

## Result

Implemented on `main`, commits:

- `281bca7` — WIP baseline (user's websearch work committed first, as requested).
- `3c0e052` — `feat(research): resume checkpoints for task understanding, general research, and PREPARE`.
- `a8df535` — `fix(research): persist general agent id before waiting for its turn`.
- `de9e3c0` — `refactor(research): flatten general worker resume branch`.
- `358cb90` — `fix(research): backward-compatible resume state and logic review fixes`.

## Changes

- `src/athena/research/contracts.py`: added frozen `GeneralTurnOutcome` (agent id + result).
- `src/athena/research/supervisor/state.py`: resume fields (`task_text`,
  `kaggle_download`, `task_research_task`, `task_research_ref`,
  `task_research_agent_id`, `evaluator_ref`) live in a sibling **`resume.json`**
  bound to a digest of the core payload, so `state.json` keeps the legacy schema
  and old binaries can still read it. `load()` also migrates intermediate inline
  layouts and drops refs that lack a task-owner.
- `src/athena/research/supervisor/supervisor.py`: Kaggle decision persisted and
  restored; `checkpoint_evaluator`; PREPARE skips when a trusted SOTA exists;
  `dispatch_general` caches research results **keyed by task text** (different
  tasks never hit the cache nor hijack the checkpoint) and reuses the persisted
  worker id only for the same task with no cache.
- `src/athena/research/agent_turn_runner.py`: `run_general_turn` returns the
  outcome, persists task owner + worker id **before** waiting, interrupts the
  still-running worker on timeout (otherwise resume wedges on `AgentBusyError`),
  and reads/writes only the authoritative `rt.state`.
- `src/athena/research/runtime.py`: `start()` skips the task-understanding turn
  when `state.task_understanding` is persisted (emits
  `断点续传：复用已持久化的任务理解…`); `start_task()` persists the first full
  task text only on a fresh run and reconstructs the effective task text from
  persisted text or structured understanding on resume.
- `src/athena/research/phase_runner.py`: PREPARE reuses a resolvable frozen
  evaluator bundle; EDA writes go through the authoritative `rt.state`.
- `src/athena/app_server/thread_runtime.py`: failure path deliberately keeps
  rollback-only (rollout stays in sync with in-memory context); the checkpoint
  data that mattered moved into explicit state-level persistence.
- Tests added/updated across `test/unit/...` and `test/integration/...`
  (see `test/unit/research/test_breakpoint_resume.py` for hermetic coverage).

## Verification evidence

- Hermetic unit batch (state / thread_runtime / supervisor / runtime_settings /
  breakpoint_resume): **60 passed**.
- Additional runtime batch (ideators / survey / eval-handoff / task-seeding):
  35 passed; the 2 remaining failures are pre-existing from the WIP baseline
  (`DataProfile` was removed from `athena.research.data_models` but the debate
  ideator still lazily imports it), unrelated to these commits.
- `python -m compileall` on all changed production modules: exit 0.
- `scripts/check_code_style.py` on all changed production modules: exit 0
  (32 pre-existing R3 docstring advisories, non-blocking by design).
- All changed Python files formatted with in-process Black 26.5.1 using the
  repo's `[tool.black]` config (the venv's CLI Black hangs in this sandbox, so
  formatting was applied via `black.format_str`; output identical semantics).
- Independent review subagent ran against the first implementation; its findings
  (rollout/context divergence, timeout wedge, task-identity hijack, stale
  `rt._state`, legacy migration) were fixed in `358cb90` and are covered by the
  updated tests.
- Sandbox note: tests that spawn real `git` through captured subprocess pipes
  fail with the sandbox's documented named-pipe boundary
  (`_winapi.CreateNamedPipe` PermissionError) before reaching changed code; the
  new paths are covered by hermetic tests instead.

## Real-project replay

`new_kaggle_test` is still running its PREPARE on the pre-fix process, so it was
not restarted in place. Its persisted `state.json` already contains
`task_understanding`; on the next restart with this code the first supervisor
line must be `断点续传：复用已持久化的任务理解，跳过任务理解回合。` instead of
`任务理解中：…`. This is the same assertion `test_start_skips_task_understanding_when_persisted`
verifies hermetically.

## Rollback

Revert `3c0e052`, `a8df535`, `de9e3c0`, and `358cb90`. `state.json` stays
legacy-shaped, so old binaries keep reading it; delete the sibling
`.athena/resume.json` if you want to discard the new checkpoint data.
