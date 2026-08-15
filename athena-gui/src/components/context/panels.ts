import { lazy, type ComponentType } from "react";

// Panels are code-split so heavy dependencies (reactflow, recharts, d3-dag,
// monaco) only load when their panel is first opened.
const MetricChart = lazy(() => import("../MetricChart"));
const ResearchTreeViz = lazy(() => import("../ResearchTreeViz"));
const DiffViewer = lazy(() => import("../DiffViewer"));
const FileTree = lazy(() => import("../FileTree"));
const HypothesisGraphViz = lazy(() => import("../HypothesisGraphViz"));
const AlgorithmsPanel = lazy(() => import("../AlgorithmsPanel"));
const SettingsPanel = lazy(() => import("../SettingsPanel"));
const LLMIOPanel = lazy(() => import("../LLMIOPanel"));
const ExperimentManager = lazy(() => import("../ExperimentManager"));
const ExperimentLog = lazy(() => import("../ExperimentLog"));
const ReportPanel = lazy(() => import("../ReportPanel"));
const EDAPreview = lazy(() => import("../EDAPreview"));

export const PANELS: Record<string, ComponentType> = {
  metrics: MetricChart,
  "research-tree": ResearchTreeViz,
  "experiment-log": ExperimentLog,
  diff: DiffViewer,
  files: FileTree,
  "eda-report": EDAPreview,
  report: ReportPanel,
  "hypothesis-graph": HypothesisGraphViz,
  algorithms: AlgorithmsPanel,
  settings: SettingsPanel,
  "llm-io": LLMIOPanel,
  experiments: ExperimentManager,
};

export const PANEL_TITLES: Record<string, string> = {
  metrics: "指标",
  "research-tree": "研究树",
  "experiment-log": "实验日志",
  diff: "差异",
  files: "文件",
  "eda-report": "EDA 报告",
  report: "报告",
  "hypothesis-graph": "假设图",
  algorithms: "图算法",
  settings: "设置",
  "llm-io": "LLM 轨迹",
  experiments: "实验管理",
};

export const PANEL_KEYS = Object.keys(PANELS);
