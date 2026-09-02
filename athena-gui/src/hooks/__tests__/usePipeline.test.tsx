import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
  beforeEach(() => {
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
    bridgeMocks.subscribeToPipelineEvents.mockResolvedValue([]);
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
