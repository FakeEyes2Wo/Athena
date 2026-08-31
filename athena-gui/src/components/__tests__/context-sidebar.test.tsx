import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { ContextSidebar } from "../shell/ContextSidebar";

const bridgeMocks = vi.hoisted(() => ({ sessionsListFor: vi.fn() }));

vi.mock("../../lib/tauri-bridge", () => ({
  sessionsListFor: bridgeMocks.sessionsListFor,
}));

function renderSidebar(currentRoot: string, recentRoots: string[]) {
  return renderUi(
    <ContextSidebar
      module="session"
      currentRoot={currentRoot}
      recentRoots={recentRoots}
      sessions={[{ id: "default", title: "会话" }]}
      runningSessions={[]}
      currentSessionId="default"
      onSwitchWorkspace={vi.fn()}
      onSelectWorkspace={vi.fn()}
      onSelectSession={vi.fn()}
      onNewSession={vi.fn()}
      onDeleteSession={vi.fn()}
      onOpenDrawer={vi.fn()}
    />,
  );
}

/** 侧栏里工作区分组的渲染顺序（按各组 nav 的 aria-label）。 */
function renderedWorkspaceOrder(): string[] {
  return screen
    .getAllByRole("navigation")
    .map((nav) => (nav.getAttribute("aria-label") ?? "").replace(" 会话", ""));
}

describe("ContextSidebar 工作区顺序", () => {
  beforeEach(() => {
    bridgeMocks.sessionsListFor.mockReset();
    bridgeMocks.sessionsListFor.mockResolvedValue({ sessions: [], running: [] });
  });

  it("按 recentRoots 的顺序渲染，当前工作区不被置顶", async () => {
    // 选中某个工作区的会话不该把它拽到最前面——顺序原本是什么样，选完还是什么样。
    renderSidebar("/w/beta", ["/w/alpha", "/w/beta", "/w/gamma"]);

    await waitFor(() => {
      expect(renderedWorkspaceOrder()).toEqual(["alpha", "beta", "gamma"]);
    });
  });

  it("当前工作区不在 recentRoots 里时仍然显示", async () => {
    renderSidebar("/w/fresh", ["/w/alpha"]);

    await waitFor(() => {
      expect(renderedWorkspaceOrder()).toEqual(["fresh", "alpha"]);
    });
  });

  it("每个工作区只渲染一组", async () => {
    renderSidebar("/w/beta", ["/w/alpha", "/w/beta"]);

    await waitFor(() => {
      expect(renderedWorkspaceOrder()).toEqual(["alpha", "beta"]);
    });
    expect(bridgeMocks.sessionsListFor).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.sessionsListFor).toHaveBeenCalledWith("/w/alpha");
  });
});
