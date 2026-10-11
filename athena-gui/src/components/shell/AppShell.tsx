import { useCallback, useEffect, useMemo, useState } from "react";
import type { ModuleKey } from "../../types/ui";
import { usePipeline } from "../../hooks/usePipeline";
import { useBackendStatus } from "../../hooks/useBackendStatus";
import { ResearchSignalContext } from "../../hooks/useResearchSignal";
import { ConversationPane } from "../conversation/ConversationPane";
import { ModulePage } from "../pages/ModulePage";
import { Icon } from "../common/Icon";
import { useConfirm } from "../common/useConfirm";
import { SettingsPanel } from "../SettingsPanel";
import { Sidebar } from "./Sidebar";
import { Inspector } from "./Inspector";
import { LogDrawer } from "./LogDrawer";
import { ConnectionBanner } from "./ConnectionBanner";
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

type RightPanel = "inspector" | "log" | null;

const SIDEBAR_KEY = "athena.sidebar.collapsed";

function initialCollapsed(): boolean {
  try {
    const stored = localStorage.getItem(SIDEBAR_KEY);
    if (stored !== null) return stored === "1";
  } catch {
    // ignore
  }
  return typeof window.matchMedia === "function" && window.matchMedia("(max-width: 860px)").matches;
}

/** Unified workspace: sidebar + main view (conversation or research page) + right panel. */
export function AppShell({
  currentRoot,
  recentRoots,
  switching,
  onSwitchWorkspace,
  onSelectWorkspace,
  pipeline,
}: AppShellProps) {
  const [module, setModule] = useState<ModuleKey>("session");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(initialCollapsed);
  const [rightPanel, setRightPanel] = useState<RightPanel>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsVersion, setSettingsVersion] = useState(0);
  const [confirm, confirmDialog] = useConfirm();
  const backend = useBackendStatus();
  const { viewModel } = pipeline;
  const sessions = pipeline.sessions ?? [];
  const attention = (pipeline.humanRequests?.length ?? 0) > 0;

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed ? "1" : "0");
    } catch {
      // ignore
    }
  }, [sidebarCollapsed]);

  // Panels re-fetch their snapshots whenever this fingerprint of the run changes.
  const signal = useMemo(
    () => ({
      token: [
        viewModel.phase,
        viewModel.status,
        viewModel.rightRail.searchAttempts,
        viewModel.rightRail.successes,
        viewModel.rightRail.latestExperimentId,
        viewModel.rightRail.bestPrimary,
        viewModel.plans.length,
        pipeline.currentSessionId,
      ].join("|"),
      phase: viewModel.phase,
      running: viewModel.status === "running",
    }),
    [
      pipeline.currentSessionId,
      viewModel.phase,
      viewModel.plans.length,
      viewModel.rightRail.bestPrimary,
      viewModel.rightRail.latestExperimentId,
      viewModel.rightRail.searchAttempts,
      viewModel.rightRail.successes,
      viewModel.status,
    ],
  );

  const sessionTitle = sessions.find((session) => session.id === pipeline.currentSessionId)?.title;

  const requestDelete = useCallback(
    async (id: string) => {
      const title = sessions.find((session) => session.id === id)?.title ?? "该会话";
      const ok = await confirm({
        title: "删除会话？",
        message: `「${title}」的对话记录、运行状态与实验工作区将被永久删除，此操作无法撤销。`,
        confirmLabel: "删除",
        tone: "danger",
      });
      if (ok) await pipeline.deleteSession?.(id);
    },
    [confirm, pipeline, sessions],
  );

  const requestStop = useCallback(async () => {
    const ok = await confirm({
      title: "停止当前研究？",
      message: "正在进行的实验会被终止，已完成的实验与结果会保留。",
      confirmLabel: "停止研究",
      tone: "danger",
    });
    if (ok) await pipeline.stopRun?.();
  }, [confirm, pipeline]);

  const newSession = useCallback(() => {
    pipeline.newSession?.();
    setModule("session");
  }, [pipeline]);

  const togglePanel = (panel: Exclude<RightPanel, null>) =>
    setRightPanel((current) => (current === panel ? null : panel));

  const headerActions = (
    <>
      {module === "session" && (
        <button
          type="button"
          className="icon-btn"
          onClick={() => togglePanel("inspector")}
          aria-pressed={rightPanel === "inspector"}
          aria-label="研究概览"
          title="研究概览"
        >
          <Icon name="panelRight" size={17} />
        </button>
      )}
      <button
        type="button"
        className="icon-btn"
        onClick={() => togglePanel("log")}
        aria-pressed={rightPanel === "log"}
        aria-label={rightPanel === "log" ? "关闭日志" : "运行日志"}
        title="运行日志"
      >
        <Icon name="log" size={17} />
      </button>
    </>
  );

  return (
    <ResearchSignalContext.Provider value={signal}>
      <div className={styles.shell}>
        <Sidebar
          collapsed={sidebarCollapsed}
          onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
          module={module}
          onSelectModule={setModule}
          currentRoot={currentRoot}
          recentRoots={recentRoots}
          sessions={sessions}
          currentSessionId={pipeline.currentSessionId}
          runStatus={viewModel.status}
          attention={attention && module !== "session"}
          onSwitchWorkspace={onSwitchWorkspace}
          onSelectWorkspace={onSelectWorkspace}
          onSelectSession={(id) => {
            setModule("session");
            void pipeline.switchSession?.(id);
          }}
          onNewSession={newSession}
          onDeleteSession={(id) => void requestDelete(id)}
          onRenameSession={pipeline.renameSession}
          onOpenSettings={() => setSettingsOpen(true)}
        />

        <main className={styles.main} aria-busy={switching || undefined}>
          <ConnectionBanner status={backend} />
          {module === "session" ? (
            <ConversationPane
              key={pipeline.currentSessionId}
              pipeline={pipeline}
              title={sessionTitle}
              headerActions={headerActions}
              onStopRun={() => void requestStop()}
              onNewSession={newSession}
              onOpenSettings={() => setSettingsOpen(true)}
              settingsVersion={settingsVersion}
            />
          ) : (
            <ModulePage
              key={module}
              module={module}
              actions={headerActions}
              attention={attention}
              onOpenConversation={() => setModule("session")}
            />
          )}
        </main>

        {rightPanel === "inspector" && module === "session" && (
          <Inspector
            viewModel={viewModel}
            draft={pipeline.activeDraft ?? null}
            onClose={() => setRightPanel(null)}
            onOpenModule={(next) => {
              setModule(next);
              setRightPanel(null);
            }}
          />
        )}
        {rightPanel === "log" && (
          <LogDrawer entries={pipeline.logs ?? []} onClose={() => setRightPanel(null)} onClear={pipeline.clearLogs} />
        )}
      </div>

      <SettingsPanel
        open={settingsOpen}
        onClose={() => {
          setSettingsOpen(false);
          setSettingsVersion((value) => value + 1);
        }}
        onSaved={() => setSettingsVersion((value) => value + 1)}
        onSwitchWorkspace={() => {
          setSettingsOpen(false);
          onSwitchWorkspace();
        }}
      />
      {confirmDialog}
    </ResearchSignalContext.Provider>
  );
}
