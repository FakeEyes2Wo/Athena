# Module-by-module simplification review

Baseline: `4b9dbaa`. Scope: tracked backend, TUI, gateway, GUI source, Rust bridge, Rust/TypeScript implementations, and development scripts. Tests and assets embedded in these roots remain listed so coverage cannot silently shrink.

Coverage correction: the original 391-file inventory omitted 65 Rust, 138 TypeScript/JavaScript implementation and colocated test files, and 8 root development scripts. These completed or independently buildable modules belong to the user's repository-wide goal even when Python is the production entrypoint. All 211 were unchanged from the baseline when enumerated and are added as Pending, bringing the baseline ledger to 602 files. Enumeration is not review; earlier 17/391 reports covered only the original subset. New files introduced by this refactor are tracked separately and must also be verified.

Inventory is not a completed semantic review. Each pending file requires content inspection, caller tracing, a retain/merge/delete decision, and relevant verification before closing this plan. Reconcile new or removed files before final acceptance.

## Tasks

- [x] Enumerate tracked application source files and record baseline line counts.
- [x] Read all four serving source files and trace their immediate callers.
- [x] Resolve serving compatibility entrypoint duplication and verify callers (22 tests before and after; module CLI help exits 0).
- [ ] Review every remaining file and module; record decisions and evidence.
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
- [ ] Verify the final integrated application, publish completion report, remove plan and pointer.
- [ ] Merge into main, push, and remove this task's temporary branch/worktree.

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

| File | Baseline lines | Review |
| --- | ---: | --- |
| `athena_ts/packages/athena-research/test/contracts.test.ts` | New | Reviewed; three bundle-default/provenance/validation-projection cases |
| `athena_ts/packages/athena-research/test/script-runner.test.ts` | New | Reviewed; 29 snapshot/staging/output cases with mocked subprocesses |
| `athena_ts/packages/athena-research/test/script-runner.uv-smoke.mjs` | New | Reviewed; explicit offline real-uv freeze/restore/trusted-score smoke passes |
| `athena_ts/packages/athena-research/test/execution.test.ts` | New | Reviewed; six real-process command/workdir/event/error/timeout cases |
| `athena_ts/packages/athena-research/test/public-api.test.ts` | New | Reviewed; removed legacy root/adapter exports and retained canonical DSH services |
| `athena-gui/src/components/__tests__/common-contracts.test.tsx` | New | Reviewed; verifies error boundary and persisted theme transitions |
| `athena-gui/scripts/dev-backend.cjs` | 38 | Pending |
| `athena-gui/scripts/dev-web.cjs` | 46 | Pending |
| `athena-gui/scripts/ensure-generated.mjs` | 15 | Pending |
| `athena-gui/src-tauri/src/commands/chat.rs` | 12 | Pending |
| `athena-gui/src-tauri/src/commands/clarification.rs` | 270 | Pending |
| `athena-gui/src-tauri/src/commands/dialog.rs` | 103 | Pending |
| `athena-gui/src-tauri/src/commands/experiments.rs` | 47 | Pending |
| `athena-gui/src-tauri/src/commands/graph.rs` | 27 | Pending |
| `athena-gui/src-tauri/src/commands/mod.rs` | 11 | Pending |
| `athena-gui/src-tauri/src/commands/research.rs` | 32 | Pending |
| `athena-gui/src-tauri/src/commands/search.rs` | 42 | Pending |
| `athena-gui/src-tauri/src/commands/settings.rs` | 27 | Pending |
| `athena-gui/src-tauri/src/commands/state.rs` | 53 | Pending |
| `athena-gui/src-tauri/src/commands/traces.rs` | 19 | Pending |
| `athena-gui/src-tauri/src/commands/validate.rs` | 18 | Pending |
| `athena-gui/src-tauri/src/events.rs` | 34 | Pending |
| `athena-gui/src-tauri/src/lib.rs` | 66 | Pending |
| `athena-gui/src-tauri/src/main.rs` | 6 | Pending |
| `athena-gui/src-tauri/src/python/bridge.rs` | 218 | Pending |
| `athena-gui/src-tauri/src/python/mod.rs` | 2 | Pending |
| `athena-gui/src-tauri/src/python/types.rs` | 92 | Pending |
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
| `src/athena/execution/__init__.py` | 42 | Pending |
| `src/athena/execution/backend.py` | 122 | Pending |
| `src/athena/execution/check.py` | 249 | Pending |
| `src/athena/execution/compute_config.py` | 124 | Pending |
| `src/athena/execution/events.py` | 139 | Pending |
| `src/athena/execution/monitor.py` | 286 | Pending |
| `src/athena/execution/pool.py` | 363 | Pending |
| `src/athena/execution/remote/__init__.py` | 23 | Pending |
| `src/athena/execution/remote/agent.py` | 473 | Pending |
| `src/athena/execution/remote/channel.py` | 463 | Pending |
| `src/athena/execution/remote/dataset.py` | 188 | Pending |
| `src/athena/execution/remote/mirror.py` | 191 | Pending |
| `src/athena/execution/remote/mirrored.py` | 120 | Pending |
| `src/athena/execution/remote/ssh.py` | 362 | Pending |
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
| `src/athena/research/turns/__init__.py` | 1 | Pending |
| `src/athena/research/turns/common.py` | 98 | Pending |
| `src/athena/research/turns/general.py` | 215 | Pending |
| `src/athena/research/turns/ideator.py` | 520 | Pending |
| `src/athena/research/turns/runner.py` | 98 | Pending |
| `src/athena/research/turns/support.py` | 126 | Pending |
| `src/athena/retrieval/__init__.py` | 1 | Pending |
| `src/athena/retrieval/web_search.py` | 386 | Pending |
| `src/athena/serving/__init__.py` | 1 | Reviewed; retain package boundary |
| `src/athena/serving/http_api.py` | 152 | Reviewed; merged into predictions_api and deleted |
| `src/athena/serving/model.py` | 305 | Reviewed; retain isolated model contract |
| `src/athena/serving/predictions_api.py` | 39 | Reviewed; owns HTTP implementation and CLI; 22 tests pass |
| `src/athena/utils/__init__.py` | 5 | Reviewed; delete one-function package shim; all consumers migrated |
| `src/athena/utils/single_turn_chat.py` | 135 | Reviewed; merge into core/agent/chat.py; reduce sampling arguments to AgentConfig |
| `src/athena_tui/__init__.py` | 8 | Pending |
| `src/athena_tui/__main__.py` | 6 | Pending |
| `src/athena_tui/app.py` | 668 | Pending |
| `src/athena_tui/controller.py` | 57 | Pending |
| `src/athena_tui/entrypoint.py` | 62 | Pending |
| `src/athena_tui/render.py` | 499 | Pending |
| `src/athena_tui/state.py` | 166 | Pending |
| `src/gui_gateway/__init__.py` | 1 | Pending |
| `src/gui_gateway/__main__.py` | 149 | Pending |
| `src/gui_gateway/handler.py` | 617 | Pending |
| `src/gui_gateway/human.py` | 279 | Pending |
| `src/gui_gateway/state_store.py` | 139 | Pending |
| `src/gui_gateway/transport.py` | 123 | Pending |
| `athena-rust/crates/athena-agent/src/agent.rs` | 369 | Pending |
| `athena-rust/crates/athena-agent/src/input.rs` | 47 | Pending |
| `athena-rust/crates/athena-agent/src/lib.rs` | 21 | Pending |
| `athena-rust/crates/athena-agent/src/openai_provider.rs` | 216 | Pending |
| `athena-rust/crates/athena-agent/src/provider.rs` | 141 | Pending |
| `athena-rust/crates/athena-agent/src/runner.rs` | 50 | Pending |
| `athena-rust/crates/athena-agent/src/subagent.rs` | 229 | Pending |
| `athena-rust/crates/athena-agent/tests/common/mod.rs` | 230 | Pending |
| `athena-rust/crates/athena-agent/tests/provider_mapping.rs` | 54 | Pending |
| `athena-rust/crates/athena-agent/tests/subagent.rs` | 51 | Pending |
| `athena-rust/crates/athena-agent/tests/tool_loop.rs` | 113 | Pending |
| `athena-rust/crates/athena-memory/src/compaction.rs` | 308 | Pending |
| `athena-rust/crates/athena-memory/src/context.rs` | 443 | Pending |
| `athena-rust/crates/athena-memory/src/lib.rs` | 9 | Pending |
| `athena-rust/crates/athena-memory/src/message.rs` | 188 | Pending |
| `athena-rust/crates/athena-memory/src/rollout.rs` | 519 | Pending |
| `athena-rust/crates/athena-memory/tests/compaction.rs` | 312 | Pending |
| `athena-rust/crates/athena-memory/tests/context_parity.rs` | 219 | Pending |
| `athena-rust/crates/athena-memory/tests/rollout_recovery.rs` | 371 | Pending |
| `athena-rust/crates/athena-protocol/src/envelope.rs` | 88 | Pending |
| `athena-rust/crates/athena-protocol/src/error.rs` | 103 | Pending |
| `athena-rust/crates/athena-protocol/src/lib.rs` | 162 | Pending |
| `athena-rust/crates/athena-protocol/src/method.rs` | 19 | Pending |
| `athena-rust/crates/athena-protocol/src/operations.rs` | 91 | Pending |
| `athena-rust/crates/athena-protocol/tests/python_fixtures.rs` | 386 | Pending |
| `athena-rust/crates/athena-research/src/experiment.rs` | 92 | Pending |
| `athena-rust/crates/athena-research/src/lib.rs` | 166 | Pending |
| `athena-rust/crates/athena-research/src/tree.rs` | 141 | Pending |
| `athena-rust/crates/athena-runtime/src/event.rs` | 303 | Pending |
| `athena-rust/crates/athena-runtime/src/lib.rs` | 20 | Pending |
| `athena-rust/crates/athena-runtime/src/runner.rs` | 57 | Pending |
| `athena-rust/crates/athena-runtime/src/submission.rs` | 84 | Pending |
| `athena-rust/crates/athena-runtime/src/thread_actor.rs` | 338 | Pending |
| `athena-rust/crates/athena-runtime/src/thread_handle.rs` | 111 | Pending |
| `athena-rust/crates/athena-runtime/src/thread_manager.rs` | 222 | Pending |
| `athena-rust/crates/athena-runtime/tests/common/mod.rs` | 85 | Pending |
| `athena-rust/crates/athena-runtime/tests/thread_races.rs` | 54 | Pending |
| `athena-rust/crates/athena-runtime/tests/thread_runtime.rs` | 187 | Pending |
| `athena-rust/crates/athena-server/src/client.rs` | 219 | Pending |
| `athena-rust/crates/athena-server/src/execution.rs` | 118 | Pending |
| `athena-rust/crates/athena-server/src/lib.rs` | 19 | Pending |
| `athena-rust/crates/athena-server/src/lifecycle.rs` | 62 | Pending |
| `athena-rust/crates/athena-server/src/processor.rs` | 212 | Pending |
| `athena-rust/crates/athena-server/src/subscription.rs` | 83 | Pending |
| `athena-rust/crates/athena-server/src/transport.rs` | 307 | Pending |
| `athena-rust/crates/athena-server/tests/app_server_e2e.rs` | 131 | Pending |
| `athena-rust/crates/athena-tools/src/context.rs` | 8 | Pending |
| `athena-rust/crates/athena-tools/src/executor.rs` | 111 | Pending |
| `athena-rust/crates/athena-tools/src/lib.rs` | 19 | Pending |
| `athena-rust/crates/athena-tools/src/registry.rs` | 105 | Pending |
| `athena-rust/crates/athena-tools/src/spec.rs` | 85 | Pending |
| `athena-rust/crates/athena-tools/src/tool.rs` | 44 | Pending |
| `athena-rust/crates/athena-tools/tests/registry.rs` | 166 | Pending |
| `athena-rust/crates/athena-tools/tests/tool_lifecycle.rs` | 279 | Pending |
| `athena-rust/crates/athena-types/src/domain.rs` | 237 | Pending |
| `athena-rust/crates/athena-types/src/ids.rs` | 119 | Pending |
| `athena-rust/crates/athena-types/src/lib.rs` | 122 | Pending |
| `athena-rust/crates/athena-types/src/status.rs` | 21 | Pending |
| `athena-rust/crates/athena-types/tests/python_fixtures.rs` | 216 | Pending |
| `athena-rust/crates/athena-workspace/src/command.rs` | 61 | Pending |
| `athena-rust/crates/athena-workspace/src/error.rs` | 20 | Pending |
| `athena-rust/crates/athena-workspace/src/lib.rs` | 17 | Pending |
| `athena-rust/crates/athena-workspace/src/local.rs` | 421 | Pending |
| `athena-rust/crates/athena-workspace/src/model.rs` | 10 | Pending |
| `athena-rust/crates/athena-workspace/tests/local_git_workspace.rs` | 317 | Pending |
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
| `athena_ts/packages/athena-dsh/src/index.ts` | 929 | Pending |
| `athena_ts/packages/athena-dsh/test/index.test.ts` | 154 | Pending |
| `athena_ts/packages/athena-research/src/contracts.ts` | 46 | Reviewed; delete score envelope and five unused validation placeholders; retain bundle provenance |
| `athena_ts/packages/athena-research/src/evaluation.ts` | 86 | Reviewed; canonical runner/options contracts, direct output and simplified numeric validation |
| `athena_ts/packages/athena-research/src/execution.ts` | 94 | Reviewed; deleted context/root state, single run options and four-field result |
| `athena_ts/packages/athena-research/src/index.ts` | 45 | Reviewed; retain canonical exports; replace static Recovery facade with reconcilePlans; all package builds pass |
| `athena_ts/packages/athena-research/src/report.ts` | 78 | Reviewed; retain shared pure renderer, remove unused precision option |
| `athena_ts/packages/athena-research/src/runtime.ts` | 372 | Reviewed; deleted unused standalone composition root; live DSH/Python roots retained |
| `athena_ts/packages/athena-research/src/script_runner.ts` | 231 | Reviewed; two-argument run, canonical source snapshot, shared restore and bounded file staging |
| `athena_ts/packages/athena-research/src/shell.ts` | 48 | Reviewed; deleted adapter exclusively used by removed runtime |
| `athena_ts/packages/athena-research/src/supervisor/events.ts` | 120 | Pending |
| `athena_ts/packages/athena-research/src/supervisor/experiment.ts` | 418 | Reviewed; consolidated failure handling/traversal and removed unused timeout configuration |
| `athena_ts/packages/athena-research/src/supervisor/plans.ts` | 122 | Pending |
| `athena_ts/packages/athena-research/src/supervisor/policy.ts` | 68 | Reviewed; merge policy boundary into ranker.ts and delete file; priority arguments 2 to 1; 12 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/prepare.ts` | 255 | Reviewed; removed test-only runner factory, retained distinct freeze/baseline policies |
| `athena_ts/packages/athena-research/src/supervisor/ranker.ts` | 167 | Reviewed; absorb policy; score once per candidate, snapshot history once, cache local tokens; 21 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/recovery.ts` | 69 | Reviewed; remove static class and helpers; preserve complete durable state; 12 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/scheduler.ts` | 159 | Reviewed; actions 4 fields to 2; constructor 2 arguments to 1; delete factories/forwarders; 19 tests pass |
| `athena_ts/packages/athena-research/src/supervisor/state.ts` | 111 | Reviewed; plain schema-derived state, single parse, explicit durable projection and atomic persistence |
| `athena_ts/packages/athena-research/src/supervisor/supervisor.ts` | 869 | Pending |
| `athena_ts/packages/athena-research/src/supervisor/validation.ts` | 46 | Reviewed; owns result construction and existing decision/scoring loop |
| `athena_ts/packages/athena-research/src/validation.ts` | 54 | Reviewed; merged sole production use into supervisor/validation.ts and deleted |
| `athena_ts/packages/athena-research/src/worker.ts` | 125 | Reviewed; deleted after full caller tracing proved standalone root unused |
| `athena_ts/packages/athena-research/test/evaluation.test.ts` | 47 | Reviewed; seven numeric/error/prediction/identity/direction cases |
| `athena_ts/packages/athena-research/test/report.test.ts` | 76 | Reviewed; five empty/zero/formatting/pending/validation rendering cases |
| `athena_ts/packages/athena-research/test/supervisor/events.test.ts` | 53 | Pending |
| `athena_ts/packages/athena-research/test/supervisor/experiment.test.ts` | 473 | Reviewed; manifest/scoring/settlement, bundle failures and recursive presence |
| `athena_ts/packages/athena-research/test/supervisor/plans.test.ts` | 303 | Pending |
| `athena_ts/packages/athena-research/test/supervisor/policy.test.ts` | 78 | Reviewed; retain Elo contract tests; remove tests for deleted unused helper; 12 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/prepare-plan.test.ts` | 102 | Reviewed; real PlanRunner success/artifact/feedback retry plus evaluator decisions |
| `athena_ts/packages/athena-research/test/supervisor/prepare.test.ts` | 59 | Reviewed; file/directory evaluator, labels and invalid declarations |
| `athena_ts/packages/athena-research/test/supervisor/ranker.test.ts` | 90 | Reviewed; scoring/novelty/FIFO/dedup/configuration and snapshot-count regressions; 21 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/recovery.test.ts` | 230 | Reviewed; correct active experiment fixtures; add configuration and phase recovery regressions; 12 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/scheduler.test.ts` | 247 | Reviewed; literal action contracts, null fixture correction, policy/budget/dedup regressions; 19 tests pass |
| `athena_ts/packages/athena-research/test/supervisor/state.test.ts` | 244 | Reviewed; 22 persistence/validation/ownership/parse-count/projection cases |
| `athena_ts/packages/athena-research/test/supervisor/supervisor.test.ts` | 260 | Pending |
| `athena_ts/packages/athena-research/test/supervisor/validation-plan.test.ts` | 33 | Reviewed; fourteen metric/tolerance/feedback/decision/budget cases |
| `athena_ts/packages/athena-research/test/validation.test.ts` | 40 | Reviewed; deleted helper/class tests, migrated coverage to real plan entrypoint |
| `athena_ts/packages/athena-research/test/worker.test.ts` | 57 | Reviewed; deleted tests exclusive to removed Worker; replacement public-surface test tracked separately |
| `athena_ts/vitest.workspace.ts` | 4 | Pending |
| `scripts/build.cjs` | 62 | Pending |
| `scripts/check_code_style.py` | 162 | Pending |
| `scripts/export_rust_contract_fixtures.py` | 374 | Pending |
| `scripts/gui_gateway_entry.py` | 26 | Pending |
| `scripts/prepare_examples_article.py` | 230 | Pending |
| `scripts/probe_max_tokens.py` | 117 | Pending |
| `scripts/release.cjs` | 60 | Pending |
| `scripts/run_headless.py` | 112 | Pending |
