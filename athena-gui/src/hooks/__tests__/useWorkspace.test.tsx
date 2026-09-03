import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_GUI_SETTINGS } from "../../lib/tauri-bridge";
import { useWorkspace } from "../useWorkspace";

const bridgeMocks = vi.hoisted(() => ({
  settingsGet: vi.fn(),
  setProjectRoot: vi.fn(),
  selectWorkspaceDirectory: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  settingsGet: bridgeMocks.settingsGet,
  setProjectRoot: bridgeMocks.setProjectRoot,
}));

vi.mock("../../lib/workspaceDialog", () => ({
  selectWorkspaceDirectory: bridgeMocks.selectWorkspaceDirectory,
}));

function settings(projectRoot: string) {
  return { ...DEFAULT_GUI_SETTINGS, project_root: projectRoot };
}

describe("useWorkspace", () => {
  beforeEach(() => {
    localStorage.clear();
    bridgeMocks.settingsGet.mockReset();
    bridgeMocks.setProjectRoot.mockReset();
    bridgeMocks.selectWorkspaceDirectory.mockReset();
  });

  it("keeps the picker open when the backend root is not remembered", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/new"));

    const { result } = renderHook(() => useWorkspace());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.currentRoot).toBe("/new");
    expect(result.current.pickerOpen).toBe(true);
  });

  it("closes the picker when the backend root is remembered", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a", "/b"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));

    const { result } = renderHook(() => useWorkspace());

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.pickerOpen).toBe(false);
  });

  it("records a requested session without reordering known roots", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a", "/b"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    bridgeMocks.setProjectRoot.mockResolvedValue(settings("/b"));
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    await act(async () => result.current.switchTo("/b", "s-2"));

    expect(result.current.requestedSessionId).toBe("s-2");
    expect(result.current.recentRoots).toEqual(["/a", "/b"]);
  });

  it("switches to a directory selected by the native dialog", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    bridgeMocks.selectWorkspaceDirectory.mockResolvedValue("C:/chosen");
    bridgeMocks.setProjectRoot.mockResolvedValue(settings("C:/chosen"));
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    await act(async () => result.current.browse());

    expect(bridgeMocks.selectWorkspaceDirectory).toHaveBeenCalledWith("/a");
    expect(bridgeMocks.setProjectRoot).toHaveBeenCalledWith("C:/chosen");
    expect(result.current.currentRoot).toBe("C:/chosen");
  });

  it("exposes native dialog errors and clears browsing state", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    bridgeMocks.selectWorkspaceDirectory.mockRejectedValue(
      new Error("native dialog unavailable"),
    );
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    await act(async () => result.current.browse());

    expect(result.current.error).toBe("native dialog unavailable");
    expect(result.current.browsing).toBe(false);
    expect(bridgeMocks.setProjectRoot).not.toHaveBeenCalled();
  });

  it("leaves the workspace unchanged when native browsing is cancelled", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    let cancelBrowse!: () => void;
    bridgeMocks.selectWorkspaceDirectory.mockImplementation(
      () => new Promise<null>((resolve) => {
        cancelBrowse = () => resolve(null);
      }),
    );
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    let browsing!: Promise<void>;
    act(() => {
      browsing = result.current.browse();
    });
    await waitFor(() => expect(result.current.browsing).toBe(true));
    expect(result.current.currentRoot).toBe("/a");
    expect(bridgeMocks.setProjectRoot).not.toHaveBeenCalled();

    await act(async () => {
      cancelBrowse();
      await browsing;
    });

    expect(result.current.browsing).toBe(false);
    expect(result.current.currentRoot).toBe("/a");
    expect(bridgeMocks.setProjectRoot).not.toHaveBeenCalled();
  });

  it("prevents duplicate native dialogs while browsing", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    let finishBrowse!: (value: string | null) => void;
    bridgeMocks.selectWorkspaceDirectory.mockImplementation(
      () => new Promise((resolve) => {
        finishBrowse = resolve;
      }),
    );
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    let firstBrowse!: Promise<void>;
    act(() => {
      firstBrowse = result.current.browse();
    });
    await waitFor(() => expect(result.current.browsing).toBe(true));
    await act(async () => result.current.browse());

    expect(bridgeMocks.selectWorkspaceDirectory).toHaveBeenCalledOnce();

    await act(async () => {
      finishBrowse(null);
      await firstBrowse;
    });
    expect(result.current.browsing).toBe(false);
  });
});
