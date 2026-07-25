# Athena-Rust WIP Inventory

## State at baseline (commit 9555ae7)

### Existing flat crates

| Crate | Status | Files | Tests | Action |
|---|---|---|---|---|
| `core/` | Implemented | `src/lib.rs` (149 lines) | 3 tests (Hypothesis, Thread, unknown field) | Rename to `athena-types`, move to `crates/` |
| `protocol/` | Implemented | `src/error.rs`, `src/lib.rs` | 6+ tests (was 10 in spec) | Rename to `athena-protocol`, move to `crates/` |
| `tool/` | Implemented | `src/lib.rs` (145 lines) | 0 tests (spec called for 10) | Rename to `athena-tools`, move to `crates/`, add ToolExecutor |
| `transport/` | Implemented | `src/lib.rs` | 3 tests | Absorb into `athena-server` as `transport.rs` |
| `events/` | Implemented | `src/lib.rs` | 5 tests | Absorb into `athena-runtime` as `journal.rs` |
| `memory/` | Implemented | `src/{lib,context,compaction,rollout}.rs` (868 lines) | 0 tests (spec called for tests) | Rename to `athena-memory`, move to `crates/`, convert to MessagePart model |
| `agent/` | Partial | `Cargo.toml` modified, `src/lib.rs` stub | 0 tests | Rename to `athena-agent`, move to `crates/`, full implementation per new spec |
| `runtime/` | Empty stub | `Cargo.toml`, `src/lib.rs` (0 lines) | 0 tests | Rename to `athena-runtime`, move to `crates/` |
| `server/` | Empty stub | `Cargo.toml`, `src/lib.rs` (0 lines) | 0 tests | Rename to `athena-server`, move to `crates/` |

### Missing crates (new plan)

| Crate | Status | Action |
|---|---|---|
| `athena-workspace` | Not started | Create new under `crates/` |
| `athena-research` | Not started | Create new under `crates/` |

### Architectural gaps vs new spec

1. **Package naming**: All crates named `core`, `protocol`, etc. — MUST be renamed to `athena-*`
2. **Directory**: Crates at `athena-rust/` root — MUST move to `athena-rust/crates/`
3. **Workspace config**: Missing `[workspace.package]`, `[workspace.dependencies]`, `[workspace.lints]`, `unsafe_code = "forbid"`
4. **Message model**: Simplified `role + content` — MUST become `MessagePart` enum (SystemPrompt, UserPrompt, Text, ToolCall, ToolReturn)
5. **EventJournal**: Journal assigns sequence internally; needs history + `watch::Sender<u64>` for tail
6. **Tool design**: Tool + ToolExecutor MUST be separated; Registry MUST be Builder pattern (read-only after build)
7. **Transport**: Event lane MUST use reliable send (not `try_send` fire-and-forget)
8. **Agent**: Provider MUST return event stream; AgentRunner MUST implement TurnRunner
9. **No fixture tests**: No Python fixture export/import testing exists
10. **No `unsafe_code = "forbid"`**: Missing from workspace config

### Reusable code

- Core types (AthenaThread, AthenaTurn, Hypothesis, etc.) — need newtype wrappers
- Protocol DTOs (RequestEnvelope, ResponseEnvelope, RpcError, etc.) — field names and error codes match Python
- Transport channel model — basic structure correct, needs reliable event lane
- EventJournal — core logic correct, needs internal sequence assignment + watch tail
- ContextManager — token estimation and snapshot/rollback logic correct, needs MessagePart model
- RolloutRecorder — JSONL format correct, needs structured message recording (no double-encoding)
- ToolRegistry — sorted specs correct, needs Builder pattern
