import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const eventHandlers: Array<(event: { kind: string; data: Record<string, unknown> }) => void> = [];

vi.mock("../../lib/tauri-bridge", () => ({
  sendMessage: vi.fn().mockResolvedValue({
    task_type: "classification",
    data_type: "tabular",
    target_vars: ["target"],
    primary_metric: "f1_macro",
    direction: "maximize",
    needs_configuration: true,
  }),
  startSearch: vi.fn().mockResolvedValue({ ok: true }),
  pauseSearch: vi.fn().mockResolvedValue({ ok: true }),
  resumeSearch: vi.fn().mockResolvedValue({ ok: true }),
  stopSearch: vi.fn().mockResolvedValue({ ok: true }),
  startValidation: vi.fn().mockResolvedValue({ ok: true }),
  generateReport: vi.fn().mockResolvedValue({ ok: true }),
  subscribeToPipelineEvents: vi.fn(async (handler) => {
    eventHandlers.push(handler);
    return [];
  }),
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

describe("usePipeline event mapping", () => {
  beforeEach(() => {
    eventHandlers.length = 0;
  });

  it("updates the right rail when experiment and budget events arrive", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({ kind: "budget/update", data: { remaining: 6, no_improve_streak: 1 } });
      eventHandlers[0]?.({ kind: "experiment/completed", data: { experiment_id: "exp-7", primary: 0.83 } });
    });

    expect(result.current.viewModel.rightRail.budgetRemaining).toBe(6);
    expect(result.current.viewModel.rightRail.noImproveStreak).toBe(1);
    expect(result.current.viewModel.rightRail.bestPrimary).toBe(0.83);
    expect(result.current.viewModel.rightRail.latestExperimentId).toBe("exp-7");
  });

  it("handles phase/change events", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({ kind: "phase/change", data: { phase: "VALIDATION" } });
    });

    expect(result.current.viewModel.phase).toBe("VALIDATION");
  });
});
