import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const eventHandlers: Array<(event: { kind: string; data: Record<string, unknown> }) => void> = [];

vi.mock("../../lib/tauri-bridge", () => ({
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

import { sendControl, startSearch } from "../../lib/tauri-bridge";
import { usePipeline } from "../usePipeline";

describe("usePipeline event mapping", () => {
  beforeEach(() => {
    eventHandlers.length = 0;
    vi.mocked(sendControl).mockClear();
    vi.mocked(startSearch).mockClear();
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
    // No frontend-side parse_intent: a new prompt starts the backend-owned
    // task understanding/run instead of calling the Supervisor directly.
    expect(startSearch).toHaveBeenCalledWith({ task: "kaggle URL" });
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
      // 同一条消息的 delta 带同一个 message_id（后端在建立文本缓冲时分配）。
      eventHandlers[0]?.({
        kind: "output",
        data: { seq: 1, source: "agent", channel: "text", text: "正在", message_id: "msg-1" },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: { seq: 2, source: "agent", channel: "text", text: "生成假设", message_id: "msg-1" },
      });
    });

    expect(result.current.viewModel.messages).toHaveLength(1);
    expect(result.current.viewModel.messages[0].content).toBe("正在生成假设");
  });

  it("keeps the run controls live for a session restored as paused in PREPARE", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: { phase: "PREPARE", status: "WAITING" },
      });
    });

    expect(result.current.viewModel.status).toBe("paused");
    // 切走会话时 runStarted 被清空，而 PREPARE 阶段没有 plans/attempts/experiment，
    // runActive 若只看客户端证据就会是 false，"继续"按钮被永久禁用。
    expect(result.current.runActive).toBe(true);
  });

  it("leaves the run controls dead for a brand-new idle session", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: { phase: "PREPARE", status: "IDLE" },
      });
    });

    expect(result.current.runActive).toBe(false);
  });
});
