import { renderHook, waitFor } from "@testing-library/react";
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
  humanPending: vi.fn(),
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
  humanPending: bridgeMocks.humanPending,
  subscribeToPipelineEvents: bridgeMocks.subscribeToPipelineEvents,
  PIPELINE_EVENT_NAMES: ["state", "output"],
}));

import { usePipeline } from "../usePipeline";

/** 一条 message_id 特性上线之前写下的流式 delta 记录（逐 token 各占一行）。 */
function legacyDelta(seq: number, text: string, plan = "evaluator") {
  return { type: "output", seq, source: "agent", channel: "text", text, plan };
}

describe("usePipeline transcript replay", () => {
  beforeEach(() => {
    for (const mock of Object.values(bridgeMocks)) mock.mockReset();
    bridgeMocks.stateGet.mockResolvedValue({});
    bridgeMocks.sessionsList.mockResolvedValue({
      sessions: ["default"],
      active: "default",
      running: [],
    });
    bridgeMocks.sessionDelete.mockResolvedValue({ deleted: true, sessions: [] });
    bridgeMocks.humanPending.mockResolvedValue({ requests: [] });
    bridgeMocks.subscribeToPipelineEvents.mockResolvedValue([]);
  });

  it("merges legacy id-less agent deltas back into one message", async () => {
    // message_id 上线之前，一条 agent 消息的每个 token 都各自落了一行。按 seq 兜底
    // 会让它碎成一串单词气泡（实测某工作区 25,664 条旧记录 = 25,664 条消息）。
    bridgeMocks.sessionSwitch.mockResolvedValue({
      sessions: ["default"],
      records: [
        legacyDelta(1, "Let"),
        legacyDelta(2, " me"),
        legacyDelta(3, " inspect the data."),
        {
          type: "output",
          seq: 4,
          source: "agent",
          channel: "text",
          text: 'shell_command({"command": "ls"})',
          tool: "shell_command",
          plan: "evaluator",
        },
        legacyDelta(5, "Found"),
        legacyDelta(6, " train.csv."),
      ],
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.viewModel.messages.length).toBeGreaterThan(0);
    });

    expect(result.current.viewModel.messages.map((m) => m.content)).toEqual([
      "Let me inspect the data.",
      'shell_command({"command": "ls"})',
      "Found train.csv.",
    ]);
  });

  it("keeps separate plans apart when merging legacy deltas", async () => {
    bridgeMocks.sessionSwitch.mockResolvedValue({
      sessions: ["default"],
      records: [
        legacyDelta(1, "alpha", "ideator-0"),
        legacyDelta(2, "beta", "ideator-1"),
        legacyDelta(3, "gamma", "ideator-0"),
      ],
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.viewModel.messages.length).toBe(3);
    });
    expect(result.current.viewModel.messages.map((m) => m.content)).toEqual([
      "alpha",
      "beta",
      "gamma",
    ]);
  });

  it("still honours message_id when the records carry one", async () => {
    bridgeMocks.sessionSwitch.mockResolvedValue({
      sessions: ["default"],
      records: [
        {
          type: "output",
          seq: 1,
          message_id: "msg_a",
          source: "agent",
          channel: "text",
          text: "第一条完整消息",
          plan: "evaluator",
        },
        {
          type: "output",
          seq: 2,
          message_id: "msg_b",
          source: "agent",
          channel: "text",
          text: "第二条完整消息",
          plan: "evaluator",
        },
      ],
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.viewModel.messages.length).toBe(2);
    });
    expect(result.current.viewModel.messages.map((m) => m.content)).toEqual([
      "第一条完整消息",
      "第二条完整消息",
    ]);
  });

  it("survives a transcript larger than the argument-spread limit", async () => {
    // restoreRecords 曾用 Math.max(counter, ...records.map((r) => r.seq)) 取最大 seq：
    // 它把整个数组展开成实参，记录数一多就 RangeError（Maximum call stack size
    // exceeded），整个会话直接白屏。transcript 只增不减，这是迟早会撞上的。
    const records = [];
    for (let i = 1; i <= 200000; i += 1) {
      records.push(legacyDelta(i, `t${i}`, `plan-${Math.floor((i - 1) / 400)}`));
    }
    bridgeMocks.sessionSwitch.mockResolvedValue({ sessions: ["default"], records });

    const { result } = renderHook(() => usePipeline());

    await waitFor(
      () => {
        expect(result.current.viewModel.messages.length).toBeGreaterThan(0);
      },
      { timeout: 30000 },
    );
    // 400 条一组，恰好 500 条消息；修复前这里是 RangeError，异常被挂载路径的
    // catch 吞掉，表现为整段对话静默消失（messages 长度停在 0）。
    expect(result.current.viewModel.messages).toHaveLength(500);
  }, 60000);

  it("carries the backend truncation count through to the view", async () => {
    bridgeMocks.sessionSwitch.mockResolvedValue({
      sessions: ["default"],
      records: [legacyDelta(9001, "尾巴上的一条")],
      truncated: 21791,
    });

    const { result } = renderHook(() => usePipeline());

    await waitFor(() => {
      expect(result.current.truncatedRecords).toBe(21791);
    });
  });

  it("replays a large transcript without quadratic cost", async () => {
    // 真实工作区里 default.jsonl 已经 25,791 条：逐条走 reducer 每次整表拷贝一次，
    // 单次切会话要拷 3.3 亿个元素，切换因此卡死、CPU 打满。这条锁住重放是单趟的。
    const records = [];
    for (let i = 1; i <= 20000; i += 1) {
      records.push(legacyDelta(i, ` token${i}`, `plan-${i % 50}`));
    }
    bridgeMocks.sessionSwitch.mockResolvedValue({ sessions: ["default"], records });

    const started = Date.now();
    const { result } = renderHook(() => usePipeline());

    await waitFor(
      () => {
        expect(result.current.viewModel.messages.length).toBeGreaterThan(0);
      },
      { timeout: 20000 },
    );
    const elapsed = Date.now() - started;

    // 50 个 plan 轮流出现，所以每条记录都换 plan → 20000 条消息，纯拷贝上限场景。
    expect(result.current.viewModel.messages).toHaveLength(20000);
    // 修复前逐条走 reducer：同一台机器实测 1682ms；单趟重建后余量 2 倍以上。
    expect(elapsed).toBeLessThan(800);
  }, 30000);
});
