# Single-Turn Chat Utility Design

**Date:** 2026-07-31
**Status:** Approved

## Goal

Add a reusable asynchronous utility that provides `/btw`-style model interaction. It
reuses Athena's existing Agent loop, including tool calls, cancellation, and event
emission, without requiring an app-server Thread or rollout persistence.

## Public Interface

The utility lives in `athena.utils.single_turn_chat` and exposes
`single_turn_chat()` through `athena.utils`.

```python
async def single_turn_chat(
    prompt: str,
    *,
    model: str,
    memory: ContextManager | None = None,
    tools: ToolRegistry | None = None,
    system_prompt: str | None = None,
    client: AsyncOpenAI | None = None,
    max_turns: int = 20,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    emit: EmitEvent | None = None,
    cancel: asyncio.Event | None = None,
) -> str:
    ...
```

`prompt` and `model` must be non-empty. `tools` defaults to an empty registry. The
default emitter is an asynchronous no-op, and the default cancellation event is unset.

## State Ownership

When `memory` is omitted, the utility creates an ephemeral `ContextManager`. All
messages and tool results are discarded after the returned text is extracted. This is
the default stateless `/btw` behavior.

When the caller supplies a `ContextManager`, the utility uses that object directly.
The current user message, model responses, tool calls, and tool results remain in it for
later calls. The caller owns the memory lifetime and decides when to reuse or discard it.
The utility does not keep a global session registry. A caller must not use the same
mutable memory concurrently across multiple calls.

`system_prompt=None` and `system_prompt=""` both mean that no system message is
injected. A non-empty system prompt uses the Agent loop's existing once-per-memory
injection behavior. The existing Agent injection guard is tightened to skip empty
prompts, so the provider receives no empty system message in this mode.

## Execution Flow

1. Validate the prompt, model name, and numeric sampling limits.
2. Create an empty tool registry, no-op emitter, cancellation event, and memory only
   when their caller-provided equivalents are absent.
3. Build an Agent with the existing `create_agent()` factory.
4. Create temporary `AthenaThread` and `AthenaTurn` values and an `AgentContext` whose
   explicit input override contains the literal prompt.
5. Await `Agent.run()` so provider streaming and tool-call ordering remain owned by the
   existing Agent kernel.
6. Read the final assistant text from the messages added during this call and return it.

The temporary thread and turn values are identity carriers only. No `ThreadRuntime`,
submission queue, event journal, artifact write, or rollout recorder is created.
The explicit input override prevents an `artifact://`-shaped prompt from being resolved
as a local file; normal app-server contexts retain their existing artifact behavior.

## Result And Errors

The function returns the final assistant text from the current call. It does not return
an artifact reference or expose the synthetic thread and turn identifiers.

- Blank prompt or model values raise `ValueError`.
- Non-positive `max_turns` or `max_tokens`, or a temperature outside the inclusive
  `0..2` range, raises `ValueError` before model execution.
- Provider and tool-loop failures propagate using the existing Agent behavior.
- Task cancellation propagates as `asyncio.CancelledError`.
- A completed run with no final assistant text raises `RuntimeError`.

The utility records the memory length before execution and searches only messages added
by the current call. This prevents an older assistant response in caller-owned history
from being returned when the current run produces no answer.

## Tests

Focused unit tests use the real Agent loop with a deterministic scripted provider. They
cover:

- stateless plain-text completion;
- caller-owned memory reuse across calls;
- omitted and empty system prompts;
- the shared Agent loop's empty-system-prompt behavior;
- tool execution followed by a final response;
- event forwarding;
- blank input and invalid configuration;
- no-final-text handling;
- cancellation propagation.
- literal handling for prompts that begin with `artifact://`.

The app-server test suite remains unchanged because the utility deliberately does not
enter the Thread/Turn runtime.
