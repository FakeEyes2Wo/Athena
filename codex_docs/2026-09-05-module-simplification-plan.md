# Module-by-module simplification review

Baseline: `4b9dbaa`. Scope: tracked backend, TUI, gateway, GUI source, Rust bridge, Rust/TypeScript implementations, and development scripts. Tests and assets embedded in these roots remain listed so coverage cannot silently shrink.

Coverage correction: the original 391-file inventory omitted 65 Rust, 138 TypeScript/JavaScript implementation and colocated test files, and 8 root development scripts. These completed or independently buildable modules belong to the user's repository-wide goal even when Python is the production entrypoint. All 211 were unchanged from the baseline when enumerated and are added as Pending, bringing the baseline ledger to 602 files. Enumeration is not review; earlier 17/391 reports covered only the original subset. New files introduced by this refactor are tracked separately and must also be verified.

Inventory is not a completed semantic review. Each pending file requires content inspection, caller tracing, a retain/merge/delete decision, and relevant verification before closing this plan. Reconcile new or removed files before final acceptance.

Scope update (2026-09-05): at the user's direction, merge the reviewed TypeScript checkpoint now and do not spend further simplification time in `athena_ts`. Remaining unreviewed TypeScript ledger entries are deferred, not counted as reviewed. Continue the repository-wide review on Python, GUI, gateway, Rust, and development scripts directly on `main`.

## Tasks

- [x] Enumerate tracked application source files and record baseline line counts.
- [x] Read all four serving source files and trace their immediate callers.
- [x] Resolve serving compatibility entrypoint duplication and verify callers (22 tests before and after; module CLI help exits 0).
- [ ] Review every remaining non-TypeScript file and module; record decisions and evidence.
- [ ] Implement the identified simplifications with scoped regression checks.
- [x] Resolve TypeScript recovery fixture/state-copy failures and reconcile its workspace lockfile.
- [x] Simplify TypeScript sampling task storage/dispatch and verify concurrency/result ordering.
- [x] Consolidate TypeScript Agent factory and single-turn sampling configuration.
- [x] Remove the unused TypeScript dual-signature Agent runner adapter.
- [x] Remove unused TypeScript Agent state and replace data-only constructors with types.
- [x] Remove unused Worker text forwarding and consolidate provider ownership.
- [x] Trace the standalone TypeScript composition root to actual entries and delete its unused adapter chain.
- [x] Remove redundant execution context/root state and migrate PlanRunner/prepare/DSH callers.
- [x] Remove the test-only prepare runner factory and validate the real preparation pipeline.
- [x] Consolidate PlanRunner failure handling and shared directory-presence checks.
- [x] Remove the unconsumed settlement API and test the active Supervisor policy.
- [x] Remove script metadata/result wrappers and consolidate the scoring interface.
- [x] Reduce script run arguments and consolidate frozen-file restoration/snapshot ownership.
- [x] Merge stateless validation result construction into its active orchestration and review reporting.
- [x] Remove the unused candidate score wrapper and unpopulated validation metadata.
- [x] Replace the ResearchState class with schema-derived plain state and explicit persistence.
- [x] Consolidate Plan schemas into shared contracts and reduce unused input/decision fields.
- [x] Remove the unused event projection module and retain redaction at the live failure boundary.
- [x] Remove Supervisor's duplicate workspace map and unused path accessor; resolve workspaces from the durable research tree.
- [x] Remove recovery's placeholder callback interface and check retained Plan resources at the Supervisor boundary.
- [x] Consolidate SEARCH wake/start ownership and replace the waiter collection with one loop-owned callback.
- [x] Make SEARCH own graceful stop draining; join the loop before publishing STOPPED.
- [x] Share failure persistence/notification between initial startup and background SEARCH.
- [x] Drain in-flight turns after SEARCH errors and narrow internal completion records.
- [x] Narrow the Supervisor public surface and remove unused Plan/ideation return values.
- [x] Generalize the single task owner across recovery, PREPARE, SEARCH and VALIDATE; join phases during stop/validation handoff.
- [x] Complete the Supervisor source/test review, consolidate authoritative fields, and close phase-command races.
- [x] Review the GUI gateway boundary modules and remove duplicated state/factory layers.
- [x] Review the research turn dispatcher/shared support modules and narrow their internal interfaces.
- [x] Review the web retrieval module and make its shared session the sole HTTP owner.
- [x] Review the Rust agent module and reuse the runtime turn contract directly.
- [x] Remove the unconsumed Rust research prototype crate.
- [x] Review the Rust protocol crate and collapse duplicate error/result surfaces.
- [x] Review the Rust server crate and replace the temporary transport container.
- [x] Review the Rust type crate and remove the unconsumed research domain layer.
- [x] Remove the unconsumed Rust Git-workspace prototype crate.
- [x] Review the Rust runtime crate and remove unused retained state and parameters.
- [x] Review the Rust memory crate and remove unconnected compaction/rollout prototypes.
- [x] Review the GUI development scripts and merge duplicated process launchers.
- [x] Review the complete Tauri backend and collapse forwarding and bridge file layers.
- [x] Review the Python remote mirror backend and merge its wrapper layer.
- [x] Review remote dataset staging and remove diagnostic/test-only surface.
- [x] Review execution configuration and merge monitoring contracts into their owner.
- [x] Simplify the GPU-pool lifecycle and remove redundant lease/preflight surface.
- [x] Review compute diagnostics and derive redundant status fields.
- [x] Simplify the SSH backend construction and workspace lifecycle.
- [ ] Verify the final integrated application, publish completion report, remove plan and pointer.
- [x] Merge the reviewed TypeScript checkpoint into main, push, and remove this task's temporary branch/worktree.

## TUI module review

`TuiController` remains the focused boundary that validates the runtime's two event shapes, forwards the single message command, and owns subscription cleanup. Its constructor already subscribed synchronously, so the private `_subscribe` helper and public `connect`/`run` coroutine layers were redundant; `run` had no callers and `connect` was exercised only by tests after construction. Remove all three methods and the test-only `events_seen` instance list. The controller now has only the three runtime attributes required for forwarding and cleanup, while production construction, event validation, manual-mode toggling, and close behavior are unchanged.

Fresh baseline and post-change verification each passed the same 10 unit/integration tests in `test_controller.py` and `test_tui_protocol.py`; `python -m compileall -q src/athena_tui` and `git diff --check` also passed. This first slice advanced reviewed coverage to 96/602.

The remaining six TUI files were then read completely and their production/test callers traced. Retain the package facade, module entry, argument/runtime composition, immutable state projection, pure renderer, and prompt-toolkit application as distinct cohesive roles; merging state or rendering into the already-large application would increase coupling. The entrypoint has one options namespace and every helper owns a tested boundary, so it remains unchanged.

Within the application, localize the five history/pane controls that are retained by the prompt-toolkit layout rather than application logic, combine selection anchor/cursor into one tuple, and remove the constant exit-code attribute. `AthenaApp` construction falls from 17 instance attributes to 11. Delete the unused `plans` display-state copy and forwarding `control_status` property. Delete the test-only joined-history wrapper, and reduce `apply_event` from three parameters to two by removing its unread width. Runtime event order, selection, scrolling, clipboard fallback, dynamic panes, composer behavior, rendering, resume, and shutdown remain unchanged.

Fresh full TUI verification passed 91 unit/integration tests, the module CLI help path, Python compilation, the code-style hard-rule gate, removed-symbol searches, and `git diff --check`. Closing the remaining six rows advances reviewed coverage to 118/602. Whole-repository acceptance remains pending.

## GUI gateway boundary review

Read the gateway package marker, process entry, human-request broker, state store, and WebSocket transport completely and trace their callers through the handler and gateway tests. Retain the broker's legacy and scoped request paths because both are active, retain its context binding, and retain the transport's send lock, subscription replacement, and error projection because they enforce session and wire-protocol behavior. The package marker remains the required import boundary. The large request handler remains Pending for its own complete review.

Remove the state store's zero-logic default-state wrapper and construct its data record directly on the three fallback paths. The process entry now trusts the state store's existing project-root validation instead of validating the loaded value twice, replaces a fifteen-line callable Protocol with the canonical `Callable` alias, gives its nested runtime factory private scope, avoids rebinding the requested port, and lets `main` use `start_server`'s defaults. Remove the gateway broker's prohibited future import; its Python 3.11+ annotations need no compatibility layer. No compatibility aliases or new abstractions were added.

Fresh verification passed 104 gateway, state-store, protocol, clarification, and end-to-end tests; Python compilation, the code-style hard-rule gate, and `git diff --check` also passed. Pytest reported one existing Windows subprocess finalizer warning and one cache-permission warning after all tests passed. Closing five ledger rows advances reviewed coverage to 123/602. Whole-repository acceptance remains pending.

Handler follow-up: read the complete request handler and all direct gateway/protocol tests. Retain its explicit dispatch structure because it is the Python/Rust/GUI protocol boundary and the contract test parses those routes; retain all seven state fields and the suspend, swap, resume, deletion, and persistence helpers because their distinct order and failure behavior are tested. Named-session deletion was the one duplicated calculation: remove `_session_workspace_root` and derive the external workspace once from the already-resolved state root. The full 104-test gateway selection, compilation, style hard rules, removed-symbol search, and `git diff --check` passed. Closing the handler row advances reviewed coverage to 129/602.

## Research turn boundary review

Read the turn package boundary, shared wait helpers, General/Kaggle turn mixin, public dispatcher, and citation-support mixin completely, then trace their production and test callers. Retain these files as separate cohesive roles: the shared wait loop prevents circular imports, General owns optional Kaggle handoff policy, the dispatcher owns Supervisor/Data entrypoints, and support owns the two-stage citation policy. Their distinct failure and result contracts do not justify a generic turn-execution abstraction. The large Ideator lane module remains Pending for its own complete review.

Every production heartbeat call passed both the runtime and the same `runtime.agents` object. Remove that duplicate dependency parameter and derive the agent service from the runtime, reducing the helper from seven parameters to six while preserving event projection, cursor advancement, timeout interruption, heartbeat publication, and the direct-wait path. Delete the two one-call General/Kaggle tool wrappers and call the existing kind-parameterized tool constructor directly. No compatibility signatures or replacement wrappers remain.

The same focused baseline and post-change selection passed 78 turn, event-forwarding, citation, preflight, handoff, survey, ideator, and gate tests. Python compilation, the code-style hard-rule gate, removed-symbol searches, and `git diff --check` passed. Closing five ledger rows advances reviewed coverage to 128/602. Whole-repository acceptance remains pending.

## Web retrieval review

Read both retrieval files and trace General, baseline-Ideator, and direct test callers. Retain the module boundary because moving it into core agent tools would introduce a reverse dependency on research HTTP infrastructure. Retain the shared reference session, URL/page caches, query/fetch counters, HTTP limiter, parsers, search/fetch operations, and registry builder; each has active behavior or coverage. The package marker remains the minimal namespace boundary.

Delete `_WebTool`, which retained both a session and a second HTTP attribute and allowed callers to supply inconsistent dependencies. `WebSearchTool` and `WebFetchTool` now each accept only one optional `WebSession` and use that session's HTTP client. Their constructor dependency count falls from two to one; `build_web_tools` continues to give both tools the same session. Tests express injected HTTP through that single owner, including changing the shared fake response between search and fetch.

Fresh focused verification passed all 15 retrieval and baseline-tool wiring tests. Python compilation, the code-style hard-rule gate, removed-symbol searches, and `git diff --check` passed. Closing two ledger rows advances reviewed coverage to 131/602. Whole-repository acceptance remains pending.

## Root development-script review

All eight root development scripts were read completely and their package, hook, documentation, generated-fixture, and release callers were traced. Retain the code-style hook, frozen gateway entry, model-capability probe, headless runner, and article-preparation CLI because each owns a distinct executable workflow. Merge the duplicated release composition into `build.cjs` under `--package`, preserve the three `npm run release*` commands, update the direct documentation example, and delete `release.cjs`. The unified parser now honors the last repeated `--backend` value so npm-appended overrides behave predictably.

The Rust fixture exporter no longer accepts four unused path parameters, scans `Method` once, or carries three dead imports/constants. Its status text is ASCII-safe on the Windows console; protocol/domain output hashes remain byte-identical, while message/rollout diffs contain only their pre-existing generated timestamps. The article-preparation script removes a single-use hash wrapper and derives both manifest identity fields from its existing metadata record, reducing `write_manifest` from four parameters to two. Two prohibited future imports were removed. `check_code_style.py` and `run_headless.py` were already cohesive and retain their current interfaces.

Fresh verification passed Node syntax checking, both build/package invalid-backend gates (including npm overrides), Python compilation of all scripts, all three offline CLI help paths, the code-style hard-rule gate, a complete four-file fixture export, and an offline manifest update/round trip. `git diff --check` passed. Positive PyInstaller/Tauri packaging is not run in this source review because this environment lacks PyInstaller; the package command reaches the same preserved command composition. Closing eight ledger rows advances reviewed coverage to 104/602. Whole-repository acceptance remains pending.

## Rust tool-system review

All six `athena-tools` source files and both integration test files were read completely, with construction and execution call sites traced into `athena-agent`. Replace the builder-plus-registry pair with one consuming `ToolRegistry` backed by `BTreeMap`: this deletes the public `ToolRegistryBuilder`, removes its `build()` phase, removes the cached sorted-name field, and preserves deterministic prompt ordering intrinsically. Registration still rejects duplicate names and the finished value remains immutable to callers.

Merge the eight-line `context.rs` data holder into the cohesive tool contract module and delete the file. Remove the unread `tool_name` field from `ToolContext`, the never-populated `artifacts` field from `ToolResult`, and three unused result constructors. Agent and test construction now use the single registry directly; no compatibility facade or replacement state was added. Tool execution, cancellation, lifecycle events, result truncation, error sanitization, lookup, sorting, and search behavior are unchanged.

Fresh verification passed 22 tests across `athena-tools` and `athena-agent`, plus Clippy with `-D warnings`, Rust formatting, caller searches for every removed symbol, and `git diff --check`. Closing eight ledger rows advances reviewed coverage to 112/602. The touched `athena-agent` call sites remain Pending for their own full-file reviews, and whole-repository acceptance remains pending.

## Rust agent review

Read all seven `athena-agent` source files and all four integration-test files completely, then trace the public exports and runtime/tool consumers. Retain input resolution, provider mapping, OpenAI transport, runtime adaptation, and sub-agent control as separate cohesive boundaries. Their filesystem, wire-format, runtime, and lifecycle responsibilities should not be merged into the already-large ReAct loop. The provider's real-network status/SSE error hardening remains a distinct follow-up; this review does not claim external transport readiness.

Delete the crate-local `AgentOutcome` duplicate and return `athena_runtime::TurnOutput` directly, removing the runner's field-by-field conversion. Delete the uncalled public `Agent::tools` getter, three unused direct dependencies, the redundant `Step::Error` state, and `emit_calls`' unread finish-reason parameter. Abort the per-turn cancellation bridge when the agent finishes instead of leaving one waiting task per successful turn. A pre-cancelled run now returns `AgentError::Cancelled` instead of a successful output, and plain assistant history keeps the assistant role when mapped to the provider API.

Fresh verification passed all 10 `athena-agent` tests, including new cancellation and assistant-role regressions, and the complete Rust workspace passed 190 tests. Workspace Clippy with `-D warnings`, Rust formatting, removed-symbol/dependency searches, and `git diff --check` passed. Closing eleven ledger rows advances reviewed coverage to 142/602. Whole-repository acceptance remains pending.

## Rust research prototype review

Read all three `athena-research` source files and their six inline tests completely, then search every Rust manifest/source and repository documentation reference. No production code or other crate consumes `athena-research`; workspace membership was its only external edge. Its experiment/tree model is also substantially behind the authoritative Python `ResearchTree`, so retaining it as a self-testing third implementation creates drift without a runtime capability.

Delete the entire crate rather than polishing an unused public API. Remove its workspace member and dependency declarations, regenerate the lockfile, and remove the completed-crate claim from the Rust README. The historical WIP inventory remains unchanged because it explicitly records baseline state rather than current architecture.

After deletion, the complete remaining Rust workspace passed 184 tests. Workspace Clippy with `-D warnings`, Rust formatting, repository reference searches, and `git diff --check` passed. Closing the three deleted source rows advances reviewed coverage to 145/602 while removing 359 baseline production/test lines plus the crate manifest. Whole-repository acceptance remains pending.

## Rust protocol review

Read all five protocol source files and the Python-fixture integration test completely, then trace every exported DTO, method, error helper, and message union through `athena-server` and `athena-runtime`. Retain the six request parameter DTOs, six wire envelopes, two transport unions, nine error codes, and twelve method names because they are consumed or fixture-governed. The touched server callers remain Pending for their own complete module review.

Delete five result DTOs that were never constructed, the unused exception-name mapper, six code-specific error factories, the `ErrorCode::code` forwarding method, and the unused `thiserror` dependency. `RpcError::new` is now the single construction interface. Move the application-only `RpcException` out of the protocol crate into the server client and store one `RpcError` instead of copying its three fields. Merge the 19-line method module into the protocol facade and delete its file. Remove thirteen duplicate inline tests; the fixture suite retains exact wire coverage and the two unique control/empty-response contracts.

Fresh verification passed 26 protocol fixture tests and five server tests; the complete Rust workspace passed 167 tests. Workspace Clippy with `-D warnings`, Rust formatting, removed-symbol searches, and `git diff --check` passed. Closing six protocol rows advances reviewed coverage to 151/602. Whole-repository acceptance remains pending.

## Rust server review

Read all seven server source files and the end-to-end test completely, with protocol/runtime channel and lifecycle callers traced. Retain the client, execution adapter, lifecycle, processor, subscription registry, and transport as separate responsibilities. Their error, request dispatch, shutdown, subscription pump, and independent event-lane contracts remain covered by real in-process flows.

Replace the eight-field `Transport` object, which was always immediately split, with one zero-argument `transport()` factory that constructs the client and server halves directly. Collapse seven typed/test-only half forwarding methods into two generic raw-client operations, delete the unused `Full` transport error and server-request sender, and keep server event delivery reliable through its canonical channel sender. Inline the one-call five-parameter client worker helper into client startup; remove the post-start ready receiver field, public Sequencer surface, test-only processor state getter, and four state constant exports. `MessageProcessor::start` drops its fixed capacity argument (three parameters to two), and subscription shutdown drains its map once instead of repeatedly locking it.

Fresh verification passed all five server unit/end-to-end tests, including ready admission, independent event delivery, full turn lifecycle, and shutdown. The complete Rust workspace passed 167 tests; workspace Clippy with `-D warnings`, Rust formatting, removed-symbol searches, and `git diff --check` passed. Closing eight server rows advances reviewed coverage to 159/602. Whole-repository acceptance remains pending.

## Rust type-contract review

Read all four type source files and the Python-fixture test completely, then trace every export across all remaining Rust crates. The thread/turn DTOs, four validated identifiers, two runtime status enums, and validation error are active cross-crate contracts and remain. After removing the unconsumed Rust research prototype, the seven research-domain types in `domain.rs` had no remaining production caller and existed only to pass their own tests.

Delete `domain.rs` and its `Hypothesis`, `ExperimentPlan`, `DataCard`, task metadata, metric, and hypothesis-status exports rather than retaining a stale second domain model. Remove the fourteen duplicate inline tests and eight fixture cases that only certified those dead types. Replace the public two-layer `NonBlankString` wrapper with direct String-backed ID newtypes, deleting unused `TryFrom<String>` and `AsRef<str>` implementations while preserving validation, `new`, `as_str`, display, and exact Serde behavior. Make the source modules private, move `serde_json` to test-only dependencies, and remove the later-orphaned `CommitHash` identifier with the Git-workspace crate.

Fresh focused verification passed all six retained thread/turn/identifier fixture cases. The complete Rust workspace passed 144 tests; workspace Clippy with `-D warnings`, Rust formatting, removed-symbol searches, and `git diff --check` passed. Closing five type rows advances reviewed coverage to 164/602. Whole-repository acceptance remains pending.

## Rust Git-workspace prototype review

Read all five `athena-workspace` source files and its real-Git integration test completely, then trace every manifest, type, and constructor reference across the Rust workspace. No remaining crate consumes its worktree manager, branch model, runner, writer, or error types; workspace membership and its own five tests were its only edges after the unused research prototype was removed.

Delete the entire crate instead of retaining a four-field manager and two injectable interfaces with no runtime path. Remove the workspace member/dependency declarations and current README row, regenerate the lockfile, and remove `CommitHash`, whose only production consumer was this crate. The historical WIP inventory remains unchanged because it records the baseline migration state.

After deletion, the complete remaining Rust workspace passed 139 tests. Workspace Clippy with `-D warnings`, Rust formatting, repository reference searches, and `git diff --check` passed. Closing six deleted workspace rows advances reviewed coverage to 170/602 while removing 846 baseline source/test lines plus the crate manifest. Whole-repository acceptance remains pending.

## Rust runtime review

Read all seven runtime source files and all three integration-test files completely, then trace their public methods and event/submission contracts through the agent and server crates. Retain the actor, handle, manager, runner, journal, and submission boundaries because each owns distinct concurrency or lifecycle behavior. Retain generation counters, completed context snapshots, journal tail state, and atomic thread state because race, fork, subscription, and shutdown paths consume them.

Remove the unused memory dependency; seven unread journal inspection/subscription helpers; four write-only subscription fields; the write-only completed-result map; the unused manager lookup; and the handle's unread thread ID. Successful runner signals no longer carry a result reference that the actor discarded. Interrupt, shutdown, and manager-close interfaces no longer accept ignored reason strings. Event forwarding moves owned fields instead of cloning them, and the subscription registry no longer wraps its lock in an unnecessary second `Arc`. No compatibility wrappers or replacement abstractions were added.

Fresh focused verification passed 25 runtime/server/agent tests. The complete Rust workspace passed 139 tests; workspace Clippy with `-D warnings`, Rust formatting, removed-symbol searches, and `git diff --check` passed. Closing ten runtime rows advances reviewed coverage to 180/602. Whole-repository acceptance remains pending.

## Rust memory review

Read all five memory source files and three integration-test files completely, then trace every export and `ContextManager` method through the Rust workspace and current documentation. The Rust compactor and rollout recorder had no production caller outside their own crate, while Python owns the connected compaction, checkpoint, recovery, and rollout path. The exported `AgentRunner` likewise had no constructor or runtime caller; its only retained configuration fed the unused Rust context limit.

Delete the unconnected compaction, rollout, and agent-runner implementations plus their exclusive and duplicate tests. Merge the active context buffer into `message.rs` and make that module private behind the crate facade. `ContextManager` falls from four fields to one and its constructor from one ignored limit argument to zero; token/version/snapshot/rollback/range APIs disappear with their only consumers. The crate's production dependencies fall from six to one. Retain message normalization, detached history, all wire types, and the fixture-governed Python serialization contract. Update the Rust README so it no longer advertises removed prototypes.

Fresh focused verification passed 19 memory/agent tests. The complete Rust workspace passed 80 tests; workspace Clippy with `-D warnings` and Rust formatting passed. Closing eight memory rows advances reviewed coverage to 188/602. Whole-repository acceptance remains pending.

## GUI development-script review

Read all three GUI development scripts completely and trace their npm lifecycle and direct package-script callers. Retain the generated-directory preparation script because both development and production builds require its distinct filesystem precondition. The backend-only and browser-preview launchers duplicated repository resolution, virtual-environment selection, port propagation, gateway spawning, diagnostics, and signal forwarding.

Merge both launchers into `dev.cjs`, using one `--backend-only` switch while preserving the public `npm run backend` and `npm run dev:web` commands. Share one two-argument child monitor and one shutdown path; invoke npm as an executable plus literal arguments so browser preview also works outside Windows. Delete both old scripts and add no compatibility wrappers.

Node syntax checks and obsolete-reference searches passed. The complete TypeScript/Vite production build passed before and after the change, including the retained generated-directory prebuild. Closing three baseline script rows advances reviewed coverage to 191/602; the replacement `dev.cjs` is separately tracked below. Whole-repository acceptance remains pending.

## Tauri backend review

Read all eighteen Tauri backend source files completely and trace every command through `generate_handler!`, the frontend bridge, Python gateway, event relay, and native entrypoint. Retain clarification and directory-dialog files because they own typed reply validation and platform-path selection with focused tests. Retain `main.rs` as the platform entrypoint and the application composition root in `lib.rs`. All other command files were import-plus-forwarder partitions with no independent state or policy.

Merge nine pure RPC command files into `commands/mod.rs`, make the two meaningful child modules private, and register all 36 commands through one flat command namespace. Delete the Rust-only `HumanRequest` mirror and its tests because production never deserialized it; retain the actual `HumanReply` input contract. Collapse `python/mod.rs`, `bridge.rs`, and `types.rs` into one `python.rs`; drop the unread event subscription ID and redundant error trait implementation. Inline the single-consumer event relay into `lib.rs`, remove per-event diagnostic serialization, and delete `events.rs`. No dynamic arbitrary-method command or macro-generated compatibility layer was introduced.

Fresh verification passed seven Tauri tests, including a real Python gateway RPC round trip, plus ten frontend bridge contract tests. Tauri formatting, all-target Clippy with `-D warnings`, command registration compilation, and obsolete-module searches passed. The Python gateway logs an existing connection-reset traceback when the Rust integration test closes its socket, but both test runs exit successfully. Closing eighteen baseline rows advances reviewed coverage to 209/602; the replacement `python.rs` is separately tracked below. Whole-repository acceptance remains pending.

## Python remote-mirror review

Read the execution backend, remote mirror/wrapper, remote facade, backend seam tests, and SSH backend tests completely; trace mirror and backend access through the runtime, pool, channel, dataset staging, and focused tests. Retain `ExecutionBackend` and `LocalBackend` as the source-independent runtime seam. Retain manifest hashing, bounded push/pull, pruning, source suffix policy, oversized-output evidence, and the inner SSH backend because each has a production pool or runtime consumer.

Merge `MirroredBackend` into `mirror.py` and delete `mirrored.py`, keeping workspace mirroring and its execution wrapper in one module. Remove two never-customized constructor policy parameters and their fields, reducing the wrapper from five attributes to three and its constructor from four arguments to two. Remove the test-only mirror getter plus three public WorkspaceMirror accessors used only by the former cross-file wrapper; same-module code now uses the owned mirror state directly. Export the merged backend from the remote facade and make the release-cleanup test trigger push through the real `run` contract rather than a test-only getter.

The same focused backend/channel/SSH/pool/runtime selection passed 96 tests before and after the change. Python compilation, task-scope code-style checks, and removed-module/surface searches passed. Pytest reported one existing cache-permission warning. The repository-wide style command still reports pre-existing hard-rule violations outside this slice, so whole-repository acceptance remains pending. Closing four baseline rows advances reviewed coverage to 213/602.

## Remote dataset-staging review

Read the dataset stager and its full integration-style unit suite completely, then trace specs, reports, capacity checks, placement evidence, staging, reuse, and eviction through the execution pool and compute checks. Retain content-addressed manifests, completion markers, mandatory verification, streaming file transfer, partial resume, staged-ID discovery, and pinned-dataset eviction because each protects an exercised corruption, capacity, or lifecycle boundary.

Remove the test-only `StageReport.uploaded` field and entirely unused `resumed` projection, reducing reports from five fields to four. Delete the never-disabled `verify_existing` parameter so `stage` takes only the local root and spec and always verifies completed remote data. Inline the one-call marker payload, privatize remote-root and staged-ID helpers, and make tests assert bytes and filesystem outcomes rather than internal transfer lists. Remove redundant local collections/path wrapping. Marker probing now catches only `RemoteError` instead of hiding arbitrary programming failures.

The same dataset/pool selection passed 25 tests before and after the change. Python compilation, task-scope code-style checks, and removed-surface searches passed; pytest reported one existing cache-permission warning. Closing the dataset row advances reviewed coverage to 214/602. Whole-repository acceptance remains pending.

## Execution configuration and monitoring review

Read `compute_config.py`, `events.py`, and `monitor.py` completely and traced their configuration fields, parsers, event contracts, monitor lifecycle, and public facade through the CLI, runtime settings, compute checks, and app-server observer. Retain `ComputeConfig` unchanged: all six fields have independent production consumers, file loading and mapping parsing serve separate CLI and live-settings boundaries, and the `remote` projection removes repeated mode checks at three call sites. Its focused configuration/check suite passed 18 tests.

Merge the source-independent event contracts into their sole implementation owner, `monitor.py`, and delete `events.py`. External consumers continue to import every public contract and `ExecutionMonitor` through `athena.execution`; the app-server's internal clock import now targets the owning module directly. Make the shared numeric validator private because it has no external caller. This removes one source file and one internal module dependency without adding a compatibility shim or changing the monitor constructor, event fields, state transitions, JSON metadata validation, or retention behavior.

The same 43 event, monitor, observer, and thread-runtime tests passed before and after the merge. Python compilation, Black, `git diff --check`, and obsolete-import searches passed. The broader execution suite reached an unrelated host-environment assertion that requires PowerShell 7 while this machine currently resolves Windows PowerShell 5.1; it does not exercise the merged modules. Closing three baseline rows advances reviewed coverage to 217/602. Whole-repository acceptance remains pending.

## GPU-pool lifecycle review

Read `pool.py` and its complete unit suite, then traced pool construction, readiness checks, lease acquisition/release, placement evidence, and remote execution through runtime bootstrap/settings/facade and the remote-experiment integration tests. Replace the public `cards() -> preflight() -> acquire()` sequencing contract with one `acquire()` operation that performs the initial host probe itself. Explicit operator diagnostics remain owned by `check_compute`; production callers no longer inspect pool internals to decide whether an initialization method must run.

Remove `Lease.plan_id` and `Lease.host`, deriving the retained host identity from `HostCard.name`; lease attributes fall from eight to six. Remove the unconsumed GPU-memory, Python-version, and package fields from the scheduler's retained card models while leaving the remote probe protocol intact for compute diagnostics. Use the condition's own lock instead of retaining a duplicate lock attribute, and derive dataset host state from the lease so `_stage_dataset` loses one parameter. Delete the test-only pool host/card projections and update tests to assert acquired lease behavior.

The pool's 14 tests passed after the change. The broader pool, dataset, compute-check, and remote-experiment selection passed 36 tests. Python compilation, Black, `git diff --check`, and removed-interface searches passed; pytest reported one existing cache-permission warning. The previously reviewed execution package facade is also reconciled in the ledger. Closing the pool and facade rows advances reviewed coverage to 219/602. Whole-repository acceptance remains pending.

## Compute-diagnostics review

Read `check.py` and its complete unit suite, then traced the check/result/printing flow through the CLI, compute configuration, remote channel, dataset description, and SSH runtime description. Retain this file as the cohesive operator-diagnostics boundary: merging it into the already-large CLI would mix remote probing, disk inspection, rendering, and command dispatch, while the configuration parser has independent runtime consumers.

Remove the unread `ScratchUsage.exists` field, reducing that record from five fields to four. Replace the stored `HostCheck.ok` boolean with a property derived from its authoritative error string, reducing retained host state from seven fields to six and removing the corresponding constructor argument at every result path. Preserve raw probe facts, runtime text, GPU details, scratch entries, and dataset fit because each is printed or asserted by the diagnostic contract.

The same 18 compute-check/configuration tests passed before and after the change. Python compilation, Black, `git diff --check`, and field-construction searches passed; pytest reported one existing cache-permission warning. Closing the compute-check row advances reviewed coverage to 220/602. Whole-repository acceptance remains pending.

## SSH backend review

Read `remote/ssh.py` and its complete unit suite, then traced host configuration, transport startup, backend construction, workspace mirroring, dataset staging, diagnostics, execution, output persistence, and cleanup through the pool, compute check, runtime protocol, and remote-experiment integration. Retain `SshHost`, `SshTransport`, and `SshBackend` as the configuration, byte-transport, and execution-policy boundaries; combining them would mix immutable host policy with per-connection process state and per-lease execution state.

Make each `run(workspace_root=...)` call the sole authority for local-to-remote cwd translation, removing the duplicate `_local_workspace` attribute plus `bind_local_root` and `_remote_cwd`. Remove the `remote_data_root` constructor parameter; the existing `set_data_root` now exclusively records the content-addressed path after staging. Delete the redundant `prepare_remote` step because both mirror push and direct execution create their target directories. Inline the single-use remote PATH projection and remove the test-only `facts` property. The backend constructor falls from six parameters to five, retained instance state falls from seven attributes to six, and four public/private helper methods disappear.

The same 29 SSH/backend/environment tests passed before and after the change. The broader SSH, command-request, pool, dataset, compute-check, and remote-experiment selection passed 71 tests. Python compilation, Black, `git diff --check`, and removed-interface searches passed; pytest reported one existing cache-permission warning. Closing the SSH row advances reviewed coverage to 221/602. Whole-repository acceptance remains pending.

## DSH composition-root review

DSH full-file decision: retain one composition root for Cordis service installation, 18 tool definitions, and subagent-backed Supervisor workers. Splitting these cohesive closures would add configuration, service, and worker interfaces without removing runtime state. Retain the boundary argument readers because DSH supplies `unknown` tool input, retain the declarative tool factory, and retain the eight isolated Cordis services because the preset, worker wiring, and autoresearch integration consume their independent identities. The default plugin entry and configurable factory serve distinct loader and test/composition signatures.

Caller tracing found `researchStatus` and `researchTreeSnapshot` were implementation projections accidentally exported from the package: the former had only same-file and direct-test callers, while the latter had only same-file callers. Both are now private; tests use live Context state and registered tools, and reject the removed runtime exports. `dshWorkers` and its named options remain the tested subagent adapter boundary. Focused DSH and cross-package headless verification passed five tests. All five TypeScript packages built, compiled ESM/declaration entries contain no exports for the removed projections, and the full workspace passed 537 tests across 53 files in 145.01 seconds with `--testTimeout=30000`; `git diff --check` passed. Closing the DSH source and test rows advances reviewed coverage to 95/602. Whole-repository acceptance, main integration/push, and temporary task branch/worktree cleanup remain unfinished.

## Scheduler runtime-input follow-up

Scheduler full-file review follow-up: replace the separate running-ID argument and options object with one optional runtime record, reducing `nextActions` from four parameters to three. The record contains only ephemeral inputs (`running` and `humanNext`); durable manual mode now has one authority in `ResearchState.manual_mode` instead of being copied into every call. Inline the single-use runtime shape and keep action types private. Remove `countSearchAttempts` from the package barrel after caller tracing found only Supervisor and direct scheduler tests; it remains an internal module export for those consumers. Scheduling order, recovery priority, creation limits, manual selection, ranking, and selection-local deduplication are unchanged.

Fresh focused verification passed 69 tests: 19 Scheduler, 47 Supervisor, and three public-surface tests. All five TypeScript packages built successfully. The full workspace passed 537 tests across 53 files in 148.22 seconds with the command-only `--testTimeout=30000` override, and `git diff --check` passed. All touched source/test rows were already Reviewed, so baseline coverage correctly remains 93/602. Whole-repository acceptance, main integration/push, and temporary task branch/worktree cleanup remain unfinished.

## Supervisor workspace ownership

Supervisor full-file decision: retain the orchestration module as one phase owner; splitting its state machine would add cross-file lifecycle interfaces. The source and test were read end to end, all public methods/getters were traced, and DSH/autoresearch call sites were inspected. Concurrent VALIDATE commands coalesce to one worker call and one failure report. A SEARCH command outside SEARCH is rejected before mutation. Interactive validation errors now persist/report FAILED, and a SEARCH error during validation handoff propagates and prevents validation. `fail()` ignores repeated reports after the first canonical FAILED transition.

Removed the duplicated evaluator constructor option and mutable field: the baseline experiment's durable `run_config_ref` is now the sole authority, including after reconstruction. This also deleted DSH's `baselineEvaluatorRef` helper. Grouped direction/tolerance/auto-validation, state/tree paths, and one-shot/persistent guidance into cohesive records; Supervisor instance fields fell from 18 immediately before this slice to 14. Removed six unreferenced callback type aliases and their barrel exports by inlining their signatures into `SupervisorWorkers`. The external constructor remains one options object and existing DSH worker composition is unchanged.

The new focused boundary suite passed 54 tests; after all lifecycle and attribute changes the Supervisor file passed 47 tests. Final verification after the type-alias removal passed all five package builds and 537 tests across 53 files in 111.52 seconds with `--testTimeout=30000`; `git diff --check` passed. The timeout override accommodates slow real-Git tests on this host and is not evidence for the default timeout. Closing these two baseline ledger rows advances reviewed coverage to 93/602. DSH was inspected only at affected call sites and remains Pending.

Phase-ownership follow-up: replaced `searchPromise`/`runSearch()` with one `phasePromise`/`runPhase(work)` slot shared by recovery, PREPARE, SEARCH, and VALIDATE. No additional Supervisor attribute was introduced. Startup checks the stopped flag before continuing to another phase. Interactive validation first drains SEARCH, then owns both the phase transition and validation work; automatic validation uses the same owned boundary. The private `stopDispatch()` is now shared by the two actual production consumers (stop and validation handoff), not reintroduced as a public/test-only API. `requestStop()` preserves already-produced COMPLETED or FAILED instead of overwriting terminal results with STOPPED.

Phase-ownership verification: all five package builds passed; the full TypeScript run with `--testTimeout=30000` passed 532 tests across 53 files in 73.07 seconds. `git diff --check` passed and removed SEARCH-only owner searches found no production references. Baseline coverage remains 91/602. Whole-repository acceptance, main integration/push and temporary task branch/worktree cleanup remain mandatory and unfinished.

Three pre-change gated tests showed stop returning before PREPARE, resumed VALIDATE, and interactive VALIDATE workers finished. Afterward, 42 Supervisor tests passed, including preserved phase failure during stop and SEARCH-to-interactive-validation handoff with auto-validation enabled (one validation call). Snapshot checks use the current Supervisor state because recovery intentionally replaces the input state object. These guarantees are graceful joining, not forced process cancellation. Concurrent/conflicting user phase commands and direct interactive-validation failure reporting still need review before the Supervisor ledger closes.

Public-surface follow-up: removed `startValidation()` and moved its tool-response projection to DSH's existing `research_validate` adapter, which still calls `setPhaseDecision("VALIDATE")` and returns `{status}`. Folded the test-only public `stop()` into the actual `requestStop()` operation. Made six internal methods private: `continuePhase`, `runPrepare`, `runValidation`, `runSearch`, `startPlan`, and `registerHypotheses`. Tests probe private SEARCH/Plan machinery explicitly and clean up through the real public stop request. `startPlan` no longer returns an unread ID; registration returns the one boolean consumed by slot filling, rather than constructing an unused ID list and wrapper.

Public-surface verification: all five package builds passed; the full TypeScript run with `--testTimeout=30000` passed 526 tests across 53 files in 74.00 seconds. `git diff --check` passed. Main integration/push and temporary task branch/worktree cleanup remain required at whole-goal completion, not at this API slice.

Caller tracing retained `recover()` (autoresearch engine), both getters (DSH status/evaluator wiring), and the actual tool-facing methods. The compiled declaration confirms the six methods are private; this is TypeScript API encapsulation, not runtime access control. Focused verification passed 43 tests; the DSH validation adapter test confirms the delegated decision and unchanged response. An initially unsupported Vitest matcher was replaced by this workspace's supported call-count and argument assertions. Whole Supervisor lifecycle review remains open, especially PREPARE/VALIDATE stop ownership and stop/failure reporting races. Coverage stays at 91/602.

Concurrent-error follow-up: running turns attach rejection handlers when launched and deliver a plan-tagged error to the same SEARCH loop that applies normal completions. The loop retains its first error locally, stops filling slots, drains remaining completions, then rethrows for the existing startup/background reporting boundary. Failed turns retain their frozen Plan rather than inventing an untrusted result; successfully returned siblings still settle. Errors from ideation or applying a completion use the same drain path. Existing recoverable Agent-turn exceptions still produce a null decision as before. Removed the unused public `CompletedTurn` export; its private success record now carries `nextState` rather than a full `PlanTurnResult` whose other fields were unread.

Concurrent-error verification: all five package builds passed; full TypeScript run with `--testTimeout=30000` passed 525 tests across 53 files in 77.42 seconds. `git diff --check` passed; caller search found no consumers of the removed type export. Baseline coverage remains 91/602, with whole-goal acceptance, main integration/push and temporary task branch/worktree cleanup still outstanding.

Two pre-change tests demonstrated early startup completion with an unresolved sibling, for both deterministic-turn and ideator errors. The resulting 36-test Supervisor file passed, including rejection with `undefined` and an early turn rejection while slot filling awaits ideation. The latter spans an event-loop turn without unhandled rejection. No additional Supervisor attribute, second task registry, or retry layer was introduced. PREPARE/VALIDATE shutdown and failure-reporting/stop races remain unfinished; these tests are not evidence for forced cancellation or recovery from a permanently hung worker.

Failure-reporting follow-up: extracted the startup error path into one private `fail(error)` method and replaced background SEARCH's empty catch with that method. Both paths stop new dispatch and save FAILED before attempting output/state notifications. `Promise.allSettled` attempts both notifications independently and prevents notification rejection from replacing the canonical failure result; disk-save failure still propagates. No error-history field, retry wrapper, or parallel status model was added. Direct callers of `runSearch()` still receive its rejection; startup and fire-and-forget dispatch own reporting.

Failure-reporting verification: all five package builds passed; full TypeScript run (`--testTimeout=30000`) passed 521 tests across 53 files in 83.21 seconds. `git diff --check` passed. Coverage remains 91/602; no claim of whole-application readiness or full Supervisor lifecycle completion is made. Main integration/push and temporary task branch/worktree cleanup remain mandatory at whole-goal completion.

The new pre-change entry comparison reproduced a background-only failure: startup persisted FAILED, while resume swallowed the identical ideator exception and left RUNNING. Afterward, 32 Supervisor tests passed, including both entries and rejection of each failure-notification channel. This does not yet establish cleanup of every concurrently rejecting worker, failure reporting ownership during stop races, or PREPARE/VALIDATE shutdown. Those remain required before closing the Supervisor ledger rows.

Graceful-stop follow-up: `stop()` now joins the SEARCH Promise as well as already-running turns. The loop stops filling slots but drains completed turns through its existing settlement path; only after joining does stop clear residual handles. This removes competing lifecycle ownership that previously let STOPPED return while ideation still wrote the tree, and abandoned the second of two completed turns without applying its result. A stop observed after asynchronous Plan creation retains that zero-turn Plan without dispatching it. No AbortController, second task registry, or cancellation adapter was added. DSH's stop description now accurately says it waits for in-flight work, rather than claiming local cancellation.

Graceful-stop verification: full TypeScript run with `--testTimeout=30000` passed 517 tests across 53 files in 73.39 seconds; `git diff --check` passed. No baseline ledger rows are newly closed by this lifecycle slice; coverage remains 91/602. Whole-goal acceptance, main integration/push, and temporary task branch/worktree cleanup remain required.

Two new pre-change tests failed with concrete evidence: stop returned before gated ideation was released, and concurrent settlement left statuses `[FAILED, RUNNING]`. After the change, 28 Supervisor plus four DSH tests passed, including a third gated Plan-creation/stop regression. The ideation fixture initially counted the baseline's existing pending hypothesis; corrected the assertion to identify the newly returned hypothesis specifically. Five package builds passed. These checks cover normally resolving in-flight work, not workers that never resolve, worker rejection during draining, PREPARE/VALIDATE stop ownership, or background error reporting. Those remain part of the unfinished Supervisor lifecycle review.

SEARCH wake follow-up: all resume-capable actions use `spawnSearch()` to wake an existing loop or start one if absent. Removed three duplicate wake calls and replaced the waiter Set/iteration with a single nullable callback, matching the one-loop `searchPromise` owner. `waitForWake()` rechecks RUNNING after asynchronous state publication so a resume during publication is not lost. Pause and stop retain their direct wake operation. No scheduler ranking, concurrency budget, or worker cancellation policy changed.

Verification for wake ownership: all five package builds passed; the final full TypeScript run (`--testTimeout=30000`) passed 514 tests across 53 files in 66.93 seconds. A final test-only microtask barrier ensures the stop case actually enters the waiting loop; the 25-test Supervisor file was rerun successfully after that barrier. `git diff --check` passed. Baseline coverage remains 91/602; main integration and task branch/worktree deletion remain deferred until whole-goal acceptance.

Before the production change, new tests reproduced two failures: search-budget updates and SEARCH phase decisions left an existing loop asleep. Afterward, 25 Supervisor tests passed, including five resume paths (search budget, Plan budget, mode, resume, phase), joining the same loop, stopping while waiting, and a gated in-flight WAITING publication. The Plan-budget fixture uses one slot: with two slots, a second ideator call is legitimate concurrent scheduling rather than a duplicate loop. A follow-up ownership test covers a worker synchronously rejoining SEARCH: register the Promise before invoking worker callbacks, using a microtask for loop startup. Reset the stopped flag before that microtask, so a stop issued before startup is not overwritten. Full-file completion remains pending: next inspect stop during active workers/ideation and background failure ownership; these tests do not prove worker cancellation.

Follow-up recovery review: `reconcilePlans(state, tree)` now only reconciles canonical state. Deleted `ReconcileOptions` and its two callbacks; the only production caller had supplied constant `true` functions. Supervisor recovery reads each retained context through the artifact store and checks that each SEARCH workspace is a directory. Missing resources retain the frozen Plan and persist WAITING. Other artifact errors and filesystem errors propagate instead of being silently treated as absent. This checks context availability and SEARCH directory existence, not Git integrity, every transitive artifact, or PREPARE/VALIDATE workspace health.

Kept the small pure recovery module because canonical reconciliation remains independently testable without filesystem setup. Ten reconciliation tests preserve settled/orphan removal and durable configuration; four real Supervisor resource combinations replace the two callback-only missing-resource tests. The focused Supervisor/recovery run passed 27 tests. Initial new assertions incorrectly compared omitted in-memory defaults with serialized defaults; corrected them to compare canonical JSON projections. Five package builds passed. Full TypeScript verification with the 30-second per-test override passed 506 tests across 53 files in 78.65 seconds. This supersedes earlier ledger descriptions of the callback-based checks.

Removed the private `branches` map and its second write during Plan creation. `workspace(planId)` now reads the existing `exp_${planId}` experiment's `gitwork`, the same record serialized in the research tree. Deleted `workspacePath()`: repository-wide TypeScript caller tracing found no consumers; DSH's PlanRunner construction uses `workspace()` and is unchanged. Unknown workspaces now use the tree's existing unknown-experiment error instead of an undefined map entry. No persisted schema or scheduling policy changed.

Baseline Supervisor tests: 12 passed. After the change, Supervisor/recovery tests: 25 passed. The new regression creates a Plan, reloads both JSON files into a fresh Supervisor, runs recovery, and confirms the active Plan still resolves its original workspace. This verifies metadata reconstruction, not existence or health of the physical Git worktree. All five TypeScript package builds passed.

Full TypeScript verification (`npm test -- --testTimeout=30000`): 504 passed across 53 files in 94.40 seconds. The 30-second per-test override accommodates this host's real Git operations; this is not evidence that the default timeout is reliable.

This earlier checkpoint is superseded by the completed lifecycle/caller review above. Supervisor source/test are now Reviewed and current coverage is 93/602. Whole-repository acceptance, main merge/push, and temporary task branch/worktree cleanup remain required and unfinished.

## Serving findings

`predictions_api.py` now owns the HTTP implementation and executable module entrypoint. Deleted `http_api.py` and the broad compatibility reexports; tests import model contracts from `model.py`. A tracked-file caller audit found only the removed shim importing `http_api`; the other mention was historical design documentation. Keep the model boundary because preprocessing and inference are independently testable. The fixed TESS feature contract cannot be generalized by changing names alone.

Verification: `python -m pytest test/unit/serving -q -p no:cacheprovider` returned 22 passed before and after the change. `python -m athena.serving.predictions_api --help` and `git diff --check` exited 0. Remaining files are explicitly pending; this slice does not close the repository-wide goal.

## File coverage

Memory module: all four files and runtime call sites inspected. Retain the context/token accounting, summarization, and JSONL persistence boundaries. Removed the recorder's per-instance reference to the global serialization adapter (5 slots to 4), and made asynchronous recovery call the synchronous implementation directly, deleting the private forwarding function. The existing rollback/compaction checkpoint contract remains used by ThreadRuntime. Focused memory, rollout recovery, and agent restore tests: 42 passed before and after.

Agent session and orchestration: removed `_MemoryView` and migrated every `.memory.raw` consumer to the thread-owned ContextManager. RunSession now uses a frozen, slotted dataclass instead of a handwritten constructor and four forwarding properties. Bindings remain immutable; mailbox reads remain non-consuming until checkpoint; failed turns retain retryable messages. Both list and deque mailboxes are covered. Followup tools retain only runtime, deleting the unused empty agent ID; the shared runtime tool inherits BaseTool's abstract execute contract instead of repeating it.

Agent registry: factories now accept only agent_id. Deleted the config parameter that require_spec always supplied as None, migrated both production factories and test factories, preserving fresh per-instance bindings, duplicate registration errors, and sorted type enumeration.

Verification for session/orchestration/registry: before the latest tool and factory simplifications, `test/unit/agent test/unit/app_server` returned 289 passed and 25 subtests. After migration, those suites plus Kaggle wiring/handoff and integration rolling-search/search-recovery returned 321 passed and 25 subtests. Session coverage includes list and deque mailbox acknowledgement and immutable memory binding. The scope ledger now has 12 reviewed baseline files, not a completed repository review.

Agent sampling runtime: read the complete module and traced private helper callers and tool concurrency declarations. Delete task placeholders, index-based result realignment, and the redundant completion flag parameter. Finalization now pairs ordered calls and gathered results directly (5 parameters to 4). Replace two dispatch closures and captured default arguments with one dependency-aware coroutine (6 parameters to 4). Dependencies are frozen before appending the current task, preserving parallel groups and serial barriers without self-dependencies. Retain the early had_calls flag in the streaming error path: receiving a call must disable automatic retry even if event emission fails before dispatch. Retain streaming/structured retries, input loading, and the currently consumed factory and thread-adapter APIs; broader construction API consolidation remains a follow-up with single_turn_chat.

Sampling verification: focused Agent/truncation/single-turn baseline and post-change checks both returned 70 passed. Added a five-tool parallel/serial/parallel regression that checks result-to-call alignment and uses events to prove parallelism. Agent, AgentRuntime, app-server, and single-turn suites then returned 356 passed and 25 subtests. Coverage now includes 13 reviewed baseline files; this is not full application acceptance.

Agent construction and one-turn calls: replace five primitive factory configuration keywords with the existing AgentConfig (create_agent: 9 parameters to 5). Remove create_code_agent, a pure constructor forwarding function with no independent production consumers. Known-provider callers construct Agent directly. Single-turn text calls accept the same config object instead of three sampling keywords (11 parameters to 9); citation support explicitly preserves its 200-token limit and 0.1 temperature. Explicit config is not copied or rebuilt; settings defaults and seed=None remain expressible.

Merged text and structured chat into core/agent/chat.py. Deleted utils/__init__.py, utils/single_turn_chat.py, and research/idea_generation/structured_chat.py, without compatibility shims. Both call modes share context construction while retaining their identity namespaces, literal prompt handling, tools, and separate default sampling policies. Structured results still pass Agent's validation/retry and artifact persistence before return. Migrated every tracked production/test import and public-export expectation; historical design documents remain historical.

Construction/chat verification: original focused Agent/chat/citation baseline returned 79 passed. Final combined Agent, single-turn chat, citation support, runtime survey, idea-generation, public API, AgentRuntime, and app-server suites returned 432 passed and 25 subtests. Added configuration identity, environment defaults, explicit overrides including absent seed, and real structured retry/artifact round-trip coverage. One earlier combined run hit an unrelated 0.1-second message-processor timeout; the affected suites reran with 34 passed, then the complete combined selection passed without modifying that test. Reviewed baseline coverage is now 17/391, plus the newly introduced chat module. Full-application acceptance, main merge/push, and task branch/worktree cleanup remain open until the whole scope is verified.

Additional reads awaiting module-wide decisions: agent `models.py`, `types.py`, and `agents/prompt_agent.py` have been read. AgentOutcome.next_context_ref still has active ThreadRuntime consumers: remove only alongside a complete context handoff migration, not as a dead field. `agent_runtime.py` and `supervisor_agent.py` are partially inspected, not completed reviews. turns/support.py, runtime/survey.py, idea_generation/pre_gate_checks.py and review_board.py have been read while migrating chat consumers; their broader module decisions remain pending. runtime/phase_runner.py and turns/ideator.py were inspected only around affected call sites. Remote execution's `mirror.py`, `mirrored.py`, and `backend.py` have been read; mirror transfer policies and the backend output-return contract require tracing through the pool/channel before deciding how to consolidate them.

Tool layer: completed core/tool.py and tool_types.py review. Removed the single-use _sync_ctx and _execute forwarding methods; ainvoke now owns result normalization, error wrapping and cancellation events together. ToolRegistry keeps one sorted mapping instead of a mapping plus mirrored list (2 attributes to 1), without adding sorting to the read path. Deleted Python ToolSpec.max_result_chars (5 fields to 4): no tracked Python consumer reads it; actual truncation remains in message handling. Rust's actively used field remains unchanged. Tool/schema boundaries remain separate to avoid pulling execution dependencies into data consumers. Baseline tool/Agent/context tests: 89 passed; expanded post-change tool/Agent/memory/session/app-server/chat tests: 402 passed and 25 subtests, including duplicate-registration atomicity and cancellation event coverage. Updated the Python tool guide accordingly.

GUI small-module review (two bounded parallel audits, integrated by parent): inspected five lib helpers and three associated tests plus eight common component/hook/style files. Retain the error-text, RPC adapter, path-display and native-dialog boundaries. workspaceStorage now shares JSON storage read/write and exception handling instead of repeating them. Delete unconsumed useEvents.ts, inline the one-use ErrorCard in MessageList, and delete ThemeToggle.module.css in favor of the identical global icon-btn rules. ErrorBoundary drops its unconsumed fallback prop (2 props to 1). Retain EmptyState and StatusBadge as genuinely shared primitives. Retain useTheme's mount-time storage read: caching only the initial import-time theme would make remounts stale. MessageList and global styles were inspected at affected sites only and remain pending for full review.

GUI evidence: worker post-change lib checks returned 17 passed (no pre-change GUI baseline claimed). Parent full GUI suite returned 186 passed across 20 files; TypeScript --noEmit and diff checks passed. New component tests cover error containment and both persisted theme transitions. Tests used a temporary junction to the existing active-quality-pass GUI dependencies, removed afterward; the dependency target was verified intact. With these reviews the corrected baseline ledger has 35/602 reviewed files, plus the new chat module and common-contracts test. Remaining files and full integration/delivery gates stay pending.

TypeScript Agent layer: completed registry/session/tool/tool-types/index/agent-types and five associated test-file reviews. Remove the factory config argument always supplied as null, MemoryView and its raw forwarding path, the unused ToolSpec/ToolOptions maxResultChars configuration, unused _noopEmit export, and private sync/error/schema forwarding helpers. ainvoke now owns normalization and cancellation/error events. ToolRegistry keeps one sorted Map instead of a Map plus mirrored list. Retain data/codec/status contracts and canonical package exports; session mailbox reads still return detached snapshots and checkpoint consumes messages.

TypeScript verification: core/agent/research builds passed before and after; Agent package tests returned 123 passed before and after the initial simplification. Added five regressions for single-argument factories, detached registry views, cancellation events, non-Error throws, and absence of the removed facade export. The full TypeScript workspace run then returned 405 passed / 2 failed. Both failures are in untouched recovery.ts/recovery.test.ts: orphan plans are removed before missing-context/workspace prerequisites can mark WAITING. The inspected recovery implementation imports ResearchState and core types, not the modified Agent layer. Track the conflict for the recovery module review rather than claiming full green acceptance. Source recovery.ts was fully read but its tests only partially inspected; neither is marked reviewed yet.

TypeScript environment: npm ci --offline rejected the existing out-of-sync workspace lock (including missing autoresearch/DeepSeek entries). npm install --offline --ignore-scripts --no-package-lock prepared local ignored dependencies without modifying manifests/lock; tests resolved this task's own workspace packages after building them. Reconcile the lock before final reproducible-install verification. Corrected baseline review coverage is now 46/602, plus the separately listed new files; all remaining scope and delivery gates remain open.

TypeScript recovery review: the earlier two failures were invalid fixtures, not a recovery-ordering defect. Missing experiment records are deliberately treated as orphan plans; missing-prerequisite tests now create a RUNNING canonical experiment. Preserve settlement/orphan removal before prerequisite checks. A separate regression exposed three durable fields reset during recovery (ideator_count, hypotheses_per_ideator, task_understanding). Reconciliation now copies the entire validated state and overrides only plans/status, preserving the input. Remove the static-only Recovery class and its two private helpers; expose reconcilePlans and migrate its Supervisor caller and package export. The recovery source shrank from 69 to 39 lines. Retain the standalone recovery boundary for independent reconciliation tests and the research index as the canonical public API.

Recovery verification: 12 recovery tests pass, including frozen configuration, non-mutating recovery, missing dependencies, orphan/terminal removal, running SEARCH, and both VALIDATE outcomes. Full TypeScript workspace: 412 passed across 53 files; core/agent/research, DSH, and autoresearch builds all pass. The regenerated lock adds the already-declared autoresearch/DeepSeek dependency graph without upgrading existing locked versions. A clean npm ci --ignore-scripts --no-audit --no-fund succeeded. No package manifests were changed. Reviewed baseline coverage is now 49/602, plus the separately tracked new files; repository-wide acceptance and main merge/push/branch/worktree cleanup remain open.

TypeScript scheduling/policy review: replace the four-field ScheduleAction class, four static factories, and duplicate ScheduleKind runtime object with a readonly discriminated union. Every action now has only kind and its actual payload (planId or count). SEARCH plan IDs are hypothesis IDs, so Supervisor no longer coalesces two nullable identities or silently skips malformed actions. Scheduler construction takes one policy instead of independently injectable policy/selector, guaranteeing one policy owns ranking, seeding, and settlement. Expose that readonly policy and remove seed/settle forwarding methods; migrate all Supervisor consumers. Inline the single-use readiness helper and share candidate generation after manual/automatic selection. Preserve recovery-first scheduling, finite creation budget, manual selection, FIFO score ties, and selection-local deduplication. Scheduler source shrank from 159 to 110 lines.

Retain policy.ts as the independently testable scoring strategy boundary. Delete queueOrder: the tracked TypeScript caller audit found only its own tests, while actual ranking uses Selector's weighted score with FIFO tie-breaking. EloPolicy retains its positive finite k and frozen-reference settlement contract, with a readonly parameter property replacing repeated declaration/assignment; source shrank from 68 to 53 lines. Removed four tests exclusively covering the deleted dead helper; added four scheduler regressions covering custom policy identity/ranking, unlimited plan resumption after budget exhaustion, phase/capacity constraints, and non-mutating selection-local deduplication. Corrected test fixture defaults so explicit null limits/best references remain null.

Scheduling verification: pre-change scheduler/ranker/policy/Supervisor selection returned 40 passed; after new regressions and before removing the dead helper's tests, 44 passed. Final full TypeScript run returned 412 passed across 53 files, including 19 scheduler and 12 policy tests. All five package builds and git diff --check passed. Tracked TypeScript searches find no remaining removed factory/enum/helper or forwarding-method consumers. Baseline reviewed coverage is now 53/602. Ranker source/tests have been fully read but deeper scoring/configuration review remains pending; runtime.ts, supervisor.ts, and DSH index were inspected at affected call sites only. Whole-repository verification, main merge/push, and deletion of this task's branch/worktree remain required and unfinished.

TypeScript ranking review: the policy boundary remains logically separate but no longer needs a separate file. Merge policy.ts into ranker.ts and delete policy.ts without a compatibility shim; migrate package exports, Scheduler, Supervisor, and all three test consumers. The two production files totalled 220 lines immediately before this change and now form one 168-line module. HypothesisPolicy.priority and EloPolicy.priority take one argument instead of two; no implementation used the tree parameter. Replace HypothesisPolicyLike with a Pick of the canonical interface. Delete the unused dedupThreshold getter, novelty export, private validation/normalization/scoring forwarding methods, and public rubricPrior export.

Rank now snapshots settled history once and precomputes one score per candidate before sorting. Deduplication tokenizes each input once; Jaccard computes union size algebraically instead of allocating a union set per comparison. Retain the scoring formula, finite-priority normalization, no-ID/equal-score behavior, FIFO ties, clipped cost, selection-local deduplication and fresh scores on subsequent calls. Constructor validation now rejects non-finite weights/thresholds; the previous comparisons silently admitted NaN. No repeated validation or persistent cache was introduced.

Ranking evidence: baseline ranker/policy/scheduler checks returned 35 passed. New regressions first exposed 14 tree snapshots for eight candidates and five missing non-finite configuration checks (6 failed / 6 passed). Final ranking tests: 21 passed, covering default/custom weighted scoring, cost clipping, empty/unregistered candidates, fresh re-ranking, finite/range validation, existing-candidate deduplication, novelty and non-mutation. A new test initially used duplicate hypothesis order zero; corrected its fixture before the final run. After removing three ignored stale policy build artifacts, all five TypeScript package builds passed and the complete workspace returned 429 passed across 53 files. Removed-module/API searches and git diff --check passed. Baseline review coverage is now 55/602; all whole-repository acceptance and main merge/push/temporary branch/worktree cleanup gates remain unfinished.

TypeScript cancellation review: all token construction, propagation, polling and cancellation callers were traced. No tracked caller used CancellationToken.wait; replace the custom token and its waiter list with native AbortSignal throughout AgentContext, provider/runtime, tool context, single-turn chat and research Worker. Callers now create an AbortController, pass controller.signal as cancel, and call controller.abort() to cancel. Pollers read signal.aborted. Move the unchanged CancelledError class into agent/types.ts and delete cancel.ts, including the custom-token public export. Existing cancellation/error separation remains intact; this change does not add transport-level abort or change where the existing runtime checks cancellation.

Cancellation evidence: Agent plus research Worker tests returned 130 passed before and after migration. Add public-surface rejection of the deleted token and a streaming regression that aborts twice after the first chunk and asserts no later text is emitted; strengthen pre-cancelled single-turn assertions to require CancelledError. After removing three ignored stale cancel build artifacts, all five package builds passed and the full TypeScript workspace returned 431 passed across 53 files. Tracked references to the removed TS token remain only in its negative API assertion and historical design records; Rust's distinct tokio token remains untouched. Source and stale compiled cancel modules are absent; git diff --check passed. Baseline review coverage is now 56/602; partially inspected provider/runtime/chat/Worker files are not counted as complete reviews. Whole-repository acceptance, main merge/push, and temporary task branch/worktree deletion remain required and unfinished.

TypeScript provider/configuration review: fully inspected provider.ts, settings.ts and both associated test files, then traced runtime, single-turn and Worker construction. Merge the live ChatClient/ProviderKind contract, provider environment selection and deferred-client fallback into provider.ts. Delete settings.ts and settings.test.ts; unused URL/model-name/configuration exports and their tests had no runtime consumers. Keep the fallback's existing missing-key and deferred-SDK errors explicit, not a claimed real SDK implementation. Callers with real transports continue to inject client. Historical architecture documents remain historical; the Rust/Python provider APIs are unchanged.

Remove OpenAIProvider/DeepSeekProvider constructor-only subclasses and the always-throwing AnthropicProvider class. createProvider now chooses one ResponsesProvider with the corresponding providerKind, rejecting Anthropic directly with the existing message. Direct construction uses new ResponsesProvider(model, { client, providerKind }); the environment-based factory remains available. BaseProvider is now a structural type interface, not an empty runtime superclass. Replace model-name getter/assignment with a readonly parameter property; preserve lazy client identity. Retain DSML filtering, tool-call assembly, schema-format fallback, API-message mapping and cancellation/error boundaries. The two production files totalled 488 lines immediately before this change and now form one 405-line provider module.

Provider evidence: Agent and Worker baseline returned 132 passed; removing obsolete wrapper/configuration tests left 128 passed, and new boundary/API regressions brought the selection to 136 passed. The provider file has 18 passing tests, including injected client without credentials, default/empty/explicit provider selection, unsupported-provider errors, lazy missing-key/deferred-client failures, native cancellation and DSML handling. Existing Agent tests retain OpenAI/DeepSeek structured output and retry coverage. After deleting three ignored stale settings build artifacts, all five package builds passed and the complete TypeScript workspace returned 435 passed across 52 files. Removed modules/imports are absent and git diff --check passed. Baseline reviewed coverage is now 60/602; full repository acceptance, main merge/push, and temporary task branch/worktree cleanup remain open.

TypeScript sampling runtime: delete nullable task placeholders, task-index assignment, filtered result arrays and result reindexing. Capture the serial dependency promise before appending each task; one async dispatcher now takes four arguments instead of six and replaces separate safe/serial closures. finalizeStep takes four arguments instead of five and directly consumes ordered Promise.allSettled results. Remove cancelToolTasks, a misleadingly named forwarding function that only awaited tasks; error cleanup now uses Promise.allSettled directly. Preserve the early hadCalls flag in sampleOnce's retry decision because a function-call emission failure must not reopen automatic retries. Existing output truncation, cancellation/error distinction, structured-output retries and artifact behavior remain unchanged. Runtime source shrank from 456 to 421 lines.

Sampling evidence: Agent plus research Worker tests returned 136 passed before and after the initial simplification. Added two controlled five-tool parallel/serial/parallel regressions, with an interleaved unknown tool and both successful/failing parallel results. Latches verify both parallel starts, each serial barrier, deliberately reversed completion order and exact call/result alignment without timing assertions. The full Agent test file returned 25 passed against both the original committed runtime and the new runtime. Final full TypeScript suite returned 437 passed across 52 files; all five package builds and git diff --check passed. Runtime constructor/configuration work remains open, so the runtime inventory row stays Pending; completing the associated Agent test-file review brings baseline coverage to 61/602, not full application acceptance.

TypeScript construction/chat review: delete createCodeAgent, a constructor-only forwarding function; known-provider tests construct Agent directly. createAgent accepts the existing AgentConfig instead of four duplicated primitive configuration fields (options: 5 fields to 2). SingleTurnChatOptions similarly replaces three sampling fields with config (10 fields to 8); validation takes three arguments instead of five. Remove the named no-op event helper. Explicit configuration is passed by identity, including name and toolChoice; default factories still use name "agent" and one-turn calls "single-turn-chat". Retain the independently consumed one-turn boundary, literal prompt handling, caller-owned memory, input validation, tool events and current-turn-only text extraction. Worker.runText requires no argument migration because it uses defaults.

Construction evidence: Agent plus Worker selection returned 138 passed before and after the initial migration. Four new regressions cover independent factory defaults, configuration identity, default/explicit sampling including zero temperature and required tool choice, and one/two-turn budgets. All five TypeScript package builds passed. The first full suite returned 440 passed and one 5-second timeout in the unchanged Git workspace suite; a full rerun with unchanged timeout and implementation returned 441 passed across 52 files. Removed createCodeAgent references remain only in the negative public API assertion; git diff --check passed. Source/test single-turn reviews bring baseline coverage to 63/602. This is not full application acceptance or evidence of real external LLM transport readiness.

TypeScript adapter review: the complete runtime was reread and all tracked caller references traced. Neither agentRunner nor its callable-property runWithContext API has a production consumer; delete the entire adapter, AgentRunnerFn type and factory-binding closure instead of preserving an unused compatibility signature. Production callers already use Agent.run(context). Migrate all three adapter tests to direct context calls, retaining context identity, tool invocation and bound askUser behavior assertions; the public API test now rejects the removed export. Runtime shrinks from 404 to 352 lines. Retain the runtime module as the owner of streaming, serial/parallel tools, retry and structured-output persistence. This completes its baseline review, bringing coverage to 64/602, not whole-application acceptance.

Adapter evidence: the pre-change Agent/Worker selection returned 142 passed. All five package builds passed after removal. The default full suite returned 440 passed and one 5-second timeout in the unchanged real-Git workspace test. A fresh full run with the command-only override `npm test -- --testTimeout=30000` returned 441 passed across 52 files; no test configuration or timeout was committed. This verifies functional assertions, not default-timeout reliability. Removed adapter references remain only in the negative public API assertion; git diff --check passed.

TypeScript model review: fully read models.ts and trace every tracked outcome, step, call and context construction. Remove AgentOutcome.nextContextRef: it was generated but never read in production, unlike the independently used Python contract. Remove AgentContext.messages and its unused import; context constructor/attributes shrink from nine to eight. Real mailbox consumption remains RunSession's responsibility, and conversation history remains in ContextManager. Correct the stale context comment: Agent creates memory when none is supplied.

Replace three behavior-free classes (AgentOutcome, StepOutcome, ToolCall) with exported structural types, removing their positional constructors and runtime exports. AgentOutcome is now exactly resultRef; StepOutcome is a discriminated union whose continue branch has no empty text field. Runtime uses plain objects and tests reject the removed constructors. Retain AgentConfig/AgentContext defaulting behavior and the shared models module as the runtime/provider contract boundary. No compatibility classes or field aliases remain. Baseline reviewed coverage is now 65/602.

Model evidence: focused Agent/Worker baseline returned 142 passed; after the new public-surface regression and stronger result/context shape assertions, 143 passed. All five package builds passed. Full TypeScript verification with `npm test -- --testTimeout=30000` returned 442 passed across 52 files; this command-only timeout override remains explicit because default-timeout reliability is unresolved. Tracked source searches find no removed field or constructor consumers, and git diff --check passed. Existing structured artifact/retry, tool sequencing, memory reuse, native cancellation and askUser tests remain in the passing selection.

TypeScript Worker review: read the full source/test and trace ResearchRuntime's structured, plan-decision and ideator consumers. No tracked caller uses WorkerRunner.runText; delete it rather than adding another shared chat abstraction. Text callers can use the existing singleTurnChat API. Worker now owns store plus provider instead of store/model/client (three attributes to two), constructing one reusable ResponsesProvider instead of rebuilding it each turn. Provider selection and unsupported-provider validation now occur at Worker construction; injected transport is reused and lazy SDK client resolution still occurs only when streaming needs it. Reconfiguration requires a new Worker; mutable environment changes no longer switch an existing Worker's provider.

Retain the structured Worker boundary: fresh thread/turn identities, literal input, optional caller memory/tools, schema validation/retries and artifact round-trip differ from text extraction. Keep planDecision's null-on-failure contract because ResearchRuntime consumes it. Retain the Zod adapter and shared output definitions; runtime still needs the parsed result contract. Source shrinks from 125 to 107 lines. New OpenAI/DeepSeek cases verify provider snapshot, fresh default history, explicit memory reuse, one system prompt and literal artifact-like input. The first new assertion omitted DeepSeek's existing schema instruction; corrected the expected requests without removing that behavior. Baseline reviewed coverage is now 67/602.

Worker evidence: Agent/Worker baseline returned 143 passed. Final Worker tests returned four passed, and all five package builds passed. After correcting the new DeepSeek request expectation, the full TypeScript workspace returned 444 passed across 52 files with `npm test -- --testTimeout=30000`; no timeout configuration was committed. Tracked searches find no runText callers, and git diff --check passed.

TypeScript entrypoint review supersedes the preceding Worker retention decision. Full runtime read plus repository-wide tracked caller tracing finds no TypeScript ResearchRuntime constructor/import consumer beyond its own package export. The live DSH plugin directly composes FixedFlowSupervisor and services; AutoResearchRuntime has its own plugin/scripts and headless integration. Python's identically named, actively used ResearchRuntime is separate and unchanged. Delete the unused TypeScript runtime.ts rather than polishing its 21 attributes, unconsumed settings/tree facades and duplicate orchestration adapters.

Worker and shell are exclusively reachable through that unused root (apart from exports and Worker-only tests), so delete worker.ts, shell.ts and worker.test.ts together, removing all seven runtime exports and both option type exports. This intentionally removes an unused library surface, not just private implementation details. Earlier Worker refinements remain historical evidence, not a reason to preserve unreachable code. Remove nine explicitly scoped ignored JS/map/declaration build artifacts so stale modules cannot remain loadable. Deleted sources are recoverable from Git history. Keep @athena/agent dependency because execution/experiment still import its event type. Add a new public-surface regression checking removal and the canonical services still used by DSH.

Deletion evidence: pre-change research/DSH/headless integration selection returned 192 passed. After deleting the four Worker-only cases and adding two public-surface cases, all five package builds passed and the full workspace returned 442 passed across 52 files with `npm test -- --testTimeout=30000`. The actual DSH service/tool tests and AutoResearch headless integration remain passing. Removed-module searches find only negative public API assertions; all nine stale compiled artifacts remain absent after rebuilding, and git diff --check passed. No claim of real external model execution or default-timeout reliability is made.

The source/export deletion removes 532 production lines plus the 89-line Worker-only test; the replacement public-surface test has two cases. Baseline coverage at that checkpoint is 69/602 plus separately tracked new files.

TypeScript execution review: fully inspect execution.ts and trace every execution and PlanRunner construction. PlanRunner already supplies branch.path as workdir; no real executor consumes project/environment/experiment fields from ExecutionContext. Delete the four-field context, empty ensureEnvironment and LocalExecutionRuntime's two root attributes/constructor arguments. ExecutionRuntime now contains only run(CommandOptions), replacing repeated option declarations and two-argument dispatch. Require workdir in that single options object. PlanRunner drops its context attribute/constructor argument (eight to seven); prepare's factory takes branch only (two to one), and both DSH construction paths stop rebuilding unused context dictionaries.

CommandResult drops three never-consumed placeholders (error, truncated, output_ref), reducing fields/constructor arguments from seven to four. Preserve explicit success, stdout/stderr and numeric exit status; retain the shared execution boundary and injectable run protocol. No shell parsing, environment setup, sandboxing or output truncation is added. Missing argv, command-start event ordering, child-process timeout and error normalization remain unchanged.

Execution evidence: research/DSH tests returned 189 passed before and after migration. Six new real-child-process cases cover different working directories on one stateless executor, literal argv with spaces/metacharacters, separate stdout/stderr, nonzero exit, empty argv without events, missing executable with start event, and timeout failure. The focused new file returned six passed. All five package builds passed; full workspace verification returned 448 passed across 53 files with `npm test -- --testTimeout=30000`. Removed context/root/environment helper searches and git diff --check passed. Baseline coverage is now 70/602 plus separately tracked new files.

TypeScript prepare review: fully read prepare.ts and its two test files, then trace DSH's evaluator/prepare calls. Delete planRunnerFactory, whose only override was a test fabricating a scored outcome; options shrink from eleven fields to ten. Remove its duplicate runner protocol and forwarding closure. Production still constructs a fresh PlanRunner at the same point in each attempted turn. Preserve separate evaluator and baseline loops because their freeze/output/fatal-error policies differ; keep stable workspace persistence, label/entrypoint checks, feedback retries, abandonment and trusted-result validation. Do not merge these policies into a generic retry framework.

Replace the factory-based test with two cases using the real PlanRunner, manifests, content-addressed artifact storage and fake external execution/scorer/Git ports. Verify command/workspace propagation, one committed successful result, report/evidence round-trip and retry feedback following a failed command. Add five freeze regressions for malformed JSON, non-string eval_script, missing file, missing directory entrypoint and nested labels. The baseline focused prepare/PlanRunner/DSH selection returned 41 passed; the first real-pipeline selection returned four passed.

Prepare verification: all five package builds passed; final full workspace run returned 454 passed across 53 files with `npm test -- --testTimeout=30000`. The two prepare files contribute twelve passing tests, including the real pipeline and freeze cases. No remaining tracked planRunnerFactory references were found, and git diff --check passed. The command-only timeout override does not establish default-timeout reliability.

Prepare source/test reviews bring baseline coverage at that checkpoint to 73/602.

TypeScript PlanRunner review: complete source and test-file reads, trace prepare and both DSH constructors plus settlement consumers. No caller supplies a custom timeout; remove the timeoutS attribute/constructor parameter (seven to six), retaining the explicit 120-second command limit for every injected executor. Clean failure text only at failure(), the shared evidence/result boundary; delete four redundant upstream cleanError calls while retaining whitespace normalization, redaction and length limiting. Combine identical artifact-read and parse failure catches in loadBundle.

Share hasAnyFile between experiment and prepare instead of maintaining two recursive implementations. Keep it out of the package barrel; prepare already depends on the experiment module, so this introduces no new cycle or helper file. Preserve missing/unreadable directory handling, skipped failed stat calls, nested traversal and acceptance of zero-byte files. Retain manifest validation, trusted score/state updates, settlement policy and scorer/storage/execution boundaries; no business-policy rewrite is implied by this cleanup.

The pre-change PlanRunner/prepare/Supervisor/DSH selection returned 52 passed. Four new cases cover recursive directory presence and missing/malformed/schema-invalid frozen evaluator artifacts; existing fake execution now asserts the unchanged 120-second limit. All five package builds passed, and the full workspace returned 458 passed across 53 files with `npm test -- --testTimeout=30000`, including 35 PlanRunner/manifest/settlement tests. Removed helper/configuration searches and git diff --check passed; default-timeout reliability is not claimed. Baseline file-review coverage is now 75/602. Next module: inspect script_runner.ts and evaluation.ts, their data contracts and active callers; DSH remains reviewed only at affected call sites. Whole-repository verification, main merge/push, and task branch/worktree removal remain mandatory and unfinished.

Settlement follow-up corrects the preceding retention statement: the Supervisor invokes its own private decideSettlement, not the similarly named export from experiment.ts. Delete the unused exported function, PlanSettlement type, eleven exclusive tests and their decision helper. Keep the actually used Supervisor implementation unchanged, including its two-field result; do not introduce the dead function's reason strings or PREPARE report gate into that path. PREPARE still validates its required report in its real execution pipeline. The package API regression rejects the removed export.

Add seven policy cases against the actual Supervisor method for submit, abandon, patience exhaustion, exhausted turns with/without a best result, remaining budget and unlimited turns. These complement the existing end-to-end SEARCH completion test; they are method-level tests, not new end-to-end cases. Baseline PlanRunner/Supervisor/DSH selection returned 44 passed. Review count remains 75/602: this is a correction to an already reviewed module, not another completed file. script_runner.ts has now been fully read, but its caller/test and implementation review remains Pending. Full repository acceptance, main merge/push and temporary branch/worktree cleanup remain unfinished.

Settlement verification: all five package builds passed. Full TypeScript workspace returned 454 passed across 53 files with `npm test -- --testTimeout=30000` (458 minus eleven dead-function cases plus seven active-policy cases). References now resolve only to the Supervisor method, its new tests and the negative package-export assertion; PlanSettlement is absent. git diff --check passed. Default-timeout reliability and full application readiness remain unproven.

Script/scoring interface review: trace freeze through prepare and run through TrustedEvaluator to PlanRunner/DSH. Delete BundleMetadata (description has no reader), ScriptRunResult (only outputs was consumed), and the public command-building method used only internally. freeze now accepts the entrypoint string; run returns its parsed output directly. Remove the unreferenced per-run result artifact write, while preserving frozen tree/source/project/lock artifacts and PlanRunner's independently persisted scoring evidence. Consolidate both subprocess helpers into one stdout-returning implementation, preserving argv, timeout, buffer limits and file-before-stdout output selection.

TrustedEvaluator retains its separate scoring boundary and single runner dependency. Derive EvaluatorRunner from the canonical run method and score options from Scorer instead of duplicating declarations. Remove the redundant boolean check and unreachable Number conversion catch after the string/number guard; retain missing/non-scalar/non-finite rejection, numeric strings, candidate identity, direction and ScoringError normalization. Test doubles use structural objects instead of a forwarding class. Full source/test reads and caller tracing close evaluation.ts and evaluation.test.ts, bringing baseline coverage to 77/602.

New script tests exercise real filesystem/artifact round-trips with a mocked subprocess boundary: frozen metadata, nested binary files, restored prediction files, result-file/stdout parsing, missing fields and unfrozen bundles (six cases). Four added evaluator cases cover missing scores, Error/string failures and prediction/identity/direction forwarding. The focused combined selection passed thirteen tests; all five TypeScript package builds passed. These are not real uv/Python execution or strong-isolation evidence. script_runner.ts remains Pending for deeper freeze snapshot consistency, traversal and file-injection boundary review; its four-argument run contract is not yet declared final. Whole-repository acceptance, main merge/push and task branch/worktree cleanup remain mandatory and unfinished.

Script/scoring verification: final full TypeScript workspace returned 464 passed across 54 files with `npm test -- --testTimeout=30000`. All five package builds and git diff --check passed. Removed wrapper/helper references remain only in the negative package-export assertion. The timeout override is command-only; default-timeout reliability remains unproven. No real external evaluator/model run or whole-application readiness is claimed.

Script lifecycle follow-up: complete the script_runner.ts review and trace every tracked constructor/freeze/run and directory-pack consumer. run now takes bundle plus optional extraFiles (four arguments to two); remove the always-empty request input and score-specific outputSchema. The protocol still writes an empty request.json. TrustedEvaluator alone validates primary; the runner only parses a JSON object, preferring result.json over the last nonblank stdout line. Invalid JSON and non-object output are rejected consistently. No compatibility overload remains.

Freeze reuses the entrypoint reference from its captured tree instead of rereading/storing the source after uv runs. Normalize and require an actual captured entrypoint; skip the root virtual environment before traversing it, and sort the complete file list once. Exclude stale request.json/result.json from snapshots so a previous evaluation cannot become a new result. Retain bundle metadata and the two store/workdir dependencies; environment metadata remains provenance, not an interpreter sandbox.

Restore the tree through existing loadDirectory and use one file-writing loop for frozen and injected files. Resolve paths within the fresh run directory, reject absolute/outside paths, reserve the output filename and use exclusive creation to prevent injections from overwriting frozen source/labels/dependency/request files. These are file-staging checks, not process isolation: generated Python still executes locally with the user's permissions. No generalized security framework or cleanup policy is introduced.

Evidence: pre-change script/evaluator/prepare selection passed 25 tests. The updated script file has 29 passing cases and the combined script/evaluator selection has 36, covering subprocess-time source mutation, stale output exclusion, invalid entrypoints, injected collisions, escaping/absolute paths, output shape and file precedence. Five package builds passed. An explicit offline real-uv smoke check also passed: freeze a dependency-free Python evaluator, mutate the draft, restore the frozen source/labels, inject predictions and obtain the trusted score 0.25. Reproduce after building with `node packages/athena-research/test/script-runner.uv-smoke.mjs` from athena_ts; requires uv and an installed Python >= 3.11, disables downloads, and removes its own temporary fixture. This smoke is separate from the default Vitest suite.

Lifecycle final verification: all five builds passed; the full TypeScript workspace passed 487 tests across 54 files with `npm test -- --testTimeout=30000`, plus the separate real-uv smoke above. git diff --check passed. No removed outputSchema/validateSchema consumers remain in the research package. The command-only timeout override does not establish default-timeout reliability.

Closing script_runner.ts brings baseline review coverage to 78/602; new test/smoke files are tracked separately below. Next review: research contracts, validation and reporting consumers. Whole-repository acceptance, main merge/push and task branch/worktree removal remain mandatory and unfinished.

Validation/report review: fully read both validation modules, report.ts and their three test files; trace DSH's runValidationPlan and report-tool calls plus Supervisor validation/report persistence. ValidationService has no state and only one production construction site. Merge result construction into runValidationPlan; delete src/validation.ts, its public class/gap/warning exports, the private isClose helper and unused configurable tolerances. Remove the supervisor module's redundant schema reexport; the schema remains available from contracts and the canonical package barrel.

Preserve both metric directions, gap sign, relative/absolute tie semantics and warning-with-COMPLETED behavior. The comparison against zero is expressed directly without adding a helper or service object. Preserve existing decision/scoring order, invalid-decision feedback, scorer retry, abandon and budget-exhaustion behavior. Delete the old five helper/service tests and move numeric coverage to the real runValidationPlan entrypoint: fourteen cases now cover directions, noise and exact threshold, valid completion, retries, continue feedback, invalid decisions, abandon and zero/exhausted budgets. The orchestration boundary remains one five-field options object used by DSH; no compatibility class remains.

Retain report.ts as the deterministic, shared Supervisor/DSH renderer, with two inputs and no object state. Remove fmt's unused precision parameter (two arguments to one), retaining four-decimal formatting and legacy text values. Five report cases cover empty trees/validation, zero scores, formatting, pending-versus-executed hypotheses, validation warning and repeated non-mutating rendering. The baseline validation/report/Supervisor/DSH selection passed 25 tests; after consolidation it passed 35. All five package builds passed. Removed exactly three ignored dist/validation JS/map/declaration files to prevent stale deep imports; sources and tests remain recoverable from Git history.

Validation/report final evidence: full TypeScript workspace passed 497 tests across 53 files with `npm test -- --testTimeout=30000`; all five builds and git diff --check passed. Removed symbols remain only in negative public API assertions, and obsolete source/test/compiled files are absent. The timeout override is command-only; default-timeout reliability and whole-application readiness are not claimed.

These six baseline file reviews bring coverage to 84/602. contracts.ts has been read but its cross-consumer field review remains Pending. Whole-repository acceptance, main merge/push and task branch/worktree removal remain mandatory and unfinished.

Research artifact contract review supersedes the earlier candidate identity/direction retention decision. Full contracts.ts read and score-consumer tracing show PlanRunner consumes only test_score; candidate_id/direction were passed into the scorer only to be returned unread, while both kfold fields always defaulted to null. Delete CandidateEvaluationSchema/type entirely. Scorer.score now returns a number and its options shrink from five fields to three (bundle, predictions and prediction root). PlanRunner keeps its existing plan identity, optimization direction, evidence persistence and trusted-score update; neither belongs in a redundant score envelope. Migrate all test doubles and the real-uv smoke. Numeric-string conversion and missing/non-scalar/non-finite rejection remain in TrustedEvaluator.

ValidationResult drops five fields with no production writer or reader: sota_commit, validation_commit, predictions_ref, predictions_path and evidence_ref. Results now contain six actual conclusion fields. Supervisor still persists its independently created report_ref alongside them; historical state files are not rewritten. Legacy null placeholders are ignored by the existing schema projection when supplied. Retain DataScriptBundle's freeze refs and provenance metadata, its incomplete defaults for explicit freeze checks, the validation status/default semantics and the shared contracts module; do not split schemas into additional files.

Baseline score/PlanRunner/prepare/validation/Supervisor checks returned 61 passed before and after migration. Three new contract cases verify bundle defaults, metadata JSON round-trip and legacy validation projection. Strengthen the actual Supervisor validation test to verify exactly six result fields plus report_ref, state reload equality and readable report content. All five package builds passed, and the explicit offline real-uv smoke returned the same scalar 0.25. New contract plus Supervisor tests returned 15 passed.

Artifact final verification: all five builds passed; the full TypeScript workspace passed 500 tests across 54 files with `npm test -- --testTimeout=30000`, and the separate real-uv smoke passed. Removed score-envelope references remain only in the negative API assertion; removed validation fields appear only in the legacy-projection test. git diff --check passed. Default-timeout reliability and whole-application acceptance remain unproven.

Completing contracts.ts brings baseline coverage to 85/602; contracts.test.ts is tracked separately as New. state.ts and plans.ts have been fully read but caller/serialization and test review remain Pending. Their existing state fields are not assumed dead merely because this artifact slice removed other placeholders. Whole-repository acceptance, main merge/push and temporary task branch/worktree cleanup remain mandatory and unfinished.

Research state review: read state.ts/state.test.ts fully and trace DSH initialization, prepare persistence, Supervisor publication/save, recovery cloning and scheduler consumers. All eleven durable fields remain used; retain their names, defaults, constraints and plan-key rules. Delete the ResearchState runtime class, eleven repeated property declarations and eleven constructor assignments. ResearchState is now inferred from the existing schema; ResearchStatus derives from that type and the unused ResearchPhase alias is removed. Expose parseResearchState, loadResearchState, saveResearchState and researchStateToJSON rather than construction/static/instance methods.

Load validates once through the shared parse function instead of parse followed by constructor revalidation, retaining AthenaValidationError normalization and the explicit non-object-root error. Keep exact durable-field projection and Plan serialization instead of persisting arbitrary properties from a spread. Preserve mutable state ownership, independent schema defaults, detached plan snapshots and the existing atomic writer/failure cleanup. No save-time validation layer or additional state wrapper is added. Migrate every live DSH/Supervisor/prepare/recovery caller and associated tests; package tests reject the removed runtime export. State source shrinks from 111 to 80 lines.

The baseline state/Plan/recovery selection returned 69 passed. After three regressions for plain data/default independence, a single parsing pass and exact durable projection, state/Plan/recovery/DSH returned 76 passed. All five package builds passed. The first full run exposed a missed scheduler test alias (s.toJSON); migrate both occurrences to researchStateToJSON and verify all nineteen scheduler tests. DSH's JSON.stringify(state) in its agent prompt uses PlanState, not the removed ResearchState class, and remains unchanged.

State final verification: the first full run finished with 502 passed and the one missed test migration above. After correction, a fresh full run passed 503 tests across 54 files with `npm test -- --testTimeout=30000`. All five builds and git diff --check passed. No removed constructor/static/instance state APIs remain in tracked package callers. The explicit timeout override does not prove default-timeout reliability or whole-application readiness.

Closing state source/test brings baseline review coverage to 87/602. plans.ts and plans.test.ts have now been fully read, but the broader PlanInput/PlanBest/decision consumer and serialization simplification review remains Pending. Whole-repository acceptance, main merge/push and this task's temporary branch/worktree removal remain mandatory and unfinished.

Plan contract review: complete source/test reads and trace creation, prompt projection, scoring/settlement, best-artifact loading and state serialization. Merge supervisor/plans.ts into the existing contracts.ts and delete the old module without a forwarding file. Remove unused PlanKind and the single-use schema alias. Consolidate the three identical SEARCH-only validation branches into one field loop, preserving issue order/count and errors. Keep strict hypothesis snapshots, finite best metrics, trusted reference validation, required SEARCH patience and existing counter/default rules.

PlanInput now emits nine fields instead of twelve: remove active_ancestor_hypotheses, initial_turn_limit and initial_patience, which had writers but no production reader. Stop computing the discarded ancestor array and remove the duplicate limit writes from prepare/Supervisor. Retain tree_ref as the frozen tree artifact provenance and the fields actually used by scoring, ranking and agent prompt projection. Existing content-addressed inputs are not rewritten: preprocessing removes only these three known legacy keys before strict validation. All other unknown keys still fail. This narrow read migration is required for old context_ref artifacts to remain resumable, not a retained constructor/API facade.

PlanDecision drops unused suggestions (three output fields to two), including DSH's corresponding JSON-schema property. Move planStateToJSON into state.ts as a private persistence helper; share its common fields instead of copying them in both branches. The package no longer exports that helper. Remove its three direct tests because existing state persistence tests cover both plan shapes; add omitted SEARCH-default projection coverage. Replace tests for deleted initial-limit fields with three legacy-read cases and unknown-field rejection. Strengthen the real Supervisor context test to read an old artifact through planInput while preserving both its ref and original bytes.

The research/DSH baseline passed 250 tests; the final focused selection passed 249, including 36 Plan contract tests and 23 state tests. All five package builds passed. Removed exactly three ignored dist/supervisor/plans JS/map/declaration artifacts; the old source remains recoverable from Git history. Removed-module searches find no remaining imports. Baseline coverage is now 89/602 after closing plans.ts and plans.test.ts. The larger Supervisor/DSH files remain reviewed only at affected sites; next full module review is the event projection boundary. Whole-repository acceptance, main merge/push and temporary task branch/worktree cleanup remain mandatory and unfinished.

Plan final verification: all five builds passed; full TypeScript workspace passed 502 tests across 54 files with `npm test -- --testTimeout=30000`. The one-test net decrease reflects removal/replacement of obsolete helper/field cases, not skipping failures. git diff --check passed; obsolete module imports and all three compiled files are absent. Default-timeout reliability and whole-application readiness remain unproven.

Event module review: fully read events.ts and its tests, then search the tracked TypeScript implementation for every export and constructor. EventProjector, its unused store constructor argument/sequence state, both event schemas/types, sanitizeTerminalText and truncateMiddle have no production consumers. Actual Supervisor/DSH publication does not use this adapter. Delete the entire events.ts module and its eight exclusive tests rather than refactoring the unconnected projector. Remove all corresponding package exports, with negative public API coverage.

The only live function was redact, called exclusively by PlanRunner.cleanError. Inline its two ordered replacements there, removing the regex/options container and loop. Preserve whitespace normalization, both existing secret patterns, replacement strings and the final 1000-character limit. This does not add comprehensive secret detection or terminal sanitization to the application's actual event stream, and the removed module never provided those guarantees there. Actual event publication remains unchanged.

Expand the real PlanRunner failure tests to cover eight secret syntax combinations, whitespace/length behavior and execution failures, checking persisted evidence equals the returned redacted error. There are now 33 PlanRunner tests. Baseline event/PlanRunner/Supervisor/DSH checks passed 48 tests; after deleting eight obsolete cases and adding nine live-boundary cases the focused selection passed 49. That first selection emitted a stale-source-map warning from the old compiled event module; remove exactly its three ignored JS/map/declaration files and rebuild all five packages successfully. Source/test deletions remain recoverable from Git history.

Event final evidence: all five builds passed. After rebuilding without stale event artifacts, the full TypeScript workspace passed 503 tests across 53 files with `npm test -- --testTimeout=30000`. Removed symbols remain only in negative API assertions; source/declaration/map removals and git diff --check are verified. Default-timeout reliability, comprehensive secret/terminal filtering and whole-application readiness are not claimed.

Closing the event source/test reviews brings baseline coverage to 91/602. Next full module review: Supervisor scheduling and task ownership; its earlier scoped edits do not count as a completed full-file review. Whole-repository acceptance, main merge/push and task branch/worktree cleanup remain mandatory and unfinished.

| File | Baseline lines | Review |
| --- | ---: | --- |
| `athena_ts/packages/athena-research/test/contracts.test.ts` | New | Reviewed; three bundle-default/provenance/validation-projection cases |
| `athena_ts/packages/athena-research/test/script-runner.test.ts` | New | Reviewed; 29 snapshot/staging/output cases with mocked subprocesses |
| `athena_ts/packages/athena-research/test/script-runner.uv-smoke.mjs` | New | Reviewed; explicit offline real-uv freeze/restore/trusted-score smoke passes |
| `athena_ts/packages/athena-research/test/execution.test.ts` | New | Reviewed; six real-process command/workdir/event/error/timeout cases |
| `athena_ts/packages/athena-research/test/public-api.test.ts` | New | Reviewed; removed legacy root/adapter exports and retained canonical DSH services |
| `athena-gui/src/components/__tests__/common-contracts.test.tsx` | New | Reviewed; verifies error boundary and persisted theme transitions |
| `athena-gui/scripts/dev-backend.cjs` | 38 | Reviewed; merge duplicate gateway launcher into dev.cjs and delete file |
| `athena-gui/scripts/dev-web.cjs` | 46 | Reviewed; merge duplicate dual-process launcher into dev.cjs and delete file |
| `athena-gui/scripts/dev.cjs` | New | Reviewed; one backend/preview launcher with shared monitoring and shutdown |
| `athena-gui/scripts/ensure-generated.mjs` | 15 | Reviewed; retain the minimal build-directory precondition |
| `athena-gui/src-tauri/src/commands/chat.rs` | 12 | Reviewed; merge pure command into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/clarification.rs` | 270 | Reviewed; retain typed reply boundary and delete test-only request mirror |
| `athena-gui/src-tauri/src/commands/dialog.rs` | 103 | Reviewed; retain native fallback selection and its three tests |
| `athena-gui/src-tauri/src/commands/experiments.rs` | 47 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/graph.rs` | 27 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/mod.rs` | 11 | Reviewed; own the flat RPC-forwarding command surface |
| `athena-gui/src-tauri/src/commands/research.rs` | 32 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/search.rs` | 42 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/settings.rs` | 27 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/state.rs` | 53 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/traces.rs` | 19 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/commands/validate.rs` | 18 | Reviewed; merge pure commands into commands/mod.rs and delete file |
| `athena-gui/src-tauri/src/events.rs` | 34 | Reviewed; inline the single-consumer relay into lib.rs and delete file |
| `athena-gui/src-tauri/src/lib.rs` | 66 | Reviewed; own setup, event relay and flat command registration |
| `athena-gui/src-tauri/src/main.rs` | 6 | Reviewed; retain the minimal platform entrypoint |
| `athena-gui/src-tauri/src/python/bridge.rs` | 218 | Reviewed; merge transport, wire types and tests into python.rs |
| `athena-gui/src-tauri/src/python/mod.rs` | 2 | Reviewed; remove the two-layer facade and delete file |
| `athena-gui/src-tauri/src/python/types.rs` | 92 | Reviewed; merge live wire contracts into python.rs and delete file |
| `athena-gui/src-tauri/src/python.rs` | New | Reviewed; one four-field process/WebSocket bridge and wire boundary |
| `athena-gui/src/App.tsx` | 49 | Pending |
| `athena-gui/src/__tests__/App.test.tsx` | 164 | Pending |
| `athena-gui/src/components/AlgorithmsPanel.tsx` | 127 | Pending |
| `athena-gui/src/components/DiffViewer.tsx` | 77 | Pending |
| `athena-gui/src/components/EDAPreview.module.css` | 111 | Pending |
| `athena-gui/src/components/EDAPreview.tsx` | 68 | Pending |
| `athena-gui/src/components/ExperimentLog.tsx` | 73 | Pending |
| `athena-gui/src/components/ExperimentManager.tsx` | 303 | Pending |
| `athena-gui/src/components/FileTree.tsx` | 68 | Pending |
| `athena-gui/src/components/HypothesisGraphViz.module.css` | 39 | Pending |
| `athena-gui/src/components/HypothesisGraphViz.tsx` | 210 | Pending |
| `athena-gui/src/components/LLMIOPanel.module.css` | 68 | Pending |
| `athena-gui/src/components/LLMIOPanel.tsx` | 144 | Pending |
| `athena-gui/src/components/MetricChart.tsx` | 137 | Pending |
| `athena-gui/src/components/ReportPanel.module.css` | 25 | Pending |
| `athena-gui/src/components/ReportPanel.tsx` | 114 | Pending |
| `athena-gui/src/components/ResearchTreeViz.tsx` | 289 | Pending |
| `athena-gui/src/components/SettingsPanel.module.css` | 165 | Pending |
| `athena-gui/src/components/SettingsPanel.tsx` | 506 | Pending |
| `athena-gui/src/components/__tests__/SettingsPanel.test.tsx` | 82 | Pending |
| `athena-gui/src/components/__tests__/app-shell.test.tsx` | 300 | Pending |
| `athena-gui/src/components/__tests__/conversation-pane.test.tsx` | 649 | Pending |
| `athena-gui/src/components/__tests__/function-rail.test.tsx` | 24 | Pending |
| `athena-gui/src/components/__tests__/research-tree-viz.test.tsx` | 291 | Pending |
| `athena-gui/src/components/cards/ErrorCard.tsx` | 13 | Reviewed; inline single consumer markup in MessageList and delete |
| `athena-gui/src/components/cards/IntentPreviewCard.module.css` | 245 | Pending |
| `athena-gui/src/components/cards/IntentPreviewCard.tsx` | 250 | Pending |
| `athena-gui/src/components/cards/__tests__/IntentPreviewCard.test.tsx` | 117 | Pending |
| `athena-gui/src/components/common/EmptyState.tsx` | 18 | Reviewed; retain shared two-prop primitive used by 12 consumers |
| `athena-gui/src/components/common/ErrorBoundary.tsx` | 33 | Reviewed; remove unused fallback prop; error containment test passes |
| `athena-gui/src/components/common/Icon.tsx` | 166 | Pending |
| `athena-gui/src/components/common/StatusBadge.tsx` | 13 | Reviewed; retain shared status display contract |
| `athena-gui/src/components/context/panels.ts` | 45 | Pending |
| `athena-gui/src/components/conversation/Composer.module.css` | 68 | Pending |
| `athena-gui/src/components/conversation/Composer.tsx` | 40 | Pending |
| `athena-gui/src/components/conversation/ConversationPane.module.css` | 6 | Pending |
| `athena-gui/src/components/conversation/ConversationPane.tsx` | 66 | Pending |
| `athena-gui/src/components/conversation/MessageList.module.css` | 205 | Pending |
| `athena-gui/src/components/conversation/MessageList.tsx` | 325 | Pending |
| `athena-gui/src/components/conversation/PendingHypotheses.module.css` | 34 | Pending |
| `athena-gui/src/components/conversation/PendingHypotheses.tsx` | 27 | Pending |
| `athena-gui/src/components/conversation/RunControls.module.css` | 66 | Pending |
| `athena-gui/src/components/conversation/RunControls.tsx` | 66 | Pending |
| `athena-gui/src/components/conversation/WelcomeHero.module.css` | 78 | Pending |
| `athena-gui/src/components/conversation/WelcomeHero.tsx` | 36 | Pending |
| `athena-gui/src/components/shell/AppShell.module.css` | 131 | Pending |
| `athena-gui/src/components/shell/AppShell.tsx` | 158 | Pending |
| `athena-gui/src/components/shell/AthenaWordmark.tsx` | 24 | Pending |
| `athena-gui/src/components/shell/ContextDrawer.module.css` | 75 | Pending |
| `athena-gui/src/components/shell/ContextDrawer.tsx` | 36 | Pending |
| `athena-gui/src/components/shell/ContextSidebar.module.css` | 235 | Pending |
| `athena-gui/src/components/shell/ContextSidebar.tsx` | 196 | Pending |
| `athena-gui/src/components/shell/FunctionRail.module.css` | 81 | Pending |
| `athena-gui/src/components/shell/FunctionRail.tsx` | 50 | Pending |
| `athena-gui/src/components/shell/HumanRequestDialog.module.css` | 90 | Pending |
| `athena-gui/src/components/shell/HumanRequestDialog.tsx` | 231 | Pending |
| `athena-gui/src/components/shell/LogDrawer.module.css` | 160 | Pending |
| `athena-gui/src/components/shell/LogDrawer.tsx` | 65 | Pending |
| `athena-gui/src/components/shell/ThemeToggle.module.css` | 19 | Reviewed; identical global icon-btn rules exist; delete duplicate |
| `athena-gui/src/components/shell/ThemeToggle.tsx` | 25 | Reviewed; use global icon-btn style; both theme transitions tested |
| `athena-gui/src/components/shell/WorkspacePicker.module.css` | 123 | Pending |
| `athena-gui/src/components/shell/WorkspacePicker.tsx` | 119 | Pending |
| `athena-gui/src/components/shell/WorkspaceSwitcher.module.css` | 33 | Pending |
| `athena-gui/src/components/shell/WorkspaceSwitcher.tsx` | 20 | Pending |
| `athena-gui/src/components/shell/__tests__/HumanRequestDialog.test.tsx` | 128 | Pending |
| `athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx` | 100 | Pending |
| `athena-gui/src/components/shell/navigation.ts` | 69 | Pending |
| `athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx` | 856 | Pending |
| `athena-gui/src/hooks/__tests__/usePipeline.identity.test.tsx` | 696 | Pending |
| `athena-gui/src/hooks/__tests__/usePipeline.test.tsx` | 1529 | Pending |
| `athena-gui/src/hooks/__tests__/useWorkspace.test.tsx` | 358 | Pending |
| `athena-gui/src/hooks/useEvents.ts` | 26 | Reviewed; no source/test callers; delete unused event window |
| `athena-gui/src/hooks/usePipeline.ts` | 1708 | Pending |
| `athena-gui/src/hooks/useTheme.ts` | 40 | Reviewed; retain initial paint side effect and mount-time persisted preference |
| `athena-gui/src/hooks/useWorkspace.ts` | 174 | Pending |
| `athena-gui/src/lib/__tests__/graphLayout.test.ts` | 46 | Pending |
| `athena-gui/src/lib/__tests__/rpc-error.test.ts` | 31 | Reviewed; retain RPC error mapping checks; tests pass |
| `athena-gui/src/lib/__tests__/tauri-bridge.test.ts` | 168 | Pending |
| `athena-gui/src/lib/__tests__/workspaceDialog.test.ts` | 69 | Reviewed; retain browser/native fallback checks; tests pass |
| `athena-gui/src/lib/__tests__/workspaceStorage.test.ts` | 136 | Reviewed; retain storage normalization/failure checks; tests pass |
| `athena-gui/src/lib/clarification-conversation.ts` | 97 | Pending |
| `athena-gui/src/lib/errors.ts` | 4 | Reviewed; retain shared exception-to-text boundary |
| `athena-gui/src/lib/graphLayout.ts` | 66 | Pending |
| `athena-gui/src/lib/path.ts` | 5 | Reviewed; retain workspace path display semantics |
| `athena-gui/src/lib/rpc-error.ts` | 46 | Reviewed; retain RPC wire error normalization for both transports |
| `athena-gui/src/lib/tauri-bridge.ts` | 766 | Pending |
| `athena-gui/src/lib/workspaceDialog.ts` | 20 | Reviewed; retain native-dialog/browser boundary |
| `athena-gui/src/lib/workspaceStorage.ts` | 96 | Reviewed; unify JSON storage IO and exception handling; 17 focused tests pass |
| `athena-gui/src/lib/ws-backend.ts` | 154 | Pending |
| `athena-gui/src/main.tsx` | 15 | Pending |
| `athena-gui/src/styles.css` | 1046 | Pending |
| `athena-gui/src/test/render.tsx` | 6 | Pending |
| `athena-gui/src/test/setup.ts` | 5 | Pending |
| `athena-gui/src/types/__tests__/ui.test.ts` | 27 | Pending |
| `athena-gui/src/types/ui.ts` | 172 | Pending |
| `athena-gui/src/vite-env.d.ts` | 1 | Pending |
| `src/athena/__init__.py` | 1 | Pending |
| `src/athena/agents/__init__.py` | 4 | Pending |
| `src/athena/agents/base_runner.py` | 142 | Reviewed; use session memory directly; retain mailbox and tool projection semantics |
| `src/athena/agents/ideator/__init__.py` | 4 | Pending |
| `src/athena/agents/ideator/ideator.py` | 624 | Pending |
| `src/athena/agents/ideator/types.py` | 69 | Pending |
| `src/athena/agents/ideator_agent.py` | 157 | Pending |
| `src/athena/agents/kaggle_handoff_agent.py` | 108 | Pending |
| `src/athena/agents/orchestration.py` | 215 | Reviewed; remove dummy followup attribute and repeated abstract method; retain tool policy |
| `src/athena/agents/prepare_agent.py` | 145 | Pending |
| `src/athena/agents/prompt_agent.py` | 126 | Pending |
| `src/athena/agents/prompts/baseline_ideator_agent.md` | 133 | Pending |
| `src/athena/agents/prompts/data_agent.md` | 45 | Pending |
| `src/athena/agents/prompts/eda_worker_agent.md` | 24 | Pending |
| `src/athena/agents/prompts/evaluator_agent.md` | 243 | Pending |
| `src/athena/agents/prompts/general_agent.md` | 54 | Pending |
| `src/athena/agents/prompts/ideator_agent.md` | 85 | Pending |
| `src/athena/agents/prompts/ideator_gated_agent.md` | 102 | Pending |
| `src/athena/agents/prompts/kaggle_handoff_agent.md` | 96 | Pending |
| `src/athena/agents/prompts/plan_agent.md` | 42 | Pending |
| `src/athena/agents/prompts/prepare_agent.md` | 134 | Pending |
| `src/athena/agents/prompts/prepare_eda_agent.md` | 65 | Pending |
| `src/athena/agents/prompts/supervisor_agent.md` | 73 | Pending |
| `src/athena/agents/prompts/validate_agent.md` | 12 | Pending |
| `src/athena/agents/supervisor_agent.py` | 451 | Pending |
| `src/athena/agents/task_agents.py` | 172 | Pending |
| `src/athena/agents/tools/__init__.py` | 1 | Pending |
| `src/athena/agents/tools/generic_tools.py` | 113 | Pending |
| `src/athena/app_server/__init__.py` | 52 | Pending |
| `src/athena/app_server/__main__.py` | 219 | Pending |
| `src/athena/app_server/client.py` | 469 | Pending |
| `src/athena/app_server/events.py` | 188 | Pending |
| `src/athena/app_server/exceptions.py` | 21 | Pending |
| `src/athena/app_server/execution.py` | 73 | Pending |
| `src/athena/app_server/lifecycle.py` | 181 | Pending |
| `src/athena/app_server/observability.py` | 146 | Pending |
| `src/athena/app_server/protocol.py` | 233 | Pending |
| `src/athena/app_server/server.py` | 361 | Pending |
| `src/athena/app_server/submissions.py` | 115 | Pending |
| `src/athena/app_server/thread_manager.py` | 288 | Pending |
| `src/athena/app_server/thread_runtime.py` | 633 | Pending |
| `src/athena/app_server/transport.py` | 138 | Pending |
| `src/athena/cli.py` | 1125 | Pending |
| `src/athena/core/__init__.py` | 35 | Pending |
| `src/athena/core/agent/__init__.py` | 51 | Reviewed; retain canonical exports, remove constructor alias; public API tests pass |
| `src/athena/core/agent/chat.py` | New | Reviewed; merged text/structured call context; 432 combined tests pass |
| `src/athena/core/agent/agent_runtime.py` | 730 | Pending |
| `src/athena/core/agent/models.py` | 83 | Pending |
| `src/athena/core/agent/provider.py` | 599 | Pending |
| `src/athena/core/agent/registry.py` | 55 | Reviewed; factory parameters reduced from 2 to 1; migrate production and test factories |
| `src/athena/core/agent/runtime.py` | 654 | Reviewed; remove task placeholders, result remapping, dispatch closures and redundant parameters; 356 tests pass |
| `src/athena/core/agent/session.py` | 82 | Reviewed; delete memory facade and property forwarding; preserve checkpoint ownership |
| `src/athena/core/agent/settings.py` | 242 | Pending |
| `src/athena/core/agent/tools/__init__.py` | 5 | Pending |
| `src/athena/core/agent/tools/user_input.py` | 157 | Pending |
| `src/athena/core/agent/types.py` | 229 | Pending |
| `src/athena/core/artifact_store.py` | 118 | Pending |
| `src/athena/core/contracts.py` | 58 | Pending |
| `src/athena/core/git_workspace.py` | 506 | Pending |
| `src/athena/core/human_request.py` | 229 | Pending |
| `src/athena/core/persistence.py` | 24 | Pending |
| `src/athena/core/project_lock.py` | 101 | Pending |
| `src/athena/core/research_models.py` | 97 | Pending |
| `src/athena/core/research_tree.py` | 569 | Pending |
| `src/athena/core/retry.py` | 98 | Pending |
| `src/athena/core/thread_models.py` | 24 | Pending |
| `src/athena/core/tool.py` | 208 | Reviewed; inline private forwarding layers; keep one registry mapping; 402 tests pass |
| `src/athena/core/tool_types.py` | 91 | Reviewed; remove unused max_result_chars; retain shared data contracts |
| `src/athena/core/workspace.py` | 86 | Pending |
| `src/athena/execution/__init__.py` | 42 | Reviewed; retain one public execution facade after monitoring merge |
| `src/athena/execution/backend.py` | 122 | Reviewed; retain the source-independent backend protocol and local implementation |
| `src/athena/execution/check.py` | 249 | Reviewed; four-field scratch records and derived host status |
| `src/athena/execution/compute_config.py` | 124 | Reviewed; retain six consumed fields and separate file/mapping boundaries |
| `src/athena/execution/events.py` | 139 | Reviewed; merged contracts into monitor.py and deleted file |
| `src/athena/execution/monitor.py` | 286 | Reviewed; own event contracts and monitor lifecycle in one module |
| `src/athena/execution/pool.py` | 363 | Reviewed; automatic preflight, six-field leases, one condition lock |
| `src/athena/execution/remote/__init__.py` | 23 | Reviewed; export the merged mirror backend from the remote facade |
| `src/athena/execution/remote/agent.py` | 473 | Pending |
| `src/athena/execution/remote/channel.py` | 463 | Pending |
| `src/athena/execution/remote/dataset.py` | 188 | Reviewed; four-field report, mandatory verification, private staging helpers |
| `src/athena/execution/remote/mirror.py` | 191 | Reviewed; absorb the three-field mirrored execution wrapper and narrow helper surface |
| `src/athena/execution/remote/mirrored.py` | 120 | Reviewed; merge into mirror.py and delete forwarding file |
| `src/athena/execution/remote/ssh.py` | 362 | Reviewed; five-parameter backend construction and per-run cwd ownership |
| `src/athena/execution/runtime.py` | 895 | Pending |
| `src/athena/gui/__init__.py` | 9 | Pending |
| `src/athena/gui/experiments.py` | 59 | Pending |
| `src/athena/gui/graph.py` | 309 | Pending |
| `src/athena/gui/service.py` | 345 | Pending |
| `src/athena/gui/traces.py` | 122 | Pending |
| `src/athena/kaggle/__init__.py` | 59 | Pending |
| `src/athena/kaggle/auth.py` | 72 | Pending |
| `src/athena/kaggle/client.py` | 418 | Pending |
| `src/athena/kaggle/pipeline.py` | 156 | Pending |
| `src/athena/kaggle/schemas.py` | 63 | Pending |
| `src/athena/kaggle/tool.py` | 444 | Pending |
| `src/athena/kaggle/wiring.py` | 124 | Pending |
| `src/athena/memory/__init__.py` | 13 | Reviewed; retain public exports |
| `src/athena/memory/compaction.py` | 138 | Reviewed; retain checkpoint consumed by ThreadRuntime |
| `src/athena/memory/context_manager.py` | 118 | Reviewed; retain token and rollback owner |
| `src/athena/memory/rollout.py` | 194 | Reviewed; delete redundant attribute and recovery wrapper; 42 tests pass |
| `src/athena/research/__init__.py` | 18 | Pending |
| `src/athena/research/clarification/__init__.py` | 1 | Pending |
| `src/athena/research/clarification/confirmation.py` | 240 | Pending |
| `src/athena/research/clarification/context.py` | 230 | Pending |
| `src/athena/research/clarification/controller.py` | 323 | Pending |
| `src/athena/research/clarification/errors.py` | 30 | Pending |
| `src/athena/research/clarification/generator.py` | 372 | Pending |
| `src/athena/research/clarification/handoff.py` | 86 | Pending |
| `src/athena/research/clarification/llm_generator.py` | 422 | Pending |
| `src/athena/research/clarification/models.py` | 144 | Pending |
| `src/athena/research/clarification/persistence.py` | 149 | Pending |
| `src/athena/research/clarification/requirements.py` | 96 | Pending |
| `src/athena/research/clarification/state.py` | 231 | Pending |
| `src/athena/research/config.py` | 83 | Pending |
| `src/athena/research/contracts.py` | 88 | Pending |
| `src/athena/research/data_models.py` | 46 | Pending |
| `src/athena/research/evaluation/__init__.py` | 5 | Pending |
| `src/athena/research/evaluation/evaluator.py` | 115 | Pending |
| `src/athena/research/evaluation/spec.py` | 168 | Pending |
| `src/athena/research/evaluation/trust.py` | 264 | Pending |
| `src/athena/research/evaluation/validation.py` | 70 | Pending |
| `src/athena/research/experiment_documents/__init__.py` | 11 | Pending |
| `src/athena/research/experiment_documents/models.py` | 324 | Pending |
| `src/athena/research/experiment_documents/projector.py` | 391 | Pending |
| `src/athena/research/experiment_documents/store.py` | 301 | Pending |
| `src/athena/research/exploration_files.py` | 43 | Pending |
| `src/athena/research/fork.py` | 215 | Pending |
| `src/athena/research/idea_generation/__init__.py` | 12 | Pending |
| `src/athena/research/idea_generation/citation_support.py` | 89 | Pending |
| `src/athena/research/idea_generation/gate.py` | 219 | Pending |
| `src/athena/research/idea_generation/gatekeeper.py` | 262 | Pending |
| `src/athena/research/idea_generation/idea_schemas.py` | 453 | Pending |
| `src/athena/research/idea_generation/pre_gate_checks.py` | 112 | Pending |
| `src/athena/research/idea_generation/prompts.py` | 59 | Pending |
| `src/athena/research/idea_generation/review_board.py` | 134 | Pending |
| `src/athena/research/idea_generation/structured_chat.py` | 83 | Reviewed; merged into core/agent/chat.py and deleted; schema retry/artifact test passes |
| `src/athena/research/idea_generation/validation.py` | 99 | Pending |
| `src/athena/research/literature/__init__.py` | 1 | Pending |
| `src/athena/research/literature/bench/__init__.py` | 72 | Pending |
| `src/athena/research/literature/bench/datasets/imbalance_auc.json` | 91 | Pending |
| `src/athena/research/literature/bench/datasets/imbalance_auc_recall.json` | 35 | Pending |
| `src/athena/research/literature/bench/health.py` | 175 | Pending |
| `src/athena/research/literature/bench/known_item.py` | 273 | Pending |
| `src/athena/research/literature/bench/query_sets.py` | 60 | Pending |
| `src/athena/research/literature/bench/recall.py` | 169 | Pending |
| `src/athena/research/literature/bench/reproducibility.py` | 55 | Pending |
| `src/athena/research/literature/bench/schemas.py` | 204 | Pending |
| `src/athena/research/literature/contracts.py` | 45 | Pending |
| `src/athena/research/literature/paper_markdown/__init__.py` | 15 | Pending |
| `src/athena/research/literature/paper_markdown/chunking.py` | 213 | Pending |
| `src/athena/research/literature/paper_markdown/document.py` | 65 | Pending |
| `src/athena/research/literature/paper_markdown/interfaces.py` | 102 | Pending |
| `src/athena/research/literature/paper_markdown/pdf_elements.py` | 62 | Pending |
| `src/athena/research/literature/paper_markdown/pdf_layout.py` | 79 | Pending |
| `src/athena/research/literature/paper_markdown/pdf_parser.py` | 1275 | Pending |
| `src/athena/research/literature/paper_markdown/processor.py` | 515 | Pending |
| `src/athena/research/literature/paper_markdown/quality.py` | 373 | Pending |
| `src/athena/research/literature/paper_markdown/schemas.py` | 368 | Pending |
| `src/athena/research/literature/paper_markdown/tex_bibliography.py` | 84 | Pending |
| `src/athena/research/literature/paper_markdown/tex_parser.py` | 1259 | Pending |
| `src/athena/research/literature/paper_markdown/tex_render.py` | 157 | Pending |
| `src/athena/research/literature/paper_markdown/tex_source.py` | 414 | Pending |
| `src/athena/research/literature/paper_markdown/tex_tables.py` | 196 | Pending |
| `src/athena/research/literature/paper_markdown/tool.py` | 81 | Pending |
| `src/athena/research/literature/paper_markdown/visuals.py` | 230 | Pending |
| `src/athena/research/literature/paper_rag/__init__.py` | 29 | Pending |
| `src/athena/research/literature/paper_rag/index.py` | 676 | Pending |
| `src/athena/research/literature/paper_rag/interfaces.py` | 42 | Pending |
| `src/athena/research/literature/paper_rag/schemas.py` | 173 | Pending |
| `src/athena/research/literature/paper_rag/search.py` | 567 | Pending |
| `src/athena/research/literature/paper_rag/tool.py` | 586 | Pending |
| `src/athena/research/literature/paper_rag/traversal.py` | 241 | Pending |
| `src/athena/research/literature/paper_scout/__init__.py` | 13 | Pending |
| `src/athena/research/literature/paper_scout/agent.py` | 443 | Pending |
| `src/athena/research/literature/paper_scout/backends.py` | 311 | Pending |
| `src/athena/research/literature/paper_scout/pool.py` | 222 | Pending |
| `src/athena/research/literature/paper_scout/prompts.py` | 98 | Pending |
| `src/athena/research/literature/paper_scout/reranker.py` | 210 | Pending |
| `src/athena/research/literature/paper_scout/schemas.py` | 350 | Pending |
| `src/athena/research/literature/paper_scout/scorer.py` | 326 | Pending |
| `src/athena/research/literature/paper_scout/selection.py` | 369 | Pending |
| `src/athena/research/literature/paper_scout/session.py` | 238 | Pending |
| `src/athena/research/literature/paper_scout/tool.py` | 97 | Pending |
| `src/athena/research/literature/paper_source/__init__.py` | 9 | Pending |
| `src/athena/research/literature/paper_source/arxiv.py` | 245 | Pending |
| `src/athena/research/literature/paper_source/fetcher.py` | 709 | Pending |
| `src/athena/research/literature/paper_source/http.py` | 236 | Pending |
| `src/athena/research/literature/paper_source/openalex.py` | 152 | Pending |
| `src/athena/research/literature/paper_source/payloads.py` | 198 | Pending |
| `src/athena/research/literature/paper_source/schemas.py` | 379 | Pending |
| `src/athena/research/literature/paper_source/tool.py` | 91 | Pending |
| `src/athena/research/literature/survey/__init__.py` | 43 | Pending |
| `src/athena/research/literature/survey/library.py` | 333 | Pending |
| `src/athena/research/literature/survey/pipeline.py` | 581 | Pending |
| `src/athena/research/literature/survey/providers.py` | 174 | Pending |
| `src/athena/research/literature/survey/report.py` | 237 | Pending |
| `src/athena/research/literature/survey/stages.py` | 591 | Pending |
| `src/athena/research/literature/survey/tool.py` | 110 | Pending |
| `src/athena/research/literature/survey/wiring.py` | 344 | Pending |
| `src/athena/research/output_freshness.py` | 115 | Pending |
| `src/athena/research/prepare/__init__.py` | 1 | Pending |
| `src/athena/research/prepare/authority.py` | 171 | Pending |
| `src/athena/research/prepare/baseline.py` | 629 | Pending |
| `src/athena/research/prepare/baseline_research.py` | 883 | Pending |
| `src/athena/research/prepare/data.py` | 118 | Pending |
| `src/athena/research/prepare/eda.py` | 378 | Pending |
| `src/athena/research/prepare/evaluator.py` | 363 | Pending |
| `src/athena/research/prepare/orchestrator.py` | 61 | Pending |
| `src/athena/research/prepare/repository_url.py` | 135 | Pending |
| `src/athena/research/prepare/source_verification.py` | 613 | Pending |
| `src/athena/research/report.py` | 397 | Pending |
| `src/athena/research/runtime/__init__.py` | 5 | Pending |
| `src/athena/research/runtime/bootstrap.py` | 438 | Pending |
| `src/athena/research/runtime/clarification.py` | 149 | Pending |
| `src/athena/research/runtime/control.py` | 214 | Pending |
| `src/athena/research/runtime/corpus.py` | 90 | Pending |
| `src/athena/research/runtime/event_projection.py` | 89 | Pending |
| `src/athena/research/runtime/events.py` | 457 | Pending |
| `src/athena/research/runtime/facade.py` | 728 | Pending |
| `src/athena/research/runtime/phase_runner.py` | 338 | Pending |
| `src/athena/research/runtime/resume_contract.py` | 56 | Pending |
| `src/athena/research/runtime/services.py` | 136 | Pending |
| `src/athena/research/runtime/settings.py` | 308 | Pending |
| `src/athena/research/runtime/survey.py` | 165 | Pending |
| `src/athena/research/script_runner.py` | 388 | Pending |
| `src/athena/research/splitter.py` | 308 | Pending |
| `src/athena/research/supervisor/__init__.py` | 5 | Pending |
| `src/athena/research/supervisor/deps.py` | 111 | Pending |
| `src/athena/research/supervisor/evaluator_plan.py` | 425 | Pending |
| `src/athena/research/supervisor/events.py` | 277 | Pending |
| `src/athena/research/supervisor/experiment.py` | 555 | Pending |
| `src/athena/research/supervisor/manifest.py` | 108 | Pending |
| `src/athena/research/supervisor/phases.py` | 650 | Pending |
| `src/athena/research/supervisor/plan_lifecycle.py` | 192 | Pending |
| `src/athena/research/supervisor/plan_runtime.py` | 450 | Pending |
| `src/athena/research/supervisor/plans.py` | 180 | Pending |
| `src/athena/research/supervisor/prepare.py` | 296 | Pending |
| `src/athena/research/supervisor/prompt_context.py` | 74 | Pending |
| `src/athena/research/supervisor/recovery.py` | 60 | Pending |
| `src/athena/research/supervisor/run_state.py` | 145 | Pending |
| `src/athena/research/supervisor/scheduling.py` | 389 | Pending |
| `src/athena/research/supervisor/search_loop.py` | 429 | Pending |
| `src/athena/research/supervisor/settlement.py` | 290 | Pending |
| `src/athena/research/supervisor/state.py` | 242 | Pending |
| `src/athena/research/supervisor/statistics.py` | 90 | Pending |
| `src/athena/research/supervisor/supervisor.py` | 288 | Pending |
| `src/athena/research/supervisor/validation.py` | 711 | Pending |
| `src/athena/research/supervisor/validation_contracts.py` | 84 | Pending |
| `src/athena/research/turns/__init__.py` | 1 | Reviewed; retain package boundary |
| `src/athena/research/turns/common.py` | 98 | Reviewed; derive agent service from runtime instead of duplicate parameter |
| `src/athena/research/turns/general.py` | 215 | Reviewed; delete two single-call tool wrappers; retain optional handoff policy |
| `src/athena/research/turns/ideator.py` | 520 | Pending |
| `src/athena/research/turns/runner.py` | 98 | Reviewed; retain distinct Supervisor/Data result boundaries |
| `src/athena/research/turns/support.py` | 126 | Reviewed; retain two-stage citation verification policy |
| `src/athena/retrieval/__init__.py` | 1 | Reviewed; retain minimal package boundary |
| `src/athena/retrieval/web_search.py` | 386 | Reviewed; delete dual-owner tool base and use one shared session dependency |
| `src/athena/serving/__init__.py` | 1 | Reviewed; retain package boundary |
| `src/athena/serving/http_api.py` | 152 | Reviewed; merged into predictions_api and deleted |
| `src/athena/serving/model.py` | 305 | Reviewed; retain isolated model contract |
| `src/athena/serving/predictions_api.py` | 39 | Reviewed; owns HTTP implementation and CLI; 22 tests pass |
| `src/athena/utils/__init__.py` | 5 | Reviewed; delete one-function package shim; all consumers migrated |
| `src/athena/utils/single_turn_chat.py` | 135 | Reviewed; merge into core/agent/chat.py; reduce sampling arguments to AgentConfig |
| `src/athena_tui/__init__.py` | 8 | Reviewed; retain minimal one-symbol package facade |
| `src/athena_tui/__main__.py` | 6 | Reviewed; retain required `python -m` entry |
| `src/athena_tui/app.py` | 668 | Reviewed; instance attributes 17 to 11, event reducer arguments 3 to 2, preserve prompt-toolkit behavior |
| `src/athena_tui/controller.py` | 57 | Reviewed; remove three redundant subscription lifecycle methods and one test-only attribute; 10 tests pass before and after |
| `src/athena_tui/entrypoint.py` | 62 | Reviewed; retain tested argument, TTY, composition and resume boundaries |
| `src/athena_tui/render.py` | 499 | Reviewed; retain pure renderer and delete test-only joined-history wrapper |
| `src/athena_tui/state.py` | 166 | Reviewed; remove unread plans projection and forwarding status property |
| `src/gui_gateway/__init__.py` | 1 | Reviewed; retain required package boundary |
| `src/gui_gateway/__main__.py` | 149 | Reviewed; remove duplicate root validation and callable protocol; use one server-default path |
| `src/gui_gateway/handler.py` | 617 | Reviewed; retain explicit protocol/state lifecycle; remove duplicate session-path helper |
| `src/gui_gateway/human.py` | 279 | Reviewed; retain active legacy/scoped broker contracts; remove prohibited future import |
| `src/gui_gateway/state_store.py` | 139 | Reviewed; inline zero-logic default-state wrapper |
| `src/gui_gateway/transport.py` | 123 | Reviewed; retain session subscription, serialization, and send-lock boundary |
| `athena-rust/crates/athena-agent/src/agent.rs` | 369 | Reviewed; return runtime TurnOutput directly, delete getter/error wrapper, and make cancellation terminal |
| `athena-rust/crates/athena-agent/src/input.rs` | 47 | Reviewed; retain plain and root-restricted input strategies |
| `athena-rust/crates/athena-agent/src/lib.rs` | 21 | Reviewed; remove duplicate AgentOutcome export and retain canonical facade |
| `athena-rust/crates/athena-agent/src/openai_provider.rs` | 216 | Reviewed; remove unread emitter parameter and retain isolated transport boundary |
| `athena-rust/crates/athena-agent/src/provider.rs` | 141 | Reviewed; preserve message roles instead of forcing plain text to user |
| `athena-rust/crates/athena-agent/src/runner.rs` | 50 | Reviewed; delete the unconstructed runtime adapter and its ignored context limit |
| `athena-rust/crates/athena-agent/src/subagent.rs` | 229 | Reviewed; retain concurrency, memory, event, and cancellation lifecycle owner |
| `athena-rust/crates/athena-agent/tests/common/mod.rs` | 230 | Reviewed; retain shared deterministic provider/tool/context fixtures |
| `athena-rust/crates/athena-agent/tests/provider_mapping.rs` | 54 | Reviewed; add plain assistant-role regression |
| `athena-rust/crates/athena-agent/tests/subagent.rs` | 51 | Reviewed; retain completion, messaging, event, and cancellation coverage |
| `athena-rust/crates/athena-agent/tests/tool_loop.rs` | 113 | Reviewed; retain ordering/barrier/error coverage and add pre-cancelled regression |
| `athena-rust/crates/athena-memory/src/compaction.rs` | 308 | Reviewed; delete the self-tested, unconnected compaction prototype |
| `athena-rust/crates/athena-memory/src/context.rs` | 443 | Reviewed; merge the active one-field buffer into message.rs and delete file |
| `athena-rust/crates/athena-memory/src/lib.rs` | 9 | Reviewed; expose one private-module facade without prototype exports |
| `athena-rust/crates/athena-memory/src/message.rs` | 188 | Reviewed; own wire types and the active normalized conversation buffer |
| `athena-rust/crates/athena-memory/src/rollout.rs` | 519 | Reviewed; delete the unconnected duplicate of Python rollout persistence |
| `athena-rust/crates/athena-memory/tests/compaction.rs` | 312 | Reviewed; delete tests exclusive to the removed prototype |
| `athena-rust/crates/athena-memory/tests/context_parity.rs` | 219 | Reviewed; retain fixture-governed message wire parity |
| `athena-rust/crates/athena-memory/tests/rollout_recovery.rs` | 371 | Reviewed; delete tests exclusive to the removed prototype |
| `athena-rust/crates/athena-protocol/src/envelope.rs` | 88 | Reviewed; retain fixture-governed wire envelopes and transport unions |
| `athena-rust/crates/athena-protocol/src/error.rs` | 103 | Reviewed; one RpcError constructor, no copied application exception or forwarding helpers |
| `athena-rust/crates/athena-protocol/src/lib.rs` | 162 | Reviewed; absorb method constants and delete duplicate inline tests |
| `athena-rust/crates/athena-protocol/src/method.rs` | 19 | Reviewed; merge constants into facade and delete file |
| `athena-rust/crates/athena-protocol/src/operations.rs` | 91 | Reviewed; retain consumed request DTOs and delete five unused result DTOs |
| `athena-rust/crates/athena-protocol/tests/python_fixtures.rs` | 386 | Reviewed; retain 26 canonical wire/control/boundary cases |
| `athena-rust/crates/athena-research/src/experiment.rs` | 92 | Reviewed; delete with unconsumed prototype crate |
| `athena-rust/crates/athena-research/src/lib.rs` | 166 | Reviewed; delete unused facade and inline-only tests with crate |
| `athena-rust/crates/athena-research/src/tree.rs` | 141 | Reviewed; delete stale duplicate of authoritative Python ResearchTree |
| `athena-rust/crates/athena-runtime/src/event.rs` | 303 | Reviewed; remove unused journal surface and subscription state |
| `athena-rust/crates/athena-runtime/src/lib.rs` | 20 | Reviewed; retain the minimal runtime facade |
| `athena-rust/crates/athena-runtime/src/runner.rs` | 57 | Reviewed; retain the runtime execution contract |
| `athena-rust/crates/athena-runtime/src/submission.rs` | 84 | Reviewed; remove the actor-discarded success result reference |
| `athena-rust/crates/athena-runtime/src/thread_actor.rs` | 338 | Reviewed; remove write-only completion state and ignored reasons |
| `athena-rust/crates/athena-runtime/src/thread_handle.rs` | 111 | Reviewed; remove unread identity and narrow lifecycle commands |
| `athena-rust/crates/athena-runtime/src/thread_manager.rs` | 222 | Reviewed; remove unused lookup and narrow interrupt/close interfaces |
| `athena-rust/crates/athena-runtime/tests/common/mod.rs` | 85 | Reviewed; retain shared deterministic runner fixtures |
| `athena-rust/crates/athena-runtime/tests/thread_races.rs` | 54 | Reviewed; retain late-event and terminal race coverage |
| `athena-rust/crates/athena-runtime/tests/thread_runtime.rs` | 187 | Reviewed; retain lifecycle, fork, subscription, and shutdown coverage |
| `athena-rust/crates/athena-server/src/client.rs` | 219 | Reviewed; inline one-call worker, remove retained ready state, and keep six live fields |
| `athena-rust/crates/athena-server/src/execution.rs` | 118 | Reviewed; retain method-to-runtime boundary and use one RpcError constructor |
| `athena-rust/crates/athena-server/src/lib.rs` | 19 | Reviewed; remove state/Sequencer/responder exports and expose direct transport factory |
| `athena-rust/crates/athena-server/src/lifecycle.rs` | 62 | Reviewed; consume direct transport halves and delete test-only state accessor |
| `athena-rust/crates/athena-server/src/processor.rs` | 212 | Reviewed; fixed admission capacity is internal and start takes two dependencies |
| `athena-rust/crates/athena-server/src/subscription.rs` | 83 | Reviewed; retain pump lifecycle and drain all subscriptions under one map take |
| `athena-rust/crates/athena-server/src/transport.rs` | 307 | Reviewed; replace eight-field temporary object and seven forwarding methods with direct halves |
| `athena-rust/crates/athena-server/tests/app_server_e2e.rs` | 131 | Reviewed; retain ready rejection and full lifecycle coverage through generic raw transport |
| `athena-rust/crates/athena-tools/src/context.rs` | 8 | Reviewed; merge two-field invocation context into tool.rs and delete file |
| `athena-rust/crates/athena-tools/src/executor.rs` | 111 | Reviewed; retain lifecycle boundary; construct its sole failure result directly |
| `athena-rust/crates/athena-tools/src/lib.rs` | 19 | Reviewed; remove context module and builder export; retain canonical facade |
| `athena-rust/crates/athena-tools/src/registry.rs` | 105 | Reviewed; two types/two fields to one type/one BTreeMap; delete build phase; stable order preserved |
| `athena-rust/crates/athena-tools/src/spec.rs` | 85 | Reviewed; remove unread artifacts field and three unused constructors |
| `athena-rust/crates/athena-tools/src/tool.rs` | 44 | Reviewed; absorb ToolContext and remove its unread tool-name field |
| `athena-rust/crates/athena-tools/tests/registry.rs` | 166 | Reviewed; eight duplicate/order/search/empty cases migrated to direct registry construction |
| `athena-rust/crates/athena-tools/tests/tool_lifecycle.rs` | 279 | Reviewed; six success/error/cancellation/truncation cases retained |
| `athena-rust/crates/athena-types/src/domain.rs` | 237 | Reviewed; delete seven unconsumed research-domain contracts with their prototype |
| `athena-rust/crates/athena-types/src/ids.rs` | 119 | Reviewed; remove public intermediate wrapper and unused conversion traits |
| `athena-rust/crates/athena-types/src/lib.rs` | 122 | Reviewed; retain thread/turn contracts and delete duplicate inline tests/domain exports |
| `athena-rust/crates/athena-types/src/status.rs` | 21 | Reviewed; retain two fixture-governed runtime enums |
| `athena-rust/crates/athena-types/tests/python_fixtures.rs` | 216 | Reviewed; retain six live thread/turn/identifier parity cases |
| `athena-rust/crates/athena-workspace/src/command.rs` | 61 | Reviewed; delete unconsumed injectable Git process boundary with crate |
| `athena-rust/crates/athena-workspace/src/error.rs` | 20 | Reviewed; delete crate-local error with its only consumers |
| `athena-rust/crates/athena-workspace/src/lib.rs` | 17 | Reviewed; delete unused facade and workspace member |
| `athena-rust/crates/athena-workspace/src/local.rs` | 421 | Reviewed; delete unconsumed worktree/review state manager |
| `athena-rust/crates/athena-workspace/src/model.rs` | 10 | Reviewed; delete orphaned branch record and CommitHash dependency |
| `athena-rust/crates/athena-workspace/tests/local_git_workspace.rs` | 317 | Reviewed; delete self-only five-case integration suite with prototype |
| `athena_ts/packages/athena-agent/src/agent/models.ts` | 69 | Reviewed; removed two unused fields and three data-only runtime classes |
| `athena_ts/packages/athena-agent/src/agent/provider.ts` | 415 | Reviewed; absorb settings; remove provider subclasses and abstract runtime base; preserve stream/schema/DSML behavior; 435 tests pass |
| `athena_ts/packages/athena-agent/src/agent/registry.ts` | 38 | Reviewed; factory arguments 2 to 1; independent binding and argument tests pass |
| `athena_ts/packages/athena-agent/src/agent/runtime.ts` | 456 | Reviewed; simplified sampling/configuration and deleted unused dual-signature runner |
| `athena_ts/packages/athena-agent/src/agent/session.ts` | 55 | Reviewed; remove MemoryView and forwarding getter; retain checkpoint semantics |
| `athena_ts/packages/athena-agent/src/agent/settings.ts` | 74 | Reviewed; move live contracts/configuration to provider.ts; delete unused exports and file; preserve deferred SDK limitation |
| `athena_ts/packages/athena-agent/src/agent/tools/user-input.ts` | 32 | Pending |
| `athena_ts/packages/athena-agent/src/agent/types.ts` | 215 | Reviewed; retain typed message, codec, state and error contracts; 10 tests pass |
| `athena_ts/packages/athena-agent/src/cancel.ts` | 42 | Reviewed; delete custom token/waiters; native AbortSignal throughout callers; move CancelledError to agent/types.ts; 431 tests pass |
| `athena_ts/packages/athena-agent/src/index.ts` | 29 | Reviewed; retain canonical exports; removed MemoryView no longer exported |
| `athena_ts/packages/athena-agent/src/memory/compaction.ts` | 133 | Pending |
| `athena_ts/packages/athena-agent/src/memory/context-manager.ts` | 123 | Pending |
| `athena_ts/packages/athena-agent/src/memory/index.ts` | 4 | Pending |
| `athena_ts/packages/athena-agent/src/memory/rollout.ts` | 158 | Pending |
| `athena_ts/packages/athena-agent/src/messages.ts` | 139 | Pending |
| `athena_ts/packages/athena-agent/src/single-turn-chat.ts` | 134 | Reviewed; shared AgentConfig, retained literal input/history/tool/cancellation boundary |
| `athena_ts/packages/athena-agent/src/tool-types.ts` | 74 | Reviewed; remove unused output-limit attribute; retain message truncation |
| `athena_ts/packages/athena-agent/src/tool.ts` | 183 | Reviewed; consolidate lifecycle, delete forwarding helpers and redundant registry storage |
| `athena_ts/packages/athena-agent/test/agent/agent.test.ts` | 575 | Reviewed; retain runtime/structured output contracts; add latch-controlled concurrency/failure/alignment checks; 25 tests pass before and after |
| `athena_ts/packages/athena-agent/test/agent/provider.test.ts` | 140 | Reviewed; consolidate configuration/client/streaming regressions; 18 tests pass |
| `athena_ts/packages/athena-agent/test/agent/registry.test.ts` | 75 | Reviewed; migrate factory and verify exact argument list; 6 tests pass |
| `athena_ts/packages/athena-agent/test/agent/session.test.ts` | 26 | Reviewed; direct context and detached mailbox contract test passes |
| `athena_ts/packages/athena-agent/test/agent/settings.test.ts` | 58 | Reviewed; delete obsolete configuration tests; live lazy-client and environment contracts covered in provider.test.ts |
| `athena_ts/packages/athena-agent/test/agent/types.test.ts` | 107 | Reviewed; retain protocol/state/error checks; 10 tests pass |
| `athena_ts/packages/athena-agent/test/memory/compaction.test.ts` | 85 | Pending |
| `athena_ts/packages/athena-agent/test/memory/context-manager.test.ts` | 206 | Pending |
| `athena_ts/packages/athena-agent/test/memory/rollout.test.ts` | 221 | Pending |
| `athena_ts/packages/athena-agent/test/public-api.test.ts` | 69 | Reviewed; retain canonical exports and reject removed facade; 3 tests pass |
| `athena_ts/packages/athena-agent/test/single-turn-chat.test.ts` | 179 | Reviewed; 20 tests including sampling defaults/overrides and turn budgets |
| `athena_ts/packages/athena-agent/test/tool.test.ts` | 154 | Reviewed; add cancellation, non-Error and duplicate registry checks; 16 tests pass |
| `athena_ts/packages/athena-autoresearch/dsh-ui/app.js` | 107 | Pending |
| `athena_ts/packages/athena-autoresearch/dsh-ui/hypothesis-graph.js` | 350 | Pending |
| `athena_ts/packages/athena-autoresearch/scripts/dsh-ui-server.mjs` | 204 | Pending |
| `athena_ts/packages/athena-autoresearch/scripts/run-autoresearch.mjs` | 72 | Pending |
| `athena_ts/packages/athena-autoresearch/scripts/smoke.mjs` | 115 | Pending |
| `athena_ts/packages/athena-autoresearch/src/bootstrap.ts` | 31 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/budget-service.ts` | 50 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/console-port.ts` | 36 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/gate-runner.ts` | 45 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/hypothesis-pool.ts` | 180 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/pipeline-runner.ts` | 113 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/pipeline-types.ts` | 46 | Pending |
| `athena_ts/packages/athena-autoresearch/src/core/provider-registry.ts` | 35 | Pending |
| `athena_ts/packages/athena-autoresearch/src/index.ts` | 22 | Pending |
| `athena_ts/packages/athena-autoresearch/src/plugin.ts` | 141 | Pending |
| `athena_ts/packages/athena-autoresearch/src/providers/builtins.ts` | 154 | Pending |
| `athena_ts/packages/athena-autoresearch/src/providers/compiler/local-tex.ts` | 44 | Pending |
| `athena_ts/packages/athena-autoresearch/src/providers/compiler/overleaf.ts` | 95 | Pending |
| `athena_ts/packages/athena-autoresearch/src/providers/experiment/athena-engine.ts` | 34 | Pending |
| `athena_ts/packages/athena-autoresearch/src/providers/template/curl-template.ts` | 85 | Pending |
| `athena_ts/packages/athena-autoresearch/src/providers/types.ts` | 104 | Pending |
| `athena_ts/packages/athena-autoresearch/src/runtime.ts` | 126 | Pending |
| `athena_ts/packages/athena-autoresearch/src/schemas/pool.ts` | 43 | Pending |
| `athena_ts/packages/athena-autoresearch/src/schemas/run-spec.ts` | 132 | Pending |
| `athena_ts/packages/athena-autoresearch/src/schemas/state.ts` | 102 | Pending |
| `athena_ts/packages/athena-autoresearch/src/services/packaging.ts` | 37 | Pending |
| `athena_ts/packages/athena-autoresearch/src/services/paper-composer.ts` | 69 | Pending |
| `athena_ts/packages/athena-autoresearch/src/stages.ts` | 114 | Pending |
| `athena_ts/packages/athena-autoresearch/test/_support.ts` | 53 | Pending |
| `athena_ts/packages/athena-autoresearch/test/core/budget-service.test.ts` | 43 | Pending |
| `athena_ts/packages/athena-autoresearch/test/core/gate-runner.test.ts` | 19 | Pending |
| `athena_ts/packages/athena-autoresearch/test/core/hypothesis-pool.test.ts` | 80 | Pending |
| `athena_ts/packages/athena-autoresearch/test/core/pipeline-runner.test.ts` | 58 | Pending |
| `athena_ts/packages/athena-autoresearch/test/core/provider-registry.test.ts` | 32 | Pending |
| `athena_ts/packages/athena-autoresearch/test/dsh-headless.integration.test.ts` | 72 | Pending |
| `athena_ts/packages/athena-autoresearch/test/providers/curl-template.test.ts` | 38 | Pending |
| `athena_ts/packages/athena-autoresearch/test/providers/native-svg.test.ts` | 36 | Pending |
| `athena_ts/packages/athena-autoresearch/test/providers/overleaf.test.ts` | 34 | Pending |
| `athena_ts/packages/athena-autoresearch/test/runtime.test.ts` | 49 | Pending |
| `athena_ts/packages/athena-autoresearch/test/schemas/run-spec.test.ts` | 45 | Pending |
| `athena_ts/packages/athena-autoresearch/test/services/paper-composer.test.ts` | 22 | Pending |
| `athena_ts/packages/athena-core/src/app.ts` | 53 | Pending |
| `athena_ts/packages/athena-core/src/errors.ts` | 33 | Pending |
| `athena_ts/packages/athena-core/src/id.ts` | 12 | Pending |
| `athena_ts/packages/athena-core/src/index.ts` | 21 | Pending |
| `athena_ts/packages/athena-core/src/models/contracts.ts` | 37 | Pending |
| `athena_ts/packages/athena-core/src/models/hypothesis-graph.ts` | 100 | Pending |
| `athena_ts/packages/athena-core/src/models/research-data-models.ts` | 51 | Pending |
| `athena_ts/packages/athena-core/src/models/research-models.ts` | 72 | Pending |
| `athena_ts/packages/athena-core/src/models/research-tree.ts` | 707 | Pending |
| `athena_ts/packages/athena-core/src/models/thread-models.ts` | 21 | Pending |
| `athena_ts/packages/athena-core/src/services/artifact-store.ts` | 115 | Pending |
| `athena_ts/packages/athena-core/src/services/git-workspace.ts` | 545 | Pending |
| `athena_ts/packages/athena-core/src/services/persistence.ts` | 22 | Pending |
| `athena_ts/packages/athena-core/src/services/retry.ts` | 83 | Pending |
| `athena_ts/packages/athena-core/src/services/workspace.ts` | 44 | Pending |
| `athena_ts/packages/athena-core/test/app.test.ts` | 25 | Pending |
| `athena_ts/packages/athena-core/test/errors.test.ts` | 11 | Pending |
| `athena_ts/packages/athena-core/test/id.test.ts` | 15 | Pending |
| `athena_ts/packages/athena-core/test/models/contracts.test.ts` | 32 | Pending |
| `athena_ts/packages/athena-core/test/models/research-models.test.ts` | 54 | Pending |
| `athena_ts/packages/athena-core/test/models/research-tree-scheduling.test.ts` | 324 | Pending |
| `athena_ts/packages/athena-core/test/models/research-tree.test.ts` | 389 | Pending |
| `athena_ts/packages/athena-core/test/services/artifact-store.test.ts` | 57 | Pending |
| `athena_ts/packages/athena-core/test/services/git-workspace.test.ts` | 527 | Pending |
| `athena_ts/packages/athena-core/test/services/persistence.test.ts` | 28 | Pending |
| `athena_ts/packages/athena-core/test/services/retry.test.ts` | 36 | Pending |
| `athena_ts/packages/athena-core/test/smoke.test.ts` | 7 | Pending |
| `athena_ts/packages/athena-dsh/src/index.ts` | 929 | Reviewed; retain cohesive composition root and Cordis services; privatize two internal projections |
| `athena_ts/packages/athena-dsh/test/index.test.ts` | 154 | Reviewed; four service/tool/validation/PREPARE cases plus removed-export assertions |
| `athena_ts/packages/athena-research/src/contracts.ts` | 46 | Reviewed; delete score envelope and five unused validation placeholders; retain bundle provenance |
| `athena_ts/packages/athena-research/src/evaluation.ts` | 86 | Reviewed; canonical runner/options contracts, direct output and simplified numeric validation |
| `athena_ts/packages/athena-research/src/execution.ts` | 94 | Reviewed; deleted context/root state, single run options and four-field result |
| `athena_ts/packages/athena-research/src/index.ts` | 45 | Reviewed; retain canonical exports; replace static Recovery facade with reconcilePlans; remove internal scheduling count from barrel |
| `athena_ts/packages/athena-research/src/report.ts` | 78 | Reviewed; retain shared pure renderer, remove unused precision option |
| `athena_ts/packages/athena-research/src/runtime.ts` | 372 | Reviewed; deleted unused standalone composition root; live DSH/Python roots retained |
| `athena_ts/packages/athena-research/src/script_runner.ts` | 231 | Reviewed; two-argument run, canonical source snapshot, shared restore and bounded file staging |
| `athena_ts/packages/athena-research/src/shell.ts` | 48 | Reviewed; deleted adapter exclusively used by removed runtime |
| `athena_ts/packages/athena-research/src/supervisor/events.ts` | 120 | Reviewed; delete unconnected projector/schemas/helpers; fold live redaction into PlanRunner |
| `athena_ts/packages/athena-research/src/supervisor/experiment.ts` | 418 | Reviewed; consolidated failure handling/traversal and removed unused timeout configuration |
| `athena_ts/packages/athena-research/src/supervisor/plans.ts` | 122 | Reviewed; merge schemas into contracts.ts, privatize serialization and delete file |
| `athena_ts/packages/athena-research/src/supervisor/policy.ts` | 68 | Reviewed; merge policy boundary into ranker.ts and delete file; priority arguments 2 to 1; 12 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/prepare.ts` | 255 | Reviewed; removed test-only runner factory, retained distinct freeze/baseline policies |
| `athena_ts/packages/athena-research/src/supervisor/ranker.ts` | 167 | Reviewed; absorb policy; score once per candidate, snapshot history once, cache local tokens; 21 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/recovery.ts` | 69 | Reviewed; pure two-argument reconciliation; resource checks owned by Supervisor; 27 focused tests pass |
| `athena_ts/packages/athena-research/src/supervisor/scheduler.ts` | 159 | Reviewed; actions 4 fields to 2; constructor 2 arguments to 1; runtime inputs 2 arguments to 1; one durable manual-mode authority; 19 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/state.ts` | 111 | Reviewed; plain schema-derived state, single parse, explicit durable projection and atomic persistence |
| `athena_ts/packages/athena-research/src/supervisor/supervisor.ts` | 869 | Reviewed; one 14-field phase owner, durable evaluator authority, narrowed public surface, lifecycle/race regressions |
| `athena_ts/packages/athena-research/src/supervisor/validation.ts` | 46 | Reviewed; owns result construction and existing decision/scoring loop |
| `athena_ts/packages/athena-research/src/validation.ts` | 54 | Reviewed; merged sole production use into supervisor/validation.ts and deleted |
| `athena_ts/packages/athena-research/src/worker.ts` | 125 | Reviewed; deleted after full caller tracing proved standalone root unused |
| `athena_ts/packages/athena-research/test/evaluation.test.ts` | 47 | Reviewed; seven numeric/error/prediction/identity/direction cases |
| `athena_ts/packages/athena-research/test/report.test.ts` | 76 | Reviewed; five empty/zero/formatting/pending/validation rendering cases |
| `athena_ts/packages/athena-research/test/supervisor/events.test.ts` | 53 | Reviewed; delete exclusive dead-API tests; live failure/evidence tests cover redaction |
| `athena_ts/packages/athena-research/test/supervisor/experiment.test.ts` | 473 | Reviewed; manifest/scoring/settlement, bundle failures and recursive presence |
| `athena_ts/packages/athena-research/test/supervisor/plans.test.ts` | 303 | Reviewed; 36 strict data/reference/snapshot/legacy-input cases; persistence tests own serialization |
| `athena_ts/packages/athena-research/test/supervisor/policy.test.ts` | 78 | Reviewed; retain Elo contract tests; remove tests for deleted unused helper; 12 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/prepare-plan.test.ts` | 102 | Reviewed; real PlanRunner success/artifact/feedback retry plus evaluator decisions |
| `athena_ts/packages/athena-research/test/supervisor/prepare.test.ts` | 59 | Reviewed; file/directory evaluator, labels and invalid declarations |
| `athena_ts/packages/athena-research/test/supervisor/ranker.test.ts` | 90 | Reviewed; scoring/novelty/FIFO/dedup/configuration and snapshot-count regressions; 21 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/recovery.test.ts` | 230 | Reviewed; 10 pure reconciliation cases; real missing-resource coverage moved to Supervisor tests |
| `athena_ts/packages/athena-research/test/supervisor/scheduler.test.ts` | 247 | Reviewed; literal action contracts, durable manual-mode authority, policy/budget/dedup regressions; 19 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/state.test.ts` | 244 | Reviewed; 22 persistence/validation/ownership/parse-count/projection cases |
| `athena_ts/packages/athena-research/test/supervisor/supervisor.test.ts` | 260 | Reviewed; 47 deterministic flow, recovery, wake, stop, failure and phase-concurrency cases |
| `athena_ts/packages/athena-research/test/supervisor/validation-plan.test.ts` | 33 | Reviewed; fourteen metric/tolerance/feedback/decision/budget cases |
| `athena_ts/packages/athena-research/test/validation.test.ts` | 40 | Reviewed; deleted helper/class tests, migrated coverage to real plan entrypoint |
| `athena_ts/packages/athena-research/test/worker.test.ts` | 57 | Reviewed; deleted tests exclusive to removed Worker; replacement public-surface test tracked separately |
| `athena_ts/vitest.workspace.ts` | 4 | Pending |
| `scripts/build.cjs` | 62 | Reviewed; absorb release composition behind `--package`; one parser/runner and preserved npm commands |
| `scripts/check_code_style.py` | 162 | Reviewed; retain repository hook and its distinct AST/text rules; hard-rule gate passes |
| `scripts/export_rust_contract_fixtures.py` | 374 | Reviewed; four parameters to zero, scan methods once, remove dead imports/constants; four fixture files export |
| `scripts/gui_gateway_entry.py` | 26 | Reviewed; retain frozen-only `inspect.getsource` compatibility entry used by PyInstaller |
| `scripts/prepare_examples_article.py` | 230 | Reviewed; merge single-use hash helper and reduce manifest writer from four parameters to two |
| `scripts/probe_max_tokens.py` | 117 | Reviewed; retain cohesive manual diagnostic; remove prohibited future import; help path passes |
| `scripts/release.cjs` | 60 | Reviewed; merge into build.cjs and delete duplicated parser/runner/composition file |
| `scripts/run_headless.py` | 112 | Reviewed; retain distinct headless/CI runtime entry with already-minimal one-argument helpers |
