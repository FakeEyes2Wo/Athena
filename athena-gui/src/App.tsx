import { AppShell } from "./components/shell/AppShell";
import { WorkspacePicker } from "./components/shell/WorkspacePicker";
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

/** 根应用：工作区选择门 + 统一研究工作区。 */
function App() {
  const workspace = useWorkspace();

  if (!workspace.ready) {
    return <div className="app-loading">加载中…</div>;
  }

  if (workspace.pickerOpen || workspace.error !== null) {
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
