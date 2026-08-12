# Supervisor Autonomous Search TDD Index

> **For any implementation Agent:** This plan must be executable without chat
> history, a specific model, or private coordinator context. Read `CURRENT.md`,
> the claim row, this index's global/portable contract, the assigned TDD packet,
> and only the technical-design sections explicitly named by that packet.
> Do not read another task packet for context.
> `/root` records the claim before edits. Work on shared `main`, edit only the
> claimed files, and return the evidence contract below. Passing TDD completes
> implementation; no post-TDD review Agent or review phase is required.

**Goal:** Finish the approved autonomous Supervisor with one state writer, one
Plan/Agent/workspace per experiment, two TUI events, real LLM Agents, and no
legacy Operation/SQLite path.

## Global Rules

- Run `git branch --show-current` before edits; expected output is `main`.
- Existing uncommitted code is adopted only after its focused baseline passes.
  If required behavior already passes, record it; do not manufacture RED.
- New behavior follows RED -> expected failure -> minimal GREEN -> focused
  regression. Production code never precedes its failing test.
- `/root` serializes plan checkbox changes, staging, and commits.
- Reuse existing infrastructure. Do not add a queue, repository/store wrapper,
  service container, EventBus, phase planner, message database,
  WorkspaceManager, or second scoring/priority path.
- Production and real acceptance use repository `.env` and
  `ResponsesProvider`. Tests may inject providers and external outcomes.
- Keep exactly `TODO(dataset-view)` and `TODO(search-policy)` as future work.
- Fresh RED/GREEN/regression evidence plus a task-owned commit completes a task;
  there is no additional implementation review gate.
- Each packet is self-contained for an Agent that knows only the repository and
  the documents linked by `CURRENT.md`. Do not rely on chat history, another
  Agent's private notes, or an undocumented fixture.
- A test harness may control provider replies and external completion timing,
  but it must call production entry points. It must not implement scheduling,
  phase transitions, repair loops, settlement, or recovery on the product's
  behalf.

## Portable Agent Contract

Every packet is written for an Agent with repository access and no conversation
context. The assigned Agent follows this exact sequence:

1. Confirm `main`, the recorded claim, and an empty shared Git index. Do not
   stage or commit; `/root` serializes both operations.
2. Read the packet's owned files and run its focused baseline before editing.
3. For each requested capability, identify the current owner and closest public
   method. Reuse it when complete; add only the smallest missing behavior.
4. Add one behavior test, run it, and retain the expected RED output. An import,
   fixture, syntax, or environment error is not a valid RED.
5. Implement the minimum GREEN, rerun the focused test, then run the packet's
   complete regression command and formatter/static checks.
6. Stop on a file-ownership collision, changed dependency contract, unrelated
   regression, or missing real infrastructure. Report the concrete conflict;
   do not create a compatibility facade or edit another packet's files.
7. Return the completion package below. No reviewer is dispatched after valid
   RED, GREEN, regression, and formatting evidence.

Required completion package:

```text
Task: <packet/task ID>
Agent: <recorded claim ID>
Branch: main
Infrastructure: <capability> -> <existing owner.public_method> -> reused/extended
Files: <exact changed paths, all inside packet ownership>
RED: <exact command> -> <expected behavioral failure>
GREEN: <exact focused command> -> <passed count>
Regression: <exact command> -> <passed count>
Static/format: <exact command> -> <result>
Open blockers: none
```

An Agent must not report completion if any line is absent. `/root` verifies the
package, creates the task-owned commit, and updates the plan ledger. This is
evidence reconciliation, not a second implementation review.

## Verified Infrastructure

Fresh on shared `main` at 2026-08-12 17:08 +08:00:

| Existing owner | Command | Result |
| --- | --- | --- |
| `ResearchTree`, Elo/FIFO policy | `uv run pytest test/unit/research/test_research_tree_scheduling.py tests/test_research_tree_v2.py test/unit/research/supervisor/test_policy.py -q -p no:cacheprovider` | 55 passed |
| `LocalGitWorkspace` | `uv run pytest test/unit/test_git_workspace.py -q -p no:cacheprovider` | 13 passed |
| `PlanRunner`, execution, evaluator | `uv run pytest test/unit/research/supervisor/test_experiment.py test/integration/research/test_plan_optimization.py test/unit/execution test/unit/research/test_data_scripts.py test/unit/research/test_services.py -q -p no:cacheprovider` | 88 passed |
| `SupervisorAgent`, two-event TUI | `uv run pytest test/unit/agent/test_supervisor_agent.py test/integration/research/test_human_plan_boundary.py test/unit/athena_tui test/integration/test_tui_protocol.py -q -p no:cacheprovider` | 38 passed |
| Event/TUI focused slice | `uv run pytest test/unit/research/supervisor/test_events.py test/unit/athena_tui test/integration/test_tui_protocol.py -q -p no:cacheprovider` | 32 passed |

The assigned Agent records the existing owner and closest method before adding
an interface. Extend in place unless a focused RED proves a concrete gap.

## Test Boundary

- Use real `ResearchState`, `ResearchTree`, `LocalArtifactStore`, temporary Git
  repositories/worktrees, and `AgentRuntime` in integration tests.
- Fake only external nondeterminism: provider/Agent decisions, process results,
  independent reviewer output, and Plan-turn completion timing.
- Test helpers stay in task-owned tests or their owned `conftest.py`; never add
  production test hooks.
- Crash tests raise a test-local exception at the persistence boundary, rebuild
  production objects over the same `tmp_path`, and inspect persisted results.
- No test reads `.env` or uses the network before real acceptance.

## Functional Split And Concurrency

```text
A.1 SEARCH core ----------------+
                                +--> A.2 Runtime/phase integration
I Git restore -> S VALIDATE use -+

A.2 -> D legacy deletion -> E deterministic verification -> F real .env run
```

- A keeps one Agent and claim through A.1 and A.2. It does not hand
  `supervisor.py` or `runtime.py` to another Agent.
- I may run with A.1 because their files do not overlap. S waits for I; A.2
  waits for S. D waits for A.2. E waits for D. F waits for E.
- Claims and completion evidence remain in the active implementation plan; TDD
  packets do not duplicate Agent IDs or status.

The split is functional, not by technical layer: one Agent can understand and
finish its behavior without coordinating edits with another Agent. Only A.1
and A.2 remain one claim because both own `supervisor.py` and `runtime.py`;
splitting that ownership would make the shared checkout unsafe.

The ownership boundary is exact:

| Packet | Production ownership | Test ownership |
| --- | --- | --- |
| A.1/A.2 | `agents/plan_agent.py`, its prompt, `supervisor/supervisor.py`, `supervisor/plans.py` (frozen metric rule only), `supervisor/scheduler.py` (policy forwarding only), `supervisor/events.py`, `research/runtime.py`, and `supervisor/__init__.py` | Plan-Agent, Plan-contract, Scheduler, Supervisor, rolling SEARCH, recovery, Human-boundary, and autonomous-runtime tests named in packet A |
| I | `core/workspace.py` and `core/git_workspace.py` | `test_git_workspace.py` output-restoration and boundary tests |
| S | `supervisor/validation.py` | VALIDATE consumer integration tests named in the restoration packet |
| D | legacy Supervisor/TUI modules and their obsolete tests | architecture surface and migrated compatibility tests |
| E | no product files; a discovered defect becomes a separately claimed TDD task | deterministic/static/headless verification evidence |
| F | no product files; generated runs remain ignored | real `.env` acceptance evidence and completion documents |

An Agent stops and reports a collision if a required file belongs to another
packet. `/root` resolves the boundary in this index before either Agent edits
the file.

## TDD Packets

- **A.1/A.2 SEARCH, Supervisor, Runtime, Human, phase integration:**
  `codex_docs/2026-08-12-supervisor-search-runtime-tdd.md`
- **I/S reviewed-output restoration and VALIDATE simplification:**
  `codex_docs/2026-08-12-supervisor-validation-output-restoration-tdd.md`
- **C independent VALIDATE reference contract:**
  `codex_docs/2026-08-12-supervisor-validate-tdd.md`
- **D deletion, E deterministic verification, and F real acceptance:**
  `codex_docs/2026-08-12-supervisor-cleanup-acceptance-tdd.md`

## Prerequisite Gate

Before assigning A/I/S, `/root` completes the active-plan reconciliation for
ResearchTree/Elo, Git checkpoints, PlanRunner, Scheduler/Recovery,
SupervisorAgent, and two-event TUI, then reruns all commands above.

State snapshot coverage must prove:

```text
attempts = settled SEARCH Experiments + active SEARCH Plans
successes = trusted successful SEARCH Experiments
sota = ResearchTree best Experiment ID, metric, commit
waiting = exact waiting Plan IDs and reason, else null
```

Static TUI check:

```powershell
rg -n "agent_messages|\.athena.?sessions|STATUS|REQUESTS_GET|HUMAN_REPLY" src/athena_tui
```

Expected: no active match.

Before dispatch, `/root` also verifies that every required producer exists or
is explicitly produced by one packet. The cross-packet interfaces are limited
to:

```python
register_plan_agent(...)
run_prepare_plan(...) -> PrepareResult
run_validation_plan(...) -> ValidationResult
Supervisor.start() / Supervisor.message() / Supervisor.stop()
ResearchRuntime.start() / message() / subscribe() / unsubscribe() / aclose()
```

No Agent may introduce a second phase runner, state writer, scheduler, scoring
service, workspace owner, or event protocol to avoid one of these interfaces.

## Completion

After all packets have fresh evidence and real Titanic acceptance completes,
write the final report, delete the completed implementation plan and these TDD
packets, and update `CURRENT.md` to point only to the final report.
