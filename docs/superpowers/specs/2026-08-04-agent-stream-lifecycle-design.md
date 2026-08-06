# Agent Stream Lifecycle Design

## Problem

`_sampling_loop` stops consuming a provider stream when it receives a
`response_completed` event. Breaking out of `async for` leaves an asynchronous
generator suspended at its final `yield`, so cleanup is deferred to the
async-generator finalizer and garbage collection.

The current canonical implementation is
`src/athena/core/agent/runtime.py`; it retains the behavior originally
introduced in `src/athena/core/agent/agent.py`.

## Design

Wrap the object returned by `agent.model.stream(...)` in
`contextlib.aclosing` and iterate within that asynchronous context manager.
This makes `_sampling_loop` own the stream lifetime and guarantees `aclose()`
when the loop exits through completion, an error event, an exception, or task
cancellation.

Keep event handling, tool-task cleanup, memory writes, and step outcomes
unchanged. Do not change `ResponsesProvider` or the provider interface.

The OpenAI SDK's `AsyncStream` closes its HTTP response in a `finally` block
when exhausted. `ResponsesProvider` emits `response_completed` only after that
SDK stream is exhausted, so closing the outer provider generator is compatible
with the normal HTTP cleanup path. `GeneratorExit` is not caught by the
provider's `except Exception` block.

## Test Strategy

Add a unit provider that returns and retains an asynchronous generator. Its
generator sets a flag in `finally`, emits text, and then emits
`response_completed`. After `Agent.run` returns, assert both that the `finally`
block ran and that the retained generator's `ag_frame` is `None`.

Run this test before the implementation to confirm it fails under the current
behavior. After the implementation, run the focused test and the complete
`test/unit/test_agent.py` suite locally.

## Scope

Modify only the local `main` checkout. Do not switch branches, update the
remote Idea Generation branch, push commits, or alter unrelated working-tree
changes.
