import { beforeEach, describe, expect, it, vi } from "vitest";
import { selectWorkspaceDirectory } from "../workspaceDialog";

const mocks = vi.hoisted(() => ({
  isTauri: vi.fn(),
  open: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: mocks.isTauri,
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: mocks.open,
}));

describe("selectWorkspaceDirectory", () => {
  beforeEach(() => {
    mocks.isTauri.mockReset();
    mocks.open.mockReset();
    mocks.isTauri.mockReturnValue(true);
  });

  it("opens one native directory picker at the requested default path", async () => {
    mocks.open.mockResolvedValue("C:/work/selected");

    await expect(selectWorkspaceDirectory("C:/work")).resolves.toBe(
      "C:/work/selected",
    );
    expect(mocks.open).toHaveBeenCalledWith({
      directory: true,
      multiple: false,
      defaultPath: "C:/work",
    });
  });

  it("returns null when native selection is cancelled", async () => {
    mocks.open.mockResolvedValue(null);

    await expect(selectWorkspaceDirectory()).resolves.toBeNull();
  });

  it("does not open a native dialog in browser mode", async () => {
    mocks.isTauri.mockReturnValue(false);

    await expect(selectWorkspaceDirectory("/work")).resolves.toBeNull();
    expect(mocks.open).not.toHaveBeenCalled();
  });
});
