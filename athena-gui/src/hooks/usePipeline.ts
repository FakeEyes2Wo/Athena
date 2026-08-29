import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { errorMessage } from "../lib/errors";
import {
  humanChoice,
  humanPending,
  humanReply,
  humanSkip,
  pauseSearch,
  resumeSearch,
  sendControl,
  sendMessage,
  sessionDelete,
  sessionSwitch,
  sessionsList,
  startSearch,
  stateGet,
  stopSearch,
  subscribeToPipelineEvents,
  type HumanRequest,
  type PipelineEvent,
  type SessionRecord,
  type TaskUnderstanding,
} from "../lib/tauri-bridge";
import { createEmptyPipelineViewModel, type PipelineViewModel } from "../types/ui";

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

/** 从 task understanding（TaskUnderstanding）推导会话标题。 */
function titleFromTask(preview: TaskUnderstanding): string {
  if (preview.title?.trim()) return preview.title.trim();
  const parts = [preview.task_type, preview.primary_metric].filter((p) => p && p !== "other");
  return parts.length ? parts.join(" · ") : "新会话";
}

/** Rebuilds the conversation from persisted records by replaying each output
  * record through the same reducer the live stream uses (TUI-style resume). */
function applyHistoryRecords(current: PipelineViewModel, records: SessionRecord[]): PipelineViewModel {
  let next = current;
  for (const record of records) {
    if (record.type === "user") {
      const text = typeof record.text === "string" ? record.text : "";
      if (!text.trim()) continue;
      next = {
        ...next,
        messages: [
          ...next.messages,
          { id: `user-${record.seq}`, role: "user", kind: "text", content: text },
        ],
      };
    } else {
      next = applyPipelineEvent(next, { kind: "output", data: record }, true);
    }
  }
  return next;
}

/**
 * Applies a single backend event (``state`` / ``output``) to the view model.
 *
 * ``replay`` 区分事件来源：实时订阅推的 agent 文本是增量 delta，落盘 transcript
 * 回放的是已合并的整条消息——两者形状相同，只有来源能区分该追加还是该替换。
 */
function applyPipelineEvent(
  current: PipelineViewModel,
  event: PipelineEvent,
  replay = false,
): PipelineViewModel {
  const next: PipelineViewModel = { ...current, rightRail: { ...current.rightRail } };
  const { data } = event;

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
    const understanding = data.task_understanding as TaskUnderstanding | undefined;
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

  if (event.kind === "output") {
    const text = typeof data.text === "string" ? data.text : "";
    if (!text) return next;
    const source = typeof data.source === "string" ? data.source : undefined;
    const tool = typeof data.tool === "string" ? data.tool : undefined;
    const channel = typeof data.channel === "string" ? data.channel : undefined;
    const plan = typeof data.plan === "string" ? data.plan : undefined;
    // 消息身份来自后端的 message_id（同一条消息的所有 delta 与落盘记录共用它）；
    // 升级前写下的 transcript 没有该字段，退回按 seq 兜底，每条记录自成一条消息。
    const id =
      typeof data.message_id === "string" && data.message_id
        ? data.message_id
        : `out-${typeof data.seq === "number" ? data.seq : 0}`;
    const messages = [...current.messages];

    let target = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].id === id) { target = i; break; }
    }
    if (target >= 0) {
      // 同一条消息再次到达：实时 delta 是增量所以追加，落盘回放是整条所以替换。
      // 这一步同时让「挂载回放叠加在 live 之上」自然去重，不再产生重复 React key。
      const existing = messages[target];
      messages[target] = { ...existing, content: replay ? text : existing.content + text };
      next.messages = messages;
      return next;
    }

    // 纯空白只用于把已有消息的两个词分开，不足以独立成一条消息。
    if (!text.trim()) return next;

    if (channel === "error") {
      messages.push({ id, role: "athena", kind: "error", content: text });
    } else if (source === "tool" || channel === "stdout" || channel === "stderr") {
      // 工具输出（命令结果 / 文件读写），先于 tool 调用判定。
      messages.push({ id, role: "athena", kind: "text", content: text, source: "tool", tool, channel, plan });
    } else if (tool) {
      // 工具调用（agent function_call）。
      messages.push({ id, role: "athena", kind: "text", content: text, source, tool, plan });
    } else {
      messages.push({ id, role: "athena", kind: "text", content: text, source, plan });
    }
    next.messages = messages;
    return next;
  }

  return next;
}

/**
 * Central pipeline state hook.
 * Manages the view model, subscribes to backend events, and exposes all user actions
 * (send prompt, start/stop/pause/resume search, open/close panels).
 */
export function usePipeline(workspaceRoot?: string | null) {
  const [viewModel, setViewModel] = useState<PipelineViewModel>(createEmptyPipelineViewModel);
  const [sessions, setSessions] = useState<Array<{ id: string; title: string }>>([]);
  const [currentSessionId, setCurrentSessionId] = useState("default");
  const [humanRequests, setHumanRequests] = useState<HumanRequest[]>([]);
  const [awaitingIntent, setAwaitingIntent] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const counter = useRef(0);
  const logCounter = useRef(0);
  // start_search 已发出但后端首帧未回时，也算运行中。
  const runStarted = useRef(false);
  // 会话标题按工作区隔离：不同项目目录的会话标题互不串扰。
  const titlesKey = sessionTitlesKey(workspaceRoot);
  // 空白会话：命名会话、无消息、且没有真实运行活动。切换/新建时自动清理。
  const currentSessionBlank =
    currentSessionId !== "default" &&
    viewModel.messages.length === 0 &&
    !(
      runStarted.current ||
      viewModel.plans.length > 0 ||
      viewModel.rightRail.searchAttempts > 0 ||
      viewModel.rightRail.latestExperimentId !== null
    );

  const nextId = useCallback((prefix: string) => {
    counter.current += 1;
    return `${prefix}-${counter.current}`;
  }, []);

  const appendLog = useCallback((event: PipelineEvent) => {
    const data = event.data as Record<string, unknown> | undefined;
    logCounter.current += 1;
    const entry: LogEntry = {
      id: `log-${logCounter.current}`,
      at: Date.now(),
      kind: event.kind,
      source: typeof data?.source === "string" ? data.source : undefined,
      channel: typeof data?.channel === "string" ? data.channel : undefined,
      plan: typeof data?.plan === "string" ? data.plan : undefined,
      tool: typeof data?.tool === "string" ? data.tool : undefined,
      text: typeof data?.text === "string" ? data.text : "",
    };
    setLogs((prev) => [...prev.slice(-(MAX_LOG_ENTRIES - 1)), entry]);
  }, []);

  const clearLogs = useCallback(() => setLogs([]), []);

  // 重放会话记录：续接消息序列号并重建消息列表；可选清空现有消息。
  const restoreRecords = useCallback(
    (records: SessionRecord[], resetMessages: boolean) => {
      if (records.length) {
        counter.current = Math.max(counter.current, ...records.map((r) => r.seq));
      }
      setViewModel((prev) =>
        applyHistoryRecords(resetMessages ? { ...prev, messages: [] } : prev, records),
      );
    },
    [],
  );

  // 更新会话标题（state + localStorage）。
  const renameSession = useCallback((id: string, title: string) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)));
    saveTitle(titlesKey, id, title);
  }, [titlesKey]);

  // Subscribe to backend pipeline events on mount.
  useEffect(() => {
    let mounted = true;
    let unlisteners: Array<() => void> = [];

    subscribeToPipelineEvents((event) => {
      if (!mounted) return;
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
        if (!mounted) return;
        setViewModel((prev) => applyPipelineEvent(prev, { kind: "state", data: snapshot }));
      })
      .catch(() => {
        // Non-fatal: the live subscription above will still drive updates.
      });

    // 断点续传：列出会话 → 切到最近会话 → 重放其 transcript。
    sessionsList()
      .then(({ sessions: list }) => {
        const active = list.length ? list[0] : "default";
        return sessionSwitch(active).then(({ records }) => ({ active, list, records }));
      })
      .then(({ active, list, records }) => {
        if (!mounted) return;
        const titles = loadTitles(titlesKey);
        setSessions(
          (list.length ? list : [active]).map((id) => ({ id, title: titles[id] ?? "新会话" })),
        );
        setCurrentSessionId(active);
        restoreRecords(records, false);
      })
      .catch(() => {
        // 非致命：无历史或后端不支持时保持空白会话。
      });

    return () => { mounted = false; unlisteners.forEach((fn) => fn()); };
  }, [appendLog, titlesKey]);

  // Poll for outstanding supervisor human questions while a run is active or
  // while the initial intent clarification is pending.
  useEffect(() => {
    if (viewModel.status !== "running" && !awaitingIntent) {
      setHumanRequests([]);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const { requests } = await humanPending();
        if (!cancelled) setHumanRequests(requests);
      } catch {
        // Non-fatal: ignore polling errors.
      }
    };
    void poll();
    const timer = setInterval(poll, 1500);
    return () => { cancelled = true; clearInterval(timer); };
  }, [viewModel.status, awaitingIntent]);

  // User actions.

  const startRun = useCallback(async (task?: string, messageId?: string) => {
    runStarted.current = true;
    setViewModel((prev) => ({
      ...prev,
      phase: "PREPARE",
      status: "running",
      messages: prev.messages.map((m) =>
        m.id === messageId ? { ...m, started: true } : m,
      ),
    }));
    try {
      // 原始任务文本即后端 start_search 所需的 `task`；task understanding 只用于展示与标题。
      await startSearch({ task });
    } catch (err) {
      // start_search 失败 → 阶段机没起来，重置运行标记。
      runStarted.current = false;
      setViewModel((prev) => ({
        ...prev,
        status: "error",
      }));
      throw err;
    }
  }, []);

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

    setViewModel((prev) => ({
      ...prev,
      messages: [...prev.messages, { id: nextId("user"), role: "user", kind: "text", content }],
    }));

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
        const text = errorMessage(err);
        setViewModel((prev) => ({
          ...prev,
          status: "error",
          messages: [
            ...prev.messages,
            { id: nextId("error"), role: "athena", kind: "error", content: text },
          ],
        }));
        throw err;
      }
      return;
    }

    setAwaitingIntent(true);
    // 任务理解放到后台：不阻塞启动，避免澄清问答让界面一直停在 PREPARE · 空闲。
    void sendMessage(content)
      .then((preview) => {
        renameSession(currentSessionId, titleFromTask(preview));
        setViewModel((prev) => ({
          ...prev,
          messages: [
            ...prev.messages,
            {
              id: nextId("preview"),
              role: "athena",
              kind: "intent-preview",
              content: preview.title || `任务类型: ${preview.task_type} · 主指标: ${preview.primary_metric}`,
              preview,
              task: content,
              started: true,
            },
          ],
        }));
      })
      .catch(() => {
        // 非致命：任务已经启动，preview 拿不到不阻塞运行。
      })
      .finally(() => setAwaitingIntent(false));
    try {
      // 发送即启动：直接进入 PREPARE，不再等任务理解返回。
      await startRun(content);
    } catch (err) {
      setAwaitingIntent(false);
      const text = errorMessage(err);
      setViewModel((prev) => ({
        ...prev,
        status: "error",
        messages: [
          ...prev.messages,
          { id: nextId("error"), role: "athena", kind: "error", content: text },
        ],
      }));
      throw err;
    }
  }, [currentSessionId, nextId, renameSession, startRun, viewModel]);

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
    const id = `s-${Date.now()}`;
    runStarted.current = false;
    void (async () => {
      const previousId = currentSessionId;
      const shouldDeleteBlank = currentSessionBlank;
      await sessionSwitch(id);
      // 新会话创建成功后再清理旧空白会话，避免删除失败阻塞切换/新建。
      if (shouldDeleteBlank) {
        try {
          await sessionDelete(previousId);
        } catch {
          // 非致命：删除失败不阻塞新建会话。
        }
      }
    })();
    setSessions((prev) => [{ id, title: "新会话" }, ...prev.filter((s) => s.id !== id)]);
    setCurrentSessionId(id);
    setViewModel(createEmptyPipelineViewModel());
    saveTitle(titlesKey, id, "新会话");
  }, [currentSessionBlank, currentSessionId, titlesKey]);

  const switchSession = useCallback(async (id: string) => {
    // 切到历史会话并重放其 transcript（断点续传），保留当前 phase/status。
    const previousId = currentSessionId;
    const shouldDeleteBlank = currentSessionBlank && id !== previousId;
    try {
      const { records } = await sessionSwitch(id);
      // 换会话即换 runtime，运行标记不能带过去。
      runStarted.current = false;
      setCurrentSessionId(id);
      restoreRecords(records, true);
      // 切换成功后再清理旧空白会话，避免删除失败阻塞切换。
      if (shouldDeleteBlank) {
        void sessionDelete(previousId).catch(() => {});
      }
    } catch (err) {
      // 后端重建 runtime 失败或连接抖动时，不能静默清空对话：保留当前内容并显式报错。
      setViewModel((prev) => ({
        ...prev,
        status: "error",
        messages: [
          ...prev.messages,
          {
            id: nextId("error"),
            role: "athena",
            kind: "error",
            content: `切换会话失败：${errorMessage(err)}`,
          },
        ],
      }));
    }
  }, [currentSessionBlank, currentSessionId, nextId, restoreRecords]);

  const deleteSession = useCallback(async (id: string) => {
    // default 是主项目会话，不可删除；命名会话删除后返回更新列表。
    if (id === "default") return;
    try {
      const { sessions: list } = await sessionDelete(id);
      const titles = loadTitles(titlesKey);
      delete titles[id];
      localStorage.setItem(titlesKey, JSON.stringify(titles));
      setSessions(list.map((sid) => ({ id: sid, title: titles[sid] ?? "新会话" })));
      if (id === currentSessionId) {
        await switchSession("default");
      }
    } catch (err) {
      setViewModel((prev) => ({
        ...prev,
        status: "error",
        messages: [
          ...prev.messages,
          {
            id: nextId("error"),
            role: "athena",
            kind: "error",
            content: `删除会话失败：${errorMessage(err)}`,
          },
        ],
      }));
    }
  }, [currentSessionId, nextId, switchSession, titlesKey]);

  const selectHypothesis = useCallback(async (hypothesisId: string) => {
    await sendControl(`/select ${hypothesisId}`);
  }, []);

  const answerHuman = useCallback(async (requestId: string, answer: string) => {
    const text = answer.trim();
    if (!text) return;
    await humanReply(requestId, text);
    setHumanRequests((prev) => prev.filter((r) => r.request_id !== requestId));
  }, []);

  const chooseHumanAnswer = useCallback(async (requestId: string, value: string) => {
    await humanChoice(requestId, value);
    setHumanRequests((prev) => prev.filter((r) => r.request_id !== requestId));
  }, []);

  const skipHumanAnswer = useCallback(async (requestId: string) => {
    await humanSkip(requestId);
    setHumanRequests((prev) => prev.filter((r) => r.request_id !== requestId));
  }, []);

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

  return useMemo(() => ({
    viewModel,
    runActive,
    sessions,
    currentSessionId,
    humanRequests,
    logs,
    clearLogs,
    sendPrompt,
    startRun,
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
  }), [answerHuman, chooseHumanAnswer, skipHumanAnswer, clearLogs, currentSessionId, deleteSession, humanRequests, logs, pauseRun, resumeRun, runActive, sendPrompt, sessions, startRun, stopRun, switchSession, toggleMode, newSession, selectHypothesis, viewModel]);
}
