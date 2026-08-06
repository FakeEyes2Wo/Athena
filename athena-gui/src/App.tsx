import { AppShell } from "./components/shell/AppShell";
import SessionSidebar from "./components/shell/SessionSidebar";
import { ConversationPane } from "./components/conversation/ConversationPane";
import { RightRail } from "./components/right-rail/RightRail";
import { ContextSurface } from "./components/context/ContextSurface";
import { usePipeline } from "./hooks/usePipeline";
import "./App.css";

/** Root application component that composes the shell layout with pipeline state. */
function App() {
  const pipeline = usePipeline();

  return (
    <AppShell
      sidebar={<SessionSidebar />}
      conversation={<ConversationPane pipeline={pipeline} />}
      rightRail={<RightRail viewModel={pipeline.viewModel} openPanel={pipeline.openPanel} />}
      contextSurface={<ContextSurface viewModel={pipeline.viewModel} closePanel={pipeline.closePanel} />}
    />
  );
}

export default App;
