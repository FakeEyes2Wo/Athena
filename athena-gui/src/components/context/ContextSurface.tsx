import type { PipelineViewModel } from "../../types/ui";
import MetricChart from "../MetricChart";
import ResearchTreeViz from "../ResearchTreeViz";
import DiffViewer from "../DiffViewer";
import FileTree from "../FileTree";

interface ContextSurfaceProps {
  viewModel: PipelineViewModel;
  closePanel(): void;
}

const PANEL_TITLES: Record<string, string> = {
  metrics: "指标",
  "research-tree": "研究树",
  "experiment-log": "实验日志",
  diff: "差异",
  files: "文件",
  report: "报告",
};

/** Expandable detail panel that shows the active context view (metrics, tree, diff, etc.). */
export function ContextSurface({ viewModel, closePanel }: ContextSurfaceProps) {
  if (!viewModel.contextSurface.isOpen) {
    return <section className="context-surface context-surface--closed">详情已收起</section>;
  }

  const panel = viewModel.contextSurface.activePanel;
  const title = PANEL_TITLES[panel] ?? panel;

  return (
    <section className="context-surface context-surface--open">
      <header className="context-surface__header">
        <h2>{title}</h2>
        <button onClick={() => closePanel()} aria-label="关闭详情">
          关闭
        </button>
      </header>
      {panel === "metrics" && <MetricChart />}
      {panel === "research-tree" && <ResearchTreeViz />}
      {panel === "diff" && <DiffViewer />}
      {panel === "files" && <FileTree />}
      {panel === "experiment-log" && <div className="detail-panel">实验日志</div>}
      {panel === "report" && <div className="detail-panel">报告预览</div>}
    </section>
  );
}
