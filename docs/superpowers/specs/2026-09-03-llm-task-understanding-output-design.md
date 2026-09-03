# LLM Task-Understanding Output Design

Date: 2026-09-03
Status: approved for implementation

## Problem

The GUI already has a live "task understanding" activity region, but the
production clarification path never supplies it with model activity. The GUI
starts `task_clarification_start`, which currently uses
`DeterministicClarificationGenerator`; that generator asks a fixed sequence of
questions without invoking an LLM or publishing output events. Rendering more
frontend placeholders would therefore misrepresent deterministic state as model
thinking and would not satisfy the requested behavior.

The task-understanding path must become genuinely LLM-driven and expose only
content that the model explicitly marks as public. The enforceable boundary is a
data flow: provider/Agent/runtime raw fields have no direct forwarding path to
task-understanding output; model-authored content enters through the strict
`PublicProgress.summary` field only; and the existing output projector redacts
known credential forms. This does not claim a provenance-independent proof that
arbitrary model text can never resemble a prompt, JSON, exception, or private
reasoning. Tests instead prove raw-event suppression and known-secret redaction
of an accepted public summary.

## Product decision

Add a dedicated LLM clarification generator built on Athena's existing `Agent`
loop, provider abstraction, artifact store, and structured-output validation.
The generator may call one narrow progress-reporting tool while it works and
must return a validated public summary together with either the next question or
the final task-understanding draft.

The existing controller remains authoritative for question limits, human
requests, persistence, revisions, retry, cancellation, and confirmation. The LLM
does not gain permission to start PREPARE or mutate research state.

## Implementation preflight evidence

- At planning/implementation preflight, the isolated worktree was clean at
  `00520b097b90a4724a434e1cc1f9df1ffd781e52` (before implementation).
- The focused backend command exited 0 with `125 passed` in 13.91s. The focused
  frontend command exited 0 with 5 files / `68 tests` in 3.03s.
- `npm --prefix athena-gui ci` exited 0; its own output reported 8 findings
  (1 low, 4 moderate, 2 high, 1 critical). This is not a claim that a separate
  `npm audit` command was run. Original command details remain in the ignored SDD
  ledger and must be distinguished from changes introduced by this work.

## Alternatives considered

1. **Dedicated LLM clarification Agent (selected).** This reuses the real Agent
   loop, supports observable tool calls, and preserves the confirmation boundary.
2. **Reuse the Supervisor Agent.** Rejected because it would couple pre-run
   clarification back to the research lifecycle and its broad tool surface.
3. **Call the provider directly for JSON.** Rejected because it duplicates Agent
   retry/validation behavior and cannot honestly expose Agent-loop tool activity.

## Components

### Structured public result

Reuse the existing discriminated `ClarificationStep` union and add only the two
small boundary models that the Agent path needs:

```python
ProgressStage = Literal["analysis", "question", "synthesis"]

class PublicProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    stage: ProgressStage
    summary: str = Field(min_length=1, max_length=600)

class ClarificationModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    public_update: PublicProgress
    step: ClarificationStep
```

`ClarificationModelOutput` is required because `ClarificationStep` is an
`Annotated` union, not a `BaseModel` class accepted by `Agent.output_type`.
Question and final DTOs are not duplicated.

This design creates no dedicated task-understanding output DTO. `PublicProgress`
and `ClarificationModelOutput` are strict model/tool validation envelopes;
runtime transport continues to use the extended generic `OutputEvent`.

The same `PublicProgress` validation is used for both the progress tool and the
final envelope. Before length validation, its summary is normalized with NFKC;
whitespace (including control whitespace) is converted and collapsed to single
spaces; remaining Unicode control/surrogate characters (`Cc`, `Cf`, `Cs`) are
removed; and surrounding whitespace is stripped. Empty or over-600-character
content is invalid; it is not silently truncated. This is a public
decision/conclusion summary, never a request for or representation of hidden
reasoning.

The existing generator protocol remains source-compatible. A small internal
`ClarificationTurnResult(step, public_update=None)` value and invocation helper
normalize either a legacy raw `ClarificationStep` (deterministic generators) or
a strict `ClarificationModelOutput` (the LLM generator). Existing callers of
`generate_step` continue to receive a raw step.

### LLM clarification generator

Add `LLMClarificationGenerator`, implementing the existing asynchronous
`ClarificationGenerator` protocol. Each `next_step(draft)` call:

1. Calls a pure `build_clarification_prompt(draft) -> str` projection. The
   projection receives only the canonical draft and serializes, in this fixed
   order, `original_task`, `understanding`, `answers`, `revisions`,
   `unresolved`, and `questions_asked`; it has no provider/client/config, Agent
   memory, filesystem, trace, or runtime-state input. User-controlled values are
   JSON data inside an explicitly delimited block, not prompt instructions. The
   deterministic prompt budget is defined below.
2. Creates a one-turn Athena `Agent` with the configured provider, the local
   artifact store, strict output envelope, and the progress tool.
3. Runs the real Agent loop with a fresh thread/turn identity and passes the
   exact result of `build_clarification_prompt(draft)` as
   `AgentContext.input_text`; the provider therefore receives canonical evidence
   only through that bounded request.
4. Uses a dedicated `AgentContext.emit` adapter that drops every generic Agent
   event. In particular, it never forwards structured JSON text deltas,
   function-call arguments, tool acknowledgements, or completion payloads.
5. Lets the reporting tool publish validated progress through a narrow abstract
   sink with `persist=False`. Repeated `(stage, normalized summary)` reports are
   emitted once within this generator invocation.
6. Returns the validated envelope. It does not publish the envelope's final
   update itself; the controller commits the canonical draft first.

#### Deterministic prompt encoding and budget

`MAX_CLARIFICATION_PROMPT_CHARS` is 20,000. Fixed instructions and delimiter
text are a reviewed static literal of at most 8,000 characters, leaving an
encoded state budget of 12,000 characters. The builder does not use a runtime
`assert` and does not replace the state with a three-key `truncated`/`head`/`tail`
object.

The builder constructs the six-key object in the order above and uses
`model_dump(mode="json")` only for its nested Pydantic values. It serializes with
`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`, then replaces every
literal `<` and `>` in that JSON source with `\u003c` and `\u003e` respectively
before inserting it between `<clarification_state>` and
`</clarification_state>`. `json.loads` therefore restores the original
user-controlled value, but a value such as `</clarification_state>` cannot close
the delimiter in the prompt source. Fixed instructions refer to the "delimited
JSON block" rather than spelling the closing delimiter, so the finished prompt
contains exactly one literal `</clarification_state>` marker.

All budget accounting uses the length after that safe encoding. First build the
complete normalized six-key state. If it is over the 12,000-character budget,
build a compact state with the same six keys and the following explicit canonical
evidence projection—there is no generic recursive "truncate every string"
operation:

- `original_task` is free text and may be shortened.
- `understanding` preserves `task_type`, `direction`, and every `null` value
  exactly; only its free-text fields (`title`, `dataset`, `target`,
  `primary_metric`, and `evaluation_plan`) may be shortened.
- Every `answers` item is projected as `{question, outcome, value, choice_label}`
  and omits audit-only `request_id` and `answered_at`. It preserves `outcome` and
  `null` values exactly; only `question`, non-null `value`, and non-null
  `choice_label` may be shortened.
- Every `revisions` item is projected as `{instruction}`. It omits the audit-only
  base revision and timestamp; `instruction` may be shortened.
- Every `unresolved` item is projected as `{field, reason, critical}`. It
  preserves `field` and `critical` exactly; only `reason` may be shortened.
- `questions_asked` remains its original integer.

The compact builder's encoded-character allocation is fixed: 2,400 for
`original_task`, 2,000 for `understanding`, 2,200 for `answers`, 1,800 for
`revisions`, 1,200 for `unresolved`, 64 for `questions_asked`, and 2,336 for the
six property names, delimiters, commas, braces, and safety margin. These add to
the 12,000-character state budget. Each named section budget covers only that
section's safely encoded JSON value; the separate 2,336-character reserve covers
top-level structure. Budget checks use encoded source length, never source-object
or pre-escape string length.

`shorten_free_text` selects the longest normalized free-text prefix whose safely
encoded value fits its remaining allocation, followed by the literal
`…[truncated]`; if no prefix fits, it uses the marker alone. Each projection
above calls that helper only for its listed free-text fields, in the listed field
order, until its record fits; enums, booleans, integers, identifiers, and `null`
are not shortened or coerced. A candidate unresolved item whose exact `field`
identifier alone cannot fit its remaining section budget is skipped rather than
misrepresented.

For `answers`, always include the latest record when one exists—even when its
outcome is `skip`, `timeout`, or `cancelled`—then consider older records from
newest to oldest while the section budget fits, finally restoring chronological
order. Do the same for `revisions`, always retaining the latest record when one
exists. For `unresolved`, consider critical items first in their original order,
then noncritical items in their original order, adding each projected item only
while the section budget fits. Thus selection is deterministic, latest special
outcomes are retained, and only explicitly free text can be abbreviated. Tests
cover each section's worst case and cap, deterministic repeated output, latest
`skip`/`timeout`/`cancelled` answers, field/null preservation, and the final
20,000-character limit with worst-case `<`/`>` escaping. The only limitation is
intentional bounded context: older answers/revisions and lower-priority
unresolved items may be omitted, while long free text may be abbreviated; the
top-level schema is never replaced by raw head/tail fragments.

Every call is stateless beyond the persisted draft. This avoids a second private
memory source and makes restart behavior depend only on canonical clarification
state.

### Public progress tool

The Agent receives one tool, `report_task_understanding`, whose input is the
strict `PublicProgress` schema. The tool name and scope are shared constants:

```text
REPORT_TASK_UNDERSTANDING_TOOL = "report_task_understanding"
TASK_UNDERSTANDING_SCOPE = "task_understanding"
```

Calling the tool is a real Agent-loop tool action. Its implementation has no
filesystem, network, shell, or lifecycle authority. It only publishes its
explicitly public summary through the injected sink and returns a fixed
acknowledgement to the model. The dedicated emit adapter suppresses the Agent's
generic function-call projection, so raw tool-argument fields have no direct
GUI or transcript projection path. The only model-authored sink input is a
runtime-validated `PublicProgress.summary`; the ordinary projector then redacts
known credential forms before live delivery or persistence.

The prompt asks the model to report meaningful progress before its final
structured answer. The tool call remains optional because not every compatible
provider reliably invokes tools; the required envelope still returns one
genuine model-authored public update for every successful `next_step` call. The
controller attempts to publish that update after its matching save. Actual GUI
delivery remains best-effort when a sink or subscriber fails.
`publish_public_progress` propagates `asyncio.CancelledError` from its direct
sink call; provider, outer-generator, and controller cancellation likewise
propagate. This contract deliberately does not require every internally spawned
generic-Agent subtool task to surface cancellation identically or change core
runtime cancellation behavior.

### Progress sink and commit ordering

`PublicProgressSink` is a tiny clarification-layer protocol, not a new event
service, ledger, or channel. It accepts a normalized summary, a stage
(`ProgressStage` or the controller-only `failure` stage), `session_id`, generic
`scope_id`, source, and persistence choice. Model-authored calls always supply
values taken from a validated `PublicProgress`; only the controller may supply
the fixed failure notice. The composition root adapts it to the existing
`RuntimeEvents.publish_output` path.

Commit ordering is part of the domain contract:

1. Progress-tool updates may be delivered live while the provider runs, but are
   ephemeral (`persist=False`).
2. For a question, the controller first saves the pending human request; for a
   final result, it first saves `READY_FOR_CONFIRMATION`.
3. Only after that save succeeds does the controller publish the envelope's
   final update with `persist=True` (a persistence request, not a durability
   guarantee).
4. On provider/schema/artifact failure, the controller first saves the retryable
   `FAILED` draft and then attempts exactly one fixed, persist-requested failure
   notice.
5. Sink/subscriber failures are logged and are non-fatal to the canonical draft.

The eight-question cap prevents another broker question, not the configured
generator call. At the cap the controller still invokes the selected generator
once. A returned final step is saved normally; a defensive returned question is
not sent to the broker and instead triggers the existing `best_final` projection
from persisted evidence. In both cases any returned public update is attempted
only after the ready draft save. A provider failure at the cap follows the same
retryable `FAILED` path and never silently falls back to deterministic generation.

This avoids persist-requested "ghost progress" if draft persistence fails and
keeps the generator independent of `RuntimeEvents`. It deliberately does not add
rollback or a progress ledger: the existing draft/store remains the sole source
of truth.

### Runtime event contract

Extend the existing output event with optional, generic, backward-compatible
metadata:

```json
{
  "type": "output",
  "session_id": "session-id",
  "scope": "task_understanding",
  "scope_id": "clarify-id",
  "source": "agent",
  "channel": "text",
  "text": "The target is known; I am confirming the primary metric."
}
```

`RuntimeEvents.publish_output` and `EventProjector.output` accept the optional
`session_id`, `scope`, and `scope_id` fields. They are an atomic group: all three
absent means a valid legacy unscoped event; all three non-empty means a scoped
event; a partial group is rejected/discarded and never downgraded to legacy.
`scope` remains a string so future workflows can reuse the contract, while this
feature emits exactly `task_understanding`. Existing callers and old transcript
records remain valid. Progress messages use `source="tool"` and
`tool="report_task_understanding"`; committed turn summaries use
`source="agent"`. `plan` is not overloaded for grouping.

`runtime/event_projection.py::supervisor_output` is part of this generic
contract. It completely forwards the current optional `EventProjector.output`
arguments—`message_id`, `plan`, `tool`, `artifact_ref`, `truncated`, and the
scope triple—rather than narrowing the adapter to scope alone. A future optional
parameter is not automatic: it requires an intentional adapter and composite
projection-test update.

The feature does not add an event type, WebSocket channel, database/table, or
dedicated frontend output DTO. Persist-requested events remain ordered in the
existing session transcript only when the best-effort append succeeds; live tool
progress uses the same subscription path with `persist=False`. The normal event
projector continues to redact known secret forms before display or storage. If a
`persist=True` transcript write fails, the event may still reach live subscribers
but has no replay row; the saved clarification draft remains authoritative and
is never rolled back. A successful append is replayed once through the existing
`message_id`/sequence semantics. No dedicated "partial metadata was logged"
output is created.

### Composition root

`ResearchRuntime` is the single provider composition root. When `model` is not
`None`, it constructs exactly one `ResponsesProvider(model, client=client)`,
passes that provider into `build_services`, and registers the same instance for
the Supervisor. `build_services` injects `LLMClarificationGenerator` only when a
provider is explicitly supplied; a configured model without its provider is a
composition error, not a deterministic fallback. The provider constructor is
side-effect free and may hold a lazy client; authentication or provider failure
on the first call follows the persisted `FAILED` behavior below.

When no model/provider is configured, provider-less tests and compatibility
callers retain `DeterministicClarificationGenerator`. A configured provider that
fails authentication or execution must produce the controller's existing
retryable `FAILED` draft; it never falls back within that runtime. Unit tests can
inject a fake `BaseProvider` directly into `build_services` or the generator.

The generator mode is intentionally not written into `ClarificationDraft`.
Drafts contain canonical user evidence, not the transient algorithm used to
interpret it. Resume/retry uses the current runtime's explicitly selected
provider; an operator who restarts without a model has explicitly selected the
existing deterministic compatibility path. This keeps schema version 1 readable,
requires no migration, and keeps rollback out of domain data. Feature rollback is
a normal Git/release operation (revert the feature or redeploy a prior release),
not removal or global reconfiguration of the model. Regardless of generator, the
same strict step schemas, question cap, persistence transitions, and confirmation
gate remain authoritative.

## Frontend behavior

The frontend continues to consume ordinary `output` events and the existing
renderer. `UIMessage` and `SessionRecord` expose optional `sessionId`/`session_id`,
`scope`, and `scopeId`/`scope_id` metadata without defining another output DTO.
The `SessionRecord` transcript comment names the actual runtime-relative path
`sessions/default.jsonl`, not `.athena/logs/output.jsonl`.
The reducer applies these rules:

- reject/discard a partially scoped record and discard a fully scoped output
  whose `session_id` is not the active session, both for live delivery and
  replay; neither case creates a dedicated log record or falls back to legacy;
- group `scope="task_understanding"` messages under the active clarification
  preview when `scope_id` matches its canonical draft id;
- while the start RPC is still pending and the optimistic preview has an empty
  draft id, synchronously latch the first matching-session task-understanding
  `scope_id` in both a ref and the preview state; subsequent optimistic activity
  joins that preview only when it has the same id;
- when the canonical draft arrives, replace the optimistic latch with its
  nonempty draft id and require strict equality. Earlier different-scope output
  remains ordinary history instead of being associated with the canonical card;
- if no active preview exists, render a valid scoped record as an ordinary Athena
  message rather than losing persisted history;
- show progress-tool output using the existing tool presentation;
- preserve the current positional grouping fallback only for records with all
  three scope fields absent;
- never interpret provider `reasoning_content` or raw JSON as a display message.

Session/workspace changes clear pending output batches. For initial hydration and
an explicit successful session switch, the sequence is: clear the batch/animation
frame; synchronously assign `activeSessionIdRef.current` to the new id; then call
`setCurrentSessionId`, restore records, and make the remaining state updates.
Capture the old ref before awaiting the switch RPC: a rejected or superseded RPC
leaves it unchanged, and a synchronous post-success failure restores it before
the error is reported.

`newSession` uses the same ordering at its optimistic start: capture the prior
ref and visible session state; clear the batch; synchronously set the ref to the
new session id; then install optimistic current-session/view state and start the
creation RPC. If the creation rejects while current, restore the captured ref,
former current session, and visible conversation before reporting the error;
preserve the existing failed optimistic row in the session list for compatibility
with the current retry/error presentation.
This closes the microtask window before React's later `useEffect` can update the
ref for both switching paths. No Rust payload schema change is required because
both transports carry generic JSON; contract tests prove the fields survive both
live and replay paths.

## Data flow

```text
GUI submits task
  -> task_clarification_start
  -> ClarificationController.run
  -> LLMClarificationGenerator.next_step(canonical draft)
  -> Athena Agent/provider loop
       -> report_task_understanding(summary)
          -> ephemeral scoped, redacted output -> GUI activity region
       -> validated ClarificationModelOutput
          -> public update + question/final step -> controller
  -> controller persists pending request or READY_FOR_CONFIRMATION
  -> controller publishes persist-requested scoped public update
```

PREPARE remains unreachable until the user confirms the final persisted draft.

## Error and safety behavior

- Provider, schema, or artifact failures propagate to the controller, which
  persists the existing retryable `FAILED` state, then attempts one fixed
  `"Task understanding failed. Retry to continue."` output with
  `channel="error"`. Cancellation from the provider, outer generator/controller,
  or direct public-progress publication continues to propagate and is not
  converted into provider failure. The design makes no stronger claim about
  arbitrary generic-Agent subtool-task cancellation behavior.
- Cancellation never rolls back a draft already saved before publication: during
  generation it leaves the original `CLARIFYING` draft; during question
  publication it leaves `CLARIFYING` with its saved `pending_request`; during
  final publication it leaves `READY_FOR_CONFIRMATION`; and during failure-notice
  publication it leaves `FAILED`. All four cases propagate `CancelledError`.
- Fixed failure text is the only controller-authored failure content. The
  adapter does not directly forward exception, traceback, prompt, tool-argument,
  or raw-provider fields to output; model-authored input reaches output only as a
  validated `PublicProgress.summary`, followed by known-secret redaction.
- Subscriber/display publication failures are logged and do not change the
  canonical clarification result.
- Public summaries use the exact validation above and then pass through the
  existing projector's known-secret redaction. The design does not claim that a
  pattern-based redactor can identify every unknown sensitive phrase; instead it
  objectively guarantees that only explicitly typed public fields reach the
  projector and that known credential forms are redacted.
- The adapter drops all raw structured text deltas and unknown event kinds;
  raw-event-suppression tests exercise that projection boundary.
- The generator has no research-start, filesystem-write, shell, or network tool.
- Hidden chain-of-thought and provider-private reasoning are not requested by the
  feature and have no output projection path.
- The Agent may store its fully validated envelope in the existing artifact
  store. This feature adds no artifact/transcript writer for raw streams,
  prompts, private reasoning, or debug traces.

## Software-engineering constraints

- **Single responsibility:** the generator interprets canonical evidence; the
  controller owns workflow transitions and commit ordering; `RuntimeEvents`
  owns display projection and persistence; the frontend owns presentation.
- **Dependency inversion:** clarification code depends on `BaseProvider`,
  `ArtifactStore`, and the narrow progress-sink protocol, never on a concrete API
  client or GUI transport.
- **Open/closed event contract:** generic scope metadata extends `output`
  without branching a task-specific transport or breaking legacy records.
- **Canonical state:** no Agent memory, progress ledger, or generation-mode
  field competes with `ClarificationDraft` and `ClarificationStore`.
- **Failure atomicity:** persist-requested summaries follow successful draft
  saves; live progress is explicitly ephemeral; notification or transcript-write
  failure never rewrites domain state.
- **Minimal surface:** no Supervisor reuse, no persistent clarification Agent,
  no provider changes, no reasoning event, no new renderer, and no transcript
  migration.

The runtime transcript is named `default.jsonl`, but each GUI session owns a
session-specific state root and therefore a distinct `sessions` directory. The
new `session_id` metadata is still required for stale-subscription defense and
contract clarity; this task does not re-architect transcript storage.

## Testing strategy

Use test-driven development with a fake streaming provider so the real Athena
`Agent` loop executes without network access.

Backend tests prove:

- `PublicProgress` has strict normalization, length, and extra-field behavior;
- a progress tool call is delivered ephemerally before the validated model
  result, while the final update is published only after the canonical save;
- repeated identical tool reports are emitted once within one generator call;
- raw JSON text deltas, tool arguments, acknowledgements, and unknown Agent
  event kinds have no direct output projection;
- the returned question/final step is schema-validated;
- the pure bounded prompt contains only the enumerated canonical draft evidence,
  safely encodes `<`/`>`, rejects delimiter closure, retains the fixed six-key
  schema under deterministic compaction, stays within 20,000 characters, and is
  the exact `AgentContext.input_text` observed by the scripted provider;
- accepted public summaries are length-bounded and known secret forms are
  redacted by the projector;
- provider failure saves a retryable failed draft before one generic
  persist-requested notice, while provider/outer-generator/controller and direct-publication
  cancellation propagates without changing generic-Agent subtool semantics;
- cancellation preserves the exact pre-publication canonical state in the four
  generation/question/final/failure-notice cases (`CLARIFYING`, `CLARIFYING` with
  pending request, `READY_FOR_CONFIRMATION`, and `FAILED` respectively);
- the selected generator is still invoked once at the eight-question cap; a
  final result is accepted, while a defensive question result triggers
  `best_final` without a ninth broker request and without publishing before save;
- configured runtimes choose the LLM generator while provider-less runtimes keep
  deterministic compatibility without constructing duplicate providers;
- task-understanding events use atomic generic session/scope/scope-id metadata,
  while partial metadata is rejected/discarded; a `persist=True` event replays
  once only after a successful transcript append, and append failure leaves the
  live event/draft authoritative without a replay row or rollback.

Frontend tests prove:

- scoped model and tool messages appear in the task-understanding activity region;
- optimistic empty-id previews latch the first matching-session scope id and
  accept no other scope; canonical previews then require a strict matching id;
- mismatched-session and partially scoped output are ignored;
- an old-runtime scoped event scheduled in the microtask before React's
  session-ref effect is ignored after a successful explicit switch and after
  optimistic `newSession`, while either failed path restores the old ref/current
  conversation and failed creation retains its existing optimistic session row;
- matching output remains batched and ordered;
- valid scoped history without an active preview remains visible as ordinary
  Athena output;
- legacy unscoped output behavior is unchanged.

Run focused Python and Vitest tests first, then the complete relevant Python
clarification/gateway suite, full frontend suite, production build, Rust tests,
`cargo check`, and diff checks.

## Acceptance criteria

1. The configured GUI task-understanding path invokes the real LLM before each
   clarification question or finalization decision, including the
   eight-question-cap decision; provider-less compatibility remains deterministic.
2. When the model calls the reporting tool, the generator attempts immediate,
   ephemeral publication; when the sink/subscriber succeeds, that real tool
   activity appears in the task-understanding region.
3. Every successful `next_step` returns one validated, model-authored public
   update even when the provider skips the optional progress tool. The controller
   attempts its `persist=True` publication only after the corresponding draft
   save; actual delivery remains best-effort on sink/subscriber failure.
4. Provider/Agent raw event fields have no direct task-understanding output path;
   model-authored output enters through strict `PublicProgress.summary`; tests
   prove raw-event suppression and known-credential redaction of accepted
   summaries.
5. Every new task-understanding output carries atomic session, scope, and generic
   scope-id metadata; stale-session or partially scoped output cannot enter the
   active frontend conversation.
6. Questions and final drafts still obey the existing strict schemas, eight-
   question cap, persistence rules, revision handling, and confirmation gate.
7. A configured provider failure persists a retryable `FAILED` draft and never
   starts PREPARE or silently falls back to deterministic output.
8. Provider-less compatibility callers retain deterministic clarification.
9. Sequential task-understanding publications within one generator invocation
   preserve order; identical progress-tool reports within that invocation appear
   once; each successfully appended persist-requested publication replays once.
   An append failure leaves the live draft/output authoritative without replay or
   rollback. A retry is a new invocation and may truthfully add a new update.
10. Focused Python and Vitest tests, the relevant Python clarification/gateway
    suite, full frontend tests, production frontend build, Rust tests, and
    `cargo check` pass. Any pre-existing warnings are recorded separately; this
    task introduces no new warning in changed files.

## Out of scope

- Exposing hidden model reasoning or token-level chain-of-thought.
- Giving the clarification Agent general web, shell, file-write, or research tools.
- Replacing the confirmation gate or changing PREPARE/SEARCH/VALIDATE behavior.
- Migrating old unscoped transcript rows.
- Changing the deterministic fallback used when no model is configured.
- Persisting generator/provider choice in clarification domain data.
- Adding a progress ledger, dedicated event channel, new renderer, or transport
  schema solely for task-understanding output.
