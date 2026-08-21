import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const eventHandlers: Array<(event: { kind: string; data: Record<string, unknown> }) => void> = [];

vi.mock("../../lib/tauri-bridge", () => ({
  sendMessage: vi.fn().mockResolvedValue({
    title: "图像分类 · f1_macro",
    dataset: "train.csv (tabular)",
    target: "label: multiclass",
    task_type: "classification",
    primary_metric: "f1_macro",
    direction: "maximize",
    evaluation_plan: "f1_macro over a held-out split",
    needs_configuration: true,
  }),
  sendControl: vi.fn().mockResolvedValue({ ok: true }),
  startSearch: vi.fn().mockResolvedValue({ ok: true }),
  pauseSearch: vi.fn().mockResolvedValue({ ok: true }),
  resumeSearch: vi.fn().mockResolvedValue({ ok: true }),
  stopSearch: vi.fn().mockResolvedValue({ ok: true }),
  startValidation: vi.fn().mockResolvedValue({ ok: true }),
  generateReport: vi.fn().mockResolvedValue({ ok: true }),
  stateGet: vi.fn().mockResolvedValue({}),
  sessionsList: vi.fn().mockResolvedValue({ sessions: [] }),
  sessionSwitch: vi.fn().mockResolvedValue({ records: [] }),
  subscribeToPipelineEvents: vi.fn(async (handler) => {
    eventHandlers.push(handler);
    return [];
  }),
  PIPELINE_EVENT_NAMES: ["state", "output"],
}));

import { sendControl, sendMessage } from "../../lib/tauri-bridge";
import { usePipeline } from "../usePipeline";

describe("usePipeline event mapping", () => {
  beforeEach(() => {
    eventHandlers.length = 0;
    vi.mocked(sendControl).mockClear();
    vi.mocked(sendMessage).mockClear();
  });

  it("maps a state event to phase, status, budget, and SOTA", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: {
          phase: "SEARCH",
          status: "RUNNING",
          search: { attempts: 3, limit: 10, successes: 2 },
          sota: { experiment: "exp-1", metric: 0.83, commit: "abc" },
        },
      });
    });

    expect(result.current.viewModel.phase).toBe("SEARCH");
    expect(result.current.viewModel.status).toBe("running");
    expect(result.current.viewModel.rightRail.budgetRemaining).toBe(7);
    expect(result.current.viewModel.rightRail.bestPrimary).toBe(0.83);
    expect(result.current.viewModel.rightRail.latestExperimentId).toBe("exp-1");
  });

  it("does not treat a stale RUNNING status as an active run", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: { phase: "SEARCH", status: "RUNNING" },
      });
    });

    await act(async () => {
      await result.current.sendPrompt("kaggle URL");
    });

    expect(result.current.viewModel.status).toBe("running");
    expect(sendMessage).toHaveBeenCalledWith("kaggle URL");
    expect(sendControl).not.toHaveBeenCalled();
  });

  it("updates the intent preview card from the supervisor task understanding", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      await result.current.sendPrompt("analyze this CSV");
    });

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: {
          phase: "PREPARE",
          status: "RUNNING",
          task_understanding: {
            title: "Titanic 分类",
            dataset: "train.csv",
            target: "survived",
            task_type: "classification",
            primary_metric: "accuracy",
            direction: "maximize",
            evaluation_plan: "holdout accuracy",
          },
        },
      });
    });

    const preview = result.current.viewModel.messages.find(
      (m) => m.kind === "intent-preview",
    );
    expect(preview?.preview?.title).toBe("Titanic 分类");
    expect(preview?.preview?.primary_metric).toBe("accuracy");
  });

  it("appends output events as athena messages", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({
        kind: "output",
        data: { seq: 1, source: "supervisor", channel: "text", text: "开始准备" },
      });
    });

    expect(result.current.viewModel.messages.map((m) => m.kind)).toEqual(["text"]);
    expect(result.current.viewModel.messages[0]).toMatchObject({
      role: "athena",
      content: "开始准备",
    });
  });

  it("coalesces streaming agent text deltas into one message", async () => {    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({
        kind: "output",
        data: { seq: 1, source: "agent", channel: "text", text: "正在" },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: { seq: 2, source: "agent", channel: "text", text: "生成假设" },
      });
    });

    expect(result.current.viewModel.messages).toHaveLength(1);
    expect(result.current.viewModel.messages[0].content).toBe("正在生成假设");
  });
});
