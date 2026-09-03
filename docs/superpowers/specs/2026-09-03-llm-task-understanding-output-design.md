# LLM Task-Understanding Output Design

Date: 2026-09-03
Status: approved for implementation planning

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

### Structured model result

Introduce a strict `ClarificationModelOutput` envelope because the existing
discriminated `ClarificationStep` union is not itself a `BaseModel` and cannot be
passed directly as `Agent.output_type`.

The envelope contains:

- `public_summary`: a short, user-facing account of what was established and
  what decision comes next;
- `step`: the existing validated `ClarificationQuestionStep` or
  `ClarificationFinalStep`.

The summary has a bounded length and is explicitly public. It must describe
conclusions and uncertainty, not private reasoning steps.

### LLM clarification generator

Add `LlmClarificationGenerator`, implementing the existing asynchronous
`ClarificationGenerator` protocol. Each `next_step(draft)` call:

1. Builds a prompt from the original task, current structured understanding,
   persisted answers, revisions, unresolved items, and question count.
2. Creates a one-turn Athena `Agent` with the configured provider, the local
   artifact store, strict output envelope, and the progress tool.
3. Runs the real Agent loop with a fresh thread/turn identity.
4. Suppresses raw `agent/text_delta` events because they contain the structured
   JSON transport rather than user-facing prose.
5. Publishes progress-tool calls immediately, tracks their normalized summaries,
   and publishes the validated final `public_summary` only when it is not a
   duplicate. A turn with no tool call still publishes that final summary.
6. Returns only the validated `step` to `ClarificationController`.

Every call is stateless beyond the persisted draft. This avoids a second private
memory source and makes restart behavior depend only on canonical clarification
state.

### Public progress tool

The Agent receives one tool, `report_task_understanding`, with a strict input
schema containing a bounded `summary` and a small stage enum such as
`analysis`, `question`, or `synthesis`.

Calling the tool is a real Agent-loop tool action. Its implementation has no
filesystem, network, shell, or lifecycle authority. It only publishes its
explicitly public summary and returns an acknowledgement to the model. Raw tool
arguments are not separately echoed, preventing duplicate messages.

The prompt asks the model to report meaningful progress before its final
structured answer. If a provider omits the optional tool call, the required
envelope summary still guarantees at least one genuine model-authored update for
the turn.

### Runtime event contract

Extend output events with optional, backward-compatible metadata:

```json
{
  "type": "output",
  "session_id": "session-id",
  "scope": "task_understanding",
  "draft_id": "clarify-id",
  "source": "agent",
  "channel": "text",
  "text": "The target is known; I am confirming the primary metric.",
  "plan": "task-understanding",
  "tool": null
}
```

`RuntimeEvents.publish_output` and `EventProjector.output` accept the optional
metadata. Existing callers and old transcript records remain valid. Progress
tool messages use `source="tool"` and
`tool="report_task_understanding"`; validated turn summaries use
`source="agent"`.

Events remain ordered and persisted through the existing transcript. The normal
event projector continues to redact secrets before display or storage.

### Composition root

When `ResearchConfig.model` is configured, `build_services` constructs a provider
and injects `LlmClarificationGenerator` into the existing controller. The GUI
always supplies its configured model, so its default path becomes LLM-driven.

When no model is configured, provider-less tests and compatibility callers retain
`DeterministicClarificationGenerator`. A configured model that later fails
authentication or provider execution must produce the controller's existing
retryable `FAILED` draft; it must not silently claim a deterministic fallback was
an LLM result.

## Frontend behavior

The frontend continues to consume ordinary `output` events. It adds the optional
session/scope/draft metadata to its message projection and applies these rules:

- discard a scoped output whose `session_id` is not the active session;
- render `scope="task_understanding"` messages in the task-understanding activity
  region associated with the active clarification preview;
- show progress-tool output using the existing tool presentation;
- preserve the legacy positional grouping fallback for older transcript records;
- never interpret provider `reasoning_content` or raw JSON as a display message.

This also closes the known stale-runtime race for newly scoped clarification
events when users switch sessions or workspaces.

## Data flow

```text
GUI submits task
  -> task_clarification_start
  -> ClarificationController.run
  -> LlmClarificationGenerator.next_step(canonical draft)
  -> Athena Agent/provider loop
       -> report_task_understanding(summary)
          -> scoped, redacted output event -> GUI activity region
       -> validated ClarificationModelOutput
          -> scoped public summary -> GUI activity region
          -> question/final step -> ClarificationController
  -> controller persists pending answer or READY_FOR_CONFIRMATION
```

PREPARE remains unreachable until the user confirms the final persisted draft.

## Error and safety behavior

- Provider, schema, or artifact failures propagate to the controller, which
  persists the existing retryable `FAILED` state.
- Before propagating a model failure, the generator may publish only a generic
  failure notice; exception text, tracebacks, prompts, and credentials are not
  displayed.
- Subscriber/display publication failures are logged and do not change the
  canonical clarification result.
- Public summaries are length-bounded, normalized, and passed through existing
  secret redaction.
- The adapter drops all raw structured text deltas and any unknown event kinds.
- The generator has no research-start, filesystem-write, shell, or network tool.
- Hidden chain-of-thought and provider-private reasoning are never requested,
  stored, or forwarded.

## Testing strategy

Use test-driven development with a fake streaming provider so the real Athena
`Agent` loop executes without network access.

Backend tests prove:

- a progress tool call is published before the validated model summary;
- raw JSON text deltas are not published;
- the returned question/final step is schema-validated;
- the prompt contains canonical draft evidence and not private runtime state;
- public output is redacted and length-bounded;
- provider failure yields a retryable failed draft without leaking error text;
- configured runtimes choose the LLM generator while provider-less runtimes keep
  deterministic compatibility;
- task-understanding events persist and replay with session/scope/draft metadata.

Frontend tests prove:

- scoped model and tool messages appear in the task-understanding activity region;
- mismatched-session scoped output is ignored;
- matching output remains batched and ordered;
- legacy unscoped output behavior is unchanged.

Run focused Python and Vitest tests first, then the complete relevant Python
clarification/gateway suite, full frontend suite, production build, Rust tests,
`cargo check`, and diff checks.

## Acceptance criteria

1. The GUI task-understanding path invokes a real configured LLM before producing
   each clarification question or final synthesis.
2. During every successful loop, model-authored public progress appears in the
   task-understanding region; when the model calls the reporting tool, that real
   tool activity appears immediately.
3. At least one validated, model-authored public update is emitted per successful
   model turn even when the provider skips the optional progress tool.
4. Raw structured JSON, prompts, provider-private reasoning, exception details,
   credentials, and hidden chain-of-thought never appear in output events.
5. Every new task-understanding output carries session, draft, and scope metadata;
   stale-session output cannot enter the active frontend conversation.
6. Questions and final drafts still obey the existing strict schemas, eight-
   question cap, persistence rules, revision handling, and confirmation gate.
7. A configured provider failure persists a retryable `FAILED` draft and never
   starts PREPARE or silently falls back to deterministic output.
8. Provider-less compatibility callers retain deterministic clarification.
9. Live events and transcript replay preserve ordering and do not duplicate a
   progress update.
10. Focused and regression test suites, frontend build, and Rust checks pass with
    no task-owned warnings or errors.

## Out of scope

- Exposing hidden model reasoning or token-level chain-of-thought.
- Giving the clarification Agent general web, shell, file-write, or research tools.
- Replacing the confirmation gate or changing PREPARE/SEARCH/VALIDATE behavior.
- Migrating old unscoped transcript rows.
- Changing the deterministic fallback used when no model is configured.
