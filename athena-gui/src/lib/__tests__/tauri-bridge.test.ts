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

import {
  PIPELINE_EVENT_NAMES,
  EMPTY_RESEARCH_TREE,
  subscribeToPipelineEvents,
} from "../tauri-bridge";

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

  it("subscribes to the backend state/output event channels", async () => {
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
