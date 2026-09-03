import { beforeEach, describe, expect, it, vi } from "vitest";
import { selectWorkspaceDirectory } from "../workspaceDialog";

const mocks = vi.hoisted(() => ({
  isTauri: vi.fn(),
  invoke: vi.fn(),
  open: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: mocks.isTauri,
  invoke: mocks.invoke,
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: mocks.open,
}));

describe("selectWorkspaceDirectory", () => {
  beforeEach(() => {
    mocks.isTauri.mockReset();
    mocks.invoke.mockReset();
    mocks.open.mockReset();
    mocks.isTauri.mockReturnValue(true);
    mocks.invoke.mockResolvedValue("C:/safe");
  });

  it("opens the native picker at the trusted resolved directory", async () => {
    mocks.open.mockResolvedValue("C:/selected");

    await expect(selectWorkspaceDirectory("C:/deleted")).resolves.toBe(
      "C:/selected",
    );
    expect(mocks.invoke).toHaveBeenCalledWith(
      "workspace_dialog_start_directory",
      { requested: "C:/deleted" },
    );
    expect(mocks.open).toHaveBeenCalledWith({
      directory: true,
      multiple: false,
      defaultPath: "C:/safe",
    });
  });

  it("returns null when native selection is cancelled", async () => {
    mocks.open.mockResolvedValue(null);

    await expect(selectWorkspaceDirectory()).resolves.toBeNull();
  });

  it("omits an unsafe default when the resolver has no usable directory", async () => {
    mocks.invoke.mockResolvedValue(null);
    mocks.open.mockResolvedValue(null);

    await expect(selectWorkspaceDirectory("C:/deleted")).resolves.toBeNull();
    expect(mocks.open).toHaveBeenCalledWith({
      directory: true,
      multiple: false,
    });
  });

  it("does not open a native dialog in browser mode", async () => {
    mocks.isTauri.mockReturnValue(false);

    await expect(selectWorkspaceDirectory("/work")).resolves.toBeNull();
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.open).not.toHaveBeenCalled();
  });
});
