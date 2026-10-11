import { useEffect, useRef } from "react";
import { AppShell } from "./components/shell/AppShell";
import { BackendUnavailable } from "./components/shell/BackendUnavailable";
import { WorkspacePicker } from "./components/shell/WorkspacePicker";
import { BrandMark } from "./components/common/BrandMark";
import { useBackendStatus } from "./hooks/useBackendStatus";
import { usePipeline } from "./hooks/usePipeline";
import { useWorkspace } from "./hooks/useWorkspace";

type Workspace = ReturnType<typeof useWorkspace>;

/** 一个工作区的完整会话；按 root 作为 key，切换工作区时整体重置状态。 */
function WorkspaceSession({ workspace }: { workspace: Workspace }) {
  const pipeline = usePipeline(workspace.currentRoot, workspace.requestedSessionId);
  return (
    <AppShell
      currentRoot={workspace.currentRoot}
      recentRoots={workspace.recentRoots}
      switching={workspace.switching}
      onSwitchWorkspace={workspace.openPicker}
      onSelectWorkspace={(path, sessionId) => void workspace.switchTo(path, sessionId)}
      pipeline={pipeline}
    />
  );
}

/** 根应用：后端连接门 + 工作区选择门 + 统一研究工作区。 */
function App() {
  const workspace = useWorkspace();
  const backend = useBackendStatus();
  const unreachable = workspace.ready && workspace.currentRoot === null && workspace.error !== null;
  const reloadRef = useRef(workspace.reload);
  reloadRef.current = workspace.reload;

  // The gateway came up after a failed first load: pick the session back up automatically.
  // Keyed on the two flags only, so a failing reload cannot retrigger itself.
  useEffect(() => {
    if (unreachable && backend === "open") void reloadRef.current();
  }, [backend, unreachable]);

  if (!workspace.ready) {
    return (
      <div className="app-loading" role="status">
        <BrandMark size={36} />
        <span className="app-loading__spinner" aria-hidden />
        <span>正在连接 Athena…</span>
      </div>
    );
  }

  if (unreachable && backend !== "open") {
    return <BackendUnavailable error={workspace.error} onRetry={workspace.reload} />;
  }

  if (workspace.pickerOpen) {
    return (
      <WorkspacePicker
        currentRoot={workspace.currentRoot}
        recentRoots={workspace.recentRoots}
        error={workspace.error}
        switching={workspace.switching}
        browsing={workspace.browsing}
        onSelect={(path) => void workspace.switchTo(path)}
        onBrowse={() => void workspace.browse()}
        onContinue={workspace.closePicker}
      />
    );
  }

  return <WorkspaceSession key={workspace.currentRoot ?? "default"} workspace={workspace} />;
}

export default App;
