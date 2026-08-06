# Agent Stream Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every provider asynchronous generator deterministically when `_sampling_loop` stops consuming it.

**Architecture:** `_sampling_loop` remains the owner of the provider stream and wraps it with `contextlib.aclosing`. A retained-generator unit test observes the generator's `finally` block and frame state without relying on garbage collection.

**Tech Stack:** Python 3.11+, `contextlib.aclosing`, pytest, pytest-asyncio

## Global Constraints

- Modify only the local `main` checkout.
- Do not switch branches, update remote branches, or push commits.
- Preserve provider interfaces, event handling, tool cleanup, memory writes, and step outcomes.
- Preserve unrelated working-tree changes.

---

### Task 1: Deterministic Provider Stream Closure

**Files:**
- Modify: `test/unit/test_agent.py`
- Modify: `src/athena/core/agent/runtime.py`

**Interfaces:**
- Consumes: `Agent.run(ctx: AgentContext) -> AgentOutcome` and provider `stream(config, tools, messages, cancel)` asynchronous iterators.
- Produces: `_sampling_loop(agent: Agent, ctx: AgentContext) -> StepOutcome` with deterministic closure of its provider stream.

- [x] **Step 1: Write the failing regression test**

Add this method to `TestBaseAgent` in `test/unit/test_agent.py`:

```python
async def test_response_completed_closes_provider_stream_immediately(self):
    class ClosingProbeProvider:
        def __init__(self):
            self.closed = False
            self.generator = None

        def stream(self, *_args):
            async def events():
                try:
                    yield StreamEvent(
                        "text_delta", {"delta": "done", "accumulated": "done"}
                    )
                    yield StreamEvent("response_completed")
                finally:
                    self.closed = True

            self.generator = events()
            return self.generator

    tools = ToolRegistry()
    provider = ClosingProbeProvider()
    agent = Agent(ResponsesProvider("model"), tools, "system")
    agent.model = provider
    ctx = AgentContext(
        AthenaThread(
            thread_id="t1",
            session_id="s1",
            status="running",
            context_ref="ctx://0",
        ),
        AthenaTurn(
            turn_id="t1.1",
            thread_id="t1",
            request_ref="request",
            status="running",
        ),
        lambda *_args: asyncio.sleep(0),
        tools,
        asyncio.Event(),
    )

    await agent.run(ctx)

    assert provider.closed
    assert provider.generator is not None
    assert provider.generator.ag_frame is None
```

- [x] **Step 2: Run the test to verify RED**

Run:

```powershell
uv run pytest test/unit/test_agent.py::TestBaseAgent::test_response_completed_closes_provider_stream_immediately -q
```

Expected: FAIL at `assert provider.closed` because the retained asynchronous generator remains suspended at `response_completed`.

- [x] **Step 3: Implement deterministic stream ownership**

Import `aclosing` in `src/athena/core/agent/runtime.py`:

```python
from contextlib import aclosing
```

Wrap the existing event loop without changing its `match` branches:

```python
try:
    async with aclosing(
        agent.model.stream(agent.config, agent.tools, mem.items, ctx.cancel)
    ) as stream:
        async for event in stream:
            match event.kind:
```

Indent the existing `match event.kind` branches under this `async for`; keep
every branch body unchanged.

- [x] **Step 4: Run the focused test to verify GREEN**

Run:

```powershell
uv run pytest test/unit/test_agent.py::TestBaseAgent::test_response_completed_closes_provider_stream_immediately -q
```

Expected: PASS, proving the provider's `finally` block ran and `ag_frame` was cleared before `Agent.run` returned.

- [x] **Step 5: Run the Agent regression suite**

Run:

```powershell
uv run pytest test/unit/test_agent.py -q
```

Expected: all tests PASS with no pending-task or asynchronous-generator warnings.

- [x] **Step 6: Check formatting and the final diff**

Run:

```powershell
uv run black --check src/athena/core/agent/runtime.py test/unit/test_agent.py
git diff --check -- src/athena/core/agent/runtime.py test/unit/test_agent.py
git diff -- src/athena/core/agent/runtime.py test/unit/test_agent.py
```

Expected: Black and `git diff --check` pass; the diff contains only the regression test, the `aclosing` import, and the stream context manager.

- [x] **Step 7: Commit the implementation locally**

```powershell
git add -- src/athena/core/agent/runtime.py test/unit/test_agent.py docs/superpowers/plans/2026-08-04-agent-stream-lifecycle.md
git commit -m "fix(agent): close provider streams deterministically"
```

Do not push the commit.
