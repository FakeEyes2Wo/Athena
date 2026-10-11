import { Suspense, useEffect, useRef, useState } from "react";
import { IconButton, Label, type LabelColorOptions } from "@primer/react";
import { GearIcon, LogIcon, SidebarCollapseIcon, SidebarExpandIcon } from "@primer/octicons-react";
import type { ContextPanelKey, ModuleKey, PipelineStatus } from "../../types/ui";
import { usePipeline } from "../../hooks/usePipeline";
import { basename } from "../../lib/path";
import { ConversationPane } from "../conversation/ConversationPane";
import { Icon } from "../common/Icon";
import { EmptyState } from "../common/EmptyState";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { ModuleNav } from "./ModuleNav";
import { ContextSidebar } from "./ContextSidebar";
import { ContextDrawer } from "./ContextDrawer";
import { LogDrawer } from "./LogDrawer";
import { ThemeToggle } from "./ThemeToggle";
import { HumanRequestDialog } from "./HumanRequestDialog";
import { PANELS } from "../context/panels";
import { SettingsPanel } from "../SettingsPanel";
import { MODULE_BY_KEY } from "./navigation";
import styles from "./AppShell.module.css";

type Pipeline = ReturnType<typeof usePipeline>;

interface AppShellProps {
  currentRoot: string | null;
  recentRoots: string[];
  switching: boolean;
  onSwitchWorkspace(): void;
  onSelectWorkspace(path: string, sessionId?: string): void;
  pipeline: Pipeline;
}

/** 运行状态 → 顶栏状态标签配色。 */
const STATUS_VARIANT: Record<PipelineStatus, LabelColorOptions> = {
  idle: "secondary",
  running: "success",
  paused: "attention",
  completed: "accent",
  error: "danger",
};

/** 统一研究工作区：GitHub 仓库页式顶栏 + 模块标签栏 + 左侧栏 / 主区 / 右侧详情。 */
export function AppShell({ currentRoot, recentRoots, switching, onSwitchWorkspace, onSelectWorkspace, pipeline }: AppShellProps) {
  const [module, setModule] = useState<ModuleKey>("session");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [drawerPanel, setDrawerPanel] = useState<ContextPanelKey | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const def = MODULE_BY_KEY[module];
  const { viewModel } = pipeline;
  const workspaceName = currentRoot ? basename(currentRoot) : "未选择工作区";
  const sessionTitle = pipeline.sessions.find((session) => session.id === pipeline.currentSessionId)?.title;

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
  }, [module, pipeline.currentSessionId, pipeline.viewModel.status, pipeline.switchSession]);

  return (
    <div className={styles.shell}>
      <header className={styles.topbar}>
        <IconButton
          icon={sidebarCollapsed ? SidebarExpandIcon : SidebarCollapseIcon}
          variant="invisible"
          aria-label={sidebarCollapsed ? "展开侧栏" : "折叠侧栏"}
          onClick={() => setSidebarCollapsed((v) => !v)}
        />
        <span className={styles.mark} aria-hidden="true">
          <Icon name="sparkles" size={16} />
        </span>
        <nav className={styles.breadcrumb} aria-label="当前位置">
          <span className={styles.wordmark}>Athena</span>
          <span className={styles.separator} aria-hidden="true">/</span>
          <span className={styles.crumb} title={currentRoot ?? undefined}>{workspaceName}</span>
          {sessionTitle && (
            <>
              <span className={styles.separator} aria-hidden="true">/</span>
              <span className={styles.crumbCurrent} title={sessionTitle}>{sessionTitle}</span>
            </>
          )}
        </nav>
        <div className={styles.topbarActions}>
          <span data-status={viewModel.status}>
            <Label variant={STATUS_VARIANT[viewModel.status] ?? "secondary"}>
              {viewModel.phase || "idle"} · {viewModel.status}
            </Label>
          </span>
          <IconButton
            icon={LogIcon}
            variant="invisible"
            aria-label={logOpen ? "关闭日志" : "打开日志"}
            onClick={() => setLogOpen((v) => !v)}
          />
          <ThemeToggle />
          <IconButton icon={GearIcon} variant="invisible" aria-label="设置" onClick={() => setSettingsOpen(true)} />
        </div>
      </header>

      <ModuleNav
        module={module}
        counts={{ experiments: viewModel.rightRail.searchAttempts }}
        onSelect={setModule}
      />

      <div className={styles.body}>
        {!sidebarCollapsed && (
          <ContextSidebar
            module={module}
            currentRoot={currentRoot}
            recentRoots={recentRoots}
            switching={switching}
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
            <ConversationPane key={pipeline.currentSessionId} pipeline={pipeline} />
          ) : (
            <div className={styles.mainPanel}>
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

      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}

      <HumanRequestDialog
        request={pipeline.humanRequests?.[0] ?? null}
        onReply={(reply) => {
          const current = pipeline.humanRequests?.[0];
          if (current && typeof pipeline.replyToHumanRequest === "function") {
            return pipeline.replyToHumanRequest(current.request_id, reply);
          }
          return undefined;
        }}
        settling={pipeline.settlingRequestId === pipeline.humanRequests?.[0]?.request_id}
        error={pipeline.humanPendingError}
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
