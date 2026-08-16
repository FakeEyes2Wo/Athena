import { AppShell } from "./components/shell/AppShell";
import { WorkspacePicker } from "./components/shell/WorkspacePicker";
import { usePipeline } from "./hooks/usePipeline";
import { useWorkspace } from "./hooks/useWorkspace";

type Workspace = ReturnType<typeof useWorkspace>;

/** 一个工作区的完整会话；按 root 作为 key，切换工作区时整体重置状态。 */
function WorkspaceSession({ workspace }: { workspace: Workspace }) {
  const pipeline = usePipeline(workspace.currentRoot);
  return (
    <AppShell
      currentRoot={workspace.currentRoot}
      recentRoots={workspace.recentRoots}
      onSwitchWorkspace={workspace.openPicker}
      onSelectWorkspace={(path) => void workspace.switchTo(path)}
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

  if (workspace.pickerOpen) {
    return (
      <WorkspacePicker
        currentRoot={workspace.currentRoot}
        recentRoots={workspace.recentRoots}
        error={workspace.error}
        switching={workspace.switching}
        onSelect={(path) => void workspace.switchTo(path)}
        onContinue={workspace.closePicker}
      />
    );
  }

  return <WorkspaceSession key={workspace.currentRoot ?? "default"} workspace={workspace} />;
}

export default App;
