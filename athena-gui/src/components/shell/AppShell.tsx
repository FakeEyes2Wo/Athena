import { Suspense, useEffect, useRef, useState } from "react";
import type { ContextPanelKey, ModuleKey } from "../../types/ui";
import { usePipeline } from "../../hooks/usePipeline";
import { ConversationPane } from "../conversation/ConversationPane";
import { Icon } from "../common/Icon";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { FunctionRail } from "./FunctionRail";
import { ContextSidebar } from "./ContextSidebar";
import { ContextDrawer } from "./ContextDrawer";
import { LogDrawer } from "./LogDrawer";
import { ThemeToggle } from "./ThemeToggle";
import { AthenaWordmark } from "./AthenaWordmark";
import { HumanRequestDialog } from "./HumanRequestDialog";
import { PANELS } from "../context/panels";
import { SettingsPanel } from "../SettingsPanel";
import { MODULE_BY_KEY } from "./navigation";
import styles from "./AppShell.module.css";

type Pipeline = ReturnType<typeof usePipeline>;

interface AppShellProps {
  currentRoot: string | null;
  recentRoots: string[];
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string): void;
  pipeline: Pipeline;
}

/** 统一研究工作区：顶栏 + 功能轨 + 上下文侧栏 + 主工作区 + 详情抽屉。 */
export function AppShell({ currentRoot, recentRoots, onSwitchWorkspace, onSelectWorkspace, pipeline }: AppShellProps) {
  const [module, setModule] = useState<ModuleKey>("session");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [drawerPanel, setDrawerPanel] = useState<ContextPanelKey | null>(null);
  const [logOpen, setLogOpen] = useState(false);

  const def = MODULE_BY_KEY[module];
  const { viewModel } = pipeline;

  // 切回「会话」模块时，重新拉取当前会话的 transcript，让右侧工作区恢复历史对话；
  // 运行中跳过（此时消息由实时事件驱动，避免重放打断进行中的执行）。
  const previousModule = useRef(module);
  useEffect(() => {
    const wasAway = previousModule.current !== "session";
    previousModule.current = module;
    if (
      module === "session" &&
      wasAway &&
      pipeline.viewModel.status !== "running"
    ) {
      void pipeline.switchSession(pipeline.currentSessionId);
    }
  }, [module, pipeline]);

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <button
          type="button"
          className={styles["topbar__collapse"]}
          onClick={() => setSidebarCollapsed((v) => !v)}
          aria-label={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
          title={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
        >
          <Icon name={sidebarCollapsed ? "panelLeft" : "panelLeftClose"} size={17} />
        </button>
        <div className={styles["topbar__brand"]}>
          <span className={styles["topbar__mark"]} aria-hidden>
            <Icon name="sparkles" size={15} />
          </span>
          <AthenaWordmark />
        </div>
        <div className={styles["topbar__status"]}>
          <span className={styles["status-dot"]} data-status={viewModel.status} />
          <span>
            {viewModel.phase || "idle"} · {viewModel.status}
          </span>
        </div>
        <button
          type="button"
          className={styles["topbar__collapse"]}
          onClick={() => setLogOpen((v) => !v)}
          aria-label={logOpen ? "关闭日志" : "打开日志"}
          title="运行日志"
        >
          <Icon name="log" size={17} />
        </button>
        <ThemeToggle />
      </header>

      <div className={styles.body}>
        <FunctionRail module={module} onSelect={setModule} />
        {!sidebarCollapsed && (
          <ContextSidebar
            module={module}
            currentRoot={currentRoot}
            recentRoots={recentRoots}
            sessions={pipeline.sessions}
            currentSessionId={pipeline.currentSessionId}
            onSwitchWorkspace={onSwitchWorkspace}
            onSelectWorkspace={onSelectWorkspace}
            onSelectSession={pipeline.switchSession}
            onNewSession={pipeline.newSession}
            onDeleteSession={pipeline.deleteSession}
            onOpenDrawer={setDrawerPanel}
          />
        )}
        <main className={styles.main}>
          {module === "session" ? (
            <ConversationPane pipeline={pipeline} />
          ) : module === "settings" ? (
            <SettingsPanel onClose={() => setModule("session")} />
          ) : (
            <div className={styles["main-panel"]}>
              <PanelView panel={def.panel} />
            </div>
          )}
        </main>
        {drawerPanel && (
          <ContextDrawer panel={drawerPanel} onClose={() => setDrawerPanel(null)} />
        )}
        {logOpen && (
          <LogDrawer
            entries={pipeline.logs}
            onClose={() => setLogOpen(false)}
            onClear={pipeline.clearLogs}
          />
        )}
      </div>

      <HumanRequestDialog
        requests={pipeline.humanRequests}
        onAnswer={pipeline.answerHuman}
      />
    </div>
  );
}

function PanelView({ panel }: { panel: ContextPanelKey | null }) {
  const Panel = panel ? PANELS[panel] : undefined;
  return (
    <Suspense fallback={<EmptyState icon="loader" message="加载中…" />}>
      <ErrorBoundary>
        {Panel ? <Panel /> : <EmptyState icon="loader" message="该视图暂不可用" />}
      </ErrorBoundary>
    </Suspense>
  );
}
