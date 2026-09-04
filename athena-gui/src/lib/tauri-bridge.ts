import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { wsBackend } from "./ws-backend";
import { toBackendError } from "./rpc-error";

/** Supervisor-style task understanding returned by the backend after parsing. */
export interface TaskUnderstanding {
  title: string;
  dataset: string;
  target: string;
  task_type: string;
  primary_metric: string;
  direction: string;
  evaluation_plan: string;
  /** Present on the old GUI-side understanding; backend Supervisor state may omit it. */
  needs_configuration?: boolean;
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

/** Known pipeline event channel names subscribed by the frontend.

  Python emits ``state``, ``output``, ``clarification``, and ``human_request``
  over the WebSocket relay.
*/
export const PIPELINE_EVENT_NAMES = [
  "state",
  "output",
  "clarification",
  "human_request",
] as const;

/** True when running inside a Tauri webview (detected via window internals). */
const hasTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/** Exported backend connectivity flag (true only in the Tauri desktop shell). */
export const isTauri = hasTauri;

async function invokeWithDomainErrors<T>(promise: Promise<T>): Promise<T> {
  try {
    return await promise;
  } catch (err) {
    throw toBackendError(err);
  }
}

/** Route one RPC: Tauri → ``invoke(command)``, browser preview → WebSocket call. */
function rpc<T>(method: string, params?: Record<string, unknown>, tauriCmd = method): Promise<T> {
  const call = hasTauri
    ? invoke<T>(tauriCmd, params)
    : wsBackend.call(method, params ?? {}) as Promise<T>;
  return invokeWithDomainErrors(call);
}

/** Normalize snake_case WebSocket fields to camelCase Tauri command arguments. */
function invokeOrRequest<T>(
  method: string,
  params: Record<string, unknown>,
  tauriCmd = method,
): Promise<T> {
  const call = !hasTauri
    ? wsBackend.call(method, params) as Promise<T>
    : invoke<T>(
        tauriCmd,
        Object.fromEntries(
          Object.entries(params).map(([key, value]) => [
            key.replace(/_([a-z])/g, (_, char: string) => char.toUpperCase()),
            value,
          ]),
        ),
      );
  return invokeWithDomainErrors(call);
}

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

/** Kick off PREPARE after the user confirms the latest clarification draft. */
export function startSearch(
  draftId: string,
  revision: number,
  acknowledgeUnresolved: boolean,
): Promise<unknown> {
  return invokeOrRequest("start_search", {
    draft_id: draftId,
    revision,
    acknowledge_unresolved: acknowledgeUnresolved,
  });
}

/** Pause the active search loop (changes are preserved). */
export function pauseSearch(): Promise<unknown> {
  return rpc("pause", undefined, "pause_search");
}

/** Resume a previously paused search loop. */
export function resumeSearch(): Promise<unknown> {
  return rpc("resume", undefined, "resume_search");
}

/** Stop the active search loop and finalize the run. */
export function stopSearch(): Promise<unknown> {
  return rpc("stop", undefined, "stop_search");
}

/** Send a raw control command or prose to the runtime (e.g. "/select <id>"). */
export function sendControl(text: string): Promise<unknown> {
  return rpc("message", { text });
}

/** Run the validation workflow on the current SOTA model. */
export function startValidation(): Promise<unknown> {
  return rpc("start_validation");
}

/** Generate a final report from the current run results. */
export function generateReport(): Promise<unknown> {
  return rpc("generate_report");
}

/** Fetch the full research tree from the backend. */
export function treeGet(): Promise<{ tree: ResearchTreeData }> {
  return rpc<{ tree: ResearchTreeData }>("tree_get");
}

/** Persist the current research tree to disk. */
export function treeSave(): Promise<{ saved: boolean; path: string }> {
  return rpc<{ saved: boolean; path: string }>("tree_save");
}

/** Load a previously saved research tree from disk. */
export function treeLoad(): Promise<{ loaded: boolean; tree: ResearchTreeData }> {
  return rpc<{ loaded: boolean; tree: ResearchTreeData }>("tree_load");
}

/** One persisted session record from the runtime session log (``sessions/default.jsonl``).

  ``type === "user"`` marks a user message; anything else is an ``OutputEvent``
  (supervisor/agent/tool text). ``seq`` is the global monotonic order used to
  de-duplicate and order messages when restoring the conversation.
*/
export interface SessionRecord {
  type: string;
  seq: number;
  /** 消息身份：同一条消息的所有投影共用它。升级前写下的记录没有该字段。 */
  message_id?: string | null;
  text?: string;
  source?: string;
  channel?: string;
  plan?: string | null;
  tool?: string | null;
  session_id?: string | null;
  scope?: string | null;
  scope_id?: string | null;
  [key: string]: unknown;
}

/** Session ids in the current workspace (most-recent first) + the one last opened here. */
export function sessionsList(): Promise<{ sessions: string[]; active: string | null }> {
  return rpc<{ sessions: string[]; active: string | null }>("sessions_list");
}

/** List session ids in an arbitrary workspace directory (without switching runtime). */
export function sessionsListFor(path: string): Promise<{ sessions: string[] }> {
  return rpc<{ sessions: string[] }>("sessions_list_for", { path });
}

/** Switch the active session; returns its transcript and the updated session list
  * (the backend recycles the blank session being left behind). */
export function sessionSwitch(
  sessionId: string,
): Promise<{ records: SessionRecord[]; sessions?: string[] }> {
  if (hasTauri) return invoke("session_switch", { sessionId });
  return wsBackend.call("session_switch", { session_id: sessionId }) as Promise<{
    records: SessionRecord[];
    sessions?: string[];
  }>;
}

/** Delete a session (its transcript + state) and return the updated id list.
  * ``default`` has no directory of its own, so deleting it resets the workspace's
  * default session instead of removing the workspace. */
export function sessionDelete(
  sessionId: string,
): Promise<{ deleted: boolean; sessions: string[] }> {
  if (hasTauri) return invoke("session_delete", { sessionId });
  return wsBackend.call("session_delete", { session_id: sessionId }) as Promise<{
    deleted: boolean;
    sessions: string[];
  }>;
}

/* ── Hypothesis graph ─────────────────────────────────────────────── */

/** A single hypothesis-graph node keyed by hypothesis id. */
export interface HypGraphNode {
  id: string;
  experiment_id: string | null;
  statement: string;
  status: string;
  priority: number;
  order: number | null;
  supersedes: string[];
  parent_id: string | null;
  sources: string[];
  primary: number | null;
  sota: boolean;
}

/** A directed edge in the hypothesis graph (lineage or supersedes). */
export interface HypGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: "lineage" | "supersedes";
  label: string;
}

export interface HypGraph {
  nodes: HypGraphNode[];
  edges: HypGraphEdge[];
}

export interface GraphAlgorithmParam {
  name: string;
  type: string;
  required: boolean;
}

export interface GraphAlgorithmInfo {
  name: string;
  label: string;
  params: GraphAlgorithmParam[];
}

/* ── Settings ─────────────────────────────────────────────────────── */

export interface ModelConnection {
  provider: string;
  base_url: string;
  model_name: string;
  llm_api_key: string;
}

export interface ComputeHostSettings {
  name: string;
  ssh: string;
  gpus: number[] | "auto";
  max_leases: number;
}

export interface ComputeSettings {
  mode: "local" | "ssh";
  placement: "pack" | "spread" | "homogeneous";
  fallback: "never" | "ask";
  gpus_per_experiment: number;
  queue_timeout_s: number | null;
  hosts: ComputeHostSettings[];
}

export interface GuiSettings {
  project_root: string;
  model: string | null;
  concurrency: number;
  search_limit: number;
  ideation: "ideageneration" | "baseline" | "debate";
  direction: "maximize" | "minimize";
  tolerance: number;
  auto_validate: boolean;
  skip_validate: boolean;
  manual_mode: boolean;
  phase: string;
  status: string;
  data_root: string | null;
  experiment_timeout_s: number;
  compute: ComputeSettings;
  model_connection: ModelConnection;
}

export const DEFAULT_GUI_SETTINGS: GuiSettings = {
  project_root: "",
  model: null,
  concurrency: 1,
  search_limit: 0,
  ideation: "ideageneration",
  direction: "maximize",
  tolerance: 0,
  auto_validate: false,
  skip_validate: false,
  manual_mode: false,
  phase: "idle",
  status: "idle",
  data_root: null,
  experiment_timeout_s: 3600,
  compute: {
    mode: "local",
    placement: "pack",
    fallback: "never",
    gpus_per_experiment: 1,
    queue_timeout_s: null,
    hosts: [],
  },
  model_connection: {
    provider: "deepseek",
    base_url: "",
    model_name: "",
    llm_api_key: "",
  },
};

/* ── LLM I/O traces ───────────────────────────────────────────────── */

export interface TraceSummary {
  agent_id: string;
  file: string;
  size_bytes: number;
  messages: number;
  updated_at: number;
}

export type TraceRole = "system" | "user" | "assistant" | "tool";

export interface TraceMessage {
  seq: number;
  ts: string;
  role: TraceRole;
  kind: string;
  content: string;
  args?: Record<string, unknown>;
}

export interface TraceDetail {
  agent_id: string;
  messages: TraceMessage[];
}

/* ── Experiments ──────────────────────────────────────────────────── */

export interface ExperimentPlan {
  kind: string;
  change: string;
  rubrics: string[];
  run_config_ref: string;
  budget: Record<string, unknown>;
  acceptance_rule: string;
}

export interface GitWorkBranch {
  path: string;
  branch: string;
  base_commit: string;
}

export interface EvalResult {
  experiment_id: string;
  primary: number;
  secondary: Record<string, number>;
  per_sample: string;
}

export interface ComparisonVerdict {
  winner: "baseline" | "candidate" | "tie";
  p_value: number;
}

/** One experiment record (the ``experiment`` object, id-stamped by the backend). */
export interface ExperimentRecord {
  id: string;
  parent_id: string | null;
  hypothesis_id: string;
  commit: string;
  plan: ExperimentPlan;
  gitwork: GitWorkBranch;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  eval: EvalResult | null;
  verdict: ComparisonVerdict | null;
  artifacts: Record<string, string>;
  error: string | null;
}

/** The v3 hypothesis object (superset of the v2 ``HypothesisData``). */
export interface HypothesisObject {
  id: string;
  statement: string;
  intervention: string;
  expected_effect: string;
  status: "PROPOSED" | "SUPPORTED" | "REFUTED" | "REJECTED";
  evidence_refs: string[];
  parent_id: string | null;
  supersedes: string[];
  priority: number;
  order: number | null;
  patience: number;
  turn_limit: number | null;
  sources: string[];
}

export interface ExperimentDetail {
  experiment: ExperimentRecord;
  hypothesis: HypothesisObject;
  sota: boolean;
  path?: string[];
  descendants?: string[];
}

/* ── New RPC wrappers ─────────────────────────────────────────────── */

/** Fetch the projected runtime state snapshot. */
export function stateGet(): Promise<Record<string, unknown>> {
  return rpc<Record<string, unknown>>("state_get");
}

/** One base64-encoded figure from the EDA report. */
export interface EdaFigure {
  name: string;
  mime: string;
  data: string;
}

/** EDA report (Markdown + figures) for the workspace preview panel. */
export interface EdaReport {
  eda_dir: string | null;
  report: string | null;
  report_name?: string | null;
  figures: EdaFigure[];
}

/** Fetch the EDA Markdown report and figures from the backend. */
export function edaReport(): Promise<EdaReport> {
  return rpc<EdaReport>("eda_report");
}

/** Fetch the hypothesis graph (nodes + lineage/supersedes edges). */
export function hypothesisGraph(): Promise<HypGraph> {
  return rpc<HypGraph>("hypothesis_graph");
}

/** List available graph algorithms and their parameter schemas. */
export function graphAlgorithms(): Promise<{ algorithms: GraphAlgorithmInfo[] }> {
  return rpc<{ algorithms: GraphAlgorithmInfo[] }>("graph_algorithms");
}

/** Run one graph algorithm against the current tree. */
export function graphAlgorithm(
  name: string,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return rpc<Record<string, unknown>>("graph_algorithm", { name, params });
}

/** Fetch the current runtime settings. */
export function settingsGet(): Promise<GuiSettings> {
  return rpc<GuiSettings>("settings_get");
}

/** Apply a whitelisted settings patch and return the updated settings. */
export function settingsSet(patch: Partial<GuiSettings>): Promise<GuiSettings> {
  return rpc<GuiSettings>("settings_set", { patch });
}

/** Switch the runtime to a new project directory and return the fresh settings. */
export function setProjectRoot(path: string): Promise<GuiSettings> {
  return rpc<GuiSettings>("set_project_root", { path });
}

/** List available LLM agent traces. */
export function tracesList(): Promise<{ traces: TraceSummary[] }> {
  return rpc<{ traces: TraceSummary[] }>("traces_list");
}

/** Read one agent trace as a normalized message timeline. */
export function traceGet(agentId: string): Promise<TraceDetail> {
  return rpc<TraceDetail>("trace_get", { agent_id: agentId });
}

/** List experiments, optionally filtered by plan kind. */
export function experimentsList(kind?: string): Promise<{ experiments: ExperimentDetail[] }> {
  return rpc<{ experiments: ExperimentDetail[] }>("experiments_list", kind ? { kind } : undefined);
}

/** Fetch one experiment detail with its ancestor path and descendants. */
export function experimentGet(experimentId: string): Promise<ExperimentDetail | null> {
  return rpc<ExperimentDetail | null>("experiment_get", { experiment_id: experimentId });
}

/** Advance an experiment status via the allowed-transition table. */
export function experimentTransition(
  experimentId: string,
  status: string,
  error?: string,
): Promise<{ experiment: ExperimentDetail | null }> {
  return rpc<{ experiment: ExperimentDetail | null }>("experiment_transition", { experiment_id: experimentId, status, error });
}

/** Mark a successful baseline/search experiment as the current SOTA. */
export function experimentSetSota(experimentId: string): Promise<{ sota_id: string }> {
  return rpc<{ sota_id: string }>("experiment_set_sota", { experiment_id: experimentId });
}

/* ── Clarification / confirmed context ─────────────────────────────── */

/** One selectable option for a human request. */
export interface HumanChoice {
  label: string;
  value: string;
}

/** One typed human request (shared with Python and Rust). */
export interface HumanRequest {
  request_id: string;
  session_id: string;
  scope_id: string;
  scope_kind: string;
  prompt: string;
  choices?: HumanChoice[] | null;
  allow_custom: boolean;
  allow_skip: boolean;
  created_at: string;
  expires_at: string;
}

/** Typed client reply: exactly one of choice / text / skip. */
export type HumanReply =
  | { kind: "choice"; value: string }
  | { kind: "text"; text: string }
  | { kind: "skip" };

/** Structured task understanding used by the pre-run clarification draft. */
export interface ClarificationUnderstanding {
  title: string;
  dataset: string | null;
  target: string | null;
  task_type: string;
  primary_metric: string | null;
  direction: string | null;
  evaluation_plan: string | null;
}

/** One persisted clarification answer/outcome. */
export interface ClarificationAnswer {
  request_id: string;
  question: string;
  outcome: "choice" | "text" | "skip" | "timeout" | "cancelled";
  value: string | null;
  choice_label: string | null;
  answered_at: string;
}

/** A missing or unresolved understanding field. */
export interface UnresolvedItem {
  field: string;
  reason: string;
  critical: boolean;
}

/** Retryable failure information shown on FAILED drafts. */
export interface ClarificationFailure {
  code: string;
  message: string;
  retryable: boolean;
}

/** Clarification draft DTO mirrored from the Python controller. */
export interface ClarificationDraftDto {
  schema_version?: 1;
  draft_id: string;
  revision: number;
  session_id: string;
  original_task: string;
  status: string;
  understanding: ClarificationUnderstanding;
  answers: ClarificationAnswer[];
  revisions?: unknown[];
  unresolved: UnresolvedItem[];
  pending_request?: HumanRequest | null;
  failure: ClarificationFailure | null;
  questions_asked: number;
  created_at: string;
  updated_at: string;
}

/** Start or resume clarification for the active session. */
export function taskClarificationStart(
  task: string,
): Promise<{ draft_id: string; revision: number; status: string }> {
  return invokeOrRequest("task_clarification_start", { task });
}

/** Read the authoritative latest draft. */
export function taskClarificationGet(
  draftId: string,
): Promise<ClarificationDraftDto> {
  return invokeOrRequest("task_clarification_get", { draft_id: draftId });
}

/** Retry a failed clarification draft. */
export function taskClarificationRetry(
  draftId: string,
  revision: number,
): Promise<ClarificationDraftDto> {
  return invokeOrRequest("task_clarification_retry", {
    draft_id: draftId,
    revision,
  });
}

/** Ask the model to revise the draft from the displayed revision. */
export function taskClarificationRevise(
  draftId: string,
  revision: number,
  instruction: string,
): Promise<ClarificationDraftDto> {
  return invokeOrRequest("task_clarification_revise", {
    draft_id: draftId,
    revision,
    instruction,
  });
}

/** Cancel the active clarification draft. */
export function taskClarificationCancel(
  draftId: string,
  revision: number,
): Promise<ClarificationDraftDto> {
  return invokeOrRequest("task_clarification_cancel", {
    draft_id: draftId,
    revision,
  });
}

/* ── Human requests ───────────────────────────────────────────────── */

/** List the supervisor's outstanding human questions. */
export function humanPending(): Promise<{ requests: HumanRequest[] }> {
  return invokeOrRequest("human_pending", {});
}

/** Answer one outstanding human question with one typed reply. */
export function humanReply(
  requestId: string,
  reply: HumanReply,
): Promise<{ replied: boolean }> {
  return invokeOrRequest("human_reply", {
    request_id: requestId,
    reply,
  });
}

/** Subscribe to all known pipeline event channels and invoke the handler on each emission. */
export function subscribeToPipelineEvents(
  handler: (evt: PipelineEvent) => void,
): Promise<UnlistenFn[]> {
  if (!hasTauri) {
    // 浏览器预览：直接连 Python 网关的 WebSocket（无 Tauri shell）。
    const unlisten = wsBackend.subscribe((kind, data) => {
      handler({ kind, data: (data ?? {}) as PipelineEvent["data"] });
    });
    return Promise.resolve([unlisten]);
  }

  return Promise.all(
    PIPELINE_EVENT_NAMES.map((name) =>
      listen<PipelineEvent>(name, (event) => {
        handler(normalizePipelineEvent(name, event.payload));
      }),
    ),
  );
}
