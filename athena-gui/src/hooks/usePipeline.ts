import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  hasClarificationMessage,
  renderClarificationOutcome,
  renderClarificationQuestion,
  renderClarificationReply,
} from "../lib/clarification-conversation";
import { errorMessage } from "../lib/errors";
import * as bridge from "../lib/tauri-bridge";
import {
  persistWorkspaceSessions,
  type SessionSummary,
} from "../lib/workspaceStorage";
import {
  createEmptyPipelineViewModel,
  type ClarificationDraftDto,
  type ClarificationPreview,
  type ClarificationStatus,
  type HumanReply,
  type HumanRequest,
  type PipelineViewModel,
  type UIMessage,
} from "../types/ui";

export interface LogEntry {
  id: string;
  at: number;
  kind: string;
  source?: string;
  channel?: string;
  plan?: string;
  tool?: string;
  text: string;
}

const MAX_LOG_ENTRIES = 500;
let optimisticSessionCounter = 0;

function nextOptimisticSessionId(): string {
  optimisticSessionCounter += 1;
  return `s-${Date.now()}-${optimisticSessionCounter}`;
}

/** Maps the backend runtime status to the frontend pipeline status. */
const RUNTIME_STATUS_MAP: Record<string, PipelineViewModel["status"]> = {
  IDLE: "idle",
  RUNNING: "running",
  WAITING: "paused",
  COMPLETED: "completed",
  STOPPED: "completed",
  FAILED: "error",
};

/** Slash-command help shown by ``/help`` (mirrors the TUI overlay text). */
const HELP_TEXT = "可用命令：/pause /resume /stop /manual /auto /select <id> /help";

/** Stop confirmation that tolerates jsdom (where ``window.confirm`` throws). */
function confirmStop(): boolean {
  try {
    if (typeof window === "undefined" || typeof window.confirm !== "function") return true;
    return window.confirm("停止当前研究执行？");
  } catch {
    return true;
  }
}

function loadTitles(key: string): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(key) ?? "{}");
  } catch {
    return {};
  }
}

function saveTitle(key: string, id: string, title: string): void {
  try {
    const titles = loadTitles(key);
    titles[id] = title;
    localStorage.setItem(key, JSON.stringify(titles));
  } catch {
    // 非致命：标题仅用于展示。
  }
}

/** 会话标题按工作区隔离的 localStorage 键（与 usePipeline 内一致）。 */
export function sessionTitlesKey(workspaceRoot?: string | null): string {
  return `athena-session-titles:${workspaceRoot ?? "default"}`;
}

/** 读取某个工作区的会话标题（供跨工作区分组展示复用）。 */
export function loadSessionTitles(workspaceRoot?: string | null): Record<string, string> {
  return loadTitles(sessionTitlesKey(workspaceRoot));
}

/** Draft statuses the frontend exposes directly (backend may also emit CONFIRMED/CANCELLED). */
function normalizeClarificationStatus(status: string | undefined): ClarificationStatus | null {
  if (!status) return null;
  if (status === "CONFIRMED" || status === "RUNNING") return "RUNNING";
  if (status === "CANCELLED") return "IDLE";
  if (status === "CLARIFYING" || status === "READY_FOR_CONFIRMATION" || status === "CONFIRMING" || status === "FAILED") {
    return status as ClarificationStatus;
  }
  return null;
}

/** Read an id from either the snake_case RPC DTO or a camelCase bridge return. */
function draftIdOf(value: { draft_id?: string; draftId?: string } | null | undefined): string | undefined {
  return value?.draft_id ?? value?.draftId;
}

/** Convert the backend draft DTO into the frontend preview projection. */
function toClarificationPreview(draft: ClarificationDraftDto & { draftId?: string }): ClarificationPreview {
  return {
    draftId: draftIdOf(draft) ?? "",
    revision: draft.revision,
    status: normalizeClarificationStatus(draft.status) ?? "CLARIFYING",
    understanding: {
      title: draft.understanding?.title ?? "",
      dataset: draft.understanding?.dataset ?? null,
      target: draft.understanding?.target ?? null,
      task_type: draft.understanding?.task_type ?? "other",
      primary_metric: draft.understanding?.primary_metric ?? null,
      direction: draft.understanding?.direction ?? null,
      evaluation_plan: draft.understanding?.evaluation_plan ?? null,
    },
    answers: Array.isArray(draft.answers) ? draft.answers : [],
    unresolved: Array.isArray(draft.unresolved) ? draft.unresolved : [],
    failure: draft.failure ?? null,
  };
}

/** Is this RPC result a full draft (rather than a start summary)? */
function isFullDraft(value: unknown): value is ClarificationDraftDto {
  return typeof value === "object" && value !== null && typeof (value as ClarificationDraftDto).understanding === "object";
}

/** Find the latest intent-preview message whose draftId matches (or the latest one). */
function findPreviewIndex(
  messages: Array<{ id: string; kind: string; preview?: unknown }>,
  draftId?: string,
): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.kind !== "intent-preview" || !message.preview) continue;
    const preview = message.preview as Partial<ClarificationPreview>;
    if (draftId) {
      if (preview.draftId === draftId) return i;
    } else if (preview.draftId !== undefined) {
      return i;
    }
  }
  return -1;
}

/** Apply a draft update to the most relevant intent-preview message. */
function replacePreview(
  current: PipelineViewModel,
  draftId: string | undefined,
  patch: (preview: ClarificationPreview) => ClarificationPreview,
): PipelineViewModel {
  let idx = findPreviewIndex(current.messages, draftId);
  // A newly submitted placeholder has draftId ""; once the start RPC returns a
  // real id, the authoritative draft replaces that placeholder.
  if (idx < 0 && draftId) idx = findPreviewIndex(current.messages, undefined);
  if (idx < 0) return current;
  const message = current.messages[idx];
  const currentPreview = message.preview as ClarificationPreview | undefined;
  if (!currentPreview) return current;
  const nextPreview = patch(currentPreview);
  return {
    ...current,
    messages: current.messages.map((m, i) => (i === idx ? { ...m, preview: nextPreview } : m)),
  };
}

/** 从 task understanding（TaskUnderstanding）推导会话标题。 */
function titleFromTask(preview: { title?: string; task_type?: string; primary_metric?: string | null }): string {
  if (preview.title?.trim()) return preview.title.trim();
  const parts = [preview.task_type, preview.primary_metric].filter((p) => p && p !== "other");
  return parts.length ? parts.join(" · ") : "新会话";
}

interface OutputBatch {
  messages: UIMessage[];
  messageIndex: Map<string, number>;
  deltas: Map<string, string>;
}

function createOutputBatch(messages: UIMessage[]): OutputBatch {
  return {
    messages: [...messages],
    messageIndex: new Map(messages.map((message, index) => [message.id, index])),
    deltas: new Map(),
  };
}

function applyOutputEventToBatch(
  batch: OutputBatch,
  event: bridge.PipelineEvent,
  replay: boolean,
): void {
  const { data } = event;
  const text = typeof data.text === "string" ? data.text : "";
  if (!text) return;
  const source = typeof data.source === "string" ? data.source : undefined;
  const tool = typeof data.tool === "string" ? data.tool : undefined;
  const channel = typeof data.channel === "string" ? data.channel : undefined;
  const plan = typeof data.plan === "string" ? data.plan : undefined;
  const id =
    typeof data.message_id === "string" && data.message_id
      ? data.message_id
      : `out-${typeof data.seq === "number" ? data.seq : 0}`;
  const target = batch.messageIndex.get(id);

  if (target !== undefined) {
    const existing = batch.messages[target];
    if (replay) {
      batch.messages[target] = { ...existing, content: text };
    } else {
      batch.deltas.set(id, `${batch.deltas.get(id) ?? ""}${text}`);
    }
    return;
  }

  if (!text.trim()) return;

  let message: UIMessage;
  const content = replay ? text : "";
  if (channel === "error") {
    message = { id, role: "athena", kind: "error", content };
  } else if (source === "tool" || channel === "stdout" || channel === "stderr") {
    message = { id, role: "athena", kind: "text", content, source: "tool", tool, channel, plan };
  } else if (tool) {
    message = { id, role: "athena", kind: "text", content, source, tool, plan };
  } else {
    message = { id, role: "athena", kind: "text", content, source, plan };
  }
  batch.messageIndex.set(id, batch.messages.length);
  batch.messages.push(message);
  if (!replay) batch.deltas.set(id, text);
}

function finishOutputBatch(batch: OutputBatch): UIMessage[] {
  for (const [id, delta] of batch.deltas) {
    const target = batch.messageIndex.get(id);
    if (target === undefined) continue;
    const existing = batch.messages[target];
    batch.messages[target] = { ...existing, content: existing.content + delta };
  }
  return batch.messages;
}

/** Applies output events with one messages copy and indexed message lookup. */
export function applyOutputEvents(
  current: PipelineViewModel,
  events: bridge.PipelineEvent[],
  replay = false,
): PipelineViewModel {
  const batch = createOutputBatch(current.messages);
  events.forEach((event) => applyOutputEventToBatch(batch, event, replay));
  return {
    ...current,
    rightRail: { ...current.rightRail },
    messages: finishOutputBatch(batch),
  };
}

/** Rebuilds the conversation from persisted records through the output batch reducer. */
function applyHistoryRecords(current: PipelineViewModel, records: bridge.SessionRecord[]): PipelineViewModel {
  const batch = createOutputBatch(current.messages);
  for (const record of records) {
    if (record.type === "user") {
      const text = typeof record.text === "string" ? record.text : "";
      if (!text.trim()) continue;
      const message: UIMessage = {
        id: `user-${record.seq}`,
        role: "user",
        kind: "text",
        content: text,
      };
      batch.messageIndex.set(message.id, batch.messages.length);
      batch.messages.push(message);
    } else {
      applyOutputEventToBatch(
        batch,
        { kind: "output", data: record as unknown as Record<string, unknown> },
        true,
      );
    }
  }
  return { ...current, messages: finishOutputBatch(batch) };
}

/**
 * Applies a single backend event (``state`` / ``output`` / ``clarification``) to the view model.
 *
 * ``replay`` 区分事件来源：实时订阅推的 agent 文本是增量 delta，落盘 transcript
 * 回放的是已合并的整条消息——两者形状相同，只有来源能区分该追加还是该替换。
 */
function applyPipelineEvent(
  current: PipelineViewModel,
  event: bridge.PipelineEvent,
  replay = false,
): PipelineViewModel {
  if (event.kind === "output") return applyOutputEvents(current, [event], replay);
  const next: PipelineViewModel = { ...current, rightRail: { ...current.rightRail } };
  const { data } = event;

  if (event.kind === "clarification") {
    const draft = (data as { draft?: ClarificationDraftDto }).draft;
    if (!draft) return next;
    const preview = toClarificationPreview(draft);
    return replacePreview(next, preview.draftId, () => preview);
  }

  if (event.kind === "state") {
    if (typeof data.phase === "string" && data.phase.trim()) {
      next.phase = data.phase;
    }
    if (typeof data.status === "string" && RUNTIME_STATUS_MAP[data.status]) {
      next.status = RUNTIME_STATUS_MAP[data.status];
    }
    const search = data.search as {
      attempts?: number;
      limit?: number;
      successes?: number;
      concurrency?: number;
    } | undefined;
    if (search) {
      if (typeof search.attempts === "number") next.rightRail.searchAttempts = search.attempts;
      if (typeof search.limit === "number") next.rightRail.searchLimit = search.limit;
      if (typeof search.successes === "number") next.rightRail.successes = search.successes;
      if (typeof search.concurrency === "number") next.rightRail.workers = search.concurrency;
      if (typeof search.attempts === "number" && typeof search.limit === "number") {
        next.rightRail.budgetRemaining = Math.max(0, search.limit - search.attempts);
      }
    }
    const sota = data.sota as { experiment?: string; metric?: number | null } | null | undefined;
    if (sota) {
      if (typeof sota.metric === "number") next.rightRail.bestPrimary = sota.metric;
      if (typeof sota.experiment === "string") next.rightRail.latestExperimentId = sota.experiment;
    }
    if (Array.isArray(data.plans)) {
      next.plans = data.plans as Array<{ id: string }>;
    }
    if (Array.isArray(data.pending)) {
      next.pending = data.pending as Array<{ id: string; statement: string }>;
    }
    if (typeof data.manual === "boolean") {
      next.manual = data.manual;
    }
    // Supervisor 结构化任务理解：更新最新一张意图预览卡（取代预解析占位值）。
    const understanding = data.task_understanding as bridge.TaskUnderstanding | undefined;
    if (understanding && typeof understanding === "object") {
      let idx = -1;
      for (let i = next.messages.length - 1; i >= 0; i -= 1) {
        if (next.messages[i].kind === "intent-preview") {
          idx = i;
          break;
        }
      }
      if (idx >= 0) {
        next.messages = next.messages.map((m, i) =>
          i === idx ? { ...m, preview: understanding } : m,
        );
      }
    }
    return next;
  }


  return next;
}

const {
  stateGet,
  sessionsList,
  sessionSwitch,
  sessionDelete,
  pauseSearch,
  resumeSearch,
  stopSearch,
  sendControl,
} = bridge;

function errorCode(err: unknown): string | undefined {
  if (typeof err === "object" && err !== null && "code" in err) {
    return String((err as { code: unknown }).code);
  }
  return undefined;
}

/**
 * Central pipeline state hook.
 * Manages the view model, subscribes to backend events, and exposes all user actions
 * (send prompt, clarify/confirm/revise/retry/cancel, pause/resume/stop, etc).
 */
export function usePipeline(
  workspaceRoot?: string | null,
  requestedSessionId?: string | null,
) {
  const [viewModel, setViewModel] = useState<PipelineViewModel>(createEmptyPipelineViewModel);
  const [clarificationStatus, setClarificationStatus] = useState<ClarificationStatus>("IDLE");
  const [sessions, setSessions] = useState<Array<{ id: string; title: string }>>([]);
  const [currentSessionId, setCurrentSessionId] = useState("default");
  const [humanRequests, setHumanRequests] = useState<HumanRequest[]>([]);
  const [humanPendingError, setHumanPendingError] = useState<string | null>(null);
  const [settlingRequestId, setSettlingRequestId] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const counter = useRef(0);
  const logCounter = useRef(0);
  const pendingOutputEventsRef = useRef<Array<{
    event: bridge.PipelineEvent;
    logEntry: LogEntry;
  }>>([]);
  const outputFrameRef = useRef<number | null>(null);
  const activeSessionIdRef = useRef(currentSessionId);
  const sessionRequestEpochRef = useRef(0);
  const pendingCreationsRef = useRef(new Map<string, Promise<unknown>>());
  const workspaceCacheRoot = workspaceRoot ?? "default";
  const authoritativeSessionsRef = useRef<{
    root: string;
    sessions: SessionSummary[];
  }>({ root: workspaceCacheRoot, sessions: [] });
  const mountedRef = useRef(true);
  const currentDraftIdRef = useRef<string | null>(null);
  const currentRevisionRef = useRef(-1);
  // start_search 已发出但后端首帧未回时，也算运行中。
  const runStarted = useRef(false);
  // 会话标题按工作区隔离：不同项目目录的会话标题互不串扰。
  const titlesKey = sessionTitlesKey(workspaceRoot);
  const settlingRequestRef = useRef<string | null>(null);
  const humanPollFailures = useRef(0);

  const nextId = useCallback((prefix: string) => {
    counter.current += 1;
    return `${prefix}-${counter.current}`;
  }, []);

  const createLogEntry = useCallback((event: bridge.PipelineEvent): LogEntry => {
    const data = event.data as Record<string, unknown> | undefined;
    logCounter.current += 1;
    return {
      id: `log-${logCounter.current}`,
      at: Date.now(),
      kind: event.kind,
      source: typeof data?.source === "string" ? data.source : undefined,
      channel: typeof data?.channel === "string" ? data.channel : undefined,
      plan: typeof data?.plan === "string" ? data.plan : undefined,
      tool: typeof data?.tool === "string" ? data.tool : undefined,
      text: typeof data?.text === "string" ? data.text : "",
    };
  }, []);

  const appendLogEntries = useCallback((entries: LogEntry[]) => {
    if (!entries.length) return;
    setLogs((prev) => {
      if (entries.length >= MAX_LOG_ENTRIES) return entries.slice(-MAX_LOG_ENTRIES);
      return [...prev.slice(-(MAX_LOG_ENTRIES - entries.length)), ...entries];
    });
  }, []);

  const appendLog = useCallback((event: bridge.PipelineEvent) => {
    appendLogEntries([createLogEntry(event)]);
  }, [appendLogEntries, createLogEntry]);

  const drainOutputEvents = useCallback(() => {
    const pending = pendingOutputEventsRef.current;
    if (!pending.length) return;
    pendingOutputEventsRef.current = [];
    setViewModel((prev) => applyOutputEvents(prev, pending.map(({ event }) => event)));
    appendLogEntries(pending.map(({ logEntry }) => logEntry));
  }, [appendLogEntries]);

  const discardPendingOutput = useCallback(() => {
    if (outputFrameRef.current !== null) {
      cancelAnimationFrame(outputFrameRef.current);
      outputFrameRef.current = null;
    }
    pendingOutputEventsRef.current = [];
  }, []);

  const flushPendingOutput = useCallback(() => {
    if (outputFrameRef.current !== null) {
      cancelAnimationFrame(outputFrameRef.current);
      outputFrameRef.current = null;
    }
    drainOutputEvents();
  }, [drainOutputEvents]);

  const queueOutputEvent = useCallback((event: bridge.PipelineEvent) => {
    pendingOutputEventsRef.current.push({ event, logEntry: createLogEntry(event) });
    if (outputFrameRef.current !== null) return;
    const frameId = requestAnimationFrame(() => {
      if (outputFrameRef.current !== frameId) return;
      outputFrameRef.current = null;
      if (!mountedRef.current) {
        pendingOutputEventsRef.current = [];
        return;
      }
      drainOutputEvents();
    });
    outputFrameRef.current = frameId;
  }, [createLogEntry, drainOutputEvents]);

  const clearLogs = useCallback(() => setLogs([]), []);

  const appendError = useCallback((text: string) => {
    setViewModel((prev) => ({
      ...prev,
      status: "error",
      messages: [
        ...prev.messages,
        { id: nextId("error"), role: "athena", kind: "error", content: text },
      ],
    }));
  }, [nextId]);

  // 会话列表一律由后端给：哪些会话存在（有 transcript / state.json）只有它知道。
  const applySessions = useCallback(
    (ids: string[] | undefined) => {
      if (!ids) return;
      const titles = loadTitles(titlesKey);
      const authoritative = ids.map((id) => ({ id, title: titles[id] ?? "新会话" }));
      authoritativeSessionsRef.current = { root: workspaceCacheRoot, sessions: authoritative };
      persistWorkspaceSessions(workspaceCacheRoot, authoritative);
      setSessions((current) => {
        const authoritativeIds = new Set(ids);
        const pending = current.filter(
          (session) =>
            pendingCreationsRef.current.has(session.id) &&
            !authoritativeIds.has(session.id),
        );
        return [...pending, ...authoritative];
      });
    },
    [titlesKey, workspaceCacheRoot],
  );

  // 重放会话记录：续接消息序列号并重建消息列表；可选清空现有消息。
  const restoreRecords = useCallback(
    (records: bridge.SessionRecord[], resetMessages: boolean) => {
      flushPendingOutput();
      if (records.length) {
        counter.current = Math.max(counter.current, ...records.map((r) => r.seq));
      }
      setViewModel((prev) =>
        applyHistoryRecords(resetMessages ? { ...prev, messages: [] } : prev, records),
      );
    },
    [flushPendingOutput],
  );

  // 更新会话标题（state + localStorage）。
  const renameSession = useCallback((id: string, title: string) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)));
    saveTitle(titlesKey, id, title);
    const authoritativeState = authoritativeSessionsRef.current;
    if (
      authoritativeState.root !== workspaceCacheRoot ||
      !authoritativeState.sessions.some((session) => session.id === id)
    ) return;
    const authoritative = authoritativeState.sessions.map((session) =>
      session.id === id ? { ...session, title } : session,
    );
    authoritativeSessionsRef.current = { root: workspaceCacheRoot, sessions: authoritative };
    persistWorkspaceSessions(workspaceCacheRoot, authoritative);
  }, [titlesKey, workspaceCacheRoot]);

  // The latest clarification preview stored on any intent-preview message.
  const latestPreview = useMemo(() => {
    const messages = viewModel.messages;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const message = messages[i];
      if (message.kind === "intent-preview" && message.preview && "draftId" in message.preview) {
        return message.preview as ClarificationPreview;
      }
    }
    return null;
  }, [viewModel.messages]);

  // Apply a full draft or a start summary to the latest matching preview message.
  const applyDraftToPreview = useCallback((
    draft: (ClarificationDraftDto & { draftId?: string }) | { draft_id?: string; draftId?: string; revision: number; status: string } | null,
    draftId?: string,
  ) => {
    if (!draft) return;
    const targetDraftId = draftId ?? draftIdOf(draft);
    const incomingRevision = (draft as { revision?: number }).revision ?? -1;
    if (targetDraftId) currentDraftIdRef.current = targetDraftId;
    if (incomingRevision >= 0) currentRevisionRef.current = incomingRevision;
    setViewModel((prev) => {
      const patch = (current: ClarificationPreview): ClarificationPreview => {
        if (isFullDraft(draft)) {
          return toClarificationPreview(draft);
        }
        return {
          ...current,
          draftId: targetDraftId || current.draftId,
          revision: incomingRevision,
          status: normalizeClarificationStatus((draft as { status?: string }).status) ?? current.status,
        };
      };
      const next = replacePreview(prev, targetDraftId || undefined, patch);
      // When clarification finishes, move the authoritative card to the end so
      // it appears after the Q&A conversation instead of above it.
      if (isFullDraft(draft) && draft.status !== "CLARIFYING") {
        const idx = findPreviewIndex(next.messages, targetDraftId);
        if (idx >= 0 && idx < next.messages.length - 1) {
          const message = next.messages[idx];
          next.messages = [...next.messages.filter((_, i) => i !== idx), message];
        }
      }
      return next;
    });
  }, []);

  /** Show one clarification question in the chat as part of the task-understanding flow. */
  const appendClarificationQuestion = useCallback((request: HumanRequest) => {
    const requestId = request.request_id;
    setViewModel((prev) => {
      if (hasClarificationMessage(prev.messages, requestId, "question")) return prev;
      return { ...prev, messages: [...prev.messages, renderClarificationQuestion(request)] };
    });
  }, []);

  /** Show the user's typed reply in the chat once the backend accepts it. */
  const appendClarificationReply = useCallback((requestId: string, reply: HumanReply) => {
    setViewModel((prev) => {
      if (hasClarificationMessage(prev.messages, requestId, "reply")) return prev;
      return { ...prev, messages: [...prev.messages, renderClarificationReply(requestId, reply)] };
    });
  }, []);

  /** Show server-generated timeout/cancellation outcomes that did not come from a user reply. */
  const appendClarificationOutcome = useCallback((
    requestId: string,
    outcome: { kind?: string; value?: string | null; choice_label?: string | null } | null | undefined,
  ) => {
    setViewModel((prev) => {
      const message = renderClarificationOutcome(requestId, outcome);
      if (!message || hasClarificationMessage(prev.messages, requestId, "outcome") || hasClarificationMessage(prev.messages, requestId, "reply")) return prev;
      return { ...prev, messages: [...prev.messages, message] };
    });
  }, []);

  useEffect(() => {
    activeSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (outputFrameRef.current !== null) {
        cancelAnimationFrame(outputFrameRef.current);
        outputFrameRef.current = null;
      }
      pendingOutputEventsRef.current = [];
      sessionRequestEpochRef.current += 1;
      pendingCreationsRef.current.clear();
    };
  }, []);

  // Subscribe to backend pipeline events on mount.
  useEffect(() => {
    let mounted = true;
    let unlisteners: Array<() => void> = [];
    const hydrationEpoch = ++sessionRequestEpochRef.current;

    subscribeToPipelineEvents((event) => {
      if (!mounted) return;

      if (event.kind === "output") {
        queueOutputEvent(event);
        return;
      }

      flushPendingOutput();

      if (event.kind === "clarification") {
        const payload = event.data as { session_id?: string; draft?: ClarificationDraftDto };
        if (payload.session_id && payload.session_id !== activeSessionIdRef.current) {
          appendLog(event);
          return;
        }
        const draft = payload.draft;
        if (draft) {
          const incomingDraftId = draftIdOf(draft);
          const currentRevision =
            currentDraftIdRef.current === incomingDraftId ? currentRevisionRef.current : -1;
          setViewModel((prev) => {
            const currentSoon = prev.messages.find((m) => {
              const p = m.preview as ClarificationPreview | undefined;
              return m.kind === "intent-preview" && p?.draftId === incomingDraftId;
            })?.preview as ClarificationPreview | undefined;
            if (currentSoon && draft.revision < currentSoon.revision) return prev;
            return replacePreview(prev, incomingDraftId, () => toClarificationPreview(draft));
          });
          if (currentRevision <= draft.revision) {
            setClarificationStatus(normalizeClarificationStatus(draft.status) ?? "CLARIFYING");
          }
        }
        appendLog(event);
        return;
      }

      if (event.kind === "human_request") {
        const payload = event.data as {
          action?: "created" | "settled";
          session_id?: string;
          request?: HumanRequest;
          request_id?: string;
          outcome?: { kind?: string; value?: string | null; choice_label?: string | null } | null;
        };
        // Ignore questions belonging to another session; the frontend must not
        // render or settle another conversation's request.
        if (payload.session_id && payload.session_id !== activeSessionIdRef.current) {
          appendLog(event);
          return;
        }
        if (payload?.action === "created" && payload.request) {
          setHumanRequests((prev) =>
            prev.some((r) => r.request_id === payload.request?.request_id)
              ? prev
              : [...prev, payload.request as HumanRequest],
          );
          appendClarificationQuestion(payload.request);
          setHumanPendingError(null);
        } else if (payload?.action === "settled" && payload.request_id) {
          setHumanRequests((prev) => prev.filter((r) => r.request_id !== payload.request_id));
          if (payload.outcome?.kind === "timeout" || payload.outcome?.kind === "cancelled") {
            appendClarificationOutcome(payload.request_id, payload.outcome);
          }
        }
        appendLog(event);
        return;
      }

      setViewModel((prev) => applyPipelineEvent(prev, event));
      appendLog(event);
    }).then((fns) => {
      if (!mounted) { fns.forEach((fn) => fn()); return; }
      unlisteners = fns;
    });

    // Seed the view model from the current projected state so a fresh session
    // (including after a workspace switch remount) never starts blank while
    // waiting for the next streamed event.
    stateGet()
      .then((snapshot) => {
        if (!mounted || hydrationEpoch !== sessionRequestEpochRef.current) return;
        setViewModel((prev) => applyPipelineEvent(prev, { kind: "state", data: snapshot }));
      })
      .catch(() => {
        // Non-fatal: the live subscription above will still drive updates.
      });

    // 断点续传：列出会话 → 切到本工作区上次用的会话 → 重放其 transcript。
    sessionsList()
      .then(({ sessions: list, active }) => {
        if (!mounted || hydrationEpoch !== sessionRequestEpochRef.current) return null;
        // active 由后端按工作区记住；从没用过的工作区列表为空，落在 default 上，
        // 它同样要等到真跑起来才会出现在侧栏里。
        const target = requestedSessionId && list.includes(requestedSessionId)
          ? requestedSessionId
          : active ?? "default";
        return sessionSwitch(target).then((result) => ({ target, list, result }));
      })
      .then((hydration) => {
        if (!hydration || !mounted || hydrationEpoch !== sessionRequestEpochRef.current) return;
        const { target, list, result } = hydration;
        applySessions(result.sessions ?? list);
        setCurrentSessionId(target);
        restoreRecords(result.records, true);
      })
      .catch(() => {
        // 非致命：无历史或后端不支持时保持空白会话。
      });

    return () => { mounted = false; unlisteners.forEach((fn) => fn()); };
  }, [appendClarificationOutcome, appendClarificationQuestion, appendLog, applySessions, applyDraftToPreview, flushPendingOutput, queueOutputEvent, requestedSessionId, restoreRecords]);

  // Poll for outstanding human questions. Pre-run states use a low-frequency
  // fallback (10s); once RUNNING we keep the existing 1.5s recovery poll.
  useEffect(() => {
    const preRun =
      clarificationStatus === "CLARIFYING" ||
      clarificationStatus === "READY_FOR_CONFIRMATION" ||
      clarificationStatus === "CONFIRMING";
    const running = clarificationStatus === "RUNNING" || viewModel.status === "running";

    if (!preRun && !running) {
      setHumanRequests([]);
      setHumanPendingError(null);
      humanPollFailures.current = 0;
      return;
    }

    let cancelled = false;
    const interval = running ? 1500 : 10000;
    const poll = async () => {
      try {
        const { requests } = await bridge.humanPending();
        if (!cancelled) {
          setHumanRequests(requests);
          requests.forEach((request) => appendClarificationQuestion(request));
          setHumanPendingError(null);
          humanPollFailures.current = 0;
        }
      } catch (err) {
        humanPollFailures.current += 1;
        if (humanPollFailures.current >= 3 && !cancelled) {
          setHumanPendingError(`无法获取待确认问题：${errorMessage(err)}`);
        }
      }
    };
    void poll();
    const timer = setInterval(poll, interval);
    return () => { cancelled = true; clearInterval(timer); };
  }, [appendClarificationQuestion, clarificationStatus, viewModel.status]);

  // The backend is the only owner of task understanding. When its authoritative
  // state event fills the preview card, use that understanding to name the
  // session (the frontend never computes understanding itself).
  const latestUnderstanding = useMemo(() => {
    for (let i = viewModel.messages.length - 1; i >= 0; i -= 1) {
      const message = viewModel.messages[i];
      if (message.kind !== "intent-preview" || !message.preview) continue;
      const understanding =
        "draftId" in message.preview ? message.preview.understanding : message.preview;
      if (understanding?.title?.trim()) return understanding;
    }
    return null;
  }, [viewModel.messages]);

  useEffect(() => {
    if (latestUnderstanding) {
      renameSession(currentSessionId, titleFromTask(latestUnderstanding));
    }
  }, [currentSessionId, latestUnderstanding, renameSession]);

  // User actions.

  /** Start (or resume) clarification for a task. This never starts research. */
  const startClarification = useCallback(async (task: string) => {
    const content = task.trim();
    if (!content) return;
    // User input supersedes any mount-time transcript hydration still in flight.
    sessionRequestEpochRef.current += 1;

    const previewId = nextId("preview");
    const title = content.slice(0, 40) || "新任务";
    setClarificationStatus("CLARIFYING");
    setViewModel((prev) => ({
      ...prev,
      phase: "idle",
      status: "idle",
      messages: [
        ...prev.messages,
        { id: nextId("user"), role: "user", kind: "text", content },
        {
          id: previewId,
          role: "athena",
          kind: "intent-preview",
          content: "任务理解中…",
          preview: {
            draftId: "",
            revision: 0,
            status: "CLARIFYING",
            understanding: {
              title: "",
              dataset: null,
              target: null,
              task_type: "other",
              primary_metric: null,
              direction: null,
              evaluation_plan: null,
            },
            answers: [],
            unresolved: [],
            failure: null,
          },
          task: content,
          started: false,
        },
      ],
    }));
    renameSession(currentSessionId, title);

    try {
      const result = await bridge.taskClarificationStart(content);
      applyDraftToPreview(result, draftIdOf(result));
      setClarificationStatus(
        normalizeClarificationStatus(result.status) ?? "CLARIFYING",
      );
    } catch (err) {
      setClarificationStatus("FAILED");
      appendError(errorMessage(err));
      throw err;
    }
  }, [appendError, applyDraftToPreview, currentSessionId, nextId, renameSession]);

  /** Confirm the latest draft and start research only after the gated RPC succeeds. */
  const confirmDraft = useCallback(async (acknowledgeUnresolved: boolean) => {
    const preview = latestPreview;
    const draftId = preview?.draftId || currentDraftIdRef.current || "";
    const revision = preview?.revision ?? currentRevisionRef.current;
    if (!draftId || revision < 0) return;
    setClarificationStatus("CONFIRMING");
    setViewModel((prev) => replacePreview(prev, draftId, (p) => ({ ...p, status: "CONFIRMING" })));

    try {
      await bridge.startSearch(draftId, revision, acknowledgeUnresolved);
      runStarted.current = true;
      setClarificationStatus("RUNNING");
      setViewModel((prev) => ({
        ...replacePreview(prev, draftId, (p) => ({ ...p, status: "RUNNING" })),
        phase: "PREPARE",
        status: "running",
        messages: prev.messages.map((m) => {
          const messagePreview = m.preview as ClarificationPreview | undefined;
          if (m.kind === "intent-preview" && messagePreview?.draftId === draftId) {
            return { ...m, started: true };
          }
          return m;
        }),
      }));
      const sessionRefreshEpoch = sessionRequestEpochRef.current;
      void sessionsList()
        .then(({ sessions: list }) => {
          if (
            !mountedRef.current ||
            sessionRefreshEpoch !== sessionRequestEpochRef.current
          ) return;
          applySessions(list);
        })
        .catch(() => {});
    } catch (err) {
      const code = errorCode(err);
      if (code === "stale_revision") {
        try {
          const fresh = await bridge.taskClarificationGet(draftId);
          applyDraftToPreview(fresh, draftId);
          setClarificationStatus(normalizeClarificationStatus(fresh.status) ?? "CLARIFYING");
        } catch (reloadErr) {
          setClarificationStatus("FAILED");
          appendError(errorMessage(reloadErr));
        }
        throw err;
      }

      setClarificationStatus("FAILED");
      appendError(errorMessage(err));
      throw err;
    }
  }, [appendError, applyDraftToPreview, latestPreview, nextId, applySessions]);

  /** Send a revision instruction and return the draft to CLARIFYING. */
  const reviseDraft = useCallback(async (instruction: string) => {
    const preview = latestPreview;
    if (!preview || !preview.draftId) return;
    const text = instruction.trim();
    if (!text) return;

    setClarificationStatus("CLARIFYING");
    try {
      const draft = await bridge.taskClarificationRevise(preview.draftId, preview.revision, text);
      applyDraftToPreview(draft, preview.draftId);
      setClarificationStatus(normalizeClarificationStatus(draft.status) ?? "CLARIFYING");
    } catch (err) {
      setClarificationStatus("FAILED");
      throw err;
    }
  }, [applyDraftToPreview, latestPreview]);

  /** Retry a retryable FAILED draft. */
  const retryDraft = useCallback(async () => {
    const preview = latestPreview;
    if (!preview || !preview.draftId) return;

    setClarificationStatus("CLARIFYING");
    try {
      const draft = await bridge.taskClarificationRetry(preview.draftId, preview.revision);
      applyDraftToPreview(draft, preview.draftId);
      setClarificationStatus(normalizeClarificationStatus(draft.status) ?? "CLARIFYING");
    } catch (err) {
      setClarificationStatus("FAILED");
      throw err;
    }
  }, [applyDraftToPreview, latestPreview]);

  /** Cancel the current draft; backend settles any pending request and returns to IDLE. */
  const cancelDraft = useCallback(async () => {
    const preview = latestPreview;
    if (!preview || !preview.draftId) return;

    try {
      await bridge.taskClarificationCancel(preview.draftId, preview.revision);
      setClarificationStatus("IDLE");
      setHumanRequests([]);
      setViewModel((prev) => ({
        ...prev,
        status: "idle",
        messages: prev.messages.map((m) => {
          const messagePreview = m.preview as ClarificationPreview | undefined;
          if (m.kind === "intent-preview" && messagePreview?.draftId === preview.draftId) {
            return { ...m, preview: { ...messagePreview, status: "IDLE" as ClarificationPreview["status"] } };
          }
          return m;
        }),
      }));
    } catch (err) {
      setClarificationStatus("FAILED");
      throw err;
    }
  }, [latestPreview]);

  /** Unified human reply dispatch with double-submit protection. */
  const replyToHumanRequest = useCallback(async (requestId: string, reply: HumanReply) => {
    if (settlingRequestRef.current === requestId) return;
    settlingRequestRef.current = requestId;
    setSettlingRequestId(requestId);
    try {
      await bridge.humanReply(requestId, reply);
      appendClarificationReply(requestId, reply);
      setHumanRequests((prev) => prev.filter((r) => r.request_id !== requestId));
      setHumanPendingError(null);
    } catch (err) {
      const code = errorCode(err);
      if (code === "stale_request" || code === "expired_request" || code === "duplicate_reply") {
        try {
          const { requests } = await bridge.humanPending();
          setHumanRequests(requests);
        } catch {
          // Keep the existing request list; the typed reason below is still visible.
        }
        setHumanPendingError(`该问题已失效（${code}）：${errorMessage(err)}`);
      } else {
        setHumanPendingError(errorMessage(err));
      }
      throw err;
    } finally {
      settlingRequestRef.current = null;
      setSettlingRequestId(null);
    }
  }, [appendClarificationReply]);

  const answerHuman = useCallback((requestId: string, answer: string) => {
    const text = answer.trim();
    if (!text) return Promise.resolve();
    return replyToHumanRequest(requestId, { kind: "text", text });
  }, [replyToHumanRequest]);

  const chooseHumanAnswer = useCallback((requestId: string, value: string) => {
    return replyToHumanRequest(requestId, { kind: "choice", value });
  }, [replyToHumanRequest]);

  const skipHumanAnswer = useCallback((requestId: string) => {
    return replyToHumanRequest(requestId, { kind: "skip" });
  }, [replyToHumanRequest]);

  const sendPrompt = useCallback(async (msg: string) => {
    const content = msg.trim();
    if (!content) return;

    // Slash command surface (mirrors the TUI command surface).
    if (content.startsWith("/")) {
      const [cmd, ...rest] = content.split(/\s+/);
      const arg = rest.join(" ").trim();
      switch (cmd) {
        case "/pause":
          await pauseSearch();
          setViewModel((prev) => ({ ...prev, status: "paused" }));
          return;
        case "/resume":
          await resumeSearch();
          setViewModel((prev) => ({ ...prev, status: "running" }));
          return;
        case "/stop":
          if (!confirmStop()) return;
          await stopSearch();
          setViewModel((prev) => ({ ...prev, status: "completed" }));
          return;
        case "/manual":
          await sendControl("/manual");
          setViewModel((prev) => ({ ...prev, manual: true }));
          return;
        case "/auto":
          await sendControl("/auto");
          setViewModel((prev) => ({ ...prev, manual: false }));
          return;
        case "/select":
          if (arg) await sendControl(`/select ${arg}`);
          return;
        case "/help":
          setViewModel((prev) => ({
            ...prev,
            messages: [
              ...prev.messages,
              { id: nextId("help"), role: "athena", kind: "text", content: HELP_TEXT },
            ],
          }));
          return;
        default:
          // Unknown slash command → fall through as a normal message.
          break;
      }
    }

    // 运行中/暂停中才把文本当 Supervisor 指导；status 首帧可能虚报，须以真实活动为准。
    const runInProgress =
      (viewModel.status === "running" || viewModel.status === "paused") &&
      (runStarted.current ||
        viewModel.plans.length > 0 ||
        viewModel.rightRail.searchAttempts > 0 ||
        viewModel.rightRail.latestExperimentId !== null);
    if (runInProgress) {
      try {
        const response = (await sendControl(content)) as { response?: unknown };
        const reply = typeof response?.response === "string" ? response.response : "";
        if (reply) {
          setViewModel((prev) => ({
            ...prev,
            messages: [
              ...prev.messages,
              { id: nextId("athena"), role: "athena", kind: "text", content: reply },
            ],
          }));
        }
      } catch (err) {
        appendError(errorMessage(err));
        throw err;
      }
      return;
    }

    await startClarification(content);
  }, [appendError, nextId, startClarification, viewModel]);

  const pauseRun = useCallback(async () => {
    await pauseSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "paused",
    }));
  }, []);

  const resumeRun = useCallback(async () => {
    await resumeSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "running",
    }));
  }, []);

  const stopRun = useCallback(async () => {
    await stopSearch();
    setViewModel((prev) => ({
      ...prev,
      status: "completed",
    }));
  }, []);

  const toggleMode = useCallback(async () => {
    const nextManual = !viewModel.manual;
    await sendControl(nextManual ? "/manual" : "/auto");
    setViewModel((prev) => ({ ...prev, manual: nextManual }));
  }, [viewModel.manual]);

  const newSession = useCallback(() => {
    // 新建一个独立会话（后端 transcript 按 session_id 分文件），并清空视图。
    // 旧会话若是空白的，由后端在切换时回收，前端不做判定。
    discardPendingOutput();
    const id = nextOptimisticSessionId();
    runStarted.current = false;
    setClarificationStatus("IDLE");
    saveTitle(titlesKey, id, "新会话");
    const requestEpoch = ++sessionRequestEpochRef.current;
    const creation = sessionSwitch(id);
    pendingCreationsRef.current.set(id, creation);
    void creation.then(
      (result) => {
        if (pendingCreationsRef.current.get(id) === creation) {
          pendingCreationsRef.current.delete(id);
        }
        if (mountedRef.current && requestEpoch === sessionRequestEpochRef.current) {
          applySessions(result.sessions);
        }
      },
      () => {
        // Keep the optimistic row on failure so the user can retry or delete it.
        if (pendingCreationsRef.current.get(id) === creation) {
          pendingCreationsRef.current.delete(id);
        }
      },
    );
    setSessions((prev) => [{ id, title: "新会话" }, ...prev.filter((s) => s.id !== id)]);
    setCurrentSessionId(id);
    setViewModel(createEmptyPipelineViewModel());
  }, [applySessions, discardPendingOutput, titlesKey]);

  const switchSession = useCallback(async (id: string) => {
    // 切到历史会话并重放其 transcript（断点续传），保留当前 phase/status。
    const requestEpoch = ++sessionRequestEpochRef.current;
    try {
      const { records, sessions: list } = await sessionSwitch(id);
      if (requestEpoch !== sessionRequestEpochRef.current) return;
      // 换会话即换 runtime，运行标记不能带过去。
      discardPendingOutput();
      runStarted.current = false;
      setCurrentSessionId(id);
      restoreRecords(records, true);
      applySessions(list);
      setClarificationStatus("IDLE");
      setHumanRequests([]);
    } catch (err) {
      if (requestEpoch !== sessionRequestEpochRef.current) return;
      // 后端重建 runtime 失败或连接抖动时，不能静默清空对话：保留当前内容并显式报错。
      appendError(`切换会话失败：${errorMessage(err)}`);
    }
  }, [appendError, applySessions, discardPendingOutput, restoreRecords]);

  const deleteSession = useCallback(async (id: string) => {
    // 删 default 不是删工作区，而是重置默认会话（后端清掉它的 transcript 与状态）。
    const requestEpoch = ++sessionRequestEpochRef.current;
    try {
      const pendingCreation = pendingCreationsRef.current.get(id);
      if (pendingCreation) await pendingCreation;
      const { sessions: list } = await sessionDelete(id);
      if (!mountedRef.current) return;
      const titles = loadTitles(titlesKey);
      delete titles[id];
      localStorage.setItem(titlesKey, JSON.stringify(titles));
      setSessions((current) => current.filter((session) => session.id !== id));
      if (requestEpoch !== sessionRequestEpochRef.current) return;
      applySessions(list);
      if (id === activeSessionIdRef.current) {
        await switchSession("default");
      }
    } catch (err) {
      if (!mountedRef.current || requestEpoch !== sessionRequestEpochRef.current) return;
      appendError(`删除会话失败：${errorMessage(err)}`);
    }
  }, [appendError, applySessions, switchSession, titlesKey]);

  const selectHypothesis = useCallback(async (hypothesisId: string) => {
    await sendControl(`/select ${hypothesisId}`);
  }, []);

  /** Legacy compatibility alias: old callers that wanted a raw start instead get the gate. */
  const startRun = useCallback(async (task?: string) => {
    const preview = latestPreview;
    if (preview?.draftId) {
      await confirmDraft(false);
      return;
    }
    if (task) {
      await startClarification(task);
    }
  }, [confirmDraft, latestPreview, startClarification]);

  // 后端报上来的 running/paused 同样意味着"这次运行确实存在"。切换会话时
  // runStarted 被清空，而停在 PREPARE 的会话没有 plans/attempts/实验可作证据，
  // 只认客户端证据会把暂停/继续/停止三个按钮一起禁掉。全新会话是 IDLE，不受影响。
  const runActive =
    runStarted.current ||
    viewModel.status === "running" ||
    viewModel.status === "paused" ||
    viewModel.plans.length > 0 ||
    viewModel.rightRail.searchAttempts > 0 ||
    viewModel.rightRail.latestExperimentId !== null;

  const status = clarificationStatus;

  return useMemo(() => ({
    viewModel,
    status,
    runActive,
    sessions,
    currentSessionId,
    humanRequests,
    humanPendingError,
    settlingRequestId,
    logs,
    clearLogs,
    sendPrompt,
    startRun,
    startClarification,
    confirmDraft,
    reviseDraft,
    retryDraft,
    cancelDraft,
    replyToHumanRequest,
    pauseRun,
    resumeRun,
    stopRun,
    toggleMode,
    newSession,
    switchSession,
    deleteSession,
    selectHypothesis,
    answerHuman,
    chooseHumanAnswer,
    skipHumanAnswer,
  }), [answerHuman, cancelDraft, chooseHumanAnswer, clarificationStatus, clearLogs, confirmDraft, currentSessionId, deleteSession, humanPendingError, humanRequests, logs, pauseRun, replyToHumanRequest, resumeRun, retryDraft, reviseDraft, runActive, sendPrompt, sessions, settlingRequestId, skipHumanAnswer, startClarification, startRun, status, stopRun, switchSession, toggleMode, newSession, selectHypothesis, viewModel]);
}

// Re-export convenience wrappers used by tests and components.
export const subscribeToPipelineEvents = bridge.subscribeToPipelineEvents;
