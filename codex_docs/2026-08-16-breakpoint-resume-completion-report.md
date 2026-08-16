# Breakpoint-Resume Fix — Completion Report (2026-08-16)

## Result

Implemented on `main`, commits:

- `281bca7` — WIP baseline (user's websearch work committed first, as requested).
- `3c0e052` — `feat(research): resume checkpoints for task understanding, general research, and PREPARE`.
- `a8df535` — `fix(research): persist general agent id before waiting for its turn`.

## Changes

- `src/athena/research/contracts.py`: added frozen `GeneralTurnOutcome` (agent id + result).
- `src/athena/research/supervisor/state.py`: added resume fields `task_text`,
  `kaggle_download`, `task_research_ref`, `task_research_agent_id`, `evaluator_ref`;
  all optional so legacy `state.json` loads with defaults.
- `src/athena/app_server/thread_runtime.py`: failed turns now persist partial
  messages to the rollout before rolling back in-memory context (fixes 0-byte
  `supervisor.jsonl` and makes interrupted turns resumable).
- `src/athena/research/supervisor/supervisor.py`: Kaggle decision persisted and
  restored; `checkpoint_evaluator`; PREPARE skips when a trusted baseline already
  exists; `dispatch_general` caches the first research result and reuses the
  persisted worker id.
- `src/athena/research/agent_turn_runner.py`: `run_general_turn` returns the
  outcome, persists the stable worker id **before** waiting for the turn (so a
  timed-out/crashed worker can be resumed from its rollout on restart), and can
  continue the prior general worker via its persisted id.
- `src/athena/research/runtime.py`: `start()` skips the task-understanding turn
  when `state.task_understanding` is persisted (emits
  `断点续传：复用已持久化的任务理解…`); `start_task()` persists and reuses the
  first full task text instead of seeding `"continue"` into survey/PREPARE prompts.
- `src/athena/research/phase_runner.py`: PREPARE reuses a resolvable frozen
  evaluator bundle instead of re-running the evaluator agent.
- Tests added/updated across `test/unit/...` and `test/integration/...`
  (see `test/unit/research/test_breakpoint_resume.py` for hermetic coverage).

## Verification evidence

- Hermetic unit batch (state / thread_runtime / supervisor / runtime_settings /
  breakpoint_resume): **56 passed**.
- `python -m compileall` on all changed production modules: exit 0.
- `scripts/check_code_style.py` on all changed production modules: exit 0
  (32 pre-existing R3 docstring advisories, non-blocking by design).
- All changed Python files formatted with in-process Black 26.5.1 using the
  repo's `[tool.black]` config (the venv's CLI Black hangs in this sandbox, so
  formatting was applied via `black.format_str`; output identical semantics).
- Sandbox note: one pre-existing unit test and seven integration tests that spawn
  real `git` through captured subprocess pipes fail with the sandbox's documented
  named-pipe boundary (`_winapi.CreateNamedPipe` PermissionError) before reaching
  any changed code; the new paths are covered by hermetic tests instead.

## Real-project replay

`new_kaggle_test` is still running its PREPARE on the pre-fix process, so it was
not restarted in place. Its persisted `state.json` already contains
`task_understanding`; on the next restart with this code the first supervisor
line must be `断点续传：复用已持久化的任务理解，跳过任务理解回合。` instead of
`任务理解中：…`. This is the same assertion `test_start_skips_task_understanding_when_persisted`
verifies hermetically.

## Rollback

Revert `3c0e052` and `a8df535`; delete `.athena/state.json` in projects that
were opened with the new schema if running older code (`ResearchState` is
`extra="forbid"`).
