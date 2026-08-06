import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const bridgeMocks = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  startSearch: vi.fn(),
  pauseSearch: vi.fn(),
  resumeSearch: vi.fn(),
  stopSearch: vi.fn(),
  startValidation: vi.fn(),
  generateReport: vi.fn(),
  subscribeToPipelineEvents: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", () => ({
  sendMessage: bridgeMocks.sendMessage,
  startSearch: bridgeMocks.startSearch,
  pauseSearch: bridgeMocks.pauseSearch,
  resumeSearch: bridgeMocks.resumeSearch,
  stopSearch: bridgeMocks.stopSearch,
  startValidation: bridgeMocks.startValidation,
  generateReport: bridgeMocks.generateReport,
  subscribeToPipelineEvents: bridgeMocks.subscribeToPipelineEvents,
  PIPELINE_EVENT_NAMES: [
    "chat/token",
    "chat/intent_parsed",
    "experiment/started",
    "experiment/completed",
    "budget/update",
    "phase/change",
  ],
}));

import { usePipeline } from "../usePipeline";

describe("usePipeline", () => {
  beforeEach(() => {
    bridgeMocks.sendMessage.mockReset();
    bridgeMocks.startSearch.mockReset();
    bridgeMocks.pauseSearch.mockReset();
    bridgeMocks.resumeSearch.mockReset();
    bridgeMocks.stopSearch.mockReset();
    bridgeMocks.startValidation.mockReset();
    bridgeMocks.generateReport.mockReset();
    bridgeMocks.subscribeToPipelineEvents.mockReset();

    bridgeMocks.sendMessage.mockResolvedValue({
      task_type: "classification",
      data_type: "tabular",
      target_vars: ["target"],
      primary_metric: "f1_macro",
      direction: "maximize",
      needs_configuration: true,
    });
    bridgeMocks.startSearch.mockResolvedValue({ ok: true });
    bridgeMocks.pauseSearch.mockResolvedValue({ ok: true });
    bridgeMocks.resumeSearch.mockResolvedValue({ ok: true });
    bridgeMocks.stopSearch.mockResolvedValue({ ok: true });
    bridgeMocks.startValidation.mockResolvedValue({ ok: true });
    bridgeMocks.generateReport.mockResolvedValue({ ok: true });
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
  });

  it("updates run state and context surface controls through the exposed API", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.startRun({
        task_type: "classification",
        data_type: "tabular",
        target_vars: ["target"],
        primary_metric: "f1_macro",
        direction: "maximize",
        needs_configuration: true,
      });
    });

    expect(bridgeMocks.startSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        max_experiments: 10,
        task_type: "classification",
      }),
    );
    expect(result.current.viewModel.status).toBe("running");

    act(() => {
      result.current.openPanel("report");
    });
    expect(result.current.viewModel.contextSurface).toEqual({
      isOpen: true,
      activePanel: "report",
    });

    act(() => {
      result.current.closePanel();
    });
    expect(result.current.viewModel.contextSurface).toEqual({
      isOpen: false,
      activePanel: "report",
    });

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
});
