# Supervisor SEARCH And Runtime TDD

> Assigned Agent reads `CURRENT.md`, the execution TDD index, this packet, and
> technical-design sections 3-10 and 14-19 only. Keep one claim from SEARCH
> core through Runtime integration. Do not hand `supervisor.py` to another
> Agent.

**Goal:** Implement the only `ResearchState`/`ResearchTree` writer, rolling
SEARCH, recovery, Human decisions, and the thin `ResearchRuntime` composition
boundary.

## Ownership

Create/modify only:

- `src/athena/agents/plan_agent.py`
- `src/athena/core/agent/prompts/plan_agent.md`
- `src/athena/research/supervisor/supervisor.py`
- `src/athena/research/supervisor/plans.py` (A.1 frozen metric rule only)
- `src/athena/research/supervisor/scheduler.py` (A.1 policy forwarding only)
- `src/athena/research/supervisor/__init__.py`
- `test/unit/agent/test_plan_agent.py`
- `test/unit/research/supervisor/test_supervisor.py`
- `test/unit/research/supervisor/test_plan_contracts.py`
- `test/unit/research/supervisor/test_scheduler.py`
- `test/integration/research/test_rolling_search.py`
- `test/integration/research/test_search_recovery.py`
- after PREPARE/VALIDATE packets commit: `src/athena/research/runtime.py`
- after PREPARE/VALIDATE packets commit: `src/athena/research/supervisor/events.py`
- after PREPARE/VALIDATE packets commit:
  `test/integration/research/test_autonomous_research.py`
- after PREPARE/VALIDATE packets commit:
  `test/integration/research/test_human_plan_boundary.py`

Modify `src/athena/core/research_tree.py` only if a focused RED proves a
missing read query. Do not modify PREPARE/VALIDATE Agent modules.

## Required Infrastructure

Reuse `ResearchState`, `ResearchTree`, `Scheduler`, `Recovery`, `AgentRuntime`,
`LocalGitWorkspace`, `PlanRunner`, `LocalArtifactStore`, and `EventProjector`.
Do not add a queue, repository, service container, EventBus, WorkspaceManager,
or another priority/scoring implementation.

```python
PlanTurn = Callable[[str, PlanState], Awaitable[PlanTurnResult]]
SupervisorTurn = Callable[[str], Awaitable[str]]
Publish = Callable[
    [Literal["output", "state"], dict[str, object]],
    Awaitable[None],
]


class Supervisor(SupervisorActions):
    def __init__(
        self,
        *,
        project_root: Path,
        state: ResearchState,
        tree: ResearchTree,
        store: ArtifactStore,
        agents: AgentRuntime,
        workspaces: LocalGitWorkspace,
        scheduler: Scheduler,
        recovery: Recovery,
        evaluator_ref: ArtifactRef,
        run_plan_turn: PlanTurn,
        run_supervisor_turn: SupervisorTurn,
        publish: Publish,
        auto_validate: bool = False,
    ) -> None: ...
    async def start(self) -> None: ...
    async def run_search(self) -> None: ...
    async def start_plan(self, hypothesis_id: str) -> str: ...
    async def message(self, text: str) -> str: ...
    async def stop(self) -> None: ...
```

The two callables are narrow adapters to existing AgentRuntime/PlanRunner
behavior, not new runtimes.

Use the existing concrete entry points:

```python
AgentRuntime.create_root(agent_type, task, agent_id=hypothesis_id, name=hypothesis_id)
AgentRuntime.send_message(hypothesis_id, feedback)
AgentRuntime.followup(hypothesis_id, task)
AgentRuntime.wait_run(run_id)
LocalGitWorkspace.create(base_commit, branch=hypothesis_id)
LocalGitWorkspace.diff(workspace)
LocalGitWorkspace.commit(workspace, approved_diff, message)
PlanRunner.run_turn(plan_id, state, plan_input, emit=emit)
Scheduler.next_actions(state, tree, running_ids, human_next=human_next)
Recovery.reconcile(state, tree, workspace_exists, artifact_exists)
EventProjector.output(...) / EventProjector.tool_output(...)
```

If one exact call differs in the checked-out code, adapt to that existing public
method and record it in the evidence. Do not wrap the owner just to reproduce
the spelling above.

## A.0 PlanAgent Contract

The PlanAgent is a prompt-driven `Agent` registered through
`AgentTypeRegistry` and adapted by `BaseAgentRunner`. It uses
`generic_tool_registry(workspace, runtime=execution)` and structured
`PlanDecision`; production passes `ResponsesProvider`, while unit tests inject
the provider.

```python
def register_plan_agent(
    registry: AgentTypeRegistry,
    *,
    provider: object,
    artifacts: ArtifactStore,
    workspace_for: Callable[[str], Path],
    execution: ExecutionRuntime,
) -> None: ...
```

- [ ] **A.0 RED:** Add `test_plan_agent_returns_structured_decision` and
  `test_plan_agent_uses_its_named_workspace`. The first rejects ordinary prose
  without a valid `PlanDecision`; the second creates two Agent IDs and proves
  their write/shell tools resolve to different workspaces.

  Run:
  `uv run pytest test/unit/agent/test_plan_agent.py -q -p no:cacheprovider`

  Expected RED: missing PlanAgent registration/prompt, not fixture failure.

- [ ] **A.0 GREEN:** Register one fresh Agent specification per Hypothesis ID.
  The prompt requires inspect -> implement -> run/debug -> return structured
  decision. It may suggest later hypotheses, but has no Supervisor actions and
  cannot mutate ResearchTree or ResearchState.

- [ ] **A.0 regression:** Run the focused test plus
  `test/unit/agent/test_supervisor_agent.py`,
  `test/unit/agent/test_generic_tools.py`, and
  `test/unit/research/supervisor/test_plan_contracts.py`.

## Test Harness

Keep `_Harness` in the owned test files. Use real `ResearchState`,
`ResearchTree`, `LocalArtifactStore`, and temporary Git repositories. Only the
Agent turn and Plan turn are fakes, driven by queued results and per-Plan
`asyncio.Event`. Harness helpers call public production methods and read
persisted files; they never mutate expected state/tree directly. Crash helpers
raise `InjectedCrash`, reconstruct production objects over the same `tmp_path`,
and assert recovered state.

## A.1 SEARCH Core

- [ ] **RED 1: frozen Plan and stable identity**

  ```python
  @pytest.mark.asyncio
  async def test_new_plan_freezes_evaluator_tree_and_human_context(harness):
      await harness.supervisor.record_guidance("use ViT next", scope="next")
      await harness.supervisor.start_plan("hyp_vit")
      frozen = await harness.plan_input("hyp_vit")
      assert frozen.hypothesis.id == "hyp_vit"
      assert frozen.evaluator_ref == harness.evaluator_ref
      assert frozen.human_context == "use ViT next"
      assert harness.identity("hyp_vit") == {
          "plan": "hyp_vit", "agent": "hyp_vit",
          "workspace": "hyp_vit", "log": "hyp_vit",
      }
  ```

  Run: `uv run pytest test/unit/research/supervisor/test_supervisor.py -q -p no:cacheprovider`

  Expected RED: `Supervisor`/`start_plan` absent, not fixture/import failure.

- [ ] **GREEN 1:** Implement loading, validated actions, immutable Plan input,
  stable identity, and atomic state persistence. Freeze the real evaluator ref;
  never create `{}` as an evaluator placeholder.

- [ ] **RED 2: rolling completion and turn durability**

  ```python
  @pytest.mark.asyncio
  async def test_first_completion_refills_without_batch_barrier(harness):
      await harness.start_search(limit=6, concurrency=4)
      assert set(harness.running_ids) == {"h1", "h2", "h3", "h4"}
      await harness.finish("h2", metric=0.82)
      assert set(harness.running_ids) == {"h1", "h3", "h4", "h5"}


  @pytest.mark.asyncio
  async def test_turn_is_persisted_before_dispatch(harness):
      await harness.crash_during_dispatch("h1")
      assert harness.reload_state().plans["h1"].turns_used == 1
  ```

  Add these named tests to the same file:

  ```text
  test_four_running_plans_have_distinct_agent_workspace_and_log_ids
  test_ready_plan_precedes_new_hypothesis
  test_human_next_is_consumed_once_without_priority_mutation
  test_attempts_count_created_plans_not_turns_or_scores
  test_out_of_order_results_use_each_frozen_reference
  ```

  Each test calls `Supervisor.run_search()` or `Supervisor.start_plan()` and
  observes public state/tree/workspace/log outputs. No harness helper may call
  `Scheduler.next_actions()` or settle an Experiment directly.

  Run: `uv run pytest test/integration/research/test_rolling_search.py -q -p no:cacheprovider`

  Expected RED: no integrated Supervisor loop.

- [ ] **GREEN 2:** Use `asyncio.wait(..., FIRST_COMPLETED)` or the existing
  AgentRuntime equivalent. Apply one transition, persist, publish a complete
  state, and refill. Delegate all order/slot decisions to `Scheduler`.

- [ ] **RED 3: settlement and recovery**

  Test historical-best settlement for submit/patience/turn exhaustion; no
  patience change for execution/provider/evaluator-infrastructure failure;
  waiting slot release; Tree-first persistence; no duplicate settlement:

  ```python
  @pytest.mark.asyncio
  async def test_tree_first_crash_settles_once(harness):
      await harness.crash_after_tree_settlement("h1", metric=0.81)
      await harness.restart()
      assert harness.final_experiment_count("h1") == 1
      assert "h1" not in harness.state.plans
  ```

  Run: `uv run pytest test/integration/research/test_search_recovery.py -q -p no:cacheprovider`

  Add named cases:

  ```text
  test_submit_settles_historical_best_not_branch_tip
  test_patience_exhaustion_settles_historical_best
  test_turn_exhaustion_with_best_settles
  test_turn_exhaustion_without_best_waits_and_releases_slot
  test_execution_provider_and_infrastructure_failures_do_not_consume_patience
  test_tree_first_crash_settles_once
  test_missing_context_or_workspace_waits_without_recreation
  ```

- [ ] **GREEN 3:** Persist Tree settlement before removing the Plan from
  state. Reconcile a final Experiment exactly once. Resume the same Plan Agent,
  context, and workspace; never invent missing frozen inputs. Only a trusted
  non-improving score increments stale rounds.

- [ ] **A.1 regression**

  ```powershell
  uv run pytest test/unit/agent/test_plan_agent.py test/unit/research/supervisor/test_supervisor.py test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py test/unit/research/supervisor/test_scheduler.py test/unit/research/supervisor/test_recovery.py test/unit/research/supervisor/test_experiment.py test/unit/research/supervisor/test_policy.py -q -p no:cacheprovider
  rg -n "class (Queue|Repository|WorkspaceManager|EventBus)|asyncio\.Queue" src/athena/research/supervisor/supervisor.py
  ```

  Expected: tests pass; static search has no matches.

- [ ] Return A.1 RED/GREEN output and files to `/root`. Do not release the
  claim. Wait for PREPARE and VALIDATE task commits, then continue A.2.

## A.2 Runtime And Phase Integration

Target public surface:

```python
class ResearchRuntime:
    async def start(self) -> None: ...
    async def message(self, text: str) -> str: ...
    def subscribe(self, emit: EmitFn) -> str: ...
    def unsubscribe(self, subscription_id: str) -> None: ...
    async def aclose(self) -> None: ...
```

- [ ] **RED 4: one authority across phases**

  ```python
  @pytest.mark.asyncio
  async def test_all_phases_use_one_authority(harness):
      await harness.runtime.start()
      await harness.drive_to_completion([0.71, 0.73, 0.72, 0.75])
      assert harness.phases == ["PREPARE", "SEARCH", "VALIDATE", "COMPLETED"]
      assert harness.state.status == "COMPLETED"
      assert harness.state_writers == {"Supervisor"}
      assert set(harness.event_kinds) == {"output", "state"}
  ```

  Run: `uv run pytest test/integration/research/test_autonomous_research.py -q -p no:cacheprovider`

  Expected RED: Runtime still composes legacy coordinator/state.

- [ ] **GREEN 4:** Move Plan creation, guidance, budgets, phase mutation,
  scheduling, recovery, and phase transitions behind `Supervisor`. Delete
  `_StateFacade`. Runtime constructs existing owners, binds callables, handles
  exact `/stop|/pause|/resume`, delegates ordinary Human messages, publishes,
  and closes. It never mutates state/tree.

- [ ] **RED 5: Human scope and Search boundary**

  Add these named tests to `test_human_plan_boundary.py`:

  ```text
  test_persistent_guidance_is_frozen_into_every_later_plan
  test_next_guidance_is_frozen_once
  test_human_data_interpretation_creates_a_later_hypothesis
  test_model_family_request_creates_a_later_hypothesis
  test_continue_three_more_attempts_extends_search_limit
  test_unlimited_waiting_plan_keeps_execution_safety_limits
  test_budget_extension_resumes_waiting_plan_before_new_plan
  test_explicit_validate_and_stop_are_applied
  test_ordinary_prose_containing_stop_is_not_a_command
  test_attempt_limit_finishes_active_plans_then_waits_or_auto_validates
  test_four_successes_do_not_stop_production_search
  ```

  Use scripted SupervisorAgent tool calls for natural language assertions; do
  not parse Chinese or English phrases in Runtime.

- [ ] **GREEN 5:** Implement Human effects through the existing
  `SupervisorActions` methods. Freeze guidance only when a new PlanInput is
  created. Keep exact `/stop`, `/pause`, `/resume` handling deterministic and
  delegate all ordinary prose to the long-lived SupervisorAgent.

- [ ] **GREEN 5: phase adapters:** Use stable `prepare`/`validate` identities.
  PREPARE commits trusted baseline/SOTA. VALIDATE starts from frozen SOTA and
  records its distinct validation commit/result.

- [ ] **State projection:** After every transition assert:

  ```text
  attempts = settled SEARCH Experiments + active SEARCH Plans
  successes = trusted successful SEARCH Experiments
  sota = ResearchTree best Experiment ID, metric, commit
  waiting = exact waiting Plan IDs and reason, else null
  ```

  Reconnect receives complete state and no old output replay.

- [ ] **Completed-command tool projection:** Add an integration case that feeds
  one completed `CommandResult` containing both stdout and stderr. Assert one
  `output` event, stderr-first combined UTF-8-safe preview of at most 512 bytes,
  and the complete redacted `output_ref` as its artifact reference. Runtime must
  not call `EventProjector.tool_output` once per stream delta and must not create
  multiple output artifacts for one command. Delete the module-level
  `events.tool_output(seq=...)`; only the runtime-owned `EventProjector`
  advances sequence numbers.

- [ ] **A.2 regression and ownership**

  ```powershell
  uv run pytest test/integration/research/test_autonomous_research.py test/integration/research/test_rolling_search.py test/integration/research/test_search_recovery.py test/integration/research/test_human_plan_boundary.py test/integration/research/test_prepare_agent_contract.py test/integration/research/test_validate_agent_contract.py test/unit/research/supervisor/test_events.py test/unit/athena_tui -q -p no:cacheprovider
  rg -n "_StateFacade|start_search_plan|fill_one_slot|research_state\.(plans|phase|status|search_limit|concurrency)\s*=" src/athena/research/runtime.py
  rg -n "pending_hypotheses|queue_order|\.priority" src/athena/research/runtime.py
  rg -n "^async def tool_output" src/athena/research/supervisor/events.py
  ```

  Expected: tests pass; all three searches have no matches.

- [ ] Return all RED/GREEN evidence, reused interfaces, and files to `/root`.
  Proposed commits: `feat: add single-writer rolling search`, then
  `feat: integrate autonomous research phases`.

Fresh A.0-A.2 RED, GREEN, focused regression, and the two scoped commits finish
Task 9. Do not request or wait for a separate implementation review.
