import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let pipelineEventHandler:
  | ((event: { kind: string; data: Record<string, unknown> }) => void)
  | null = null;

const bridgeMocks = vi.hoisted(() => ({
  startSearch: vi.fn(),
  pauseSearch: vi.fn(),
  resumeSearch: vi.fn(),
  stopSearch: vi.fn(),
  sendControl: vi.fn(),
  startValidation: vi.fn(),
  generateReport: vi.fn(),
  stateGet: vi.fn(),
  sessionsList: vi.fn(),
  sessionSwitch: vi.fn(),
  sessionDelete: vi.fn(),
  subscribeToPipelineEvents: vi.fn(),
  taskClarificationStart: vi.fn(),
  taskClarificationGet: vi.fn(),
  taskClarificationRevise: vi.fn(),
  taskClarificationRetry: vi.fn(),
  taskClarificationCancel: vi.fn(),
  humanPending: vi.fn(),
  humanReply: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", () => ({
  startSearch: bridgeMocks.startSearch,
  pauseSearch: bridgeMocks.pauseSearch,
  resumeSearch: bridgeMocks.resumeSearch,
  stopSearch: bridgeMocks.stopSearch,
  sendControl: bridgeMocks.sendControl,
  startValidation: bridgeMocks.startValidation,
  generateReport: bridgeMocks.generateReport,
  stateGet: bridgeMocks.stateGet,
  sessionsList: bridgeMocks.sessionsList,
  sessionSwitch: bridgeMocks.sessionSwitch,
  sessionDelete: bridgeMocks.sessionDelete,
  subscribeToPipelineEvents: bridgeMocks.subscribeToPipelineEvents,
  taskClarificationStart: bridgeMocks.taskClarificationStart,
  taskClarificationGet: bridgeMocks.taskClarificationGet,
  taskClarificationRevise: bridgeMocks.taskClarificationRevise,
  taskClarificationRetry: bridgeMocks.taskClarificationRetry,
  taskClarificationCancel: bridgeMocks.taskClarificationCancel,
  humanPending: bridgeMocks.humanPending,
  humanReply: bridgeMocks.humanReply,
  PIPELINE_EVENT_NAMES: ["state", "output", "clarification", "human_request"],
}));

import { usePipeline } from "../usePipeline";
import { loadWorkspaceSessions } from "../../lib/workspaceStorage";

const readyDraft = {
  schema_version: 1 as const,
  draft_id: "draft-1",
  revision: 2,
  session_id: "default",
  original_task: "predict churn",
  status: "READY_FOR_CONFIRMATION",
  understanding: {
    title: "Predict churn",
    dataset: "churn.csv",
    target: "churned",
    task_type: "classification",
    primary_metric: "f1",
    direction: "maximize",
    evaluation_plan: "holdout f1",
  },
  answers: [],
  unresolved: [],
  failure: null,
  questions_asked: 3,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
};

const clarifyingDraft = {
  ...readyDraft,
  revision: 1,
  status: "CLARIFYING",
};

describe("usePipeline", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    localStorage.clear();
    for (const key of Object.keys(bridgeMocks) as Array<keyof typeof bridgeMocks>) {
      bridgeMocks[key].mockReset();
    }

    bridgeMocks.startSearch.mockResolvedValue({ ok: true });
    bridgeMocks.pauseSearch.mockResolvedValue({ ok: true });
    bridgeMocks.resumeSearch.mockResolvedValue({ ok: true });
    bridgeMocks.stopSearch.mockResolvedValue({ ok: true });
    bridgeMocks.sendControl.mockResolvedValue({ ok: true });
    bridgeMocks.startValidation.mockResolvedValue({ ok: true });
    bridgeMocks.generateReport.mockResolvedValue({ ok: true });
    bridgeMocks.stateGet.mockResolvedValue({});
    bridgeMocks.sessionsList.mockResolvedValue({ sessions: [], active: null });
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: [] });
    bridgeMocks.sessionDelete.mockResolvedValue({ deleted: true, sessions: [] });
    pipelineEventHandler = null;
    bridgeMocks.subscribeToPipelineEvents.mockImplementation(async (handler) => {
      pipelineEventHandler = handler;
      return [];
    });
    bridgeMocks.taskClarificationStart.mockResolvedValue({
      draft_id: "draft-1",
      revision: 0,
      status: "CLARIFYING",
    });
    bridgeMocks.taskClarificationGet.mockResolvedValue(readyDraft);
    bridgeMocks.taskClarificationRevise.mockResolvedValue(clarifyingDraft);
    bridgeMocks.taskClarificationRetry.mockResolvedValue(clarifyingDraft);
    bridgeMocks.taskClarificationCancel.mockResolvedValue({ ok: true });
    bridgeMocks.humanPending.mockResolvedValue({ requests: [] });
    bridgeMocks.humanReply.mockResolvedValue({ replied: true });
  });

  it("does not start search when a task is submitted", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });

    expect(bridgeMocks.taskClarificationStart).toHaveBeenCalledWith("predict churn");
    expect(bridgeMocks.startSearch).not.toHaveBeenCalled();
    expect(result.current.status).toBe("CLARIFYING");
    expect(result.current.viewModel.messages.map((message) => message.kind)).toEqual([
      "text",
      "intent-preview",
    ]);
    expect(result.current.viewModel.messages[1].started).toBe(false);
  });

  it("reloads the authoritative draft after a stale confirmation revision", async () => {
    bridgeMocks.taskClarificationStart.mockResolvedValue(readyDraft);
    bridgeMocks.startSearch.mockRejectedValueOnce(
      Object.assign(new Error("stale revision"), { code: "stale_revision" }),
    );

    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });
    expect(result.current.status).toBe("READY_FOR_CONFIRMATION");

    await act(async () => {
      await result.current.confirmDraft(false).catch(() => undefined);
    });

    expect(bridgeMocks.startSearch).toHaveBeenCalledWith("draft-1", 2, false);
    expect(bridgeMocks.taskClarificationGet).toHaveBeenCalledWith("draft-1");
  });

  it("enters RUNNING only after the gated start succeeds", async () => {
    bridgeMocks.taskClarificationStart.mockResolvedValue(readyDraft);

    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });

    await act(async () => {
      await result.current.confirmDraft(false);
    });

    expect(bridgeMocks.startSearch).toHaveBeenCalledWith("draft-1", 2, false);
    expect(result.current.status).toBe("RUNNING");
    expect(result.current.viewModel.messages.find((m) => m.kind === "intent-preview")?.started).toBe(true);
    expect(result.current.viewModel.status).toBe("running");
  });

  it("routes slash commands to the matching runtime control", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("/pause");
    });
    expect(bridgeMocks.pauseSearch).toHaveBeenCalledTimes(1);
    expect(result.current.viewModel.status).toBe("paused");

    await act(async () => {
      await result.current.sendPrompt("/manual");
    });
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("/manual");
    expect(result.current.viewModel.manual).toBe(true);

    await act(async () => {
      await result.current.sendPrompt("/select hyp-7");
    });
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("/select hyp-7");
  });

  it("routes exact locale-independent continue to the shared resume action", async () => {
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(pipelineEventHandler).not.toBeNull());

    act(() => {
      pipelineEventHandler?.({
        kind: "state",
        data: {
          phase: "PREPARE",
          status: "FAILED",
          resume_available: true,
          resume_reason: "failed",
        },
      });
    });

    await act(async () => {
      await result.current.sendPrompt("  CONTINUE  ");
    });

    expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.taskClarificationStart).not.toHaveBeenCalled();
    expect(result.current.viewModel.messages.filter((message) => message.kind === "intent-preview")).toHaveLength(0);
    expect(result.current.viewModel.status).toBe("running");
  });

  it("keeps multiword continue prose on its existing task path", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("continue research");
    });

    expect(bridgeMocks.resumeSearch).not.toHaveBeenCalled();
    expect(bridgeMocks.taskClarificationStart).toHaveBeenCalledWith("continue research");
  });

  it("does not treat continue three more attempts as the resume command", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("continue three more attempts");
    });

    expect(bridgeMocks.resumeSearch).not.toHaveBeenCalled();
    expect(bridgeMocks.taskClarificationStart).toHaveBeenCalledWith(
      "continue three more attempts",
    );
  });

  it("shares one in-flight resume request across rapid continuation submissions", async () => {
    let resolveResume: (() => void) | undefined;
    bridgeMocks.resumeSearch.mockImplementation(
      () => new Promise<void>((resolve) => { resolveResume = resolve; }),
    );
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(pipelineEventHandler).not.toBeNull());
    act(() => {
      pipelineEventHandler?.({
        kind: "state",
        data: { phase: "PREPARE", status: "IDLE", resume_available: true },
      });
    });

    let first: Promise<void>;
    let second: Promise<void>;
    act(() => {
      first = result.current.sendPrompt("continue");
      second = result.current.sendPrompt("continue");
    });
    expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveResume?.();
      await Promise.all([first!, second!]);
    });
  });

  it("uses the new session identity for an immediate resume after creation", async () => {
    let resolveOldResume: (() => void) | undefined;
    let resolveNewResume: (() => void) | undefined;
    bridgeMocks.resumeSearch
      .mockImplementationOnce(
        () => new Promise<void>((resolve) => { resolveOldResume = resolve; }),
      )
      .mockImplementationOnce(
        () => new Promise<void>((resolve) => { resolveNewResume = resolve; }),
      );
    const { result } = renderHook(() => usePipeline());

    let oldResume: Promise<void>;
    let newResume: Promise<void>;
    let newSessionId = "";
    await act(async () => {
      oldResume = result.current.sendPrompt("continue");
      result.current.newSession();
      const switchCalls = bridgeMocks.sessionSwitch.mock.calls;
      newSessionId = switchCalls[switchCalls.length - 1]?.[0] as string;
      newResume = result.current.sendPrompt("continue");
      await Promise.resolve();
    });
    expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveOldResume?.();
      await oldResume!;
    });
    expect(result.current.currentSessionId).toBe(newSessionId);
    expect(result.current.viewModel.status).toBe("idle");
    expect(result.current.viewModel.resumeAvailable).toBe(false);

    await act(async () => {
      resolveNewResume?.();
      await newResume!;
    });
    expect(result.current.viewModel.status).toBe("running");
  });

  it("uses the switched session identity for an immediate resume after switching", async () => {
    let rejectOldResume: ((reason?: unknown) => void) | undefined;
    let resolveNewResume: (() => void) | undefined;
    bridgeMocks.resumeSearch
      .mockImplementationOnce(
        () => new Promise<void>((_resolve, reject) => { rejectOldResume = reject; }),
      )
      .mockImplementationOnce(
        () => new Promise<void>((resolve) => { resolveNewResume = resolve; }),
      );
    const { result } = renderHook(() => usePipeline());

    let oldResume: Promise<void>;
    let newResume: Promise<void>;
    act(() => {
      oldResume = result.current.sendPrompt("continue");
    });
    expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.switchSession("fresh");
      newResume = result.current.sendPrompt("continue");
    });
    expect(result.current.currentSessionId).toBe("fresh");
    expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(2);

    await act(async () => {
      rejectOldResume?.(new Error("old session failed"));
      await oldResume!.catch(() => undefined);
    });
    expect(result.current.viewModel.status).toBe("idle");
    expect(result.current.viewModel.messages.filter((message) => message.kind === "error")).toHaveLength(0);

    await act(async () => {
      resolveNewResume?.();
      await newResume!;
    });
    expect(result.current.viewModel.status).toBe("running");
  });

  it("keeps existing history when a resumable run rejects continuation", async () => {
    bridgeMocks.resumeSearch.mockRejectedValue(new Error("nothing to resume"));
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(pipelineEventHandler).not.toBeNull());
    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });
    const history = result.current.viewModel.messages;
    act(() => {
      pipelineEventHandler?.({
        kind: "state",
        data: { phase: "PREPARE", status: "FAILED", resume_available: true },
      });
    });

    await act(async () => {
      await result.current.sendPrompt("continue").catch(() => undefined);
    });

    expect(bridgeMocks.resumeSearch).toHaveBeenCalledTimes(1);
    expect(result.current.viewModel.messages).toHaveLength(history.length + 1);
    expect(result.current.viewModel.messages.slice(0, history.length)).toEqual(history);
    expect(result.current.viewModel.messages.filter((message) => message.kind === "error")).toHaveLength(1);
    expect(result.current.viewModel.messages[result.current.viewModel.messages.length - 1]).toMatchObject({ kind: "error", content: "nothing to resume" });
    expect(result.current.viewModel.status).toBe("error");
  });

  it("routes prose to the supervisor while a run is active", async () => {
    bridgeMocks.taskClarificationStart.mockResolvedValue(readyDraft);
    bridgeMocks.sendControl.mockResolvedValue({ response: "收到，已调整计划。" });

    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("analyze this CSV");
    });
    await act(async () => {
      await result.current.confirmDraft(false);
    });
    await act(async () => {
      await result.current.sendPrompt("请优先做草莓");
    });

    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("请优先做草莓");
    expect(result.current.viewModel.messages.some(
      (message) => message.role === "athena" && message.content === "收到，已调整计划。",
    )).toBe(true);
  });

  it("toggles manual/auto mode through the exposed action", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.toggleMode();
    });
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("/manual");
    expect(result.current.viewModel.manual).toBe(true);

    await act(async () => {
      await result.current.toggleMode();
    });
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("/auto");
    expect(result.current.viewModel.manual).toBe(false);
  });

  it("unifies human replies and protects against double submission", async () => {
    let resolveReply: (() => void) | undefined;
    bridgeMocks.humanReply.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveReply = () => resolve({ replied: true });
        }),
    );
    bridgeMocks.humanPending.mockResolvedValue({
      requests: [
        {
          request_id: "req-1",
          session_id: "s-1",
          scope_id: "draft-1",
          scope_kind: "clarification",
          prompt: "Choose metric",
          choices: [{ label: "F1", value: "f1" }],
          allow_custom: true,
          allow_skip: true,
          created_at: "2026-09-01T10:00:00Z",
          expires_at: "2026-09-01T10:02:00Z",
        },
      ],
    });

    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.startClarification("predict churn");
    });
    expect(result.current.humanRequests).toHaveLength(1);

    await act(async () => {
      const first = result.current.replyToHumanRequest("req-1", { kind: "text", text: "F1" });
      const second = result.current.replyToHumanRequest("req-1", { kind: "text", text: "F1 again" });
      expect(bridgeMocks.humanReply).toHaveBeenCalledTimes(1);
      expect(bridgeMocks.humanReply).toHaveBeenCalledWith("req-1", { kind: "text", text: "F1" });
      resolveReply?.();
      await Promise.allSettled([first, second]);
    });

    await waitFor(() => expect(result.current.humanRequests).toHaveLength(0));
  });

  it("restores persisted session records into the conversation on mount", async () => {
    bridgeMocks.sessionSwitch.mockResolvedValue({
      records: [
        { type: "user", seq: 1, text: "analyze this CSV" },
        { type: "output", seq: 2, source: "supervisor", channel: "text", text: "开始准备" },
        { type: "output", seq: 3, source: "agent", channel: "text", text: "正在", plan: "ideator-1" },
        { type: "output", seq: 4, source: "agent", channel: "text", text: "生成假设", plan: "ideator-1" },
      ],
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.viewModel.messages).toHaveLength(4);
    });
    expect(result.current.viewModel.messages[0]).toMatchObject({
      id: "user-1",
      role: "user",
      content: "analyze this CSV",
    });
    expect(result.current.viewModel.messages.slice(1).map((m) => m.content)).toEqual([
      "开始准备",
      "正在",
      "生成假设",
    ]);
  });

  it("replays a large mixed transcript with the same ordered message semantics as live deltas", async () => {
    const records: Array<Record<string, unknown>> = [
      { type: "user", seq: 1, text: "run the analysis" },
    ];
    let agentContent = "";
    let supervisorContent = "";
    let seq = 2;

    for (let index = 0; index < 100; index += 1) {
      agentContent += `${index}|`;
      records.push({
        type: "output",
        seq,
        message_id: "agent-stream",
        source: "agent",
        channel: "text",
        text: agentContent,
      });
      seq += 1;

      if (index < 50) {
        supervisorContent += `${index};`;
        records.push({
          type: "output",
          seq,
          message_id: "supervisor-stream",
          source: "supervisor",
          channel: "text",
          text: supervisorContent,
        });
        seq += 1;
      }

      if (index % 10 === 0) {
        records.push({
          type: "output",
          seq,
          message_id: `tool-${index}`,
          source: "tool",
          channel: "stdout",
          tool: "inspect_dataset",
          text: `chunk-${index}`,
        });
        seq += 1;
      }
    }
    bridgeMocks.sessionSwitch.mockResolvedValue({ records, sessions: ["default"] });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => expect(result.current.viewModel.messages).toHaveLength(13));
    expect(result.current.viewModel.messages.map((message) => message.id)).toEqual([
      "user-1",
      "agent-stream",
      "supervisor-stream",
      "tool-0",
      "tool-10",
      "tool-20",
      "tool-30",
      "tool-40",
      "tool-50",
      "tool-60",
      "tool-70",
      "tool-80",
      "tool-90",
    ]);
    expect(result.current.viewModel.messages[1]?.content).toBe(
      Array.from({ length: 100 }, (_, index) => `${index}|`).join(""),
    );
    expect(result.current.viewModel.messages[2]?.content).toBe(
      Array.from({ length: 50 }, (_, index) => `${index};`).join(""),
    );
    expect(result.current.viewModel.messages.slice(3).map((message) => message.content)).toEqual([
      "chunk-0",
      "chunk-10",
      "chunk-20",
      "chunk-30",
      "chunk-40",
      "chunk-50",
      "chunk-60",
      "chunk-70",
      "chunk-80",
      "chunk-90",
    ]);
  });

  it("clears the conversation view on new session without clearing the transcript", async () => {
    bridgeMocks.sessionSwitch.mockResolvedValue({
      records: [
        { type: "user", seq: 1, text: "analyze this CSV" },
        { type: "output", seq: 2, source: "supervisor", channel: "text", text: "开始准备" },
      ],
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.viewModel.messages).toHaveLength(2);
    });

    act(() => {
      result.current.newSession();
    });

    expect(result.current.viewModel.messages).toHaveLength(0);
  });

  it("leaves blank-session cleanup to the backend when switching away", async () => {
    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.currentSessionId).toBe("default");
    });
    await act(async () => {
      result.current.newSession();
    });
    const blankId = result.current.currentSessionId;
    expect(blankId).not.toBe("default");

    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default"] });
    await act(async () => {
      await result.current.switchSession("default");
    });

    expect(bridgeMocks.sessionDelete).not.toHaveBeenCalled();
    expect(result.current.sessions.map((s) => s.id)).toEqual(["default"]);
  });

  it("keeps the session list empty for an untouched workspace", async () => {
    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default");
    });
    expect(result.current.sessions).toEqual([]);
  });

  it("restores the workspace's last active session on mount", async () => {
    bridgeMocks.sessionsList.mockResolvedValue({
      sessions: ["default", "s-2"],
      active: "s-2",
    });
    bridgeMocks.sessionSwitch.mockResolvedValue({
      records: [],
      sessions: ["default", "s-2"],
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.currentSessionId).toBe("s-2");
    });
    expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("s-2");
  });

  it("replaces live messages when initial session hydration is empty", async () => {
    let resolveSessions: ((value: { sessions: string[]; active: string | null }) => void) | undefined;
    bridgeMocks.sessionsList.mockImplementation(
      () => new Promise((resolve) => {
        resolveSessions = resolve;
      }),
    );
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default"] });

    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(pipelineEventHandler).not.toBeNull());
    act(() => {
      pipelineEventHandler?.({
        kind: "output",
        data: { seq: 1, message_id: "live-1", source: "agent", channel: "text", text: "live message" },
      });
      // A non-output event synchronously flushes queued output before it applies.
      pipelineEventHandler?.({ kind: "state", data: { phase: "PREPARE" } });
    });
    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual(["live message"]);

    await act(async () => {
      resolveSessions?.({ sessions: ["default"], active: "default" });
    });

    await waitFor(() => expect(result.current.viewModel.messages).toEqual([]));
  });

  it("ignores an older session switch that resolves after a newer one", async () => {
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    let resolveOlder: ((value: { records: Array<Record<string, unknown>>; sessions: string[] }) => void) | undefined;
    let resolveNewer: ((value: { records: Array<Record<string, unknown>>; sessions: string[] }) => void) | undefined;
    bridgeMocks.sessionSwitch.mockImplementation((id: string) => new Promise((resolve) => {
      if (id === "older") resolveOlder = resolve;
      if (id === "newer") resolveNewer = resolve;
    }));

    let olderSwitch: Promise<void>;
    let newerSwitch: Promise<void>;
    act(() => {
      olderSwitch = result.current.switchSession("older");
      newerSwitch = result.current.switchSession("newer");
    });
    await act(async () => {
      resolveNewer?.({
        records: [{ type: "output", seq: 2, message_id: "newer", text: "newer transcript" }],
        sessions: ["older", "newer"],
      });
      await newerSwitch!;
    });
    await act(async () => {
      resolveOlder?.({
        records: [{ type: "output", seq: 1, message_id: "older", text: "older transcript" }],
        sessions: ["older", "newer"],
      });
      await olderSwitch!;
    });

    expect(result.current.currentSessionId).toBe("newer");
    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual(["newer transcript"]);
  });

  it("prefers an available requested session during initial restore", async () => {
    bridgeMocks.sessionsList.mockResolvedValue({
      sessions: ["default", "s-2"],
      active: "default",
    });
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default", "s-2"] });

    renderHook(() => usePipeline("C:/workspace", "s-2"));

    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalled());
    expect(bridgeMocks.sessionSwitch.mock.calls[0]).toEqual(["s-2"]);
  });

  it("clears stale cached summaries after authoritative empty hydration", async () => {
    const cacheKey = "athena.workspace.sessions:C:/workspace";
    localStorage.setItem(cacheKey, JSON.stringify([{ id: "stale", title: "Stale" }]));

    renderHook(() => usePipeline("C:/workspace"));

    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));
    await waitFor(() => expect(localStorage.getItem(cacheKey)).toBeNull());
  });

  it("persists understanding title changes for authoritative sessions", async () => {
    bridgeMocks.sessionsList.mockResolvedValue({ sessions: ["default"], active: "default" });
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default"] });
    bridgeMocks.taskClarificationStart.mockResolvedValue(readyDraft);
    const { result } = renderHook(() => usePipeline("C:/workspace"));
    await waitFor(() => expect(result.current.sessions.map((session) => session.id)).toEqual(["default"]));

    await act(async () => {
      await result.current.sendPrompt("draft task title");
    });

    await waitFor(() => expect(loadWorkspaceSessions("C:/workspace")).toEqual([
      { id: "default", title: "Predict churn" },
    ]));
  });

  it("does not persist title changes for optimistic-only sessions", async () => {
    bridgeMocks.sessionsList.mockResolvedValue({ sessions: ["default"], active: "default" });
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default"] });
    const { result } = renderHook(() => usePipeline("C:/workspace"));
    await waitFor(() => expect(result.current.sessions.map((session) => session.id)).toEqual(["default"]));

    bridgeMocks.sessionSwitch.mockImplementation(() => new Promise(() => {}));
    act(() => result.current.newSession());
    const optimisticId = result.current.currentSessionId;
    await act(async () => {
      await result.current.sendPrompt("Optimistic title");
    });

    expect(result.current.sessions).toContainEqual({ id: optimisticId, title: "Optimistic title" });
    expect(loadWorkspaceSessions("C:/workspace")).toEqual([{ id: "default", title: "新会话" }]);
  });

  it("waits for new-session creation before deleting it", async () => {
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    let resolveCreation: ((value: { records: never[]; sessions: string[] }) => void) | undefined;
    bridgeMocks.sessionSwitch.mockImplementation((id: string) => {
      if (id === "default") return Promise.resolve({ records: [], sessions: ["default"] });
      return new Promise((resolve) => {
        resolveCreation = resolve;
      });
    });
    bridgeMocks.sessionDelete.mockResolvedValue({ deleted: true, sessions: ["default"] });

    act(() => result.current.newSession());
    const newId = result.current.currentSessionId;
    let deletion: Promise<void>;
    act(() => {
      deletion = result.current.deleteSession(newId);
    });
    expect(bridgeMocks.sessionDelete).not.toHaveBeenCalled();

    await act(async () => {
      resolveCreation?.({ records: [], sessions: ["default", newId] });
      await deletion!;
    });

    expect(bridgeMocks.sessionDelete).toHaveBeenCalledWith(newId);
    expect(result.current.sessions.some((session) => session.id === newId)).toBe(false);
  });

  it("keeps same-millisecond new sessions unique with independent pending deletions", async () => {
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));
    bridgeMocks.sessionSwitch.mockClear();

    const creationResolvers: Array<(
      value: { records: never[]; sessions: string[] },
    ) => void> = [];
    bridgeMocks.sessionSwitch.mockImplementation((id: string) => {
      if (id === "default") return Promise.resolve({ records: [], sessions: ["default"] });
      return new Promise((resolve) => {
        creationResolvers.push(resolve);
      });
    });
    bridgeMocks.sessionDelete.mockResolvedValue({ deleted: true, sessions: [] });
    const now = vi.spyOn(Date, "now").mockReturnValue(1234567890);

    act(() => {
      result.current.newSession();
      result.current.newSession();
    });
    const [firstId, secondId] = bridgeMocks.sessionSwitch.mock.calls.map(([id]) => id as string);
    expect(firstId).not.toBe(secondId);

    let firstDeletion: Promise<void>;
    let secondDeletion: Promise<void>;
    act(() => {
      firstDeletion = result.current.deleteSession(firstId);
      secondDeletion = result.current.deleteSession(secondId);
    });
    expect(bridgeMocks.sessionDelete).not.toHaveBeenCalled();

    await act(async () => {
      creationResolvers[0]?.({ records: [], sessions: [firstId, secondId] });
      await firstDeletion!;
    });
    expect(bridgeMocks.sessionDelete).toHaveBeenCalledWith(firstId);
    expect(bridgeMocks.sessionDelete).not.toHaveBeenCalledWith(secondId);

    await act(async () => {
      creationResolvers[1]?.({ records: [], sessions: [firstId, secondId] });
      await secondDeletion!;
    });
    expect(bridgeMocks.sessionDelete.mock.calls.map(([id]) => id)).toEqual([firstId, secondId]);
    now.mockRestore();
  });

  it("removes a pending new session from React state and cache when its delete is superseded by a switch", async () => {
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    let resolveCreation: ((value: { records: never[]; sessions: string[] }) => void) | undefined;
    let resolveDelete: ((value: { deleted: boolean; sessions: string[] }) => void) | undefined;
    bridgeMocks.sessionSwitch.mockImplementation((id: string) => {
      if (id === "newer") {
        return Promise.resolve({ records: [], sessions: ["newer", deletingId] });
      }
      return new Promise((resolve) => {
        resolveCreation = resolve;
      });
    });
    bridgeMocks.sessionDelete.mockImplementation(
      () => new Promise((resolve) => {
        resolveDelete = resolve;
      }),
    );

    act(() => result.current.newSession());
    const deletingId = result.current.currentSessionId;
    let deletion: Promise<void>;
    act(() => {
      deletion = result.current.deleteSession(deletingId);
    });
    await act(async () => {
      resolveCreation?.({ records: [], sessions: ["default", deletingId] });
    });
    await waitFor(() => expect(bridgeMocks.sessionDelete).toHaveBeenCalledWith(deletingId));

    await act(async () => {
      await result.current.switchSession("newer");
    });
    await act(async () => {
      resolveDelete?.({ deleted: true, sessions: ["default"] });
      await deletion!;
    });

    expect(result.current.currentSessionId).toBe("newer");
    expect(result.current.sessions.map((session) => session.id)).toEqual(["newer"]);
    expect(loadWorkspaceSessions("default").map((session) => session.id)).toEqual(["newer"]);
  });

  it("does not append an older delete failure after switching sessions", async () => {
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    let rejectDelete: ((reason: Error) => void) | undefined;
    bridgeMocks.sessionDelete.mockImplementation(
      () => new Promise((_, reject) => {
        rejectDelete = reject;
      }),
    );
    let deletion: Promise<void>;
    act(() => {
      deletion = result.current.deleteSession("default");
    });
    await waitFor(() => expect(bridgeMocks.sessionDelete).toHaveBeenCalledWith("default"));

    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["newer"] });
    await act(async () => {
      await result.current.switchSession("newer");
    });
    await act(async () => {
      rejectDelete?.(new Error("old delete failed"));
      await deletion!;
    });

    expect(result.current.currentSessionId).toBe("newer");
    expect(result.current.viewModel.messages).toEqual([]);
    expect(result.current.viewModel.status).toBe("idle");
  });

  it("ignores a mount snapshot that resolves after a session switch", async () => {
    let resolveSnapshot: ((value: Record<string, unknown>) => void) | undefined;
    bridgeMocks.stateGet.mockImplementation(
      () => new Promise((resolve) => {
        resolveSnapshot = resolve;
      }),
    );
    const { result } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["newer"] });
    await act(async () => {
      await result.current.switchSession("newer");
    });
    act(() => {
      pipelineEventHandler?.({
        kind: "state",
        data: {
          phase: "NEW_PHASE",
          status: "STOPPED",
          plans: [{ id: "new-plan" }],
          pending: [{ id: "new-pending", statement: "new pending" }],
        },
      });
    });

    await act(async () => {
      resolveSnapshot?.({
        phase: "OLD_PHASE",
        status: "RUNNING",
        plans: [{ id: "old-plan" }],
        pending: [{ id: "old-pending", statement: "old pending" }],
      });
    });

    expect(result.current.viewModel).toMatchObject({
      phase: "NEW_PHASE",
      status: "completed",
      plans: [{ id: "new-plan" }],
      pending: [{ id: "new-pending", statement: "new pending" }],
    });
  });

  it("seeds the current session when initial session hydration fails", async () => {
    bridgeMocks.sessionsList.mockRejectedValue(new Error("session list unavailable"));
    bridgeMocks.stateGet.mockResolvedValue({
      phase: "PREPARE",
      status: "FAILED",
      resume_available: true,
      resume_reason: "failed",
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => expect(bridgeMocks.stateGet).toHaveBeenCalledTimes(1));
    expect(result.current.currentSessionId).toBe("default");
    expect(result.current.viewModel).toMatchObject({
      phase: "PREPARE",
      status: "error",
      resumeAvailable: true,
      resumeReason: "failed",
    });
  });

  it("does not apply a pending creation response after unmount", async () => {
    const { result, unmount } = renderHook(() => usePipeline());
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    let resolveCreation: ((value: { records: never[]; sessions: string[] }) => void) | undefined;
    bridgeMocks.sessionSwitch.mockImplementation(
      () => new Promise((resolve) => {
        resolveCreation = resolve;
      }),
    );
    const storageRead = vi.spyOn(Storage.prototype, "getItem");
    act(() => result.current.newSession());
    const newId = result.current.currentSessionId;
    unmount();
    storageRead.mockClear();

    await act(async () => {
      resolveCreation?.({ records: [], sessions: [newId] });
    });

    expect(storageRead).not.toHaveBeenCalled();
    storageRead.mockRestore();
  });

  it("does not apply a deferred post-confirm session refresh after unmount", async () => {
    let resolveRefresh: ((value: { sessions: string[]; active: null }) => void) | undefined;
    bridgeMocks.sessionsList
      .mockResolvedValueOnce({ sessions: ["default"], active: "default" })
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRefresh = resolve;
      }));
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default"] });
    bridgeMocks.taskClarificationStart.mockResolvedValue(readyDraft);
    const { result, unmount } = renderHook(() => usePipeline("C:/workspace"));
    await waitFor(() => expect(result.current.sessions.map((session) => session.id)).toEqual(["default"]));
    await act(async () => {
      await result.current.sendPrompt("predict churn");
      await result.current.confirmDraft(false);
    });
    await waitFor(() => expect(bridgeMocks.sessionsList).toHaveBeenCalledTimes(2));

    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    unmount();
    storageWrite.mockClear();
    await act(async () => {
      resolveRefresh?.({ sessions: ["stale"], active: null });
      await Promise.resolve();
    });

    expect(storageWrite).not.toHaveBeenCalled();
    expect(loadWorkspaceSessions("C:/workspace")).toEqual([{ id: "default", title: "Predict churn" }]);
  });

  it("does not apply a post-confirm session refresh superseded by session navigation", async () => {
    let resolveRefresh: ((value: { sessions: string[]; active: null }) => void) | undefined;
    bridgeMocks.sessionsList
      .mockResolvedValueOnce({ sessions: ["default"], active: "default" })
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRefresh = resolve;
      }));
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["default"] });
    bridgeMocks.taskClarificationStart.mockResolvedValue(readyDraft);
    const { result } = renderHook(() => usePipeline("C:/workspace"));
    await waitFor(() => expect(result.current.sessions.map((session) => session.id)).toEqual(["default"]));
    await act(async () => {
      await result.current.sendPrompt("predict churn");
      await result.current.confirmDraft(false);
    });
    await waitFor(() => expect(bridgeMocks.sessionsList).toHaveBeenCalledTimes(2));

    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [], sessions: ["newer"] });
    await act(async () => {
      await result.current.switchSession("newer");
    });
    await act(async () => {
      resolveRefresh?.({ sessions: ["stale"], active: null });
      await Promise.resolve();
    });

    expect(result.current.sessions.map((session) => session.id)).toEqual(["newer"]);
    expect(loadWorkspaceSessions("C:/workspace")).toEqual([{ id: "newer", title: "新会话" }]);
  });

  it("keeps a failed optimistic row in React state without persisting it", async () => {
    const cacheKey = "athena.workspace.sessions:C:/workspace";
    const { result } = renderHook(() => usePipeline("C:/workspace"));
    await waitFor(() => expect(bridgeMocks.sessionSwitch).toHaveBeenCalledWith("default"));

    let rejectCreation: ((reason: Error) => void) | undefined;
    bridgeMocks.sessionSwitch.mockImplementation(
      () => new Promise((_, reject) => {
        rejectCreation = reject;
      }),
    );
    act(() => result.current.newSession());
    const newId = result.current.currentSessionId;
    expect(localStorage.getItem(cacheKey)).toBeNull();
    let deletion: Promise<void>;
    act(() => {
      deletion = result.current.deleteSession(newId);
    });

    await act(async () => {
      rejectCreation?.(new Error("creation failed"));
      await deletion!;
    });

    expect(bridgeMocks.sessionDelete).not.toHaveBeenCalled();
    expect(result.current.sessions.some((session) => session.id === newId)).toBe(true);
    expect(localStorage.getItem(cacheKey)).toBeNull();
    const messages = result.current.viewModel.messages;
    expect(messages[messages.length - 1]?.content).toContain("creation failed");
  });

  it("deletes the default session instead of silently ignoring it", async () => {
    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.currentSessionId).toBe("default");
    });
    await act(async () => {
      await result.current.deleteSession("default");
    });

    expect(bridgeMocks.sessionDelete).toHaveBeenCalledWith("default");
    expect(result.current.sessions).toEqual([]);
  });
});
