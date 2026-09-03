import { act, renderHook, waitFor } from "@testing-library/react";
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
    status: "READY_FOR_CONFIRMATION",
    understanding: { title: "Titanic 分类", dataset: "train.csv", target: "survived" },
    answers: [],
    unresolved: [],
    failure: null,
  }),
  taskClarificationRevise: vi.fn().mockResolvedValue({ draft_id: "draft-1", revision: 2, status: "CLARIFYING" }),
  taskClarificationRetry: vi.fn().mockResolvedValue({ draft_id: "draft-1", revision: 2, status: "CLARIFYING" }),
  taskClarificationCancel: vi.fn().mockResolvedValue({ ok: true }),
  humanPending: vi.fn().mockResolvedValue({ requests: [] }),
  humanReply: vi.fn().mockResolvedValue({ ok: true }),
  PIPELINE_EVENT_NAMES: ["state", "output", "clarification", "human_request"],
}));

import { sendControl, sessionSwitch, startSearch, taskClarificationStart } from "../../lib/tauri-bridge";
import { usePipeline } from "../usePipeline";

async function renderHydratedPipeline() {
  const hook = renderHook(() => usePipeline());
  await waitFor(() => expect(sessionSwitch).toHaveBeenCalledWith("default"));
  await act(async () => undefined);
  return hook;
}

describe("usePipeline event mapping", () => {
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
    vi.mocked(sendControl).mockClear();
    vi.mocked(sessionSwitch).mockClear();
    vi.mocked(startSearch).mockClear();
    vi.mocked(taskClarificationStart).mockClear();
    vi.mocked(taskClarificationStart).mockResolvedValue({
      draft_id: "draft-1",
      revision: 1,
      status: "CLARIFYING",
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("maps a state event to phase, status, budget, and SOTA", async () => {
    const { result } = await renderHydratedPipeline();

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

  it("projects resume capability and clears omitted fields on the next complete snapshot", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: {
          phase: "PREPARE",
          status: "FAILED",
          resume_available: true,
          resume_reason: "failed",
        },
      });
    });
    expect(result.current.viewModel.resumeAvailable).toBe(true);
    expect(result.current.viewModel.resumeReason).toBe("failed");
    expect(result.current.runActive).toBe(true);

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: { phase: "PREPARE", status: "IDLE" },
      });
    });
    expect(result.current.viewModel.resumeAvailable).toBe(false);
    expect(result.current.viewModel.resumeReason).toBeNull();
    expect(result.current.runActive).toBe(false);
  });

  it("keeps an interrupted restored PREPARE session actionable", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: {
          phase: "PREPARE",
          status: "IDLE",
          resume_available: true,
          resume_reason: "interrupted",
        },
      });
    });

    expect(result.current.viewModel.status).toBe("idle");
    expect(result.current.viewModel.resumeAvailable).toBe(true);
    expect(result.current.runActive).toBe(true);
  });

  it("does not treat a stale RUNNING status as an active run", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: { phase: "SEARCH", status: "RUNNING" },
      });
    });

    await act(async () => {
      await result.current.sendPrompt("kaggle URL");
    });

    expect(taskClarificationStart).toHaveBeenCalledWith("kaggle URL");
    expect(startSearch).not.toHaveBeenCalled();
    expect(sendControl).not.toHaveBeenCalled();
    expect(result.current.status).toBe("CLARIFYING");
  });

  it("updates the intent preview card from the supervisor task understanding", async () => {
    const { result } = await renderHydratedPipeline();

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
    expect(preview?.preview).toMatchObject({
      title: "Titanic 分类",
      primary_metric: "accuracy",
    });
  });

  it("applies clarification events to the authoritative preview", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });

    await act(async () => {
      eventHandlers[0]?.({
        kind: "clarification",
        data: {
          session_id: "default",
          draft: {
            schema_version: 1,
            draft_id: "draft-1",
            revision: 3,
            session_id: "default",
            original_task: "predict churn",
            status: "READY_FOR_CONFIRMATION",
            understanding: {
              title: "Predict churn",
              dataset: "churn.csv",
              target: "churned",
              task_type: "classification",
              primary_metric: "f1",
              direction: "maximize",
              evaluation_plan: "holdout f1",
            },
            answers: [],
            unresolved: [],
            failure: null,
            questions_asked: 2,
            created_at: "2026-09-01T10:00:00Z",
            updated_at: "2026-09-01T10:00:00Z",
          },
        },
      });
    });

    const preview = result.current.viewModel.messages.find(
      (m) => m.kind === "intent-preview",
    );
    expect(preview?.preview).toMatchObject({ draftId: "draft-1", revision: 3 });
    expect(result.current.status).toBe("READY_FOR_CONFIRMATION");
  });

  it("shows clarification questions and user replies in the conversation flow", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });

    await act(async () => {
      eventHandlers[0]?.({
        kind: "human_request",
        data: {
          session_id: "default",
          action: "created",
          request: {
            request_id: "req-1",
            session_id: "default",
            scope_id: "draft-1",
            scope_kind: "clarification",
            prompt: "Which metric should be primary?",
            choices: [
              { label: "F1", value: "f1" },
              { label: "AUC", value: "auc" },
            ],
            allow_custom: true,
            allow_skip: true,
            created_at: "2026-09-01T10:00:00Z",
            expires_at: "2026-09-01T10:02:00Z",
          },
        },
      });
    });

    expect(
      result.current.viewModel.messages.find((m) => m.id === "clarify-q-req-1"),
    ).toMatchObject({
      role: "athena",
      content: expect.stringContaining("Which metric should be primary?"),
    });

    await act(async () => {
      await result.current.replyToHumanRequest("req-1", { kind: "text", text: "macro F1" });
    });

    expect(
      result.current.viewModel.messages.find((m) => m.id === "clarify-a-req-1"),
    ).toMatchObject({
      role: "user",
      content: "macro F1",
    });
  });

  it("ignores human requests from another session", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });

    await act(async () => {
      eventHandlers[0]?.({
        kind: "human_request",
        data: {
          session_id: "other-session",
          action: "created",
          request: {
            request_id: "req-other",
            session_id: "other-session",
            scope_id: "draft-other",
            scope_kind: "clarification",
            prompt: "Should not appear",
            choices: [],
            allow_custom: true,
            allow_skip: true,
            created_at: "2026-09-01T10:00:00Z",
            expires_at: "2026-09-01T10:02:00Z",
          },
        },
      });
    });

    expect(
      result.current.viewModel.messages.find((m) => m.id === "clarify-q-req-other"),
    ).toBeUndefined();
    expect(result.current.humanRequests).toHaveLength(0);
  });

  it("records server-generated timeout as a clarification note", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      await result.current.sendPrompt("predict churn");
    });

    await act(async () => {
      eventHandlers[0]?.({
        kind: "human_request",
        data: {
          session_id: "default",
          action: "settled",
          request_id: "req-timeout",
          outcome: { kind: "timeout", request_id: "req-timeout", value: null },
        },
      });
    });

    expect(
      result.current.viewModel.messages.find((m) => m.id === "clarify-o-req-timeout"),
    ).toMatchObject({ content: "该问题已超时" });
  });

  it("appends output events as athena messages", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      eventHandlers[0]?.({
        kind: "output",
        data: { seq: 1, source: "supervisor", channel: "text", text: "开始准备" },
      });
    });

    act(() => runNextAnimationFrame());

    expect(result.current.viewModel.messages.map((m) => m.kind)).toEqual(["text"]);
    expect(result.current.viewModel.messages[0]).toMatchObject({
      role: "athena",
      content: "开始准备",
    });
  });

  it("accepts only atomic current-session output and preserves scope metadata", async () => {
    const { result } = await renderHydratedPipeline();

    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 1,
          source: "agent",
          channel: "text",
          text: "Understanding the task",
          message_id: "scoped-agent",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-1",
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 2,
          source: "tool",
          channel: "stdout",
          text: "Reported progress",
          message_id: "scoped-tool",
          tool: "report_task_understanding",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-1",
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 7,
          source: "agent",
          channel: "text",
          text: " wrong scope",
          message_id: "scoped-agent",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-other",
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 3,
          source: "agent",
          channel: "text",
          text: "wrong session",
          message_id: "wrong-session",
          session_id: "other-session",
          scope: "task_understanding",
          scope_id: "draft-1",
        },
      });
      for (const metadata of [
        { session_id: "default" },
        { scope: "task_understanding" },
        { scope_id: "draft-1" },
        { session_id: "default", scope: "task_understanding" },
        { session_id: "default", scope_id: "draft-1" },
        { scope: "task_understanding", scope_id: "draft-1" },
      ]) {
        eventHandlers[0]?.({
          kind: "output",
          data: {
            seq: 4,
            source: "agent",
            channel: "text",
            text: "partial scope",
            message_id: `partial-${Object.keys(metadata).join("-")}`,
            ...metadata,
          },
        });
      }
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 5,
          source: "agent",
          channel: "text",
          text: "legacy output",
          message_id: "legacy-output",
          session_id: null,
          scope: null,
          scope_id: null,
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 6,
          source: "agent",
          channel: "text",
          text: " first",
          message_id: "scoped-agent",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-1",
        },
      });
    });

    act(() => runNextAnimationFrame());

    expect(result.current.viewModel.messages.map((message) => message.content)).toEqual([
      "Understanding the task first",
      "Reported progress",
      "legacy output",
    ]);
    expect(result.current.viewModel.messages.slice(0, 2)).toEqual([
      expect.objectContaining({
        sessionId: "default",
        scope: "task_understanding",
        scopeId: "draft-1",
      }),
      expect.objectContaining({
        sessionId: "default",
        scope: "task_understanding",
        scopeId: "draft-1",
      }),
    ]);
    expect(result.current.logs.map((entry) => entry.text)).toEqual([
      "Understanding the task",
      "Reported progress",
      " wrong scope",
      "legacy output",
      " first",
    ]);
  });

  it("latches the first optimistic task-understanding scope until the canonical draft", async () => {
    let releaseStart = () => {};
    vi.mocked(taskClarificationStart).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseStart = () =>
            resolve({ draft_id: "draft-canonical", revision: 1, status: "CLARIFYING" });
        }),
    );
    const { result } = await renderHydratedPipeline();
    let start!: Promise<void>;

    act(() => {
      start = result.current.sendPrompt("predict churn");
    });
    await act(async () => undefined);
    act(() => {
      for (const [seq, scopeId] of ["draft-a", "draft-b"].entries()) {
        eventHandlers[0]?.({
          kind: "output",
          data: {
            seq: seq + 1,
            source: "agent",
            channel: "text",
            text: scopeId,
            message_id: `message-${scopeId}`,
            session_id: "default",
            scope: "task_understanding",
            scope_id: scopeId,
          },
        });
      }
    });
    act(() => runNextAnimationFrame());

    const optimistic = result.current.viewModel.messages.find(
      (message) => message.kind === "intent-preview",
    )?.preview;
    expect(optimistic).toMatchObject({ draftId: "", optimisticScopeId: "draft-a" });
    expect(
      result.current.viewModel.messages
        .filter((message) => message.scope === "task_understanding")
        .map((message) => message.scopeId),
    ).toEqual(["draft-a", "draft-b"]);

    await act(async () => {
      releaseStart();
      await start;
    });

    const canonical = result.current.viewModel.messages.find(
      (message) => message.kind === "intent-preview",
    )?.preview;
    expect(canonical).toMatchObject({ draftId: "draft-canonical" });
    expect(canonical).toMatchObject({ optimisticScopeId: undefined });
  });

  it("does not latch task-understanding output after optimistic start fails", async () => {
    vi.mocked(taskClarificationStart).mockRejectedValueOnce(new Error("start failed"));
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      await result.current.sendPrompt("predict churn").catch(() => undefined);
    });
    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 1,
          source: "agent",
          channel: "text",
          text: "late output",
          message_id: "late-output",
          session_id: "default",
          scope: "task_understanding",
          scope_id: "draft-late",
        },
      });
    });
    act(() => runNextAnimationFrame());

    const preview = result.current.viewModel.messages.find(
      (message) => message.kind === "intent-preview",
    )?.preview;
    expect(preview).toMatchObject({ draftId: "" });
    expect(preview).not.toHaveProperty("optimisticScopeId");
  });

  it("batches a burst into one frame while preserving every ordered log entry", async () => {
    const { result } = await renderHydratedPipeline();
    const beforeBurst = result.current.viewModel;

    act(() => {
      for (let index = 0; index < 100; index += 1) {
        eventHandlers[0]?.({
          kind: "output",
          data: {
            seq: index + 1,
            source: "agent",
            channel: "text",
            text: `${index}|`,
            message_id: "burst-message",
          },
        });
      }
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 101,
          source: "agent",
          channel: "tool_call",
          text: '{"path":"train.csv"}',
          message_id: "burst-tool-call",
          tool: "inspect_dataset",
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 102,
          source: "tool",
          channel: "stdout",
          text: "rows=891",
          message_id: "burst-tool-output",
          tool: "inspect_dataset",
        },
      });
    });

    expect(result.current.viewModel).toBe(beforeBurst);
    expect(result.current.logs).toEqual([]);
    expect(requestAnimationFrame).toHaveBeenCalledTimes(1);

    act(() => runNextAnimationFrame());

    expect(result.current.viewModel.messages).toEqual([
      expect.objectContaining({
        id: "burst-message",
        content: Array.from({ length: 100 }, (_, index) => `${index}|`).join(""),
        source: "agent",
      }),
      expect.objectContaining({
        id: "burst-tool-call",
        content: '{"path":"train.csv"}',
        tool: "inspect_dataset",
      }),
      expect.objectContaining({
        id: "burst-tool-output",
        content: "rows=891",
        source: "tool",
        channel: "stdout",
      }),
    ]);
    expect(result.current.logs).toHaveLength(102);
    expect(result.current.logs.slice(0, 100).map((entry) => entry.text)).toEqual(
      Array.from({ length: 100 }, (_, index) => `${index}|`),
    );
    expect(result.current.logs.slice(-2)).toMatchObject([
      {
        id: "log-101",
        kind: "output",
        source: "agent",
        channel: "tool_call",
        tool: "inspect_dataset",
        text: '{"path":"train.csv"}',
      },
      {
        id: "log-102",
        kind: "output",
        source: "tool",
        channel: "stdout",
        tool: "inspect_dataset",
        text: "rows=891",
      },
    ]);
    expect(animationFrames.size).toBe(0);
  });

  it("cancels and discards a queued output frame on unmount", async () => {
    const { result, unmount } = await renderHydratedPipeline();

    act(() => {
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 1,
          source: "agent",
          channel: "text",
          text: "discard me",
          message_id: "pending-output",
        },
      });
    });

    expect(result.current.viewModel.messages).toEqual([]);
    expect(animationFrames.size).toBe(1);
    unmount();

    expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
    expect(animationFrames.size).toBe(0);
  });

  it("coalesces clarification output deltas and preserves backend tool activity", async () => {
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      await result.current.sendPrompt("analyze train.csv");
    });
    const existingMessages = result.current.viewModel.messages;

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
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 3,
          source: "agent",
          channel: "tool_call",
          text: '{"path":"train.csv"}',
          message_id: "tool-call-1",
          tool: "inspect_dataset",
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 4,
          source: "tool",
          channel: "stdout",
          text: "rows=891",
          message_id: "tool-output-1",
          tool: "inspect_dataset",
        },
      });
      eventHandlers[0]?.({
        kind: "output",
        data: {
          seq: 5,
          source: "tool",
          channel: "stderr",
          text: "validation failed",
          message_id: "tool-error-1",
          tool: "inspect_dataset",
        },
      });
    });

    act(() => runNextAnimationFrame());

    expect(result.current.viewModel.messages).toHaveLength(6);
    expect(result.current.viewModel.messages[0]).toBe(existingMessages[0]);
    expect(result.current.viewModel.messages[1]).toBe(existingMessages[1]);
    expect(result.current.viewModel.messages[2]).toMatchObject({
      id: "msg-1",
      content: "正在生成假设",
      source: "agent",
    });
    expect(result.current.viewModel.messages[3]).toMatchObject({
      id: "tool-call-1",
      content: '{"path":"train.csv"}',
      source: "agent",
      tool: "inspect_dataset",
    });
    expect(result.current.viewModel.messages[4]).toMatchObject({
      id: "tool-output-1",
      content: "rows=891",
      source: "tool",
      tool: "inspect_dataset",
      channel: "stdout",
    });
    expect(result.current.viewModel.messages[5]).toMatchObject({
      id: "tool-error-1",
      content: "validation failed",
      source: "tool",
      tool: "inspect_dataset",
      channel: "stderr",
    });
  });

  it("keeps the run controls live for a session restored as paused in PREPARE", async () => {
    const { result } = await renderHydratedPipeline();

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
    const { result } = await renderHydratedPipeline();

    await act(async () => {
      eventHandlers[0]?.({
        kind: "state",
        data: { phase: "PREPARE", status: "IDLE" },
      });
    });

    expect(result.current.runActive).toBe(false);
  });
});
