import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_GUI_SETTINGS } from "../../lib/tauri-bridge";
import { useWorkspace } from "../useWorkspace";

const bridgeMocks = vi.hoisted(() => ({
  settingsGet: vi.fn(),
  setProjectRoot: vi.fn(),
}));

vi.mock("../../lib/tauri-bridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/tauri-bridge")>()),
  settingsGet: bridgeMocks.settingsGet,
  setProjectRoot: bridgeMocks.setProjectRoot,
}));

function settings(projectRoot: string) {
  return { ...DEFAULT_GUI_SETTINGS, project_root: projectRoot };
}

describe("useWorkspace", () => {
  beforeEach(() => {
    localStorage.clear();
    bridgeMocks.settingsGet.mockReset();
    bridgeMocks.setProjectRoot.mockReset();
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
});
