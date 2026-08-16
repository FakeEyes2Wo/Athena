# Breakpoint-Resume Fix — Completion Report (2026-08-16)

## Result

Implemented on `main`, commits:

- `281bca7` — WIP baseline (user's websearch work committed first, as requested).
- `3c0e052` — `feat(research): resume checkpoints for task understanding, general research, and PREPARE`.
- `a8df535` — `fix(research): persist general agent id before waiting for its turn`.
- `de9e3c0` — `refactor(research): flatten general worker resume branch`.
- `358cb90` — `fix(research): backward-compatible resume state and logic review fixes`.
- `20aaa44` — `fix(research): ten-pass logic review and simplification`.
- `ee5ded2` — `fix(research): restore debate/gated ideator paths and their tests`.

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
  breakpoint_resume): **63 passed**.
- Runtime batch (ideators / survey / eval-handoff / task-seeding): **37 passed**
  (the two pre-existing ideator failures were fixed in `ee5ded2`).
- Idea-generation suite: **32 passed** (six pre-existing failures fixed).
- Full `test/unit/research`: **319 passed**.
- `python -m compileall` on all changed production modules: exit 0.
- `scripts/check_code_style.py` on all changed production modules: exit 0
  (R3 docstring advisories remain non-blocking by design; R4 separator lines in
  `idea_schemas.py` were removed).
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

## Extra repair rounds 11–12 (commit `ee5ded2`)

Round 11 — legacy bugs:
- `agents/ideator/ideator.py` no longer imports deleted `DataProfile` /
  `retrieval.types`; context items are dumped duck-typed (pydantic / dataclass).
- `_run_debate_ideator_turn` uses a local `_DebateProfile`; debate mode runs again.
- `IdeatorHypothesisBatch` regained the optional `eda_request` field; gated output
  is normalized to `HypothesisBatch` so `run_ideator_turn` and dynamic EDA work.
- Updated stale tests (`_ideation="gated"`, list-return fakes) to the real
  contracts.

Round 12 — expanded regression and cleanup:
- Removed R4 ASCII separator lines from `idea_schemas.py`.
- Fixed six pre-existing idea-generation test failures (missing
  `survey_corpus_ref` in fakes + old list-return assertions).
- Full `test/unit/research` green (319 passed).

## Ten-pass logic review and simplification (commit `20aaa44`)

Pass 1 persistence layer: fixed inline+resume mixed-state migration gap and
deleted the dead `resume_applied` flag.
Pass 2 runtime task-text paths: no new defects; helper behavior verified.
Pass 3 supervisor: normalize `task` before cache-key comparison; persist the
cleared broken cache ref immediately so restarts don't re-hit it.
Pass 4 agent turns: supervisor/data/general timeout now interrupt the still
running worker (prevents permanent `AgentBusyError` wedge); data turn got the
missing timeout; pre-wait checkpoint block simplified.
Pass 5 phase/thread boundaries: evaluator reuse and rollback-only failure path
re-verified, no change.
Pass 6 single-writer consistency: remaining `_state` references only exist
before `recover()` runs; authoritative `rt.state` used everywhere else.
Pass 7 crash windows/compatibility: split-file, digest binding, inline
migration covered by tests.
Pass 8 test gaps: added broken-cache healing and timeout-interrupt tests.
Pass 9 simplification: extracted `_interrupt_agent` (removes 12 duplicated
lines), extracted `_merge_resume`, flattened `start()` understanding branch.
Pass 10 end-to-end re-read and final verification (63 passed).

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
