# GUI、Gateway、TUI 模块审查

审查范围：athena-gui/src、scripts、src-tauri/src（排除依赖和生成目录），src/gui_gateway、src/athena/gui、src/athena_tui 及对应测试。遵循当前 engineering optimization plan 与代码规范；历史 module-simplification plan 中此前已 Review 的模块按当前差异复查，Pending GUI 模块逐项核对源码、调用/API 与测试后作 retain/change 决定。历史 ledger 由 root 统一维护，本报告不改它。

结论：保留当前模块边界与测试组织；本轮确认并修复两个 trace 安全缺陷（agent 路径可越界、工具参数/compact summary 中明文凭据），清除图算法中的不可达返回值，并补充漏掉的内部/公开 docstring、移除 gateway broker 装饰横线。已有 workspace/session epoch、clarification cancel/revision、服务 subscription 与 TUI 用户 logo 相关改动均保留。没有发现需要拆分组件、改协议或删除调用模块的证据。

## 逐文件 retain/change 决策

下表涵盖上述范围内当前可见的源码、资源、脚本与专属测试文件。change 表示保留并验证当前缺陷修复/会话恢复改动，不表示全部由本次宽审引入；retain 表示审查后保留当前实现。

| 文件 | 决策 | 理由 |
| --- | --- | --- |
| athena-gui/scripts/dev.cjs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/scripts/dev.test.cjs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/scripts/ensure-generated.mjs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/__tests__/App.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/App.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/__tests__/app-shell.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/__tests__/common-contracts.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/__tests__/conversation-pane.test.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/components/__tests__/markdown.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/__tests__/research-tree-viz.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/__tests__/SettingsPanel.test.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/components/AlgorithmsPanel.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/AlgorithmsPanel.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/cards/__tests__/IntentPreviewCard.test.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/components/cards/IntentPreviewCard.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/cards/IntentPreviewCard.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/components/common/BrandMark.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/Dialog.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/Dialog.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/EmptyState.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/ErrorBoundary.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/Icon.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/Markdown.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/Menu.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/Menu.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/StatusBadge.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/common/useConfirm.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/__tests__/HumanRequestPanel.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/__tests__/MessageList.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/Composer.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/Composer.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/ConversationPane.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/ConversationPane.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/HumanRequestPanel.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/HumanRequestPanel.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/MessageList.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/MessageList.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/PendingHypotheses.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/PendingHypotheses.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/RunControls.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/RunControls.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/WelcomeHero.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/conversation/WelcomeHero.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/EDAPreview.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/EDAPreview.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/ExperimentLog.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/ExperimentManager.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/ExperimentManager.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/HypothesisGraphViz.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/LLMIOPanel.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/LLMIOPanel.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/MetricChart.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/pages/ModulePage.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/pages/ModulePage.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/ReportPanel.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/ReportPanel.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/ResearchTreeViz.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/SettingsPanel.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/SettingsPanel.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/components/shell/__tests__/WorkspacePicker.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/AppShell.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/AppShell.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/BackendUnavailable.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/BackendUnavailable.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/ConnectionBanner.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/ConnectionBanner.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/Inspector.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/Inspector.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/LogDrawer.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/LogDrawer.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/MainHeader.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/MainHeader.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/navigation.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/Sidebar.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/Sidebar.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/ThemeToggle.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/WorkspacePicker.module.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/components/shell/WorkspacePicker.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/__tests__/usePipeline.events.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/__tests__/usePipeline.identity.test.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/__tests__/usePipeline.test.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/hooks/__tests__/useWorkspace.test.tsx | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/hooks/useBackendStatus.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/useInitialViewport.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/usePipeline.ts | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/hooks/useRemoteData.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/useResearchSignal.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/useTheme.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/hooks/useWorkspace.ts | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/lib/__tests__/errors.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/__tests__/graphLayout.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/__tests__/rpc-error.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/__tests__/tauri-bridge.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/__tests__/workspaceDialog.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/__tests__/workspaceStorage.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/__tests__/ws-backend.test.ts | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/lib/clarification-conversation.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/errors.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/graphLayout.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/labels.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/path.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/rpc-error.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/tauri-bridge.ts | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/lib/workspaceDialog.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/workspaceStorage.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/lib/ws-backend.ts | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| athena-gui/src/main.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/styles.css | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/test/render.tsx | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/test/setup.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/types/__tests__/ui.test.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/types/ui.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src/vite-env.d.ts | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src-tauri/src/commands/clarification.rs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src-tauri/src/commands/dialog.rs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src-tauri/src/commands/mod.rs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src-tauri/src/lib.rs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src-tauri/src/main.rs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| athena-gui/src-tauri/src/python.rs | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/athena/gui/__init__.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/athena/gui/experiments.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/athena/gui/graph.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/athena/gui/service.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/athena/gui/traces.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/athena_tui/__init__.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/athena_tui/__main__.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/athena_tui/app.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/athena_tui/controller.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/athena_tui/entrypoint.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/athena_tui/render.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/athena_tui/state.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/gui_gateway/__init__.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/gui_gateway/__main__.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/gui_gateway/controller.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/gui_gateway/handler.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/gui_gateway/human.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| src/gui_gateway/state_store.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| src/gui_gateway/transport.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| tests/test_gui_gateway_e2e.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/test_gui_gateway_handler.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| tests/test_gui_gateway_main.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/test_gui_gateway_transport.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/test_gui_gateway_transport_import.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/test_gui_protocol_contract.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/athena_tui/__init__.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/athena_tui/test_app.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/athena_tui/test_controller.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/athena_tui/test_entrypoint.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/athena_tui/test_render.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |
| tests/unit/athena_tui/test_state.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/gui/test_experiments.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/gui/test_graph.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/gui/test_state_store.py | retain | 依赖关系、协议消费者或现有测试支撑当前边界；无可证实冗余，整体 build/test 通过 |
| tests/unit/gui/test_traces.py | change | 保留并回归验证当前缺陷修复/会话恢复边界 |

## 具体修复

- src/athena/gui/traces.py：resolve trace 文件并要求其 parent 精确等于 rollout 根目录；越界路径作为 unknown trace 拒绝。tool-call 参数递归屏蔽含 key/token/password/secret 字段，字符串形式的内联凭据沿用公共 redactor；compaction summary 也走 redactor。
- tests/unit/gui/test_traces.py：覆盖 ../outside、嵌套凭据和 compaction 凭据。
- src/athena/gui/graph.py：删除 DFS 完成后的死 return False。
- docstring checker 命中的本范围嵌套函数和 PendingRequest / active_session_id 已补说明；src/gui_gateway/human.py 装饰分隔线已移除。TUI logo、产品文案和状态机没有被重写。

## 验证

- GUI: npm test — 23 Vitest files / 252 tests passed，Node dev-launcher 1/1 passed；有已知 React act(...) stderr warning。
- GUI production build: npm run build passed（TypeScript + Vite）。
- Python gateway/GUI/TUI: targeted pytest collected 196, all passed；有 1 个 Windows asyncio subprocess finalizer PytestUnraisableExceptionWarning。
- Rust/Tauri: cargo fmt --manifest-path athena-gui/src-tauri/Cargo.toml -- --check、cargo check --offline、cargo test --offline passed; Rust tests 10/10。测试中的真实本地 Python gateway 连接在关闭时打印 WinError 64 连接断开日志，但 Rust 测试通过。
- black --check 本范围 26 个 Python 文件通过；git diff --check 通过。python scripts/check_code_style.py 阻断规则通过，剩余 23 条 R3 提示位于范围外 cli/kaggle/retrieval 文件。

未执行真实 GUI、用户工作区、会话、服务或 GPU 任务。文件保存在 ignored submission/reproduction 下供审阅。

## Manual regression addendum (2026-10-15)

- `src/athena/gui/traces.py`: confirmed `_tool_args` recursively attempted `json.loads` on every nested string. The new regression proves decoded string values such as `"123"`, `"null"`, and `"true"` retain their string types while nested secret keys still redact. Parsing is now limited to the outer serialized argument value; recursive traversal only redacts.
- `src/athena_tui/app.py`: simplified the character-overlap branch into a conditional append without changing wide-character column accounting or selection bounds.
- Review of `branch_nesting_candidates`: `src/athena/execution/monitor.py`'s conditional event/state transitions remain explicit because `_transition` is optional and notifications must only include successful transitions; flattening this would obscure the guard or alter state ordering. No owned try/with candidate was present in the inventory.
- Added trigger comments to scoped specific exception handlers; the strict style gate now passes across `src/athena/gui`, `src/gui_gateway`, `src/athena_tui`, and `src/athena/execution`.
- Focused verification is partially blocked by a concurrent out-of-scope UTF-8 decode error in `src/athena/research/evaluation/evaluator.py`; no file in that research directory was changed here. The isolated test run before that concurrent corruption passed the trace regression and both spawn-cancel lifecycle cases (3 passed).
- After isolating the blocker, remote-channel lifecycle regressions passed independently (34 tests); GUI trace, TUI and gateway collections still transitively import the malformed research evaluator and remain deferred.
- After root restored the out-of-scope UTF-8 files from the preserved snapshot, GUI/TUI/gateway regressions were rerun: `pytest -q tests/unit/gui tests/unit/athena_tui tests/test_gui_gateway_handler.py tests/test_gui_gateway_transport.py tests/unit/gui/test_state_store.py tests/unit/execution/test_monitor.py` — 170 passed. No research source was edited for this review.


## Cross-session human reply completion fence (2026-10-15)

- `athena-gui/src/hooks/usePipeline.ts`: a late reply rejection could start an asynchronous `humanPending` refresh; after switching sessions, that old result could replace the new session request list, publish a stale error, or clear the new request settlement lock. Reply completion now applies state only while the captured session and request epoch remain current. Settlement cleanup is owned by the exact in-flight settlement object, so an old `finally` cannot release a newer request lock.
- `athena-gui/src/hooks/__tests__/usePipeline.test.tsx`: added a deferred refresh regression covering session switch, stale response, current lock retention, deduplication, and successful completion. It failed before the fence because `req-old` appeared in the fresh session, and passes after the fix.
- Verification: targeted regression 1 passed; hook tests `npx vitest run src/hooks/__tests__` ? 4 files / 107 tests passed. Root owns final GUI-wide tests and production build.


## Same-session clarification request ownership follow-up (2026-10-15)

- Added a human-request revision counter and required both the captured request-list revision and exact settlement identity before an asynchronous reply may refresh request state or publish/clear its error. A newly-created question in the same session invalidates stale error/refresh ownership without invalidating the older accepted success's historical transcript entry.
- Four deterministic races cover (1) stale old refresh resolving after a new question, (2) old success resolving after the newer submission has failed, (3) old refresh failure arriving after a new question, and (4) a no-change pending poll between current reply submission and its rejection. These regressions were red before the ownership guard; they now pass while preserving the older reply transcript and the newer error/request/settling state.
- Verification: targeted same-session/session-switch regressions passed; `npx vitest run src/hooks/__tests__` ? 4 files / 111 tests passed. Full GUI/build remains with root.

## Final integrated verification

- `HumanRequestPanel` invalidates its request epoch during cleanup; a late response cannot set error/submitting state on a newer question. Deferred-response tests cover both success and failure, and the component's tests await React updates.
- Pending-request comparison uses choice label/value fields, so equivalent choices with a different object-key order do not invalidate reply ownership. `cancelDraft` includes the shared request-update callback in its dependencies. The unchanged-poll regression exercises reordered choice keys.
- Root's final `npm test` passed all 259 GUI tests, 5 build-script tests and 1 launcher test. TypeScript/Vite production build passed after the final source edits. The intermediate act warnings above did not occur in this final run.
- The complete Python default suite passed after UTF-8 recovery: 3126 passed, 1 skipped, 52 subtests passed, with the one existing Windows subprocess finalizer warning. Earlier deferred collections in this report are resolved; the completion report records the remaining environment and integration limits.
