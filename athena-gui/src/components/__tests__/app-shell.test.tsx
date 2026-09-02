import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, fireEvent, waitFor, within } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { AppShell } from "../shell/AppShell";
import { createEmptyPipelineViewModel } from "../../types/ui";

const bridgeMocks = vi.hoisted(() => ({
  sessionsListFor: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  sessionsListFor: bridgeMocks.sessionsListFor,
}));

function makePipeline(overrides: Record<string, unknown> = {}) {
  return {
    viewModel: createEmptyPipelineViewModel(),
    sessions: [],
    currentSessionId: "default",
    sendPrompt: vi.fn().mockResolvedValue(undefined),
    startRun: vi.fn().mockResolvedValue(undefined),
    pauseRun: vi.fn(),
    resumeRun: vi.fn(),
    stopRun: vi.fn(),
    toggleMode: vi.fn(),
    newSession: vi.fn(),
    switchSession: vi.fn().mockResolvedValue(undefined),
    selectHypothesis: vi.fn(),
    ...overrides,
  };
}

describe("AppShell", () => {
  beforeEach(() => {
    localStorage.clear();
    bridgeMocks.sessionsListFor.mockReset();
    bridgeMocks.sessionsListFor.mockResolvedValue({ sessions: [] });
  });

  it("renders brand, workspace, and the function-rail modules", () => {
    renderUi(
      <AppShell currentRoot="C:/projects/titanic" recentRoots={[]} onSwitchWorkspace={vi.fn()} onSelectWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
    );

    expect(screen.getAllByText("Athena").length).toBeGreaterThan(0);
    expect(screen.getAllByText("titanic").length).toBeGreaterThan(0);
    for (const label of ["研究树", "实验", "数据分析", "模型轨迹", "报告", "设置"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("toggles the context sidebar via the top-bar control", () => {
    renderUi(
      <AppShell currentRoot={null} recentRoots={[]} onSwitchWorkspace={vi.fn()} onSelectWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
    );

    expect(screen.getByText("新会话")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "折叠侧栏" }));
    expect(screen.queryByText("新会话")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "展开侧栏" }));
    expect(screen.getByText("新会话")).toBeInTheDocument();
  });

  it("lists sessions and switches on select", () => {
    const switchSession = vi.fn().mockResolvedValue(undefined);

    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [
            { id: "default", title: "新会话" },
            { id: "s-1", title: "分类 · f1_macro" },
          ],
          currentSessionId: "default",
          switchSession,
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "分类 · f1_macro" }));
    expect(switchSession).toHaveBeenCalledWith("s-1");
  });

  it("renders an empty state for a workspace without sessions", () => {
    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({ sessions: [] }) as never}
      />,
    );

    expect(screen.getByText("暂无会话")).toBeInTheDocument();
    // 只剩顶部的「新会话」按钮，不再有一行凭空占位的会话。
    expect(screen.getAllByText("新会话")).toHaveLength(1);
  });

  it("offers deletion for the default session too", () => {
    const deleteSession = vi.fn();

    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [{ id: "default", title: "默认会话" }],
          currentSessionId: "default",
          deleteSession,
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "删除会话 默认会话" }));
    expect(deleteSession).toHaveBeenCalledWith("default");
  });

  it("keeps workspace order stable and targets a session in another workspace", async () => {
    const onSelectWorkspace = vi.fn();
    bridgeMocks.sessionsListFor.mockImplementation(async (root: string) => ({
      sessions: root === "C:/gamma" ? ["s-gamma"] : [],
    }));
    localStorage.setItem(
      "athena-session-titles:C:/gamma",
      JSON.stringify({ "s-gamma": "s-gamma" }),
    );

    renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={["C:/alpha", "C:/beta", "C:/gamma"]}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={onSelectWorkspace}
        pipeline={makePipeline() as never}
      />,
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "s-gamma" })).toBeInTheDocument());
    const headings = screen
      .getAllByRole("button")
      .filter((button) => ["alpha", "beta", "gamma"].includes(button.textContent ?? ""));
    expect(headings.map((heading) => heading.textContent)).toEqual(["alpha", "beta", "gamma"]);

    fireEvent.click(screen.getByRole("button", { name: "s-gamma" }));
    expect(onSelectWorkspace).toHaveBeenCalledWith("C:/gamma", "s-gamma");
  });

  it("keeps the workspace switch action in a footer outside the scroll region", () => {
    renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={["C:/beta"]}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline() as never}
      />,
    );

    const scrollRegion = screen.getByTestId("workspace-scroll");
    const switchButton = screen.getByRole("button", { name: "切换工作区" });
    const footer = switchButton.closest("footer");
    expect(footer).not.toBeNull();
    expect(scrollRegion).not.toContainElement(switchButton);
    expect(within(footer as HTMLElement).getByRole("button", { name: "切换工作区" })).toBe(switchButton);
  });
});
