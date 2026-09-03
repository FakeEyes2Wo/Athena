import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const eventHandlers: Array<(event: { kind: string; data: Record<string, unknown> }) => void> = [];

vi.mock("../../lib/tauri-bridge", () => ({
  sendMessage: vi.fn().mockResolvedValue({}),
  sendControl: vi.fn().mockResolvedValue({ ok: true }),
  startSearch: vi.fn().mockResolvedValue({ ok: true }),
  pauseSearch: vi.fn().mockResolvedValue({ ok: true }),
  resumeSearch: vi.fn().mockResolvedValue({ ok: true }),
  stopSearch: vi.fn().mockResolvedValue({ ok: true }),
  startValidation: vi.fn().mockResolvedValue({ ok: true }),
  generateReport: vi.fn().mockResolvedValue({ ok: true }),
  humanPending: vi.fn().mockResolvedValue({ requests: [] }),
  humanReply: vi.fn().mockResolvedValue({ ok: true }),
  humanChoice: vi.fn().mockResolvedValue({ ok: true }),
  humanSkip: vi.fn().mockResolvedValue({ ok: true }),
  stateGet: vi.fn().mockResolvedValue({}),
  sessionsList: vi.fn().mockResolvedValue({ sessions: ["default"], active: "default" }),
  sessionSwitch: vi.fn().mockResolvedValue({ records: [], sessions: ["default"] }),
  sessionDelete: vi.fn().mockResolvedValue({ deleted: true, sessions: [] }),
  subscribeToPipelineEvents: vi.fn(async (handler) => {
    eventHandlers.push(handler);
    return [];
  }),
  taskClarificationStart: vi.fn().mockResolvedValue({
    draft_id: "draft-1",
    revision: 1,
    status: "CLARIFYING",
  }),
  taskClarificationGet: vi.fn().mockResolvedValue({
    draft_id: "draft-1",
    revision: 1,
    status: "CLARIFYING",
    understanding: { title: "", dataset: null, target: null, task_type: "other", primary_metric: null, direction: null, evaluation_plan: null },
    answers: [],
    unresolved: [],
    failure: null,
  }),
  taskClarificationRevise: vi.fn().mockResolvedValue({ draft_id: "draft-1", revision: 2, status: "CLARIFYING" }),
  taskClarificationRetry: vi.fn().mockResolvedValue({ draft_id: "draft-1", revision: 2, status: "CLARIFYING" }),
  taskClarificationCancel: vi.fn().mockResolvedValue({ ok: true }),
  PIPELINE_EVENT_NAMES: ["state", "output", "clarification", "human_request"],
}));

import { sessionSwitch, sessionsList } from "../../lib/tauri-bridge";
import { usePipeline } from "../usePipeline";

/** 一次真实 turn 的实时事件流：逐 delta，persist=False，seq 与落盘不同源。 */
const LIVE_EVENTS = [
  { type: "output", seq: 1, source: "agent", channel: "text", text: "Let me ", plan: "plan-1", message_id: "msg-a" },
  { type: "output", seq: 2, source: "agent", channel: "text", text: "inspect the ", plan: "plan-1", message_id: "msg-a" },
  { type: "output", seq: 3, source: "agent", channel: "text", text: "data.", plan: "plan-1", message_id: "msg-a" },
  { type: "output", seq: 5, source: "agent", channel: "text", tool: "shell_command", text: 'shell_command({"command": "ls"})', plan: "plan-1", message_id: "msg-tool" },
  { type: "output", seq: 6, source: "tool", channel: "stdout", text: "train.csv", plan: "plan-1", message_id: "msg-out" },
  { type: "output", seq: 7, source: "agent", channel: "text", text: "Found ", plan: "plan-1", message_id: "msg-b" },
  { type: "output", seq: 8, source: "agent", channel: "text", text: "train.csv.", plan: "plan-1", message_id: "msg-b" },
];

/** 同一段轨迹的落盘记录：agent 文本已合并成整条，seq 与实时流不同。 */
const PERSISTED_RECORDS = [
  { type: "output", seq: 4, source: "agent", channel: "text", text: "Let me inspect the data.", plan: "plan-1", message_id: "msg-a" },
  { type: "output", seq: 5, source: "agent", channel: "text", tool: "shell_command", text: 'shell_command({"command": "ls"})', plan: "plan-1", message_id: "msg-tool" },
  { type: "output", seq: 6, source: "tool", channel: "stdout", text: "train.csv", plan: "plan-1", message_id: "msg-out" },
  { type: "output", seq: 9, source: "agent", channel: "text", text: "Found train.csv.", plan: "plan-1", message_id: "msg-b" },
];

/** 让挂载回放停在 sessionsList 上，直到测试放行——复现「回放叠加在 live 之上」。 */
function deferHistory(records: Array<Record<string, unknown>>): () => void {
  let release = () => {};
  vi.mocked(sessionsList).mockImplementation(
    () =>
      new Promise((resolve) => {
        release = () => resolve({ sessions: ["default"], active: "default" });
      }),
  );
  vi.mocked(sessionSwitch).mockResolvedValue({ records: records as never, sessions: ["default"] });
  return () => release();
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("usePipeline message identity", () => {
  beforeEach(() => {
    eventHandlers.length = 0;
    vi.mocked(sessionsList).mockReset();
    vi.mocked(sessionSwitch).mockReset();
  });

  it("merges live deltas and the replayed record into one message per message_id", async () => {
    const release = deferHistory(PERSISTED_RECORDS);
    const { result } = renderHook(() => usePipeline());
    await flush();

    await act(async () => {
      for (const event of LIVE_EVENTS) eventHandlers[0]?.({ kind: "output", data: event });
    });
    release();
    await flush();

    const messages = result.current.viewModel.messages;
    expect(messages.map((m) => m.content)).toEqual([
      "Let me inspect the data.",
      'shell_command({"command": "ls"})',
      "train.csv",
      "Found train.csv.",
    ]);
    // React key 唯一：回放不再与 live 事件撞 key。
    const ids = messages.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
    // 没有任何一条消息把另一条整条吞进自己尾巴。
    expect(messages.some((m) => m.content.includes("Found train.csv.Let me"))).toBe(false);
  });

  it("keeps a whitespace-only delta that separates two words", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();

    const parts = ["The workspace is empty", " ", "and the data is at `D:\\tmp\\data`."];
    await act(async () => {
      parts.forEach((text, i) =>
        eventHandlers[0]?.({
          kind: "output",
          data: { type: "output", seq: i + 1, source: "agent", channel: "text", text, plan: "plan-1", message_id: "msg-w" },
        }),
      );
    });
    expect(result.current.viewModel.messages.map((m) => m.content)).toEqual([parts.join("")]);
  });

  it("keeps legacy records without message_id as separate messages", async () => {
    const legacy = PERSISTED_RECORDS.map(({ message_id, ...rest }) => {
      void message_id;
      return rest;
    });
    const release = deferHistory(legacy);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();

    const messages = result.current.viewModel.messages;
    expect(messages.map((m) => m.content)).toEqual([
      "Let me inspect the data.",
      'shell_command({"command": "ls"})',
      "train.csv",
      "Found train.csv.",
    ]);
    const ids = messages.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
