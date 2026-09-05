# Module-by-module simplification review

Baseline: `4b9dbaa`. Scope: tracked backend, TUI, gateway, GUI source, Rust bridge and GUI development scripts. Tests and assets embedded in these roots remain listed so coverage cannot silently shrink.

Inventory is not a completed semantic review. Each pending file requires content inspection, caller tracing, a retain/merge/delete decision, and relevant verification before closing this plan. Reconcile new or removed files before final acceptance.

## Tasks

- [x] Enumerate tracked application source files and record baseline line counts.
- [x] Read all four serving source files and trace their immediate callers.
- [x] Resolve serving compatibility entrypoint duplication and verify callers (22 tests before and after; module CLI help exits 0).
- [ ] Review every remaining file and module; record decisions and evidence.
- [ ] Implement the identified simplifications with scoped regression checks.
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

Additional reads awaiting module-wide decisions: agent `models.py`, `types.py`, `__init__.py`, `agents/prompt_agent.py`, and `core/tool.py` have been read. AgentOutcome.next_context_ref still has active ThreadRuntime consumers: remove only alongside a complete context handoff migration, not as a dead field. `agent_runtime.py` and `supervisor_agent.py` are partially inspected, not completed reviews. `utils/single_turn_chat.py` forwards model configuration through an eleven-parameter convenience API; review its call sites and the agent construction API together before changing it. Remote execution's `mirror.py`, `mirrored.py`, and `backend.py` have been read; mirror transfer policies and the backend output-return contract require tracing through the pool/channel before deciding how to consolidate them.

| File | Baseline lines | Review |
| --- | ---: | --- |
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
| `athena-gui/src/components/cards/ErrorCard.tsx` | 13 | Pending |
| `athena-gui/src/components/cards/IntentPreviewCard.module.css` | 245 | Pending |
| `athena-gui/src/components/cards/IntentPreviewCard.tsx` | 250 | Pending |
| `athena-gui/src/components/cards/__tests__/IntentPreviewCard.test.tsx` | 117 | Pending |
| `athena-gui/src/components/common/EmptyState.tsx` | 18 | Pending |
| `athena-gui/src/components/common/ErrorBoundary.tsx` | 33 | Pending |
| `athena-gui/src/components/common/Icon.tsx` | 166 | Pending |
| `athena-gui/src/components/common/StatusBadge.tsx` | 13 | Pending |
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
| `athena-gui/src/components/shell/ThemeToggle.module.css` | 19 | Pending |
| `athena-gui/src/components/shell/ThemeToggle.tsx` | 25 | Pending |
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
| `athena-gui/src/hooks/useEvents.ts` | 26 | Pending |
| `athena-gui/src/hooks/usePipeline.ts` | 1708 | Pending |
| `athena-gui/src/hooks/useTheme.ts` | 40 | Pending |
| `athena-gui/src/hooks/useWorkspace.ts` | 174 | Pending |
| `athena-gui/src/lib/__tests__/graphLayout.test.ts` | 46 | Pending |
| `athena-gui/src/lib/__tests__/rpc-error.test.ts` | 31 | Pending |
| `athena-gui/src/lib/__tests__/tauri-bridge.test.ts` | 168 | Pending |
| `athena-gui/src/lib/__tests__/workspaceDialog.test.ts` | 69 | Pending |
| `athena-gui/src/lib/__tests__/workspaceStorage.test.ts` | 136 | Pending |
| `athena-gui/src/lib/clarification-conversation.ts` | 97 | Pending |
| `athena-gui/src/lib/errors.ts` | 4 | Pending |
| `athena-gui/src/lib/graphLayout.ts` | 66 | Pending |
| `athena-gui/src/lib/path.ts` | 5 | Pending |
| `athena-gui/src/lib/rpc-error.ts` | 46 | Pending |
| `athena-gui/src/lib/tauri-bridge.ts` | 766 | Pending |
| `athena-gui/src/lib/workspaceDialog.ts` | 20 | Pending |
| `athena-gui/src/lib/workspaceStorage.ts` | 96 | Pending |
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
| `src/athena/core/agent/__init__.py` | 51 | Pending |
| `src/athena/core/agent/agent_runtime.py` | 730 | Pending |
| `src/athena/core/agent/models.py` | 83 | Pending |
| `src/athena/core/agent/provider.py` | 599 | Pending |
| `src/athena/core/agent/registry.py` | 55 | Reviewed; factory parameters reduced from 2 to 1; migrate production and test factories |
| `src/athena/core/agent/runtime.py` | 654 | Pending |
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
| `src/athena/core/tool.py` | 208 | Pending |
| `src/athena/core/tool_types.py` | 91 | Pending |
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
| `src/athena/research/idea_generation/structured_chat.py` | 83 | Pending |
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
| `src/athena/utils/__init__.py` | 5 | Pending |
| `src/athena/utils/single_turn_chat.py` | 135 | Pending |
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
