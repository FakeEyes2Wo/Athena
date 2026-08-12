# Supervisor Autonomous Search Implementation Plan

> **Implementation status:** IN PROGRESS
> **Implementation owner:** Codex agent `/root`
> **Claimed at:** 2026-08-12 15:39:46 +08:00
> **Active task:** T13-P2 PREPARE readable output projection
> **Task implementation owner:** Codex agent `/root/t13_prepare_display_fix`
> **Plan monitor:** ACTIVE; `scripts/watch_codex_plans.ps1` checks every 5 minutes
> **Concurrency guard:** Do not claim or implement this plan concurrently.
> Dependency-ready tasks may run only after `/root` assigns their non-overlapping
> file ownership. Coordinate with `/root` before editing plan-owned files.

> **Shared-checkout rule:** Every Agent works directly on `main`. One Agent may
> claim exactly one task, and each task may be modified by exactly one Agent.
> Only `/root` may fill, transfer, or clear the claim fields below and perform
> plan updates, staging, or commits. An empty claim blocks edits to that task.

> **For assigned implementation Agents:** Follow the assigned task's infrastructure
> audit and red-green-regression steps exactly, then return fresh command evidence
> to `/root` for checkbox updates. A passing TDD cycle is the implementation gate.

**Goal:** Replace the Operation/SQLite Supervisor with an infrastructure-first, one-Plan/one-Agent autonomous runtime that supports rolling concurrent SEARCH, natural-language Human guidance, trusted patience optimization, recovery, and a two-event TUI.

**Architecture:** `ResearchRuntime` composes one deterministic single-writer `Supervisor`, one long-lived `SupervisorAgent`, existing `AgentRuntime`, `LocalGitWorkspace`, `ResearchTree`, `ExecutionRuntime`, and `TrustedEvaluator`. Unfinished work is atomically stored in `.athena/state.json`; completed research is stored in `.athena/research_tree.json`; structured Agent recovery logs live in `.athena/logs/agents`. SEARCH uses stable Hypothesis IDs for Plan, Agent, workspace, and log identity.

**Tech Stack:** Python 3.11+, asyncio, Pydantic, pytest/pytest-asyncio, prompt-toolkit, Git worktrees, existing Athena ArtifactStore/AgentRuntime/ExecutionRuntime/TrustedEvaluator, real OpenAI-compatible provider loaded from `.env`.

**Technical design:** `codex_docs/2026-08-12-supervisor-autonomous-search-technical-design.md`

**Executable TDD and file ownership:**
`codex_docs/2026-08-12-supervisor-autonomous-search-execution-tdd.md`. Its
Task A.1/I/S/A.2 decomposition is authoritative when this older task narrative
lists overlapping files: one Task 9 Agent owns `supervisor.py` and `runtime.py`
through A.1 and A.2; T11-I/T11-S never edit those files.

## Global Constraints

- Before every task, inspect the existing owner and interface listed in Technical Design section 3. Record the reused interface in the task's completion evidence.
- Add a new abstraction only when the existing owner lacks a concrete required behavior; extend the current interface when ownership is already correct.
- Preserve unrelated worktree changes. Stage only files owned by the task.
- Before every task action, verify `git branch --show-current` returns `main`.
- Do not create or use a feature branch or worktree for this plan.
- Each Agent may appear in exactly one non-empty claim row. Each task has exactly
  one claim row and one modifying Agent for its entire implementation lifecycle.
- Agents may not self-claim. `/root` records the Agent ID and claim time before
  the Agent edits any task file; `/root` also serializes all staging and commits.
- Use TDD: add the focused failing test, verify the expected failure, implement the smallest behavior, then run the affected regression slice.
- A task is complete after its infrastructure audit, failing-test evidence,
  passing focused and regression tests, checkbox update, and task commit. Do
  not add a post-TDD implementation review stage. Product-level Git diff
  validation and VALIDATE's independent LLM check remain required behavior.
- Production and real acceptance use the repository `.env` and a real provider. Deterministic tests may inject fakes.
- SEARCH defaults: 10 attempts, concurrency 4, maximum 12 Plan turns, maximum patience 5.
- A Human may explicitly extend a waiting Plan or remove its turn limit; execution time/cost/cancellation safety limits remain active.
- TUI receives only `output` and `state`; it never reads logs or session files.
- Dataset-view work is out of scope and must be represented only by the concentrated TODO from the Technical Design.
- Do not restore deleted historical plans or documentation. Update only this active plan and `codex_docs/CURRENT.md`.
- After all acceptance criteria have fresh evidence: write `codex_docs/2026-08-12-supervisor-autonomous-search-final-report.md`, delete this implementation plan, and remove its pointer from `codex_docs/CURRENT.md`.

## Task Sets And Claim Ledger

Existing worktree changes do not constitute a claim. The newly assigned Agent
must audit and adopt or correct those changes within the claimed task. Claim
fields intentionally start blank. `/root` fills a claim before dispatch and
clears it only after the task evidence and task-owned commit are complete.

### Set M: Completion Maintenance

This maintenance task is independent of implementation sets and owns only the
completion ledger and deletion of completed entries from this plan. It does not
count as a product implementation task and never edits product or test files.

| Task | Scope | Agent claim | Claimed at | Status |
| --- | --- | --- | --- | --- |
| M1 | Verify completed tasks, record them, and delete completed task-list/detail entries | `/root_2_u` | 2026-08-12 16:44:04 +08:00 | Claimed; audit every 5 minutes while this plan is active |

### Set H: Deterministic Verification

| Task | Scope | Agent claim | Claimed at | Status |
| --- | --- | --- | --- | --- |
| T13-S | Settle SEARCH Plan-turn exceptions before phase advancement | `/root/t13_search_failure_audit` | 2026-08-12 23:31:15 +08:00 | Claimed; TDD fix in progress |
| T13-P2 | Project one readable PREPARE decision and normalize display newlines | `/root/t13_prepare_display_fix` | 2026-08-12 23:34:00 +08:00 | Claimed; TDD fix in progress |
| T13 | Full deterministic verification and infrastructure audit | __________ | __________ | Paused after defect discovery; restart from Step 1 after T13-S |

### Set I: Real Acceptance And Completion

| Task | Scope | Agent claim | Claimed at | Status |
| --- | --- | --- | --- | --- |
| T14 | Real-LLM Titanic acceptance and completion | __________ | __________ | Unclaimed; run after T13 |

### Claim Invariants

- A claim is valid only when Agent ID and claim time are both non-empty and the
  Agent has no other claim in this ledger.
- The claimed Agent is the only Agent allowed to modify that task's product or
  test files. Review-only Agents must not edit those files and are separate
  tasks only when `/root` explicitly adds a claim row.
- A task keeps the same modifying Agent until completion. Transfer requires
  `/root` to stop the old Agent, record the transfer in Status, and then replace
  the claim; two Agents never modify one task concurrently or sequentially
  without an explicit recorded transfer.
- Every Agent verifies `main` before work and stops immediately if the branch
  changes. `/root` checks this again before accepting evidence or committing.
- `/root` alone edits this ledger and task checkboxes. Task Agents return exact
  RED/GREEN commands, results, and changed-file lists without staging them.

### Completion-Maintenance Authority

`/root_2_u` is the sole exception to `/root`-only plan editing, limited to task
completion maintenance. It may:

- read the active plan, current task evidence, task-owned commits, and fresh
  verification output on `main`;
- append a completed-task record to `codex_docs/COMPLETED.md`;
- remove the completed task's row from the claim ledger and its detailed task
  section from this active plan after the record is durable;
- delete `codex_docs/finished_*.md` temporary files only after all unique
  evidence from them is preserved in `COMPLETED.md`;
- update cross-set dependency text only to replace a removed completed task
  with its `COMPLETED.md` record reference.

It may not modify product/test code, delete an unfinished or unverified task,
delete the active plan, delete the final completion report, change claims for
active tasks, stage/commit another task's files, or operate off `main`.

A task is removable only when all of the following are present:

1. the recorded unique modifying Agent has returned final evidence;
2. fresh focused and required regression commands pass on `main`;
3. `/root` has created a task-owned commit with no unrelated files;
4. the active plan acceptance steps are satisfied with no known open finding;
5. `COMPLETED.md` records task ID, owner, commit, commands/results, files, reuse
   evidence, completion time, and any retained follow-up.

If any condition is missing, `/root_2_u` records nothing and leaves the task in
the active lists with a concise blocker status.

---
> **Archived baseline (2026-08-12):** Tasks 1-5 had their detailed sections
> removed before reconciliation. Completed reconciliations R1-T1 through R1-T4,
> R2-T5, Tasks 6 through 12, T11-I, and T11-S are recorded in
> `codex_docs/COMPLETED.md`.

### Task 13: Full Deterministic Verification And Infrastructure Audit

**Files:**
- Modify tests only for defects found in target behavior.
- Update checkboxes in: `codex_docs/2026-08-12-supervisor-autonomous-search-implementation-plan.md`

**Dependencies:** completed `COMPLETED.md` entry T12.

**Completion gate:** Fresh deterministic tests and audit evidence complete this
task. There is no implementation review phase.

> **Paused:** A deterministic audit reproduced an unhandled SEARCH Plan-turn
> exception leaving an active Plan while auto mode advances to VALIDATE. T13-S
> owns that TDD fix. Human TUI acceptance also found raw structured PREPARE
> deltas rendering as empty `* agent` rows and CRLF previews exposing `^M`;
> T13-P2 owns that non-overlapping TDD fix. T13 must restart from Step 1 after
> both tasks complete.

**Infrastructure audit:** Repeat Technical Design section 3's matrix against the final code. Search for duplicate owners, direct subprocess scoring, alternative artifact stores, TUI file reads, old `.athena/sessions`, SQLite Supervisor imports, and workspace managers.

- [ ] **Step 1: Run static duplicate-path searches**

```powershell
rg -n "sqlite3|SupervisorOperation|OperationType|HumanRequest|REQUESTS_GET|HUMAN_REPLY|MESSAGES_GET|read_new_messages|\.athena.?sessions|WorkspaceManager" src/athena/research src/athena_tui
rg -n "create_subprocess|subprocess\." src/athena/research/supervisor
rg -n "TrustedEvaluator|ExecutionRuntime|LocalGitWorkspace|LocalArtifactStore|AgentRuntime" src/athena/research/supervisor src/athena/research/runtime.py
```

Expected: forbidden legacy searches return no active target-path matches; infrastructure searches show the intended owners.

- [ ] **Step 2: Run formatting and type/import checks configured by the repository**

Discover commands from `pyproject.toml`; run the repository's configured formatter/linter/type checker without adding new tooling. At minimum:

```powershell
uv run black --check src test
```

- [ ] **Step 3: Run the complete deterministic test suite**

Run: `uv run pytest -q`

Expected: all tests pass with no skipped target Supervisor integration tests.

- [ ] **Step 4: Run a fresh deterministic headless execution**

Use the existing headless runner interface discovered in `scripts/run_headless.py`, with injected deterministic Agent doubles only for this verification. Expected: PREPARE -> rolling SEARCH -> VALIDATE -> COMPLETED, and captured event kinds equal `{"output", "state"}`.

- [ ] **Step 5: Record the infrastructure audit in the plan**

For every matrix row in Technical Design section 3, record the final file/interface actually used and any narrow extension. If a duplicate remains, remove it and rerun Steps 1-4 before checking this item.

- [ ] **Step 6: Commit verification fixes only if required**

```powershell
git add <only-files-fixed-by-this-task> codex_docs/2026-08-12-supervisor-autonomous-search-implementation-plan.md
git commit -m "test: verify autonomous supervisor integration"
```

### Task T13-S: Settle SEARCH Plan-Turn Exceptions Before Phase Advancement

> **Agent claim:** `/root/t13_search_failure_audit`  **Claimed at:** 2026-08-12 23:31:15 +08:00

**Files:**
- Modify: `src/athena/research/supervisor/supervisor.py`
- Test: `test/integration/research/test_rolling_search.py`

**Dependencies:** completed `COMPLETED.md` entries T12 and T13-P; discovered by T13.

**Infrastructure audit:** Keep failure lifecycle ownership in the single-writer
`Supervisor`; reuse existing Plan settlement/state publication. Do not change
provider, runtime, TUI, scheduler accounting, or add a second failure owner.

- [ ] **Step 1: RED reproduces an exception leaving an active unclosed Plan**
- [ ] **Step 2: GREEN settles or waits the failed Plan before limit evaluation**
- [ ] **Step 3: Regression proves auto mode cannot validate unfinished SEARCH**
- [ ] **Step 4: Commit only T13-S files and evidence**

### Task T13-P2: Project Readable PREPARE Decisions Without Empty Agent Rows

> **Agent claim:** `/root/t13_prepare_display_fix`  **Claimed at:** 2026-08-12 23:34:00 +08:00

**Files:**
- Modify: `src/athena/research/supervisor/prepare.py`
- Modify: `src/athena/research/supervisor/events.py`
- Test: `test/integration/research/test_prepare_agent_contract.py`
- Test: `test/unit/research/supervisor/test_events.py`

**Dependencies:** completed `COMPLETED.md` entry T13-P; Human TUI acceptance
rejected its raw structured-delta display behavior.

**Infrastructure audit:** Keep PREPARE semantics in `run_prepare_plan()` and
reuse the validated `PlanDecision.reason` plus the existing `EmitEvent` callback.
Keep display safety/normalization in `EventProjector`; do not modify the TUI,
provider, AgentRuntime, task-seeding work, public event kinds, or artifact logs.

- [ ] **Step 1: RED reproduces empty Agent rows and missing readable PREPARE reason**
- [ ] **Step 2: GREEN publishes one validated decision reason per PREPARE run**
- [ ] **Step 3: GREEN normalizes CRLF/CR in display text without adding event kinds**
- [ ] **Step 4: Regression proves tools remain visible and raw PlanDecision JSON is hidden**
- [ ] **Step 5: Commit only T13-P2 files and evidence**

### Task 14: Real-LLM Titanic Acceptance And Completion

**Files:**
- Create a fresh acceptance project under an ignored/generated execution directory selected by the existing headless runner; do not commit generated model outputs.
- Create: `codex_docs/2026-08-12-supervisor-autonomous-search-final-report.md`
- Modify: `codex_docs/CURRENT.md`
- Delete after success: `codex_docs/2026-08-12-supervisor-autonomous-search-implementation-plan.md`

**Dependencies:** Task 13.

**Completion gate:** Fresh real `.env` evidence plus the final deterministic
rerun complete this task. There is no implementation review phase.

**Infrastructure audit:** Use `athena.core.agent.settings` for `.env`, `ResponsesProvider` for LLM calls, the existing headless entrypoint, and the production Supervisor composition. Do not add an acceptance-only provider, fixed solution generator, fake `model.py`, or alternate runner.

- [ ] **Step 1: Preflight the real environment without printing secrets**

Verify only boolean credential presence and resolved model/base URL source. Confirm the acceptance directory is fresh, Titanic data is available, target is `Survived`, and configuration resolves to attempts 10/concurrency 4.

- [ ] **Step 2: Start the real headless execution**

Invoke the repository's production headless runner with the fresh project and natural-language task. Use `.env`; do not pass a fake client. Capture the output/state stream and process exit status.

- [ ] **Step 3: Persist through the entire execution**

Do not stop after the first score. Continue until one terminal condition:

- success: VALIDATE and COMPLETED;
- explicit Human/user cancellation;
- execution-level safety timeout/cost limit;
- unrecoverable external provider failure after the runtime's configured retry behavior.

If a product defect appears, add a deterministic failing regression test, fix through the existing infrastructure owner, rerun Task 13, then restart acceptance in a new fresh project.

- [ ] **Step 4: Verify acceptance artifacts**

Check programmatically:

```text
PREPARE trusted baseline exists
SEARCH plans created <= 10
trusted successful SEARCH experiments >= 4
baseline excluded from the four
ResearchTree SOTA has trusted evidence and commit
VALIDATE has original SOTA commit and distinct validation commit
final trusted metric, predictions, and evidence exist
terminal phase/status are COMPLETED
event kinds are exactly output/state
no TUI/session-file polling occurred
```

- [ ] **Step 5: Re-run the complete test suite after any acceptance fixes**

Run: `uv run pytest -q`

- [ ] **Step 6: Write the final report**

Include:

- exact commit under test;
- commands used, with secrets omitted;
- full deterministic test count and result;
- PREPARE baseline metric;
- number of SEARCH attempts, successes, failures, and each trusted metric;
- final SOTA and validation commit IDs;
- final metric and artifact paths/refs;
- event-kind verification;
- infrastructure audit table;
- remaining scoped TODOs (`dataset-view`, `search-policy`) and no other target placeholders.

- [ ] **Step 7: Close the active plan according to AGENTS.md**

Delete this implementation plan and replace `codex_docs/CURRENT.md` content with:

```markdown
# Current Athena Work

No active implementation plan.

Latest completion report:
- `codex_docs/2026-08-12-supervisor-autonomous-search-final-report.md`
```

- [ ] **Step 8: Commit only completion-owned documentation**

```powershell
git add codex_docs/CURRENT.md codex_docs/2026-08-12-supervisor-autonomous-search-final-report.md
git add -u codex_docs/2026-08-12-supervisor-autonomous-search-implementation-plan.md
git commit -m "docs: complete autonomous supervisor plan"
```

## Plan Self-Review

- Spec coverage: Tasks 1-14 cover persistence, ResearchTree reuse, policy,
  Agent recovery, Git checkpoints, manifest execution, Human input, TUI,
  rolling SEARCH, PREPARE, VALIDATE, deletion, deterministic verification, and
  real-LLM acceptance.
- Infrastructure coverage: every implementation task has an explicit audit and
  names the existing owner/interface it must reuse.
- Dependency consistency: Wave 1 produces all contracts and extensions used by
  Waves 2-4; cross-task public names are defined in `Interfaces` blocks.
- Placeholder scan: no unresolved placeholder or open implementation choice remains. The two
  future TODOs are named and intentionally out of scope.
- Completion gate: the plan cannot be deleted until both the full deterministic
  suite and real-LLM acceptance have fresh evidence.
