import type {
  BudgetState,
  ClarificationAnswer,
  ClarificationDraftDto,
  ClarificationFailure,
  ClarificationUnderstanding,
  ExperimentEvent,
  HumanChoice,
  HumanReply,
  HumanRequest,
  TaskUnderstanding,
  UnresolvedItem,
} from "../lib/tauri-bridge";

export type {
  ClarificationAnswer,
  ClarificationDraftDto,
  ClarificationFailure,
  ClarificationUnderstanding,
  HumanChoice,
  HumanReply,
  HumanRequest,
  UnresolvedItem,
};

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

/**
 * Frontend clarification/run state.
 *
 * IDLE -> CLARIFYING -> READY_FOR_CONFIRMATION -> CONFIRMING -> RUNNING
 *                       |-> revise -> CLARIFYING
 *                       |-> cancel -> IDLE
 *                       `-> retry -> CLARIFYING (from FAILED)
 */
export type ClarificationStatus =
  | "IDLE"
  | "CLARIFYING"
  | "READY_FOR_CONFIRMATION"
  | "CONFIRMING"
  | "RUNNING"
  | "FAILED";

export type UIMessageKind =
  | "text"
  | "intent-preview"
  | "run-status"
  | "result-summary"
  | "error";

/** Frontend projection of the authoritative clarification draft. */
export interface ClarificationPreview {
  draftId: string;
  /** First scoped activity id observed before the canonical draft id arrives. */
  optimisticScopeId?: string;
  revision: number;
  status: ClarificationStatus;
  understanding: ClarificationUnderstanding;
  answers: ClarificationAnswer[];
  unresolved: UnresolvedItem[];
  failure: ClarificationFailure | null;
}

export type UIMessagePreview = ClarificationPreview | TaskUnderstanding;

export interface UIMessage {
  id: string;
  role: "user" | "athena";
  kind: UIMessageKind;
  content: string;
  /** New clarification projection, or a legacy supervisor-style understanding. */
  preview?: UIMessagePreview;
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
  /** Canonical runtime session for a fully scoped output event. */
  sessionId?: string;
  /** Generic runtime output scope, such as ``task_understanding``. */
  scope?: string;
  /** Identity of the scoped workflow instance. */
  scopeId?: string;
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
  /** Current research plan ids emitted by the backend. */
  plans: Array<{ id: string }>;
  /** PROPOSED hypotheses awaiting manual selection. */
  pending: Array<{ id: string; statement: string }>;
  /** True when the runtime is in manual hypothesis-selection mode. */
  manual: boolean;
  /** Whether the latest authoritative state permits resuming this durable task. */
  resumeAvailable: boolean;
  /** The backend's durable resume classification, when supplied. */
  resumeReason: string | null;
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
    plans: [],
    pending: [],
    manual: false,
    resumeAvailable: false,
    resumeReason: null,
  };
}
