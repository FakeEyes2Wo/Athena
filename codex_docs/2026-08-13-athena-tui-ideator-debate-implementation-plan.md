# Athena TUI Ideator Debate Board Implementation Plan

> **For agentic workers:** Use test-driven-development for every behavior.
> Work directly on `main`, preserve unrelated changes, and stage only files
> owned by this plan.

**Goal:** Run and display up to three Ideators with adaptive one-, two-, or
three-lane debate output inside the existing scrollable TUI history.

**Architecture:** `ResearchRuntime` fans one Ideator request out to at most
three AgentRuntime threads and projects their existing journals with stable
`OutputEvent.plan` labels. `athena_tui.render` groups those labeled entries into
one cell-width-aware debate board while all viewport state remains in
`AthenaApp`.

**Tech Stack:** Python 3.11+, asyncio, Pydantic, prompt-toolkit 3.0.52, pytest,
pytest-asyncio.

## Global Constraints

- Keep the public TUI protocol limited to `output` and `state`.
- Do not add fields to `OutputEvent` or `StateEvent`.
- Launch and display at most three Ideators.
- Render exactly one lane per actual selected Ideator; never render empty lanes.
- Preserve ordinary history and the existing unified scroll state.
- Preserve unrelated worktree changes.

## Task 1: Concurrent Ideator Fan-Out And Event Projection

**Files:**
- Modify: `src/athena/research/runtime.py`
- Create: `test/unit/research/test_runtime_ideators.py`

**Interfaces:**
- Reuse `AgentRuntime.create_root`, `run_events`, and `wait_run`.
- Reuse `ResearchRuntime._project_agent_event(plan, kind, event_ref, data)`.
- Preserve `_run_ideator_turn(count: int) -> list[Hypothesis]`.

- [ ] Write focused tests with a deterministic fake AgentRuntime proving
  counts `1`, `2`, and `4` launch `1`, `2`, and `3` Agents with target
  allocations `[1]`, `[1, 1]`, and `[2, 1, 1]`.
- [ ] Run the focused tests and retain RED caused by the current single Agent.
- [ ] Add `_ideator_allocations`, `_forward_ideator_events`, and one-lane run
  helpers; gather lanes concurrently and merge successful batches in order.
- [ ] Add a partial-failure test proving one failed lane emits a labeled error
  and successful peers still return hypotheses.
- [ ] Run focused tests GREEN and the existing Supervisor Ideator wiring slice.

## Task 2: Adaptive Debate Board Rendering

**Files:**
- Modify: `src/athena_tui/render.py`
- Modify: `test/unit/athena_tui/test_render.py`

**Interfaces:**
- Consume existing `HistoryEntry.plan` values `ideator-1..ideator-N`.
- Keep `render_history_lines(state, width)` as the application-facing API.

- [ ] Add failing renderer tests for one, two, three, and four distinct labels,
  ordinary-history retention, equal lane widths, and the three-lane cap.
- [ ] Add a failing narrow-width test proving two actual Ideators become two
  stacked blocks rather than three placeholders or unreadable narrow columns.
- [ ] Run renderer tests and retain RED from the absent debate grouping.
- [ ] Implement lane selection, per-lane wrapping, cell padding, horizontal
  composition, and narrow stacking as pure rendering helpers.
- [ ] Run renderer tests GREEN and verify all lines stay within widths 40, 72,
  80, and 120.

## Task 3: Viewport And Protocol Regression

**Files:**
- Modify product code only if a fresh failing regression requires it.
- Update this plan's checkboxes with exact evidence.

- [ ] Run `test/unit/athena_tui/test_app.py` to prove the debate board still
  flows through `_history_fragments` and unified scrolling.
- [ ] Run the complete TUI unit and protocol slices.
- [ ] Run the Supervisor Ideator wiring and runtime focused tests together.
- [ ] Confirm changed files do not include event schemas, controller, Agent
  kernel, or Supervisor policy.

## Acceptance Criteria

- [ ] One actual Ideator renders one lane, two render two lanes, and three
  render three lanes.
- [ ] Requests above three launch and display only three Ideators.
- [ ] Ordinary output remains visible and is not duplicated.
- [ ] Lane rows never exceed terminal width and narrow terminals stack lanes.
- [ ] Wheel, PageUp/PageDown, End, unseen count, and tail following remain one
  shared viewport behavior.
- [ ] A failed lane does not discard successful peer hypotheses.
- [ ] TUI protocol remains exactly `output` and `state`.
