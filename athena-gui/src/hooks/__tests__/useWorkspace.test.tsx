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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
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

  it("runs one root change at a time and coalesces queued intent to the latest switch", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    const active = deferred<ReturnType<typeof settings>>();
    const latest = deferred<ReturnType<typeof settings>>();
    bridgeMocks.setProjectRoot.mockImplementation((root: string) => {
      if (root === "/b") return active.promise;
      if (root === "/d") return latest.promise;
      throw new Error(`unexpected workspace switch: ${root}`);
    });
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    let activeSwitch!: Promise<void>;
    let supersededSwitch!: Promise<void>;
    let latestSwitch!: Promise<void>;
    act(() => {
      activeSwitch = result.current.switchTo("/b", "s-b");
      supersededSwitch = result.current.switchTo("/c", "s-c");
      latestSwitch = result.current.switchTo("/d", "s-d");
    });

    expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(1);
    expect(bridgeMocks.setProjectRoot).toHaveBeenNthCalledWith(1, "/b");

    await act(async () => {
      active.resolve(settings("/b"));
    });
    await waitFor(() => expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(2));
    expect(bridgeMocks.setProjectRoot).toHaveBeenNthCalledWith(2, "/d");
    expect(bridgeMocks.setProjectRoot).not.toHaveBeenCalledWith("/c");
    expect(result.current.currentRoot).toBe("/a");
    expect(result.current.requestedSessionId).toBeNull();

    await act(async () => {
      latest.resolve(settings("/d"));
      await Promise.all([activeSwitch, supersededSwitch, latestSwitch]);
    });

    expect(result.current.currentRoot).toBe("/d");
    expect(result.current.requestedSessionId).toBe("s-d");
    expect(result.current.recentRoots).toEqual(["/d", "/a"]);
    expect(result.current.pickerOpen).toBe(false);
    expect(result.current.switching).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("ignores a superseded failure and continues with the latest queued switch", async () => {
    localStorage.setItem("athena.workspace.recent", JSON.stringify(["/a"]));
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    const active = deferred<ReturnType<typeof settings>>();
    const latest = deferred<ReturnType<typeof settings>>();
    bridgeMocks.setProjectRoot.mockImplementation((root: string) => {
      if (root === "/b") return active.promise;
      if (root === "/d") return latest.promise;
      throw new Error(`unexpected workspace switch: ${root}`);
    });
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    let activeSwitch!: Promise<void>;
    let latestSwitch!: Promise<void>;
    act(() => {
      activeSwitch = result.current.switchTo("/b", "s-b");
      result.current.switchTo("/c", "s-c");
      latestSwitch = result.current.switchTo("/d", "s-d");
    });
    await waitFor(() => expect(result.current.switching).toBe(true));
    expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(1);

    await act(async () => {
      active.reject(new Error("stale failure"));
    });
    await waitFor(() => expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(2));

    expect(result.current.currentRoot).toBe("/a");
    expect(result.current.requestedSessionId).toBeNull();
    expect(result.current.switching).toBe(true);
    expect(result.current.error).toBeNull();
    expect(bridgeMocks.setProjectRoot).toHaveBeenNthCalledWith(2, "/d");
    expect(bridgeMocks.setProjectRoot).not.toHaveBeenCalledWith("/c");

    await act(async () => {
      latest.resolve(settings("/d"));
      await Promise.all([activeSwitch, latestSwitch]);
    });
    expect(result.current.currentRoot).toBe("/d");
    expect(result.current.requestedSessionId).toBe("s-d");
    expect(result.current.switching).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("clears switching after a final rejection so a later switch can retry", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    const failed = deferred<ReturnType<typeof settings>>();
    bridgeMocks.setProjectRoot.mockReturnValueOnce(failed.promise);
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    let failedSwitch!: Promise<void>;
    act(() => {
      failedSwitch = result.current.switchTo("/b", "s-b");
    });
    await act(async () => {
      failed.reject(new Error("root unavailable"));
      await failedSwitch;
    });

    expect(result.current.currentRoot).toBe("/a");
    expect(result.current.switching).toBe(false);
    expect(result.current.error).toBe("root unavailable");

    bridgeMocks.setProjectRoot.mockResolvedValueOnce(settings("/retry"));
    await act(async () => result.current.switchTo("/retry", "s-retry"));

    expect(bridgeMocks.setProjectRoot).toHaveBeenLastCalledWith("/retry");
    expect(result.current.currentRoot).toBe("/retry");
    expect(result.current.requestedSessionId).toBe("s-retry");
    expect(result.current.switching).toBe(false);
    expect(result.current.error).toBeNull();
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

  it("opens the native browser while switching and queues its selected workspace", async () => {
    bridgeMocks.settingsGet.mockResolvedValue(settings("/a"));
    bridgeMocks.selectWorkspaceDirectory.mockResolvedValue("/d");
    const active = deferred<ReturnType<typeof settings>>();
    const selected = deferred<ReturnType<typeof settings>>();
    bridgeMocks.setProjectRoot.mockImplementation((root: string) =>
      root === "/b" ? active.promise : selected.promise,
    );
    const { result } = renderHook(() => useWorkspace());
    await waitFor(() => expect(result.current.ready).toBe(true));

    let activeSwitch!: Promise<void>;
    let browse!: Promise<void>;
    act(() => {
      activeSwitch = result.current.switchTo("/b");
    });
    await waitFor(() => expect(result.current.switching).toBe(true));
    act(() => {
      browse = result.current.browse();
    });

    await waitFor(() => expect(bridgeMocks.selectWorkspaceDirectory).toHaveBeenCalledWith("/a"));
    expect(bridgeMocks.setProjectRoot).toHaveBeenCalledTimes(1);

    await act(async () => {
      active.resolve(settings("/b"));
    });
    await waitFor(() => expect(bridgeMocks.setProjectRoot).toHaveBeenNthCalledWith(2, "/d"));
    await act(async () => {
      selected.resolve(settings("/d"));
      await Promise.all([activeSwitch, browse]);
    });

    expect(result.current.currentRoot).toBe("/d");
    expect(result.current.browsing).toBe(false);
  });
});
