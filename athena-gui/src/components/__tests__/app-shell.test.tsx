import { describe, expect, it, vi } from "vitest";
import { screen, fireEvent } from "@testing-library/react";
import { renderUi } from "../../test/render";
import { AppShell } from "../shell/AppShell";
import { createEmptyPipelineViewModel } from "../../types/ui";

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
  it("renders brand, workspace, and the function-rail modules", () => {
    renderUi(
      <AppShell currentRoot="C:/projects/titanic" onSwitchWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
    );

    expect(screen.getAllByText("Athena").length).toBeGreaterThan(0);
    expect(screen.getAllByText("titanic").length).toBeGreaterThan(0);
    for (const label of ["研究树", "实验", "数据分析", "模型轨迹", "报告", "设置"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("toggles the context sidebar via the top-bar control", () => {
    renderUi(
      <AppShell currentRoot={null} onSwitchWorkspace={vi.fn()} pipeline={makePipeline() as never} />,
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
        onSwitchWorkspace={vi.fn()}
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
});
