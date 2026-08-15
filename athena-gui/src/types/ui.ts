import type { BudgetState, ExperimentEvent, TaskUnderstanding } from "../lib/tauri-bridge";

export const CONTEXT_PANELS = [
  "metrics",
  "research-tree",
  "experiment-log",
  "diff",
  "files",
  "eda-report",
  "report",
  "hypothesis-graph",
  "algorithms",
  "settings",
  "llm-io",
  "experiments",
] as const;

export type ContextPanelKey = (typeof CONTEXT_PANELS)[number];

/** 顶层模块（功能轨入口）：会话 + 六个研究视图 + 设置。 */
export type ModuleKey =
  | "session"
  | "research-tree"
  | "experiments"
  | "eda"
  | "llm-io"
  | "report"
  | "settings";

export type PipelineStatus = "idle" | "running" | "paused" | "completed" | "error";

export type UIMessageKind =
  | "text"
  | "intent-preview"
  | "run-status"
  | "result-summary"
  | "error";

export interface UIMessage {
  id: string;
  role: "user" | "athena";
  kind: UIMessageKind;
  content: string;
  preview?: TaskUnderstanding;
  /** Original task text the user typed (carried to ``start_search`` on confirm). */
  task?: string;
  /** True once the user confirmed and started this intent. */
  started?: boolean;
  budget?: BudgetState;
  event?: ExperimentEvent;
  /** Backend output source: supervisor / agent / tool. */
  source?: string;
  /** Function-call tool name (present on tool-call records). */
  tool?: string;
  /** Backend output channel: text / stdout / stderr / error. */
  channel?: string;
  /** Backend plan id (e.g. "ideator-0"), used to keep streaming lanes separate. */
  plan?: string;
}

export interface RightRailSummary {
  budgetRemaining: number;
  searchAttempts: number;
  searchLimit: number;
  /** 已成功的实验次数（对齐 TUI 的 ``search.successes``）。 */
  successes: number;
  /** 当前并发 worker 数（对齐 TUI 的 ``search.concurrency``）。 */
  workers: number;
  bestPrimary: number | null;
  latestExperimentId: string | null;
}

export interface PipelineViewModel {
  phase: string;
  status: PipelineStatus;
  messages: UIMessage[];
  rightRail: RightRailSummary;
  /** PROPOSED hypotheses awaiting manual selection. */
  pending: Array<{ id: string; statement: string }>;
  /** True when the runtime is in manual hypothesis-selection mode. */
  manual: boolean;
}

/** Returns a default PipelineViewModel with idle state and empty messages. */
export function createEmptyPipelineViewModel(): PipelineViewModel {
  return {
    phase: "idle",
    status: "idle",
    messages: [],
    rightRail: {
      budgetRemaining: 0,
      searchAttempts: 0,
      searchLimit: 0,
      successes: 0,
      workers: 0,
      bestPrimary: null,
      latestExperimentId: null,
    },
    pending: [],
    manual: false,
  };
}
