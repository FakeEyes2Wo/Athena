import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const bridgeMocks = vi.hoisted(() => ({
  sendMessage: vi.fn(),
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
  sendMessage: bridgeMocks.sendMessage,
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
    bridgeMocks.sendMessage.mockReset();
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

    bridgeMocks.sendMessage.mockResolvedValue({
      title: "图像分类 · f1_macro",
      dataset: "train.csv (tabular)",
      target: "label: multiclass",
      task_type: "classification",
      primary_metric: "f1_macro",
      direction: "maximize",
      evaluation_plan: "f1_macro over a held-out split",
      needs_configuration: true,
    });
    bridgeMocks.startSearch.mockResolvedValue({ ok: true });
    bridgeMocks.pauseSearch.mockResolvedValue({ ok: true });
    bridgeMocks.resumeSearch.mockResolvedValue({ ok: true });
    bridgeMocks.stopSearch.mockResolvedValue({ ok: true });
    bridgeMocks.sendControl.mockResolvedValue({ ok: true });
    bridgeMocks.startValidation.mockResolvedValue({ ok: true });
    bridgeMocks.generateReport.mockResolvedValue({ ok: true });
    bridgeMocks.stateGet.mockResolvedValue({});
    bridgeMocks.sessionsList.mockResolvedValue({ sessions: [] });
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [] });
    bridgeMocks.sessionDelete.mockResolvedValue({ deleted: true, sessions: [] });
    bridgeMocks.subscribeToPipelineEvents.mockResolvedValue([]);
  });

  it("adds a user message and an intent preview card when a prompt is sent", async () => {
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
    expect(result.current.viewModel.messages[1].preview?.primary_metric).toBe("f1_macro");
    // 发送即启动：拿到任务理解后应自动调用 start_search。
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

  it("routes slash commands to the matching runtime control instead of parse_intent", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("/pause");
    });
    expect(bridgeMocks.pauseSearch).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
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
    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
  });

  it("routes prose to the supervisor while a run is active instead of parse_intent", async () => {
    bridgeMocks.startSearch.mockResolvedValue({ ok: true });
    bridgeMocks.sendControl.mockResolvedValue({ response: "收到，已调整计划。" });
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.startRun("analyze this CSV");
    });
    await act(async () => {
      await result.current.sendPrompt("请优先做草莓");
    });

    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
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

  it("auto-deletes a blank new session when switching away", async () => {
    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.currentSessionId).toBe("default");
    });
    await act(async () => {
      result.current.newSession();
    });
    const blankId = result.current.currentSessionId;
    expect(blankId).not.toBe("default");

    await act(async () => {
      await result.current.switchSession("default");
    });

    expect(bridgeMocks.sessionDelete).toHaveBeenCalledWith(blankId);
  });

  it("does not delete a non-blank session when creating a new one", async () => {
    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.currentSessionId).toBe("default");
    });
    await act(async () => {
      result.current.newSession();
    });
    const usedId = result.current.currentSessionId;

    // 在这个命名会话里发送并自动启动，使其不再是空白会话。
    await act(async () => {
      await result.current.sendPrompt("analyze this CSV");
    });
    expect(result.current.viewModel.messages.length).toBeGreaterThan(0);

    await act(async () => {
      result.current.newSession();
    });

    expect(bridgeMocks.sessionDelete).not.toHaveBeenCalledWith(usedId);
  });
});
