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

import { sendControl, startSearch, taskClarificationStart } from "../../lib/tauri-bridge";
import { usePipeline } from "../usePipeline";

describe("usePipeline event mapping", () => {
  beforeEach(() => {
    eventHandlers.length = 0;
    vi.mocked(sendControl).mockClear();
    vi.mocked(startSearch).mockClear();
    vi.mocked(taskClarificationStart).mockClear();
    vi.mocked(taskClarificationStart).mockResolvedValue({
      draft_id: "draft-1",
      revision: 1,
      status: "CLARIFYING",
    });
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

    expect(taskClarificationStart).toHaveBeenCalledWith("kaggle URL");
    expect(startSearch).not.toHaveBeenCalled();
    expect(sendControl).not.toHaveBeenCalled();
    expect(result.current.status).toBe("CLARIFYING");
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
    expect(preview?.preview).toMatchObject({
      title: "Titanic 分类",
      primary_metric: "accuracy",
    });
  });

  it("applies clarification events to the authoritative preview", async () => {
    const { result } = renderHook(() => usePipeline());

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
    const { result } = renderHook(() => usePipeline());

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
    const { result } = renderHook(() => usePipeline());

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
    const { result } = renderHook(() => usePipeline());

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
