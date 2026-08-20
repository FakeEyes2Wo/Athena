import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/** 后端推来的 state 帧类型（只用到本文件断言的字段）。 */
type StateFrame = { kind: string; data: Record<string, unknown> };

const eventHandlers: Array<(event: StateFrame) => void> = [];

const bridgeMocks = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  sendControl: vi.fn(),
  startSearch: vi.fn(),
  pauseSearch: vi.fn(),
  resumeSearch: vi.fn(),
  stopSearch: vi.fn(),
  stateGet: vi.fn(),
  sessionsList: vi.fn(),
  sessionSwitch: vi.fn(),
  sessionDelete: vi.fn(),
  humanPending: vi.fn(),
  humanReply: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", () => ({
  ...bridgeMocks,
  subscribeToPipelineEvents: vi.fn(async (handler: (event: StateFrame) => void) => {
    eventHandlers.push(handler);
    return [];
  }),
  PIPELINE_EVENT_NAMES: ["state", "output"],
}));

import { usePipeline } from "../usePipeline";

/** 全新 runtime 上报的状态帧：RUNNING/SEARCH，但没有任何真实活动。 */
function freshRuntimeFrame(overrides: Record<string, unknown> = {}): StateFrame {
  return {
    kind: "state",
    data: {
      phase: "SEARCH",
      status: "RUNNING",
      plans: [],
      search: { attempts: 0, limit: 10, successes: 0, concurrency: 2 },
      sota: null,
      ...overrides,
    },
  };
}

describe("usePipeline first-prompt routing", () => {
  beforeEach(() => {
    eventHandlers.length = 0;
    Object.values(bridgeMocks).forEach((mock) => mock.mockReset());
    bridgeMocks.sendMessage.mockResolvedValue({
      title: "Titanic 分类",
      dataset: "train.csv",
      target: "survived",
      task_type: "classification",
      primary_metric: "accuracy",
      direction: "maximize",
      evaluation_plan: "holdout accuracy",
      needs_configuration: false,
    });
    bridgeMocks.sendControl.mockResolvedValue({ response: "收到。" });
    bridgeMocks.startSearch.mockResolvedValue({ ok: true });
    bridgeMocks.stateGet.mockResolvedValue({});
    bridgeMocks.sessionsList.mockResolvedValue({ sessions: [] });
    bridgeMocks.sessionSwitch.mockResolvedValue({ records: [] });
    bridgeMocks.humanPending.mockResolvedValue({ requests: [] });
  });

  // 回归：网关的 fresh runtime 一连上就推 RUNNING/SEARCH，但阶段机从未启动。
  // 光看 status 会把首条任务误路由到 message，start_search 永远不被调用。
  it("routes the first prompt to parse_intent when the reported run has no activity", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.(freshRuntimeFrame());
    });
    await act(async () => {
      await result.current.sendPrompt("predict titanic survival");
    });

    expect(bridgeMocks.sendControl).not.toHaveBeenCalled();
    expect(bridgeMocks.sendMessage).toHaveBeenCalledWith("predict titanic survival");
    expect(result.current.viewModel.messages.map((m) => m.kind)).toEqual([
      "text",
      "intent-preview",
    ]);
  });

  it("routes prose to the supervisor once the backend reports active plans", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.(
        freshRuntimeFrame({ plans: [{ id: "hyp-1", kind: "SEARCH", turns_used: 1 }] }),
      );
    });
    await act(async () => {
      await result.current.sendPrompt("请优先做草莓");
    });

    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("请优先做草莓");
  });

  it("routes prose to the supervisor once search attempts exist", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.(
        freshRuntimeFrame({ search: { attempts: 2, limit: 10, successes: 1, concurrency: 2 } }),
      );
    });
    await act(async () => {
      await result.current.sendPrompt("请优先做草莓");
    });

    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("请优先做草莓");
  });

  it("routes prose to the supervisor once a SOTA baseline exists", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.(
        freshRuntimeFrame({ sota: { experiment: "exp_baseline", metric: 0.78, commit: "abc" } }),
      );
    });
    await act(async () => {
      await result.current.sendPrompt("请优先做草莓");
    });

    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("请优先做草莓");
  });

  // startSearch 已发出但后端首帧尚未回来的窗口内，普通文本仍属于运行中对话。
  it("routes prose to the supervisor right after startRun, before any backend activity", async () => {
    const { result } = renderHook(() => usePipeline());

    await act(async () => {
      eventHandlers[0]?.(freshRuntimeFrame());
    });
    await act(async () => {
      await result.current.startRun("predict titanic survival");
    });
    await act(async () => {
      await result.current.sendPrompt("请优先做草莓");
    });

    expect(bridgeMocks.sendMessage).not.toHaveBeenCalled();
    expect(bridgeMocks.sendControl).toHaveBeenCalledWith("请优先做草莓");
  });

  // 后端修复后 fresh runtime 上报 IDLE：前端必须认得，否则顶栏状态回退成陈旧值。
  it("maps the IDLE runtime status to an idle pipeline status", async () => {
    const { result } = renderHook(() => usePipeline());

    // 先推 RUNNING 把 status 顶离初始值，避免断言空洞通过。
    await act(async () => {
      eventHandlers[0]?.(freshRuntimeFrame());
    });
    expect(result.current.viewModel.status).toBe("running");

    await act(async () => {
      eventHandlers[0]?.(freshRuntimeFrame({ status: "IDLE", phase: "PREPARE" }));
    });

    expect(result.current.viewModel.status).toBe("idle");
  });
});
