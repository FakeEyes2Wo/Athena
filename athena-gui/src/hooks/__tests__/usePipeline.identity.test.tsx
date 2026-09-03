import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const eventHandlers: Array<(event: { kind: string; data: Record<string, unknown> }) => void> = [];
let nextAnimationFrameId = 1;
let animationFrames = new Map<number, FrameRequestCallback>();

function runNextAnimationFrame(): void {
  const next = animationFrames.entries().next().value as
    | [number, FrameRequestCallback]
    | undefined;
  if (!next) throw new Error("No animation frame was scheduled");
  animationFrames.delete(next[0]);
  next[1](16);
}

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
    nextAnimationFrameId = 1;
    animationFrames = new Map();
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      const id = nextAnimationFrameId;
      nextAnimationFrameId += 1;
      animationFrames.set(id, callback);
      return id;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => {
      animationFrames.delete(id);
    }));
    eventHandlers.length = 0;
    vi.mocked(sessionsList).mockReset();
    vi.mocked(sessionSwitch).mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("merges live deltas and the replayed record into one message per message_id", async () => {
    const release = deferHistory(PERSISTED_RECORDS);
    const { result } = renderHook(() => usePipeline());
    await flush();

    await act(async () => {
      for (const event of LIVE_EVENTS) eventHandlers[0]?.({ kind: "output", data: event });
    });
    expect(result.current.viewModel.messages).toEqual([]);
    act(() => runNextAnimationFrame());
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

  it("lets authoritative hydration replace a queued live delta before the stale frame runs", async () => {
    const release = deferHistory([
      {
        type: "output",
        seq: 2,
        source: "agent",
        channel: "text",
        text: "A",
        message_id: "same-message",
      },
    ]);
    const { result } = renderHook(() => usePipeline());
    await flush();

    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 1,
          source: "agent",
          channel: "text",
          text: "A",
          message_id: "same-message",
        },
      });
    });
    const staleFrame = animationFrames.get(1);
    expect(staleFrame).toBeDefined();
    expect(result.current.viewModel.messages).toEqual([]);

    release();
    await flush();

    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual(["A"]);
    expect(result.current.logs.map((entry) => entry.text)).toEqual(["A"]);
    expect(cancelAnimationFrame).toHaveBeenCalledWith(1);

    act(() => staleFrame?.(16));

    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual(["A"]);
    expect(result.current.logs.map((entry) => entry.text)).toEqual(["A"]);
  });

  it("discards queued output and logs when starting a new session", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();

    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 1,
          source: "agent",
          channel: "text",
          text: "old session",
          message_id: "old-session-message",
        },
      });
    });
    const staleFrame = animationFrames.get(1);
    expect(staleFrame).toBeDefined();

    vi.mocked(sessionSwitch).mockImplementation(() => new Promise(() => {}));
    act(() => result.current.newSession());
    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 2,
          source: "agent",
          channel: "text",
          text: "new session",
          message_id: "new-session-message",
        },
      });
      staleFrame?.(16);
    });

    expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
    expect(result.current.viewModel.messages).toEqual([]);
    expect(result.current.logs).toEqual([]);

    act(() => runNextAnimationFrame());

    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual([
      "new session",
    ]);
    expect(result.current.logs.map((entry) => entry.text)).toEqual(["new session"]);
  });

  it("discards queued prior-session output and logs after a successful switch", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();

    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 1,
          source: "agent",
          channel: "text",
          text: "old session",
          message_id: "old-session-message",
        },
      });
    });
    const staleFrame = animationFrames.get(1);
    expect(staleFrame).toBeDefined();
    vi.mocked(sessionSwitch).mockResolvedValue({
      records: [
        {
          type: "output",
          seq: 2,
          source: "agent",
          channel: "text",
          text: "new session",
          message_id: "new-session-message",
        },
      ] as never,
      sessions: ["new-session"],
    });

    await act(async () => {
      await result.current.switchSession("new-session");
    });
    act(() => staleFrame?.(16));

    expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual([
      "new session",
    ]);
    expect(result.current.logs).toEqual([]);
  });

  it("keeps a whitespace-only delta that separates two words", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();

    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 0,
          source: "supervisor",
          channel: "text",
          text: "stable",
          message_id: "stable-message",
        },
      });
    });
    act(() => runNextAnimationFrame());
    const stableMessage = result.current.viewModel.messages[0];

    const parts = ["The workspace is empty", " ", "and the data is at `D:\\tmp\\data`."];
    await act(async () => {
      parts.forEach((text, i) =>
        eventHandlers[0]?.({
          kind: "output",
          data: { type: "output", seq: i + 1, source: "agent", channel: "text", text, plan: "plan-1", message_id: "msg-w" },
        }),
      );
    });
    expect(result.current.viewModel.messages.map((m) => m.content)).toEqual(["stable"]);
    expect(result.current.viewModel.messages[0]).toBe(stableMessage);

    act(() => runNextAnimationFrame());

    expect(result.current.viewModel.messages.map((m) => m.content)).toEqual([
      "stable",
      parts.join(""),
    ]);
    expect(result.current.viewModel.messages[0]).toBe(stableMessage);
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

  it("restores only legacy and selected-session scoped records with metadata", async () => {
    const release = deferHistory([
      {
        type: "output",
        seq: 1,
        source: "agent",
        channel: "text",
        text: "selected scoped",
        message_id: "selected-scoped",
        session_id: "default",
        scope: "task_understanding",
        scope_id: "draft-1",
      },
      {
        type: "output",
        seq: 2,
        source: "agent",
        channel: "text",
        text: "other scoped",
        message_id: "other-scoped",
        session_id: "other-session",
        scope: "task_understanding",
        scope_id: "draft-1",
      },
      {
        type: "output",
        seq: 3,
        source: "agent",
        channel: "text",
        text: "partial scoped",
        message_id: "partial-scoped",
        session_id: "default",
        scope: "task_understanding",
      },
      {
        type: "output",
        seq: 4,
        source: "agent",
        channel: "text",
        text: "legacy transcript",
        message_id: "legacy-transcript",
      },
    ]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();

    expect(result.current.viewModel.messages).toEqual([
      expect.objectContaining({
        id: "selected-scoped",
        content: "selected scoped",
        sessionId: "default",
        scope: "task_understanding",
        scopeId: "draft-1",
      }),
      expect.objectContaining({ id: "legacy-transcript", content: "legacy transcript" }),
    ]);
  });

  it("gates old scoped output synchronously after a successful session switch", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();
    vi.mocked(sessionSwitch).mockResolvedValue({ records: [], sessions: ["next"] });

    await act(async () => {
      await result.current.switchSession("next");
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 1,
          source: "agent",
          channel: "text",
          text: "stale output",
          message_id: "stale-output",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-old",
        },
      });
    });

    expect(animationFrames.size).toBe(0);
    expect(result.current.currentSessionId).toBe("next");
    expect(result.current.viewModel.messages).toEqual([]);
    expect(result.current.logs).toEqual([]);
  });

  it("leaves the current session and queued output intact when switches are superseded or fail", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();
    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 1,
          source: "agent",
          channel: "text",
          text: "still current",
          message_id: "current-output",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-current",
        },
      });
    });

    let resolveFirst = (_value: { records: never[]; sessions: string[] }) => {};
    let rejectSecond = (_error: Error) => {};
    vi.mocked(sessionSwitch)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectSecond = reject;
          }),
      );
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.switchSession("first");
      second = result.current.switchSession("second");
    });
    await act(async () => {
      resolveFirst({ records: [], sessions: ["first"] });
      await first;
    });
    await act(async () => {
      rejectSecond(new Error("switch failed"));
      await second;
    });

    expect(result.current.currentSessionId).toBe("default");
    expect(animationFrames.size).toBe(1);
    act(() => runNextAnimationFrame());
    expect(result.current.viewModel.messages).toContainEqual(
      expect.objectContaining({ id: "current-output", content: "still current" }),
    );
    expect(result.current.logs.map((entry) => entry.text)).toEqual(["still current"]);
  });

  it("restores the former conversation when optimistic session creation fails", async () => {
    const release = deferHistory([]);
    const { result } = renderHook(() => usePipeline());
    await flush();
    release();
    await flush();
    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 1,
          source: "agent",
          channel: "text",
          text: "former conversation",
          message_id: "former-message",
        },
      });
    });
    act(() => runNextAnimationFrame());
    vi.mocked(sessionSwitch).mockRejectedValueOnce(new Error("creation failed"));

    act(() => {
      result.current.newSession();
      eventHandlers[0]?.({
        kind: "output",
        data: {
          type: "output",
          seq: 2,
          source: "agent",
          channel: "text",
          text: "old runtime after optimistic switch",
          message_id: "late-old-output",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-old",
        },
      });
    });
    const optimisticId = result.current.currentSessionId;
    await flush();

    expect(result.current.currentSessionId).toBe("default");
    expect(result.current.sessions.some((session) => session.id === optimisticId)).toBe(true);
    expect(result.current.viewModel.messages).toEqual([
      expect.objectContaining({ id: "former-message", content: "former conversation" }),
      expect.objectContaining({ kind: "error", content: "creation failed" }),
    ]);
    expect(result.current.logs.map((entry) => entry.text)).toEqual(["former conversation"]);
    expect(animationFrames.size).toBe(0);
  });
});
