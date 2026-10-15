# Whole-repository engineering review (2026-10-15)

Local implementation and verification are complete. The module reports distinguish changes from retained boundaries and record caller/test evidence. Historical counts in individual slices are intermediate and overlapping; use the completion report for final totals.

## Reports

| Scope | Review |
| --- | --- |
| Core and app-server | [core-app-review.md](core-app-review.md) |
| Execution resources | [execution-review.md](execution-review.md) |
| Execution cancellation follow-up | [execution-lifecycle-independent-review.md](execution-lifecycle-independent-review.md) |
| Research and Agents | [research-agents-review.md](research-agents-review.md) |
| GUI / gateway / TUI / native bridge | [gui-tui-review.md](gui-tui-review.md) |
| Memory, input, locks, retrieval, serving and tools | [root-module-review.md](root-module-review.md) |
| Rust and independent shutdown follow-up | [root-rust-independent-review.md](root-rust-independent-review.md) |
| Development scripts | [tooling-review.md](tooling-review.md) |

## Coverage reconciliation

The older inventory has 602 numeric baseline rows. Of its 231 previous Pending rows, all 136 still-present paths now have file-level decisions; 95 are absent historical paths. Missing files were not counted as newly reviewed code.

Across the complete baseline, 226 paths are absent, including 138 retired athena_ts paths. The [archived module review ledger](module-review-ledger.md) preserves the historical decisions. Its current source/test rows were reconciled, integrated validation and main-branch delivery are complete, and unrelated plans retain their existing scope.

The following 56 current code/source paths are outside the original numeric baseline. Some already appeared as New rows in that ledger; they are listed here without double-counting the 602-file baseline. Module decisions are in the reports above.

| Current path outside the numeric baseline |
| --- |
| `athena-gui/scripts/dev.cjs` |
| `athena-gui/scripts/dev.test.cjs` |
| `athena-gui/src-tauri/build.rs` |
| `athena-gui/src-tauri/src/python.rs` |
| `athena-gui/src/components/AlgorithmsPanel.module.css` |
| `athena-gui/src/components/ExperimentManager.module.css` |
| `athena-gui/src/components/__tests__/common-contracts.test.tsx` |
| `athena-gui/src/components/__tests__/markdown.test.tsx` |
| `athena-gui/src/components/common/BrandMark.tsx` |
| `athena-gui/src/components/common/Dialog.module.css` |
| `athena-gui/src/components/common/Dialog.tsx` |
| `athena-gui/src/components/common/Markdown.tsx` |
| `athena-gui/src/components/common/Menu.module.css` |
| `athena-gui/src/components/common/Menu.tsx` |
| `athena-gui/src/components/common/useConfirm.tsx` |
| `athena-gui/src/components/conversation/HumanRequestPanel.module.css` |
| `athena-gui/src/components/conversation/HumanRequestPanel.tsx` |
| `athena-gui/src/components/conversation/__tests__/HumanRequestPanel.test.tsx` |
| `athena-gui/src/components/conversation/__tests__/MessageList.test.tsx` |
| `athena-gui/src/components/pages/ModulePage.module.css` |
| `athena-gui/src/components/pages/ModulePage.tsx` |
| `athena-gui/src/components/shell/BackendUnavailable.module.css` |
| `athena-gui/src/components/shell/BackendUnavailable.tsx` |
| `athena-gui/src/components/shell/ConnectionBanner.module.css` |
| `athena-gui/src/components/shell/ConnectionBanner.tsx` |
| `athena-gui/src/components/shell/Inspector.module.css` |
| `athena-gui/src/components/shell/Inspector.tsx` |
| `athena-gui/src/components/shell/MainHeader.module.css` |
| `athena-gui/src/components/shell/MainHeader.tsx` |
| `athena-gui/src/components/shell/Sidebar.module.css` |
| `athena-gui/src/components/shell/Sidebar.tsx` |
| `athena-gui/src/hooks/useBackendStatus.ts` |
| `athena-gui/src/hooks/useInitialViewport.ts` |
| `athena-gui/src/hooks/useRemoteData.ts` |
| `athena-gui/src/hooks/useResearchSignal.ts` |
| `athena-gui/src/lib/__tests__/errors.test.ts` |
| `athena-gui/src/lib/__tests__/ws-backend.test.ts` |
| `athena-gui/src/lib/labels.ts` |
| `athena-gui/vite.config.ts` |
| `athena-gui/vitest.config.ts` |
| `athena-rust/crates/athena-agent/tests/openai_provider.rs` |
| `scripts/__tests__/build.test.cjs` |
| `scripts/check_release_secrets.py` |
| `scripts/release_dependency_inventory.py` |
| `scripts/tui_entry.py` |
| `src/athena/core/agent/chat.py` |
| `src/athena/research/literature/bench/metrics.py` |
| `src/athena/research/literature/bench/models.py` |
| `src/athena/research/literature/bench/retrieval.py` |
| `src/athena/research/literature/paper_markdown/models.py` |
| `src/athena/research/literature/paper_rag/models.py` |
| `src/athena/research/literature/source_payload.py` |
| `src/athena/research/prediction_contract.py` |
| `src/athena/research/prepare/authority_local.py` |
| `src/athena/research/runtime/environment_repair.py` |
| `src/gui_gateway/controller.py` |

## Verification and limits

[Completion report](../../codex_docs/2026-10-15-engineering-optimization-completion.md) records final suites, skipped/warning cases, standards, scope limits and the recovered encoding incident. Raw logs and snapshots remain under ignored `submission/reproduction/2026-10-15-engineering-optimization/`. No new paid SEARCH/model session or external release was performed.

The subsequent [publication report](../../codex_docs/2026-10-15-publication-completion.md) records upstream integration, the final 3,156-test backend run, startup checks, ignore rules and verified source delivery to origin/main.
