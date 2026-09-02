# Task Clarification Confirmation Gate Design

Date: 2026-09-01
Status: approved for implementation planning

## 1. Problem summary

Athena currently starts the research lifecycle as soon as the user submits a task. Task
understanding and human clarification then run inside the already-running PREPARE
lifecycle. The GUI still renders an intent preview card with a confirmation affordance,
but new tasks mark that card as started immediately. The card is therefore a status
display rather than a decision gate.

The existing clarification implementation also has contract and durability problems:

- Python, Rust, and TypeScript do not share one reply shape. Native Tauri choice and
  skip calls do not match the Rust command signature.
- Human replies are flattened into ambiguous strings such as `choice:f1` and `skip`.
- The runtime's answer recorder records an awaitable before it has obtained the answer.
- Clarification Q&A is transient and can be lost or replaced during checkpoint resume.
- The promised maximum of eight questions is not enforced by deterministic code.
- Task-understanding failure can continue into PREPARE without a confirmed contract.
- The clarification handoff is not a named file and is not delivered uniformly to every
  downstream phase that depends on task intent.

## 2. Product decision

Restore the intent preview as a real decision gate. No data download, EDA, model
training, SEARCH, or VALIDATE work may begin until the user confirms the latest
clarification draft.

The authoritative state flow is:

```text
IDLE
  -> CLARIFYING
  -> READY_FOR_CONFIRMATION
       |-> revise -> CLARIFYING
       |-> cancel -> IDLE
       `-> confirm latest revision -> RUNNING / PREPARE
```

`CLARIFYING` and `READY_FOR_CONFIRMATION` are pre-run states. They must not be
represented as `RUNNING`, and they must not start the research Supervisor lifecycle.

## 3. Goals

- Ask only questions whose answers can change the executable task contract.
- Persist every settled question before asking the next question.
- Present the latest task-understanding draft, Q&A, and unresolved items for review.
- Start PREPARE only after an atomic confirmation of the latest draft revision.
- Use one typed human-request contract across Python, Rust, TypeScript, WebSocket, and
  Tauri transports.
- Preserve old free-text callers during a bounded compatibility window.
- Make task context available consistently to all downstream agents that need it.
- Make timeout, explicit skip, cancellation, and transport failure distinguishable.

## 4. Non-goals

- Redesigning the research phase machine after PREPARE starts.
- Adding confidence-based automatic confirmation.
- Automatically editing datasets or evaluation code from the preview card.
- Replacing the general agent runtime's `request_user_input` tool.
- Making every non-critical TaskUnderstanding field mandatory.

## 5. Clarification draft model

The pre-run workflow owns a persisted, versioned `ClarificationDraft`. It is separate
from `ResearchState.task_understanding`, which remains the confirmed run contract.

```python
class DraftUnderstanding(BaseModel):
    title: str
    dataset: str | None
    target: str | None
    task_type: Literal["classification", "regression", "ranking", "other"]
    primary_metric: str | None
    direction: Literal["maximize", "minimize"] | None
    evaluation_plan: str | None

class ClarificationAnswer(BaseModel):
    request_id: str
    question: str
    outcome: Literal["choice", "text", "skip", "timeout", "cancelled"]
    value: str | None
    choice_label: str | None
    answered_at: str

class UnresolvedItem(BaseModel):
    field: str
    reason: str
    critical: bool

class ClarificationFailure(BaseModel):
    code: str
    message: str
    retryable: bool

class ClarificationRevision(BaseModel):
    base_revision: int
    instruction: str
    requested_at: str

class ClarificationDraft(BaseModel):
    schema_version: Literal[1]
    draft_id: str
    revision: int
    session_id: str
    original_task: str
    status: Literal[
        "CLARIFYING", "READY_FOR_CONFIRMATION", "CONFIRMED", "CANCELLED", "FAILED"
    ]
    understanding: DraftUnderstanding
    answers: list[ClarificationAnswer]
    revisions: list[ClarificationRevision]
    unresolved: list[UnresolvedItem]
    pending_request: HumanRequest | None
    failure: ClarificationFailure | None
    questions_asked: int
    created_at: str
    updated_at: str
```

Rules:

- `revision` increases after every persisted answer and every regenerated understanding.
- `questions_asked` is enforced by the controller, not by an LLM prompt.
- The hard maximum is eight questions per draft.
- A resumed session loads the draft and continues from persisted Q&A. It never replaces
  an existing detailed handoff with an empty reconstruction.
- Draft fields may be unknown. The confirmed run contract must never acquire `accuracy`
  or `maximize` merely from model defaults.
- Dataset, target, primary metric, direction, and evaluation plan are critical when the
  selected task type requires them. The controller records every missing required field
  as an `UnresolvedItem(critical=True)`; task-type-specific validation determines when a
  target or metric is not applicable.

## 6. Human request and reply contract

Human requests remain reusable for pre-run clarification and runtime questions, but
their scope and reply are explicit.

```json
{
  "request_id": "req-123",
  "session_id": "s-9",
  "scope_id": "clarify-123",
  "scope_kind": "clarification",
  "prompt": "Which metric should be primary?",
  "choices": [
    {"label": "ROC-AUC", "value": "roc_auc"},
    {"label": "F1", "value": "f1"}
  ],
  "allow_custom": true,
  "allow_skip": true,
  "created_at": "2026-09-01T10:00:00Z",
  "expires_at": "2026-09-01T10:02:00Z"
}
```

The client submits exactly one discriminated reply:

```json
{"kind": "choice", "value": "roc_auc"}
{"kind": "text", "text": "use macro F1"}
{"kind": "skip"}
```

Server-generated outcomes use `timeout` or `cancelled`; clients cannot forge those
outcomes. The broker validates all of the following before settling a request:

- the request belongs to the active session and scope;
- exactly one reply variant is present;
- a choice value occurs in the request's choices;
- choices contain two or three items with unique, non-empty values;
- at least one response mode is available;
- prompt, label, value, and custom-text length limits are satisfied;
- `allow_custom` and `allow_skip` are actual booleans.

Python dataclasses or Pydantic models, Rust Serde enums, and TypeScript discriminated
unions must encode the same contract. The native and WebSocket adapters must share
contract fixtures so transport-specific parameter drift fails tests.

## 7. Backend workflow and APIs

Task clarification is started separately from research execution.

### 7.1 Start or resume clarification

```text
task_clarification_start({ task })
  -> { draft_id, revision, status }
```

The operation creates or resumes the current session's draft and schedules only the
clarification controller. It does not call `ResearchRuntime.start()`.

Only one non-terminal draft may exist per session. Starting with identical normalized
task text resumes it; different task text returns a conflict until the caller explicitly
cancels the existing draft.

### 7.2 Read the authoritative draft

```text
task_clarification_get({ draft_id })
  -> ClarificationDraft
```

The GUI uses this readback after reconnects, missed events, and stale-revision errors.

### 7.3 Confirm and start atomically

The existing research start call becomes:

```text
start_search({ draft_id, revision, acknowledge_unresolved })
```

The backend performs one serialized operation:

1. Verify the draft belongs to the active session.
2. Verify its status is `READY_FOR_CONFIRMATION`.
3. Compare the submitted revision with the persisted revision.
4. Require an explicit acknowledgement when critical unresolved items remain.
5. Materialize the confirmed TaskUnderstanding and clarification handoff.
6. Mark the draft `CONFIRMED`.
7. Commit the state changes.
8. Start PREPARE.

If any step before the commit fails, PREPARE remains unstarted. A stale revision returns
a typed conflict and the GUI reloads the latest draft.

### 7.4 Revise or cancel

```text
task_clarification_retry({ draft_id, revision })
task_clarification_revise({ draft_id, revision, instruction })
task_clarification_cancel({ draft_id, revision })
```

Retry clears a retryable failure and returns the draft to `CLARIFYING`. Revision returns
the draft to `CLARIFYING` without discarding prior answers or revision instructions.
Cancellation settles any pending broker request and returns the session to `IDLE`.

## 8. Controller behavior

The clarification controller owns the deterministic loop:

```text
load draft
while questions_asked < 8:
    generate next question or final draft
    validate model output
    if final: persist READY_FOR_CONFIRMATION and stop
    persist pending question metadata
    await one typed human outcome
    persist the outcome and increment revision
on limit:
    generate the best draft from persisted evidence
    record missing critical facts in unresolved
    persist READY_FOR_CONFIRMATION
```

The LLM may decide that no further answer changes the contract, but it cannot bypass the
question counter, validation, persistence, or confirmation gate.

LLM or provider failure sets the draft to `FAILED` with a retryable error. A deterministic
heuristic may populate a replacement draft, but it remains unconfirmed and visibly lists
its unresolved/defaulted fields. Failure never starts PREPARE automatically.

## 9. Persistence and task-context delivery

The persisted `ClarificationDraft` is canonical before confirmation. Confirmation derives
two outputs from the same revision:

- `ResearchState.task_understanding`: structured confirmed run contract;
- `TASK_CLARIFICATION.md`: human-readable immutable handoff containing the original task,
  structured Q&A outcomes, confirmed understanding, unresolved items, draft ID, and
  revision.

The Markdown file is materialized under the session's handoff directory and also stored
in the content-addressed artifact store. `handoff_refs["task_clarification"]` references
that exact content.

A centralized task-context provider supplies the confirmed understanding and handoff to
PREPARE, SEARCH Ideators, Plan agents, Evaluators, and VALIDATE prompts. Individual agents
must not implement one-off `task_clarification` lookups.

## 10. Frontend behavior

Submitting a new task creates a clarification draft instead of marking a run as started.

- `CLARIFYING`: show task-understanding progress and any active question dialog.
- `READY_FOR_CONFIRMATION`: render the authoritative preview with `Confirm and start`,
  `Revise`, and `Cancel` actions.
- `CONFIRMED/RUNNING`: mark the confirmed revision as started and show research progress.
- `FAILED`: show the error with retry and cancel actions.

Human-request creation and settlement are pushed through the existing event subscription.
`human_pending` remains a recovery/readback endpoint and is not gated on `RUNNING` status.
Polling may remain as a low-frequency reconnect fallback, but polling errors must surface
after a bounded retry threshold.

The dialog resets local input when `request_id` changes, disables actions while settling,
shows stale/expired outcomes, traps focus, provides an accessible label, supports keyboard
navigation, and returns focus after closing.

## 11. Compatibility and migration

- Existing persisted runs with `task_understanding` are treated as already confirmed and
  resume without a new gate.
- Existing detailed `handoff_refs["task_clarification"]` values are retained verbatim.
- Existing free-text `human_reply(request_id, answer)` remains accepted for one migration
  path and is converted to `{kind: "text", text: answer}` at the boundary. Removing this
  path is outside this work and requires a separately approved deprecation plan.
- New choice and skip replies use the structured payload in Python, Rust, and TypeScript.
- Headless and CLI callers must choose an explicit policy: interactive confirmation or
  explicit `auto_confirm=True`. There is no implicit auto-confirm default.
- Rollback may disable the GUI gate behind a configuration flag, but it must not delete
  draft or handoff data. Auto-confirm under rollback must still be explicit.

## 12. Error handling

- Timeout persists an outcome of `timeout`; it is never recorded as a user skip.
- Session switch or run cancellation settles scoped requests as `cancelled`.
- Artifact write or state-save failure leaves the draft unconfirmed and surfaces an error.
- Missing or corrupt handoff content is a confirmation/start blocker until regenerated
  from the canonical draft.
- A stale or duplicate reply returns a typed reason; the GUI readbacks current state rather
  than silently removing the question.
- Double confirmation is idempotent for the same draft and revision.

## 13. Acceptance criteria

1. Submitting a task cannot start PREPARE before the latest draft revision is confirmed.
2. The GUI visibly distinguishes `CLARIFYING`, `READY_FOR_CONFIRMATION`, and `RUNNING`.
3. Choice, text, and skip replies work identically through WebSocket and native Tauri.
4. Invalid, conflicting, stale, cross-session, and duplicate replies are rejected with
   typed errors.
5. The controller never asks more than eight questions and accepts only two or three
   unique choices.
6. Explicit skip, timeout, cancellation, and free text remain distinguishable in state and
   in `TASK_CLARIFICATION.md`.
7. Restarting during clarification preserves settled Q&A and resumes without overwriting
   the detailed handoff.
8. LLM failure never starts PREPARE and gives the user a retryable or reviewable draft.
9. Confirming a stale revision cannot start a run.
10. Confirmation atomically persists the confirmed TaskUnderstanding, named Markdown
    handoff, artifact ref, and confirmed draft revision before PREPARE starts.
11. PREPARE, Ideator, Plan, Evaluator, and VALIDATE receive the same confirmed task context.
12. Existing confirmed checkpoints resume without a new confirmation prompt.
13. The dialog passes keyboard, focus-management, and accessible-name tests.
14. Python unit/integration tests, TypeScript component/hook/bridge tests, Rust contract
    tests, and a native/WebSocket end-to-end clarification test all pass.

## 14. Risks and mitigations

- **State-machine expansion:** keep pre-run draft states outside the research phase enum and
  expose one projection layer to the GUI.
- **Cross-language drift:** generate or fixture-test the same JSON examples in all three
  languages.
- **Resume ambiguity:** bind every draft and request to a session and persist every settled
  answer before continuing.
- **Duplicate sources of truth:** treat the draft as canonical before confirmation and the
  confirmed TaskUnderstanding as canonical afterward; Markdown is always derived.
- **Compatibility pressure:** isolate legacy free-text conversion at the RPC boundary;
  this work does not remove that compatibility path.
- **Extra click:** keep confirmation to one clear action and preselect no hidden defaults.

## 15. Rollback strategy

The change is rolled out behind `task_confirmation_gate`, which defaults to `true` for the
GUI. Rolling back sets it to `false` and disables the new GUI entry path while preserving
draft schemas, confirmed state, and handoff artifacts. With the flag off, starting from raw
task text still requires the caller to pass `auto_confirm=True`; no rollback path may
reinterpret an unconfirmed draft as a confirmed run implicitly.

## 16. Open questions

None required for implementation planning. The product decision is to require explicit
confirmation before PREPARE.
