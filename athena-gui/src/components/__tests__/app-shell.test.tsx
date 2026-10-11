import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, fireEvent, within } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { AppShell } from "../shell/AppShell";
import { createEmptyPipelineViewModel } from "../../types/ui";

const bridgeMocks = vi.hoisted(() => ({
  sessionsListFor: vi.fn(),
}));

const storageMocks = vi.hoisted(() => ({
  loadWorkspaceSessions: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  sessionsListFor: bridgeMocks.sessionsListFor,
}));

vi.mock("../../lib/workspaceStorage", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/workspaceStorage")>();
  storageMocks.loadWorkspaceSessions.mockImplementation(actual.loadWorkspaceSessions);
  return {
    ...actual,
    loadWorkspaceSessions: storageMocks.loadWorkspaceSessions,
  };
});

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
    storageMocks.loadWorkspaceSessions.mockClear();
  });

  it("renders brand, workspace, and the function-rail modules", () => {
    renderUi(
      <AppShell currentRoot="C:/projects/titanic" recentRoots={[]} switching={false} onSwitchWorkspace={vi.fn()} onSelectWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
    );

    expect(screen.getAllByText("Athena").length).toBeGreaterThan(0);
    expect(screen.getAllByText("titanic").length).toBeGreaterThan(0);
    for (const label of ["研究树", "实验", "数据分析", "模型轨迹", "报告", "设置"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("toggles the context sidebar via the top-bar control", () => {
    renderUi(
      <AppShell currentRoot={null} recentRoots={[]} switching={false} onSwitchWorkspace={vi.fn()} onSelectWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
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
        switching={false}
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
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({ sessions: [] }) as never}
      />,
    );

    expect(screen.getByText("暂无会话")).toBeInTheDocument();
    // 只剩顶部的「新会话」按钮，不再有一行凭空占位的会话。
    expect(screen.getAllByText("新会话")).toHaveLength(1);
  });

  it("offers deletion for the default session too", async () => {
    const deleteSession = vi.fn();

    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [{ id: "default", title: "默认会话" }],
          currentSessionId: "default",
          deleteSession,
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "更多操作 默认会话" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    // Deleting is destructive: nothing happens until the confirmation is accepted.
    expect(deleteSession).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "删除会话？" });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    });
    expect(deleteSession).toHaveBeenCalledWith("default");
  });

  it("keeps the session when deletion is cancelled", async () => {
    const deleteSession = vi.fn();

    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [{ id: "s-1", title: "流失预测" }],
          currentSessionId: "s-1",
          deleteSession,
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "更多操作 流失预测" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
    await act(async () => {
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }));
    });
    expect(deleteSession).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders cached sessions synchronously in stable workspace order without an RPC", () => {
    const onSelectWorkspace = vi.fn();
    localStorage.setItem(
      "athena.workspace.sessions:C:/gamma",
      JSON.stringify([{ id: "s-gamma", title: "Cached Gamma" }]),
    );

    renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={["C:/alpha", "C:/beta", "C:/gamma"]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={onSelectWorkspace}
        pipeline={makePipeline() as never}
      />,
    );

    expect(screen.getByRole("button", { name: "Cached Gamma" })).toBeInTheDocument();
    const headings = screen
      .getAllByRole("button")
      .filter((button) => ["alpha", "beta", "gamma"].includes(button.textContent ?? ""));
    expect(headings.map((heading) => heading.textContent)).toEqual(["alpha", "beta", "gamma"]);

    expect(bridgeMocks.sessionsListFor).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Cached Gamma" }));
    expect(onSelectWorkspace).toHaveBeenCalledWith("C:/gamma", "s-gamma");
  });

  it("renders an empty state synchronously for an uncached workspace without an RPC", () => {
    renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={["C:/beta", "C:/gamma"]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [{ id: "s-current", title: "Current" }],
          currentSessionId: "s-current",
        }) as never}
      />,
    );

    expect(screen.getByRole("button", { name: "gamma" })).toBeInTheDocument();
    expect(screen.getByText("暂无会话")).toBeInTheDocument();
    expect(bridgeMocks.sessionsListFor).not.toHaveBeenCalled();
  });

  it("does not reload cached workspace summaries for an unrelated streamed rerender", () => {
    const recentRoots = ["C:/beta", "C:/gamma"];
    localStorage.setItem(
      "athena.workspace.sessions:C:/gamma",
      JSON.stringify([{ id: "s-gamma", title: "Cached Gamma" }]),
    );
    const { rerender } = renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={recentRoots}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline() as never}
      />,
    );

    expect(storageMocks.loadWorkspaceSessions).toHaveBeenCalledOnce();
    expect(storageMocks.loadWorkspaceSessions).toHaveBeenCalledWith("C:/gamma");

    rerender(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={recentRoots}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          viewModel: {
            ...createEmptyPipelineViewModel(),
            phase: "SEARCH",
            status: "running",
          },
        }) as never}
      />,
    );

    expect(document.querySelector('[data-status="running"]')).toBeInTheDocument();
    expect(storageMocks.loadWorkspaceSessions).toHaveBeenCalledOnce();
  });

  it("keeps workspace and session navigation interactive while switching", () => {
    const onSwitchWorkspace = vi.fn();
    const onSelectWorkspace = vi.fn();
    const switchSession = vi.fn().mockResolvedValue(undefined);
    localStorage.setItem(
      "athena.workspace.sessions:C:/gamma",
      JSON.stringify([{ id: "s-gamma", title: "s-gamma" }]),
    );

    renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={["C:/beta", "C:/gamma"]}
        switching
        onSwitchWorkspace={onSwitchWorkspace}
        onSelectWorkspace={onSelectWorkspace}
        pipeline={makePipeline({
          sessions: [{ id: "s-current", title: "s-current" }],
          currentSessionId: "s-current",
          switchSession,
        }) as never}
      />,
    );

    expect(screen.getByRole("button", { name: "s-gamma" })).toBeInTheDocument();
    const otherWorkspace = screen.getByRole("button", { name: "gamma" });
    const otherSession = screen.getByRole("button", { name: "s-gamma" });
    const workspacePicker = screen.getByRole("button", { name: "切换工作区" });
    expect(otherWorkspace).not.toBeDisabled();
    expect(otherSession).not.toBeDisabled();
    expect(workspacePicker).not.toBeDisabled();

    fireEvent.click(otherWorkspace);
    fireEvent.click(otherSession);
    fireEvent.click(workspacePicker);
    expect(onSelectWorkspace).toHaveBeenNthCalledWith(1, "C:/gamma");
    expect(onSelectWorkspace).toHaveBeenNthCalledWith(2, "C:/gamma", "s-gamma");
    expect(onSwitchWorkspace).toHaveBeenCalledOnce();

    const currentSession = screen.getByRole("button", { name: "s-current" });
    expect(currentSession).not.toBeDisabled();
    fireEvent.click(currentSession);
    expect(switchSession).toHaveBeenCalledWith("s-current");
  });

  it("keeps the workspace switch action in a footer outside the scroll region", () => {
    renderUi(
      <AppShell
        currentRoot="C:/beta"
        recentRoots={["C:/beta"]}
        switching={false}
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

  it("navigates between views without rebuilding the session runtime", () => {
    const switchSession = vi.fn().mockResolvedValue(undefined);

    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({ switchSession }) as never}
      />,
    );

    const research = screen.getByRole("button", { name: "研究树" });
    fireEvent.click(research);
    expect(research).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "研究树" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "对话" }));
    expect(screen.getByRole("button", { name: "对话" })).toHaveAttribute("aria-current", "page");
    // Returning to the conversation must not call session_switch: that rebuilds the
    // runtime and cancels any clarification question that is still pending.
    expect(switchSession).not.toHaveBeenCalled();
  });

  it("opens settings as a dialog over the current view", () => {
    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline() as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    expect(screen.getByRole("dialog", { name: "设置" })).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "设置" })).not.toBeInTheDocument();
  });

  it("asks for confirmation before stopping a run", async () => {
    const stopRun = vi.fn().mockResolvedValue(undefined);
    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          stopRun,
          runActive: true,
          viewModel: { ...createEmptyPipelineViewModel(), phase: "SEARCH", status: "running" },
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    expect(stopRun).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(within(screen.getByRole("dialog", { name: "停止当前研究？" })).getByRole("button", { name: "停止研究" }));
    });
    expect(stopRun).toHaveBeenCalledOnce();
  });

  it("toggles the research inspector from the conversation header", () => {
    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline() as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "研究概览" }));
    expect(screen.getByRole("complementary", { name: "研究概览" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭研究概览" }));
    expect(screen.queryByRole("complementary", { name: "研究概览" })).not.toBeInTheDocument();
  });

  it("renames a session from its menu", () => {
    const renameSession = vi.fn();
    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          sessions: [{ id: "s-1", title: "旧标题" }],
          currentSessionId: "s-1",
          renameSession,
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "更多操作 旧标题" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
    const input = screen.getByRole("textbox", { name: "会话名称" });
    fireEvent.change(input, { target: { value: "新标题" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(renameSession).toHaveBeenCalledWith("s-1", "新标题");
  });

  it("flags a pending question on the conversation entry while another view is open", () => {
    renderUi(
      <AppShell
        currentRoot="C:/projects/titanic"
        recentRoots={[]}
        switching={false}
        onSwitchWorkspace={vi.fn()}
        onSelectWorkspace={vi.fn()}
        pipeline={makePipeline({
          humanRequests: [
            {
              request_id: "req-1",
              session_id: "default",
              scope_id: "draft-1",
              scope_kind: "clarification",
              prompt: "目标列？",
              allow_custom: true,
              allow_skip: true,
              created_at: "2026-10-10T10:00:00Z",
              expires_at: "2099-10-10T10:02:00Z",
            },
          ],
        }) as never}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "实验" }));
    expect(screen.getByText("Athena 有一个问题正在等待你回答。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "前往对话" }));
    expect(screen.getByRole("region", { name: "需要你的确认" })).toBeInTheDocument();
  });
});
