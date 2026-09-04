# Supervisor Transactional Control and Search Resume Design

Date: 2026-09-04
Status: approved; implementation planning queued behind the active plan

## Problem

The Supervisor currently sees thirteen Athena-specific decision tools, plus human
input and optional competition tools. Three separate tools read overlapping pieces
of research context, while several write tools are small variations of one state
transition. This increases prompt cost and gives the model too many superficially
similar choices. It also makes an apparently simple request such as “再 search 20
个” ambiguous: `configure_search` accepts an absolute limit, not an increment.

The lifecycle has a second gap. A normally settled run is persisted as
`phase=COMPLETED, status=COMPLETED`; the GUI no longer routes ordinary input from
that session to the Supervisor, and the runtime has no supported transition from
COMPLETED back to SEARCH. Manually changing `state.json` would leave the prior
validation looking current, could lose `resume.json` data, and would not reliably
re-arm the Agent runtime or Search scheduler.

The user needs to reopen such a session, ask the Supervisor for a bounded amount of
additional experimentation, and continue from the existing ResearchTree and SOTA.
The same change should reduce the public Supervisor tool surface without granting
raw filesystem writes to framework-owned state. A failed control mutation must not
leave memory, `state.json`, and `resume.json` disagreeing.

The session sidebar also displays workspace groups whose known session list is
empty. Those groups should be hidden as a presentation rule only. No workspace,
session state, recent-root entry, or browser storage may be deleted.

## Goals

1. Reduce the normal Supervisor tool surface from thirteen domain tools to five or
   fewer, excluding optional external integrations.
2. Give every Supervisor turn a compact, bounded, versioned research snapshot
   without requiring three preliminary read calls.
3. Route all Supervisor-owned state changes through typed, serialized transactions;
   do not expose arbitrary field patches or `.athena` write access.
4. Automatically restore state after a control transaction fails, including
   recoverable process-crash windows across `state.json` and `resume.json`.
5. Interpret “再 search N 个”, “run N more experiments”, and equivalent intent as
   additional budget measured from the moment the transaction is accepted.
6. Reopen a normally settled COMPLETED session at its existing checkpoint, retain
   research history, invalidate the prior validation as a current result, disable
   automatic validation durably, and stop at SEARCH/WAITING when the new budget is
   exhausted.
7. Hide empty workspace groups in the session sidebar without deleting any data.
8. Produce deterministic, offline-testable behavior and actionable file logs for
   every control transaction failure.

## Maintainability and API budget

The implementation treats cognitive load as a compatibility constraint, not a style
preference:

- The normal model-visible surface is exactly the five tools in this design. Legacy
  tool names may exist only as non-registered adapters during migration; no second
  public path may perform the same mutation.
- `update_research` and `manage_hypotheses` each expose one strict request envelope:
  `{context_version, action}`. Action-specific values live inside the discriminated
  action model. Framework values such as session ID, operation ID, journal paths,
  retry policy, runtime handles, and rollback snapshots are derived internally and
  are never model parameters.
- `inspect_research` exposes only `section`, optional opaque `cursor`, and bounded
  `page_size`. It does not expose storage paths, projection internals, or query
  language syntax.
- Domain choices use enums or discriminated models rather than clusters of booleans.
  Defaults and safety caps have one configuration owner and are not repeated across
  tool, runtime, and GUI layers.
- The mutation coordinator, durable journal store, context projector, resume policy,
  and GUI routing projection each have one named owner module. Compatibility adapters
  contain delegation only, carry removal tests/milestones, and do not accumulate new
  behavior.
- Public tool-name and JSON-schema snapshots, import-boundary tests, and duplicate-
  path searches enforce this budget. A new parameter or tool requires an explicit
  design change, migration note, and test update.
- The transaction machinery remains scoped to Athena ResearchState/ResearchTree
  control operations. It is not generalized into a repository-wide framework until
  a second proven consumer exists.

## Non-goals

- The Supervisor does not receive `write_file`, shell, or unrestricted Python access
  to `.athena/**`.
- This design does not make arbitrary ResearchState fields model-editable.
- Resuming a FAILED run, overriding an explicit STOPPED run, deleting hypotheses,
  or repairing a broken experiment is not the same operation as reopening a
  normally settled run.
- Additional budget is not a promise of N successful scores or N newly generated
  hypotheses. It permits at most N new SEARCH Plan starts; a genuine lack of viable
  hypotheses or a later infrastructure failure may stop earlier and must be shown.
- Experiment execution is not enclosed in the state transaction. A Plan failure
  after the resume transaction returned success is a research-domain outcome, not
  a reason to erase the accepted budget change.
- Existing validation artifacts and historical reports are not deleted.
- Recent-workspace persistence and the workspace picker are not redesigned.
- This work does not broaden General Agent, Plan Agent, or Kaggle permissions.

## Product decisions

### Controlled direct access, not raw state-file access

“Direct read” means the platform supplies a safe ResearchContext projection at the
start of a Supervisor turn. “Direct write” means the Supervisor submits one typed
domain action to a transaction boundary. The model never writes JSON files and never
selects arbitrary object paths.

Rollback is automatic platform behavior. There is no model-facing `rollback_state`
tool: requiring a model to notice its own partial failure and call a compensating
tool would fail precisely when rollback matters most.

### Normally settled sessions are resumable

The accepted resume sources are:

- `phase=SEARCH, status=WAITING`, provided the wait is compatible with adding global
  experiment budget; and
- `phase=COMPLETED, status=COMPLETED`, with a trusted existing SOTA and evaluator.

`FAILED` is not silently converted to success, and `STOPPED` is not overridden by
prose. Both return a typed non-resumable error and leave state unchanged. A separate
recovery or explicit resume workflow may address them later.

### Old validation remains historical, not current

When COMPLETED is reopened, the old validation dictionary, report reference, final
score, and SOTA identity are recorded in the durable control audit entry before
`ResearchState.validation` is cleared. Content-addressed artifacts, transcript rows,
ResearchTree entries, and immutable run documents remain on disk. Mutable “current
final” projections must no longer present that score as current; if an existing
mutable report would be replaced, its prior bytes are retained through the existing
artifact/run record or an immutable archived copy.

Revealing a final score consumes that final-evaluator generation. Continuing Search
after seeing it is scientifically useful, but a later score from the same hidden
final evaluator is no longer an independent final validation. The resume transaction
therefore records the final evaluator as consumed. Resumed Plan and Ideator context
must not include the revealed final score. A later transition to VALIDATE requires a
fresh independent evaluator generation; if none exists, the user must explicitly
acknowledge reuse and the resulting report is labelled exploratory rather than an
independent final result.

For new runs, consumption is persisted atomically when a final result is first
revealed, not deferred until a later resume. A legacy completed run with a current
validation result is conservatively treated as consumed during migration, even when
older metadata has no generation marker.

### Empty workspaces are hidden only

The session sidebar filters out any workspace group whose session array is empty.
The top-level New Session and Switch Workspace controls remain visible even when no
groups remain. `recentRoots`, per-workspace session caches, backend state, and disk
directories are untouched. A hidden recent root remains reachable from the workspace
picker.

## Alternatives considered

1. **Versioned context plus typed transaction facade (selected).** This removes the
   repeated reads and consolidates state writes while retaining domain validation,
   scheduler side effects, atomic persistence, idempotency, and testable rollback.
2. **One raw `read_state` / `write_state` / `rollback_state` interface.** This has the
   fewest names but exposes illegal phase combinations, cannot infer required side
   effects, and depends on the model to compensate after an error. It is rejected.
3. **Keep every specialized tool and expose only phase-valid subsets.** Dynamic
   projection modestly reduces per-turn choice, but duplicated schemas and the
   absolute-versus-additional budget ambiguity remain. Phase-aware projection is
   still useful inside the selected facade, but is insufficient alone.
4. **Parse resume phrases in the React client.** This would be language-specific,
   unavailable to TUI/API callers, and place lifecycle authority in the wrong layer.
   The GUI sends prose unchanged; the Supervisor chooses the typed backend action.

## Public Supervisor surface

The normal registry contains these tools:

| Tool | Responsibility | Concurrency |
| --- | --- | --- |
| `inspect_research` | Paginated detail beyond the injected compact context | read-only, safe |
| `update_research` | Typed ResearchState/control actions | serialized, unsafe to parallelize |
| `manage_hypotheses` | Propose or select hypotheses through ResearchTree domain rules | serialized, unsafe to parallelize |
| `dispatch_general` | Delegate external inspection or repair, then checkpoint refs | saga; checkpoint serialized |
| `request_user_input` | Ask one necessary human decision | existing behavior |

`kaggle_get_competition` is projected only when the Kaggle integration is relevant.
Kaggle configuration becomes an `update_research` action. The new clarification
controller remains authoritative for task understanding, so
`record_task_understanding` is removed from normal Supervisor turns after the
currently active LLM task-understanding plan lands. A legacy-only adapter may expose
that action before PREPARE when loading an old project without a confirmed contract.

The old public names may remain temporarily as internal Python adapters during
migration, but they are absent from the model-visible registry after cutover.

### Phase-aware action projection

`update_research` uses a strict discriminated union. Only actions valid for the
current phase are included in the projected schema, so consolidating names does not
replace thirteen small tools with one unbounded, confusing schema.

The supported action families are:

- `resume_search(additional_attempts)`
- `configure_search(search_limit?, concurrency?)`
- `update_plan_budget(plan_id, turn_limit?, unlimited_turns?, patience?)`
- `set_phase(decision)`
- `set_manual_mode(manual)`
- `record_guidance(text, scope)`
- `configure_kaggle(enabled, download)`

The wire shape does not flatten variant parameters into the tool signature:

```json
{
  "context_version": "sha256:...",
  "action": {
    "kind": "resume_search",
    "additional_attempts": 20
  }
}
```

Every input model is strict and forbids unknown fields. At least one field must be
present where relevant; existing maximum concurrency, Plan-turn, and patience limits
remain authoritative. Search additions and resulting absolute limits are checked
against configured caps with overflow-safe arithmetic. `configure_search.search_limit`
cannot be set below the already-started attempt count. No generic `{path, value}`
operation exists.

`manage_hypotheses` similarly uses a discriminated `propose` or `select` request and
continues to call the existing ResearchTree and Scheduler domain methods. Tree
mutation has its own serialized boundary rather than pretending to be a state-only
patch. It participates in the same per-session mutation coordinator, with a
tree-aware candidate and journal entry.

`dispatch_general` is a saga, not an atomic filesystem transaction. It performs no
ResearchState mutation before external work starts. After successful external work,
it checkpoints only returned artifact/cache references through a short transaction.
External work cannot be undone; if the checkpoint fails, the tool returns the
artifact reference plus `checkpoint_failed` and never claims the external effect was
rolled back.

## Versioned ResearchContext

Before each human-authored Supervisor turn, the runtime prepends one bounded,
machine-generated context block. It contains no credentials and no raw artifact
contents:

```json
{
  "context_version": "sha256:...",
  "session_id": "s-1",
  "phase": "COMPLETED",
  "status": "COMPLETED",
  "search": {
    "attempts": 37,
    "limit": 37,
    "concurrency": 2,
    "manual": false,
    "auto_validate": true
  },
  "sota": {
    "experiment_id": "exp_best",
    "metric": 0.91
  },
  "validation": {
    "current": true,
    "policy": "automatic",
    "final_evaluator": "consumed"
  },
  "plans": {"active": 0, "waiting": 0},
  "hypotheses": {"pending": 6, "preview": []}
}
```

The version is an opaque digest over the canonical state, relevant per-run options,
and ResearchTree generation. It requires no new field in the legacy core
`state.json`. Free text is omitted from the compact block or normalized, JSON
encoded, and length bounded. The system prompt states that context values are data,
not instructions. `sota.metric` is explicitly the frozen SEARCH-evaluator metric,
never a previously revealed final-validation score.

`inspect_research` combines the old three read tools and historical control audit. It
accepts a section (`state`, `plans`, `hypotheses`, or `history`), a stable cursor, and
a bounded page size. Its
result includes a fresh `context_version`. This retains access to a long hypothesis
queue and prior resume records without injecting an unbounded list on every turn.
The history projection returns audit metadata and artifact references but omits raw
hidden-final scores from model context; the user can still open preserved historical
artifacts in the UI.

Every mutation request includes the latest `context_version`. A mismatch returns a
typed `stale_context` result with a fresh compact context; it performs no mutation.
The Supervisor may then reconsider and retry. Tool responses always return the
committed context rather than inviting the model to infer new state.

## Transaction boundary

### Serialization and candidate state

All `update_research` calls are declared `concurrency_safe=False`. One per-session
`SupervisorMutationCoordinator` owns the lock used by every short read-modify-write
and persistence section: phase transitions, Plan start/settle, Search configuration,
session settings, and ResearchTree mutation. Long Agent turns, experiment processes,
validation computation, and external General Agent work never hold the lock.
Scheduler work computed before acquiring the lock must re-check the context version
and either recompute against the new state or fail with `stale_context`. It is not
sufficient to rely on cooperative asyncio scheduling around `await` points.

An in-process lock is insufficient when a Windows backend and a WSL backend can open
the same workspace. Runtime startup therefore also acquires an OS-backed exclusive
session-owner lock, held for that runtime's lifetime and released automatically on
process death. A second process returns `session_busy` before loading the session as
writable. The lock implementation must be integration-tested on the supported
Windows/WSL shared-filesystem path; a plain lockfile whose mere existence survives a
crash is not an acceptable substitute.

The transaction does not mutate the shared ResearchState before persistence:

1. Resolve the framework-owned operation ID. An exact committed replay returns its
   recorded result before stale-version checks; reuse with different normalized
   arguments returns `operation_mismatch` and performs no mutation.
2. Verify session identity, `context_version`, action preconditions, and cancellation.
3. Snapshot the current in-memory state/tree, relevant runtime options, scheduler wake
   state, and the exact prior bytes/presence of every document the action may change,
   including `state.json`, `resume.json`, and applicable ResearchTree files.
4. Build a deep candidate copy and apply the action through domain methods.
5. Validate the complete candidate with `ResearchState.model_validate` and all
   action-specific invariants.
6. Perform filesystem and runtime preflight checks that do not mutate durable state.
7. Write a durable PREPARED transaction record.
8. Persist the candidate document set, then update the shared in-memory objects
   without breaking the identities shared by Runtime, Services, and Supervisor.
9. Mark the transaction STATE_COMMITTED.
10. Start only the immediate, resumable side effects required by the action.
11. Record COMPLETED, publish the new snapshot, and return the structured result.

Using candidate copies means common validation and `os.replace` failures leave
shared memory untouched. The durable record handles the less common multi-document
and process-crash windows.

### Durable transaction record

The session state root owns a single active control journal and a bounded completion
ledger. Each record includes:

- session ID, framework-derived operation ID, and normalized-arguments digest;
- action and sanitized arguments;
- prior and candidate context versions;
- exact before/after action-owned document payloads, including file-absence markers;
- prior runtime options needed for compensation;
- phase: `PREPARED`, `STATE_COMMITTED`, `ROLLBACK_PENDING`, `COMPLETED`, or
  `ROLLED_BACK`;
- result summary or redacted error classification; and
- audit metadata for superseded validation when applicable.

The journal is framework-owned and written with the same temp/fsync/replace
discipline. Persistence uses unique, same-directory temporary names rather than the
shared `state.json.tmp`, fsyncs data before replacement, retries only bounded Windows
access/sharing violations, and cleans up owned temporary files. It is never exposed
as a model-writable file. The bounded completion ledger retains enough recent
operation IDs to make transport/provider retries idempotent without growing forever.

Every successful `resume_search` transaction also creates an immutable, non-compacted
resume-history record keyed by operation ID. It records the source phase, accepted
attempt count, added budget, validation policy, and evaluator-generation status; for
COMPLETED sources it also references the prior validation/report and prior SOTA
identity. The record is
first embedded in the durable PREPARED journal before any current validation identity
is cleared, then materialized as an operation-named committed record after state
commit and before the transaction becomes COMPLETED. Recovery from STATE_COMMITTED
finishes that materialization. The committed record also acts as the permanent
idempotency receipt for budget additions, so even an old replay cannot add the same
allowance twice.
Completion-ledger compaction may discard full results for unrelated expired control
operations, but it must never erase scientific/user-visible resume history or its
operation-ID uniqueness key. A PREPARED transaction that rolls back does not create a
committed receipt; its journal failure entry remains distinguishable from success.

### Crash recovery

Runtime construction resolves an active control journal before normal state loading
or automatic session resume:

- `PREPARED`: restore the before payloads and remove any partial candidate files.
- `STATE_COMMITTED`: finish installing the after payloads, reconstruct the in-memory
  state, and re-arm the declared resumable side effect exactly once.
- `ROLLBACK_PENDING`: cancel or ignore any candidate side effect, restore the before
  payloads, and keep retrying recovery on later startup if storage remains blocked.
- `COMPLETED`: return the recorded result for a duplicate operation and compact the
  active journal into the completion ledger.
- `ROLLED_BACK`: verify the before payloads, retain the failure audit entry, and
  clear the active journal.

If restoring disk state is itself blocked (for example persistent Windows access
denial), the runtime keeps the known before-image in memory, does not start Search,
retains the journal for the next startup, logs both the original and rollback
errors, and returns `rollback_failed`. It must not falsely report that rollback was
completed. This is the honest limit of recovery when the storage device refuses all
writes.

After a committed resume exists, a missing, corrupt, or digest-mismatched resume
sidecar fails closed: the new runtime parks the session at SEARCH/WAITING, keeps
automatic validation disabled using the immutable resume receipt as evidence, and
returns `resume_metadata_invalid` for repair. It never falls back to a global
auto-validate default.

### Operation identity and retries

The operation ID is derived from the real provider tool-call ID plus session
identity. The current Agent loop constructs `ToolContext.call_id` from
`turn_id:tool_name`, which collides when the same tool is called twice in one turn.
The implementation must preserve the actual provider call ID (for example
`turn_id:provider_call_id`) before transaction idempotency is enabled.

Replaying the same operation ID returns its recorded result and never adds budget a
second time. A later human request produces a new operation ID and intentionally
creates a new transaction. The deterministic `/search +N` transport supplies its own
request identity; retries preserve that identity, while a separately submitted human
command receives a new one.

### What rolls back

Rollback covers validation failures, stale-state conflicts, persistence failures,
and failures to start an immediate required side effect before the tool returns.
Before compensation begins, the journal is marked `ROLLBACK_PENDING`. Compensation
cancels any newly created scheduler task before restoring state.

An action such as `set_phase(VALIDATE)` commits only the validated control intent and
starts a resumable job handle inside this boundary. The long validation computation
runs outside the lock and transaction; a later computation failure is a truthful
domain failure, not a reason to erase the accepted transition. The action also
enforces the consumed-final-evaluator guard described above.

The following are deliberately not rollback triggers:

- a SEARCH Plan or validation job failing after the transaction returned success;
- a subscriber/WebSocket missing the committed state notification;
- a transcript/log notification failure after the canonical state committed; or
- failure of an external General Agent operation, which is outside the state
  transaction.

These errors are logged and projected through their existing domain paths. Rolling
back a committed budget because a later experiment failed would erase truthful
history and could not undo external compute.

## `resume_search` semantics

### Preconditions

The action requires:

- a strict positive integer `additional_attempts`;
- an addition and resulting limit within configured safety caps;
- a current `context_version`;
- one accepted source state pair (`SEARCH/WAITING` or
  `COMPLETED/COMPLETED`);
- a trusted SOTA experiment with a commit and evaluation;
- a readable frozen SEARCH evaluator/reference configuration;
- no unreconciled live Plan/worker in a COMPLETED source;
- no unresolved control transaction; and
- a scheduler/runtime that can be initialized without changing durable research
  state.

For SEARCH/WAITING, the preflight distinguishes global-budget exhaustion from an
exhausted Plan budget or manual-selection wait. If a Plan needs its own budget, the
action returns `plan_budget_required` with the relevant IDs instead of claiming that
global budget fixed it. If manual mode is enabled, it remains enabled and the result
states that a selection is still required; resuming Search does not silently change
the user's scheduling policy.

### Attempt accounting

At the serialized transaction boundary:

```text
attempts_at_acceptance = terminal SEARCH experiments + active SEARCH Plans
new_search_limit = attempts_at_acceptance + additional_attempts
```

Active Plans count because they have already been started; they are not part of the
new allowance. Existing pending hypotheses are consumed before new ideation, in
accordance with the current Scheduler. The operation adds capacity for at most N
new Plan starts, regardless of whether they later SUCCEED, FAIL, or are abandoned.

The transaction result contains `attempts_before`, `additional_attempts`,
`search_limit`, `phase`, `status`, `auto_validate`, any manual/Plan blocker, and the
new context version.

### COMPLETED-to-SEARCH transition

For a normally completed session, the candidate mutation is:

```text
archive current validation identity in the control audit
validation = None
final_evaluator_generation = consumed
phase = SEARCH
status = RUNNING
search_limit = attempts_at_acceptance + additional_attempts
auto_validate_override = false
```

`auto_validate_override` is a backward-compatible resume-sidecar field. At runtime
construction it overrides the global default for that session. A later explicit
user-originated settings action may enable automatic validation again; nothing else
does so implicitly. While the override is false, the scheduler cannot enter VALIDATE
and the model-visible phase action cannot bypass the policy. Manual validation
requires an exact gateway command/UI confirmation token that the model cannot mint;
natural-language requests may ask for that confirmation but cannot silently validate.

Task text, task understanding, data contract, evaluator refs, corpus refs, handoff
refs, ResearchTree, SOTA, pending hypotheses, Git commits, and historical artifacts
are preserved. PREPARE is not rerun. The frozen SEARCH evaluator remains available
for comparable experiment scoring; the consumed marker applies to the revealed final
evaluator and controls whether a later result may be called independent validation.
The transaction starts a new Search epoch. Downstream Plan/Ideator prompt builders
exclude prior final-validation payloads, report excerpts, and final-score transcript
messages from that epoch's context; those records remain visible only as history.

After the state commit, the runtime starts Agent infrastructure if necessary,
re-arms a completed lifecycle handle, wakes an existing Search loop or spawns one if
absent, and publishes one durable Supervisor message such as:

```text
已从断点恢复：原有 37 次尝试，新增预算 20，当前上限 57；自动验证已关闭，SEARCH 已启动。
```

When the allowance is exhausted and no Plan is still running, the existing
interactive lifecycle parks at `phase=SEARCH, status=WAITING`. It does not enter
VALIDATE.

### Deterministic fallback

Natural-language Supervisor intent is the primary interface. The same domain action
may also be reached through an exact `/search +N` runtime command for provider
outages, automated recovery, and deterministic operational testing. The fallback
does not implement a second state path; it calls the identical transaction service.

## Existing-session input routing

The backend derives and projects `interaction_mode=clarification|supervisor` from
authoritative session state in summaries and state events. Any session with a SOTA,
SEARCH attempts, active Plans, or another authoritative research checkpoint receives
`supervisor`, even when its status is COMPLETED; a genuinely empty new session
receives `clarification`. The GUI routes prose from this field rather than rebuilding
lifecycle rules from status labels. During mixed-version rollout only, it may use the
existing history/status heuristic when the field is absent. Starting an unrelated
task requires New Session.

Before the first completed-session Supervisor turn, the runtime ensures Agent
infrastructure is started without automatically running a COMPLETED lifecycle. The
Supervisor may inspect the injected snapshot and call `resume_search`; merely opening
or switching to a completed session remains read-only and never auto-resumes it.

Slash commands retain their existing deterministic precedence. `/search +N` joins
that command layer. Phrases merely containing words such as “stop” or “search” are
still passed to the Supervisor and are not parsed as commands.

## Error handling and observability

Every control operation has a transaction/operation ID in structured file logs. Log
records include action, session, before/after versions, journal phase, exception type,
full traceback in the backend log, rollback attempt, and rollback result. Known
credentials are redacted through the existing logging/output policy.

The primary sink is the existing rotating session/backend file log. If the session
directory is the failing resource, logging falls back to the process-level file log
outside that state root, then to stderr plus the bounded in-memory diagnostics ring.
Failure of one sink never masks the original exception. If no durable sink is
writable, the human-facing result explicitly reports `logging_degraded`; software
cannot honestly guarantee a file record when the storage layer rejects every write.

Human-facing tool errors are concise and typed:

- `stale_context`
- `session_busy`
- `operation_mismatch`
- `invalid_transition`
- `not_resumable`
- `resume_metadata_invalid`
- `limit_exceeded`
- `plan_budget_required`
- `persistence_failed`
- `side_effect_start_failed`
- `checkpoint_failed`
- `rollback_failed`
- `logging_degraded` (secondary warning; never replaces the primary error)

An access-denied error names the affected framework path and operation ID without
claiming success. The Supervisor prompt continues to prohibit prose-only success
claims: it must base its answer on the returned committed result.

Publishing to subscribers happens after canonical commit. A subscriber failure is
logged but cannot make a committed transaction appear rolled back. On reconnection,
the normal state snapshot is authoritative.

Downstream Search, Plan, validation, and external-dispatch exceptions continue to
write full tracebacks to the session/backend log. Where an asynchronous job was
started by a control transaction, its records carry the originating operation ID for
correlation; the user-visible event remains concise and redacted.

## Frontend workspace visibility

`ContextSidebar` builds current and recent workspace groups as it does today, then
filters `group.sessions.length === 0` before rendering. It no longer renders the
per-group `暂无会话` placeholder.

The current workspace uses the backend-authoritative session list. Other workspaces
use their existing cached authoritative summaries; an absent/empty cache hides the
group until that workspace is selected and hydrated. This may hide an old valid
workspace whose cache predates the feature, but the workspace picker still exposes
the recent root, so no access or data is lost. This design intentionally avoids a
fan-out RPC across every recent filesystem path merely to render the sidebar.

Tests must prove that rendering does not remove localStorage keys, mutate
`recentRoots`, call session deletion, or touch disk paths.

## Security and permission boundaries

- Existing generic shell/write tools continue rejecting writes under `.athena/**`.
- ResearchContext is an allowlisted projection, not `ResearchState.model_dump()`;
  credentials, raw artifacts, and unbounded model-authored text are excluded.
- State action schemas use strict discriminated unions and phase-specific
  projection. Unknown fields and invalid enum values fail before persistence.
- Path-bearing references are canonicalized into allowlisted session/artifact roots;
  traversal, symlink escape, and arbitrary absolute-path checkpointing are rejected.
- Mutation tools are never concurrency-safe and cannot execute in parallel with
  another state/tree mutation.
- Only the process holding the exclusive session-owner lock may expose mutation tools
  or start scheduler work for that session.
- Journal and error output are sanitized before human projection; backend logs keep
  the traceback but still apply known-secret redaction.
- General Agent side effects remain outside the transaction and are never described
  as rollback-safe.
- A revealed final score is not fed back into Search prompts, and reusing its
  evaluator cannot be presented as fresh independent validation.

## Compatibility and migration

Core `state.json` retains its existing schema. The per-session validation policy,
automatic-validation override, and final-evaluator generation marker are stored in
the digest-bound resume sidecar; `interaction_mode` is derived and is not persisted.
An older Athena release can still parse the core state, but parsing does not imply
safe downgrade execution because it ignores the new sidecar keys. A launcher/runtime
capability marker must therefore prevent an older backend from auto-starting such a
resumed session. The documented downgrade procedure resolves active journals, parks
affected sessions at SEARCH/WAITING, and disables validation globally before an old
backend is allowed to open them writable. The control journal uses a new
framework-owned filename and is resolved by the new runtime before normal loading.

The implementation should proceed in compatibility stages:

1. Add transaction/recovery services and tests while retaining existing public
   tools.
2. Route existing Python action methods through those services where applicable.
3. Add compact context injection and the consolidated tools; keep old methods as
   internal adapters.
4. Cut the old names from the model-visible registry and update the Supervisor
   prompt.
5. Add completed-session routing, natural-language and `/search +N` resume paths.
6. Apply the independent sidebar visibility filter.

A release rollback must first use the new code to resolve every active transaction
journal and apply the downgrade parking/validation-disable procedure, then may restore
the old registry/prompt. It must not delete journals, hand a possibly half-installed
state pair to an older binary, or let an old runtime ignore a safety override while
running Search.

## Testing strategy

### Tool and context contracts

- Snapshot the normal and Kaggle tool-name sets and enforce the maximum count.
- Snapshot public schemas and prove both mutation tools have only
  `context_version`/`action` at the top level, while the read tool has only
  `section`/`cursor`/`page_size`; framework-owned transaction parameters are absent.
- Assert phase-aware schemas expose only legal action variants.
- Prove compact context is deterministic, bounded, credential-free, and uses a
  changing version when relevant state/tree data changes.
- Prove `inspect_research` pagination is stable and bounded.
- Prove each mutation schema rejects coercion, unknown fields, empty updates, and
  out-of-range limits.

### Transaction and recovery

- Inject failures before journal write, after PREPARED, between core/resume writes,
  after STATE_COMMITTED, and while starting the scheduler.
- At every point, assert the documented before/after recovery state, shared-object
  identity, journal phase, and full error logging.
- Simulate Windows `PermissionError`/`WinError 5`, including rollback failure and
  successful recovery after the handle is released.
- Fail each logging sink in turn and prove fallback preserves the original traceback;
  if all durable sinks fail, prove `logging_degraded` is surfaced without masking it.
- Cancel the tool at each await boundary and prove cancellation cannot leave an
  unclassified journal.
- Submit two mutations concurrently and prove serialization or `stale_context`,
  never lost updates.
- Start competing backend processes against one session, including the supported
  Windows/WSL shared path, and prove exactly one becomes writable while the other
  receives `session_busy`; prove crash release and clean reacquisition.
- Replay the same provider call ID and prove idempotent output; issue a new call ID
  and prove it is a new operation.
- Reuse one operation ID with different arguments and prove `operation_mismatch`
  occurs before any state change.
- Prove two calls to the same tool in one Agent turn receive different real call IDs.

### Search resume

- Build a COMPLETED fixture with settled experiments, trusted SOTA/evaluator,
  validation/report refs, and no live Plans; add 20 and assert limit equals the
  command-time attempt count plus 20.
- Prove old validation is absent from current state but remains discoverable through
  the immutable resume audit and referenced artifact/run history after completion
  ledger compaction.
- Prove the revealed final-evaluator generation is marked consumed, the score is not
  injected from state, artifacts, or prior transcript into the new epoch's
  Plan/Ideator context, and no later report claims independent final validation
  without a fresh evaluator or explicit exploratory-reuse acknowledgement.
- Reconstruct the runtime from disk and prove the per-run automatic-validation
  override remains false.
- Remove or corrupt the resume sidecar after a committed resume and prove startup
  fails closed at SEARCH/WAITING with `resume_metadata_invalid`, never VALIDATE.
- Run the scheduler with fakes until the allowance is consumed and assert the final
  state is SEARCH/WAITING and validation was never called.
- Race concurrent Plan starts at the final remaining slot and prove the shared
  mutation coordinator cannot overshoot the accepted limit.
- Prove neither the Scheduler nor a model-authored phase action can bypass the
  manual-only validation policy; only the gateway's user confirmation can do so.
- Cover SEARCH/WAITING, in-flight attempt accounting, manual mode, exhausted Plan
  budget, no viable hypotheses, missing SOTA/evaluator, FAILED, and STOPPED.
- Drive a fake Supervisor provider from Chinese and English resume requests and
  assert it chooses
  `update_research(action={"kind":"resume_search","additional_attempts":20})`.
- Prove `/search +N` reaches the same transaction implementation.

### GUI

- `interaction_mode` is the primary prose-routing input; the legacy heuristic is used
  only when an older backend omits the field.
- A completed session with research history sends prose to `sendControl` rather than
  starting task clarification.
- A genuinely empty new session retains the clarification path.
- A successful resume updates attempts/limit/status from backend state events and
  shows the committed Supervisor answer.
- Current and cached workspace groups with zero sessions are absent, while groups
  with sessions retain stable ordering and navigation.
- New Session and Switch Workspace remain usable when all groups are hidden.
- localStorage, recent roots, backend deletion, and disk are unchanged by hiding.

### Regression gates

Run focused Supervisor, scheduler, lifecycle, persistence, GUI handler/transport,
`usePipeline`, AppShell/sidebar, and workspace-storage suites; then run the complete
backend and frontend suites, frontend production build, and Tauri Rust tests. Normal
tests use fake providers and no network.

## Acceptance criteria

1. Normal Supervisor turns expose no more than five core tools; optional Kaggle read
   capability is the only phase-dependent external addition.
2. The model receives a bounded current snapshot without first calling three read
   tools and can request paginated details through one read tool.
3. No Supervisor-accessible tool can write arbitrary `.athena` content or arbitrary
   ResearchState paths.
4. All state mutations are strict, version-checked, serialized, and backed by a
   crash-recoverable journal.
5. Every injected commit/start failure either restores the before state or reports
   `rollback_failed` while retaining a recoverable journal; no failure is reported as
   successful rollback without evidence.
6. Repeating one resume operation ID cannot add SEARCH budget twice, including after
   bounded completion-ledger compaction.
7. From a valid COMPLETED session with A accepted attempts, “再 search 20 个” commits
   a limit of A+20, clears only the current validation identity, retains history,
   disables automatic validation durably, and starts SEARCH.
8. The resumed run parks at SEARCH/WAITING after consuming its allowance and never
   enters VALIDATE unless a later explicit human action requests it.
9. Merely opening a completed session never resumes it; an explicit transaction is
   required.
10. Empty workspace groups are absent from the sidebar with zero persistence or
    deletion side effects.
11. Full tracebacks and rollback outcomes are present in backend logs, while
    human-facing errors remain concise and redacted.
12. A revealed final evaluator is marked consumed, its score is excluded from resumed
    Search context, and any later reuse is explicitly labelled exploratory rather
    than falsely reported as independent validation.
13. Parallel Plan starts cannot overshoot the accepted Search limit, and a competing
    Windows/WSL backend cannot become a second writer for the same session.
14. With automatic validation disabled, neither the Scheduler nor a model-authored
    action can enter VALIDATE without a user-originated gateway confirmation.
15. The public parameter budget is enforced by schema snapshots, legacy aliases are
    absent from the registry, and there is one implementation path per domain action.

## Repository sequencing

`codex_docs/CURRENT.md` currently points to the unfinished
`2026-09-03-llm-task-understanding-output` implementation plan. That work owns
`athena-gui/src/types/ui.ts` and `usePipeline.ts`, which overlap this design's input
routing. In accordance with the repository instructions, this design is queued and
must not replace the active plan or modify those files until the active plan is
completed and its closeout updates `CURRENT.md`. The implementation plan for this
design must be written against the resulting head, with a fresh overlap audit.

To keep review units small, planning is split after that audit: a backend plan owns
transactional persistence, Supervisor APIs, Search resume, lifecycle policy, and the
derived interaction mode; a dependent frontend plan owns interaction-mode routing and
empty-workspace presentation only. The backend contract and compatibility tests must
be green before frontend implementation begins. Neither plan duplicates domain rules
from the other.
