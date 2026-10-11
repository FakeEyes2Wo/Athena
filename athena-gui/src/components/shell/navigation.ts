import type { IconName } from "../common/Icon";
import type { ModuleKey } from "../../types/ui";

/** One top-level view reachable from the sidebar. */
export interface ModuleDef {
  key: ModuleKey;
  label: string;
  icon: IconName;
  /** One-line explanation shown in the page header. */
  description: string;
}

/** Sidebar views; ``session`` is the conversation, the rest are research pages. */
export const MODULES: ModuleDef[] = [
  { key: "session", label: "对话", icon: "chat", description: "描述任务、回答澄清问题并跟踪研究进展" },
  { key: "research-tree", label: "研究树", icon: "tree", description: "实验谱系、假设之间的关系与图算法分析" },
  { key: "experiments", label: "实验", icon: "experiments", description: "全部实验的状态、指标与变更内容" },
  { key: "eda", label: "数据分析", icon: "chart", description: "PREPARE 阶段生成的探索性数据分析报告" },
  { key: "llm-io", label: "模型轨迹", icon: "llm", description: "各 Agent 与模型之间的完整输入输出记录" },
  { key: "report", label: "报告", icon: "report", description: "研究摘要与可导出的 Markdown 报告" },
];

/** key → module definition. */
export const MODULE_BY_KEY = Object.fromEntries(MODULES.map((m) => [m.key, m])) as Record<
  ModuleKey,
  ModuleDef
>;
