import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/** ML task preview returned by the backend after intent parsing. */
export interface TaskPreview {
  task_type: string;
  data_type: string;
  target_vars: string[];
  primary_metric: string;
  direction: string;
  needs_configuration: boolean;
}

export interface BudgetState {
  remaining: number;
  no_improve_streak: number;
  is_exhausted: boolean;
}

export interface ExperimentEvent {
  kind: string;
  data: {
    experiment_id?: string;
    hypothesis?: string;
    primary?: number;
    winner?: string;
    p_value?: number;
    phase?: string;
    remaining?: number;
    no_improve_streak?: number;
    status?: string;
    [key: string]: unknown;
  };
}

export type PipelineEvent = ExperimentEvent;

export interface HypothesisData {
  statement: string;
  intervention: string;
  expected_effect: string;
  status: "PROPOSED" | "SUPPORTED" | "REFUTED" | "REJECTED";
  evidence_refs: string[];
  patience_grant: number;
  patience_evidence_ref: string | null;
  id: string;
  parent_id: string | null;
  sources: string[];
}

export interface ExperimentPlanData {
  kind: string;
  change: string;
  rubrics: string[];
  run_config_ref: string;
  budget: Record<string, unknown>;
  acceptance_rule: string;
}

export interface GitWorkBranchData {
  path: string;
  branch: string;
  base_commit: string;
}

export interface EvalResultData {
  experiment_id: string;
  primary: number;
  secondary: Record<string, number>;
  per_sample: string;
}

export interface ComparisonVerdictData {
  winner: "baseline" | "candidate" | "tie";
  p_value: number;
}

export interface ResearchExperimentData {
  parent_id: string | null;
  hypothesis_id: string;
  commit: string;
  plan: ExperimentPlanData;
  gitwork: GitWorkBranchData;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  eval: EvalResultData | null;
  verdict: ComparisonVerdictData | null;
  artifacts: Record<string, string>;
  error: string | null;
}

/** Serialized research tree data structure. */
export interface ResearchTreeData {
  version: 2;
  sota_id: string | null;
  hypotheses: Record<string, HypothesisData>;
  experiments: Record<string, ResearchExperimentData>;
}

export const EMPTY_RESEARCH_TREE: ResearchTreeData = {
  version: 2,
  sota_id: null,
  hypotheses: {},
  experiments: {},
};

/** Known pipeline event channel names subscribed by the frontend. */
export const PIPELINE_EVENT_NAMES = [
  "chat/token",
  "chat/intent_parsed",
  "experiment/started",
  "experiment/completed",
  "budget/update",
  "phase/change",
  "tree/updated",
] as const;

/** True when running inside a Tauri webview (detected via window internals). */
const hasTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/** Ensures every pipeline event has a kind and data payload, defaulting to the channel name. */
function normalizePipelineEvent(
  channel: (typeof PIPELINE_EVENT_NAMES)[number],
  payload: Partial<PipelineEvent> | undefined,
): PipelineEvent {
  return {
    kind: payload?.kind ?? channel,
    data: payload?.data ?? {},
  };
}

/** Send a user message to the backend and receive the parsed task preview. */
export async function sendMessage(msg: string): Promise<TaskPreview> {
  return invoke("send_message", { message: msg });
}

/** Kick off the automated ML search loop. */
export async function startSearch(config: Record<string, unknown>): Promise<unknown> {
  if (!hasTauri) return { ok: true };
  return invoke("start_search", { config });
}

/** Pause the active search loop (changes are preserved). */
export async function pauseSearch(): Promise<unknown> {
  if (!hasTauri) return { ok: true };
  return invoke("pause_search");
}

/** Resume a previously paused search loop. */
export async function resumeSearch(): Promise<unknown> {
  if (!hasTauri) return { ok: true };
  return invoke("resume_search");
}

/** Stop the active search loop and finalize the run. */
export async function stopSearch(): Promise<unknown> {
  if (!hasTauri) return { ok: true };
  return invoke("stop_search");
}

/** Run the validation workflow on the current SOTA model. */
export async function startValidation(): Promise<unknown> {
  if (!hasTauri) return { ok: true };
  return invoke("start_validation");
}

/** Generate a final report from the current run results. */
export async function generateReport(): Promise<unknown> {
  if (!hasTauri) return { ok: true };
  return invoke("generate_report");
}

/** Fetch the full research tree from the backend. */
export async function treeGet(): Promise<{ tree: ResearchTreeData }> {
  if (!hasTauri) return { tree: EMPTY_RESEARCH_TREE };
  return invoke("tree_get");
}

/** Persist the current research tree to disk. */
export async function treeSave(): Promise<{ saved: boolean; path: string }> {
  if (!hasTauri) return { saved: false, path: "" };
  return invoke("tree_save");
}

/** Load a previously saved research tree from disk. */
export async function treeLoad(): Promise<{ loaded: boolean; tree: ResearchTreeData }> {
  if (!hasTauri) return { loaded: false, tree: EMPTY_RESEARCH_TREE };
  return invoke("tree_load");
}

/** Subscribe to all known pipeline event channels and invoke the handler on each emission. */
export function subscribeToPipelineEvents(
  handler: (evt: PipelineEvent) => void,
): Promise<UnlistenFn[]> {
  if (!hasTauri) {
    queueMicrotask(() => {
      handler({ kind: "phase/change", data: { phase: "idle" } });
      handler({ kind: "budget/update", data: { remaining: 10, no_improve_streak: 0 } });
    });
    return Promise.resolve([() => {}]);
  }

  return Promise.all(
    PIPELINE_EVENT_NAMES.map((name) =>
      listen<PipelineEvent>(name, (event) => {
        handler(normalizePipelineEvent(name, event.payload));
      }),
    ),
  );
}

/** Subscribe to experiment completion events only. */
export function onEvent(handler: (evt: ExperimentEvent) => void): Promise<UnlistenFn> {
  if (!hasTauri) return Promise.resolve(() => {});
  return listen<ExperimentEvent>("experiment/completed", (event) => {
    handler(normalizePipelineEvent("experiment/completed", event.payload));
  });
}
