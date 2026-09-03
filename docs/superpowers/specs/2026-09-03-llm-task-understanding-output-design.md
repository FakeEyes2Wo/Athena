# LLM Task-Understanding Output Design

Date: 2026-09-03
Status: approved for implementation planning; independent engineering review incorporated

## Problem

The GUI already has a live "task understanding" activity region, but the
production clarification path never supplies it with model activity. The GUI
starts `task_clarification_start`, which currently uses
`DeterministicClarificationGenerator`; that generator asks a fixed sequence of
questions without invoking an LLM or publishing output events. Rendering more
frontend placeholders would therefore misrepresent deterministic state as model
thinking and would not satisfy the requested behavior.

The task-understanding path must become genuinely LLM-driven and expose only
content that the model explicitly marks as public. Provider-private reasoning,
raw structured JSON deltas, prompts, credentials, and hidden chain-of-thought
must never be projected into the GUI.

## Product decision

Add a dedicated LLM clarification generator built on Athena's existing `Agent`
loop, provider abstraction, artifact store, and structured-output validation.
The generator may call one narrow progress-reporting tool while it works and
must return a validated public summary together with either the next question or
the final task-understanding draft.

The existing controller remains authoritative for question limits, human
requests, persistence, revisions, retry, cancellation, and confirmation. The LLM
does not gain permission to start PREPARE or mutate research state.

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
   projection serializes, in a fixed order, only `original_task`,
   `understanding`, `answers`, `revisions`, `unresolved`, and
   `questions_asked`. It excludes provider/client/config objects, Agent memory,
   filesystem paths, traces, and runtime state. User-controlled values are JSON
   data inside an explicitly delimited block, not prompt instructions. Each
   string is capped at 2,000 characters and the complete prompt at 20,000
   characters; the most recent answers/revisions are retained when compaction is
   necessary.
2. Creates a one-turn Athena `Agent` with the configured provider, the local
   artifact store, strict output envelope, and the progress tool.
3. Runs the real Agent loop with a fresh thread/turn identity.
4. Uses a dedicated `AgentContext.emit` adapter that drops every generic Agent
   event. In particular, it never forwards structured JSON text deltas,
   function-call arguments, tool acknowledgements, or completion payloads.
5. Lets the reporting tool publish validated progress through a narrow abstract
   sink with `persist=False`. Repeated `(stage, normalized summary)` reports are
   emitted once within this generator invocation.
6. Returns the validated envelope. It does not publish the envelope's final
   update itself; the controller commits the canonical draft first.

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
generic function-call projection, so raw tool arguments never become GUI or
transcript records.

The prompt asks the model to report meaningful progress before its final
structured answer. The tool call remains optional because not every compatible
provider reliably invokes tools; the required envelope update still guarantees
one genuine model-authored public update for every successful `next_step` call.

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
   durable final update (`persist=True`).
4. On provider/schema/artifact failure, the controller first saves the retryable
   `FAILED` draft and then attempts exactly one durable, fixed failure notice.
5. Sink/subscriber failures are logged and are non-fatal to the canonical draft.

This avoids durable "ghost progress" if draft persistence fails and keeps the
generator independent of `RuntimeEvents`. It deliberately does not add rollback
or a progress ledger: the existing draft/store remains the sole source of truth.

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
event; a partial group is rejected and logged rather than downgraded to legacy.
`scope` remains a string so future workflows can reuse the contract, while this
feature emits exactly `task_understanding`. Existing callers and old transcript
records remain valid. Progress messages use `source="tool"` and
`tool="report_task_understanding"`; committed turn summaries use
`source="agent"`. `plan` is not overloaded for grouping.

The feature does not add an event type, WebSocket channel, database/table, or
frontend DTO. Durable events remain ordered in the existing session transcript;
live tool progress uses the same subscription path without being logged. The
normal event projector continues to redact known secret forms before display or
storage. Here, "durable" means `persist=True` through the existing best-effort
transcript writer: if the filesystem rejects that secondary record, the saved
clarification draft remains canonical and the existing logger records the
failure. Each publication uses the projector's existing unique `message_id`.

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
requires no migration, and makes rollback a composition change rather than a
domain-data downgrade. Regardless of generator, the same strict step schemas,
question cap, persistence transitions, and confirmation gate remain authoritative.

## Frontend behavior

The frontend continues to consume ordinary `output` events and the existing
renderer. `UIMessage` and `SessionRecord` expose optional `sessionId`/`session_id`,
`scope`, and `scopeId`/`scope_id` metadata without defining another output DTO.
The reducer applies these rules:

- reject/log a partially scoped record and discard a fully scoped output whose
  `session_id` is not the active session, both for live delivery and replay;
- group `scope="task_understanding"` messages under the active clarification
  preview when `scope_id` matches its canonical draft id;
- while the start RPC is still pending and the optimistic preview has an empty
  draft id, use the existing one-active-preview invariant to accept a matching-
  session task-understanding event, then use strict id matching once the backend
  draft arrives;
- if no active preview exists, render a valid scoped record as an ordinary Athena
  message rather than losing durable history;
- show progress-tool output using the existing tool presentation;
- preserve the current positional grouping fallback only for records with all
  three scope fields absent;
- never interpret provider `reasoning_content` or raw JSON as a display message.

Session/workspace changes already clear pending output batches. The new ingress
filter additionally closes the stale-runtime race for scoped clarification
events. No Rust payload schema change is required because both transports carry
generic JSON; contract tests prove the fields survive both live and replay paths.

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
  -> controller publishes durable scoped public update
```

PREPARE remains unreachable until the user confirms the final persisted draft.

## Error and safety behavior

- Provider, schema, or artifact failures propagate to the controller, which
  persists the existing retryable `FAILED` state, then attempts one fixed
  `"Task understanding failed. Retry to continue."` output with
  `channel="error"`. Cancellation continues to propagate and is not converted
  into provider failure.
- Exception text, tracebacks, prompts, tool arguments, raw provider output, and
  credentials are never used as public failure content.
- Subscriber/display publication failures are logged and do not change the
  canonical clarification result.
- Public summaries use the exact validation above and then pass through the
  existing projector's known-secret redaction. The design does not claim that a
  pattern-based redactor can identify every unknown sensitive phrase; instead it
  objectively guarantees that only explicitly typed public fields reach the
  projector and that known credential forms are redacted.
- The adapter drops all raw structured text deltas and any unknown event kinds.
- The generator has no research-start, filesystem-write, shell, or network tool.
- Hidden chain-of-thought and provider-private reasoning are never requested,
  stored, or forwarded.
- The Agent may store its fully validated envelope in the existing artifact
  store. Raw streams, prompts, private reasoning, and debug traces are not added
  to the transcript or a new artifact.

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
- **Failure atomicity:** durable summaries follow successful draft saves; live
  progress is explicitly ephemeral; notification failure never rewrites domain
  state.
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
- raw JSON text deltas are not published;
- the returned question/final step is schema-validated;
- the pure bounded prompt contains only the enumerated canonical draft evidence;
- public output is redacted and length-bounded;
- provider failure saves a retryable failed draft before one generic durable
  notice and does not catch cancellation;
- configured runtimes choose the LLM generator while provider-less runtimes keep
  deterministic compatibility without constructing duplicate providers;
- task-understanding events persist and replay with the atomic generic
  session/scope/scope-id metadata, while partial metadata is rejected.

Frontend tests prove:

- scoped model and tool messages appear in the task-understanding activity region;
- optimistic empty-id previews accept the first matching-session scoped output,
  then canonical previews require a matching scope id;
- mismatched-session and partially scoped output are ignored;
- matching output remains batched and ordered;
- valid scoped history without an active preview remains visible as ordinary
  Athena output;
- legacy unscoped output behavior is unchanged.

Run focused Python and Vitest tests first, then the complete relevant Python
clarification/gateway suite, full frontend suite, production build, Rust tests,
`cargo check`, and diff checks.

## Acceptance criteria

1. The GUI task-understanding path invokes a real configured LLM before producing
   each clarification question or final synthesis.
2. During every successful loop, model-authored public progress appears in the
   task-understanding region; when the model calls the reporting tool, that real
   tool activity appears immediately and remains explicitly ephemeral.
3. At least one validated, model-authored public update is emitted per successful
   `next_step` invocation even when the provider skips the optional progress
   tool; the durable update is emitted only after the corresponding draft save.
4. Raw structured JSON, prompts, provider-private reasoning, exception details,
   raw tool arguments, known credential forms, and hidden chain-of-thought never
   appear in output events.
5. Every new task-understanding output carries atomic session, scope, and generic
   scope-id metadata; stale-session or partially scoped output cannot enter the
   active frontend conversation.
6. Questions and final drafts still obey the existing strict schemas, eight-
   question cap, persistence rules, revision handling, and confirmation gate.
7. A configured provider failure persists a retryable `FAILED` draft and never
   starts PREPARE or silently falls back to deterministic output.
8. Provider-less compatibility callers retain deterministic clarification.
9. Live output preserves order; identical progress-tool reports within one
   generator invocation appear once; replay contains each durable publication
   once. A retry is a new invocation and may truthfully add a new update.
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
