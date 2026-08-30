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
  PIPELINE_EVENT_NAMES: ["state", "output"],
}));

import { usePipeline } from "../usePipeline";

describe("usePipeline", () => {
  beforeEach(() => {
    bridgeMocks.startSearch.mockReset();
    bridgeMocks.pauseSearch.mockReset();
    bridgeMocks.resumeSearch.mockReset();
    bridgeMocks.stopSearch.mockReset();
    bridgeMocks.sendControl.mockReset();
    bridgeMocks.startValidation.mockReset();
    bridgeMocks.generateReport.mockReset();
    bridgeMocks.stateGet.mockReset();
    bridgeMocks.sessionsList.mockReset();
    bridgeMocks.sessionSwitch.mockReset();
    bridgeMocks.sessionDelete.mockReset();
    bridgeMocks.subscribeToPipelineEvents.mockReset();

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
  });

  it("adds a user message and a placeholder intent preview card when a prompt is sent", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("analyze this CSV");
    });

    expect(result.current.viewModel.messages.map((message) => message.kind)).toEqual([
      "text",
      "intent-preview",
    ]);
    expect(result.current.viewModel.messages[0]).toMatchObject({
      role: "user",
      content: "analyze this CSV",
    });
    // The frontend only shows a placeholder; the backend Supervisor owns the
    // actual task understanding and will fill this card through a state event.
    expect(result.current.viewModel.messages[1].preview?.primary_metric).toBe("");
    expect(result.current.viewModel.messages[1].preview?.needs_configuration).toBe(true);
    // 发送即启动：不调用前端的独立理解，直接交给后端统一理解并进入 PREPARE。
    expect(bridgeMocks.startSearch).toHaveBeenCalledWith({ task: "analyze this CSV" });
    expect(result.current.viewModel.status).toBe("running");
  });

  it("updates run state and context surface controls through the exposed API", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.startRun("analyze this CSV");
    });

    expect(bridgeMocks.startSearch).toHaveBeenCalledWith({ task: "analyze this CSV" });
    expect(result.current.viewModel.status).toBe("running");

    await act(async () => {
      await result.current.pauseRun();
    });
    expect(result.current.viewModel.status).toBe("paused");

    await act(async () => {
      await result.current.resumeRun();
    });
    expect(result.current.viewModel.status).toBe("running");

    await act(async () => {
      await result.current.stopRun();
    });
    expect(result.current.viewModel.status).toBe("completed");
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
    bridgeMocks.startSearch.mockResolvedValue({ ok: true });
    bridgeMocks.sendControl.mockResolvedValue({ response: "收到，已调整计划。" });
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.startRun("analyze this CSV");
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

    // 这些记录是升级前的形状（没有 message_id）：每条落盘记录各自成为一条消息，
    // 不会被并进上一条的尾巴里。
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
    // 空白判定只有后端看得见（会话目录里有没有 transcript / state.json）：
    // 前端曾用 view model 去猜，猜错就是误删一个真实会话。
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
