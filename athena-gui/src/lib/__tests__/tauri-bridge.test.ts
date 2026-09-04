import { beforeEach, describe, expect, it, vi } from "vitest";

const { listenMock, invokeMock } = vi.hoisted(() => ({
  listenMock: vi.fn(),
  invokeMock: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: listenMock,
}));

import humanReplyValid from "../../../../test/fixtures/clarification/human_reply_valid.json";
import humanRequestValid from "../../../../test/fixtures/clarification/human_request_valid.json";
import {
  EMPTY_RESEARCH_TREE,
  PIPELINE_EVENT_NAMES,
  humanReply,
  resumeSearch,
  startSearch,
  subscribeToPipelineEvents,
  taskClarificationCancel,
  taskClarificationGet,
  taskClarificationRetry,
  taskClarificationRevise,
  taskClarificationStart,
} from "../tauri-bridge";
import type { HumanReply, HumanRequest } from "../tauri-bridge";

describe("subscribeToPipelineEvents", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    listenMock.mockReset();
    listenMock.mockImplementation((name, handler) => {
      if (name === "state") {
        handler({ payload: { data: { phase: "SEARCH" } } });
      }
      return Promise.resolve(() => {});
    });
  });

  it("defines the strict v2 empty tree fallback", () => {
    expect(EMPTY_RESEARCH_TREE).toEqual({
      version: 2,
      sota_id: null,
      hypotheses: {},
      experiments: {},
    });
  });

  it("subscribes to the backend event channels", async () => {
    const unlisten = await subscribeToPipelineEvents(() => {});

    expect(listenMock.mock.calls.map(([name]) => name)).toEqual([...PIPELINE_EVENT_NAMES]);
    expect(unlisten).toHaveLength(PIPELINE_EVENT_NAMES.length);
  });

  it("normalizes missing event kinds to the listened channel", async () => {
    const received: Array<{ kind: string; data: { phase?: string } }> = [];

    await subscribeToPipelineEvents((event) => {
      received.push(event);
    });

    expect(received[0]).toMatchObject({
      kind: "state",
      data: { phase: "SEARCH" },
    });
  });
});

describe("clarification RPC normalization", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    invokeMock.mockResolvedValue({});
    listenMock.mockReset();
    listenMock.mockResolvedValue(() => {});
  });

  it("startSearch normalizes to Tauri camelCase args", () => {
    startSearch("draft-1", 4, true);
    expect(invokeMock).toHaveBeenCalledWith("start_search", {
      draftId: "draft-1",
      revision: 4,
      acknowledgeUnresolved: true,
    });
  });

  it("uses the shared native resume command", () => {
    resumeSearch();
    expect(invokeMock).toHaveBeenCalledWith("resume_search", undefined);
  });

  it("uses the WebSocket resume RPC outside the native shell", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(window, "__TAURI_INTERNALS__");
    const call = vi.fn().mockResolvedValue({});
    Object.defineProperty(window, "__TAURI_INTERNALS__", {
      configurable: true,
      value: undefined,
    });
    delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    vi.resetModules();
    vi.doMock("../ws-backend", () => ({ wsBackend: { call } }));

    try {
      const browserBridge = await import("../tauri-bridge");
      await browserBridge.resumeSearch();

      expect(call).toHaveBeenCalledWith("resume", {});
      expect(invokeMock).not.toHaveBeenCalled();
    } finally {
      if (descriptor) Object.defineProperty(window, "__TAURI_INTERNALS__", descriptor);
      vi.doUnmock("../ws-backend");
      vi.resetModules();
    }
  });

  it("clarification methods normalize draft_id to draftId", () => {
    taskClarificationStart("predict churn");
    expect(invokeMock).toHaveBeenCalledWith("task_clarification_start", { task: "predict churn" });

    taskClarificationGet("draft-1");
    expect(invokeMock).toHaveBeenCalledWith("task_clarification_get", { draftId: "draft-1" });

    taskClarificationRetry("draft-1", 2);
    expect(invokeMock).toHaveBeenCalledWith("task_clarification_retry", { draftId: "draft-1", revision: 2 });

    taskClarificationRevise("draft-1", 2, "add details");
    expect(invokeMock).toHaveBeenCalledWith("task_clarification_revise", {
      draftId: "draft-1",
      revision: 2,
      instruction: "add details",
    });

    taskClarificationCancel("draft-1", 2);
    expect(invokeMock).toHaveBeenCalledWith("task_clarification_cancel", {
      draftId: "draft-1",
      revision: 2,
    });
  });

  it("humanReply sends the typed reply object to Tauri", () => {
    humanReply("req-1", { kind: "skip" });
    expect(invokeMock).toHaveBeenCalledWith("human_reply", {
      requestId: "req-1",
      reply: { kind: "skip" },
    });
  });
});

describe("shared typed contract fixtures", () => {
  it("the valid fixture matches the TypeScript HumanRequest shape", () => {
    const request = humanRequestValid as HumanRequest;
    expect(request.request_id).toBe("req-1");
    expect(request.choices).toHaveLength(2);
  });

  it("the valid reply fixture matches the TypeScript HumanReply union", () => {
    const replies = humanReplyValid as HumanReply[];
    expect(replies).toHaveLength(3);
    expect(replies[0]).toMatchObject({ kind: "choice", value: "f1" });
    expect(replies[1]).toMatchObject({ kind: "text", text: "use macro F1" });
    expect(replies[2]).toEqual({ kind: "skip" });
  });
});
