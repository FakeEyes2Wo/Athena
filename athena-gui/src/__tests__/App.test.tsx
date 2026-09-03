import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createEmptyPipelineViewModel } from "../types/ui";

const bridgeMocks = vi.hoisted(() => ({
  settingsGet: vi.fn(),
  setProjectRoot: vi.fn(),
}));

const pipelineMocks = vi.hoisted(() => ({
  usePipeline: vi.fn(),
}));

vi.mock("../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/tauri-bridge")>()),
  settingsGet: bridgeMocks.settingsGet,
  setProjectRoot: bridgeMocks.setProjectRoot,
}));

vi.mock("../hooks/usePipeline", () => ({
  usePipeline: pipelineMocks.usePipeline,
}));

import App from "../App";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function settings(projectRoot: string) {
  return { project_root: projectRoot };
}

function pipeline() {
  return {
    viewModel: createEmptyPipelineViewModel(),
    sessions: [{ id: "current", title: "Current Session" }],
    currentSessionId: "current",
    sendPrompt: vi.fn().mockResolvedValue(undefined),
    startRun: vi.fn().mockResolvedValue(undefined),
    pauseRun: vi.fn(),
    resumeRun: vi.fn(),
    stopRun: vi.fn(),
    toggleMode: vi.fn(),
    newSession: vi.fn(),
    deleteSession: vi.fn(),
    switchSession: vi.fn().mockResolvedValue(undefined),
    selectHypothesis: vi.fn(),
  };
}

describe("App workspace switching", () => {
  beforeEach(() => {
    localStorage.clear();
    bridgeMocks.settingsGet.mockReset();
    bridgeMocks.setProjectRoot.mockReset();
    pipelineMocks.usePipeline.mockReset();
    pipelineMocks.usePipeline.mockReturnValue(pipeline());
  });

  it("coalesces rapid sidebar workspace switches without committing the superseded root", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a", "/b", "/c"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    const firstSwitch = deferred<ReturnType<typeof settings>>();
    const finalSwitch = deferred<ReturnType<typeof settings>>();
    bridgeMocks.setProjectRoot
      .mockReturnValueOnce(firstSwitch.promise)
      .mockReturnValueOnce(finalSwitch.promise);

    render(<App />);

    const workspaceB = await screen.findByRole("button", { name: "b" });
    const workspaceC = screen.getByRole("button", { name: "c" });
    expect(workspaceB).toBeEnabled();
    expect(workspaceC).toBeEnabled();

    fireEvent.click(workspaceB);
    fireEvent.click(workspaceC);

    expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.setProjectRoot).toHaveBeenLastCalledWith("/b");
    expect(workspaceB).toBeEnabled();
    expect(workspaceC).toBeEnabled();

    await act(async () => {
      firstSwitch.resolve(settings("/b"));
      await firstSwitch.promise;
    });

    await waitFor(() => {
      expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(2);
      expect(bridgeMocks.setProjectRoot).toHaveBeenLastCalledWith("/c");
    });
    expect(pipelineMocks.usePipeline.mock.calls.map(([root]) => root)).not.toContain("/b");

    await act(async () => {
      finalSwitch.resolve(settings("/c"));
      await finalSwitch.promise;
    });

    await waitFor(() => expect(screen.getByRole("button", { name: "c" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "b" })).toBeEnabled();
    expect(pipelineMocks.usePipeline).toHaveBeenLastCalledWith("/c", null);
  });

  it("keeps the picker dismissible and usable through repeated final sidebar switch failures", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a", "/b"]));
    localStorage.setItem(
      "athena.workspace.sessions:/b",
      JSON.stringify([{ id: "target", title: "Target Session" }]),
    );
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    const firstSwitch = deferred<ReturnType<typeof settings>>();
    const retrySwitch = deferred<ReturnType<typeof settings>>();
    bridgeMocks.setProjectRoot
      .mockReturnValueOnce(firstSwitch.promise)
      .mockReturnValueOnce(retrySwitch.promise);

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "b" }));
    expect(bridgeMocks.setProjectRoot).toHaveBeenNthCalledWith(1, "/b");

    await act(async () => {
      firstSwitch.reject(new Error("root unavailable"));
      await firstSwitch.promise.catch(() => undefined);
    });

    expect(await screen.findByText("root unavailable")).toBeVisible();
    expect(screen.getByRole("button", { name: "继续使用当前" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "继续使用当前" }));
    expect(await screen.findByRole("button", { name: "Current Session" })).toBeVisible();
    expect(screen.queryByText("root unavailable")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "切换工作区" }));
    expect(screen.getByText("root unavailable")).toBeVisible();
    fireEvent.click(screen.getByTitle("/b"));
    expect(bridgeMocks.setProjectRoot).toHaveBeenNthCalledWith(2, "/b");
    expect(screen.queryByText("root unavailable")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "/a" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "/b" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "浏览目录…" })).toBeEnabled();
    const manualPath = screen.getByPlaceholderText("输入项目目录的绝对路径…");
    expect(manualPath).toBeEnabled();
    fireEvent.change(manualPath, { target: { value: "/c" } });
    expect(screen.getByRole("button", { name: "打开" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "继续使用当前" })).toBeDisabled();

    await act(async () => {
      retrySwitch.reject(new Error("root unavailable"));
      await retrySwitch.promise.catch(() => undefined);
    });

    await waitFor(() => expect(screen.getByText("root unavailable")).toBeVisible());
    expect(screen.getByRole("button", { name: "继续使用当前" })).toBeEnabled();
  });
});
