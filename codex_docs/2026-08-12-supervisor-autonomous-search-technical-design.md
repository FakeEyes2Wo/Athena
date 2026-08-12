# Supervisor Autonomous Research - Technical Design

Status: approved for implementation

Date: 2026-08-12

## 1. Purpose

Replace the current fine-grained Supervisor Plan/Operation machinery with a
small autonomous research runtime that can complete PREPARE, concurrent SEARCH,
and VALIDATE using real LLM agents.

The design optimizes for low cognitive load:

- one Plan owns one Agent and one workspace;
- the long-lived SupervisorAgent owns global research context;
- PlanAgents own implementation, execution, debugging, and local optimization;
- ResearchTree remains the only owner of hypotheses, final experiments, scores,
  lineage, evidence, and SOTA;
- `state.json` contains only unfinished execution state;
- no Supervisor database, Operation DAG, message queue, or lease protocol;
- TUI receives only `output` and `state` events.

## 2. Scope

### In scope

- Simplify and split `athena.research.supervisor` around coarse Plans.
- Preserve Supervisor-owned Git workspaces.
- Run PREPARE through one autonomous PrepareAgent.
- Run SEARCH with up to four concurrent, independently recoverable PlanAgents.
- Schedule hypotheses by a replaceable priority policy; ship single-sided Elo.
- Support natural-language Human guidance and control through SupervisorAgent.
- Preserve trusted scoring and exactly-once logical final validation.
- Move recoverable Agent conversations to `.athena/logs/agents/*.jsonl`.
- Replace TUI file polling and the current event surface with `output`/`state`.
- Run a real-LLM Titanic acceptance execution from repository `.env` settings.

### Out of scope

- Incremental dataset views, DuckDB, Parquet, or candidate-private data views.
- Multiple Supervisor processes controlling one project.
- Distributed workers or remote workspace coordination.
- A generic scheduler plugin registry.
- Replaying old token deltas after reconnect.
- Allowing VALIDATE to improve the model using final-test information.

Keep one concentrated extension marker:

```python
# TODO(dataset-view): Add candidate-private incremental dataset views after
# manifest-based autonomous SEARCH is stable and accepted end to end.
```

## 3. Existing Infrastructure To Keep

The implementation must extend existing infrastructure instead of introducing
parallel abstractions.

### ResearchTree

Keep the existing Hypothesis/Experiment model in
`src/athena/core/research_tree.py`.

- Hypothesis represents a testable research claim before execution.
- Experiment represents its execution and final trusted result.
- Experiment parent links already represent code/result lineage.
- ResearchTree already owns score evidence, artifacts, commits, terminal
  experiment status, and SOTA.
- `save()` already performs atomic JSON replacement.

Do not merge these objects into a new ResearchNode and do not create another
experiment table.

### Other retained infrastructure

- `AgentRuntime`: create, follow up, resume, wait, and stream Agent turns.
- `LocalGitWorkspace`: create/recover worktrees, review diffs, and commit.
- `LocalArtifactStore`: immutable Plan inputs, evidence, predictions, reports,
  full tool output, and trusted best-result records.
- `ExecutionRuntime`: bounded command execution in a project workspace.
- `TrustedEvaluator` and `DataScriptRunner`: scoring with frozen labels.
- Existing EvalSpec direction and numeric tie tolerance.
- Existing execution duration and cancellation limits.

### Infrastructure-first compatibility gate

Infrastructure reuse is a completion requirement, not a preference. Before
implementing each feature, the assigned implementation Agent must inspect the
current owner and answer these questions in the task evidence:

1. Which existing component already owns the capability?
2. Which existing public method or contract is the closest integration point?
3. Can the requirement be met by extending that interface without adding a
   parallel abstraction?
4. If not, what concrete missing behavior makes a new component necessary?

The implementation must use the current interface when it is complete. It may
make a narrow, backward-compatible extension when the owner is correct but one
capability is missing. It must not wrap a complete interface merely to rename
it or create a second owner.

Required first-choice interfaces:

| Capability | Existing owner/interface | Permitted change |
| --- | --- | --- |
| Agent create/follow-up/resume/wait | `AgentRuntime` | Add caller-supplied stable Agent IDs and event forwarding only. |
| Agent context persistence | `RolloutRecorder`, `resume_context_sync`, `ContextManager` | Move the deterministic JSONL path to `logs/agents`; do not invent another transcript format. |
| Agent construction and tools | `AgentTypeRegistry`, `BaseAgentRunner`, `RunToolProjector` | Register Prepare/Supervisor/Plan/Validate roles through existing factories and projections. |
| Artifacts | `LocalArtifactStore` through `ArtifactStore` | Store new immutable payloads through existing `put_*`/`get_*` APIs. |
| Workspace lifecycle | `LocalGitWorkspace` through `GitWorkspace` | Extend review/commit for iterative checkpoints; do not add a WorkspaceManager. |
| Command execution | `ExecutionRuntime` | Execute manifest argv through its bounded workspace-aware API. |
| Trusted scoring | `TrustedEvaluator`, `DataScriptRunner` | Adapt Plan outputs to the existing evaluator; do not add a scoring path. |
| Research history | `ResearchTree` | Add only scheduling and selective-inheritance fields/methods. |
| Runtime subscription | `ResearchRuntime.subscribe/unsubscribe` | Normalize published records to `output` and `state`; do not add a second event bus. |
| Provider and `.env` settings | `ResponsesProvider`, `athena.core.agent.settings` | Require the existing real provider path in production/acceptance. |
| Execution safety | `ExecutionRuntime`, execution monitor limits | Reuse timeout/cancellation signals; do not create Plan-local process monitors. |

At completion, the same audit is repeated against the final code. A feature is
not accepted if it bypasses a complete existing facility, duplicates ownership,
or leaves both old and new active paths without an explicit compatibility
consumer.

## 4. Target Architecture

```text
ResearchRuntime
|- Supervisor                    single deterministic state writer
|- SupervisorAgent               long-lived global research context
|- AgentRuntime                  concurrent Agent turns and recovery
|- LocalGitWorkspace             Supervisor-owned worktrees
|- ResearchTree                  durable research history and SOTA
|- HypothesisPolicy              scheduling boundary
`- TrustedEvaluator              only trusted score producer
```

There is one Supervisor process per project. It owns the asyncio scheduling
loop and is the only writer of `state.json` and `research_tree.json`. Parallel
Agents return results to this loop; they never write either authority file.

### Responsibilities

#### SupervisorAgent

- receives every ordinary Human message;
- maintains global task and research context in its Agent log;
- reads ResearchTree snapshots and completed Plan evidence;
- generates, deduplicates, and accepts hypotheses;
- selects `supersedes` relationships;
- assigns Plan turn and patience budgets within configured limits;
- may single-shot prioritize an explicitly requested next hypothesis;
- proposes subsequent hypotheses after Plan settlement.

SupervisorAgent does not directly mutate ResearchTree, start processes, commit
Git, score results, or bypass configured safety limits. It uses narrow tools
implemented by Supervisor; the deterministic side validates every request.

#### Supervisor

- freezes Plan inputs;
- schedules Plans and fills concurrency slots;
- owns workspaces and Git commits;
- validates and runs `experiment.json`;
- calls TrustedEvaluator;
- maintains Plan budgets and patience;
- settles hypotheses and updates ResearchTree;
- emits TUI events;
- reconciles persisted state after a crash.

#### PlanAgent

- owns exactly one Plan and one hypothesis;
- creates any number of project files in its workspace;
- implements, runs, debugs, and repairs its approach;
- uses trusted metric feedback to optimize the same hypothesis;
- returns `continue`, `submit`, or `abandon`;
- may suggest future hypotheses but cannot register or schedule them.

PlanAgent is registered through the existing `AgentTypeRegistry`, constructed
as the existing prompt-driven `Agent`, adapted through `BaseAgentRunner`, and
given `generic_tool_registry` for its named workspace. It is not a new runtime
class and does not own phase or persistence state.

## 5. Project Persistence

```text
.athena/
|- state.json
|- research_tree.json
|- artifacts/
|- logs/
|  `- agents/
|     |- supervisor.jsonl
|     |- prepare.jsonl
|     |- <hypothesis_id>.jsonl
|     `- validate.jsonl
`- workspaces/
   |- prepare/
   |- <hypothesis_id>/
   `- validate/
```

### Files and ownership

`research_tree.json` contains durable scientific history. Completed Plans are
represented by final Experiments and Hypothesis evidence.

`state.json` contains only unfinished execution state and current Research
configuration. A completed Plan is removed immediately after its ResearchTree
settlement is durably saved.

Agent JSONL logs are the sole Agent conversation persistence and context
recovery source. They retain structured roles, tool calls, tool results, and
compaction checkpoints. The existing `.athena/sessions` path and the duplicate
plain-text `BaseAgentRunner` logs are removed.

The TUI never reads Agent logs or any project file.

### No SQLite

Delete the Supervisor database and all associated abstractions:

- operations;
- facts;
- dispatch records;
- HumanRequest rows;
- executions and Plan history rows;
- final-test attempt rows;
- leases, generations, revisions, and CAS wrappers;
- Store/Journal/Repository families built only for that schema.

Multiple Supervisor processes for one project are unsupported. A second start
must fail fast while the first local process is active; this is a process
lifecycle check, not a durable lease protocol.

## 6. Persistent State Contract

Illustrative `state.json`:

```json
{
  "status": "RUNNING",
  "phase": "SEARCH",
  "search_limit": 10,
  "concurrency": 4,
  "plans": {
    "hyp_vit_01": {
      "kind": "SEARCH",
      "context_ref": "artifact:plan-input",
      "turns_used": 4,
      "turn_limit": 12,
      "patience": 4,
      "stale_rounds": 1,
      "best_ref": "artifact:plan-best"
    }
  },
  "validation": null
}
```

`status` is one of `RUNNING`, `WAITING`, `COMPLETED`, or `STOPPED`.

`phase` is one of `PREPARE`, `SEARCH`, `VALIDATE`, or `COMPLETED`. It is kept
as an explicit high-level checkpoint; the implementation must not add a second
fine-grained phase machine.

`plans` contains every unsettled Plan. PREPARE and VALIDATE use fixed keys
`prepare` and `validate`. A SEARCH Plan key is its Hypothesis ID.

The live TUI status of an Agent, tool, or workspace is not persisted here. It
is a runtime projection.

### Plan state

```python
class PlanState:
    kind: Literal["PREPARE", "SEARCH", "VALIDATE"]
    context_ref: ArtifactRef
    turns_used: int
    turn_limit: int | None
    patience: int | None = None
    stale_rounds: int = 0
    best_ref: ArtifactRef | None = None
```

`turn_limit=None` means Human explicitly requested unlimited Plan turns. The
Plan remains subject to execution-level time, cost, platform cancellation, and
Human stop limits.

Only SEARCH uses `patience`, `stale_rounds`, and `best_ref`. PREPARE and
VALIDATE omit them in serialized JSON.

### Plan input artifact

Plan inputs are immutable after creation:

```python
class PlanInput:
    hypothesis: Hypothesis | None
    active_ancestor_hypotheses: list[Hypothesis]
    reference_experiment_id: str | None
    reference_metric: float | None
    reference_priority: float
    evaluator_ref: ArtifactRef
    tree_ref: ArtifactRef
    human_context: str
    initial_turn_limit: int | None
    initial_patience: int | None
```

PREPARE and VALIDATE use phase-specific optional fields but the same immutable
artifact principle. Reference experiment, metric, priority, and Human context
must never be recomputed from newer global state for an existing Plan.

### Trusted best artifact

```python
class PlanBest:
    metric: float
    commit: CommitHash
    evidence_ref: ArtifactRef
```

Every trusted improvement updates this immutable artifact and then atomically
updates `best_ref` in `state.json`.

## 7. Stable Identity

For SEARCH only:

```text
hypothesis_id
= Plan ID
= Agent session ID
= workspace ID
= Agent log ID
```

This removes Plan-to-Agent and Plan-to-workspace mapping state.

AgentRuntime must allow a caller-supplied stable Agent ID. Creation and resume
must be idempotent for the same Hypothesis ID.

The final Experiment retains its own existing Experiment ID. ResearchTree must
enforce that one Hypothesis can settle at most one Experiment.

## 8. ResearchTree Extensions

Preserve the existing ResearchTree API and persistence shape as far as
possible. Extend Hypothesis only with scheduling and selective inheritance
data:

```python
class Hypothesis:
    # existing statement/intervention/expected_effect/evidence fields
    parent_id: str | None              # base Experiment ID
    supersedes: list[str]              # ancestor Hypothesis IDs
    priority: float = 1000.0
    order: int                         # stable FIFO tie-break
    patience: int
    turn_limit: int | None
```

The existing Hypothesis status continues to represent scientific settlement.
Whether a proposed Hypothesis is active or waiting is derived from
`state.json.plans`; do not duplicate an execution status in ResearchTree.

### Selective inheritance

Every ResearchTree Experiment remains based on one parent Experiment. The
effective hypothesis set for a child is:

```text
all hypotheses on the parent Experiment path
- explicitly superseded hypothesis IDs
+ the child hypothesis
```

Example:

```text
H1: Age missingness is informative             keep
H2: family-size feature is useful              keep
H3: XGBoost is suitable                        supersede
H4: tree depth 6 is suitable                   supersede
H5: ViT is more suitable                       add
```

H5 uses the H4 Experiment as its code base but declares
`supersedes=[H3, H4]`. Data hypotheses H1 and H2 remain active.

Plan-local tuning such as learning-rate changes or encoder selection does not
create ResearchTree nodes. It is optimization within the H5 Plan. A new node
is created only when SupervisorAgent proposes a new testable research claim.

## 9. Hypothesis Scheduling

### Policy boundary

```python
class HypothesisPolicy(Protocol):
    def seed(self, parent: Hypothesis | None) -> float: ...
    def priority(self, hypothesis: Hypothesis, tree: ResearchTree) -> float: ...
    def settle(self, reference_priority: float, outcome: Outcome) -> float: ...
```

Ship only `EloPolicy`. Do not add registration, discovery, generic option
dictionaries, or policy persistence.

```python
# TODO(search-policy): Replace EloPolicy with an evidence-aware scheduling
# policy after enough real-search traces exist. Keep Supervisor dependent only
# on HypothesisPolicy.
```

### Single-sided Elo

- Root priority: `1000`.
- Child seed: its parent Hypothesis settled priority.
- Plan creation freezes the reference priority.
- `K=32`; a newly seeded child has expected score `0.5`.
- WIN: reference priority + 16.
- DRAW: reference priority.
- LOSS: reference priority - 16.
- Only the candidate Hypothesis is updated. The reference Hypothesis never
  changes after settlement.

This single-sided rule makes concurrent completion order irrelevant.

Outcome uses the Plan's final trusted metric against its frozen reference and
the frozen EvalSpec direction/tolerance. Abandonment or exhausted work with no
trusted result is LOSS. Infrastructure interruption is not an outcome.

### Queue order

Normal queue ordering is:

```text
policy priority descending
then Hypothesis order ascending
```

`order` is a monotonic integer assigned when the Hypothesis enters
ResearchTree. Equal-priority hypotheses therefore behave as FIFO.

### Human single-shot priority

An explicit instruction such as "try ViT next" may cause SupervisorAgent to
select or create that Hypothesis for the next available new Plan. It bypasses
the policy once but does not alter long-term priority.

Scheduling precedence is:

1. resumable READY Plans;
2. one explicitly requested next Hypothesis;
3. normal policy/FIFO queue;
4. request exactly enough new hypotheses from SupervisorAgent to fill slots.

No fixed hypothesis pool size or generation batch size exists.

## 10. Rolling Concurrent SEARCH

Default environment configuration:

```env
ATHENA_MAX_SEARCH_ATTEMPTS=10
ATHENA_SEARCH_CONCURRENCY=4
ATHENA_MAX_PLAN_TURNS=12
ATHENA_MAX_PLAN_PATIENCE=5
```

Task configuration may override defaults for a Research execution. Human may
change the current execution. SupervisorAgent assigns each new Hypothesis a
turn limit and patience within the configured maxima.

SEARCH uses rolling slot filling:

```text
while attempts remain:
    settle every completed Plan serially
    resume READY existing Plans first
    create highest-priority new Plans until slots are full
    wait for any Plan to return
```

When one Plan finishes, the scheduler does not wait for the other concurrent
Plans. It settles the result, asks SupervisorAgent for derived hypotheses when
needed, reranks candidates, and immediately fills the free slot.

The attempt count is the number of SEARCH Plans created, not successful scores
and not Agent turns. A Plan consumes exactly one attempt regardless of how many
debugging or trusted-scoring turns it uses. Baseline PREPARE does not count.

The production runtime does not require four successful experiments. The
four-success rule belongs only to real-LLM acceptance.

### PlanAgent result

Every Agent turn returns:

```python
class PlanDecision:
    decision: Literal["continue", "submit", "abandon"]
    reason: str
    suggestions: list[str] = []
```

- `continue`: validate and score the current implementation, return evidence
  to the same Agent, and allow further optimization.
- `submit`: stop exploring and settle the historically best trusted commit.
- `abandon`: settle LOSS only if there is no trusted best; if a trusted best
  exists, settle that best instead.

Ordinary text must never be parsed as an implicit abandon action.

### Turn budget

One Agent follow-up consumes one Plan turn. Tool calls, LLM internal samples,
command executions, and evaluator calls do not independently consume turns.

Increment `turns_used` and atomically save `state.json` before starting the
turn. A crash therefore cannot restore spent turn budget.

Every turn tells the Agent total and remaining turn budget, patience,
stale-round count, and execution deadline.

### Patience

Patience applies after the first trusted score and represents consecutive
trusted optimization rounds without significant Plan-local improvement.

- First trusted score sets `best_ref` and `stale_rounds=0`.
- A trusted score significantly better than the Plan's historical best commits
  the revision, updates `best_ref`, and resets stale rounds to zero.
- A trusted score without significant improvement increments stale rounds.
- Command failure, missing output, provider failure, cancellation, and process
  recovery do not affect stale rounds.
- Agent may submit before patience expires.
- When `stale_rounds == patience`, Supervisor automatically settles the
  historical best.

There are two separate comparisons:

- Plan-local improvement controls patience against this Plan's best metric.
- Final Hypothesis outcome controls Elo against the frozen reference metric.

### Budget exhaustion

- Turn limit exhausted with a trusted best: automatically settle the best.
- Turn limit exhausted without a trusted score: keep the Plan unresolved in
  `state.json`, release its concurrency slot, and expose WAITING to Human.
- A waiting Plan does not occupy a concurrency slot.
- Human may add turns, set unlimited turns, or abandon the Plan.
- A re-enabled existing Plan has priority over creating a new Plan.

## 11. Iterative Git Checkpoints

The current `LocalGitWorkspace` accepts only one reviewed commit per worktree.
SEARCH patience requires multiple trusted revisions on one Plan branch.

Extend the existing class rather than adding a workspace manager:

```text
Agent edits
-> execute manifest
-> trusted score
-> review diff from current HEAD
-> commit scored revision on the same branch
-> continue editing from that commit
```

Each trusted scoring round produces a commit, including non-improving rounds,
so evidence remains reproducible. `best_ref` points to the best one. A later
worse commit never replaces the best.

Commit must remain review-bound: a commit is accepted only if the staged tree
matches the previously reviewed diff. After commit, the workspace starts a new
review cycle from the new HEAD.

The final Experiment records the historical best commit, not necessarily the
branch tip when exploration stops.

## 12. Experiment Manifest

PlanAgent may implement an arbitrary multi-file project. No `model.py` or
single Python entrypoint is required.

Workspace root must contain:

```json
{
  "version": 1,
  "commands": [
    ["uv", "run", "python", "-m", "solution.train"],
    ["uv", "run", "python", "-m", "solution.predict"]
  ],
  "outputs": {
    "predictions": "outputs/predictions.csv",
    "report": "outputs/report.md"
  }
}
```

Rules:

- Commands are argv arrays, never shell strings.
- Commands and outputs resolve inside the assigned workspace.
- The manifest cannot declare labels, scores, Git commands, or workspace paths.
- Predictions are mandatory. Report is mandatory for PREPARE and final Plan
  settlement, and may be updated throughout SEARCH.
- CSV may be an experiment output; Athena does not require CSV as the only
  project or dataset representation.
- Command failure, missing output, invalid predictions, or evaluator failure
  is returned to the same PlanAgent for repair.

## 13. PREPARE

PREPARE is one Plan, one PrepareAgent, and one workspace. It replaces the
Supervisor-level `DataAgent -> InitAgent -> ResearchAgent` chain.

PrepareAgent autonomously:

- inspects project data;
- writes reproducible EDA code and report;
- records data roles and target understanding;
- produces the evaluator bundle inputs required for freezing labels;
- implements a baseline multi-file project and `experiment.json`;
- debugs until the baseline receives a trusted score.

Supervisor:

- owns the workspace and raw-data boundary;
- validates and freezes the evaluator and labels;
- executes the baseline manifest;
- commits the baseline;
- initializes ResearchTree baseline Experiment and SOTA.

Existing DataAgent/InitAgent prompts and reliable checks may be migrated into
PrepareAgent. Their old Supervisor runtime roles and nested orchestration are
removed. PREPARE has no internal `step` state and no patience counter.

## 14. Human Input And Global Memory

SupervisorAgent is the only global long-lived research Agent:

```text
agent_id = supervisor
log = .athena/logs/agents/supervisor.jsonl
```

Ordinary Human text is appended as a structured user message before the
SupervisorAgent turn starts. The append is flushed. The call is acknowledged
only after the input is durable. A crash after the user record but before a
complete response leaves an incomplete SupervisorAgent turn that is resumed on
restart.

SupervisorAgent turns are serialized. No Supervisor message table, mailbox
database, message status, timestamp, HumanRequest, request ID, answer API, or
message cursor is introduced.

### Plan boundary

Every Plan receives an immutable context artifact at creation. Human research
guidance never changes an already created Plan. It affects the next Plan
created after SupervisorAgent processes the message.

Examples of ordinary research guidance:

- data interpretation;
- Human-proposed hypothesis;
- a requested model family;
- a persistent constraint such as avoiding a method;
- a one-shot request such as trying ViT next.

Human may explicitly modify an unresolved waiting Plan's budget. This changes
execution budget only; it does not inject new research guidance into a running
Plan. A request to use a different approach becomes a new Hypothesis/Plan.

### Strong commands

The single TUI input remains a text message. Supervisor deterministically
handles only explicit commands such as `/stop`, `/pause`, and `/resume` before
LLM interpretation. Ordinary prose cannot accidentally trigger a destructive
command.

## 15. TUI Protocol

The server emits exactly two top-level event kinds.

### `output`

An ordered append-only display event:

```json
{
  "type": "output",
  "seq": 42,
  "source": "agent",
  "channel": "text",
  "text": "testing the ViT encoder",
  "plan": "hyp_vit_01",
  "tool": null,
  "artifact_ref": null,
  "truncated": false
}
```

`source` is `supervisor`, `agent`, or `tool`. `channel` is `text`, `stdout`,
`stderr`, or `error`. Text deltas and completed records use the same event;
consumers append in `seq` order.

Tool preview rules:

- stdout and stderr share a 512-byte preview limit;
- stderr is allocated first and stdout receives the remainder;
- truncation is UTF-8 safe;
- preview and full artifact are redacted through one path;
- full redacted output exceeding the preview is stored in ArtifactStore;
- tool name and a compact safe argument summary may be shown;
- full file bodies, prompts, secrets, and large JSON are not shown inline.

### `state`

A complete replaceable runtime snapshot:

```json
{
  "type": "state",
  "status": "RUNNING",
  "phase": "SEARCH",
  "plans": [],
  "search": {"attempts": 6, "limit": 10, "successes": 4, "concurrency": 4},
  "sota": {"experiment": "exp_4", "metric": 0.84},
  "waiting": null
}
```

Each Plan projection includes its ID, derived status, project-relative
workspace, Agent status, current tool summary, turns, patience, and best metric.
This is a runtime projection, not a second persistence model.

### TUI input and reconnect

TUI sends one `message(text)` command surface. It does not call STATUS,
REQUESTS_GET, HUMAN_REPLY, TREE_GET, MESSAGES_GET, or read session files.

On connect/reconnect, Supervisor immediately emits the current full `state`.
Only new `output` events follow. Old token deltas are not replayed.

## 16. Crash Recovery

Recovery continues scientific state, not broken network streams.

- An interrupted token or tool stream is not resumed or synthesized.
- Complete structured Agent messages already in JSONL restore context.
- Incomplete assistant output is ignored.
- The same Plan, stable Agent ID, workspace, and budget are recovered.
- The next Agent turn is told that the previous turn was interrupted and must
  inspect workspace files, Git status, manifest outputs, and failure evidence.
- An interrupted turn already counted because `turns_used` was saved first.
- Tool/run IDs and live tool state are process-local and never recovered.

### Two-file write order

Keep separate `research_tree.json` and `state.json`; do not add shared versions,
WAL files, or transaction IDs.

Plan settlement order is fixed:

1. atomically write the settled Experiment and Hypothesis result to
   `research_tree.json`;
2. remove the Plan from `state.json` and atomically write it.

If a crash occurs between writes, recovery sees a final Experiment for an
active Plan's Hypothesis and removes the stale Plan without settling twice.

Other reconciliation rules:

- Plan and no final Experiment: resume the Plan.
- Proposed Hypothesis and no Plan: queued candidate.
- Final Experiment and no Plan: completed normally.
- Plan missing context artifact or workspace: WAITING for Human rebuild or
  LOSS decision; never silently recreate different frozen input.

### Network failure

Provider/network failure is repairable execution evidence. It does not affect
patience or Elo. Supervisor retries the same Plan until turn budget, Human stop,
or execution safety limit.

## 17. VALIDATE

VALIDATE is one Plan, one ValidateAgent, and one workspace created from the
frozen SOTA commit.

Input:

```python
class ValidationInput:
    sota_commit: CommitHash
    evaluator_ref: ArtifactRef
    final_dataset_ref: ArtifactRef
    validation_key: str
```

`validation_key = hash(sota_commit + evaluator_ref + final_dataset_ref)`.

Output:

```python
class ValidationResult:
    sota_commit: CommitHash
    validation_commit: CommitHash
    metric: float
    predictions_ref: ArtifactRef
    evidence_ref: ArtifactRef
```

An independent validation commit is allowed for environment compatibility. It
does not become SOTA and is not merged into the Search result.

Allowed changes include dependency locking, paths, entrypoints, device
compatibility, serialization compatibility, deterministic seeds, and other
runtime-only repairs. Model architecture, features, preprocessing semantics,
hyperparameters, training strategy, and any use of final labels are forbidden.

Acceptance requires Git diff review, ValidateAgent explanation, an independent
LLM diff review, and deterministic leakage checks. A rejected diff is returned
to the same ValidateAgent.

Logical exactly-once behavior is artifact based:

- complete trusted result for the validation key: commit it without rerun;
- predictions exist but no trusted score: continue scoring;
- partial or invalid output: rerun under the same key;
- never publish two logical final results for the same frozen key.

Human may add repair turns, provide environment information, pause, or stop.
Human may not tune the model during VALIDATE. Continuing research requires an
explicit cancellation of VALIDATE and a return to SEARCH through new
Hypotheses and Plans.

## 18. Search Limit Boundary

At the configured Search attempt limit:

- no new SEARCH Plan is created;
- existing active or resumed Plans may finish;
- interactive mode exposes a WAITING state and asks through `output` whether
  to extend SEARCH or enter VALIDATE;
- auto mode enters VALIDATE;
- Human may extend the attempt limit or enter VALIDATE using natural language;
- if every Search Plan failed, VALIDATE may use the PREPARE baseline SOTA.

No HumanRequest object or request queue is created.

## 19. Code Organization

Target Supervisor package:

```text
src/athena/research/supervisor/
|- __init__.py             public contracts only
|- supervisor.py           single-writer loop and phase transitions
|- prepare.py              one PREPARE Agent repair loop; returns baseline data
|- plans.py                Plan state/input/decision contracts
|- scheduler.py            rolling slots and policy/FIFO selection
|- policy.py               HypothesisPolicy and EloPolicy
|- experiment.py           manifest validation, execution, scoring loop
|- recovery.py             state/tree/workspace reconciliation
|- events.py               output/state projection and redaction
|- validation.py           one VALIDATE Agent repair/recovery loop
`- state.py                atomic state.json load/save
```

Prompt-driven role registration remains under `src/athena/agents/`:

```text
agents/plan_agent.py       register one fresh PlanAgent per Hypothesis ID
agents/prepare_agent.py    register the stable prepare Agent
agents/validate_agent.py   register the stable validate Agent
```

`prepare.py` and `validation.py` are narrow phase functions. They return
validated results to `Supervisor`; they never write ResearchState or
ResearchTree and are not phase state machines.

The exact split may follow existing module conventions, but files must retain
one clear responsibility. Do not recreate `planning/` as multiple phase
planners, a generic operation executor, or `storage/` Store wrappers.

ResearchRuntime remains the public composition root. Its public surface should
be reduced to start/close, subscribe, and one message command. Compatibility
shims may exist only where an external caller still requires them and must have
an explicit deletion test/owner.

## 20. Deletion Inventory

Delete after replacement coverage exists:

- fine-grained `SupervisorOperation`, Operation types/statuses, Plan validator,
  Operation executor, PlanFactory, and deterministic phase planner graph;
- Supervisor SQLite schema and all storage modules;
- `HumanRequest`, its Store, TUI queue/editor modes, REQUESTS_GET, HUMAN_REPLY;
- status/tree/message polling APIs made redundant by full `state`;
- `SupervisorMessageResponder` status-only assistant;
- `.athena/sessions` readers and rollout offsets in CLI/TUI;
- duplicate `<agent_id>.log` writer in BaseAgentRunner;
- nested Supervisor runtime roles for DataAgent and InitAgent after PrepareAgent
  contains the retained behavior;
- active dataset-view routing and partially connected dataset-view state;
- fake-provider end-to-end acceptance paths.

Unit-test fakes remain allowed as injected test doubles. Production startup and
acceptance must require a real provider from `.env`.

## 21. Error Handling

- Invalid SupervisorAgent tool request: reject it and return the validation
  error to the same SupervisorAgent turn.
- Invalid PlanDecision: return a structured contract error to the same Agent;
  do not infer from prose.
- Manifest or output failure: evidence artifact plus same-Plan follow-up.
- Trusted evaluator failure caused by candidate output: same-Plan repair.
- Evaluator infrastructure failure: retry without patience or Elo effect.
- Workspace path escape or forbidden command: reject immediately and expose an
  error output event.
- Repeated process crash: retain workspace and Plan until execution safety
  limit or explicit Human decision.
- Missing frozen evaluator or baseline: PREPARE cannot complete.
- Corrupt `state.json` or ResearchTree: fail closed with the exact project path
  and recovery guidance; do not silently initialize over existing state.

## 22. Testing Strategy

Implementation follows baseline-first TDD. Each task begins with a focused
failing test, implements the smallest behavior, and then runs its affected
regression slice.

### Unit tests

- ResearchTree selective inheritance and `supersedes` validation.
- One final Experiment per Hypothesis.
- Elo seed, WIN/DRAW/LOSS settlement, single-sided immutability, and FIFO ties.
- Frozen reference behavior under out-of-order concurrent completion.
- Rolling slot filling and immediate refill.
- Existing READY Plan priority over a new Hypothesis.
- Human one-shot next selection without priority mutation.
- Search attempt accounting independent of turns and scores.
- Turn accounting before run start and after crash.
- Patience reset on improvement and increment on trusted non-improvement.
- No patience change for execution/provider/infrastructure failure.
- Historical best settlement for submit, patience exhaustion, and turn
  exhaustion.
- WAITING/no-slot behavior when turns expire without a trusted score.
- Stable Hypothesis ID across Plan, Agent, workspace, and log.
- Immutable Plan input under later Human messages and Tree updates.
- Iterative reviewed commits in one worktree and historical best checkout.
- Manifest argv/path/output validation.
- VALIDATE diff allow/deny rules and validation-key recovery.
- Atomic state write and two-file crash reconciliation.
- Agent log recovery from complete records and truncated final lines.
- Output sequencing, redaction, UTF-8-safe 512-byte tool preview, and artifact
  spill.
- State snapshot replacement and no TUI filesystem access.

### Integration tests

- PREPARE completes with one Agent and produces a trusted baseline.
- Four SEARCH Agents run concurrently with isolated workspaces and logs.
- One Plan finishes early and rolling refill starts another without a batch
  barrier.
- Human input during running Plans changes the next Plan only.
- Waiting Plan releases its slot and resumes before new Plan creation after a
  Human budget extension.
- Crash during Agent turn resumes the same Plan and counts the interrupted
  turn.
- Crash between ResearchTree and state writes reconciles without duplicate
  settlement.
- Search limit stops new Plan creation and transitions to VALIDATE.
- VALIDATE records distinct SOTA and validation commits.
- TUI operates using only `output`, `state`, and text input.

### Real-LLM acceptance

Use a fresh Titanic multi-file project with target `Survived`.

- Load the real model and credentials from repository `.env`.
- Do not use a fake provider or fixed generated `model.py`.
- Configure at most 10 SEARCH attempts and concurrency 4.
- PREPARE must produce a trusted baseline.
- At least four SEARCH Plans must finish with valid trusted scores; baseline
  does not count.
- Failed Plans retain evidence but do not count toward the four successes.
- The run must reach VALIDATE and COMPLETED.
- The final result must record SOTA commit, independent validation commit,
  trusted metric, predictions, and evidence.
- Captured TUI events must contain only `output` and `state` kinds.

Run the complete repository test suite after focused and integration tests.

## 23. Migration Sequence

1. Add target state, Plan, policy, and event contracts behind tests.
2. Extend ResearchTree minimally for scheduling and selective inheritance.
3. Make Agent IDs caller supplied and move structured recovery logs to
   `.athena/logs/agents`.
4. Add iterative reviewed Git checkpoints.
5. Implement autonomous PREPARE and manifest execution.
6. Implement one SEARCH Plan loop, trusted feedback, best commit, and patience.
7. Add rolling concurrent scheduling and SupervisorAgent hypothesis tools.
8. Add natural-language Human input with immutable Plan boundaries.
9. Implement VALIDATE and logical exactly-once recovery.
10. Replace TUI integration with only `output` and `state`.
11. Run deterministic migration/regression tests.
12. Delete replaced Operation, storage, HumanRequest, session polling, and
    duplicate logging code.
13. Run the real-LLM Titanic acceptance execution.
14. Remove the completed implementation plan and update `codex_docs/CURRENT.md`
    according to repository instructions.

No automatic migration of the old Supervisor SQLite database is required.
Existing experiment Git repositories, artifacts, and valid ResearchTree files
remain usable. An unfinished old Operation-based execution must be restarted
under the new coarse Plan runtime.

## 24. Acceptance Criteria

The design is implemented when all of the following have fresh evidence:

- Supervisor has no Operation DAG or SQLite persistence path.
- PREPARE, SEARCH, and VALIDATE each follow one-Plan/one-Agent ownership.
- SEARCH supports four rolling concurrent Plans.
- One Hypothesis has one PlanAgent/workspace/log identity and one final
  Experiment.
- Human research guidance affects the next created Plan, not active Plans.
- Human can propose hypotheses, alter limits, resume waiting Plans, request
  unlimited retry, enter VALIDATE, and stop through natural language.
- Trusted score feedback drives Plan-local optimization and consecutive
  no-improvement patience.
- Every completion path settles the historical best trusted commit.
- Hypothesis scheduling uses replaceable `HypothesisPolicy`, single-sided Elo,
  and FIFO ties.
- Selective `supersedes` retains compatible upstream data hypotheses.
- Recovery resumes workspace and Agent context without pretending to resume a
  broken network stream.
- TUI reads no Agent/session files and consumes only `output` and `state`.
- Tool previews obey the redacted 512-byte combined limit.
- Real `.env` LLM acceptance completes Titanic with at least four trusted
  SEARCH successes within ten attempts.

## 25. Design Self-Review

- Placeholder scan: no unresolved TBD fields remain; the two explicit TODOs
  are intentionally scoped future extensions.
- Consistency: Plan identity, persistence ownership, Human boundary, rolling
  scheduling, patience, and crash recovery use the same rules throughout.
- Scope: dataset views, distributed coordination, and advanced scheduling are
  excluded from this implementation.
- Ambiguity: final settlement always uses the historical best trusted commit;
  no path uses the Agent's last untrusted workspace state.
- Existing infrastructure: ResearchTree is retained rather than redesigned;
  LocalGitWorkspace and AgentRuntime receive narrow required extensions.
