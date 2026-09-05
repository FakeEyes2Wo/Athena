# athena-rust

A Rust migration of the Athena runtime, built **contract-first** for behavioural
parity with the Python source in `../src/athena`. Python remains the production
entry point; these crates are validated against Python fixtures and behaviour and
are not yet wired into the production call path (see the migration plan).

## Workspace layout

Dependency direction (no cycles): `athena-types` is the root; `athena-server`
depends on protocol + runtime; `athena-agent` implements the runtime
`TurnRunner`.

| Crate | Responsibility | Source (`src/athena`) | Status |
|---|---|---|---|
| `athena-types` | Validated identifiers, thread/turn DTOs, statuses | `core/schemas.py` | ✅ done |
| `athena-protocol` | v1 wire protocol, error codes, method names | `app_server/protocol.py` | ✅ done |
| `athena-memory` | `ContextManager`, compaction, rollout, `MessagePart` | `memory/*` | ✅ done |
| `athena-tools` | `Tool`/`ToolExecutor`, read-only registry | `core/tool*.py` | ✅ done |
| `athena-runtime` | Event journal, per-thread actor, `TurnRunner`, `ThreadManager` | `app_server/{events,submissions,thread_runtime,thread_manager}.py` | ✅ done |
| `athena-agent` | Provider stream, tool-call loop, sub-agents, `AgentRunner` | `core/agent/*` | ✅ done |
| `athena-server` | Transport, message processor, execution, subscriptions, lifecycle | `app_server/{transport,server,execution,client,lifecycle}.py` | ✅ done |

## Build & test

```bash
cargo check --workspace
cargo test  --workspace

# quality gates (must all pass)
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
```

## Engineering conventions

- Package names are `athena-*`; imports are `athena_*`.
- `unsafe_code = "forbid"`; production code has no `unwrap()`/`expect()` (both are
  `deny`-linted, allowed only inside `#[cfg(test)]`).
- Only protocol/persistence DTOs derive `Serialize`/`Deserialize`.
- Thread state is owned by one actor per thread (no shared `Mutex<ThreadRuntime>`);
  `Request`/`Response`/`ServerRequest`/`Event` are never silently dropped — only
  notifications are best-effort.
- Every crate ships behaviour tests; an empty crate is never "done".

## Notable design points

- **Event journal** assigns each sequence number internally and exposes a `watch`
  tail, so no waiter misses an append even when notifications coalesce.
- **Terminal-event ownership**: only the thread actor commits `turn_completed` /
  `turn_failed` / `turn_interrupted`. A completing turn racing an interrupt yields
  exactly one terminal — verified by a 1000-iteration race test.
- **Tool concurrency**: concurrency-safe tools run as one parallel batch; each
  non-safe tool is a strict serial barrier.
- **`OpenAiProvider`** performs real Chat Completions SSE and is therefore not
  exercised by tests; all agent/runtime/server tests use fakes and never touch the
  network.

## Provider configuration

`OpenAiProvider::new(base_url, api_key)` is constructed explicitly (no implicit
env read). A caller typically sources `base_url`/`api_key` from the same
environment variables the Python side uses (e.g. `OPENAI_API_KEY`).

## Migration & rollback

Python stays the sole production entry point throughout the migration. The Rust
crates only read compatible fixtures and never mutate Python data. A later,
separate integration plan will add an explicit `python`/`rust` engine selection
with a verified rollback path before any default is switched. See
`../docs/superpowers/plans/2026-07-25-athena-rust.md` and
`.superpowers/sdd/progress.md`.
