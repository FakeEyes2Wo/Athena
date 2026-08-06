import type { ContextPanelKey, PipelineViewModel } from "../../types/ui";

interface RightRailProps {
  viewModel: PipelineViewModel;
  openPanel(panel: ContextPanelKey): void;
}

/** Right sidebar with summary cards for pipeline status, budget, best result, and report. */
export function RightRail({ viewModel, openPanel }: RightRailProps) {
  const { rightRail } = viewModel;

  return (
    <section className="right-rail">
      <button
        className="summary-card"
        onClick={() => openPanel("experiment-log")}
        aria-label="状态"
      >
        <span>状态</span>
        <strong>{viewModel.phase}</strong>
        <small>{viewModel.status}</small>
      </button>

      <button
        className="summary-card"
        onClick={() => openPanel("metrics")}
        aria-label="预算"
      >
        <span>预算</span>
        <strong>{rightRail.budgetRemaining}</strong>
        <small>连续 {rightRail.noImproveStreak}</small>
      </button>

      <button
        className="summary-card"
        onClick={() => openPanel("research-tree")}
        aria-label="最佳结果"
      >
        <span>最佳结果</span>
        <strong>{rightRail.bestPrimary != null ? rightRail.bestPrimary.toFixed(4) : "--"}</strong>
        <small>{rightRail.latestExperimentId ?? "暂无实验"}</small>
      </button>

      <button
        className="summary-card"
        onClick={() => openPanel("report")}
        aria-label="打开报告"
      >
        <span>研究</span>
        <strong>打开报告</strong>
        <small>查看详情</small>
      </button>
    </section>
  );
}
