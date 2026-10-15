# Rust independent review and repair (2026-10-15)

## Scope and review basis

I read `codex_docs/CURRENT.md`, the active engineering optimization plan, `docs/代码规范.md`, the Code Simplifier skill, and the Rust sections of `docs/codex_docs/2026-09-05-module-simplification-plan.md`. That older ledger marks the Rust crate modules Reviewed. This pass rechecked their current workspace exports/dependency direction and the changed caller surfaces rather than reopening unrelated Rust modules. Python remains the production entrypoint; these crates are migration code validated against the Python protocol and fixtures.

Current caller paths inspected for this delta:

- `athena-agent::Agent::run` -> `run_loop` -> `sampling_step` -> `run_tools` -> `invoke_one` -> `ToolExecutor` -> event bridge. `ToolCall.call_id` already carries the provider's canonical ID through assistant/tool history and now also reaches `ToolContext` and lifecycle event refs.
- `athena-runtime::FairMux` is constructed by `athena-server::AppServer::create` and the direct server E2E setup. Subscription registry owns per-subscription event producers; mux forwards to the server event lane.
- `athena-server::MessageProcessor` is constructed by `AppServer::create`; `AppServer::shutdown` first requests protocol shutdown, then invokes processor drain, removes subscriptions, aborts the mux, and closes the thread manager.

## Per-crate retain/change decisions

| Crate and reviewed modules | Decision and current evidence |
|---|---|
| `athena-types`: `ids.rs`, `status.rs`, `lib.rs`; fixture tests | Retain the validated identifier/status boundary and Python parity tests. No changed caller or redundant layer found. |
| `athena-protocol`: `envelope.rs`, `error.rs`, `operations.rs`, `lib.rs`; Python fixture tests | Retain the canonical v1 DTO/error/method boundary. Existing error code `Overloaded` supports capacity rejection without adding a protocol value. |
| `athena-tools`: `tool.rs`, `spec.rs`, `registry.rs`, `executor.rs`, `lib.rs`; registry/lifecycle tests | Retain tool definition, immutable registry, and lifecycle execution boundaries. The agent already uses provider call IDs in tool history; lifecycle identity now matches. |
| `athena-memory`: `message.rs`, `lib.rs`; context parity tests | Retain the one-field context buffer and fixture-governed message conversion; no change needed. |
| `athena-runtime`: `event.rs`, `runner.rs`, `submission.rs`, `thread_actor.rs`, `thread_handle.rs`, `thread_manager.rs`, `lib.rs`; runtime/race tests | Retain per-thread actor ownership, terminal-event fencing, and journal sequencing. Change `FairMux` idle polling only; round-robin delivery remains. |
| `athena-agent`: `agent.rs`, `input.rs`, `provider.rs`, `openai_provider.rs`, `subagent.rs`, `lib.rs`; shared fixtures and provider/mapping/subagent/tool-loop tests | Retain separate input/provider/OpenAI/subagent boundaries. Change `invoke_one` identity to use the canonical provider `call_id`; add repeated-same-name tool regression. |
| `athena-server`: `client.rs`, `execution.rs`, `lifecycle.rs`, `processor.rs`, `subscription.rs`, `transport.rs`, `lib.rs`; in-process E2E tests | Retain client, adapter, lifecycle, processor, subscription, and transport separation. Change processor admission and executor task ownership; add capacity/shutdown regression. |

Reviewed Rust test sources include agent `common`, `openai_provider`, `provider_mapping`, `subagent`, and `tool_loop`; memory `context_parity`; protocol and type `python_fixtures`; runtime `common`, `thread_races`, and `thread_runtime`; server `app_server_e2e`; tools `registry` and `tool_lifecycle`. The old ledger's documented deleted prototype crates remain deleted and are not recreated.

## Defects and repairs

1. **Tool event identity collision.** `invoke_one` built `ToolContext.call_id` from `{turn_id}:{tool_name}`. Parallel provider calls to the same tool therefore emitted duplicate lifecycle event refs even though their history entries retained distinct provider IDs. A new test first failed with four `event:turn-1:echo` refs; it now passes after forwarding `tc.call_id` unchanged.
2. **FairMux idle spin.** An empty registered subscription used `yield_now`, repeatedly polling a worker; no-subscription sleep was 50 ms. Both idle cases now use a 1 ms bounded wait, keeping admission/delivery structure simple while capping polling. Tests cover prompt delivery after adding a subscription, round-robin fairness under two backlogs, and termination when the outgoing receiver closes.
3. **Processor saturation and orphan executors.** Dispatch awaited a semaphore permit before reading the next transport message, so 64 blocked executors also blocked `server/shutdown`. Handlers used detached `tokio::spawn`, so timing out and aborting only the dispatcher left executor futures alive. Dispatch now owns handlers in a `JoinSet`, continues reading at capacity and returns existing `OVERLOADED` for excess work, and drains normally. Timed shutdown signals the dispatcher to abort and join all owned handlers; a bounded grace fallback aborts a dispatcher blocked in a transport send. Dropping the processor also signals cancellation. A deterministic test occupies all 64 slots, sends an excess request followed by shutdown, checks overload/shutdown responses, then verifies all 64 executor futures are dropped after timeout.
4. **All-target Clippy failures.** Existing local-socket integration fixtures used panic-on-setup `unwrap`/`expect`, which failed the workspace's `-D warnings` all-target gate. Added narrow test-target-only allowances in `athena-agent/tests/openai_provider.rs`; the processor regression uses a test-module-only `expect_used` allowance. Production lint settings remain unchanged.

## Files changed

- `athena-rust/crates/athena-agent/src/agent.rs`
- `athena-rust/crates/athena-agent/tests/tool_loop.rs`
- `athena-rust/crates/athena-agent/tests/openai_provider.rs`
- `athena-rust/crates/athena-runtime/src/event.rs`
- `athena-rust/crates/athena-server/src/processor.rs`

No Cargo manifest, protocol DTO, fixture, or non-Rust source changed.

## Verification

On the final formatted tree:

- `cargo fmt --all --check` — passed.
- `cargo clippy --workspace --all-targets --offline -- -D warnings` — passed.
- `cargo test --workspace --offline` — passed, 90 tests, zero failures; doc-test targets also passed.
- The call-ID regression was observed failing before its implementation and passing afterward. Focused FairMux tests and the saturated processor test passed independently before the full run.

## Boundaries

The OpenAI provider tests use local fake HTTP servers; no real provider/network validation was performed. Rust remains migration code and no Python production runtime integration was exercised. FairMux uses bounded 1 ms polling rather than a new wake-notification channel, avoiding extra synchronization state at the cost of up to roughly 1 ms idle delivery latency.

## Shutdown ownership follow-up

A second review pass added two deterministic backpressure regressions to the
processor tests. They fill all 64 control-response slots with pre-initialization
rejections, start a pending executor handler, and then block the protocol
`server/shutdown` response on the full lane.

- `dropping_processor_aborts_dispatch_blocked_on_response_send` failed before
  the fix: `Drop` only notified the watch channel, but dispatch was awaiting
  `send_response` directly and could not poll that watch. The dispatch handle
  was detached and its `JoinSet` retained pending handlers. `Drop` now signals
  stop and aborts its owned dispatcher handle; dropping the dispatch future
  aborts the `JoinSet` children. The test observes the executor guard dropped
  within 200 ms.
- `repeated_shutdown_waits_for_backpressured_handler_cleanup` exercises a
  concurrent second shutdown while the first is in its bounded dispatch join.
  Previously `TERMINATED` was stored before the join, so a repeated call could
  return while the handler was still live. Shutdown now retains the dispatch
  mutex while joining/aborting the stored handle and marks `TERMINATED` only
  after that work completes. Repeated callers either observe completed state or
  wait for the serialized owner to release the dispatch handle.
- The dispatcher join remains bounded by `DISPATCH_STOP_GRACE`; on expiration
  shutdown aborts and awaits the dispatch task. A dispatcher blocked while
  sending protocol-shutdown, not-ready, or admission-error responses now races
  those sends against the stop watch, so an explicit stop reaches the normal
  `JoinSet::abort_all` and join path even when the response lane is full. The
  repeated-shutdown backpressure test covers this path.
- The grace fallback only guarantees that the dispatcher task is aborted and
  joined. If the dispatcher itself cannot observe stop before the grace expires,
  dropping its `JoinSet` requests cancellation of child handlers but cannot
  asynchronously join them. `MessageProcessor::Drop` likewise cannot await;
  it requests stop and aborts the dispatcher handle. The backpressure tests
  verify the current local transport exits these paths promptly, but that is
  not a general join guarantee for arbitrary executor futures under fallback
  abort or synchronous `Drop`.

Verification after this follow-up:

- `cargo test --workspace --offline` — passed; server unit and end-to-end tests
  plus all workspace doc-test targets passed.
- `cargo clippy --workspace --all-targets --offline -- -D warnings` — passed.
- `cargo fmt --all --check` — passed.
