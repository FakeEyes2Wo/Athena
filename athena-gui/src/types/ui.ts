import type { BudgetState, ExperimentEvent, TaskPreview } from "../lib/tauri-bridge";

export const CONTEXT_PANELS = [
  "metrics",
  "research-tree",
  "experiment-log",
  "diff",
  "files",
  "report",
] as const;

export type ContextPanelKey = (typeof CONTEXT_PANELS)[number];

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
  preview?: TaskPreview;
  budget?: BudgetState;
  event?: ExperimentEvent;
}

export interface RightRailSummary {
  budgetRemaining: number;
  noImproveStreak: number;
  bestPrimary: number | null;
  latestExperimentId: string | null;
}

export interface PipelineViewModel {
  phase: string;
  status: PipelineStatus;
  messages: UIMessage[];
  contextSurface: {
    isOpen: boolean;
    activePanel: ContextPanelKey;
  };
  rightRail: RightRailSummary;
}

/** Returns a default PipelineViewModel with idle state and empty messages. */
export function createEmptyPipelineViewModel(): PipelineViewModel {
  return {
    phase: "idle",
    status: "idle",
    messages: [],
    contextSurface: {
      isOpen: false,
      activePanel: "metrics",
    },
    rightRail: {
      budgetRemaining: 0,
      noImproveStreak: 0,
      bestPrimary: null,
      latestExperimentId: null,
    },
  };
}
