import type { IconName } from "../common/Icon";
import type { ContextPanelKey, ModuleKey } from "../../types/ui";

/** 一个功能轨模块：key、展示名、图标，以及它在主工作区呈现的面板。 */
export interface ModuleDef {
  key: ModuleKey;
  label: string;
  icon: IconName;
  /** 主工作区面板；null 表示聊天（会话模块）。 */
  panel: ContextPanelKey | null;
}

/** 功能轨主入口（设置固定在底部，单独处理）。 */
export const MODULES: ModuleDef[] = [
  { key: "session", label: "会话", icon: "chat", panel: null },
  { key: "research-tree", label: "研究树", icon: "tree", panel: "research-tree" },
  { key: "experiments", label: "实验", icon: "experiments", panel: "experiments" },
  { key: "eda", label: "数据分析", icon: "chart", panel: "eda-report" },
  { key: "llm-io", label: "模型轨迹", icon: "llm", panel: "llm-io" },
  { key: "report", label: "报告", icon: "report", panel: "report" },
];

export const SETTINGS_MODULE: ModuleDef = {
  key: "settings",
  label: "设置",
  icon: "settings",
  panel: "settings",
};

/** key → 模块定义（O(1) 查询，替代每次 ``MODULES.find``）。 */
export const MODULE_BY_KEY = Object.fromEntries(
  [...MODULES, SETTINGS_MODULE].map((m) => [m.key, m]),
) as Record<ModuleKey, ModuleDef>;

/** 上下文侧栏里可打开的详情视图（在抽屉中呈现）。 */
export interface NavItem {
  key: ContextPanelKey;
  label: string;
  icon: IconName;
}

export const RELATED_PANELS: Record<ModuleKey, NavItem[]> = {
  session: [
    { key: "metrics", label: "指标", icon: "metrics" },
    { key: "experiment-log", label: "实验日志", icon: "clock" },
    { key: "hypothesis-graph", label: "假设图", icon: "network" },
  ],
  "research-tree": [
    { key: "hypothesis-graph", label: "假设图", icon: "network" },
    { key: "algorithms", label: "图算法", icon: "algorithms" },
    { key: "metrics", label: "指标", icon: "metrics" },
  ],
  experiments: [
    { key: "experiment-log", label: "实验日志", icon: "clock" },
    { key: "metrics", label: "指标", icon: "metrics" },
    { key: "diff", label: "差异", icon: "diff" },
    { key: "files", label: "文件", icon: "files" },
  ],
  eda: [
    { key: "metrics", label: "指标", icon: "metrics" },
    { key: "files", label: "文件", icon: "files" },
  ],
  "llm-io": [],
  report: [
    { key: "metrics", label: "指标", icon: "metrics" },
    { key: "experiment-log", label: "实验日志", icon: "clock" },
  ],
  settings: [],
};
